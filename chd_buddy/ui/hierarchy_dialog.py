"""Okno zarządzania zależnościami rodzic → dzieci DAT-ów PER PLATFORMA.

DAT-y tej samej platformy (np. pełny Redump PS2 + 1G1R PS2 + tłumaczenia PS2)
mogą leżeć w różnych katalogach — hierarchia zachodzi między nimi, nie między
katalogami. To okno grupuje DAT-y po platformie i pozwala ustalić, który jest
rodzicem (trzyma pliki fizycznie), a które dziećmi (symlinki), oraz KATALOG
DOCELOWY każdego DAT-a (gdzie fizycznie lądują pliki/symlinki). Zapis:
_priorytet.txt (hierarchia) + _reguly.json (target per DAT).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.dirrules import DirRules, save_rule
from ..core.datstore import group_by_platform, save_priority
from ..core.i18n import tr


class HierarchyDialog(QDialog):
    def __init__(self, parent, entries, dat_root: Path, rom_root: Path):
        super().__init__(parent)
        self.setWindowTitle(tr("Zależności DAT-ów — rodzic → dzieci (per platforma)"))
        self.resize(940, 660)
        self.dat_root = dat_root
        self.rom_root = rom_root
        self.saved_path: Path | None = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr(
            "WSZYSTKIE platformy (także z jednym DAT-em). W obrębie platformy: "
            "góra = RODZIC (pliki fizyczne), niżej = dzieci (symlinki). "
            "DAT o innej nazwie (np. FinalBurn Neo - SNES Games) możesz "
            "PRZYPIĄĆ do platformy rodzica przyciskiem 🔗 — stanie się jej "
            "dzieckiem (hierarchia + wspólny format).")))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Platforma / DAT"), tr("ROM-ów"), tr("rola"),
                                   tr("katalog docelowy")])
        self.tree.setColumnWidth(0, 340)
        self.tree.setColumnWidth(1, 70)
        self.tree.setColumnWidth(2, 80)
        self.tree.itemDoubleClicked.connect(self._on_double)
        lay.addWidget(self.tree, 1)

        rules = DirRules(dat_root)
        groups = group_by_platform(entries, rules)   # z ręcznymi przypięciami
        multi = 0
        for plat in sorted(groups):
            ents = groups[plat]     # już w kolejności priorytetu z discover()
            if len(ents) > 1:
                multi += 1
            head = self._new_group(plat, expanded=len(ents) > 1)
            for i, e in enumerate(ents):
                ch = QTreeWidgetItem([e.name, str(e.rom_count),
                                      (tr("RODZIC") if i == 0 else tr("dziecko")),
                                      self._target_display(e)])
                ch.setData(0, Qt.ItemDataRole.UserRole, e)
                head.addChild(ch)
            self._refresh_head(head)
        lay.addWidget(QLabel(tr("Platform: {n} (z hierarchią: {m}; pojedyncze "
                             "zwinięte). 🔗 przypina DAT do innej platformy, "
                             "✂ przywraca własną.").format(n=len(groups), m=multi)))

        btns = QHBoxLayout()
        self.btn_up = QPushButton(tr("▲ wyżej"))
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down = QPushButton(tr("▼ niżej"))
        self.btn_down.clicked.connect(lambda: self._move(+1))
        self.btn_parent = QPushButton(tr("⭐ Ustaw jako rodzic"))
        self.btn_parent.clicked.connect(self._make_parent)
        self.btn_pin = QPushButton(tr("🔗 Przypnij do platformy…"))
        self.btn_pin.setToolTip(tr(
            "Wybrany DAT staje się DZIECKIEM wskazanej platformy — dostaje "
            "symlinki do plików jej rodzica i dziedziczy format, mimo że "
            "nazwa DAT-a jest inna (np. FinalBurn Neo → Nintendo SNES)."))
        self.btn_pin.clicked.connect(self._pin_to_platform)
        self.btn_unpin = QPushButton(tr("✂ Odepnij (własna platforma)"))
        self.btn_unpin.clicked.connect(self._unpin)
        self.btn_target = QPushButton(tr("📁 Zmień katalog docelowy…"))
        self.btn_target.clicked.connect(self._edit_target)
        for b in (self.btn_up, self.btn_down, self.btn_parent, self.btn_pin,
                  self.btn_unpin, self.btn_target):
            btns.addWidget(b)
        btns.addStretch()
        lay.addLayout(btns)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        # zmiany do zapisania: {nazwa_dat: target_rel} i {nazwa_dat: alias}
        self._target_edits: dict[str, str] = {}
        self._alias_edits: dict[str, str] = {}

    # --- węzły platform -------------------------------------------------------

    def _new_group(self, plat_key: str, expanded: bool) -> QTreeWidgetItem:
        head = QTreeWidgetItem([plat_key, "", "", ""])
        f = head.font(0)
        f.setBold(True)
        head.setFont(0, f)
        head.setFlags(head.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        head.setData(0, Qt.ItemDataRole.UserRole, ("plat", plat_key))
        self.tree.addTopLevelItem(head)
        head.setExpanded(expanded)
        return head

    def _refresh_head(self, head: QTreeWidgetItem) -> None:
        n = head.childCount()
        head.setText(1, f"{n} " + tr("DAT") + ("" if n == 1 else tr("-ów")))
        for i in range(n):
            head.child(i).setText(2, (tr("RODZIC") if i == 0 else tr("dziecko")))

    def _find_group(self, plat_key: str) -> QTreeWidgetItem | None:
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            d = it.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(d, tuple) and d[1] == plat_key:
                return it
        return None

    # --- przypinanie DAT-a do innej platformy ---------------------------------

    def _pin_to_platform(self) -> None:
        it, parent = self._selected_dat()
        if it is None:
            return
        entry = it.data(0, Qt.ItemDataRole.UserRole)
        cur_key = parent.data(0, Qt.ItemDataRole.UserRole)[1]
        plats = [self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[1]
                 for i in range(self.tree.topLevelItemCount())]
        choices = [p for p in plats if p != cur_key]
        if not choices:
            return
        chosen, ok = QInputDialog.getItem(
            self, tr("Przypnij do platformy"),
            f"DAT:  {entry.name}\n\nStanie się DZIECKIEM platformy:",
            choices, 0, False)
        if not ok or not chosen:
            return
        target = self._find_group(chosen)
        if target is None:
            return
        parent.takeChild(parent.indexOfChild(it))
        target.addChild(it)                     # na koniec = dziecko
        target.setExpanded(True)
        self._refresh_head(target)
        self._refresh_head(parent)
        if parent.childCount() == 0:            # pusta grupa znika
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(parent))
        self.tree.setCurrentItem(it)
        self._alias_edits[entry.name] = chosen

    def _unpin(self) -> None:
        from ..core.datstore import platform_key
        it, parent = self._selected_dat()
        if it is None:
            return
        entry = it.data(0, Qt.ItemDataRole.UserRole)
        own = platform_key(entry.name)
        cur_key = parent.data(0, Qt.ItemDataRole.UserRole)[1]
        if own == cur_key:
            return                              # już we własnej platformie
        parent.takeChild(parent.indexOfChild(it))
        self._refresh_head(parent)
        if parent.childCount() == 0:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(parent))
        target = self._find_group(own) or self._new_group(own, expanded=True)
        target.addChild(it)
        target.setExpanded(True)
        self._refresh_head(target)
        self.tree.setCurrentItem(it)
        self._alias_edits[entry.name] = ""      # "" = usuń regułę (domyślna)

    # --- katalog docelowy ---------------------------------------------------

    def _target_display(self, entry) -> str:
        """Katalog docelowy DAT-a względem ROM-ów (albo bezwzględny)."""
        try:
            return str(Path(entry.target_dir).relative_to(self.rom_root))
        except ValueError:
            return str(entry.target_dir)

    def _on_double(self, item, col) -> None:
        if item.parent() is not None and col == 3:
            self._edit_target()

    def _edit_target(self) -> None:
        it, _parent = self._selected_dat()
        if it is None:
            return
        entry = it.data(0, Qt.ItemDataRole.UserRole)
        cur = it.text(3)
        # najpierw wybór folderu (wygodniej), z opcją wpisania ręcznego
        chosen = QFileDialog.getExistingDirectory(
            self, tr("Katalog docelowy dla:") + f" {entry.name}",
            str(self.rom_root / cur) if cur else str(self.rom_root))
        if chosen:
            try:
                rel = str(Path(chosen).relative_to(self.rom_root))
            except ValueError:
                rel = chosen               # poza ROM-ami → ścieżka bezwzględna
        else:
            rel, ok = QInputDialog.getText(
                self, tr("Katalog docelowy"),
                tr("Ścieżka względem katalogu ROM-ów (np. ps2):"), text=cur)
            if not ok:
                return
            rel = rel.strip()
        it.setText(3, rel)
        self._target_edits[entry.name] = rel

    # --- hierarchia ----------------------------------------------------------

    def _selected_dat(self):
        it = self.tree.currentItem()
        if it is None or it.parent() is None:
            return None, None
        return it, it.parent()

    def _refresh_roles(self, platform_item: QTreeWidgetItem) -> None:
        for i in range(platform_item.childCount()):
            platform_item.child(i).setText(2, (tr("RODZIC") if i == 0 else tr("dziecko")))

    def _move(self, delta: int) -> None:
        it, parent = self._selected_dat()
        if it is None:
            return
        idx = parent.indexOfChild(it)
        new = idx + delta
        if 0 <= new < parent.childCount():
            parent.takeChild(idx)
            parent.insertChild(new, it)
            parent.setExpanded(True)
            self.tree.setCurrentItem(it)
            self._refresh_roles(parent)

    def _make_parent(self) -> None:
        it, parent = self._selected_dat()
        if it is None:
            return
        idx = parent.indexOfChild(it)
        parent.takeChild(idx)
        parent.insertChild(0, it)
        self.tree.setCurrentItem(it)
        self._refresh_roles(parent)

    def _save(self) -> None:
        names: list[str] = []
        for i in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(i)
            for j in range(head.childCount()):
                names.append(head.child(j).text(0))
        try:
            self.saved_path = save_priority(self.dat_root, names)
            for dat_name, target in self._target_edits.items():
                save_rule(self.dat_root, dat_name, {"target": target})
            # ręczne przypięcia platform ("" = powrót do własnej / usuń regułę)
            for dat_name, alias in self._alias_edits.items():
                save_rule(self.dat_root, dat_name, {"platform": alias})
        except OSError as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, tr("Zależności"), tr("Nie zapisano:") + f"\n{e}")
            return
        self.accept()

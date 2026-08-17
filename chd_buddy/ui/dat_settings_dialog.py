"""Ustawienia pojedynczego DAT-a (odpowiednik „Dir DAT Settings" z RomVaulta).

Zbiera w jednym oknie: katalog docelowy, układ wypakowanych gier
(podkatalog per gra / płasko), format przechowywania (zostaw / ZIP / 7z /
CHD / RVZ — dotyczy RODZICA), oraz reguły (skip, tylko kompletne, kopie→
symlinki, podmiana Japan→tłumaczenia). Zapis do _reguly.json pod nazwą DAT-a.

Uwaga: format „chd" ma sens dla systemów płytowych; GameCube/Wii używają
„rvz" (RomVault-owa reguła). Sam plik reguły tylko konfiguruje — właściwa
konwersja przy naprawie jest osobnym krokiem.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.dirrules import DirRules, save_rule, suggest_format
from ..core.i18n import tr

_FORMAT_LABELS = [
    ("keep", "zostaw jak jest (bez konwersji)"),
    ("auto", "AUTO wg systemu (płyta→CHD, GC/Wii→RVZ, kartridż→ZIP)"),
    ("extract", "wypakowane w podkatalogach"),
    ("zip", "ZIP (spakowane)"),
    ("7z", "7z (spakowane)"),
    ("chd", "CHD (systemy płytowe)"),
    ("rvz", "RVZ (GameCube / Wii)"),
]


class DatSettingsDialog(QDialog):
    def __init__(self, parent, entry, dat_root: Path, rom_root: Path = None,
                 *, is_child: bool = False, inherited_format: str = ""):
        super().__init__(parent)
        self.entry = entry
        self.dat_root = dat_root
        self.rom_root = Path(rom_root) if rom_root else None
        self.is_child = is_child
        self.setWindowTitle(tr("Ustawienia DAT-a:") + f" {entry.name}")
        self.resize(640, 360)
        eff = DirRules(dat_root).for_entry(entry)
        # wartości POCZĄTKOWE (kaskada) — zapisujemy tylko to, co user zmieni
        self._initial = {
            "target": eff.get("target", ""),
            "subdir_per_game": bool(eff.get("subdir_per_game", True)),
            "skip": bool(eff.get("skip", False)),
            "only_complete": bool(eff.get("only_complete", True)),
            "dedup_copies": bool(eff.get("dedup_copies", True)),
            "prefer_translations": bool(eff.get("prefer_translations", False)),
            "role": eff.get("role", "collection"),
            "format": (inherited_format or "keep") if is_child
                      else eff.get("format", "keep"),
        }

        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)

        form.addRow(QLabel(f"<b>{entry.name}</b>  ({entry.rom_count} "
                           + tr("ROM-ów") + ")"))

        # katalog docelowy (target) + wybór folderu
        self.e_target = QLineEdit(eff.get("target", ""))
        self.e_target.setPlaceholderText(tr("puste = wg struktury (rom_root/…/nazwa)"))
        tw = QWidget()
        trow = QHBoxLayout(tw)
        trow.setContentsMargins(0, 0, 0, 0)
        trow.addWidget(self.e_target, 1)
        btn_browse = QPushButton(tr("Wybierz…"))
        btn_browse.clicked.connect(self._browse_target)
        trow.addWidget(btn_browse)
        form.addRow(tr("Katalog docelowy (względem ROM-ów):"), tw)

        # układ wypakowanych gier
        self.chk_subdir = QCheckBox(tr("gry wieloplikowe w podkatalogu per gra "
                                    "(odznacz = płasko)"))
        self.chk_subdir.setChecked(bool(eff.get("subdir_per_game", True)))
        form.addRow(tr("Układ:"), self.chk_subdir)

        # format przechowywania (dotyczy rodzica; dzieci dziedziczą)
        self.cmb_format = QComboBox()
        for key, label in _FORMAT_LABELS:
            self.cmb_format.addItem(tr(label), key)
        cur = (inherited_format or "keep") if is_child else eff.get("format", "keep")
        i = self.cmb_format.findData(cur)
        self.cmb_format.setCurrentIndex(i if i >= 0 else 0)
        frow = QHBoxLayout()
        frow.addWidget(self.cmb_format, 1)
        btn_sugg = QPushButton(tr("Sugeruj"))
        btn_sugg.setToolTip(tr("CHD dla płyt; RVZ dla GameCube/Wii; reszta: zostaw."))
        btn_sugg.clicked.connect(self._suggest)
        frow.addWidget(btn_sugg)
        if is_child:
            # dziecko = symlinki do plików rodzica → MUSI mieć jego format
            self.cmb_format.setEnabled(False)
            btn_sugg.setEnabled(False)
            label = tr("Format przechowywania (dziedziczony z rodzica):")
        else:
            label = tr("Format przechowywania (rodzic):")
        form.addRow(label, frow)

        # reguły
        self.chk_skip = QCheckBox(tr("pomiń ten DAT (nie raportuj / nie buduj)"))
        self.chk_skip.setChecked(bool(eff.get("skip", False)))
        self.chk_complete = QCheckBox(tr("buduj tylko kompletne gry"))
        self.chk_complete.setChecked(bool(eff.get("only_complete", True)))
        self.chk_dedup = QCheckBox(tr("kopie potwierdzonych → symlinki"))
        self.chk_dedup.setChecked(bool(eff.get("dedup_copies", True)))
        self.chk_trans = QCheckBox(tr("podmieniaj wersje (Japan) na tłumaczenia [T-En]"))
        self.chk_trans.setChecked(bool(eff.get("prefer_translations", False)))
        for c in (self.chk_skip, self.chk_complete, self.chk_dedup,
                  self.chk_trans):
            form.addRow("", c)

        # ROLA DAT-u: zwykła kolekcja albo pula tłumaczeń
        self.cmb_role = QComboBox()
        self.cmb_role.addItem(tr("kolekcja (parent/child)"), "collection")
        self.cmb_role.addItem(tr("tłumaczenia (pula wariantów do podmiany)"),
                              "translations")
        ri = self.cmb_role.findData(eff.get("role", "collection"))
        self.cmb_role.setCurrentIndex(ri if ri >= 0 else 0)
        self.cmb_role.setToolTip(tr(
            "„tłumaczenia” = ten DAT to źródło fanowskich tłumaczeń; jego gry "
            "służą do podmiany w innych DAT-ach (nie jest celem podstawowym)."))
        form.addRow(tr("Rola DAT-u:"), self.cmb_role)

        if is_child:
            note_txt = tr("To DAT-DZIECKO swojej platformy — jego pliki to "
                        "symlinki do plików RODZICA, więc format jest "
                        "dziedziczony i niezmienialny tutaj. Zmień go w "
                        "ustawieniach DAT-a rodzica (albo w oknie hierarchii).")
        else:
            note_txt = tr("Format (chd/rvz/zip) to docelowy sposób przechowywania "
                        "RODZICA — dzieci platformy dziedziczą go automatycznie "
                        "(są symlinkami). Konwersja przy naprawie jest osobnym "
                        "krokiem; teraz zapisujesz preferencję.")
        note = QLabel(note_txt)
        note.setWordWrap(True)
        lay.addWidget(note)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse_target(self) -> None:
        base = self.rom_root or self.dat_root
        start = str(base / self.e_target.text()) if self.e_target.text() else str(base)
        chosen = QFileDialog.getExistingDirectory(
            self, tr("Katalog docelowy"), start)
        if not chosen:
            return
        if self.rom_root:
            try:
                self.e_target.setText(str(Path(chosen).relative_to(self.rom_root)))
                return
            except ValueError:
                pass
        self.e_target.setText(chosen)

    def _suggest(self) -> None:
        from ..core.shortcuts import detect_system
        from ..core.icons import clean_system_name
        short = detect_system(clean_system_name(self.entry.name)) \
            or detect_system(self.entry.name)
        fmt = suggest_format(short)
        i = self.cmb_format.findData(fmt)
        if i >= 0:
            self.cmb_format.setCurrentIndex(i)

    def _save(self) -> None:
        current = {
            "target": self.e_target.text().strip(),
            "subdir_per_game": self.chk_subdir.isChecked(),
            "skip": self.chk_skip.isChecked(),
            "only_complete": self.chk_complete.isChecked(),
            "dedup_copies": self.chk_dedup.isChecked(),
            "prefer_translations": self.chk_trans.isChecked(),
            "role": self.cmb_role.currentData(),
        }
        if not self.is_child:                 # dziecko dziedziczy format rodzica
            current["format"] = self.cmb_format.currentData()
        # zapisz TYLKO to, co user zmienił względem stanu początkowego
        # (strip_defaults=False: nadpisanie na wartość domyślną też przetrwa)
        updates = {k: v for k, v in current.items()
                   if v != self._initial.get(k)}
        if not updates:
            self.accept()
            return
        try:
            save_rule(self.dat_root, self.entry.name, updates,
                      strip_defaults=False)
        except OSError as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, tr("Ustawienia"), tr("Nie zapisano:") + f"\n{e}")
            return
        self.accept()

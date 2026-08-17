"""Ustawienia zbiorcze dla KATALOGU DAT-ów (np. 1G1R, ROMS, [T-En]Collection).

Reguły ustawione tu obowiązują WSZYSTKIE DAT-y w tym katalogu (i podkatalogach),
o ile pojedynczy DAT nie ma własnej reguły (kaskada global → katalog → DAT).
Zapis do _reguly.json pod kluczem = ścieżka katalogu względem dat_root
(albo "*" dla ustawień globalnych/korzenia).
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

from ..core.dirrules import save_rule
from ..core.i18n import tr

_FORMATS = [
    ("keep", "zostaw jak jest"),
    ("auto", "AUTO wg systemu (płyta→CHD, GC/Wii→RVZ, kartridż→ZIP)"),
    ("zip", "ZIP"), ("7z", "7z"), ("chd", "CHD"), ("rvz", "RVZ"),
]
_NAMINGS = [("dat", "z DAT-a (<header><name>, np. Sony - PlayStation 2)"),
            ("es", "EmulationStation (np. ps2, psx, dreamcast)")]


class FolderSettingsDialog(QDialog):
    def __init__(self, parent, folder_key: str, dat_root: Path,
                 rom_root: Path = None):
        super().__init__(parent)
        self.folder_key = folder_key      # np. "1G1R" albo "*" (globalne)
        self.dat_root = dat_root
        self.rom_root = Path(rom_root) if rom_root else None
        title = (tr("Ustawienia globalne (wszystkie DAT-y)") if folder_key == "*"
                 else tr("Ustawienia katalogu:") + f" {folder_key}")
        self.setWindowTitle(title)
        self.resize(660, 360)

        # aktualne surowe reguły dla tego klucza (bez kaskady — czysty wpis)
        cur = self._raw_rule()

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{title}</b> — " + tr("obowiązuje wszystkie DAT-y "
                             "w tym katalogu; pojedynczy DAT może nadpisać.")))
        form = QFormLayout()
        lay.addLayout(form)

        self.chk_parent = QCheckBox(tr("wszystkie DAT-y tego katalogu są RODZICAMI "
                                    "swoich platform (trzymają pliki fizyczne)"))
        self.chk_parent.setChecked(bool(cur.get("parent_priority", False)))
        form.addRow(tr("Rola:"), self.chk_parent)

        # ROLA wszystkich DAT-ów w katalogu: kolekcja albo pula tłumaczeń
        self.cmb_role = QComboBox()
        self.cmb_role.addItem(tr("kolekcja (parent/child)"), "collection")
        self.cmb_role.addItem(tr("tłumaczenia (pula wariantów do podmiany)"),
                              "translations")
        self._select(self.cmb_role, cur.get("role", "collection"))
        self.cmb_role.setToolTip(tr(
            "„tłumaczenia” = wszystkie DAT-y w tym katalogu to źródło fanowskich "
            "tłumaczeń (pula wariantów do podmiany w innych DAT-ach)."))
        form.addRow(tr("Rola DAT-u:"), self.cmb_role)

        self.cmb_format = QComboBox()
        for k, l in _FORMATS:
            self.cmb_format.addItem(tr(l), k)
        self._select(self.cmb_format, cur.get("format", "keep"))
        form.addRow(tr("Format przechowywania:"), self.cmb_format)

        self.cmb_naming = QComboBox()
        for k, l in _NAMINGS:
            self.cmb_naming.addItem(tr(l), k)
        self._select(self.cmb_naming, cur.get("naming", "dat"))
        form.addRow(tr("Nazwy katalogów per system:"), self.cmb_naming)

        self.e_root = QLineEdit(cur.get("rom_root", ""))
        self.e_root.setPlaceholderText(tr("puste = główny katalog ROM-ów"))
        rw = QWidget()
        rrow = QHBoxLayout(rw)
        rrow.setContentsMargins(0, 0, 0, 0)
        rrow.addWidget(self.e_root, 1)
        b = QPushButton(tr("Wybierz…"))
        b.clicked.connect(self._browse_root)
        rrow.addWidget(b)
        form.addRow(tr("Osobny rom_root (np. dla dzieci):"), rw)

        self.chk_subdir = QCheckBox(tr("gry wieloplikowe w podkatalogu per gra"))
        self.chk_subdir.setChecked(bool(cur.get("subdir_per_game", True)))
        self.chk_complete = QCheckBox(tr("buduj tylko kompletne gry"))
        self.chk_complete.setChecked(bool(cur.get("only_complete", True)))
        self.chk_trans = QCheckBox(tr("podmieniaj (Japan) na tłumaczenia [T-En]"))
        self.chk_trans.setChecked(bool(cur.get("prefer_translations", False)))
        for c in (self.chk_subdir, self.chk_complete, self.chk_trans):
            form.addRow("", c)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _raw_rule(self) -> dict:
        import json
        p = self.dat_root / "_reguly.json"
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return dict(data.get(self.folder_key, {})
                                or data.get(self.folder_key.lower(), {}))
            except (OSError, ValueError):
                pass
        return {}

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        i = combo.findData(value)
        combo.setCurrentIndex(i if i >= 0 else 0)

    def _browse_root(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("Osobny katalog ROM-ów"),
                                             self.e_root.text() or "")
        if d:
            self.e_root.setText(d)

    def _save(self) -> None:
        updates = {
            "parent_priority": self.chk_parent.isChecked(),
            "format": self.cmb_format.currentData(),
            "naming": self.cmb_naming.currentData(),
            "rom_root": self.e_root.text().strip(),
            "subdir_per_game": self.chk_subdir.isChecked(),
            "only_complete": self.chk_complete.isChecked(),
            "prefer_translations": self.chk_trans.isChecked(),
            "role": self.cmb_role.currentData(),
        }
        try:
            save_rule(self.dat_root, self.folder_key, updates)
        except OSError as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, tr("Ustawienia"), tr("Nie zapisano:") + f"\n{e}")
            return
        self.accept()

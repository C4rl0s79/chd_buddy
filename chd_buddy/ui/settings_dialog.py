"""Okno ustawień: ścieżki narzędzi, kompresja, wątki, tryb low-disk."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..core import presets
from ..core.i18n import tr
from ..core.settings import Settings


def _path_row(line: QLineEdit, pick_file: bool, parent) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(line)
    btn = QPushButton("…")
    btn.setFixedWidth(32)

    def browse():
        if pick_file:
            p, _ = QFileDialog.getOpenFileName(parent, tr("Wybierz plik"))
        else:
            p = QFileDialog.getExistingDirectory(parent, tr("Wybierz katalog"))
        if p:
            line.setText(p)

    btn.clicked.connect(browse)
    lay.addWidget(btn)
    return w


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(tr("Ustawienia"))
        self.setMinimumWidth(560)

        form = QFormLayout()
        self.chdman = QLineEdit(settings.chdman_path)
        self.seven = QLineEdit(settings.seven_zip_path)
        self.out_dir = QLineEdit(settings.output_dir)
        self.work_dir = QLineEdit(settings.work_dir)
        self.scratch_dir = QLineEdit(settings.scratch_dir)
        self.scratch_dir.setPlaceholderText(
            tr("fallback dla plików tymczasowych, gdy brak RAM dysku "
               "(puste = auto)"))
        self.preset = QComboBox(); self.preset.addItems(presets.PRESET_NAMES)
        self.preset.setCurrentText(settings.compression_preset)
        self.preset.setToolTip(tr("CHD: dobór kodeków chdman. default = chdman "
                                  "decyduje; max = najlepsza kompresja (wolniej); "
                                  "fast = szybciej; none = bez kompresji."))
        self.zip_level = QSpinBox(); self.zip_level.setRange(0, 9)
        self.zip_level.setValue(int(getattr(settings, "zip_level", 6)))
        self.zip_level.setToolTip(tr("ZIP: poziom DEFLATE. 0 = bez kompresji "
                                     "(szybko), 6 = domyślny, 9 = najmniejszy "
                                     "plik (wolniej)."))
        self.rvz_level = QSpinBox(); self.rvz_level.setRange(1, 22)
        self.rvz_level.setValue(int(getattr(settings, "rvz_level", 5)))
        self.rvz_level.setToolTip(tr("RVZ (GameCube/Wii): poziom zstd 1–22. "
                                     "5 = domyślny; wyżej = mniejszy plik, wolniej."))
        self.rvz_block = QSpinBox(); self.rvz_block.setRange(32, 2048)
        self.rvz_block.setSingleStep(32); self.rvz_block.setSuffix(" KB")
        self.rvz_block.setValue(int(getattr(settings, "rvz_block_kb", 128)))
        self.rvz_block.setToolTip(tr("RVZ: rozmiar bloku (128 KB = domyślny)."))
        self.threads = QSpinBox(); self.threads.setRange(0, 128)
        self.threads.setValue(settings.threads)
        self.threads.setSpecialValueText("auto")
        self.verify = QCheckBox(tr("Weryfikuj po utworzeniu"))
        self.verify.setChecked(settings.verify_after_create)
        self.aggressive = QCheckBox(tr("Tryb low-disk (usuwaj oryginał po extract)"))
        self.aggressive.setChecked(settings.aggressive_low_disk)
        self.roundtrip = QCheckBox(tr("Walidacja round-trip (gdy brak DAT)"))
        self.roundtrip.setChecked(settings.verify_roundtrip)
        self.del_source = QCheckBox(tr("Usuń źródła po udanej konwersji (cue/gdi/bin/iso)"))
        self.del_source.setChecked(settings.delete_source_after_convert)
        self.dat_dir = QLineEdit(settings.dat_dir)
        self.dat_dir.setPlaceholderText(tr("folder ze wszystkimi .dat (PS1/PS2/DC/Saturn) — dopasowanie po SHA-1"))
        self.quarantine = QLineEdit(settings.quarantine_dir_name)
        self.quarantine.setPlaceholderText(tr("nazwa podkatalogu na niezweryfikowane, np. nieznane"))

        form.addRow(tr("chdman:"), _path_row(self.chdman, True, self))
        form.addRow(tr("7-Zip:"), _path_row(self.seven, True, self))
        form.addRow(tr("Katalog wyjściowy:"), _path_row(self.out_dir, False, self))
        form.addRow(tr("Katalog roboczy:"), _path_row(self.work_dir, False, self))
        form.addRow(tr("Katalog tymczasowy (fallback):"),
                    _path_row(self.scratch_dir, False, self))
        form.addRow(tr("Folder DAT:"), _path_row(self.dat_dir, False, self))
        form.addRow(tr("Kwarantanna (podkatalog):"), self.quarantine)
        form.addRow(tr("Preset kompresji CHD:"), self.preset)
        form.addRow(tr("Poziom ZIP (0–9):"), self.zip_level)
        form.addRow(tr("Poziom RVZ (zstd 1–22):"), self.rvz_level)
        form.addRow(tr("Blok RVZ:"), self.rvz_block)
        form.addRow(tr("Wątki:"), self.threads)
        form.addRow("", self.verify)
        form.addRow("", self.roundtrip)
        form.addRow("", self.del_source)
        form.addRow("", self.aggressive)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(buttons)

    def apply_to_settings(self) -> Settings:
        s = self.settings
        s.chdman_path = self.chdman.text().strip()
        s.seven_zip_path = self.seven.text().strip()
        s.output_dir = self.out_dir.text().strip()
        s.work_dir = self.work_dir.text().strip()
        s.scratch_dir = self.scratch_dir.text().strip()
        s.compression_preset = self.preset.currentText()
        s.zip_level = int(self.zip_level.value())
        s.rvz_level = int(self.rvz_level.value())
        s.rvz_block_kb = int(self.rvz_block.value())
        s.threads = self.threads.value()
        s.verify_after_create = self.verify.isChecked()
        s.verify_roundtrip = self.roundtrip.isChecked()
        s.delete_source_after_convert = self.del_source.isChecked()
        s.aggressive_low_disk = self.aggressive.isChecked()
        s.dat_dir = self.dat_dir.text().strip()
        s.quarantine_dir_name = self.quarantine.text().strip() or "nieznane"
        s.save()
        return s

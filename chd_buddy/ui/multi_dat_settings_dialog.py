"""Zbiorcza edycja ustawień KILKU zaznaczonych DAT-ów naraz.

Workflow wg użytkownika: najpierw ustawienia całego katalogu (folder) piszą się
dla wszystkich DAT-ów (kaskada), potem można nadpisać pojedyncze — albo kilka
zaznaczonych naraz TUTAJ. Każde pole ma przełącznik „zmień": zapisujemy TYLKO
zaznaczone pola do reguły KAŻDEGO wybranego DAT-a (pozostałe zostają bez zmian).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
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
_NAMINGS = [("dat", "z DAT-a (<header><name>)"),
            ("es", "EmulationStation (ps2, psx, dreamcast…)")]


class MultiDatSettingsDialog(QDialog):
    def __init__(self, parent, entries: list, dat_root: Path):
        super().__init__(parent)
        self.entries = list(entries)
        self.dat_root = dat_root
        self.setWindowTitle(tr("Ustawienia") + f" {len(self.entries)} " + tr("zaznaczonych DAT-ów"))
        self.resize(680, 420)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "<b>" + tr("Zbiorcza edycja {n} DAT-ów.").format(n=len(self.entries)) + "</b> "
            + tr("Zaznacz pole wyboru zmien obok ustawien, ktore chcesz zapisac "
            "dla WSZYSTKICH wybranych — reszta ustawien kazdego DAT-a zostaje "
            "nietknieta.")))
        # lista nazw (skrót)
        names = ", ".join(e.name for e in self.entries[:8])
        if len(self.entries) > 8:
            names += f" … (+{len(self.entries) - 8})"
        lbl = QLabel(names)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: gray;")
        lay.addWidget(lbl)

        form = QFormLayout()
        lay.addLayout(form)

        # (klucz reguły, widżet-checkbox „zmień", widżet-wartość, getter)
        self._fields: list[tuple[str, QCheckBox, object, object]] = []

        self.cmb_format = QComboBox()
        for k, l in _FORMATS:
            self.cmb_format.addItem(tr(l), k)
        self._add_row(form, "format", tr("Format przechowywania:"), self.cmb_format,
                      lambda: self.cmb_format.currentData())

        self.cmb_naming = QComboBox()
        for k, l in _NAMINGS:
            self.cmb_naming.addItem(tr(l), k)
        self._add_row(form, "naming", tr("Nazwy katalogów per system:"),
                      self.cmb_naming, lambda: self.cmb_naming.currentData())

        self.chk_subdir = QCheckBox(tr("gry wieloplikowe w podkatalogu per gra"))
        self._add_row(form, "subdir_per_game", tr("Układ:"), self.chk_subdir,
                      lambda: self.chk_subdir.isChecked())

        self.chk_complete = QCheckBox(tr("buduj tylko kompletne gry"))
        self._add_row(form, "only_complete", tr("Kompletność:"), self.chk_complete,
                      lambda: self.chk_complete.isChecked())

        self.chk_dedup = QCheckBox(tr("kopie potwierdzonych → symlinki"))
        self._add_row(form, "dedup_copies", tr("Dedup:"), self.chk_dedup,
                      lambda: self.chk_dedup.isChecked())

        self.chk_trans = QCheckBox(tr("podmieniaj (Japan) na tłumaczenia [T-En]"))
        self._add_row(form, "prefer_translations", tr("Tłumaczenia:"),
                      self.chk_trans, lambda: self.chk_trans.isChecked())

        self.cmb_role = QComboBox()
        self.cmb_role.addItem(tr("kolekcja (parent/child)"), "collection")
        self.cmb_role.addItem(tr("tłumaczenia (pula wariantów)"), "translations")
        self._add_row(form, "role", tr("Rola DAT-u:"), self.cmb_role,
                      lambda: self.cmb_role.currentData())

        self.chk_skip = QCheckBox(tr("pomiń te DAT-y (nie raportuj / nie buduj)"))
        self._add_row(form, "skip", tr("Pomiń:"), self.chk_skip,
                      lambda: self.chk_skip.isChecked())

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _add_row(self, form, key, label, widget, getter) -> None:
        chk = QCheckBox(tr("zmień"))
        chk.toggled.connect(widget.setEnabled)
        widget.setEnabled(False)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(chk)
        h.addWidget(widget, 1)
        form.addRow(label, row)
        self._fields.append((key, chk, widget, getter))

    def _save(self) -> None:
        updates = {key: getter() for key, chk, _w, getter in self._fields
                   if chk.isChecked()}
        if not updates:
            self.reject()
            return
        try:
            for e in self.entries:
                save_rule(self.dat_root, e.name, updates, strip_defaults=False)
        except OSError as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, tr("Ustawienia"), tr("Nie zapisano:") + f"\n{e}")
            return
        self.accept()

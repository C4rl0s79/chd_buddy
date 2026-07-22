"""Okno wyboru grafiki dla ikony gry (jak ręczne wyszukiwanie w PyLinks).

Kandydaci (Libretro boxart/tytuł/snap + SteamGridDB + IGDB + TheGamesDB)
pobierani w wątku roboczym z paskiem postępu; miniatury pokazują się
w siatce, dwuklik albo „Zapisz" tworzy wielorozmiarowe .ico w wybranym
katalogu docelowym.
"""
from __future__ import annotations

import traceback
from pathlib import Path
from typing import Optional

from ..core.i18n import tr

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


class _Signals(QObject):
    progress = Signal(int, int, str)     # done, total, etykieta
    done = Signal(object, str)


class _Fetcher(QRunnable):
    def __init__(self, system: str, title: str, keys: dict):
        super().__init__()
        self.system, self.title, self.keys = system, title, keys
        self.signals = _Signals()

    @Slot()
    def run(self) -> None:
        try:
            from ..core.icons import (IgdbClient, SgdbClient, TgdbClient,
                                      artwork_candidates)
            k = self.keys
            sgdb = SgdbClient(k["sgdb"]) if k.get("sgdb") else None
            igdb = (IgdbClient(k["igdb_id"], k["igdb_secret"])
                    if k.get("igdb_id") and k.get("igdb_secret") else None)
            tgdb = TgdbClient(k["tgdb"]) if k.get("tgdb") else None
            cands = artwork_candidates(
                self.system, self.title, sgdb=sgdb, igdb=igdb, tgdb=tgdb,
                on_progress=lambda i, n, m: self.signals.progress.emit(i, n, m))
        except Exception as e:
            self.signals.done.emit(None, f"{e}\n{traceback.format_exc()}")
            return
        self.signals.done.emit(cands, "")


class IconPickDialog(QDialog):
    """Siatka kandydatów; zwraca wybranego przez `picked` po accept()."""

    def __init__(self, parent, system: str, title: str, keys, ico_path: Path):
        super().__init__(parent)
        self.setWindowTitle(tr("Ikona:") + f" {title}")
        self.resize(760, 560)
        self._cands: list[dict] = []
        self._ico_path = ico_path
        # zgodność wstecz: gdy podano sam klucz SGDB (str) zamiast słownika
        keys = {"sgdb": keys} if isinstance(keys, str) else dict(keys or {})
        self.picked: Optional[dict] = None

        lay = QVBoxLayout(self)
        self.info = QLabel(tr("Szukam grafik dla:") + f" {title}  ({system})…")
        lay.addWidget(self.info)

        # wybór katalogu docelowego .ico (widoczny i zmienialny)
        dst_row = QHBoxLayout()
        dst_row.addWidget(QLabel(tr("Zapisz ikonę do:")))
        self.dst_edit = QLineEdit(str(ico_path))
        dst_row.addWidget(self.dst_edit, 1)
        btn_dir = QPushButton(tr("Zmień folder…"))
        btn_dir.clicked.connect(self._choose_dir)
        dst_row.addWidget(btn_dir)
        lay.addLayout(dst_row)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setIconSize(QSize(160, 160))
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.itemDoubleClicked.connect(lambda _i: self._save())
        lay.addWidget(self.grid, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)         # nieokreślony do 1. sygnału
        self.progress.setFormat(tr("szukam grafik…"))
        lay.addWidget(self.progress)

        btns = QHBoxLayout()
        self.btn_save = QPushButton(tr("💾 Zapisz ikonę"))
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save)
        btn_cancel = QPushButton(tr("Anuluj"))
        btn_cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(self.btn_save)
        btns.addWidget(btn_cancel)
        lay.addLayout(btns)

        self._worker = _Fetcher(system, title, keys)
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.done.connect(self._filled)
        QThreadPool.globalInstance().start(self._worker)

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress.setFormat(f"{msg}  (%v/%m)")
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat(msg)

    def _filled(self, cands, err: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.progress.setFormat(tr("gotowe"))
        if err:
            self.info.setText(tr("Błąd pobierania kandydatów:") + f" {err.splitlines()[0]}")
            return
        self._cands = cands or []
        if not self._cands:
            self.info.setText(tr("Nie znaleziono żadnych grafik "
                              "(Libretro/SGDB/IGDB/TGDB)."))
            return
        self.info.setText(tr("Kandydatów: {n} — wybierz i Zapisz (dwuklik też "
                          "działa).").format(n=len(self._cands)))
        for i, c in enumerate(self._cands):
            pm = QPixmap()
            pm.loadFromData(c.get("preview") or b"")
            it = QListWidgetItem(QIcon(pm), c["label"])
            it.setData(Qt.ItemDataRole.UserRole, i)
            self.grid.addItem(it)
        self.btn_save.setEnabled(True)

    def _choose_dir(self) -> None:
        cur = Path(self.dst_edit.text())
        d = QFileDialog.getExistingDirectory(
            self, tr("Katalog docelowy ikony"), str(cur.parent))
        if d:
            self.dst_edit.setText(str(Path(d) / cur.name))

    def _save(self) -> None:
        items = self.grid.selectedItems()
        if not items:
            QMessageBox.information(self, tr("Ikona"), tr("Zaznacz grafikę."))
            return
        cand = self._cands[items[0].data(Qt.ItemDataRole.UserRole)]
        self._ico_path = Path(self.dst_edit.text().strip() or self._ico_path)
        from ..core.icons import save_icon
        try:
            ok = save_icon(cand, self._ico_path)
        except Exception as e:
            QMessageBox.warning(self, tr("Ikona"), tr("Nie udało się zapisać:") + f"\n{e}")
            return
        if not ok:
            QMessageBox.warning(self, tr("Ikona"),
                                tr("Nie udało się pobrać pełnego obrazu."))
            return
        self.picked = cand
        self.accept()

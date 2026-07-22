"""Osobne okno postępu — widać CO SIĘ DZIEJE i da się przerwać.

Wcześniej długie operacje (zwłaszcza CHD) wyglądały jak zawieszony program:
pasek stał, nie było wiadomo przy którym pliku jesteśmy. To okno pokazuje
bieżącą operację, licznik, czas i pełny log na żywo — plus przycisk Przerwij.

Przerwanie jest BEZPIECZNE: to, co już policzone/naprawione, jest zapisane
(indeks commituje partiami, operacje plikowe są atomowe), więc kolejny przebieg
kontynuuje, a nie zaczyna od zera.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..core.i18n import tr


class ProgressDialog(QDialog):
    """Modeless okno postępu. `cancel_event` to threading.Event."""

    def __init__(self, parent, title: str, cancel_event):
        super().__init__(parent)
        self.cancel_event = cancel_event
        self._t0 = time.monotonic()
        self._finished = False
        self.setWindowTitle(title)
        self.resize(760, 420)
        # bez przycisku zamykania — kończy się samo albo przez Przerwij
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

        lay = QVBoxLayout(self)
        self.lbl_op = QLabel(tr("Start…"))
        self.lbl_op.setWordWrap(True)
        f = self.lbl_op.font()
        f.setBold(True)
        self.lbl_op.setFont(f)
        lay.addWidget(self.lbl_op)

        # OGÓLNY: wszystkie pliki do naprawy (przesuwa się z każdym plikiem)
        lay.addWidget(QLabel(tr("Ogółem (pliki):")))
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)          # nieokreślony do 1. sygnału
        self.bar.setTextVisible(True)
        lay.addWidget(self.bar)

        # SZCZEGÓŁOWY: bieżący plik (kompresja / wypakowanie / przenoszenie)
        self.lbl_detail = QLabel("")
        self.lbl_detail.setWordWrap(True)
        lay.addWidget(self.lbl_detail)
        self.bar_detail = QProgressBar()
        self.bar_detail.setRange(0, 100)
        self.bar_detail.setValue(0)
        self.bar_detail.setTextVisible(True)
        self.bar_detail.setFormat("")
        lay.addWidget(self.bar_detail)

        self.lbl_time = QLabel(tr("czas:") + " 0:00")
        lay.addWidget(self.lbl_time)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch()
        self.btn_cancel = QPushButton(tr("⛔ Przerwij"))
        self.btn_cancel.setToolTip(
            "Zatrzymuje po bieżącej operacji. Wszystko, co już zrobione, "
            "zostaje zapisane — kolejny przebieg dokończy resztę.")
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_close = QPushButton(tr("Zamknij"))
        self.btn_close.setEnabled(False)
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_close)
        lay.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    # --- API wołane z okna głównego -----------------------------------------

    def append_log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    # QProgressBar w Qt bierze C++ int (32-bit). Postęp BAJTOWY dużych plików
    # (CHD/ISO > 2,1 GB) przekracza INT_MAX i rzuca OverflowError. Skalujemy
    # (done, total) do bezpiecznego zakresu, zachowując proporcję (procent).
    _SAFE_MAX = 1_000_000

    @classmethod
    def _scale(cls, done: int, total: int) -> tuple[int, int]:
        if total <= cls._SAFE_MAX:
            return int(done), int(total)
        d = int(done * cls._SAFE_MAX / total) if total else 0
        return max(0, min(d, cls._SAFE_MAX)), cls._SAFE_MAX

    def set_progress(self, done: int, total: int, text: str) -> None:
        if total > 0:
            d, t = self._scale(done, total)
            self.bar.setRange(0, t)
            self.bar.setValue(d)
            # liczniki (pliki/CHD/DAT-y) są małe → pokaż X/Y; przy skalowaniu
            # dużych wartości pokaż sam procent, żeby X/Y nie mylił
            self.bar.setFormat("%v / %m  (%p%)" if total <= self._SAFE_MAX
                               else "%p%")
        else:
            self.bar.setRange(0, 0)
            self.bar.setFormat("")
        if text:
            self.lbl_op.setText(text)

    def set_detail(self, done: int, total: int, text: str) -> None:
        """Pasek szczegółowy: postęp bieżącego pliku (kompresja/przenoszenie).
        total<=0 => tryb nieokreślony (pulsuje); done<0 => wyzeruj/schowaj."""
        if done < 0:
            self.bar_detail.setRange(0, 100)
            self.bar_detail.setValue(0)
            self.bar_detail.setFormat("")
            self.lbl_detail.setText("")
            return
        if total > 0:
            d, t = self._scale(done, total)
            self.bar_detail.setRange(0, t)
            self.bar_detail.setValue(d)
            self.bar_detail.setFormat("%p%")
        else:
            self.bar_detail.setRange(0, 0)      # pulsujący (nieznany postęp)
            self.bar_detail.setFormat("")
        self.lbl_detail.setText(text or "")

    def finish(self, err: str = "") -> None:
        self._finished = True
        self._timer.stop()
        self.bar.setRange(0, 100)
        self.bar_detail.setRange(0, 100)
        self.bar_detail.setValue(0)
        self.bar_detail.setFormat("")
        self.lbl_detail.setText("")
        was_cancel = self.cancel_event.is_set()
        self.bar.setValue(0 if err else 100)
        if err:
            self.lbl_op.setText(tr("BŁĄD — szczegóły w logu"))
        elif was_cancel:
            self.lbl_op.setText(tr("PRZERWANE — postęp zapisany, można wznowić"))
        else:
            self.lbl_op.setText(tr("Gotowe"))
        self.btn_cancel.setEnabled(False)
        self.btn_close.setEnabled(True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.show()

    # --- wewnętrzne ----------------------------------------------------------

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.btn_cancel.setEnabled(False)
        self.lbl_op.setText(tr("Przerywam po bieżącej operacji… (postęp zapisany)"))
        self.append_log(tr("== ŻĄDANIE PRZERWANIA — kończę bieżącą operację =="))

    def _tick(self) -> None:
        s = int(time.monotonic() - self._t0)
        self.lbl_time.setText(tr("czas:") + f" {s // 60}:{s % 60:02d}")

    def closeEvent(self, ev) -> None:      # nie zamykaj w trakcie pracy
        if self._finished:
            ev.accept()
        else:
            ev.ignore()

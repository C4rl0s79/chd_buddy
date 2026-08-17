"""Główne okno CHD Buddy.

Kolejka przetwarzana jest ściśle sekwencyjnie (jeden aktywny worker),
co realizuje wymóg pracy przy małej ilości wolnego miejsca: nowy plik
startuje dopiero po zakończeniu i podmianie poprzedniego.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThreadPool
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QTableView,
    QVBoxLayout, QWidget,
)

from ..core.chdman import CHDMan, CHDManNotFound
from ..core.models import (
    AuditVerdict, Job, JobStatus, MediaType, Operation,
)
from ..core.scanner import scan_paths
from ..core.settings import Settings
from ..core.i18n import tr
from .settings_dialog import SettingsDialog
from .worker import JobWorker

_STATUS_LABEL = {
    JobStatus.PENDING: "oczekuje",
    JobStatus.RUNNING: "w toku",
    JobStatus.DONE: "✔ gotowe",
    JobStatus.FAILED: "✗ błąd",
    JobStatus.CANCELLED: "anulowano",
    JobStatus.BLOCKED_DISK: "⛔ za mało miejsca",
    JobStatus.SKIPPED: "pominięto",
    JobStatus.QUARANTINED: "⚠ kwarantanna (brak w DAT)",
}

_OP_LABEL = {
    Operation.AUDIT: "audyt",
    Operation.CREATE: "utwórz",
    Operation.RECOMPRESS: "rekompresja",
    Operation.RETYPE: "napraw typ",
    Operation.VERIFY: "weryfikacja",
}


class JobTableModel(QAbstractTableModel):
    COLUMNS = ["Plik", "Operacja", "Typ", "Postęp", "Status"]

    def __init__(self):
        super().__init__()
        self.jobs: list[Job] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self.jobs)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def add_jobs(self, jobs: list[Job]) -> None:
        if not jobs:
            return
        start = len(self.jobs)
        self.beginInsertRows(QModelIndex(), start, start + len(jobs) - 1)
        self.jobs.extend(jobs)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self.jobs.clear()
        self.endResetModel()

    def job_changed(self, job: Job) -> None:
        try:
            row = self.jobs.index(job)
        except ValueError:
            return
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def data(self, index, role=Qt.DisplayRole):
        job = self.jobs[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return job.src.name
            if col == 1:
                return _OP_LABEL.get(job.operation, job.operation.value)
            if col == 2:
                media = job.media_type
                if job.audit is not None and job.audit.detected_media != MediaType.UNKNOWN:
                    media = job.audit.detected_media
                return media.value if media != MediaType.UNKNOWN else "?"
            if col == 3:
                return f"{job.progress:.0f}%" if job.progress > 0 else ""
            if col == 4:
                base = _STATUS_LABEL.get(job.status, job.status.value)
                if job.status_text:
                    return f"{base} — {job.status_text}"
                return base
        if role == Qt.ForegroundRole and col == 4:
            if job.status == JobStatus.DONE:
                return QColor("#2e7d32")
            if job.status in (JobStatus.FAILED,):
                return QColor("#c62828")
            if job.status == JobStatus.BLOCKED_DISK:
                return QColor("#ef6c00")
            if job.status == JobStatus.QUARANTINED:
                return QColor("#8e24aa")
            if job.audit is not None and job.audit.verdict == AuditVerdict.SUSPECT_WRONG_TYPE:
                return QColor("#ef6c00")
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return None


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(1)  # sekwencyjnie!
        self.cancel_event = threading.Event()
        self._running = False
        self._pending_idx = 0
        self._active_worker = None  # trzyma referencję do bieżącego workera
        self._dat_index = None       # indeks DAT budowany na starcie batcha
        self.chd: Optional[CHDMan] = None

        from .. import __version__ as _ver
        self.setWindowTitle(tr("CHD Buddy") + f"  v{_ver}")
        self.resize(1040, 680)
        self._build_ui()
        self.setAcceptDrops(True)
        self._try_init_chdman()

    # --- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        self.drop = QLabel(tr("📁  Przeciągnij tu pliki/foldery (CHD lub źródła)\n"
                           "lub kliknij „Dodaj…”"))
        self.drop.setAlignment(Qt.AlignCenter)
        self.drop.setMinimumHeight(90)
        self.drop.setStyleSheet(
            "QLabel{border:2px dashed #9aa0a6;border-radius:10px;color:#5f6368;"
            "font-size:14px;} QLabel[active=\"true\"]{border-color:#1a73e8;"
            "background:#e8f0fe;color:#1a73e8;}")
        root.addWidget(self.drop)

        # pasek trybu
        bar = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems([
            tr("Audyt (wykryj złe CHD)"),
            tr("Konwertuj źródła → CHD"),
            tr("Rekompresja w miejscu"),
            tr("Napraw typ (DVD-jako-CD)"),
        ])
        bar.addWidget(QLabel(tr("Tryb:")))
        bar.addWidget(self.mode)
        self.chk_verify = QPushButton(tr("Dodaj…"))
        self.chk_verify.clicked.connect(self._pick_files)
        bar.addStretch()
        bar.addWidget(self.chk_verify)
        root.addLayout(bar)

        # tabela
        self.model = JobTableModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: self._update_deep_button())
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 5):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        root.addWidget(self.table, 3)

        # log
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText(tr("Log operacji…"))
        root.addWidget(self.log, 1)

        # progres + przyciski
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        root.addWidget(self.progress)

        btns = QHBoxLayout()
        self.btn_start = QPushButton(tr("▶ Start"))
        self.btn_start.clicked.connect(self._start)
        self.btn_fix = QPushButton(tr("🔧 Napraw wykryte"))
        self.btn_fix.setToolTip(tr("Napraw (retype) obrazy oznaczone w audycie jako "
                                "podejrzane — bez ponownego skanowania."))
        self.btn_fix.clicked.connect(self._repair_detected)
        self.btn_fix.setEnabled(False)
        self.btn_deep = QPushButton(tr("🔍 Sprawdź dokładnie (DAT)"))
        self.btn_deep.setToolTip(tr("Zaznacz jeden plik: próbuje różnych metod "
                                 "wypakowania (extractdvd/cd/hd/raw/ld + deframe) "
                                 "aż wynik zwaliduje się w DAT. Wymaga folderu DAT."))
        self.btn_deep.clicked.connect(self._deep_check)
        self.btn_deep.setEnabled(False)
        self.btn_cancel = QPushButton(tr("⏹ Przerwij"))
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setEnabled(False)
        self.btn_clear = QPushButton(tr("🗑 Wyczyść"))
        self.btn_clear.clicked.connect(self._clear)
        self.btn_settings = QPushButton(tr("⚙ Ustawienia"))
        self.btn_settings.clicked.connect(self._open_settings)
        btns.addStretch()
        for b in (self.btn_start, self.btn_fix, self.btn_deep, self.btn_cancel,
                  self.btn_clear, self.btn_settings):
            btns.addWidget(b)
        root.addLayout(btns)

        self.setCentralWidget(central)

    def _try_init_chdman(self) -> None:
        try:
            chd = CHDMan(self.settings.chdman_path or None)
        except CHDManNotFound as e:
            self.chd = None
            self._log(f"UWAGA: {e}")
            return
        # Konstruktor przeszedł (ścieżka istnieje) — sprawdź, czy da się uruchomić.
        try:
            ver = chd.version()
        except OSError as e:
            # Plik istnieje, ale to nie jest prawidłowy program (WinError 193)
            # albo inny błąd uruchomienia. Nie wywalaj apki.
            self.chd = None
            self._log(
                f"UWAGA: '{chd.binary}' nie jest prawidłowym plikiem chdman.exe "
                f"({e}). Wejdź w Ustawienia i wskaż właściwy chdman.exe."
            )
            return
        self.chd = chd
        self._log(f"chdman: {chd.binary} (wersja {ver})")

    # --- Drag & drop ------------------------------------------------------

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_drop_active(True)

    def dragLeaveEvent(self, e) -> None:
        self._set_drop_active(False)

    def dropEvent(self, e: QDropEvent) -> None:
        self._set_drop_active(False)
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls()]
        self._add_paths(paths)

    def _set_drop_active(self, active: bool) -> None:
        self.drop.setProperty("active", "true" if active else "false")
        self.drop.style().unpolish(self.drop)
        self.drop.style().polish(self.drop)

    # --- Dodawanie zadań ---------------------------------------------------

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("Wybierz pliki"),
            filter=tr("Obsługiwane (*.chd *.cue *.gdi *.iso *.img *.toc *.nrg);;Wszystkie (*)"))
        if paths:
            self._add_paths([Path(p) for p in paths])

    def _add_paths(self, paths: list[Path]) -> None:
        mode = self.mode.currentIndex()
        jobs: list[Job] = []
        if mode == 1:  # konwersja źródeł
            for it in scan_paths(paths):
                jobs.append(Job(src=it.path, operation=Operation.CREATE,
                                media_type=it.media_type,
                                dst_dir=self.settings.resolved_output_dir(it.path)))
        else:
            op = {0: Operation.AUDIT, 2: Operation.RECOMPRESS,
                  3: Operation.RETYPE}[mode]
            chd_files: list[Path] = []
            for p in paths:
                if p.is_file() and p.suffix.lower() == ".chd":
                    chd_files.append(p)
                elif p.is_dir():
                    chd_files.extend(sorted(p.rglob("*.chd")))
            for f in chd_files:
                jobs.append(Job(src=f, operation=op,
                                verify_after=(op == Operation.AUDIT and False)))
        if not jobs:
            self._log("Nie znaleziono pasujących plików dla wybranego trybu.")
        self.model.add_jobs(jobs)

    # --- Kolejka (sekwencyjna) --------------------------------------------

    def _suspect_jobs(self) -> list:
        """Podejrzane (DVD-jako-CD) jeszcze NIE przekształcone w retype."""
        return [j for j in self.model.jobs
                if j.operation == Operation.AUDIT
                and j.audit is not None
                and j.audit.verdict == AuditVerdict.SUSPECT_WRONG_TYPE]

    def _refresh_fix_button(self) -> None:
        self.btn_fix.setEnabled(not self._running and bool(self._suspect_jobs()))

    def _repair_detected(self) -> None:
        if self._running:
            return
        suspects = self._suspect_jobs()
        if not suspects:
            QMessageBox.information(self, tr("Brak wykrytych"),
                                    tr("Najpierw uruchom audyt — nie ma obrazów "
                                    "oznaczonych jako podejrzane."))
            return
        # Przełącz podejrzane wiersze w zadania retype (typ docelowy z audytu),
        # bez ponownego skanowania — korzystamy z wyniku audytu.
        for j in suspects:
            j.operation = Operation.RETYPE
            j.media_type = j.audit.expected_media or MediaType.DVD
            j.status = JobStatus.PENDING
            j.progress = 0
            j.status_text = ""
            self.model.job_changed(j)
        self._log(f"Naprawa {len(suspects)} wykrytych obrazów (retype)…")
        self._start()

    def _selected_job(self):
        sel = self.table.selectionModel().selectedRows()
        if len(sel) != 1:
            return None
        row = sel[0].row()
        if 0 <= row < len(self.model.jobs):
            return self.model.jobs[row]
        return None

    def _update_deep_button(self) -> None:
        self.btn_deep.setEnabled(not self._running and self._selected_job() is not None)

    def _deep_check(self) -> None:
        if self._running:
            return
        job = self._selected_job()
        if job is None:
            QMessageBox.information(self, tr("Zaznacz plik"),
                                    tr("Zaznacz w tabeli dokładnie jeden plik CHD."))
            return
        self._try_init_chdman()
        if self.chd is None:
            QMessageBox.warning(self, tr("Brak działającego chdman"),
                                tr("Wskaż poprawny chdman.exe w Ustawieniach."))
            return
        self._dat_index = self._build_dat_index()
        if self._dat_index is None or self._dat_index.games == 0:
            QMessageBox.warning(self, tr("Brak DAT"),
                                tr("Głęboka walidacja wymaga wczytanego DAT. "
                                "Ustaw folder DAT w Ustawieniach."))
            return
        # Uruchom TYLKO ten jeden plik (nie ruszaj pozostałych zadań).
        job.operation = Operation.DEEPCHECK
        job.status = JobStatus.PENDING
        job.progress = 0
        job.status_text = ""
        self.model.job_changed(job)
        self.cancel_event.clear()
        self._running = True
        self.btn_start.setEnabled(False)
        self.btn_fix.setEnabled(False)
        self.btn_deep.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self._pending_idx = len(self.model.jobs)  # blokuj uruchamianie innych
        self._log(f"Głęboka walidacja: {job.src.name}…")
        self._launch(job)

    def _start(self) -> None:
        if self._running:
            return
        # Ponowna walidacja chdman TUŻ przed batchem — binarka mogła zostać
        # wyzerowana/poddana kwarantannie przez antywirusa od czasu startu apki.
        self._try_init_chdman()
        if self.chd is None:
            QMessageBox.warning(self, tr("Brak działającego chdman"),
                                tr("Nie znaleziono działającego chdman. Sprawdź ścieżkę "
                                "w Ustawieniach oraz czy antywirus nie usunął/wyzerował "
                                "chdman.exe (dodaj wykluczenie dla chdman.exe i folderu ROM)."))
            return
        if not self.model.jobs:
            return
        self.cancel_event.clear()
        self._running = True
        self._pending_idx = 0
        self._dat_index = self._build_dat_index()
        self.btn_start.setEnabled(False)
        self.btn_fix.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self._process_next()

    def _build_dat_index(self):
        """Buduje indeks DAT RAZ na batch (nie per plik). None gdy brak DAT."""
        dat_dir = (self.settings.dat_dir or "").strip()
        if not dat_dir:
            return None
        try:
            from ..core.datfile import DatIndex
            idx = DatIndex.from_paths([Path(dat_dir)])
            self._log(f"DAT: wczytano {idx.games} gier / {len(idx.by_sha1)} hashy "
                      f"z {dat_dir}")
            if idx.games == 0:
                self._log("DAT: uwaga — nie znaleziono żadnych wpisów (sprawdź folder).")
            return idx
        except Exception as e:
            self._log(f"DAT: nie udało się wczytać ({e}) — kontynuuję bez DAT.")
            return None

    def _process_next(self) -> None:
        if self.cancel_event.is_set():
            self._finish_queue("Przerwano przez użytkownika.")
            return
        # Strażnik: czy chdman nadal istnieje i nie jest wyzerowany (antywirus)?
        if self.chd is not None and not self.chd.healthy():
            self._finish_queue(
                "PRZERWANO: chdman.exe zniknął lub został wyzerowany w trakcie "
                "(prawdopodobnie antywirus). Dodaj wykluczenie dla chdman.exe "
                "i folderu ROM, przywróć plik i uruchom ponownie.")
            return
        while self._pending_idx < len(self.model.jobs):
            job = self.model.jobs[self._pending_idx]
            self._pending_idx += 1
            if job.status in (JobStatus.PENDING,):
                self._launch(job)
                return
        self._finish_queue("Wszystkie zadania zakończone.")

    def _launch(self, job: Job) -> None:
        self.progress.setValue(0)
        worker = JobWorker(job, self.chd, self.settings, self.cancel_event,
                           dat_index=getattr(self, "_dat_index", None))
        # KLUCZOWE: QRunnable ma domyślnie autoDelete=True. Po run() Qt kasuje
        # workera (i jego WorkerSignals), a międzywątkowe sygnały finished/
        # audited mogą wciąż czekać w kolejce zdarzeń → use-after-free i twardy
        # crash. Wyłączamy autoDelete i trzymamy referencję po stronie Pythona.
        worker.setAutoDelete(False)
        self._active_worker = worker
        worker.signals.progress.connect(self._on_progress)
        worker.signals.log.connect(self._log)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.audited.connect(self._on_audited)
        self.model.job_changed(job)
        self.pool.start(worker)

    def _on_audited(self, result) -> None:
        # Wynik audytu jest już przypięty do job.audit w workerze; odświeżamy
        # wiersz, żeby werdykt (kolor/typ) był widoczny natychmiast.
        w = self._active_worker
        if w is not None:
            self.model.job_changed(w.job)

    def _on_progress(self, job: Job, pct: float, msg: str) -> None:
        if pct >= 0:
            job.progress = pct
            self.progress.setValue(int(pct))
        job.status_text = msg[:60]
        self.model.job_changed(job)

    def _on_finished(self, job: Job, ok: bool, message: str) -> None:
        job.status_text = message[:80]
        job.progress = 100 if ok else job.progress
        self.model.job_changed(job)
        self._log(f"{job.src.name}: {message}")
        self._process_next()

    def _cancel(self) -> None:
        self.cancel_event.set()
        self._log("Żądanie przerwania — dokończę bieżący plik i zatrzymam.")

    def _finish_queue(self, msg: str) -> None:
        self._running = False
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress.setValue(0)
        self._refresh_fix_button()
        self._update_deep_button()
        self._log(msg)

    def _clear(self) -> None:
        if self._running:
            return
        self.model.clear()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings = dlg.apply_to_settings()
            self._try_init_chdman()

    def _log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

"""Most między backendem a Qt: QRunnable emitujący sygnały postępu.

Każde zadanie (audit/convert/recompress/retype) uruchamiane jest w puli
wątków, a wyniki wracają do GUI przez sygnały. Anulowanie przez threading.Event.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ..core import fixer, presets
from ..core.audit import audit_chd
from ..core.chdman import CHDMan
from ..core.models import Job, JobStatus, MediaType, Operation
from ..core.settings import Settings


class WorkerSignals(QObject):
    progress = Signal(object, float, str)   # job, percent, message
    log = Signal(str)
    finished = Signal(object, bool, str)    # job, ok, message
    audited = Signal(object)                # AuditResult


class JobWorker(QRunnable):
    def __init__(self, job: Job, chd: CHDMan, settings: Settings,
                 cancel_event: threading.Event, dat_index=None):
        super().__init__()
        self.job = job
        self.chd = chd
        self.settings = settings
        self.cancel_event = cancel_event
        self.dat_index = dat_index
        self.signals = WorkerSignals()

    def _progress(self, pct: float, msg: str) -> None:
        self.signals.progress.emit(self.job, pct, msg)

    def _log(self, msg: str) -> None:
        self.signals.log.emit(msg)

    @Slot()
    def run(self) -> None:
        job = self.job
        job.status = JobStatus.RUNNING
        try:
            if job.operation == Operation.AUDIT:
                self._run_audit()
            elif job.operation == Operation.CREATE:
                self._run_create()
            elif job.operation == Operation.RECOMPRESS:
                self._run_recompress()
            elif job.operation == Operation.RETYPE:
                self._run_retype()
            elif job.operation == Operation.DEEPCHECK:
                self._run_deepcheck()
            elif job.operation == Operation.VERIFY:
                self._run_verify()
            else:
                self.signals.finished.emit(job, False, f"nieznana operacja: {job.operation}")
        except Exception as e:  # pragma: no cover - obrona przed crashem wątku
            self.signals.finished.emit(job, False, f"wyjątek: {e}")

    # --- Poszczególne operacje --------------------------------------------

    def _run_audit(self) -> None:
        r = audit_chd(self.chd, self.job.src, self.settings,
                      do_verify=self.job.verify_after,
                      on_progress=self._progress, cancel_event=self.cancel_event)
        self.job.audit = r
        self.signals.audited.emit(r)
        self.signals.finished.emit(self.job, True, r.message)

    def _run_create(self) -> None:
        media = self.job.media_type if self.job.media_type != MediaType.UNKNOWN else MediaType.CD
        dst_dir = self.job.dst_dir or self.settings.resolved_output_dir(self.job.src)
        comp = self.job.compression or presets.compression_for(
            self.settings.compression_preset, media)
        out = fixer.create_from_source(
            self.chd, self.job.src, media, dst_dir, self.settings,
            compression=comp, on_progress=self._progress, log=self._log,
            cancel_event=self.cancel_event,
            delete_source=self.settings.delete_source_after_convert)
        self.job.dst_path = dst_dir / (self.job.src.stem + ".chd")
        self._finish(out.ok, out.message)

    def _run_recompress(self) -> None:
        info = self.chd.info(self.job.src)
        comp = self.job.compression or presets.compression_for(
            self.settings.compression_preset, info.detected_media)
        out = fixer.recompress_file(
            self.chd, self.job.src, self.settings, compression=comp, info=info,
            on_progress=self._progress, log=self._log, cancel_event=self.cancel_event)
        self.job.budget = out.budget
        self._finish(out.ok, out.message, out.budget)

    def _run_retype(self) -> None:
        info = self.chd.info(self.job.src)
        target = self.job.media_type if self.job.media_type != MediaType.UNKNOWN else MediaType.DVD
        comp = self.job.compression or presets.compression_for(
            self.settings.compression_preset, target)
        out = fixer.retype_file(
            self.chd, self.job.src, target, self.settings, compression=comp, info=info,
            on_progress=self._progress, log=self._log, cancel_event=self.cancel_event,
            dat_index=self.dat_index)  # quarantine_dir=None => obok pliku
        self.job.budget = out.budget
        if out.quarantined:
            self.job.status = JobStatus.QUARANTINED
            self.signals.finished.emit(self.job, False, out.message)
            return
        self._finish(out.ok, out.message, out.budget)

    def _run_verify(self) -> None:
        res = self.chd.verify(self.job.src, on_progress=self._progress,
                              cancel_event=self.cancel_event)
        self._finish(res.ok, "verify OK" if res.ok else "verify FAILED")

    def _run_deepcheck(self) -> None:
        from ..core import deepcheck
        if self.dat_index is None:
            self._finish(False, "głęboka walidacja wymaga wczytanego DAT")
            return
        work_dir = self.settings.resolved_work_dir(self.job.src)
        r = deepcheck.deep_identify(
            self.chd, self.job.src, self.dat_index, work_dir,
            on_progress=self._progress, log=self._log,
            cancel_event=self.cancel_event)
        self.job.deep = r
        if r.ok:
            self._finish(True, f"DAT: '{r.game}' ({r.media.value}) metodą: {r.method}")
        else:
            self._finish(False, f"brak dopasowania w DAT (prób: {len(r.tried)})")

    def _finish(self, ok: bool, message: str, budget=None) -> None:
        if not ok and budget is not None and not budget.fits:
            self.job.status = JobStatus.BLOCKED_DISK
        else:
            self.job.status = JobStatus.DONE if ok else JobStatus.FAILED
        self.signals.finished.emit(self.job, ok, message)

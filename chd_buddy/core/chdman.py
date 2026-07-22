"""Wrapper na chdman (z MAME).

Kluczowe różnice względem naiwnego podejścia:
- chdman aktualizuje postęp znakiem CR (``\\r``), nie LF, więc czytamy strumień
  bajtowo i sami dzielimy na tokeny po \\r/\\n.
- Faktyczny format postępu to np. ``Compressing, 12.3% complete... (ratio=35.2%)``
  oraz ``Verifying, 40.0% complete...`` / ``Extracting, ...``.
- Kodeki kompresji zależą od polecenia (CD vs DVD/HD) — patrz presets.py.
- Obsługa anulowania przez terminate()/kill() na uchwycie procesu.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from .models import CHDInfo

ProgressCB = Callable[[float, str], None]

# Postęp: dowolne polecenie chdman kończy liczbę procentów słowem "complete".
_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*complete", re.IGNORECASE)
_RATIO_RE = re.compile(r"ratio\s*=\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)


@dataclass
class CHDManResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


class CHDManError(RuntimeError):
    pass


class CHDManNotFound(FileNotFoundError):
    pass


class CHDMan:
    """Cienka warstwa nad binarką chdman(.exe)."""

    def __init__(self, binary: str | os.PathLike[str] | None = None):
        candidate = str(binary) if binary else "chdman"
        resolved = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if not resolved:
            raise CHDManNotFound(
                f"Nie znaleziono chdman: '{candidate}'. "
                f"Ustaw ścieżkę w ustawieniach lub dodaj do PATH."
            )
        self.binary = resolved

    # --- Infrastruktura uruchamiania --------------------------------------

    @staticmethod
    def _no_window_kwargs() -> dict:
        """Ukryj konsolę na Windows przy uruchamianiu z GUI."""
        if sys.platform.startswith("win"):
            return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        return {}

    def _run(self, args: list[str]) -> CHDManResult:
        proc = subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **self._no_window_kwargs(),
        )
        return CHDManResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )

    def healthy(self) -> bool:
        """Tani test bez spawnu: binarka istnieje i ma niezerowy rozmiar.

        Łapie sytuację, gdy antywirus wyzeruje/usunie chdman.exe w trakcie batcha.
        """
        try:
            p = Path(self.binary)
            return p.is_file() and p.stat().st_size > 0
        except OSError:
            return False

    def version(self) -> str:
        # chdman bez argumentów wypisuje help/wersję na stderr; użyj 'help'
        res = self._run(["help"])
        text = (res.stdout or "") + (res.stderr or "")
        m = re.search(r"chdman[^\n]*?(\d+\.\d+)", text)
        return m.group(1) if m else "unknown"

    # --- Odczyt strumienia z podziałem na \r i \n -------------------------

    @staticmethod
    def _iter_tokens(pipe) -> Iterator[str]:
        """Czyta strumień bajtowy i yielduje linie dzielone po \\r oraz \\n."""
        buf = bytearray()
        while True:
            chunk = pipe.read(1024)
            if not chunk:
                break
            for byte in chunk:
                if byte in (0x0D, 0x0A):  # \r lub \n
                    if buf:
                        yield buf.decode("utf-8", "replace")
                        buf.clear()
                else:
                    buf.append(byte)
        if buf:
            yield buf.decode("utf-8", "replace")

    def _stream(
        self,
        args: list[str],
        on_progress: Optional[ProgressCB],
        cancel_event: Optional[threading.Event] = None,
    ) -> CHDManResult:
        """Uruchamia polecenie z parsowaniem postępu w czasie rzeczywistym."""
        proc = subprocess.Popen(
            [self.binary, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            **self._no_window_kwargs(),
        )
        captured: list[str] = []
        assert proc.stdout is not None
        try:
            for token in self._iter_tokens(proc.stdout):
                captured.append(token)
                if cancel_event is not None and cancel_event.is_set():
                    self._terminate(proc)
                    proc.wait(timeout=10)
                    return CHDManResult(False, "\n".join(captured), "cancelled", -1)
                if on_progress is not None:
                    m = _PROGRESS_RE.search(token)
                    if m:
                        # chdman potrafi przestrzelić >100% na plikach z błędnie
                        # zapisaną geometrią (np. DVD-jako-CD) — przycinamy do
                        # sensownego zakresu, żeby pasek postępu nie wariował.
                        pct = float(m.group(1))
                        if pct > 100.0:
                            pct = 100.0
                        elif pct < 0.0:
                            pct = 0.0
                        msg = token.strip()
                        on_progress(pct, msg)
                        continue
                    if token.strip() and on_progress is not None:
                        on_progress(-1.0, token.strip())
        finally:
            proc.stdout.close()
        proc.wait()
        full = "\n".join(captured)
        return CHDManResult(proc.returncode == 0, full, "", proc.returncode)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
        except OSError:
            pass

    # --- Polecenia informacyjne -------------------------------------------

    def info(self, path: Path) -> CHDInfo:
        res = self._run(["info", "-v", "-i", str(path)])
        info = parse_info(res.stdout, path)
        try:
            info.file_bytes = path.stat().st_size
        except OSError:
            pass
        if not res.ok and not info.version:
            info.raw_info = (res.stdout or "") + (res.stderr or "")
        return info

    def verify(
        self,
        path: Path,
        on_progress: Optional[ProgressCB] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> CHDManResult:
        return self._stream(["verify", "-i", str(path)], on_progress, cancel_event)

    # --- Tworzenie ---------------------------------------------------------

    def create(
        self,
        media_cmd: str,
        src: Path,
        dst: Path,
        compression: Optional[str] = None,
        threads: int = 0,
        force: bool = True,
        on_progress: Optional[ProgressCB] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> CHDManResult:
        """media_cmd: createcd / createdvd / createhd / createraw / createld."""
        args = [media_cmd, "-i", str(src), "-o", str(dst)]
        if force:
            args.append("-f")
        if compression:
            args += ["-c", compression]
        if threads and threads > 0:
            args += ["-np", str(threads)]
        return self._stream(args, on_progress, cancel_event)

    # --- Ekstrakcja --------------------------------------------------------

    def extract(
        self,
        media_cmd: str,
        src: Path,
        dst: Path,
        force: bool = True,
        on_progress: Optional[ProgressCB] = None,
        cancel_event: Optional[threading.Event] = None,
        extra_args: Optional[list] = None,
    ) -> CHDManResult:
        """media_cmd: extractcd / extractdvd / extracthd / extractraw / extractld.

        extra_args — np. ["-sb"] (extractcd --splitbin, chdman >= 0.264):
        osobny .bin per ścieżka, jak dzieli Redump."""
        args = [media_cmd, "-i", str(src), "-o", str(dst)]
        if force:
            args.append("-f")
        if extra_args:
            args.extend(str(a) for a in extra_args)
        return self._stream(args, on_progress, cancel_event)

    # --- Kopiowanie / rekompresja (CHD -> CHD, bez pełnego extractu) -------

    def copy(
        self,
        src: Path,
        dst: Path,
        compression: Optional[str] = None,
        threads: int = 0,
        force: bool = True,
        on_progress: Optional[ProgressCB] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> CHDManResult:
        args = ["copy", "-i", str(src), "-o", str(dst)]
        if force:
            args.append("-f")
        if compression:
            args += ["-c", compression]
        if threads and threads > 0:
            args += ["-np", str(threads)]
        return self._stream(args, on_progress, cancel_event)


# --- Parser outputu 'chdman info -v' ----------------------------------------

_INT_RE = re.compile(r"[\d,]+")


def _first_int(line: str) -> int:
    m = _INT_RE.search(line)
    if not m:
        return 0
    return int(m.group(0).replace(",", ""))


def parse_info(text: str, path: Path) -> CHDInfo:
    """Parsuje tekstowy output ``chdman info -v`` do CHDInfo."""
    info = CHDInfo(path=path, raw_info=text)
    for raw in text.splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith("file version"):
            info.version = _first_int(line)
        elif low.startswith("logical size"):
            info.logical_bytes = _first_int(line)
        elif low.startswith("hunk size"):
            info.hunk_bytes = _first_int(line)
        elif low.startswith("unit size"):
            info.unit_bytes = _first_int(line)
        elif low.startswith("total hunks"):
            info.total_hunks = _first_int(line)
        elif low.startswith("chd size"):
            info.chd_bytes = _first_int(line)
        elif low.startswith("compression"):
            # "Compression:  cdlz (CD LZMA), cdzl (CD Deflate), cdfl (CD FLAC)"
            after = line.split(":", 1)[1] if ":" in line else ""
            codecs = re.findall(r"\b([a-z]{2,5})\b\s*\(", after.lower())
            if not codecs:
                codecs = [c.strip() for c in after.lower().split(",") if c.strip()]
            info.compression = codecs
        elif low.startswith("sha1") and not low.startswith("data sha1"):
            info.sha1 = line.split(":", 1)[1].strip() if ":" in line else ""
        elif low.startswith("data sha1"):
            info.data_sha1 = line.split(":", 1)[1].strip() if ":" in line else ""
        elif low.startswith("metadata"):
            tm = re.search(r"tag\s*=\s*'?([A-Za-z0-9]{2,4})'?", line, re.IGNORECASE)
            if tm:
                info.metadata_tags.append(tm.group(1).upper())
    return info

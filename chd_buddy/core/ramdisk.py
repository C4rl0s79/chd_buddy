"""RAM dysk (ImDisk) na operacje tymczasowe: wypakowanie/przepakowanie CHD.

Wszystkie ciężkie ekstrakcje (identyfikacja CD-CHD, przepakowanie kontenera,
round-trip) lądują na ulotnym dysku w RAM zamiast na fizycznym NTFS. Dzięki
temu: nie zapychają dysku kolekcji, nic nie zostaje po przerwaniu (brak
utraconych klastrów), i jest szybciej.

Cykl życia: tworzymy przy starcie programu (inicjalizacja chwilę trwa),
usuwamy przy zamknięciu. Gdy plik nie mieści się w RAM albo ImDisk nie ma —
scratch spada na fizyczny dysk z wolnym miejscem (patrz scratch.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

LogCB = Callable[[str], None]
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Aktywny RAM dysk (ścieżka korzenia) — ustawiany po utworzeniu, czyszczony
# po usunięciu. scratch.py preferuje go, gdy plik się mieści.
_ACTIVE: Optional[Path] = None


def _imdisk_exe() -> Optional[str]:
    exe = shutil.which("imdisk")
    if exe:
        return exe
    cand = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "imdisk.exe"
    return str(cand) if cand.is_file() else None


def available() -> bool:
    return _imdisk_exe() is not None


def active_root() -> Optional[Path]:
    """Korzeń aktywnego RAM dysku albo None."""
    if _ACTIVE is not None and _ACTIVE.is_dir():
        return _ACTIVE
    return None


def reuse_if_exists(letter: str = "R") -> Optional[Path]:
    """Szybko (bez subprocess) rejestruje ISTNIEJĄCY, zapisywalny wolumin
    <letter>: jako aktywny RAM dysk. Do wywołania SYNCHRONICZNIE na starcie —
    dzięki temu scratch widzi RAM dysk z poprzedniej sesji od razu, nawet zanim
    dokończy się pełne create() w tle."""
    global _ACTIVE
    root = Path(f"{letter}:\\")
    if _ready(root):
        _ACTIVE = root
        return root
    return None


def _writable(root: Path) -> bool:
    try:
        probe = root / ".chdbuddy_probe"
        probe.write_bytes(b"x")
        probe.unlink()
        return True
    except OSError:
        return False


def _ready(root: Path) -> bool:
    """Wolumin zamontowany i zapisywalny (wydzielone dla testowalności)."""
    return root.is_dir() and _writable(root)


def create(size_gb: int = 30, letter: str = "R", label: str = "RAMTEMP",
           timeout: float = 20.0, log: Optional[LogCB] = None,
           attempts: int = 4, retry_wait: float = 3.0) -> Optional[Path]:
    """Tworzy RAM dysk ImDisk i zwraca jego korzeń (albo None).

    imdisk -a -s <N>G -m <L>: -p "/fs:ntfs /q /y /v:<label>"
    Czeka aż wolumin będzie zamontowany i zapisywalny (inicjalizacja trwa).

    Tuż po starcie systemu/programu ImDisk bywa chwilowo bez zasobów pamięci
    ("Za mało zasobów pamięci", błąd 3) albo poprzedni wolumin R: jeszcze się
    demontuje — dlatego ponawiamy `attempts` razy z odczekaniem `retry_wait`.
    """
    global _ACTIVE
    exe = _imdisk_exe()
    if exe is None:
        if log:
            log("RAM dysk: brak ImDisk — operacje tymczasowe na dysku "
                "fizycznym z wolnym miejscem.")
        return None
    drive = f"{letter}:"
    root = Path(drive + "\\")
    last_err = ""
    for attempt in range(1, attempts + 1):
        # już istnieje (np. po poprzedniej sesji / poprzedniej próbie) i pisze
        if _ready(root):
            _ACTIVE = root
            if log:
                log(f"RAM dysk: używam istniejącego {drive}")
            return root
        try:
            r = subprocess.run(
                [exe, "-a", "-s", f"{size_gb}G", "-m", drive,
                 "-p", f"/fs:ntfs /q /y /v:{label}"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", creationflags=_FLAGS)
        except OSError as e:
            last_err = str(e)
            r = None
        if r is not None and r.returncode == 0:
            # sukces polecenia — czekaj aż wolumin będzie zapisywalny
            deadline = time.time() + timeout
            while time.time() < deadline:
                if _ready(root):
                    _ACTIVE = root
                    if log:
                        log(f"RAM dysk: gotowy {drive} "
                            f"({size_gb} GB, ulotny).")
                    return root
                time.sleep(0.4)
            last_err = f"nie zainicjalizował się w {timeout:.0f}s"
        elif r is not None:
            last_err = (f"ImDisk zwrócił {r.returncode}: "
                        f"{(r.stderr or r.stdout).strip()[:200]}")
        if attempt < attempts:
            if log:
                log(f"RAM dysk: próba {attempt}/{attempts} nieudana "
                    f"({last_err}) — czekam {retry_wait:.0f}s i ponawiam.")
            time.sleep(retry_wait)
    if log:
        log(f"RAM dysk: nie utworzono {drive} po {attempts} próbach "
            f"({last_err}). Operacje tymczasowe pójdą na dysk fizyczny.")
    return None


def remove(letter: str = "R", log: Optional[LogCB] = None) -> None:
    """Usuwa RAM dysk (imdisk -D -m <L>:) — wywołać przy zamknięciu programu."""
    global _ACTIVE
    _ACTIVE = None
    exe = _imdisk_exe()
    if exe is None:
        return
    drive = f"{letter}:"
    if not Path(drive + "\\").exists():
        return
    try:
        subprocess.run([exe, "-D", "-m", drive],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=_FLAGS)
        if log:
            log(f"RAM dysk: usunięty {drive}.")
    except OSError:
        pass

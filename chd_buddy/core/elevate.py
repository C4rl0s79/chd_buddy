"""Uprawnienia do symlinków na Windows: sprawdzenie i podniesienie (UAC).

Symlink na Windows wymaga albo trybu dewelopera (Ustawienia → System → Dla
deweloperów → Tryb dewelopera), albo uruchomienia procesu jako administrator.
Zasada programu: gdy symlinku NIE DA SIĘ utworzyć — NIC nie kopiujemy w zamian
(żadnych duplikatów zapychających dysk), miejsce po prostu zostaje puste.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def is_admin() -> bool:
    """Czy proces działa z podniesionymi uprawnieniami (Windows)."""
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def can_symlink(probe_dir: Path | None = None) -> bool:
    """Sprawdza FAKTYCZNĄ możliwość utworzenia symlinku (próba w temp).

    Uwzględnia tryb dewelopera — bez niego i bez admina zwróci False.
    """
    base = Path(probe_dir) if probe_dir else Path(tempfile.gettempdir())
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    target = base / f".chdbuddy_probe_target_{os.getpid()}"
    link = base / f".chdbuddy_probe_link_{os.getpid()}"
    try:
        target.write_bytes(b"x")
    except OSError:
        return False
    ok = False
    try:
        os.symlink(str(target), str(link))
        ok = True
    except OSError:
        ok = False
    finally:
        for p in (link, target):
            try:
                if p.is_symlink() or p.exists():
                    os.unlink(p)
            except OSError:
                pass
    return ok


def symlink_status() -> tuple[bool, str]:
    """(czy_można, opis) — do pokazania w GUI."""
    if can_symlink():
        how = "administrator" if is_admin() else "tryb dewelopera"
        return True, f"Symlinki: DOSTĘPNE ({how})."
    return False, ("Symlinki: NIEDOSTĘPNE — brak uprawnień. Włącz tryb "
                   "dewelopera (Ustawienia → System → Dla deweloperów) albo "
                   "uruchom program jako administrator. Bez tego miejsca "
                   "dzieci zostaną PUSTE (nic nie kopiujemy).")


def relaunch_as_admin() -> bool:
    """Uruchamia ponownie TEN program z prośbą o podniesienie (UAC).

    Zwraca True, gdy udało się wystartować nowy proces (wtedy wywołujący
    powinien się zamknąć). False = użytkownik odmówił albo błąd.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
    except Exception:
        return False
    exe = sys.executable or ""
    if not exe:
        return False
    # uruchamiamy modułowo, tak jak startuje GUI: pythonw -m chd_buddy.main
    # `--no-elevate` = twardy stop pętli: podniesiony proces już nie próbuje
    # podnosić się ponownie (poza kontrolą is_admin, na wszelki wypadek).
    args = "-m chd_buddy.main --no-elevate"
    workdir = str(Path(__file__).resolve().parents[2])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, args, workdir, 1)
    except Exception:
        return False
    return int(rc) > 32          # <=32 => błąd/odmowa UAC

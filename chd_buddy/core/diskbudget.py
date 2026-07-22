"""Preflight wolnego miejsca dla pojedynczego pliku.

Model: nigdy nie trzymamy więcej niż roboczy zestaw JEDNEGO pliku. Dzięki temu
batch 3 TB może działać przy kilku GB wolnego, o ile najgorszy pojedynczy plik
mieści się w dostępnej przestrzeni.

Strategie:
- RECOMPRESS (chdman copy): CHD->CHD bez pełnego extractu.
    peak = old_chd + new_chd
- RETYPE bezpieczny: extract + recreate, oryginał trzymany do weryfikacji.
    peak = old_chd + extract(logical) + new_chd
- RETYPE agresywny (aggressive_low_disk): usuń oryginał po udanym extract.
    peak = max(old_chd + extract, extract + new_chd)
  Ryzyko: chwilowo istnieje tylko extract; odzysk możliwy przez ponowne create.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .models import CHDInfo, DiskBudget


def _estimate_new_chd_bytes(info: CHDInfo, safety_factor: float) -> int:
    """Szacowany rozmiar nowego CHD (zwykle ~ stary; z zapasem)."""
    base = info.chd_bytes or info.file_bytes or info.logical_bytes
    return int(base * max(1.0, safety_factor))


def free_bytes_for(path: Path) -> int:
    """Wolne miejsce na woluminie, na którym leży (lub powstanie) ścieżka."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def budget_recompress(
    info: CHDInfo,
    work_dir: Path,
    safety_factor: float = 1.15,
    reserve: int = 0,
) -> DiskBudget:
    new_chd = _estimate_new_chd_bytes(info, safety_factor)
    peak = new_chd + reserve  # stary CHD już zajmuje miejsce; liczymy PRZYROST
    breakdown = {"new_chd": new_chd, "reserve": reserve}
    return DiskBudget(
        free_bytes=free_bytes_for(work_dir),
        required_peak_bytes=peak,
        strategy="recompress",
        breakdown=breakdown,
    )


def budget_retype(
    info: CHDInfo,
    work_dir: Path,
    aggressive: bool = False,
    safety_factor: float = 1.15,
    reserve: int = 0,
) -> DiskBudget:
    old_chd = info.file_bytes or info.chd_bytes
    extract = info.logical_bytes
    new_chd = _estimate_new_chd_bytes(info, safety_factor)
    if aggressive:
        # oryginał usuwany po extract => szczyt to większa z dwóch faz
        peak = max(extract, new_chd) + reserve  # przyrost względem istniejącego CHD
        breakdown = {"extract": extract, "new_chd": new_chd, "reserve": reserve,
                     "mode": 1}
    else:
        # extract + nowy CHD powstają obok istniejącego oryginału
        peak = extract + new_chd + reserve
        breakdown = {"old_chd": old_chd, "extract": extract, "new_chd": new_chd,
                     "reserve": reserve, "mode": 0}
    return DiskBudget(
        free_bytes=free_bytes_for(work_dir),
        required_peak_bytes=peak,
        strategy="retype_aggressive" if aggressive else "retype_safe",
        breakdown=breakdown,
    )

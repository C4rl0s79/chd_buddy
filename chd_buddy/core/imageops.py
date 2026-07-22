"""Operacje na obrazach potrzebne przy naprawie typu nośnika.

Gdy DVD-owy .iso zostanie błędnie spakowany przez ``createcd``, ``extractcd``
zwróci .cue + .bin. Aby odtworzyć oryginalny obraz do ``createdvd`` potrzebny
jest .iso (sektory 2048 B). W zależności od trybu ścieżki w .cue:
  - MODE1/2048 -> .bin JEST już obrazem 2048 B (wystarczy użyć wprost),
  - MODE1/2352 -> trzeba wyłuskać 2048 B danych użytkownika z każdej ramki.

UWAGA: deframing MODE1/2352 jest wrażliwy i wymaga potwierdzenia na realnym
zbiorze (walidacja DAT). MODE1/2048 jest bezpieczny (kopia 1:1).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_TRACK_RE = re.compile(r"TRACK\s+\d+\s+(\w+/?\d*)", re.IGNORECASE)
_FILE_RE = re.compile(r'FILE\s+"([^"]+)"', re.IGNORECASE)

# Rozmiar ramki fizycznej i offset danych użytkownika dla MODE1.
_CD_RAW = 2352
_MODE1_USER = 2048
_MODE1_HEADER = 16  # sync(12) + header(4)


@dataclass
class CueTrackInfo:
    bin_path: Optional[Path]
    mode: str            # np. "MODE1/2048", "MODE1/2352"
    track_count: int
    sector_size: int     # 2048 lub 2352


def parse_cue(cue: Path) -> CueTrackInfo:
    text = cue.read_text(encoding="utf-8", errors="replace")
    files = _FILE_RE.findall(text)
    tracks = _TRACK_RE.findall(text)
    mode = tracks[0].upper() if tracks else ""
    if "/2352" in mode or mode.endswith("2352"):
        sector = _CD_RAW
    elif "/2048" in mode or mode.endswith("2048"):
        sector = _MODE1_USER
    else:
        sector = _MODE1_USER  # domyślnie zakładamy dane 2048
    bin_path = (cue.parent / files[0]).resolve() if files else None
    return CueTrackInfo(
        bin_path=bin_path,
        mode=mode,
        track_count=len(tracks),
        sector_size=sector,
    )


def is_safe_single_data_track(info: CueTrackInfo) -> bool:
    """Czy obraz to pojedyncza ścieżka danych MODE1 (bezpieczny retype)."""
    return info.track_count == 1 and info.mode.startswith("MODE1")


def bin_to_iso(bin_path: Path, sector_size: int, iso_out: Path,
               chunk_sectors: int = 4096) -> Path:
    """Tworzy .iso (2048 B/sektor) z .bin. Dla 2048 to kopia, dla 2352 deframing."""
    if sector_size == _MODE1_USER:
        # Już 2048 — wystarczy przeniesienie/kopiowanie zawartości.
        with bin_path.open("rb") as src, iso_out.open("wb") as dst:
            while True:
                buf = src.read(chunk_sectors * _MODE1_USER)
                if not buf:
                    break
                dst.write(buf)
        return iso_out

    if sector_size == _CD_RAW:
        with bin_path.open("rb") as src, iso_out.open("wb") as dst:
            while True:
                frame = src.read(_CD_RAW)
                if len(frame) < _CD_RAW:
                    break
                dst.write(frame[_MODE1_HEADER:_MODE1_HEADER + _MODE1_USER])
        return iso_out

    raise ValueError(f"Nieobsługiwany rozmiar sektora: {sector_size}")

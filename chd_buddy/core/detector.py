"""Wykrywanie typu nośnika (CD / DVD / HD) dla źródeł i CHD.

Problem: samo rozszerzenie .iso nie mówi, czy to CD-ROM czy DVD-ROM.
chdman rozdziela je na createcd vs createdvd, a błędny wybór daje obraz,
który nie przechodzi walidacji DAT po ekstrakcji.

Strategia dla .iso:
  1. Rozmiar pliku — najpewniejszy sygnał praktyczny. CD (nawet z overburn)
     rzadko przekracza ~870-950 MB; powyżej => DVD.
  2. Odczyt Primary Volume Descriptor (sektor 16, offset 32768) w celu
     potwierdzenia struktury ISO9660 i policzenia rozmiaru logicznego.
  3. Detekcja sekwencji UDF (sektory 16+, znaczniki NSR0x) jako dodatkowy,
     lecz niepewny sygnał DVD (część PS2 DVD używa czystego ISO9660).

Detekcja typu z istniejącego CHD opiera się na CHDInfo (unit size, kodeki,
tagi metadanych) — patrz models.CHDInfo.
"""
from __future__ import annotations

from pathlib import Path

from .models import (
    CD_MAX_LOGICAL_BYTES,
    DVD_SECTOR,
    MediaType,
    SourceType,
)

# Progi rozmiaru dla .iso (bajty).
_ISO_CD_SOFT_LIMIT = 900 * 1024 * 1024   # powyżej => bardzo prawdopodobne DVD
_ISO_CD_HARD_LIMIT = 950 * 1024 * 1024   # powyżej => praktycznie na pewno DVD

# Mapowanie rozszerzenie -> SourceType
EXT_MAP: dict[str, SourceType] = {
    ".cue": SourceType.CUE,
    ".gdi": SourceType.GDI,
    ".toc": SourceType.TOC,
    ".nrg": SourceType.NRG,
    ".cdr": SourceType.CDR,
    ".iso": SourceType.ISO,
    ".img": SourceType.IMG,
    ".raw": SourceType.RAW,
    ".chd": SourceType.CHD,
    ".zip": SourceType.ARCHIVE,
    ".7z": SourceType.ARCHIVE,
    ".rar": SourceType.ARCHIVE,
}


class Detection:
    """Wynik detekcji: typ, pewność 0..1 i uzasadnienie."""

    __slots__ = ("media", "confidence", "reason")

    def __init__(self, media: MediaType, confidence: float, reason: str):
        self.media = media
        self.confidence = confidence
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover
        return f"Detection({self.media.value}, {self.confidence:.2f}, {self.reason!r})"


def source_type_of(path: Path) -> SourceType:
    return EXT_MAP.get(path.suffix.lower(), SourceType.UNKNOWN)


def _has_udf(path: Path) -> bool:
    """Szuka znaczników NSR02/NSR03 w Volume Recognition Sequence."""
    try:
        with path.open("rb") as fh:
            # VRS zaczyna się od sektora 16; sprawdź kilka kolejnych sektorów.
            for sector in range(16, 20):
                fh.seek(sector * DVD_SECTOR)
                block = fh.read(7)
                if len(block) < 7:
                    break
                ident = block[1:6]
                if ident in (b"NSR02", b"NSR03"):
                    return True
    except OSError:
        return False
    return False


def _read_iso9660_logical_size(path: Path) -> int:
    """Zwraca rozmiar logiczny z Primary Volume Descriptor lub 0."""
    try:
        with path.open("rb") as fh:
            fh.seek(16 * DVD_SECTOR)
            pvd = fh.read(DVD_SECTOR)
        if len(pvd) < 132:
            return 0
        # Bajt 0 = typ deskryptora (1 = PVD), bajty 1..5 = "CD001".
        if pvd[0] != 1 or pvd[1:6] != b"CD001":
            return 0
        # Volume Space Size: little-endian pod offsetem 80 (LSB kopia).
        blocks = int.from_bytes(pvd[80:84], "little")
        # Logical Block Size: LE pod offsetem 128.
        block_size = int.from_bytes(pvd[128:130], "little") or DVD_SECTOR
        return blocks * block_size
    except OSError:
        return 0


def detect_iso_media(path: Path) -> Detection:
    """Klasyfikuje .iso jako CD lub DVD."""
    try:
        size = path.stat().st_size
    except OSError:
        return Detection(MediaType.UNKNOWN, 0.0, "brak dostępu do pliku")

    reasons: list[str] = []
    if size >= _ISO_CD_HARD_LIMIT:
        return Detection(
            MediaType.DVD, 0.97,
            f"rozmiar {size / 1024 / 1024:.0f} MB > limit CD",
        )
    if _has_udf(path):
        reasons.append("wykryto UDF")
        return Detection(MediaType.DVD, 0.9, "; ".join(reasons))
    if size >= _ISO_CD_SOFT_LIMIT:
        reasons.append(f"rozmiar {size / 1024 / 1024:.0f} MB blisko/ponad limit CD")
        return Detection(MediaType.DVD, 0.8, "; ".join(reasons))

    # Poniżej limitu — prawdopodobnie CD, ale niepewne dla małych DVD.
    logical = _read_iso9660_logical_size(path)
    if logical and logical >= _ISO_CD_HARD_LIMIT:
        return Detection(MediaType.DVD, 0.85, "rozmiar logiczny ISO9660 > limit CD")
    return Detection(
        MediaType.CD, 0.6,
        f"rozmiar {size / 1024 / 1024:.0f} MB w zakresie CD (możliwy mały DVD)",
    )


def detect_source_media(path: Path) -> Detection:
    """Zwraca detekcję typu nośnika dla dowolnego wspieranego źródła."""
    st = source_type_of(path)
    if st in (SourceType.CUE, SourceType.GDI, SourceType.TOC,
              SourceType.NRG, SourceType.CDR):
        return Detection(MediaType.CD, 0.95, f"format {st.value} => CD")
    if st == SourceType.ISO:
        return detect_iso_media(path)
    if st in (SourceType.IMG, SourceType.RAW):
        return Detection(MediaType.HD, 0.5, "obraz surowy => HD/RAW (wymaga potwierdzenia)")
    return Detection(MediaType.UNKNOWN, 0.0, "nierozpoznane źródło")

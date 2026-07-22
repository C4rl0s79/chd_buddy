"""Presety kompresji zależne od typu nośnika.

KRYTYCZNE: createcd wymaga kodeków CD (cdlz/cdzl/cdfl), a createdvd/createhd
kodeków ogólnych (lzma/zlib/huff/flac). Podanie złych kodeków = błąd chdman.
Domyślnie zwracamy None -> chdman sam dobiera poprawne wartości dla polecenia.
"""
from __future__ import annotations

from typing import Optional

from .models import MediaType

# Presety per typ nośnika. "default" => None (chdman decyduje).
_PRESETS: dict[str, dict[MediaType, Optional[str]]] = {
    "default": {
        MediaType.CD: None,
        MediaType.DVD: None,
        MediaType.HD: None,
        MediaType.RAW: None,
        MediaType.LD: None,
    },
    "max": {
        MediaType.CD: "cdlz,cdzl,cdfl",
        MediaType.DVD: "lzma,zlib,huff,flac",
        MediaType.HD: "lzma,zlib,huff,flac",
        MediaType.RAW: "lzma,zlib,huff,flac",
        MediaType.LD: "avhu",
    },
    "fast": {
        MediaType.CD: "cdzl",
        MediaType.DVD: "zlib",
        MediaType.HD: "zlib",
        MediaType.RAW: "zlib",
        MediaType.LD: "avhu",
    },
    "none": {
        MediaType.CD: "none",
        MediaType.DVD: "none",
        MediaType.HD: "none",
        MediaType.RAW: "none",
        MediaType.LD: "none",
    },
}

PRESET_NAMES = tuple(_PRESETS.keys())


def compression_for(preset: str, media: MediaType) -> Optional[str]:
    """Zwraca łańcuch kompresji dla polecenia chdman albo None (domyślne)."""
    table = _PRESETS.get(preset, _PRESETS["default"])
    return table.get(media)

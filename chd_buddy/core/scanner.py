"""Skanowanie wejścia i budowa listy SourceItem gotowych do konwersji.

Zasady:
- .cue / .gdi / .toc opisują ścieżki -> traktujemy jako jeden element, a pliki
  .bin/.raw wskazane wewnątrz stają się companionami (nie osobnymi zadaniami).
- Luźne .bin bez .cue są ignorowane (nie da się ich jednoznacznie spakować).
- Archiwa .zip/.7z/.rar zwracamy jako element ARCHIVE (rozpakowanie robi worker
  tuż przed konwersją, żeby nie zajmować miejsca przedwcześnie).
"""
from __future__ import annotations

import re
from pathlib import Path

from .detector import detect_source_media, source_type_of
from .models import MediaType, SourceItem, SourceType

# Rozszerzenia plików wskazywanych przez .cue/.gdi (companiony do pominięcia
# jako osobne zadania).
_COMPANION_EXTS = {".bin", ".raw", ".img"}

_CUE_FILE_RE = re.compile(r'FILE\s+"([^"]+)"', re.IGNORECASE)


def _cue_companions(cue: Path) -> list[Path]:
    out: list[Path] = []
    try:
        text = cue.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for m in _CUE_FILE_RE.finditer(text):
        ref = (cue.parent / m.group(1)).resolve()
        out.append(ref)
    return out


def _gdi_companions(gdi: Path) -> list[Path]:
    out: list[Path] = []
    try:
        lines = gdi.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines[1:]:  # pierwsza linia = liczba ścieżek
        parts = line.split()
        if len(parts) >= 5:
            fname = parts[4].strip('"')
            out.append((gdi.parent / fname).resolve())
    return out


def scan_paths(paths: list[Path], recursive: bool = True) -> list[SourceItem]:
    """Skanuje pliki/foldery i zwraca zdeduplikowaną listę SourceItem."""
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            files.extend(f for f in it if f.is_file())

    # Zbierz companiony wskazane przez .cue/.gdi/.toc, żeby ich nie liczyć osobno.
    consumed: set[Path] = set()
    descriptors: list[tuple[Path, SourceType, list[Path]]] = []
    for f in files:
        st = source_type_of(f)
        if st in (SourceType.CUE, SourceType.GDI):
            comps = _cue_companions(f) if st == SourceType.CUE else _gdi_companions(f)
            descriptors.append((f, st, comps))
            for c in comps:
                consumed.add(c.resolve())

    items: list[SourceItem] = []
    seen_keys: set[str] = set()

    def _add(item: SourceItem) -> None:
        key = item.path.stem.lower() if item.source_type in (
            SourceType.CUE, SourceType.GDI, SourceType.TOC,
        ) else str(item.path.resolve()).lower()
        if key in seen_keys:
            return
        seen_keys.add(key)
        items.append(item)

    # Najpierw deskryptory (mają pierwszeństwo nad luźnymi companionami).
    for path, st, comps in descriptors:
        det = detect_source_media(path)
        _add(SourceItem(
            path=path, source_type=st, media_type=det.media,
            companions=comps, confidence=det.confidence, detect_reason=det.reason,
        ))

    # Reszta plików.
    for f in files:
        st = source_type_of(f)
        if st == SourceType.UNKNOWN:
            continue
        if st in (SourceType.CUE, SourceType.GDI):
            continue  # już dodane wyżej
        if f.resolve() in consumed and st in (SourceType.IMG, SourceType.RAW):
            continue  # companion .bin/.img wskazany przez .cue/.gdi
        if f.suffix.lower() in _COMPANION_EXTS and f.resolve() in consumed:
            continue
        det = detect_source_media(f)
        _add(SourceItem(
            path=f, source_type=st, media_type=det.media,
            confidence=det.confidence, detect_reason=det.reason,
        ))

    return items

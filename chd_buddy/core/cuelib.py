"""Biblioteka plików .cue (paczki Redump „Cuesheets") — indeks po SHA-1.

Cue jest normalnym ROM-em w DAT-cie (ma sumę SHA-1), więc każdy plik z
biblioteki da się jednoznacznie przypisać do gry. Biblioteka czyta paczki
.zip (bez wypakowywania na dysk) i luźne pliki .cue z katalogu
``<dat_root>/cues``.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

LogCB = Callable[[str], None]


class CueLibrary:
    """sha1 -> (źródło, członek) dla wszystkich cue w katalogu biblioteki."""

    def __init__(self, root: Path, log: Optional[LogCB] = None):
        self.root = Path(root)
        # sha1 -> (ścieżka zip albo pliku, nazwa członka w zipie albo "")
        self._by_sha1: Dict[str, Tuple[Path, str]] = {}
        if not self.root.is_dir():
            return
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() == ".zip":
                try:
                    with zipfile.ZipFile(p) as z:
                        for name in z.namelist():
                            if not name.lower().endswith(".cue"):
                                continue
                            data = z.read(name)
                            h = hashlib.sha1(data).hexdigest()
                            self._by_sha1.setdefault(h, (p, name))
                except Exception as e:      # zła paczka nie może ubić skanu
                    if log:
                        log(f"CUE: nieczytelny zip {p.name}: {e}")
            elif p.suffix.lower() == ".cue":
                try:
                    h = hashlib.sha1(p.read_bytes()).hexdigest()
                    self._by_sha1.setdefault(h, (p, ""))
                except OSError:
                    continue
        if log:
            log(f"Biblioteka cue: {len(self._by_sha1)} plików ({self.root})")

    def __len__(self) -> int:
        return len(self._by_sha1)

    def load(self, sha1: str) -> Optional[bytes]:
        """Zawartość cue o danym SHA-1 (z zipa albo pliku) — albo None."""
        hit = self._by_sha1.get((sha1 or "").lower())
        if hit is None:
            return None
        src, member = hit
        try:
            if member:
                with zipfile.ZipFile(src) as z:
                    return z.read(member)
            return src.read_bytes()
        except Exception:
            return None

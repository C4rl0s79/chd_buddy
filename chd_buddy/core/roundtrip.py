"""Walidacja round-trip: dowód bajt-w-bajt, że nowy CHD zawiera dokładnie te
dane, które w niego zapakowano.

Dlaczego to konieczne: ``chdman verify`` potwierdza tylko integralność
*kontenera* CHD (hunki dają się rozpakować), a NIE to, że dane logiczne są
zgodne ze źródłem. Przy nieodwracalnej naprawie in-place (po weryfikacji
oryginał jest kasowany) i braku miejsca na kopie zapasowe potrzebujemy
twardszego dowodu.

Mechanizm: liczymy SHA-1 obrazu tuż przed pakowaniem (``createdvd``), a po
spakowaniu wypakowujemy obraz z powrotem (``extractdvd``) i porównujemy hash.
Zgodność => pakowanie/rozpakowanie jest przezroczyste, więc oryginał można
bezpiecznie podmienić. Rozbieżność => rollback, oryginał nietknięty.

Uwaga o miejscu: plik kontrolny jest kasowany natychmiast po zhashowaniu,
a wołający powinien usunąć obraz źródłowy przed re-ekstrakcją, więc w danym
momencie na dysku leży najwyżej jeden obraz naraz (przyjazne dla low-disk).
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Callable, Optional

from .chdman import CHDMan
from .models import MediaType

ProgressCB = Callable[[float, str], None]
_CHUNK = 8 * 1024 * 1024  # 8 MB


def sha1_file(path: Path, on_progress: Optional[ProgressCB] = None) -> str:
    """Strumieniowy SHA-1 pliku (stała pamięć niezależnie od rozmiaru)."""
    h = hashlib.sha1()
    total = path.stat().st_size or 1
    done = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if on_progress is not None:
                on_progress(done * 100.0 / total, "hashowanie")
    return h.hexdigest()


def extract_and_hash(
    chd: CHDMan,
    chd_path: Path,
    media: MediaType,
    work_dir: Path,
    on_progress: Optional[ProgressCB] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """Wypakowuje obraz z ``chd_path`` właściwym poleceniem i zwraca jego SHA-1.

    Plik tymczasowy usuwany jest natychmiast po zhashowaniu (oszczędność miejsca).
    """
    extract_cmd = media.extract_cmd or "extractdvd"
    out = work_dir / (chd_path.stem + ".rtcheck.img")
    out.unlink(missing_ok=True)
    try:
        res = chd.extract(extract_cmd, chd_path, out, on_progress=on_progress,
                          cancel_event=cancel_event)
        if not res.ok:
            raise RuntimeError(
                f"round-trip: extract nie powiódł się (kod {res.returncode})")
        return sha1_file(out, on_progress=on_progress)
    finally:
        out.unlink(missing_ok=True)

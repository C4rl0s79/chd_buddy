"""Operacje plikowe z raportowaniem postępu (kopiowanie/przenoszenie kawałkami).

Duże pliki (CHD/ISO — zwłaszcza z RAM dysku na fizyczny) potrafią kopiować się
kilkadziesiąt sekund. `shutil.move`/`copyfileobj` nie dają znaku życia, więc
pasek stał. Tu kopiujemy blokami i po każdym raportujemy postęp bajtowy —
każda operacja plikowa jest widoczna.

Kontrakt callbacku: on_progress(done_bytes, total_bytes, label).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Optional

ProgCB = Callable[[int, int, str], None]
_CHUNK = 8 * 1024 * 1024        # 8 MiB — kompromis szybkość / częstość aktualizacji


def copy_with_progress(src, dst, on_progress: Optional[ProgCB] = None,
                       label: str = "", chunk: int = _CHUNK) -> int:
    """Kopiuje src→dst blokami, raportując on_progress(done, total, label).

    Zachowuje metadane (copystat). Zwraca liczbę skopiowanych bajtów.
    Pusty plik też wywoła on_progress(0, 0, label) raz (widać etykietę).
    """
    src, dst = Path(src), Path(dst)
    try:
        total = src.stat().st_size
    except OSError:
        total = 0
    done = 0
    if on_progress is not None:
        on_progress(0, total, label)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(buf)
            done += len(buf)
            if on_progress is not None:
                on_progress(done, total, label)
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass
    return done


def move_with_progress(src, dst, on_progress: Optional[ProgCB] = None,
                       label: str = "") -> None:
    """Przenosi src→dst. Ten sam wolumin → os.replace (natychmiast, bez kopii).
    Inny wolumin (np. RAM R: → D:) → kopia blokami z postępem + usunięcie
    źródła. Przy błędzie w połowie kopii kasujemy plik tymczasowy (nic
    niekompletnego nie ląduje pod docelową nazwą)."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)        # atomowy rename (ten sam dysk)
        return
    except OSError:
        pass                        # cross-device — kopiujemy niżej
    tmp = dst.with_name(dst.name + ".chdbuddy_move_tmp")
    try:
        copy_with_progress(src, tmp, on_progress, label)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.unlink(src)              # źródło usuwamy dopiero po udanej kopii
    except OSError:
        pass

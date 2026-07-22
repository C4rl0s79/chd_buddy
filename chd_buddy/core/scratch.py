"""Wybór katalogu roboczego (scratch) na dysku z WOLNYM miejscem.

Ekstrakcja CHD do identyfikacji wypakowuje pełny obraz (nawet ~2× rozmiar
CHD). Gdy dysk z plikiem jest pełny, wypakowanie pada. Ale wynik ekstrakcji
tylko HASHUJEMY (nie zostaje) — więc może powstać na DOWOLNYM dysku, który ma
miejsce. Wybieramy dysk z największym zapasem (najpierw preferowany), tworzymy
tam katalog `chdbuddy_scratch`.
"""
from __future__ import annotations

import os
import shutil
import string
from pathlib import Path
from typing import Optional

_SCRATCH_NAME = "chdbuddy_scratch"


def _free(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _candidate_roots(prefer: Optional[str]) -> list[str]:
    roots: list[str] = []
    if prefer:
        roots.append(str(prefer))
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            if os.path.isdir(d):
                roots.append(d)
    else:
        roots.extend(["/tmp", "/var/tmp", str(Path.home())])
    # unikalne, z zachowaniem kolejności
    seen: set[str] = set()
    out: list[str] = []
    for r in roots:
        k = os.path.normcase(os.path.abspath(r))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def pick_scratch_root(need_bytes: int, prefer: Optional[str] = None,
                      log=None, fallback: Optional[str] = None) -> Optional[Path]:
    """Katalog scratch na dysku z >= need_bytes wolnego (największy zapas).

    Kolejność: RAM dysk (gdy aktywny i się mieści) → `fallback` (dedykowany
    katalog temp z ustawień, gdy podany i ma miejsce) → `prefer` (np. obok
    pliku) → dysk z największym zapasem.
    Zwraca gotowy do zapisu katalog albo None, gdy ŻADEN dysk nie ma zapasu.
    log(msg) — opcjonalny; loguje DLACZEGO wybrano dany dysk.
    """
    def _log(m: str) -> None:
        if log:
            log(m)

    need = max(int(need_bytes), 0)
    gb = need / 1024**3
    # RAM DYSK ma pierwszeństwo, gdy plik się mieści (ulotny, szybki, nie
    # zapycha fizycznego dysku ani nie zostawia utraconych klastrów).
    from . import ramdisk
    ram = ramdisk.active_root()
    if ram is None:
        _log(f"scratch: RAM dysk NIEaktywny (nie utworzony/wykryty) — "
             f"potrzeba ~{gb:.1f} GB, szukam na dysku fizycznym.")
    else:
        ram_free = _free(str(ram))
        if ram_free >= need:
            p = ram / _SCRATCH_NAME
            try:
                p.mkdir(parents=True, exist_ok=True)
                _log(f"scratch: RAM dysk {ram} ({ram_free/1024**3:.1f} GB "
                     f"wolne, potrzeba {gb:.1f} GB).")
                return p
            except OSError as e:
                _log(f"scratch: RAM dysk {ram} niezapisywalny ({e}).")
        else:
            _log(f"scratch: RAM dysk {ram} ZA MAŁY — {ram_free/1024**3:.1f} GB "
                 f"wolne < {gb:.1f} GB potrzeba; spadam na fallback/dysk.")
    # DEDYKOWANY fallback z ustawień (scratch_dir) — po RAM, przed „obok pliku"
    if fallback and _free(str(fallback)) >= need:
        p = Path(fallback) / _SCRATCH_NAME
        try:
            p.mkdir(parents=True, exist_ok=True)
            _log(f"scratch: katalog temp z ustawień {fallback}.")
            return p
        except OSError:
            pass
    # preferowany, jeśli sam ma dość miejsca — bez przeskakiwania na inny dysk
    if prefer and _free(str(prefer)) >= need:
        p = Path(prefer) / _SCRATCH_NAME
        try:
            p.mkdir(parents=True, exist_ok=True)
            _log(f"scratch: dysk fizyczny (obok pliku) {prefer}.")
            return p
        except OSError:
            pass
    best: Optional[str] = None
    best_free = -1
    for root in _candidate_roots(prefer):
        fr = _free(root)
        if fr >= need and fr > best_free:
            best, best_free = root, fr
    if best is None:
        _log(f"scratch: BRAK miejsca na jakimkolwiek dysku (~{gb:.1f} GB).")
        return None
    p = Path(best) / _SCRATCH_NAME
    try:
        p.mkdir(parents=True, exist_ok=True)
        _log(f"scratch: dysk fizyczny {best} ({best_free/1024**3:.1f} GB wolne).")
        return p
    except OSError:
        return None

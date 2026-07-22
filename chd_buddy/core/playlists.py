"""Playlisty .m3u dla gier multi-disc (wchłonięty make_m3u.py).

Dwa układy, automatycznie per katalog:
1. płaski — dyski jako pliki w JEDNYM katalogu:
     Final Fantasy VII (Disc 1).chd … -> Final Fantasy VII.m3u
2. podkatalogi — każdy dysk w OSOBNYM katalogu (bin/cue):
     Gra (Disc 1)/game.cue … -> Gra.m3u (wpisy "Podkatalog/plik.cue")

Skan rekurencyjny; format zapisu: LF, UTF-8 bez BOM (Batocera/RetroArch).
Katalogi zarządzane przez kombajn (icons/shortcuts) są pomijane.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# (Disc 1) / [Disk 2] / (CD 1) / (Dysk 1) / (Disc 1 of 3)
DISC_RE = re.compile(
    r"\s*[\(\[]\s*(?:Disc|Disk|CD|Dysk)\s*(\d+)(?:\s*of\s*\d+)?\s*[\)\]]",
    re.IGNORECASE)

# Priorytet rozszerzeń przy wyborze "głównego" pliku dysku (mniej = lepszy).
EXT_PRIORITY = {
    "cue": 0, "gdi": 1, "chd": 2, "iso": 3, "pbp": 4,
    "ccd": 5, "nrg": 6, "mdf": 7, "img": 8, "bin": 9,
}
DISC_EXTS = set(EXT_PRIORITY)

# Podkatalogi kombajnu — nie zawierają dysków.
_SKIP_DIRS = {"icons", "shortcuts", "images", "manuals", "videos"}


def strip_disc(name: str) -> str:
    return re.sub(r"\s{2,}", " ", DISC_RE.sub(" ", name)).strip()


def _disc_number(name: str) -> int:
    m = DISC_RE.search(name)
    return int(m.group(1)) if m else 0


def _ext(p: Path) -> str:
    return p.suffix.lower().lstrip(".")


def _pick_main_file(files: list[Path]) -> Optional[Path]:
    cand = [f for f in files if _ext(f) in DISC_EXTS]
    if not cand:
        return None
    return min(cand, key=lambda f: (EXT_PRIORITY.get(_ext(f), 99), f.name.lower()))


@dataclass
class M3uGroup:
    title: str
    mode: str                 # "flat" | "subdir"
    discs: list[str] = field(default_factory=list)
    m3u_path: Path = Path()


def _groups_flat(dirpath: Path) -> list[M3uGroup]:
    best: dict[tuple[str, int], Path] = {}
    try:
        entries = sorted(p for p in dirpath.iterdir() if p.is_file())
    except OSError:
        return []
    for f in entries:
        if _ext(f) not in DISC_EXTS or not DISC_RE.search(f.name):
            continue
        key = (strip_disc(f.stem), _disc_number(f.name))
        cur = best.get(key)
        if cur is None or (EXT_PRIORITY.get(_ext(f), 99)
                           < EXT_PRIORITY.get(_ext(cur), 99)):
            best[key] = f
    by_title: dict[str, list[tuple[int, Path]]] = {}
    for (title, n), f in best.items():
        by_title.setdefault(title, []).append((n, f))
    out = []
    for title, items in by_title.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda t: (t[0], t[1].name.lower()))
        out.append(M3uGroup(title, "flat", [f.name for _, f in items],
                            dirpath / f"{title}.m3u"))
    return out


def _groups_subdirs(dirpath: Path) -> list[M3uGroup]:
    try:
        subdirs = sorted(d for d in dirpath.iterdir() if d.is_dir())
    except OSError:
        return []
    by_title: dict[str, list[tuple[int, str]]] = {}
    for d in subdirs:
        if not DISC_RE.search(d.name):
            continue
        try:
            inner = [p for p in sorted(d.iterdir()) if p.is_file()]
        except OSError:
            continue
        main = _pick_main_file(inner)
        if main is None:
            continue
        by_title.setdefault(strip_disc(d.name), []).append(
            (_disc_number(d.name), f"{d.name}/{main.name}"))
    out = []
    for title, items in by_title.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda t: (t[0], t[1].lower()))
        out.append(M3uGroup(title, "subdir", [rel for _, rel in items],
                            dirpath / f"{title}.m3u"))
    return out


def scan_m3u(root: Path) -> list[M3uGroup]:
    """Rekurencyjnie znajduje wszystkie grupy multi-disc pod root."""
    seen: set[str] = set()
    result: list[M3uGroup] = []
    for dirpath, dirnames, _files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        d = Path(dirpath)
        for g in (_groups_flat(d) + _groups_subdirs(d)):
            key = os.path.normcase(str(g.m3u_path))
            if key in seen:
                continue
            seen.add(key)
            result.append(g)
    result.sort(key=lambda g: str(g.m3u_path).lower())
    return result


def write_m3u(group: M3uGroup) -> None:
    """LF, UTF-8 bez BOM — zgodnie z Batocera/RetroArch."""
    content = "\n".join(group.discs) + "\n"
    with open(group.m3u_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


@dataclass
class M3uStats:
    created: int = 0
    skipped: int = 0

    def summary(self) -> str:
        return f"utworzono {self.created}, istniejące pominięto {self.skipped}"


def generate_m3u(root: Path, *, overwrite: bool = False, dry_run: bool = False,
                 log: Optional[Callable[[str], None]] = None) -> M3uStats:
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"'{root}' nie jest katalogiem")
    stats = M3uStats()
    for g in scan_m3u(root):
        exists = g.m3u_path.exists()
        if exists and not overwrite:
            stats.skipped += 1
            continue
        if log:
            rel = g.m3u_path.relative_to(root)
            log(f"[{'PODGLĄD' if dry_run else ('NADPISZ' if exists else 'TWÓRZ')}]"
                f" [{g.mode}] {rel}  ({len(g.discs)} dysków)")
        if not dry_run:
            write_m3u(g)
        stats.created += 1
    return stats

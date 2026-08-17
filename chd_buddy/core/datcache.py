"""Przyrostowy cache sparsowanych DAT-ów.

Parsowanie 391 DAT-ów (1,36 mln ROM-ów) z XML jest wolne — a DAT-y zmieniają
się rzadko. Cache trzyma per plik: (mtime, rozmiar) + nazwa z nagłówka +
lista gier. Przy kolejnym wczytaniu niezmienione DAT-y ładują się z cache;
tylko nowe/zmienione są parsowane ponownie. Cache jest zapisywany po każdym
odkryciu, jeśli coś się zmieniło.

Format pliku: pickle {abspath: {"sig": (mtime_ns, size), "name": str,
"games": [DatGame]}}. Wersjonowany — niezgodna wersja = cache ignorowany.
"""
from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Optional

from .datfile import DatGame, parse_dat, parse_dat_header
from .settings import app_base_dir

CACHE_FILENAME = "dat_parse_cache.pkl"
CACHE_VERSION = 3      # v3: DatGame.cloneof/romof + DatRom.merge (MAME)

REPORT_CACHE_FILENAME = "report_state_cache.pkl"
REPORT_CACHE_VERSION = 4      # v4: + archive_names_ok (zła nazwa w archiwum)


def cache_path() -> Path:
    return app_base_dir() / CACHE_FILENAME


def report_cache_path() -> Path:
    return app_base_dir() / REPORT_CACHE_FILENAME


def save_report_states(reports, path: Optional[Path] = None) -> None:
    """Zapisuje ZWARTY stan raportu: per DAT (po ścieżce) → {gra: {rom_lower:
    (kod_stanu, source_path, member, via_chd)}}. Pozwala po ponownym otwarciu
    programu pokazać ostatni wynik skanu WRAZ z planem naprawy (skąd plik)."""
    p = Path(path) if path else report_cache_path()
    from datetime import datetime
    data: dict[str, dict] = {}
    for rep in reports:
        key = str(Path(os.path.abspath(rep.entry.dat_path)))
        games: dict[str, dict] = {}
        for s in rep.statuses:
            games.setdefault(s.game, {})[s.rom.name.lower()] = (
                s.state.value, s.source_path, s.member, int(s.via_chd),
                int(getattr(s, "via_archive", False)),
                int(getattr(s, "archive_names_ok", True)))
        data[key] = games
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".pkl.tmp")
    try:
        with open(tmp, "wb") as f:
            pickle.dump({"version": REPORT_CACHE_VERSION,
                         "saved_at": datetime.now().isoformat(timespec="seconds"),
                         "reports": data}, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
    except (OSError, pickle.PickleError):
        tmp.unlink(missing_ok=True)


def load_report_states(path: Optional[Path] = None):
    """Zwraca (saved_at, {dat_abspath: {gra: {rom_lower: status}}}) albo
    (None, {}). status = lekki obiekt z polami state/source_path/member/
    via_chd (do kolorów i planu naprawy)."""
    p = Path(path) if path else report_cache_path()
    if not p.is_file():
        return None, {}
    try:
        with open(p, "rb") as f:
            blob = pickle.load(f)
        if blob.get("version") != REPORT_CACHE_VERSION:
            return None, {}
    except (OSError, pickle.PickleError, EOFError, AttributeError):
        return None, {}
    from types import SimpleNamespace

    from .matcher import RomState
    out: dict[str, dict] = {}
    for key, games in blob.get("reports", {}).items():
        out[key] = {
            g: {rn: SimpleNamespace(
                    state=RomState(v[0]), source_path=v[1], member=v[2],
                    via_chd=bool(v[3]),
                    via_archive=bool(v[4]) if len(v) > 4 else False,
                    archive_names_ok=bool(v[5]) if len(v) > 5 else True)
                for rn, v in roms.items()}
            for g, roms in games.items()}
    return blob.get("saved_at"), out


def _sig(path: Path) -> tuple[int, int]:
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


class DatParseCache:
    """Wczytuje/zapisuje sparsowane DAT-y; parsuje tylko zmienione."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else cache_path()
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with open(self.path, "rb") as f:
                blob = pickle.load(f)
            if isinstance(blob, dict) and blob.get("version") == CACHE_VERSION:
                self._data = blob.get("entries", {})
        except (OSError, pickle.PickleError, EOFError, AttributeError):
            self._data = {}       # uszkodzony/stary cache — zignoruj

    def get(self, dat: Path) -> Optional[tuple[str, list[DatGame]]]:
        """Zwraca (name, games) z cache, jeśli plik niezmieniony."""
        key = str(Path(os.path.abspath(dat)))
        rec = self._data.get(key)
        if rec is None:
            return None
        try:
            if tuple(rec["sig"]) != _sig(dat):
                return None
        except OSError:
            return None
        return rec["name"], rec["games"]

    def put(self, dat: Path, name: str, games: list[DatGame]) -> None:
        key = str(Path(os.path.abspath(dat)))
        try:
            sig = _sig(dat)
        except OSError:
            return
        self._data[key] = {"sig": sig, "name": name, "games": games}
        self._dirty = True

    def parse(self, dat: Path) -> tuple[str, list[DatGame]]:
        """Nazwa + gry DAT-a — z cache albo świeżo sparsowane (i dołożone)."""
        hit = self.get(dat)
        if hit is not None:
            return hit
        name = (parse_dat_header(dat).get("name") or dat.stem)
        games = list(parse_dat(dat))
        self.put(dat, name, games)
        return name, games

    def prune(self, present: set[str]) -> None:
        """Usuwa z cache wpisy DAT-ów, których już nie ma (present = zbiór
        aktualnych abspath)."""
        stale = [k for k in self._data if k not in present]
        for k in stale:
            del self._data[k]
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".pkl.tmp")
        try:
            with open(tmp, "wb") as f:
                pickle.dump({"version": CACHE_VERSION, "entries": self._data},
                            f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, self.path)
            self._dirty = False
        except (OSError, pickle.PickleError):
            tmp.unlink(missing_ok=True)

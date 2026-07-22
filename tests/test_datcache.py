"""Testy cache parsowania DAT-ów i cache stanu raportu."""
from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

from chd_buddy.core.datcache import (
    DatParseCache,
    load_report_states,
    save_report_states,
)
from chd_buddy.core.datstore import DatStore
from chd_buddy.core.fileindex import FileIndex
from chd_buddy.core.matcher import RomState, match_store


def _rom_attrs(content: bytes) -> str:
    return (f'size="{len(content)}" crc="{zlib.crc32(content) & 0xFFFFFFFF:08x}" '
            f'sha1="{hashlib.sha1(content).hexdigest()}"')


def _write_dat(path: Path, name: str, games: dict) -> None:
    parts = ['<?xml version="1.0"?><datafile>',
             f"<header><name>{name}</name></header>"]
    for game, roms in games.items():
        parts.append(f'<game name="{game}">')
        for rn, content in roms.items():
            parts.append(f'<rom name="{rn}" {_rom_attrs(content)}/>')
        parts.append("</game>")
    parts.append("</datafile>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")


def test_parse_cache_incremental(tmp_path: Path):
    """Cache parsowania: niezmieniony DAT z cache, zmieniony parsowany od nowa."""
    dat = tmp_path / "a.dat"
    _write_dat(dat, "System A", {"Gra": {"g.iso": b"x" * 40}})
    cache_file = tmp_path / "cache.pkl"

    c1 = DatParseCache(cache_file)
    assert c1.get(dat) is None            # pusty cache
    name, games = c1.parse(dat)           # parsuje + dokłada
    assert name == "System A" and len(games) == 1
    c1.save()

    c2 = DatParseCache(cache_file)
    assert c2.get(dat) is not None        # trafienie w cache (plik niezmieniony)

    # zmiana pliku => cache nieaktualny
    import time
    time.sleep(0.01)
    _write_dat(dat, "System A", {"Gra": {"g.iso": b"x" * 40},
                                 "Gra 2": {"g2.iso": b"y" * 40}})
    c3 = DatParseCache(cache_file)
    assert c3.get(dat) is None            # sig się nie zgadza
    _n, games2 = c3.parse(dat)
    assert len(games2) == 2


def test_datstore_uses_cache(tmp_path: Path, monkeypatch):
    """DatStore.discover z cache zwraca ten sam wynik i drugi raz nie parsuje."""
    import chd_buddy.core.datcache as dc
    monkeypatch.setattr(dc, "cache_path", lambda: tmp_path / "c.pkl")
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    _write_dat(dat_root / "a.dat", "System A", {"G": {"g.iso": b"x" * 40}})

    calls = {"n": 0}
    orig = dc.parse_dat                    # cache woła datcache.parse_dat

    def counting(path):
        calls["n"] += 1
        return orig(path)

    monkeypatch.setattr(dc, "parse_dat", counting)
    e1 = DatStore(dat_root, rom_root).discover()
    assert calls["n"] == 1                # sparsowano raz
    e2 = DatStore(dat_root, rom_root).discover()
    assert calls["n"] == 1                # drugi raz z cache (bez parsowania)
    assert e1[0].rom_count == e2[0].rom_count


def test_report_state_cache_roundtrip(tmp_path: Path):
    """Zapis/odczyt zwartego stanu raportu (per DAT → {gra: {rom: RomState}})."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data = b"MAM" * 40
    _write_dat(dat_root / "a.dat", "System A",
               {"Gra 1": {"Gra 1.iso": data},
                "Gra 2": {"Gra 2.iso": b"NIE-MAM" * 40}})
    (rom_root / "System A").mkdir(parents=True)
    (rom_root / "System A" / "Gra 1.iso").write_bytes(data)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)

    p = tmp_path / "rep.pkl"
    save_report_states(reports, p)
    saved_at, loaded = load_report_states(p)
    assert saved_at is not None
    dat_key = [k for k in loaded if k.endswith("a.dat")][0]
    states = loaded[dat_key]
    # v2: status z polami state/source_path/member/via_chd
    assert states["Gra 1"]["gra 1.iso"].state == RomState.HAVE
    assert states["Gra 2"]["gra 2.iso"].state == RomState.MISSING
    assert hasattr(states["Gra 1"]["gra 1.iso"], "source_path")

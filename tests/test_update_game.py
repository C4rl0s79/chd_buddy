"""Testy aktualizacji gry do nowszej wersji (edycja DAT + podmiana plików)."""
from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

from chd_buddy.core.datfile import DatRom, parse_dat
from chd_buddy.core.fileindex import FileIndex
from chd_buddy.core.update_game import (
    UpdatePlan,
    apply_update,
    replace_game_roms_text,
    roms_from_files,
    update_dat_file,
)


def _sums(data: bytes):
    return (f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
            hashlib.md5(data).hexdigest(), hashlib.sha1(data).hexdigest())


_DAT = (
    '<?xml version="1.0"?>\n<datafile>\n'
    '\t<machine name="Gra A">\n'
    '\t\t<description>Gra A</description>\n'
    '\t\t<rom name="Gra A.sfc" size="4" crc="aaaaaaaa" md5="a1" sha1="a2"/>\n'
    '\t</machine>\n'
    '\t<machine name="Gra B">\n'
    '\t\t<description>Gra B</description>\n'
    '\t\t<rom name="Gra B.sfc" size="5" crc="bbbbbbbb" md5="b1" sha1="b2"/>\n'
    '\t</machine>\n'
    '</datafile>\n'
)


def test_replace_game_roms_text_targeted():
    """Podmiana <rom> tylko wybranej gry; druga gra bajt-w-bajt bez zmian."""
    new = [DatRom(name="Gra A v2.sfc", size=100, crc="12345678",
                  md5="d"*32, sha1="e"*40)]
    out = replace_game_roms_text(_DAT, "Gra A", new)
    assert 'name="Gra A v2.sfc"' in out and 'crc="12345678"' in out
    assert 'crc="aaaaaaaa"' not in out            # stary ROM A zniknął
    # Gra B nietknięta
    assert '<rom name="Gra B.sfc" size="5" crc="bbbbbbbb" md5="b1" sha1="b2"/>' in out
    # nadal poprawny XML z 2 maszynami i po jednym ROM-ie
    games = list(parse_dat_from_str(out))
    ga = next(g for g in games if g.name == "Gra A")
    gb = next(g for g in games if g.name == "Gra B")
    assert [r.name for r in ga.roms] == ["Gra A v2.sfc"]
    assert ga.roms[0].crc == "12345678" and ga.roms[0].size == 100
    assert [r.name for r in gb.roms] == ["Gra B.sfc"]


def parse_dat_from_str(text: str):
    import tempfile
    import os as _os
    with tempfile.NamedTemporaryFile("w", suffix=".dat", delete=False,
                                     encoding="utf-8") as f:
        f.write(text); p = f.name
    try:
        return list(parse_dat(Path(p)))
    finally:
        _os.unlink(p)


def test_replace_missing_game_raises():
    import pytest
    with pytest.raises(KeyError):
        replace_game_roms_text(_DAT, "Nie ma", [])


def test_update_dat_file_writes_backup(tmp_path: Path):
    dat = tmp_path / "s.dat"; dat.write_text(_DAT, encoding="utf-8")
    new = [DatRom(name="Gra A v2.sfc", size=9, crc="0f0f0f0f", md5="d"*32,
                  sha1="e"*40)]
    assert update_dat_file(dat, "Gra A", new, log=lambda m: None)
    assert (tmp_path / "s.dat.bak").is_file()        # kopia zapasowa
    txt = dat.read_text(encoding="utf-8")
    assert 'name="Gra A v2.sfc"' in txt and 'crc="aaaaaaaa"' not in txt
    # dry_run nie pisze
    dat2 = tmp_path / "d2.dat"; dat2.write_text(_DAT, encoding="utf-8")
    assert update_dat_file(dat2, "Gra A", new, dry_run=True, log=lambda m: None)
    assert dat2.read_text(encoding="utf-8") == _DAT


def test_roms_from_files(tmp_path: Path):
    f = tmp_path / "x.sfc"; data = b"NEWVER" * 10; f.write_bytes(data)
    roms = roms_from_files([f])
    crc, md5, sha1 = _sums(data)
    assert roms[0].name == "x.sfc" and roms[0].size == len(data)
    assert roms[0].crc == crc and roms[0].sha1 == sha1


def test_apply_update_old_to_tosort_new_to_target(tmp_path: Path):
    """Stara wersja → ToSort; nowa (kopia) → docelowy; DAT zaktualizowany."""
    dat = tmp_path / "s.dat"; dat.write_text(_DAT, encoding="utf-8")
    target = tmp_path / "roms"; target.mkdir()
    tosort = tmp_path / "ts"
    newdir = tmp_path / "new"; newdir.mkdir()
    old = target / "Gra A.sfc"; old.write_bytes(b"OLD!")
    new = newdir / "Gra A v2.sfc"; newdata = b"NEW-VERSION" * 20
    new.write_bytes(newdata)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(target)

    plan = UpdatePlan(game_name="Gra A", dat_path=dat, target_dir=target,
                      store_format="keep", subdir=False, old_files=[old],
                      new_files=[new], new_roms=roms_from_files([new]),
                      same_format=True)
    assert apply_update(plan, index=idx, tosort=tosort, log=lambda m: None)
    # stara w ToSort, nie w docelowym
    assert not old.exists()
    assert (tosort / "roms" / "Gra A.sfc").is_file()
    # nowa skopiowana do docelowego, źródło zostało
    assert (target / "Gra A v2.sfc").read_bytes() == newdata
    assert new.is_file()                              # KOPIA — źródło zostaje
    # DAT zaktualizowany
    txt = dat.read_text(encoding="utf-8")
    assert 'name="Gra A v2.sfc"' in txt
    crc, _, sha1 = _sums(newdata)
    assert f'crc="{crc}"' in txt

"""Testy logiki rdzenia (bez potrzeby prawdziwego chdman ani PySide6)."""
from __future__ import annotations

import struct
import tempfile
from pathlib import Path

import pytest

from chd_buddy.core import diskbudget, imageops, presets
from chd_buddy.core.audit import classify_info
from chd_buddy.core.chdman import parse_info
from chd_buddy.core.detector import detect_iso_media
from chd_buddy.core.models import AuditVerdict, CHDInfo, MediaType


def test_parse_info_dvd_as_cd():
    text = (
        "File Version: 5\n"
        "Logical size: 4,700,372,992 bytes\n"
        "Unit Size:    2,448 bytes\n"
        "Compression:  cdlz (CD LZMA), cdzl (CD Deflate), cdfl (CD FLAC)\n"
        "CHD size:     3,200,000,000 bytes\n"
        "Metadata:     Tag='CHT2'  Index=0\n"
    )
    info = parse_info(text, Path("game.chd"))
    assert info.version == 5
    assert info.logical_bytes == 4_700_372_992
    assert info.unit_bytes == 2448
    assert info.is_cd_typed is True
    v, expected, _ = classify_info(info, 950 * 1024 * 1024)
    assert v == AuditVerdict.SUSPECT_WRONG_TYPE
    assert expected == MediaType.DVD


def test_parse_info_legit_dvd():
    text = (
        "File Version: 5\n"
        "Logical size: 4,700,372,992 bytes\n"
        "Unit Size:    2,048 bytes\n"
        "Compression:  lzma (LZMA), zlib (Deflate)\n"
        "CHD size:     3,100,000,000 bytes\n"
    )
    info = parse_info(text, Path("ok.chd"))
    assert info.is_cd_typed is False
    assert info.detected_media == MediaType.DVD
    v, _, _ = classify_info(info, 950 * 1024 * 1024)
    assert v == AuditVerdict.OK


def test_parse_info_small_cd_ok():
    text = (
        "File Version: 5\n"
        "Logical size: 681,984,000 bytes\n"
        "Unit Size:    2,448 bytes\n"
        "Compression:  cdlz (CD LZMA)\n"
    )
    info = parse_info(text, Path("cd.chd"))
    v, _, _ = classify_info(info, 950 * 1024 * 1024)
    assert v == AuditVerdict.OK
    assert info.detected_media == MediaType.CD


def test_presets_are_media_aware():
    assert presets.compression_for("max", MediaType.CD) == "cdlz,cdzl,cdfl"
    assert presets.compression_for("max", MediaType.DVD) == "lzma,zlib,huff,flac"
    assert presets.compression_for("default", MediaType.CD) is None


def test_iso_detection_small_is_cd(tmp_path):
    iso = tmp_path / "s.iso"
    with iso.open("wb") as f:
        f.seek(16 * 2048)
        pvd = bytearray(2048)
        pvd[0] = 1
        pvd[1:6] = b"CD001"
        struct.pack_into("<I", pvd, 80, 200000)
        struct.pack_into("<H", pvd, 128, 2048)
        f.write(pvd)
        f.seek(400 * 1024 * 1024 - 1)
        f.write(b"\x00")
    det = detect_iso_media(iso)
    assert det.media == MediaType.CD


def test_iso_detection_large_is_dvd(tmp_path):
    biso = tmp_path / "b.iso"
    with biso.open("wb") as f:
        f.truncate(1200 * 1024 * 1024)
    det = detect_iso_media(biso)
    assert det.media == MediaType.DVD


def test_disk_budget_aggressive_saves_space(tmp_path):
    info = CHDInfo(path=Path("x.chd"), logical_bytes=4_700_000_000,
                   chd_bytes=3_200_000_000, file_bytes=3_200_000_000)
    safe = diskbudget.budget_retype(info, tmp_path, aggressive=False)
    aggr = diskbudget.budget_retype(info, tmp_path, aggressive=True)
    assert aggr.required_peak_bytes < safe.required_peak_bytes


def test_bin_to_iso_deframe_2352(tmp_path):
    b = tmp_path / "t.bin"
    with b.open("wb") as f:
        for i in range(8):
            frame = bytearray(2352)
            frame[16:16 + 2048] = bytes([i % 256]) * 2048
            f.write(frame)
    out = tmp_path / "t.iso"
    imageops.bin_to_iso(b, 2352, out)
    data = out.read_bytes()
    assert len(data) == 8 * 2048
    assert data[0:2048] == bytes([0]) * 2048


def test_bin_to_iso_2048_is_copy(tmp_path):
    b = tmp_path / "u.bin"
    b.write_bytes(bytes([7]) * (2048 * 5))
    out = tmp_path / "u.iso"
    imageops.bin_to_iso(b, 2048, out)
    assert out.read_bytes() == b.read_bytes()


def test_dat_media_classification_and_lookup(tmp_path):
    from chd_buddy.core.datfile import DatIndex, parse_dat
    from chd_buddy.core.models import MediaType

    dat = tmp_path / "ps2.dat"
    dat.write_text(
        '<datafile>'
        '<game name="DVD Game">'
        '<rom name="DVD Game.iso" size="16" sha1="'
        + "a" * 40 + '"/></game>'
        '<game name="CD Game">'
        '<rom name="CD Game (Track 1).bin" size="8" sha1="'
        + "b" * 40 + '"/>'
        '<rom name="CD Game.cue" size="1" sha1="' + "c" * 40 + '"/>'
        '</game></datafile>',
        encoding="utf-8",
    )
    games = list(parse_dat(dat))
    assert games[0].media == MediaType.DVD
    assert games[1].media == MediaType.CD

    idx = DatIndex.from_paths([dat])
    assert idx.games == 2
    m = idx.match_sha1("A" * 40)  # case-insensitive
    assert m is not None and m.media == MediaType.DVD and m.game == "DVD Game"
    assert idx.match_sha1("f" * 40) is None


def test_roundtrip_sha1_streaming(tmp_path):
    from chd_buddy.core import roundtrip

    p = tmp_path / "blob.bin"
    p.write_bytes(bytes(range(256)) * 4096)  # 1 MB
    import hashlib
    assert roundtrip.sha1_file(p) == hashlib.sha1(p.read_bytes()).hexdigest()


def test_source_companions_multitrack_cue(tmp_path):
    from chd_buddy.core import fixer

    (tmp_path / "g (Track 1).bin").write_bytes(b"\x01")
    (tmp_path / "g (Track 2).bin").write_bytes(b"\x02")
    cue = tmp_path / "g.cue"
    cue.write_text(
        'FILE "g (Track 1).bin" BINARY\n  TRACK 01 MODE1/2352\n'
        'FILE "g (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n')
    names = sorted(p.name for p in fixer._source_companions(cue))
    assert names == ["g (Track 1).bin", "g (Track 2).bin", "g.cue"]


def test_source_companions_single_iso(tmp_path):
    from chd_buddy.core import fixer

    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"\x00")
    assert [p.name for p in fixer._source_companions(iso)] == ["movie.iso"]


def test_deepcheck_tries_until_dat_match(tmp_path, monkeypatch):
    import hashlib

    from chd_buddy.core import deepcheck
    from chd_buddy.core.datfile import DatIndex
    from chd_buddy.core.models import MediaType

    # Zbuduj DAT z hashem deframowanego ISO (8×2048 bloków).
    h = hashlib.sha1()
    for i in range(8):
        h.update(bytes([(i * 7 + 3) % 256]) * 2048)
    deframed = h.hexdigest()
    dat = tmp_path / "d.dat"
    dat.write_text(
        f'<datafile><game name="G"><rom name="G.iso" sha1="{deframed}"/>'
        f"</game></datafile>", encoding="utf-8")
    idx = DatIndex.from_paths([dat])

    # Atrapa CHDMan: extractdvd pada, extractcd produkuje bin 2352.
    class FakeRes:
        def __init__(self, ok, rc=0):
            self.ok = ok
            self.returncode = rc

    class FakeCHD:
        def extract(self, cmd, src, dst, on_progress=None, cancel_event=None):
            from pathlib import Path
            dst = Path(dst)
            if cmd == "extractdvd":
                return FakeRes(False, 1)
            if cmd == "extractcd":
                binp = dst.with_suffix(".bin")
                with binp.open("wb") as f:
                    for i in range(8):
                        fr = bytearray(2352)
                        fr[16:16 + 2048] = bytes([(i * 7 + 3) % 256]) * 2048
                        f.write(fr)
                dst.write_text(f'FILE "{binp.name}" BINARY\n  TRACK 01 MODE1/2352\n')
                return FakeRes(True)
            return FakeRes(False, 1)

    chd = tmp_path / "m.chd"
    chd.write_bytes(b"x")
    r = deepcheck.deep_identify(FakeCHD(), chd, idx, tmp_path)
    assert r.ok and r.media == MediaType.DVD and r.game == "G"
    assert r.method == "extractcd + deframe 2048"
    assert r.tried[0] == "extractdvd"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

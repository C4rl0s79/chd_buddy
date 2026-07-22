"""Testy indeksu plików (fileindex) i linkera (mirror + dedup)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from chd_buddy.core.fileindex import FileIndex, hash_file
from chd_buddy.core.linker import (
    LinkPrivilegeError,
    apply_dedup,
    is_link,
    mirror_tree,
    plan_dedup,
)


def _can_symlink(tmp_path: Path) -> bool:
    probe_target = tmp_path / "_probe_target.txt"
    probe_target.write_text("x", encoding="utf-8")
    probe_link = tmp_path / "_probe_link.txt"
    try:
        os.symlink(probe_target, probe_link)
    except OSError:
        return False
    finally:
        if probe_link.exists() or probe_link.is_symlink():
            probe_link.unlink(missing_ok=True)
        probe_target.unlink(missing_ok=True)
    return True


def _write(p: Path, content: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


@pytest.fixture()
def idx(tmp_path: Path) -> FileIndex:
    with FileIndex(tmp_path / "index.sqlite3") as fi:
        yield fi


# --- hash_file ----------------------------------------------------------------

def test_hash_file_known_values(tmp_path: Path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    crc, md5, sha1 = hash_file(f)
    assert crc == "352441c2"
    assert md5 == "900150983cd24fb0d6963f7d28e17f72"
    assert sha1 == "a9993e364706816aba3e25717850c26c9cd0d89d"


# --- skan przyrostowy -----------------------------------------------------------

def test_scan_incremental(idx: FileIndex, tmp_path: Path):
    root = tmp_path / "roms"
    _write(root / "ps2" / "game1.iso", b"AAAA" * 100)
    _write(root / "ps2" / "game2.iso", b"BBBB" * 100)
    _write(root / "ps1" / "game3.bin", b"CCCC" * 100)

    st1 = idx.scan(root)
    assert st1.hashed == 3
    assert st1.unchanged == 0

    # drugi skan: nic się nie zmieniło => zero haszowania
    st2 = idx.scan(root)
    assert st2.hashed == 0
    assert st2.unchanged == 3

    # modyfikacja jednego pliku => tylko on jest przeliczany
    target = root / "ps2" / "game1.iso"
    target.write_bytes(b"ZZZZ" * 200)
    os.utime(target, ns=(time.time_ns(), time.time_ns() + 10**9))
    st3 = idx.scan(root)
    assert st3.hashed == 1
    assert st3.unchanged == 2

    # --full wymusza przeliczenie wszystkiego
    st4 = idx.scan(root, full=True)
    assert st4.hashed == 3


def test_scan_marks_missing_and_restores(idx: FileIndex, tmp_path: Path):
    root = tmp_path / "roms"
    keep = root / "keep.iso"
    gone = root / "gone.iso"
    _write(keep, b"K" * 64)
    _write(gone, b"G" * 64)
    idx.scan(root)

    payload = gone.read_bytes()
    gone.unlink()
    st = idx.scan(root)
    assert st.missing == 1
    row = idx.lookup(gone)
    assert row is not None and row["missing"] == 1

    # plik wraca (ten sam rozmiar+mtime nie jest gwarantowany => może być hash)
    _write(gone, payload)
    idx.scan(root)
    row = idx.lookup(gone)
    assert row is not None and row["missing"] == 0


def test_scan_ext_filter(idx: FileIndex, tmp_path: Path):
    root = tmp_path / "roms"
    _write(root / "a.iso", b"1" * 10)
    _write(root / "b.txt", b"2" * 10)
    st = idx.scan(root, exts={"iso"})
    assert st.hashed == 1
    assert st.filtered == 1
    assert idx.lookup(root / "b.txt") is None


def test_scan_skips_temp_artifacts(idx: FileIndex, tmp_path: Path):
    """Żywe pliki tymczasowe naprawy/ekstrakcji nie trafiają do indeksu
    (regresja: dry-run planował rename .rtcheck.img trwającego retype'u)."""
    root = tmp_path / "roms"
    _write(root / "gra.iso", b"OK" * 50)
    _write(root / "chdbuddy_ab12" / "gra.chd.rtcheck.img", b"TMP" * 50)
    _write(root / "inne.rtcheck.img", b"TMP2" * 50)
    _write(root / "chdbuddy_tmp_3.lnk", b"L")
    st = idx.scan(root)
    assert st.seen == 1 and st.hashed == 1
    assert idx.lookup(root / "gra.iso") is not None
    assert idx.lookup(root / "inne.rtcheck.img") is None
    assert idx.lookup(root / "chdbuddy_ab12" / "gra.chd.rtcheck.img") is None


def test_find_sha1_and_duplicates(idx: FileIndex, tmp_path: Path):
    root = tmp_path / "roms"
    _write(root / "dat_a" / "same.iso", b"XYZ" * 1000)
    _write(root / "dat_b" / "kopia.iso", b"XYZ" * 1000)
    _write(root / "dat_a" / "inny.iso", b"QQQ" * 1000)
    idx.scan(root)

    _, _, sha1 = hash_file(root / "dat_a" / "same.iso")
    hits = idx.find_sha1(sha1)
    assert len(hits) == 2

    groups = idx.duplicate_groups()
    assert len(groups) == 1
    assert len(groups[0].paths) == 2


# --- linker: mirror -------------------------------------------------------------

def test_mirror_tree_sync_and_stale(tmp_path: Path):
    if not _can_symlink(tmp_path):
        pytest.skip("brak uprawnień do symlinków (tryb dewelopera/admin)")
    src = tmp_path / "server"
    dst = tmp_path / "retrobat"
    _write(src / "PS2" / "Gra (Disc 1).chd", b"a")
    _write(src / "PS2" / "Gra (Disc 2).chd", b"b")
    _write(src / "PS1" / "Inna.chd", b"c")
    (src / "PS2" / "images").mkdir()

    st = mirror_tree(src, dst)
    assert st.created == 3
    assert is_link(dst / "PS2" / "Gra (Disc 1).chd")
    assert (dst / "PS2" / "Gra (Disc 1).chd").read_bytes() == b"a"
    # wykluczone katalogi nie są linkowane
    assert not (dst / "PS2" / "images").exists()

    # zwykły plik użytkownika w celu jest nietykalny
    normal = dst / "PS2" / "notatki.txt"
    normal.write_text("moje", encoding="utf-8")
    # plik znika ze źródła => jego link znika przy sync
    (src / "PS2" / "Gra (Disc 2).chd").unlink()
    st2 = mirror_tree(src, dst)
    assert st2.removed_stale == 1
    assert not (dst / "PS2" / "Gra (Disc 2).chd").exists()
    assert normal.read_text(encoding="utf-8") == "moje"

    # sync jest idempotentny
    st3 = mirror_tree(src, dst)
    assert st3.created == 0
    assert st3.skipped_existing == 2


def test_mirror_dry_run_changes_nothing(tmp_path: Path):
    src = tmp_path / "server"
    dst = tmp_path / "retrobat"
    _write(src / "PS2" / "Gra.chd", b"a")
    st = mirror_tree(src, dst, dry_run=True)
    assert st.created == 1
    assert not dst.exists()


# --- linker: dedup ---------------------------------------------------------------

def test_dedup_replaces_with_symlink(idx: FileIndex, tmp_path: Path):
    if not _can_symlink(tmp_path):
        pytest.skip("brak uprawnień do symlinków (tryb dewelopera/admin)")
    root = tmp_path / "roms"
    main = root / "redump_ps2" / "gra.iso"
    dup = root / "inny_dat" / "gra.iso"
    _write(main, b"DATA" * 500)
    _write(dup, b"DATA" * 500)
    idx.scan(root)

    actions = plan_dedup(idx, prefer_roots=[str(root / "redump_ps2")])
    assert len(actions) == 1
    assert actions[0].keep == str(main)

    st = apply_dedup(actions, index=idx, dry_run=False)
    assert st.replaced == 1
    assert st.errors == 0
    assert is_link(dup)
    assert dup.read_bytes() == b"DATA" * 500      # link prowadzi do kopii
    assert not is_link(main)
    row = idx.lookup(dup)
    assert row is not None and row["is_link"] == 1
    # plik tymczasowy nie może zostać
    assert not dup.with_name(dup.name + ".chdbuddy_dedup_tmp").exists()


def test_dedup_dry_run_changes_nothing(idx: FileIndex, tmp_path: Path):
    root = tmp_path / "roms"
    _write(root / "a" / "x.iso", b"P" * 100)
    _write(root / "b" / "x.iso", b"P" * 100)
    idx.scan(root)
    actions = plan_dedup(idx)
    st = apply_dedup(actions, index=idx, dry_run=True)
    assert st.replaced == 1
    assert not is_link(root / "a" / "x.iso")
    assert not is_link(root / "b" / "x.iso")


def test_dedup_skips_changed_file(idx: FileIndex, tmp_path: Path):
    root = tmp_path / "roms"
    a = root / "a" / "x.iso"
    b = root / "b" / "x.iso"
    _write(a, b"P" * 100)
    _write(b, b"P" * 100)
    idx.scan(root)
    # plik b zmienia się PO skanie => nie wolno go zastąpić linkiem
    b.write_bytes(b"NOWE DANE")
    actions = plan_dedup(idx)
    st = apply_dedup(actions, index=idx, dry_run=False)
    assert st.replaced == 0
    assert st.skipped == 1
    assert b.read_bytes() == b"NOWE DANE"

"""Operacje plikowe z postępem: copy/move raportują bajty; move cross-drive
kopiuje blokami i kasuje źródło dopiero po sukcesie."""
from __future__ import annotations

import os
from pathlib import Path

from chd_buddy.core.fileops import copy_with_progress, move_with_progress


def test_copy_reports_byte_progress(tmp_path: Path):
    src = tmp_path / "a.bin"
    data = b"X" * (20 * 1024 * 1024 + 123)      # >kilka bloków
    src.write_bytes(data)
    dst = tmp_path / "b.bin"
    calls: list[tuple[int, int, str]] = []
    n = copy_with_progress(src, dst, on_progress=lambda d, t, s: calls.append((d, t, s)),
                           label="kopiuję a", chunk=8 * 1024 * 1024)
    assert n == len(data)
    assert dst.read_bytes() == data
    # pierwszy sygnał 0/total, ostatni total/total
    assert calls[0][:2] == (0, len(data))
    assert calls[-1][:2] == (len(data), len(data))
    assert all(s == "kopiuję a" for _d, _t, s in calls)
    # postęp rósł monotonicznie
    dones = [d for d, _t, _s in calls]
    assert dones == sorted(dones)


def test_move_same_drive_is_atomic(tmp_path: Path):
    src = tmp_path / "s.bin"
    src.write_bytes(b"DATA" * 1000)
    dst = tmp_path / "sub" / "d.bin"
    calls: list = []
    move_with_progress(src, dst, on_progress=lambda *a: calls.append(a))
    assert dst.read_bytes() == b"DATA" * 1000
    assert not src.exists()
    # ten sam dysk => os.replace => brak kopiowania blokami
    assert calls == []


def test_move_cross_drive_copies_with_progress(tmp_path: Path, monkeypatch):
    """Symulujemy inny wolumin: os.replace pada raz (cross-device) →
    kopia blokami z postępem, źródło skasowane po sukcesie."""
    src = tmp_path / "big.bin"
    data = b"Y" * (12 * 1024 * 1024)
    src.write_bytes(data)
    dst = tmp_path / "out" / "big.bin"

    real_replace = os.replace
    state = {"failed": False}

    def fake_replace(a, b):
        # pierwszy replace (src->dst) udaje cross-device; kolejne (tmp->dst) OK
        if not state["failed"] and str(a) == str(src):
            state["failed"] = True
            raise OSError(18, "Invalid cross-device link")
        return real_replace(a, b)

    monkeypatch.setattr(os, "replace", fake_replace)
    calls: list = []
    move_with_progress(src, dst, on_progress=lambda d, t, s: calls.append((d, t, s)),
                       label="przenoszę big")
    assert dst.read_bytes() == data
    assert not src.exists()                       # źródło skasowane
    assert calls and calls[-1][:2] == (len(data), len(data))
    # nie został plik tymczasowy
    assert not (dst.parent / (dst.name + ".chdbuddy_move_tmp")).exists()


def test_move_cross_drive_cleans_tmp_on_failure(tmp_path: Path, monkeypatch):
    """Błąd w połowie kopii cross-drive → nic niekompletnego nie zostaje,
    źródło NIE jest kasowane."""
    src = tmp_path / "big.bin"
    src.write_bytes(b"Z" * (4 * 1024 * 1024))
    dst = tmp_path / "out" / "big.bin"

    def boom_replace(a, b):
        raise OSError(18, "cross-device")
    monkeypatch.setattr(os, "replace", boom_replace)

    import chd_buddy.core.fileops as fo

    def boom_copy(*a, **k):
        raise OSError("dysk pełny w połowie")
    monkeypatch.setattr(fo, "copy_with_progress", boom_copy)

    try:
        move_with_progress(src, dst)
    except OSError:
        pass
    assert src.exists()                           # źródło nietknięte
    assert not (dst.parent / (dst.name + ".chdbuddy_move_tmp")).exists()

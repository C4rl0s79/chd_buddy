"""RAM dysk: retry z odczekaniem; trwałość opcji naprawy w ustawieniach."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import chd_buddy.core.ramdisk as rd
from chd_buddy.core.settings import Settings


def _fake_run(returncodes):
    """Kolejne wywołania subprocess.run zwracają kolejne returncode z listy."""
    seq = iter(returncodes)

    def run(*a, **k):
        rc = next(seq)
        return SimpleNamespace(returncode=rc, stdout="", stderr="err")
    return run


def test_ramdisk_create_retries_then_succeeds(monkeypatch):
    """Pierwsze próby padają (za mało pamięci), po odczekaniu udaje się —
    dysk jest utworzony, a nie porzucony po pierwszym błędzie."""
    monkeypatch.setattr(rd, "_imdisk_exe", lambda: "imdisk.exe")
    monkeypatch.setattr(rd.subprocess, "run", _fake_run([3, 3, 0]))
    monkeypatch.setattr(rd.time, "sleep", lambda *_: None)   # bez realnych czekań
    # wolumin „gotowy" dopiero po udanym poleceniu (returncode 0)
    calls = {"n": 0}

    def ready(root):
        calls["n"] += 1
        # first two attempts: run==3 → _ready sprawdzany na starcie pętli = False;
        # po sukcesie pierwsze sprawdzenie w deadline-loop = True
        return calls["n"] > 3
    monkeypatch.setattr(rd, "_ready", ready)
    rd._ACTIVE = None

    root = rd.create(size_gb=1, letter="R", log=None, retry_wait=0)
    assert root == Path("R:\\")
    assert rd._ACTIVE == Path("R:\\")     # zarejestrowany jako aktywny
    rd._ACTIVE = None


def test_ramdisk_create_gives_up_after_attempts(monkeypatch):
    """Gdy wszystkie próby padają — None (fallback na dysk fizyczny), nie wyjątek."""
    monkeypatch.setattr(rd, "_imdisk_exe", lambda: "imdisk.exe")
    monkeypatch.setattr(rd.subprocess, "run", _fake_run([3, 3, 3, 3]))
    monkeypatch.setattr(rd.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rd, "_ready", lambda root: False)
    rd._ACTIVE = None
    logs: list[str] = []

    root = rd.create(size_gb=1, attempts=4, retry_wait=0, log=logs.append)
    assert root is None
    assert any("po 4 próbach" in m for m in logs)


def test_ramdisk_reuses_existing(monkeypatch):
    """Gdy R: już istnieje i pisze — użyj ponownie, bez odpalania imdisk."""
    monkeypatch.setattr(rd, "_imdisk_exe", lambda: "imdisk.exe")
    def _boom(*a, **k):
        raise AssertionError("nie powinno wołać imdisk gdy dysk już jest")
    monkeypatch.setattr(rd.subprocess, "run", _boom)
    monkeypatch.setattr(rd, "_ready", lambda root: True)
    rd._ACTIVE = None
    assert rd.create(size_gb=1) == Path("R:\\")
    rd._ACTIVE = None


def test_maybe_elevate_guards(monkeypatch):
    """Auto-admin: NIE podnosi gdy wyłączone / już admin / --no-elevate;
    podnosi (woła relaunch) gdy włączone i nie-admin. Bez realnego UAC."""
    import os
    import sys
    from chd_buddy.main import _maybe_elevate
    import chd_buddy.core.elevate as el
    from chd_buddy.core.settings import Settings

    monkeypatch.setattr(os, "name", "nt")           # udawaj Windows
    called = {"n": 0}
    monkeypatch.setattr(el, "is_admin", lambda: False)
    monkeypatch.setattr(el, "relaunch_as_admin",
                        lambda: (called.__setitem__("n", called["n"] + 1), True)[1])
    s = Settings()

    # wyłączone ustawieniem
    s.auto_elevate = False
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert _maybe_elevate(s) is False and called["n"] == 0

    # --no-elevate w argv
    s.auto_elevate = True
    monkeypatch.setattr(sys, "argv", ["prog", "--no-elevate"])
    assert _maybe_elevate(s) is False and called["n"] == 0

    # już administrator
    monkeypatch.setattr(sys, "argv", ["prog"])
    monkeypatch.setattr(el, "is_admin", lambda: True)
    assert _maybe_elevate(s) is False and called["n"] == 0

    # włączone + nie-admin + brak flagi => podnosi (woła relaunch)
    monkeypatch.setattr(el, "is_admin", lambda: False)
    assert _maybe_elevate(s) is True and called["n"] == 1


def test_scratch_fallback_used_before_prefer(tmp_path, monkeypatch):
    """Kolejność scratcha: RAM (nieaktywny) → fallback (z ustawień) → prefer.
    Gdy fallback ma miejsce, jest wybrany PRZED katalogiem obok pliku."""
    import chd_buddy.core.scratch as sc
    import chd_buddy.core.ramdisk as rd
    monkeypatch.setattr(rd, "active_root", lambda: None)   # brak RAM
    fallback = tmp_path / "fallback"; fallback.mkdir()
    prefer = tmp_path / "obok"; prefer.mkdir()
    got = sc.pick_scratch_root(1024, prefer=str(prefer), fallback=str(fallback))
    assert got is not None
    assert str(fallback) in str(got)          # wybrano fallback, nie prefer
    assert got.name == "chdbuddy_scratch"


def test_scratch_dir_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "path", classmethod(
        lambda cls: tmp_path / "settings.json"))
    s = Settings()
    assert s.scratch_dir == ""
    s.scratch_dir = str(tmp_path / "temp")
    s.save()
    assert Settings.load().scratch_dir == str(tmp_path / "temp")


def test_fix_options_persist(tmp_path, monkeypatch):
    """Opcje naprawy (nieznane→ToSort, konwertuj, ...) przetrwają zapis/odczyt."""
    monkeypatch.setattr(Settings, "path", classmethod(
        lambda cls: tmp_path / "settings.json"))
    s = Settings()
    # domyślne
    assert s.fix_clean is False and s.fix_convert is False
    assert s.fix_del_tosort is True and s.fix_dedup is True
    # zmień i zapisz
    s.fix_clean = True
    s.fix_convert = True
    s.fix_del_tosort = False
    s.save()
    # wczytaj na nowo
    s2 = Settings.load()
    assert s2.fix_clean is True
    assert s2.fix_convert is True
    assert s2.fix_del_tosort is False
    assert s2.fix_dedup is True

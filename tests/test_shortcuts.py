"""Testy modułu skrótów .lnk (wykrywanie emulatorów, plan, składnia)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chd_buddy.core.shortcuts import (
    build_plan,
    create_shortcuts,
    detect_system,
    emulator_for_system,
    find_emulators,
)


def _fake_emus(root: Path, layout: dict[str, list[str]]) -> Path:
    for folder, exes in layout.items():
        d = root / folder
        d.mkdir(parents=True, exist_ok=True)
        for e in exes:
            p = d / e
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"MZ")
    return root


@pytest.fixture()
def emu_root(tmp_path: Path) -> Path:
    return _fake_emus(tmp_path / "emu", {
        "PCSX2": ["pcsx2-qt.exe", "updater.exe"],
        "DuckStation": ["duckstation-qt-x64-ReleaseLTCG.exe"],
        "Dolphin": ["Dolphin.exe", "DolphinTool.exe"],
        "xemu": ["xemu.exe"],
        "MAME": ["mame.exe", "chdman.exe"],
        "RetroArch": ["retroarch.exe", "cores/mednafen_saturn_libretro.dll",
                      "cores/mednafen_psx_hw_libretro.dll"],
        "Cemu": ["Cemu.exe"],
        "Flycast": ["flycast.exe"],
    })


def test_detect_system_aliases():
    assert detect_system("Sony - PlayStation 2") == "PS2"
    assert detect_system("ps2") == "PS2"
    assert detect_system("Sega - Dreamcast") == "DC"
    assert detect_system("Nintendo - GameCube") == "GCN"
    assert detect_system("nieznany katalog") == ""


def test_find_emulators_and_selection(emu_root: Path):
    found = find_emulators(emu_root)
    assert found["PCSX2"].name == "pcsx2-qt.exe"
    assert found["DuckStation"].name.startswith("duckstation")
    assert "Xenia" not in found
    spec, exe = emulator_for_system("PS2", found)
    assert spec.name == "PCSX2" and exe == found["PCSX2"]
    assert emulator_for_system("X360", found) is None


def test_build_plan_syntax_per_emulator(emu_root: Path, tmp_path: Path):
    found = find_emulators(emu_root)

    ps2 = tmp_path / "roms" / "Sony - PlayStation 2"
    ps2.mkdir(parents=True)
    (ps2 / "God of War (USA).chd").write_bytes(b"x")
    plan, why = build_plan(ps2, "PS2", found)
    assert why is None and len(plan) == 1
    assert plan[0].arguments == f'-batch -fullscreen -- "{ps2 / "God of War (USA).chd"}"'
    assert plan[0].target.name == "pcsx2-qt.exe"
    assert plan[0].workdir == found["PCSX2"].parent
    assert plan[0].lnk_path == ps2 / "shortcuts" / "God of War (USA).lnk"

    gcn = tmp_path / "roms" / "Nintendo - GameCube"
    gcn.mkdir(parents=True)
    (gcn / "Metroid Prime (USA).iso").write_bytes(b"x")
    plan, _ = build_plan(gcn, "GCN", found)
    assert plan[0].arguments.startswith('-e "')      # Dolphin wymaga -e

    xbx = tmp_path / "roms" / "xbox"
    xbx.mkdir(parents=True)
    (xbx / "Halo (USA).iso").write_bytes(b"x")
    plan, _ = build_plan(xbx, "XBOX", found)
    assert plan[0].arguments.startswith('-dvd_path "')

    arc = tmp_path / "roms" / "arcade"
    arc.mkdir(parents=True)
    (arc / "sf2.zip").write_bytes(b"x")
    plan, _ = build_plan(arc, "ARCADE", found)
    assert plan[0].arguments == f'sf2 -rompath "{arc}"'   # MAME: set, nie ścieżka

    sat = tmp_path / "roms" / "Sega - Saturn"
    sat.mkdir(parents=True)
    (sat / "Panzer Dragoon (USA).chd").write_bytes(b"x")
    plan, _ = build_plan(sat, "SATURN", found)
    assert "-L" in plan[0].arguments and "mednafen_saturn_libretro.dll" in plan[0].arguments


def test_build_plan_multidisc_m3u(emu_root: Path, tmp_path: Path):
    found = find_emulators(emu_root)
    ps1 = tmp_path / "Sony - PlayStation"
    ps1.mkdir()
    (ps1 / "FF VII (USA) (Disc 1).chd").write_bytes(b"x")
    (ps1 / "FF VII (USA) (Disc 2).chd").write_bytes(b"x")
    (ps1 / "FF VII (USA).m3u").write_text("d", encoding="utf-8")
    # DuckStation umie m3u => jeden skrót wskazuje playlistę
    plan, _ = build_plan(ps1, "PS1", found)
    assert len(plan) == 1
    assert plan[0].rom.suffix == ".m3u"
    # PCSX2 (bez m3u) dostałby Disc 1
    plan2, _ = build_plan(ps1, "PS2", found)
    assert plan2[0].rom.name == "FF VII (USA) (Disc 1).chd"


def test_build_plan_subdir_per_game(emu_root: Path, tmp_path: Path):
    """Gra w PODKATALOGU (bin/cue jak Dreamcast po naprawie): tytuł z nazwy
    katalogu, cel = plik główny (cue przed binami)."""
    found = find_emulators(emu_root)
    dc = tmp_path / "Dreamcast"
    game = dc / "Mortal Kombat Gold (USA)"
    game.mkdir(parents=True)
    (game / "Mortal Kombat Gold (USA) (Track 01).bin").write_bytes(b"x")
    (game / "Mortal Kombat Gold (USA).cue").write_text("FILE", encoding="utf-8")
    (dc / "icons").mkdir()
    (dc / "Solo (USA).chd").write_bytes(b"x")     # płaski CHD obok

    plan, why = build_plan(dc, "DC", found)
    assert why is None
    titles = {p.title: p for p in plan}
    assert set(titles) == {"Mortal Kombat Gold (USA)", "Solo (USA)"}
    mk = titles["Mortal Kombat Gold (USA)"]
    assert mk.rom.name == "Mortal Kombat Gold (USA).cue"
    assert mk.lnk_path.name == "Mortal Kombat Gold (USA).lnk"


def test_build_plan_attaches_icons(emu_root: Path, tmp_path: Path):
    found = find_emulators(emu_root)
    d = tmp_path / "ps2"
    (d / "icons").mkdir(parents=True)
    (d / "Gra (USA).chd").write_bytes(b"x")
    (d / "icons" / "Gra (USA).ico").write_bytes(b"\x00\x00\x01\x00")
    plan, _ = build_plan(d, "PS2", found)
    assert plan[0].icon == d / "icons" / "Gra (USA).ico"


def test_emulator_options_and_override(emu_root: Path, tmp_path: Path):
    """Wybór per system: standalone kontra rdzeń RetroArch."""
    from chd_buddy.core.shortcuts import emulator_options, resolve_choice
    found = find_emulators(emu_root)

    # PS1: standalone DuckStation + rdzeń Beetle PSX HW
    opts = emulator_options("PS1", found)
    ids = [o.id for o in opts]
    assert "DuckStation" in ids
    assert "RetroArch:mednafen_psx_hw" in ids
    # domyślnie standalone (kolejność rejestru)
    assert resolve_choice("PS1", found).id == "DuckStation"
    # override na rdzeń RetroArch
    ra = resolve_choice("PS1", found, "RetroArch:mednafen_psx_hw")
    assert ra.exe.name == "retroarch.exe" and ra.m3u_ok

    # nieaktualny override => powrót do domyślnego
    assert resolve_choice("PS1", found, "RetroArch:nie_ma").id == "DuckStation"

    # build_plan honoruje override
    ps1 = tmp_path / "psx"
    ps1.mkdir()
    (ps1 / "Gra (USA).chd").write_bytes(b"x")
    plan, _ = build_plan(ps1, "PS1", found,
                         override="RetroArch:mednafen_psx_hw")
    assert plan[0].target.name == "retroarch.exe"
    assert '-L "cores\\mednafen_psx_hw_libretro.dll"' in plan[0].arguments

    # Saturn: tylko rdzeń (brak standalone w zestawie)
    sat = resolve_choice("SATURN", found)
    assert sat is not None and sat.id == "RetroArch:mednafen_saturn"


def _com_available() -> bool:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(New-Object -ComObject WScript.Shell) -ne $null"],
        capture_output=True, text=True)
    return r.returncode == 0 and "True" in (r.stdout or "")


def test_create_shortcuts_real_lnk(emu_root: Path, tmp_path: Path):
    if not _com_available():
        pytest.skip("WScript.Shell COM niedostępny")
    found = find_emulators(emu_root)
    d = tmp_path / "ps2"
    d.mkdir()
    (d / "Gra z 'apostrofem' & spółka (USA).chd").write_bytes(b"x")
    plan, _ = build_plan(d, "PS2", found)
    st = create_shortcuts(plan)
    assert st.created == 1 and st.failed == 0
    lnk = d / "shortcuts" / "Gra z 'apostrofem' & spółka (USA).lnk"
    assert lnk.is_file()
    assert lnk.read_bytes()[:4] == b"\x4c\x00\x00\x00"   # nagłówek .lnk

    # drugi przebieg: istniejący skrót nie jest ruszany
    st2 = create_shortcuts(plan)
    assert st2.existing == 1 and st2.created == 0


def test_create_shortcuts_dry_run(emu_root: Path, tmp_path: Path):
    found = find_emulators(emu_root)
    d = tmp_path / "ps2"
    d.mkdir()
    (d / "Gra (USA).chd").write_bytes(b"x")
    plan, _ = build_plan(d, "PS2", found)
    st = create_shortcuts(plan, dry_run=True)
    assert st.created == 1
    assert not (d / "shortcuts").exists()

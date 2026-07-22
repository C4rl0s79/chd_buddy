"""Testy menedżera BIOS-ów i aktualizatora (offline)."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from chd_buddy.core import bios as bios_mod
from chd_buddy.core.bios import (
    BiosScan,
    bios_run,
    import_system_dat,
    resolve_source,
    scan_bios_dir,
)
from chd_buddy.core.updater import _extract


@pytest.fixture()
def manifest(tmp_path: Path, monkeypatch):
    """Manifest w tmp (nie dotykaj prawdziwego bios_manifest.json)."""
    monkeypatch.setattr(bios_mod, "manifest_path",
                        lambda: tmp_path / "bios_manifest.json")
    data = b"BIOS-PS1-US" * 32
    md5 = hashlib.md5(data).hexdigest()
    m = {
        "files": {
            "ps1_us": {"md5": md5, "desc": "PS1 US"},
            "ps2_bios": {"pattern": "*.bin", "size": 1000, "desc": "PS2"},
            "keys": {"name": "prod.keys", "desc": "Switch keys"},
        },
        "emulators": {
            "DuckStation": [{"file": "ps1_us", "as": "scph5501.bin"}],
            "PCSX2": [{"file": "ps2_bios", "as": "ps2_bios.bin"}],
            "Citron": [{"file": "keys", "as": "prod.keys",
                        "subdir": "user/keys"}],
            "RetroArch": [{"file": "ps1_us", "as": "w_zipie.bin",
                           "zip": "paczka.zip"}],
        },
        "install_paths": {"DuckStation": "bios", "PCSX2": "bios",
                          "Citron": "", "RetroArch": "system"},
    }
    (tmp_path / "bios_manifest.json").write_text(
        json.dumps(m), encoding="utf-8")
    return m, data


def test_scan_and_resolve_md5_name_pattern(manifest, tmp_path: Path):
    m, data = manifest
    src = tmp_path / "wejscie"
    src.mkdir()
    # BIOS PS1 w zipie pod PRZYPADKOWĄ nazwą (liczy się MD5)
    with zipfile.ZipFile(src / "jakies_biosy.zip", "w") as z:
        z.writestr("losowa_nazwa.dat", data)
    # PS2 po wzorcu+rozmiarze, keys po nazwie
    (src / "cokolwiek.bin").write_bytes(b"P" * 1000)
    (src / "podkatalog").mkdir()
    (src / "podkatalog" / "prod.keys").write_bytes(b"KEYS")

    scan = scan_bios_dir(src)
    assert resolve_source(m["files"]["ps1_us"], scan) is not None
    assert resolve_source(m["files"]["ps2_bios"], scan) is not None
    assert resolve_source(m["files"]["keys"], scan) is not None
    assert resolve_source({"md5": "0" * 32}, scan) is None


def test_bios_run_install_and_zip_packing(manifest, tmp_path: Path):
    m, data = manifest
    src = tmp_path / "wejscie"
    src.mkdir()
    with zipfile.ZipFile(src / "biosy.zip", "w") as z:
        z.writestr("x.bin", data)
    (src / "dowolny.bin").write_bytes(b"P" * 1000)

    emu_root = tmp_path / "Emulatory"
    for e in ("DuckStation", "PCSX2", "Citron", "RetroArch"):
        (emu_root / e).mkdir(parents=True)

    st = bios_run(src, emu_root=emu_root, manifest=m)
    # DuckStation: bios/scph5501.bin (install_path=bios)
    assert (emu_root / "DuckStation" / "bios" / "scph5501.bin"
            ).read_bytes() == data
    # PCSX2 po wzorcu
    assert (emu_root / "PCSX2" / "bios" / "ps2_bios.bin").exists()
    # RetroArch: BIOS zapakowany do zipa w system/
    zp = emu_root / "RetroArch" / "system" / "paczka.zip"
    with zipfile.ZipFile(zp) as z:
        assert z.read("w_zipie.bin") == data
    # Citron: brak prod.keys w źródle => brak (nieopcjonalny) raportowany
    assert "Citron" in st.missing
    assert st.copied == 3


def test_import_system_dat(manifest, tmp_path: Path):
    m, _data = manifest
    dat = tmp_path / "System.dat"
    dat.write_text(
        'game (\n\tname "Sony - PlayStation"\n'
        '\trom ( name "psxonpsp660.bin" size 524288 '
        'md5 c53ca5908936d412331790f4426c6c33 )\n)\n',
        encoding="utf-8")
    added = import_system_dat(dat, m)
    assert added == 1
    new = [v for v in m["files"].values()
           if v.get("md5") == "c53ca5908936d412331790f4426c6c33"]
    assert new and "Sony - PlayStation" in new[0]["desc"]


def test_updater_extract_strip_root_and_preserve(tmp_path: Path):
    """_extract: zdejmowanie folderu nadrzędnego + ochrona configów."""
    arch = tmp_path / "app.zip"
    with zipfile.ZipFile(arch, "w") as z:
        z.writestr("App-x64/app.exe", "NOWY")
        z.writestr("App-x64/retroarch.cfg", "NOWY-CONFIG")
        z.writestr("App-x64/dane/plik.txt", "NOWE-DANE")
    target = tmp_path / "target"
    target.mkdir()
    (target / "retroarch.cfg").write_text("MOJ-CONFIG", encoding="utf-8")

    _extract(arch, target, strip_root=True, preserve=["retroarch.cfg"])
    assert (target / "app.exe").read_text(encoding="utf-8") == "NOWY"
    assert (target / "dane" / "plik.txt").exists()
    # config użytkownika NIE został nadpisany
    assert (target / "retroarch.cfg").read_text(encoding="utf-8") == "MOJ-CONFIG"


def test_workspace_applies_paths(tmp_path: Path):
    from chd_buddy.core.settings import Settings
    for d in ("Emulatory", "roms", "bios", "dat", "to sort"):
        (tmp_path / d).mkdir()
    s = Settings()
    msgs = s.apply_workspace(str(tmp_path))
    assert s.emulators_dir == str(tmp_path / "Emulatory")
    assert s.rom_root == str(tmp_path / "roms")
    assert s.bios_dir == str(tmp_path / "bios")
    assert s.dat_root == str(tmp_path / "dat")
    assert s.tosort_dir == str(tmp_path / "to sort")
    assert len(msgs) == 5

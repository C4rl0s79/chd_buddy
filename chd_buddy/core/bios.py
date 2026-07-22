"""Menedżer BIOS-ów — wchłonięty BiosManager v1.0.

- Identyfikacja po MD5 (nazwa bez znaczenia); fallback: nazwa/wzorzec+rozmiar.
- Skan rekurencyjny katalogu wejściowego, w tym WNĘTRZA .zip/.7z.
- Manifest (bios_manifest.json obok ustawień): definicje plików + reguły
  per emulator (nazwa docelowa, pakowanie do zip, podkatalogi) + ścieżki
  instalacji w trybie portable (np. RetroArch/system, DuckStation/bios).
- Instalacja prosto do katalogów emulatorów albo eksport do <wyjście>/bios/.
- Import bazy hashy z libretro System.dat (format clrmamepro).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .settings import app_base_dir

MANIFEST_FILENAME = "bios_manifest.json"
CHUNK = 1 << 20
MAX_HASH_SIZE = 64 << 20      # BIOS-y są małe; nie hashuj > 64 MB

LogCB = Callable[[str], None]


def _noop(_m: str) -> None:  # pragma: no cover
    pass


# --- manifest (domyślny — jak w BiosManager v1.0) -----------------------------

DEFAULT_MANIFEST: dict = {
    "files": {
        "ps1_scph5500": {"md5": "8dd7d5296a650fac7319bce665a6a53c", "desc": "PS1 BIOS JP v3.0"},
        "ps1_scph5501": {"md5": "490f666e1afb15b7362b406ed1cea246", "desc": "PS1 BIOS US v3.0"},
        "ps1_scph5502": {"md5": "32736f17079d0b2b7024407c39bd3050", "desc": "PS1 BIOS EU v3.0"},
        "gba_bios":     {"md5": "a860e8c0b6d573d191e4ec7db1b1e4f6", "desc": "GBA BIOS"},
        "segacd_us":    {"md5": "2efd74e3232ff260e371b99f84024f7f", "desc": "Sega CD BIOS US (model 1)"},
        "segacd_eu":    {"md5": "e66fa1dc5820d254611fdcdba0662372", "desc": "Mega-CD BIOS EU (model 1)"},
        "segacd_jp":    {"md5": "278a9397d192149e84e820ac621a8edd", "desc": "Mega-CD BIOS JP (model 1)"},
        "saturn_jp":    {"md5": "85ec9ca47d8f6807718151cbcca8b964", "desc": "Saturn BIOS JP (sega_101)"},
        "saturn_us_eu": {"md5": "3240872c70984b6cbfda1586cab68dbe", "desc": "Saturn BIOS US/EU (mpr-17933)"},
        "dc_boot":      {"md5": "e10c53c2f8b90bab96ead2d368858623", "desc": "Dreamcast boot ROM"},
        "dc_flash":     {"md5": "0a93f7940c455905bea6e392dfde92a4", "desc": "Dreamcast flash"},
        "nds_bios7":    {"md5": "df692a80a5b1bc90728bc3dfc76cd948", "desc": "NDS ARM7 BIOS"},
        "nds_bios9":    {"md5": "a392174eb3e572fed6447e956bde4b25", "desc": "NDS ARM9 BIOS"},
        "pce_syscard3": {"md5": "38179df8f4ac870017db21ebcbf53114", "desc": "PC Engine CD System Card 3"},
        "ps2_bios":       {"pattern": "*.bin", "size": 4194304, "desc": "PS2 BIOS (dowolny zrzut 4 MB)"},
        "xbox_mcpx":      {"md5": "d49c52a4102f6df7bcf8d0617ac475ed", "name": "mcpx_1.0.bin", "desc": "Xbox MCPX bootrom 1.0"},
        "xbox_bios":      {"pattern": "complex_4627*.bin", "desc": "Xbox BIOS Complex 4627"},
        "xbox_eeprom":    {"name": "eeprom.bin", "desc": "Xbox EEPROM"},
        "nds_firmware":   {"name": "firmware.bin", "desc": "NDS firmware (zrzut z konsoli)"},
        "dsi_bios7i":     {"name": "bios7i.bin", "desc": "DSi ARM7i BIOS"},
        "dsi_bios9i":     {"name": "bios9i.bin", "desc": "DSi ARM9i BIOS"},
        "dsi_nand":       {"name": "nand.bin", "desc": "DSi NAND (zrzut z konsoli)"},
        "switch_prodkeys":  {"name": "prod.keys",  "desc": "Switch prod.keys"},
        "switch_titlekeys": {"name": "title.keys", "desc": "Switch title.keys"},
        "cemu_keys":      {"name": "keys.txt", "desc": "Wii U keys.txt"},
        "azahar_aeskeys": {"name": "aes_keys.txt", "desc": "3DS aes_keys.txt"},
        "gc_ipl":         {"name": "ipl.bin", "desc": "GameCube IPL"},
        "naomi_zip":      {"name": "naomi.zip",  "desc": "Naomi BIOS (cały zip z romsetu MAME)"},
        "awbios_zip":     {"name": "awbios.zip", "desc": "Atomiswave BIOS (cały zip)"},
        "neogeo_zip":     {"name": "neogeo.zip", "desc": "Neo Geo BIOS (cały zip)"},
    },
    "emulators": {
        "RetroArch": [
            {"file": "ps1_scph5500", "as": "scph5500.bin"},
            {"file": "ps1_scph5501", "as": "scph5501.bin"},
            {"file": "ps1_scph5502", "as": "scph5502.bin"},
            {"file": "gba_bios",     "as": "gba_bios.bin", "optional": True},
            {"file": "segacd_us",    "as": "bios_CD_U.bin"},
            {"file": "segacd_eu",    "as": "bios_CD_E.bin"},
            {"file": "segacd_jp",    "as": "bios_CD_J.bin"},
            {"file": "saturn_jp",    "as": "sega_101.bin"},
            {"file": "saturn_us_eu", "as": "mpr-17933.bin"},
            {"file": "dc_boot",      "as": "dc/dc_boot.bin", "optional": True},
            {"file": "dc_flash",     "as": "dc/dc_flash.bin", "optional": True},
            {"file": "nds_bios7",    "as": "bios7.bin", "optional": True},
            {"file": "nds_bios9",    "as": "bios9.bin", "optional": True},
            {"file": "nds_firmware", "as": "firmware.bin", "optional": True},
            {"file": "pce_syscard3", "as": "syscard3.pce"},
            {"file": "neogeo_zip",   "as": "neogeo.zip", "optional": True},
        ],
        "DuckStation": [
            {"file": "ps1_scph5500", "as": "scph5500.bin"},
            {"file": "ps1_scph5501", "as": "scph5501.bin"},
            {"file": "ps1_scph5502", "as": "scph5502.bin"},
        ],
        "PCSX2": [{"file": "ps2_bios", "as": "ps2_bios.bin"}],
        "xemu": [
            {"file": "xbox_mcpx",   "as": "mcpx_1.0.bin"},
            {"file": "xbox_bios",   "as": "complex_4627.bin"},
            {"file": "xbox_eeprom", "as": "eeprom.bin", "optional": True},
        ],
        "melonDS": [
            {"file": "nds_bios7",    "as": "bios7.bin"},
            {"file": "nds_bios9",    "as": "bios9.bin"},
            {"file": "nds_firmware", "as": "firmware.bin"},
            {"file": "dsi_bios7i",   "as": "bios7i.bin", "optional": True},
            {"file": "dsi_bios9i",   "as": "bios9i.bin", "optional": True},
            {"file": "dsi_nand",     "as": "nand.bin",   "optional": True},
        ],
        "mGBA": [{"file": "gba_bios", "as": "gba_bios.bin"}],
        "Flycast": [
            {"file": "dc_boot",    "as": "dc_boot.bin", "optional": True},
            {"file": "dc_flash",   "as": "dc_flash.bin", "optional": True},
            {"file": "naomi_zip",  "as": "naomi.zip",  "optional": True},
            {"file": "awbios_zip", "as": "awbios.zip", "optional": True},
        ],
        "ares": [
            {"file": "ps1_scph5500", "as": "scph5500.bin", "optional": True},
            {"file": "ps1_scph5501", "as": "scph5501.bin"},
            {"file": "ps1_scph5502", "as": "scph5502.bin", "optional": True},
            {"file": "segacd_us",    "as": "bios_CD_U.bin", "optional": True},
            {"file": "saturn_us_eu", "as": "mpr-17933.bin", "optional": True},
            {"file": "pce_syscard3", "as": "syscard3.pce", "optional": True},
        ],
        "Dolphin": [{"file": "gc_ipl", "as": "IPL.bin", "optional": True}],
        "Eden": [
            {"file": "switch_prodkeys",  "as": "prod.keys"},
            {"file": "switch_titlekeys", "as": "title.keys", "optional": True},
        ],
        "Citron": [
            {"file": "switch_prodkeys",  "as": "prod.keys"},
            {"file": "switch_titlekeys", "as": "title.keys", "optional": True},
        ],
        "Cemu": [{"file": "cemu_keys", "as": "keys.txt", "optional": True}],
        "Azahar": [{"file": "azahar_aeskeys", "as": "aes_keys.txt", "optional": True}],
        "MAME": [
            {"file": "neogeo_zip", "as": "neogeo.zip", "optional": True},
            {"file": "naomi_zip",  "as": "naomi.zip",  "optional": True},
        ],
    },
    # Ścieżka instalacji względem <Katalog emulatorów>\<Emulator>\ ("" = obok exe).
    "install_paths": {
        "RetroArch":   "system",
        "DuckStation": "bios",
        "PCSX2":       "bios",
        "xemu":        "bios",
        "melonDS":     "",
        "mGBA":        "",
        "Flycast":     "data",
        "ares":        "Firmware",
        "Dolphin":     "User/GC",
        "Eden":        "user/keys",
        "Citron":      "user/keys",
        "Cemu":        "",
        "Azahar":      "user/sysdata",
        "MAME":        "roms",
    },
}


def manifest_path() -> Path:
    return app_base_dir() / MANIFEST_FILENAME


def load_manifest() -> dict:
    """Wczytuje manifest, scalając nowe domyślne wpisy (bez nadpisywania)."""
    p = manifest_path()
    if p.exists():
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            m = {}
    else:
        m = {}
    changed = not p.exists()
    m.setdefault("files", {})
    m.setdefault("emulators", {})
    m.setdefault("install_paths", {})
    for fid, meta in DEFAULT_MANIFEST["files"].items():
        if fid not in m["files"]:
            m["files"][fid] = json.loads(json.dumps(meta))
            changed = True
    for emu, entries in DEFAULT_MANIFEST["emulators"].items():
        if emu not in m["emulators"]:
            m["emulators"][emu] = json.loads(json.dumps(entries))
            changed = True
    for emu, rel in DEFAULT_MANIFEST["install_paths"].items():
        if emu not in m["install_paths"]:
            m["install_paths"][emu] = rel
            changed = True
    if changed:
        save_manifest(m)
    return m


def save_manifest(m: dict) -> None:
    manifest_path().write_text(json.dumps(m, indent=2, ensure_ascii=False),
                               encoding="utf-8")


# --- import libretro System.dat (clrmamepro) ----------------------------------

_ROM_RE = re.compile(
    r'rom\s*\(\s*name\s+(?:"(?P<qname>[^"]+)"|(?P<name>\S+))(?P<rest>[^)]*)\)',
    re.IGNORECASE)
_MD5_RE = re.compile(r'md5\s+([0-9a-fA-F]{32})')
_NAME_RE = re.compile(r'^\s*name\s+"(?P<gname>[^"]+)"', re.IGNORECASE)


def import_system_dat(path: Path, manifest: dict) -> int:
    """Dokłada definicje plików z libretro System.dat. Zwraca liczbę nowych."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    existing = {v.get("md5") for v in manifest["files"].values() if v.get("md5")}
    added = 0
    current = "unknown"
    for line in text.splitlines():
        r = _ROM_RE.search(line)
        if not r:
            g = _NAME_RE.search(line)
            if g:
                current = g.group("gname")
            continue
        name = r.group("qname") or r.group("name")
        m = _MD5_RE.search(r.group("rest"))
        if not m:
            continue
        md5 = m.group(1).lower()
        if md5 in existing:
            continue
        base = re.sub(r"[^A-Za-z0-9]+", "_", f"{current}_{name}").strip("_").lower()
        fid = base
        i = 2
        while fid in manifest["files"]:
            fid = f"{base}_{i}"
            i += 1
        manifest["files"][fid] = {"md5": md5, "desc": f"{current} / {name}"}
        existing.add(md5)
        added += 1
    return added


# --- skaner --------------------------------------------------------------------

@dataclass
class Source:
    """Lokalizacja pliku BIOS: zwykły plik albo członek archiwum."""
    path: Path
    member: Optional[str] = None
    kind: str = "file"          # file | zip | 7z

    def __str__(self) -> str:
        return f"{self.path}::{self.member}" if self.member else str(self.path)

    def read_bytes(self) -> bytes:
        if self.kind == "file":
            return self.path.read_bytes()
        if self.kind == "zip":
            with zipfile.ZipFile(self.path) as z:
                return z.read(self.member)
        if self.kind == "7z":
            import py7zr
            with py7zr.SevenZipFile(self.path) as z:
                return z.read([self.member])[self.member].read()
        raise ValueError(self.kind)


@dataclass
class BiosScan:
    hash_map: dict[str, Source] = field(default_factory=dict)
    name_index: list[tuple[str, int, Source]] = field(default_factory=list)


def _md5_stream(fobj) -> str:
    h = hashlib.md5()
    while True:
        b = fobj.read(CHUNK)
        if not b:
            break
        h.update(b)
    return h.hexdigest()


def scan_bios_dir(root: Path, log: LogCB = _noop) -> BiosScan:
    """Skanuje katalog (rekurencyjnie, z wnętrzami zip/7z) po MD5 i nazwach."""
    scan = BiosScan()
    n_files = n_arch = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if ext == ".zip":
                n_arch += 1
                scan.name_index.append((fn.lower(), size, Source(p)))
                try:
                    with zipfile.ZipFile(p) as z:
                        for info in z.infolist():
                            if info.is_dir() or info.file_size > MAX_HASH_SIZE:
                                continue
                            with z.open(info) as f:
                                md5 = _md5_stream(f)
                            s = Source(p, info.filename, "zip")
                            scan.hash_map.setdefault(md5, s)
                            scan.name_index.append(
                                (Path(info.filename).name.lower(),
                                 info.file_size, s))
                except (OSError, zipfile.BadZipFile) as e:
                    log(f"  [!] Uszkodzony zip: {p} ({e})")
                continue
            if ext == ".7z":
                try:
                    import py7zr
                except ImportError:
                    log(f"  [!] Pominięto .7z (brak py7zr): {p}")
                    continue
                n_arch += 1
                scan.name_index.append((fn.lower(), size, Source(p)))
                try:
                    with py7zr.SevenZipFile(p) as z:
                        names = [i.filename for i in z.list()
                                 if not i.is_directory
                                 and (i.uncompressed or 0) <= MAX_HASH_SIZE]
                        if names:
                            for name, bio in z.read(names).items():
                                buf = bio.read()
                                s = Source(p, name, "7z")
                                scan.hash_map.setdefault(
                                    hashlib.md5(buf).hexdigest(), s)
                                scan.name_index.append(
                                    (Path(name).name.lower(), len(buf), s))
                except Exception as e:
                    log(f"  [!] Uszkodzony 7z: {p} ({e})")
                continue
            if size > MAX_HASH_SIZE:
                continue
            n_files += 1
            try:
                with open(p, "rb") as f:
                    md5 = _md5_stream(f)
            except OSError as e:
                log(f"  [!] Błąd odczytu: {p} ({e})")
                continue
            s = Source(p)
            scan.hash_map.setdefault(md5, s)
            scan.name_index.append((fn.lower(), size, s))
    log(f"Przeskanowano: {n_files} plików, {n_arch} archiwów; "
        f"unikalnych hashy: {len(scan.hash_map)}")
    return scan


def resolve_source(meta: dict, scan: BiosScan) -> Optional[Source]:
    """Dopasowanie: 1) MD5, 2) dokładna nazwa (+rozmiar), 3) wzorzec (+rozmiar)."""
    md5 = (meta.get("md5") or "").lower()
    if md5 and md5 in scan.hash_map:
        return scan.hash_map[md5]
    want = meta.get("size")
    name = meta.get("name")
    if name:
        nl = name.lower()
        for bn, sz, s in scan.name_index:
            if bn == nl and (want is None or sz == want):
                return s
    pat = meta.get("pattern")
    if pat:
        pl = pat.lower()
        for bn, sz, s in scan.name_index:
            if fnmatch.fnmatch(bn, pl) and (want is None or sz == want):
                return s
    return None


# --- eksport / instalacja --------------------------------------------------------

@dataclass
class BiosStats:
    copied: int = 0
    missing: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        n_missing = sum(len(v) for v in self.missing.values())
        return f"skopiowano {self.copied}, brakujące {n_missing}"


def export_for_emulator(emu: str, entries: list, files_db: dict,
                        scan: BiosScan, emu_dir: Path,
                        log: LogCB = _noop) -> tuple[int, list[str]]:
    """Kopiuje/pakuje BIOS-y jednego emulatora do emu_dir."""
    copied = 0
    missing: list[str] = []
    zip_groups: dict[tuple[str, str], list[tuple[str, Source]]] = {}
    for entry in entries:
        fid = entry["file"]
        meta = files_db.get(fid)
        if not meta:
            log(f"  [!] {emu}: brak definicji '{fid}' w manifeście")
            continue
        src = resolve_source(meta, scan)
        if src is None:
            if not entry.get("optional"):
                how = meta.get("md5") or meta.get("name") or meta.get("pattern") or "?"
                missing.append(f"{fid} ({meta.get('desc', '')}) [{how}]")
            continue
        subdir = entry.get("subdir", "")
        if "zip" in entry:
            zip_groups.setdefault((subdir, entry["zip"]), []).append(
                (entry["as"], src))
            continue
        dest = emu_dir / subdir / entry["as"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(src.read_bytes())
            copied += 1
            log(f"  [+] {emu}: {src} -> {dest}")
        except Exception as e:
            log(f"  [!] {emu}: błąd zapisu {dest}: {e}")
    for (subdir, zname), members in zip_groups.items():
        dest = emu_dir / subdir / zname
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
                for arcname, src in members:
                    z.writestr(arcname, src.read_bytes())
                    copied += 1
            log(f"  [+] {emu}: zapakowano {len(members)} plików -> {dest}")
        except Exception as e:
            log(f"  [!] {emu}: błąd tworzenia {dest}: {e}")
    return copied, missing


def bios_run(input_dir: Path, *, emu_root: Optional[Path] = None,
             out_dir: Optional[Path] = None,
             only: Optional[list[str]] = None,
             manifest: Optional[dict] = None,
             log: LogCB = _noop) -> BiosStats:
    """Skan + instalacja do emulatorów (emu_root) i/lub eksport (out_dir/bios).

    Instalacja: <emu_root>/<Emulator>/<install_path>/…; emulatory bez
    katalogu są pomijane z komunikatem.
    """
    m = manifest or load_manifest()
    scan = scan_bios_dir(Path(input_dir), log)
    stats = BiosStats()
    emus = only or sorted(m["emulators"])
    for emu in emus:
        entries = m["emulators"].get(emu)
        if not entries:
            log(f"  [!] nieznany emulator w manifeście: {emu}")
            continue
        dests: list[Path] = []
        if emu_root is not None:
            base = Path(emu_root) / emu
            if base.is_dir():
                rel = m.get("install_paths", {}).get(emu, "")
                dests.append(base / rel if rel else base)
            else:
                log(f"  [i] {emu}: brak katalogu {base} — pomijam instalację")
        if out_dir is not None:
            dests.append(Path(out_dir) / "bios" / emu)
        for dest in dests:
            log(f"=== {emu} -> {dest} ===")
            copied, missing = export_for_emulator(
                emu, entries, m["files"], scan, dest, log)
            stats.copied += copied
            if missing:
                stats.missing.setdefault(emu, missing)
    for emu, lst in stats.missing.items():
        for x in lst:
            log(f"  [-] BRAK {emu}: {x}")
    return stats

"""Skróty .lnk do gier — z właściwą składnią uruchamiania per emulator.

Rejestr emulatorów (EMULATORS) zna: wzorce exe, szablon argumentów i systemy.
Wykrywanie: podajesz katalog główny emulatorów (np. D:\\emu\\Emulatory),
moduł znajduje zainstalowane exe. System gry bierze się z nazwy katalogu
ROM-ów (nazwy DAT-ów Redump/No-Intro i skróty EmulationStation).

Składnie uruchamiania (zweryfikowane dla wersji Qt/aktualnych):
  DuckStation/PCSX2/PPSSPP/melonDS/mGBA/Snes9x/Flycast/ares/Xenia:
      <exe> "<rom>"                       (ścieżka pozycyjna)
  Dolphin:   Dolphin.exe -e "<rom>"      (bez -e otwiera tylko GUI)
  Cemu:      Cemu.exe -g "<rom>"
  Citron/Eden (forki yuzu): <exe> -g "<rom>"
  xemu:      xemu.exe -dvd_path "<rom>"
  MAME:      mame.exe <nazwa_setu> -rompath "<katalog>"  (set, nie ścieżka!)
  RetroArch: retroarch.exe -L "cores\\<core>_libretro.dll" "<rom>"
  RPCS3:     rpcs3.exe "<EBOOT.BIN>"     (gra = katalog; wspieramy pliki)
  shadPS4:   shadPS4.exe -g "<rom>"

Tworzenie .lnk: wsadowo przez PowerShell + WScript.Shell (COM), z manifestem
JSON (zero problemów z cudzysłowami w tytułach) i weryfikacją po zapisie.
Istniejące .lnk nie są nadpisywane bez overwrite.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from .icons import GAME_EXTS, strip_disc_tag

LogCB = Callable[[str], None]


# --- rejestr emulatorów ---------------------------------------------------------

@dataclass(frozen=True)
class EmuSpec:
    name: str
    exe_globs: tuple[str, ...]        # względem katalogu emulatora
    args: str                         # szablon: {rom} {romdir} {romstem} {core}
    systems: tuple[str, ...]          # kanoniczne skróty systemów
    core: str = ""                    # tylko RetroArch: core libretro


EMULATORS: tuple[EmuSpec, ...] = (
    # -batch: zamknij emulator po wyjściu z gry; -fullscreen: od razu pełny
    # ekran; "--" kończy przełączniki (tytuły zaczynające się od "-").
    EmuSpec("DuckStation", ("duckstation*.exe",),
            '-batch -fullscreen -- "{rom}"', ("PS1",)),
    EmuSpec("PCSX2", ("pcsx2*.exe",),
            '-batch -fullscreen -- "{rom}"', ("PS2",)),
    EmuSpec("RPCS3", ("rpcs3.exe",), '"{rom}"', ("PS3",)),
    EmuSpec("PPSSPP", ("PPSSPPWindows64.exe", "PPSSPPWindows.exe"),
            '"{rom}"', ("PSP",)),
    EmuSpec("shadPS4", ("shadPS4.exe",), '-g "{rom}"', ("PS4",)),
    EmuSpec("Flycast", ("flycast.exe",), '"{rom}"', ("DC", "NAOMI")),
    EmuSpec("Dolphin", ("Dolphin.exe",), '-e "{rom}"', ("GCN", "WII")),
    EmuSpec("Cemu", ("Cemu.exe",), '-g "{rom}"', ("WIIU",)),
    EmuSpec("Citron", ("citron.exe",), '-g "{rom}"', ("NSW",)),
    EmuSpec("Eden", ("eden.exe",), '-g "{rom}"', ("NSW",)),
    EmuSpec("Azahar", ("azahar.exe",), '"{rom}"', ("3DS",)),
    EmuSpec("melonDS", ("melonDS.exe",), '"{rom}"', ("NDS",)),
    EmuSpec("mGBA", ("mGBA.exe",), '"{rom}"', ("GBA", "GB", "GBC")),
    EmuSpec("Snes9x", ("snes9x-x64.exe", "snes9x.exe"), '"{rom}"', ("SNES",)),
    EmuSpec("xemu", ("xemu.exe",), '-dvd_path "{rom}"', ("XBOX",)),
    EmuSpec("Xenia", ("xenia_canary.exe", "xenia.exe"), '"{rom}"', ("X360",)),
    EmuSpec("MAME", ("mame.exe",), '{romstem} -rompath "{romdir}"',
            ("ARCADE", "MAME", "NEOGEO")),
    EmuSpec("ares", ("ares.exe",), '"{rom}"', ("N64", "NES", "MD", "SMS", "GG")),
    # RetroArch jako fallback z konkretnym corem
    EmuSpec("RetroArch", ("retroarch.exe",),
            '-L "cores\\{core}_libretro.dll" "{rom}"',
            ("SATURN",), core="mednafen_saturn"),
)

# Nazwy katalogów (DAT-y Redump/No-Intro + skróty EmulationStation) → system.
SYSTEM_ALIASES: dict[str, str] = {
    "sony - playstation": "PS1", "ps1": "PS1", "psx": "PS1",
    "sony - playstation 2": "PS2", "ps2": "PS2",
    "sony - playstation 3": "PS3", "ps3": "PS3",
    "sony - playstation 4": "PS4", "ps4": "PS4",
    "sony - playstation portable": "PSP", "psp": "PSP",
    "sony - playstation vita": "PSVITA", "psvita": "PSVITA",
    "sega - dreamcast": "DC", "dreamcast": "DC", "dc": "DC",
    "naomi": "NAOMI", "naomi2": "NAOMI",
    "sega - saturn": "SATURN", "saturn": "SATURN",
    "sega - mega drive - genesis": "MD", "megadrive": "MD", "genesis": "MD",
    "sega - master system - mark iii": "SMS", "mastersystem": "SMS",
    "sega - game gear": "GG", "gamegear": "GG",
    "nintendo - gamecube": "GCN", "gamecube": "GCN", "gc": "GCN", "ngc": "GCN",
    "nintendo - wii": "WII", "wii": "WII",
    "nintendo - wii u": "WIIU", "wiiu": "WIIU",
    "nintendo - nintendo switch": "NSW", "switch": "NSW", "nsw": "NSW",
    "nintendo - nintendo 3ds": "3DS", "3ds": "3DS", "n3ds": "3DS",
    "nintendo - nintendo ds": "NDS", "nds": "NDS",
    "nintendo - game boy advance": "GBA", "gba": "GBA",
    "nintendo - game boy color": "GBC", "gbc": "GBC",
    "nintendo - game boy": "GB", "gb": "GB",
    "nintendo - super nintendo entertainment system": "SNES", "snes": "SNES",
    "nintendo - nintendo entertainment system": "NES", "nes": "NES",
    "nintendo - nintendo 64": "N64", "n64": "N64",
    "microsoft - xbox": "XBOX", "xbox": "XBOX",
    "microsoft - xbox 360": "X360", "xbox360": "X360", "x360": "X360",
    "arcade": "ARCADE", "mame": "MAME", "fbneo": "ARCADE",
    "snk - neo geo": "NEOGEO", "neogeo": "NEOGEO",
    # pełne nazwy zestawów DAT (po clean_system_name) — spójność naming=es:
    # bez wpisu tutaj katalog dostawał nazwę z DAT-a zamiast ES i wychodziła
    # mieszanina konwencji ("część tak, część srak").
    "arcade - sega - naomi": "NAOMI",
    "arcade - sega - naomi 2": "NAOMI2", "naomi2": "NAOMI2",
    "sega - mega cd & sega cd": "SEGACD", "segacd": "SEGACD",
    "megacd": "SEGACD", "sega cd": "SEGACD",
    "atari - atari jaguar": "JAGUAR", "atarijaguar": "JAGUAR",
    "jaguar": "JAGUAR",
    "atari - atari 5200": "ATARI5200", "atari5200": "ATARI5200",
    "atari - atari 7800": "ATARI7800", "atari7800": "ATARI7800",
    "atari - atari 2600": "ATARI2600", "atari2600": "ATARI2600",
    "atari - atari lynx": "LYNX", "lynx": "LYNX",
    "microsoft - msx2": "MSX2", "msx2": "MSX2",
    "microsoft - msx": "MSX", "msx": "MSX",
    "nintendo - gamecube - nkit rvz": "GCN",
    "nintendo - wii - nkit rvz": "WII",
    "finalburn neo - arcade games": "FBNEO",
    "commodore - amiga": "AMIGA", "amiga": "AMIGA",
    "commodore - commodore 64": "C64", "c64": "C64",
    "nec - pc-9801": "PC98", "pc98": "PC98", "nec - pc-98": "PC98",
    "sega - sg-1000": "SG1000", "sg1000": "SG1000",
    "nec - pc engine supergrafx": "SUPERGRAFX", "supergrafx": "SUPERGRAFX",
    "nec - pc engine - turbografx-16": "PCENGINE",
    "nec - pc-88 series": "PC88", "pc88": "PC88",
    "nec - pc-fx & pc-fxga": "PCFX", "pcfx": "PCFX",
    "nintendo - family computer disk system": "FDS", "fds": "FDS",
    "nintendo - game & watch": "GAMEANDWATCH",
    "nintendo - new nintendo 3ds": "3DS",
    "nintendo - satellaview": "SATELLAVIEW",
    "nintendo - sufami turbo": "SUFAMI",
    "panasonic - 3do interactive multiplayer": "3DO", "3do": "3DO",
    "sega - 32x": "SEGA32X", "sega32x": "SEGA32X",
    "atari - jaguar cd interactive multimedia system": "JAGUARCD",
    "commodore - amiga cd32": "AMIGACD32", "amigacd32": "AMIGACD32",
    "magnavox - odyssey 2": "ODYSSEY2", "odyssey2": "ODYSSEY2",
    "mattel - intellivision": "INTELLIVISION", "intellivision": "INTELLIVISION",
    "snk - neo geo cd": "NEOGEOCD", "neogeocd": "NEOGEOCD",
}

# Emulatory rozumiejące playlisty .m3u (multi-disc jako jeden skrót).
M3U_CAPABLE = {"DuckStation", "RetroArch", "Flycast", "melonDS"}

# Rdzenie libretro → systemy (port z PyLinks; stem pliku bez _libretro.dll).
RETROARCH_CORE_SYSTEMS: dict[str, tuple[str, ...]] = {
    "mednafen_psx": ("PS1",), "mednafen_psx_hw": ("PS1",),
    "pcsx_rearmed": ("PS1",), "swanstation": ("PS1",), "duckstation": ("PS1",),
    "pcsx2": ("PS2",), "ppsspp": ("PSP",),
    "nestopia": ("NES",), "fceumm": ("NES",), "mesen": ("NES",),
    "quicknes": ("NES",), "mesen-s": ("SNES",),
    "snes9x": ("SNES",), "snes9x2002": ("SNES",), "snes9x2005": ("SNES",),
    "snes9x2010": ("SNES",), "bsnes": ("SNES",), "bsnes_hd_beta": ("SNES",),
    "bsnes_mercury_accuracy": ("SNES",), "bsnes_mercury_balanced": ("SNES",),
    "bsnes_mercury_performance": ("SNES",), "mednafen_supafaust": ("SNES",),
    "mupen64plus_next": ("N64",), "parallel_n64": ("N64",),
    "gambatte": ("GB", "GBC"), "sameboy": ("GB", "GBC"), "tgbdual": ("GB", "GBC"),
    "mgba": ("GBA", "GB", "GBC"), "vba_next": ("GBA", "GB", "GBC"),
    "vbam": ("GBA", "GB", "GBC"),
    "desmume": ("NDS",), "desmume2015": ("NDS",), "melonds": ("NDS",),
    "dolphin": ("GCN", "WII"),
    "mednafen_saturn": ("SATURN",), "yabause": ("SATURN",),
    "yabasanshiro": ("SATURN",), "kronos": ("SATURN",),
    "flycast": ("DC", "NAOMI"), "flycast_gles2": ("DC",), "redream": ("DC",),
    "genesis_plus_gx": ("MD",), "genesis_plus_gx_wide": ("MD",),
    "picodrive": ("MD", "SMS", "GG"), "blastem": ("MD",),
    "gearsystem": ("SMS", "GG"),
    "mednafen_pce": ("PCENGINE",), "mednafen_pce_fast": ("PCENGINE",),
    "mame": ("MAME", "ARCADE"), "mame2000": ("MAME",), "mame2003": ("MAME",),
    "mame2003_plus": ("MAME",), "mame2010": ("MAME",), "mame2015": ("MAME",),
    "mame2016": ("MAME",),
    "fbneo": ("MAME", "ARCADE", "NEOGEO"), "fbalpha2012": ("MAME", "NEOGEO"),
    "fbalpha2012_neogeo": ("NEOGEO",),
    "stella": ("ATARI2600",), "stella2014": ("ATARI2600",),
    "opera": ("3DO",), "mednafen_ngp": ("NGP",), "race": ("NGP",),
}

# Przyjazne nazwy rdzeni (do wyświetlania).
RETROARCH_CORE_DISPLAY: dict[str, str] = {
    "mednafen_psx": "Beetle PSX", "mednafen_psx_hw": "Beetle PSX HW",
    "pcsx_rearmed": "PCSX-ReARMed", "swanstation": "SwanStation",
    "duckstation": "DuckStation", "pcsx2": "PCSX2", "ppsspp": "PPSSPP",
    "nestopia": "Nestopia UE", "fceumm": "FCEUmm", "mesen": "Mesen",
    "snes9x": "Snes9x", "bsnes": "bsnes",
    "mupen64plus_next": "Mupen64Plus-Next", "parallel_n64": "ParaLLEl N64",
    "gambatte": "Gambatte", "sameboy": "SameBoy", "mgba": "mGBA",
    "melonds": "melonDS", "desmume": "DeSmuME", "dolphin": "Dolphin",
    "mednafen_saturn": "Beetle Saturn", "yabasanshiro": "YabaSanshiro",
    "kronos": "Kronos", "flycast": "Flycast", "redream": "Redream",
    "genesis_plus_gx": "Genesis Plus GX", "picodrive": "PicoDrive",
    "blastem": "BlastEm", "gearsystem": "Gearsystem",
    "mednafen_pce": "Beetle PCE", "mame": "MAME (current)",
    "fbneo": "FinalBurn Neo", "stella": "Stella", "opera": "Opera (3DO)",
    "mednafen_ngp": "Beetle NeoPop",
}


@dataclass(frozen=True)
class EmuChoice:
    """Jedna opcja uruchamiania systemu: standalone albo rdzeń RetroArch."""
    id: str          # "PCSX2" albo "RetroArch:mednafen_saturn"
    label: str       # do wyświetlenia w GUI
    exe: Path
    args: str        # szablon jak w EmuSpec
    m3u_ok: bool


def retroarch_cores(installed: dict[str, Path]) -> dict[str, Path]:
    """Zainstalowane rdzenie libretro: stem (bez _libretro.dll) → ścieżka."""
    ra = installed.get("RetroArch")
    if ra is None:
        return {}
    cores = ra.parent / "cores"
    if not cores.is_dir():
        return {}
    out: dict[str, Path] = {}
    for dll in sorted(cores.glob("*_libretro.dll")):
        out[dll.name[:-len("_libretro.dll")]] = dll
    return out


def emulator_options(system: str, installed: dict[str, Path]) -> list[EmuChoice]:
    """Wszystkie sposoby uruchomienia systemu: standalone + rdzenie RetroArch."""
    out: list[EmuChoice] = []
    for spec in EMULATORS:
        if spec.name == "RetroArch" or system not in spec.systems:
            continue
        exe = installed.get(spec.name)
        if exe is None:
            continue
        out.append(EmuChoice(
            id=spec.name, label=f"{spec.name} (standalone)", exe=exe,
            args=spec.args, m3u_ok=spec.name in M3U_CAPABLE))
    ra = installed.get("RetroArch")
    if ra is not None:
        for stem in sorted(retroarch_cores(installed)):
            if system not in RETROARCH_CORE_SYSTEMS.get(stem, ()):
                continue
            label = RETROARCH_CORE_DISPLAY.get(stem, stem)
            out.append(EmuChoice(
                id=f"RetroArch:{stem}",
                label=f"RetroArch — {label}", exe=ra,
                args=f'-L "cores\\{stem}_libretro.dll" "{{rom}}"',
                m3u_ok=True))
    return out


def resolve_choice(system: str, installed: dict[str, Path],
                   override_id: str = "") -> Optional[EmuChoice]:
    """Opcja dla systemu: wskazana przez użytkownika albo pierwsza dostępna.

    Nieaktualny override (odinstalowany emulator/rdzeń) jest ignorowany —
    wracamy do domyślnej kolejności rejestru.
    """
    options = emulator_options(system, installed)
    if override_id:
        for o in options:
            if o.id == override_id:
                return o
    return options[0] if options else None


def detect_system(dir_name: str) -> str:
    return SYSTEM_ALIASES.get(dir_name.strip().lower(), "")


def find_emulators(emu_root: Path) -> dict[str, Path]:
    """Skanuje katalog emulatorów: nazwa specyfikacji → ścieżka exe."""
    emu_root = Path(emu_root)
    found: dict[str, Path] = {}
    if not emu_root.is_dir():
        return found
    subdirs = {d.name.lower(): d for d in emu_root.iterdir() if d.is_dir()}
    for spec in EMULATORS:
        base = subdirs.get(spec.name.lower())
        search_dirs = [base] if base else list(subdirs.values())
        for d in search_dirs:
            for pattern in spec.exe_globs:
                hits = sorted(d.glob(pattern))
                if hits:
                    found[spec.name] = hits[0]
                    break
            if spec.name in found:
                break
    return found


def emulator_for_system(system: str, installed: dict[str, Path]
                        ) -> Optional[tuple[EmuSpec, Path]]:
    """Pierwszy zainstalowany emulator obsługujący system (kolejność rejestru)."""
    for spec in EMULATORS:
        if system in spec.systems and spec.name in installed:
            return spec, installed[spec.name]
    return None


# --- plan skrótów -----------------------------------------------------------------

@dataclass
class ShortcutSpec:
    title: str
    lnk_path: Path
    target: Path
    arguments: str
    workdir: Path
    icon: Optional[Path] = None
    rom: Optional[Path] = None


@dataclass
class ShortcutStats:
    created: int = 0
    existing: int = 0
    failed: int = 0
    no_emulator: int = 0

    def summary(self) -> str:
        return (f"utworzono {self.created}, istniejące {self.existing}, "
                f"błędy {self.failed}, bez emulatora {self.no_emulator}")


# Podkatalogi zarządzane/robocze — nie zawierają gier do skrótów.
_SKIP_SUBDIRS = {"icons", "shortcuts", "images", "manuals", "videos"}


def _pick_rom_files(rom_dir: Path, m3u_ok: bool) -> list[tuple[str, Path]]:
    """Jedna pozycja (tytuł, plik) na grę.

    Obsługiwane układy: pliki płasko (CHD/iso/zip), .m3u dla multi-disc,
    oraz PODKATALOG per gra (bin/cue, gdi+tracki) — wtedy tytułem jest
    nazwa podkatalogu, a plikiem główny wpis wg priorytetu (cue > gdi > …).
    """
    files = sorted(p for p in rom_dir.iterdir() if p.is_file()
                   and p.suffix.lower().lstrip(".") in GAME_EXTS)
    m3u = {strip_disc_tag(p.stem).lower(): p for p in files
           if p.suffix.lower() == ".m3u"}
    picked: dict[str, tuple[str, Path]] = {}
    for p in files:
        if p.suffix.lower() == ".m3u":
            continue
        title = strip_disc_tag(p.stem) or p.stem
        base = title.lower()
        if base in m3u:
            if m3u_ok:
                picked.setdefault(base, (title, m3u[base]))
                continue
            # emulator bez m3u: pierwszy dysk (sortowanie da Disc 1)
        picked.setdefault(base, (title, p))
    # gry w PODKATALOGACH per gra
    from .playlists import _pick_main_file
    for d in sorted(p for p in rom_dir.iterdir() if p.is_dir()):
        low = d.name.lower()
        if low in _SKIP_SUBDIRS or low.startswith(("chdbuddy_", "chddeep_")):
            continue
        title = strip_disc_tag(d.name) or d.name
        base = title.lower()
        if base in m3u and m3u_ok:
            picked.setdefault(base, (title, m3u[base]))
            continue
        try:
            inner = [p for p in sorted(d.iterdir()) if p.is_file()]
        except OSError:
            continue
        main = _pick_main_file(inner)
        if main is not None:
            picked.setdefault(base, (title, main))
    # gry mające tylko .m3u
    for base, p in m3u.items():
        picked.setdefault(base, (strip_disc_tag(p.stem) or p.stem, p))
    return [picked[k] for k in sorted(picked)]


def build_plan(
    rom_dir: Path,
    system: str,
    installed: dict[str, Path],
    *,
    override: str = "",
    out_dir: Optional[Path] = None,
    icons_dir: Optional[Path] = None,
) -> tuple[list[ShortcutSpec], Optional[str]]:
    """Plan skrótów dla katalogu jednego systemu.

    override — id opcji wybranej przez użytkownika ("PCSX2" /
    "RetroArch:mednafen_saturn"); puste = domyślna kolejność rejestru.
    Zwraca (plan, None) albo ([], powód) gdy brak emulatora dla systemu.
    """
    rom_dir = Path(rom_dir)
    choice = resolve_choice(system, installed, override)
    if choice is None:
        return [], f"brak emulatora dla systemu '{system}'"
    dest = Path(out_dir) if out_dir else rom_dir / "shortcuts"
    icons = Path(icons_dir) if icons_dir else rom_dir / "icons"
    plan: list[ShortcutSpec] = []
    for title, rom in _pick_rom_files(rom_dir, choice.m3u_ok):
        args = choice.args.format(rom=rom, romdir=rom.parent,
                                  romstem=rom.stem, core="")
        ico = icons / f"{title}.ico"
        plan.append(ShortcutSpec(
            title=title,
            lnk_path=dest / f"{title}.lnk",
            target=choice.exe,
            arguments=args,
            workdir=choice.exe.parent,
            icon=ico if ico.is_file() else None,
            rom=rom,
        ))
    return plan, None


# --- tworzenie .lnk (PowerShell + WScript.Shell, wsadowo) ---------------------------

# Skrót powstaje pod tymczasową nazwą ASCII (COM Save() potrafi znormalizować
# ścieżkę z nietypowymi znakami inaczej niż Python — lekcja z PyLinks v7.9.2),
# a docelową nazwę nadaje Python atomowym os.replace. Wyjście skryptu jest
# czysto ASCII (indeksy), więc kodowanie konsoli PowerShell 5.1 nie ma znaczenia.
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$manifest = Get-Content -LiteralPath $args[0] -Raw -Encoding UTF8 | ConvertFrom-Json
$sh = New-Object -ComObject WScript.Shell
foreach ($e in $manifest) {
  try {
    $lnk = $sh.CreateShortcut($e.tmp)
    $lnk.TargetPath = $e.target
    $lnk.Arguments = $e.args
    $lnk.WorkingDirectory = $e.workdir
    if ($e.icon) { $lnk.IconLocation = "$($e.icon),0" }
    $lnk.Save()
    if (Test-Path -LiteralPath $e.tmp) { Write-Output "OK`t$($e.i)" }
    else { Write-Output "FAIL`t$($e.i)`tSave nie utworzyl pliku" }
  } catch {
    Write-Output "FAIL`t$($e.i)`t$($_.Exception.Message)"
  }
}
"""


def create_shortcuts(
    plan: Sequence[ShortcutSpec],
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    log: Optional[LogCB] = None,
) -> ShortcutStats:
    stats = ShortcutStats()

    def _log(msg: str) -> None:
        if log:
            log(msg)

    todo: list[ShortcutSpec] = []
    for s in plan:
        if s.lnk_path.exists() and not overwrite:
            stats.existing += 1
            continue
        _log(f"LNK  {s.lnk_path.name}  ->  {s.target.name} {s.arguments}")
        todo.append(s)

    if dry_run or not todo:
        stats.created = len(todo) if dry_run else 0
        return stats

    # katalogi docelowe + tymczasowe nazwy ASCII (w katalogu docelowym,
    # żeby os.replace był atomowy w obrębie woluminu)
    tmp_paths: list[Path] = []
    for n, s in enumerate(todo):
        s.lnk_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_paths.append(s.lnk_path.parent / f"chdbuddy_tmp_{n}.lnk")

    manifest = [{
        "i": n,
        "tmp": str(tmp),
        "target": str(s.target),
        "args": s.arguments,
        "workdir": str(s.workdir),
        "icon": str(s.icon) if s.icon else "",
    } for n, (s, tmp) in enumerate(zip(todo, tmp_paths))]

    with tempfile.TemporaryDirectory(prefix="chdbuddy_lnk_") as td:
        mpath = Path(td) / "manifest.json"
        spath = Path(td) / "make_links.ps1"
        mpath.write_text(json.dumps(manifest, ensure_ascii=False),
                         encoding="utf-8")
        spath.write_text(_PS_SCRIPT, encoding="utf-8-sig")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(spath), str(mpath)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    results: dict[int, list[str]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] in ("OK", "FAIL"):
            try:
                results[int(parts[1])] = parts
            except ValueError:
                pass
    for n, (s, tmp) in enumerate(zip(todo, tmp_paths)):
        r = results.get(n)
        if r and r[0] == "OK" and tmp.is_file():
            try:
                os.replace(tmp, s.lnk_path)
                stats.created += 1
                continue
            except OSError as e:
                r = ["FAIL", str(n), str(e)]
        stats.failed += 1
        reason = r[2] if r and len(r) > 2 else (proc.stderr or "?").strip()
        _log(f"BŁĄD {s.lnk_path.name}: {reason}")
        tmp.unlink(missing_ok=True)
    return stats

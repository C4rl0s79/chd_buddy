"""Konwersja plików gry do docelowego formatu przechowywania.

Formaty i narzędzia:
  ZIP  — kartridże; stdlib zipfile (deflate).
  7z   — opcjonalnie; py7zr.
  CHD  — płyty; chdman (createcd dla bin/cue/gdi, createdvd dla iso).
  RVZ  — GameCube/Wii; DolphinTool.exe convert (mają OSOBNE DAT-y na RVZ).
  extract — rozpakuj archiwum/CHD do plików luzem (w podkatalogu per gra).

Każda konwersja jest WERYFIKOWANA przed usunięciem źródła:
  ZIP/7z  — po spakowaniu odczytujemy z powrotem i porównujemy SHA-1 członków;
  CHD     — round-trip (extract) + porównanie SHA-1 z oryginałem;
  RVZ     — DolphinTool verify (SHA-1 zdekompresowanego obrazu == źródło).
Źródło jest kasowane dopiero po udanej weryfikacji (atomowa podmiana tmp).
"""
from __future__ import annotations

import os
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .fileindex import hash_file

LogCB = Callable[[str], None]

CART_EXTS = {"nes", "sfc", "smc", "gb", "gbc", "gba", "n64", "z64", "v64",
             "md", "gen", "sms", "gg", "nds", "3ds", "cia", "pce", "a26",
             "a78", "lnx", "ws", "wsc", "col", "int", "vec", "rom", "bin"}
DISC_EXTS = {"iso", "cue", "gdi", "bin", "img", "chd"}


@dataclass
class ConvertResult:
    ok: bool
    dst: Optional[Path] = None
    message: str = ""


# --- wykrywanie narzędzi ------------------------------------------------------

def detect_dolphintool(emu_root: Path) -> Optional[Path]:
    """Znajduje DolphinTool.exe (konwersja RVZ) w katalogu emulatorów."""
    for cand in (emu_root / "Dolphin" / "DolphinTool.exe",):
        if cand.is_file():
            return cand
    hits = sorted(emu_root.rglob("DolphinTool.exe"))
    return hits[0] if hits else None


# --- ZIP ----------------------------------------------------------------------

def pack_zip(files: Sequence[Path], dst_zip: Path, *,
             arcnames: Optional[Sequence[str]] = None,
             level: int = 6,
             log: LogCB = lambda m: None) -> ConvertResult:
    """Pakuje pliki do ZIP i WERYFIKUJE (SHA-1 członków po odczycie).
    `level` — poziom DEFLATE 0–9 (0=store, 6=domyślny, 9=maks)."""
    arcnames = list(arcnames) if arcnames else [f.name for f in files]
    tmp = dst_zip.with_name(dst_zip.name + ".chdbuddy_tmp.zip")
    expected: dict[str, str] = {}
    lvl = max(0, min(int(level), 9))
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=lvl) as z:
            for f, arc in zip(files, arcnames):
                _, _, sha1 = hash_file(f)
                expected[arc] = sha1
                z.write(f, arc)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return ConvertResult(False, message=f"pakowanie ZIP: {e}")
    # weryfikacja
    import hashlib
    try:
        with zipfile.ZipFile(tmp) as z:
            for arc, want in expected.items():
                got = hashlib.sha1(z.read(arc)).hexdigest()
                if got != want:
                    tmp.unlink(missing_ok=True)
                    return ConvertResult(False,
                        message=f"ZIP weryfikacja: {arc} sha1 nie zgadza się")
    except (OSError, zipfile.BadZipFile) as e:
        tmp.unlink(missing_ok=True)
        return ConvertResult(False, message=f"ZIP weryfikacja: {e}")
    os.replace(tmp, dst_zip)
    log(f"  ZIP OK: {dst_zip.name} ({len(files)} plików, zweryfikowane)")
    return ConvertResult(True, dst=dst_zip)


# --- CHD (przez chdman) -------------------------------------------------------

def disc_to_chd(main_file: Path, dst_chd: Path, chdman, settings, *,
                log: LogCB = lambda m: None, on_progress=None) -> ConvertResult:
    """Konwertuje obraz płyty (cue/gdi/iso) do CHD z round-trip.

    Używa istniejącej logiki fixer.create_from_source (create + round-trip
    verify). `on_progress(pct, msg)` — postęp kompresji/weryfikacji (pasek
    szczegółowy). Zwraca ścieżkę CHD po udanej weryfikacji."""
    from . import fixer, presets
    from .models import MediaType
    ext = main_file.suffix.lower().lstrip(".")
    media = MediaType.DVD if ext == "iso" else MediaType.CD
    comp = presets.compression_for(settings.compression_preset, media)
    kw = {"on_progress": on_progress} if on_progress is not None else {}
    out = fixer.create_from_source(
        chdman, main_file, media, dst_chd.parent, settings,
        compression=comp, log=lambda m: log(f"  {m}"), delete_source=False,
        **kw)
    if not out.ok:
        return ConvertResult(False, message=out.message)
    made = dst_chd.parent / (main_file.stem + ".chd")
    if made != dst_chd and made.is_file():
        os.replace(made, dst_chd)
    log(f"  CHD OK: {dst_chd.name} (round-trip zweryfikowany)")
    return ConvertResult(True, dst=dst_chd)


# --- RVZ (przez DolphinTool) --------------------------------------------------

def iso_to_rvz(iso: Path, dst_rvz: Path, dolphintool: Path, *,
               level: int = 5, block_kb: int = 128,
               log: LogCB = lambda m: None) -> ConvertResult:
    """Konwertuje obraz GameCube/Wii (iso) do RVZ i weryfikuje (verify).
    `level` — zstd 1–22 (5=domyślny); `block_kb` — rozmiar bloku w KB."""
    tmp = dst_rvz.with_name(dst_rvz.name + ".chdbuddy_tmp.rvz")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    lvl = str(max(1, min(int(level), 22)))
    block = str(max(32, int(block_kb)) * 1024)
    try:
        r = subprocess.run(
            [str(dolphintool), "convert", "-i", str(iso), "-o", str(tmp),
             "-f", "rvz", "-c", "zstd", "-l", lvl, "-b", block],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=flags)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            return ConvertResult(False, message=f"DolphinTool convert: "
                                                f"{(r.stderr or '').strip()[:200]}")
        v = subprocess.run(
            [str(dolphintool), "verify", "-i", str(tmp), "-a", "sha1"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=flags)
        if v.returncode != 0:
            tmp.unlink(missing_ok=True)
            return ConvertResult(False, message="RVZ verify nieudany")
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return ConvertResult(False, message=f"DolphinTool: {e}")
    os.replace(tmp, dst_rvz)
    log(f"  RVZ OK: {dst_rvz.name} (DolphinTool verify)")
    return ConvertResult(True, dst=dst_rvz)


# --- plan konwersji per gra ---------------------------------------------------

def current_format(files: Sequence[Path]) -> str:
    """Zgaduje aktualny format zestawu plików gry."""
    exts = {f.suffix.lower().lstrip(".") for f in files}
    if len(files) == 1:
        only = next(iter(exts))
        if only in ("zip", "7z", "chd", "rvz"):
            return only
    if exts == {"m3u"} or "chd" in exts:
        return "chd"
    return "loose"


_DISC_MAIN_PRIORITY = {"cue": 0, "gdi": 1, "iso": 2}


def _game_physical_files(target_dir: Path, game, subdir: bool) -> list[Path]:
    """Fizyczne (nie-symlink) pliki gry w kanonicznej lokalizacji."""
    if subdir and len(game.roms) > 1:
        d = target_dir / game.name
        if not d.is_dir():
            return []
        files = [p for p in sorted(d.iterdir()) if p.is_file()]
    else:
        files = [target_dir / r.name for r in game.roms]
        files = [f for f in files if f.is_file()]
    # tylko fizyczne kopie (dzieci-symlinki obsługiwane osobno)
    return [f for f in files if not os.path.islink(f)]


@dataclass
class ConvertStats:
    converted: int = 0
    skipped: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (f"skonwertowano {self.converted}, pominięto {self.skipped}, "
                f"błędy {self.errors}")


def convert_reports(reports, rules_fn, tools: dict, index=None, *,
                    dry_run: bool = False, log: LogCB = lambda m: None,
                    cancel=None, on_progress=None, detail=None,
                    on_converted=None) -> ConvertStats:
    """Konwertuje pliki gier do formatu docelowego z reguł (resolve_format).

    tools: {"chdman": CHDMan|None, "settings": Settings, "dolphintool": Path|None}.
    Konwertuje TYLKO gry z fizycznymi, luźnymi plikami (nie-symlink, nie już
    w formacie docelowym). Weryfikacja w każdej konwersji; źródło kasowane
    dopiero po sukcesie.
    """
    from .dirrules import resolve_format
    st = ConvertStats()
    # ODROCZONE kasowanie źródeł: gry WIELOPŁYTOWE współdzielą ścieżki (np. audio
    # CDDA) — konwersja płyty 1 nie może skasować ścieżki, której potrzebuje
    # płyta 3. Zbieramy wszystkie skonsumowane źródła i kasujemy DOPIERO po
    # przerobieniu WSZYSTKICH gier (współdzielone zostają dostępne do końca).
    deferred: list = []
    deferred_dirs: list = []
    for ri, rep in enumerate(reports):
        if cancel is not None and cancel.is_set():
            log("PRZERWANO konwersję — pliki już przekonwertowane zostają.")
            break
        if on_progress:
            on_progress(ri, len(reports), f"konwersja: {rep.entry.name}")
        eff = rules_fn(rep.entry)
        if eff.get("skip"):
            continue
        fmt = resolve_format(eff.get("format", "keep"), rep.entry)
        if fmt in ("keep", "", "extract"):
            continue
        subdir = bool(eff.get("subdir_per_game", True))
        for game in rep.entry.games:
            if cancel is not None and cancel.is_set():
                break
            files = _game_physical_files(rep.entry.target_dir, game, subdir)
            if not files:
                continue
            cur = current_format(files)
            if cur == fmt:
                continue
            if cur != "loose":
                st.skipped += 1        # np. już chd/zip w innym docelowym — pomiń
                continue
            base = game.name
            if _convert_one(files, rep.entry.target_dir, base, fmt, subdir,
                            len(game.roms), tools, index, dry_run, log, st,
                            detail=detail, on_converted=on_converted,
                            deferred=deferred, deferred_dirs=deferred_dirs):
                st.converted += 1
    # DOPIERO TERAZ kasujemy źródła (wszystkie płyty zestawów już skonwertowane)
    if deferred and not dry_run:
        log(f"Kasuję {len(deferred)} plików źródłowych po konwersji "
            f"(współdzielone ścieżki były dostępne do końca).")
        for f in deferred:
            try:
                os.unlink(f)
                if index is not None:
                    index.remove_path(f)
            except OSError:
                pass
        for d in deferred_dirs:
            try:
                d.rmdir()          # tylko puste — po zabraniu ścieżek
            except OSError:
                pass
    return st


# Ile WOLNEGO miejsca musi być, licząc jako wielokrotność rozmiaru źródła.
# CHD: powstaje nowy plik + round-trip wypakowuje PEŁNY obraz do weryfikacji,
# więc chwilowo trzymamy źródło + CHD + wypakowany obraz.
_FREE_FACTOR = {"chd": 2.2, "rvz": 1.6, "zip": 1.2, "7z": 1.2}

# Artefakty robocze: zostają po przerwanej konwersji/weryfikacji i zajmują dysk.
_TEMP_MARKERS = ("chdbuddy_", "chddeep_", ".rtcheck.")


def _is_temp_artifact(name: str) -> bool:
    low = name.lower()
    return any(m in low for m in _TEMP_MARKERS)


def purge_temp_artifacts(roots, *, dry_run: bool = False,
                         log: LogCB = lambda m: None) -> tuple[int, int]:
    """Kasuje śmieci po PRZERWANYCH konwersjach/weryfikacjach (chdbuddy_*,
    chddeep_*, *.rtcheck.*). Skaner je ignoruje, więc same z siebie nigdy nie
    znikały i zajmowały dysk. Zwraca (ile_pozycji, ile_bajtów)."""
    import shutil as _sh
    n = size = 0
    for root in roots:
        p = Path(root)
        if not p.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(p, topdown=False):
            for fn in filenames:
                if not _is_temp_artifact(fn):
                    continue
                fp = Path(dirpath) / fn
                try:
                    sz = fp.stat().st_size
                except OSError:
                    sz = 0
                log(f"USUWAM śmieć: {fp} ({sz/1024**3:.2f} GB)")
                if not dry_run:
                    try:
                        fp.unlink()
                    except OSError as e:
                        log(f"  nie usunięto: {e}")
                        continue
                n += 1
                size += sz
            for dn in dirnames:
                if not _is_temp_artifact(dn):
                    continue
                dp = Path(dirpath) / dn
                log(f"USUWAM katalog roboczy: {dp}")
                if not dry_run:
                    _sh.rmtree(dp, ignore_errors=True)
                n += 1
    return n, size


def _gather_track_to_ram(status, ram_dir: Path, log: LogCB) -> Optional[Path]:
    """Kopiuje/wypakowuje JEDNĄ ścieżkę gry ze źródła (ToSort) na RAM i
    weryfikuje SHA-1 z DAT-em. Zwraca ścieżkę na RAM albo None (błąd/niezgoda).
    Docelowy katalog kolekcji NIGDY nie jest dotykany."""
    import hashlib  # noqa: F401 (spójność z resztą modułu)
    import shutil as _sh
    rom = status.rom
    dst = ram_dir / rom.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = Path(status.source_path) if status.source_path else None
    if src is None or not src.exists():
        log(f"  źródło ścieżki {rom.name} nie istnieje")
        return None
    try:
        if status.member:                       # ścieżka WEWNĄTRZ archiwum
            with zipfile.ZipFile(src) as zf, zf.open(status.member) as fh, \
                    open(dst, "wb") as out:
                _sh.copyfileobj(fh, out, 4 * 1024 * 1024)
        else:                                   # luźny plik
            with open(src, "rb") as fi, open(dst, "wb") as out:
                _sh.copyfileobj(fi, out, 8 * 1024 * 1024)
    except (OSError, zipfile.BadZipFile, KeyError) as e:
        log(f"  nie zebrano {rom.name}: {e}")
        return None
    try:
        _, _, sha1 = hash_file(dst)
    except OSError:
        return None
    if rom.sha1 and sha1 != rom.sha1.lower():
        log(f"  {rom.name}: SHA-1 NIE zgadza się z DAT-em — pomijam grę")
        return None
    return dst


def convert_from_source(reports, rules_fn, tools: dict, index=None, *,
                        dry_run: bool = False, log: LogCB = lambda m: None,
                        cancel=None, on_progress=None, detail=None,
                        on_converted=None):
    """Konwersja PROSTO ZE ŹRÓDŁA na RAM → w docelowym ląduje TYLKO finał.

    Dla gier, których źródłem są luźne pliki albo ścieżki w archiwum (ToSort),
    i które trzeba przekonwertować (chd/rvz/zip): zbiera ścieżki na RAM,
    weryfikuje SHA-1, kompresuje na RAM, weryfikuje (round-trip/verify) i
    przenosi finał do docelowego. Katalog kolekcji nigdy nie dostaje luźnych
    plików. Źródła kasowane DOPIERO po WSZYSTKICH grach (współdzielone ścieżki
    gier wielopłytowych dostępne do końca — brak D2).

    Zwraca (ConvertStats, done_keys) — done_keys to zbiór kluczy gier
    obsłużonych tutaj; placement MUSI je pominąć. Gry, których funkcja nie
    umie/nie chce ruszyć, zostawia nietknięte (obsłuży zwykły placement +
    stara konwersja) — bezpieczny fallback.
    """
    import shutil as _sh
    import tempfile
    from .dirrules import resolve_format
    from .matcher import RomState
    from .scratch import pick_scratch_root

    st = ConvertStats()
    done: set = set()
    deferred: list = []                       # ORYGINALNE źródła (kasuj po całości)
    n_total = sum(len(r.entry.games) for r in reports) or 1
    gi = 0
    for rep in reports:
        if cancel is not None and cancel.is_set():
            break
        eff = rules_fn(rep.entry)
        if eff.get("skip"):
            continue
        fmt = resolve_format(eff.get("format", "keep"), rep.entry)
        if fmt in ("keep", "", "extract"):
            continue
        subdir = bool(eff.get("subdir_per_game", True))
        by_game: dict = {}
        for s in rep.statuses:
            by_game.setdefault(s.game, []).append(s)
        for game in rep.entry.games:
            if cancel is not None and cancel.is_set():
                break
            gi += 1
            sts = by_game.get(game.name, [])
            if not sts:
                continue
            # WARUNKI (inaczej fallback do placementu):
            #  - żadna ścieżka nie MISSING/NO_HASH (kompletna),
            #  - źródło to luźne pliki/członki archiwum (nie via_chd,
            #    nie via_archive-całej-gry — te obsłuży placement),
            #  - jest co konwertować (nie same HAVE w docelowym formacie).
            if any(s.state in (RomState.MISSING, RomState.NO_HASH) for s in sts):
                continue
            if any(s.via_chd or s.via_archive for s in sts):
                continue
            if all(s.state in (RomState.HAVE, RomState.HAVE_CHD) for s in sts):
                continue
            if on_progress:
                on_progress(gi, n_total, f"konwersja (ze źródła): {game.name}")
            key = f"{id(rep.entry)}::{game.name}"
            if _convert_game_from_source(rep.entry, game, sts, fmt, subdir,
                                         tools, index, dry_run, log, st, detail,
                                         deferred, on_converted):
                done.add(key)

    # NIE kasujemy tu — oryginalne źródła (unikalne) zwracamy, a kasuje je
    # dopiero KONIEC całej naprawy (po placemencie i fallbacku), żeby żadna
    # współdzielona ścieżka nie zniknęła zanim ktoś jej jeszcze potrzebuje.
    uniq = list({os.path.normcase(str(p)): p for p in deferred}.values())
    return st, done, uniq


def purge_source_files(paths, index=None, log: LogCB = lambda m: None,
                       dry_run: bool = False) -> int:
    """Kasuje ORYGINALNE pliki źródłowe skonsumowane przez konwersję ze źródła.
    Wołane na SAMYM KOŃCU naprawy — współdzielone ścieżki gier wielopłytowych
    były dostępne do końca. Zwraca liczbę skasowanych."""
    if not paths or dry_run:
        return 0
    n = 0
    for p in paths:
        try:
            os.unlink(p)
            if index is not None:
                index.remove_path(p)
            n += 1
        except OSError:
            pass
    if n:
        log(f"Skasowano {n} plików źródłowych po konwersji ze źródła "
            f"(współdzielone ścieżki były dostępne do końca).")
    return n


def _convert_game_from_source(entry, game, sts, fmt, subdir, tools, index,
                              dry_run, log, st, detail, deferred,
                              on_converted) -> bool:
    """Jedna gra: zbierz ścieżki na RAM → kompresuj → weryfikuj → finał do
    docelowego. True gdy obsłużona (placement ją pomija)."""
    import shutil as _sh
    import tempfile
    from .scratch import pick_scratch_root

    base = game.name
    target_dir = entry.target_dir
    if fmt == "zip":
        final = target_dir / f"{base}.zip"
    elif fmt == "chd":
        final = target_dir / f"{base}.chd"
    elif fmt == "rvz":
        final = target_dir / f"{base}.rvz"
    else:
        return False

    def _dtl(d, t, txt):
        if detail is not None:
            detail(d, t, txt)

    # status per ROM (po nazwie); pomijamy roms bez statusu
    st_by_rom = {}
    for s in sts:
        st_by_rom.setdefault(s.rom.name, s)
    roms = [r for r in game.roms if r.name in st_by_rom]
    if not roms:
        return False

    try:
        need = int(sum(max(r.size, 0) for r in roms) * (_FREE_FACTOR.get(fmt, 1.5) + 1.0))
    except Exception:
        need = 0
    scratch = pick_scratch_root(
        need, prefer=str(target_dir), log=log,
        fallback=getattr(tools.get("settings"), "scratch_dir", "") or None)
    if scratch is None:
        log(f"POMIJAM (ze źródła) {base}: brak miejsca (~{need/1024**3:.1f} GB)")
        return False
    log(f"KONWERSJA(ze źródła)→{fmt.upper()}: {base}")
    if dry_run:
        st.converted += 1
        # w dry-run tylko zapowiedz — oznacz oryginalne źródła jako „do zabrania"
        for r in roms:
            sp = st_by_rom[r.name].source_path
            if sp:
                deferred.append(Path(sp))
        return True

    work = Path(tempfile.mkdtemp(prefix="chdbuddy_src_", dir=str(scratch)))
    ram_in = work / "in"
    ram_in.mkdir()
    try:
        # 1) ZBIERZ + WERYFIKUJ ścieżki na RAM
        _dtl(0, 0, f"zbieram źródło: {base}")
        gathered = []
        for r in roms:
            g = _gather_track_to_ram(st_by_rom[r.name], ram_in, log)
            if g is None:
                return False                    # niezgodność → fallback placement
            gathered.append(g)

        # 2) KOMPRESUJ na RAM
        tmp_out = work / final.name
        if fmt == "zip":
            _dtl(0, 0, f"pakowanie ZIP: {base}")
            r = pack_zip(gathered, tmp_out, log=log,
                         level=getattr(tools.get("settings"), "zip_level", 6))
        elif fmt == "chd":
            main = min(gathered, key=lambda f: (_DISC_MAIN_PRIORITY.get(
                f.suffix.lower().lstrip("."), 9), f.name.lower()))
            chd = tools.get("chdman")
            if chd is None:
                log("  brak chdman — pomijam"); return False
            _dtl(0, 0, f"kompresja CHD: {base}")
            r = disc_to_chd(main, tmp_out, chd, tools["settings"], log=log,
                            on_progress=lambda pct, msg="": _dtl(
                                int(pct), 100, f"CHD {base}: {msg or f'{int(pct)}%'}"))
        else:  # rvz
            iso = next((f for f in gathered if f.suffix.lower() == ".iso"), None)
            dt = tools.get("dolphintool")
            if iso is None or dt is None:
                log(f"  RVZ: brak iso/DolphinTool — pomijam {base}")
                return False
            _dtl(0, 0, f"kompresja RVZ: {base}")
            r = iso_to_rvz(iso, tmp_out, dt, log=log,
                           level=getattr(tools.get("settings"), "rvz_level", 5),
                           block_kb=getattr(tools.get("settings"), "rvz_block_kb", 128))
        if not r.ok:
            log(f"  BŁĄD konwersji {base}: {r.message}")
            return False
        built = r.dst if (r.dst and Path(r.dst).is_file()) else tmp_out
        if not Path(built).is_file():
            return False

        # 3) FINAŁ do docelowego (docelowy dostaje TYLKO ten plik)
        _dtl(0, 0, f"przenoszę: {final.name}")
        target_dir.mkdir(parents=True, exist_ok=True)
        _place_cross(Path(built), final, detail=detail,
                     label=f"przenoszę {final.name}")
        if index is not None:
            try:
                crc, md5, sha1 = hash_file(final)
                index.record_file(final, crc, md5, sha1)
            except OSError:
                pass
        if on_converted is not None:
            on_converted(final)
        st.converted += 1
        # ORYGINALNE źródła → do skasowania po WSZYSTKICH grach
        for r in roms:
            sp = st_by_rom[r.name].source_path
            if sp:
                deferred.append(Path(sp))
        return True
    finally:
        _dtl(-1, 0, "")
        _sh.rmtree(work, ignore_errors=True)


def _free_bytes(path: Path) -> int:
    import shutil as _sh
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return _sh.disk_usage(str(probe)).free
    except OSError:
        return 0


def _place_cross(new: Path, dst: Path, detail=None, label: str = "") -> None:
    """Przenosi gotowy plik na miejsce: os.replace (ten sam dysk, atomowo)
    albo kopia blokami z postępem (między dyskami — RAM → fizyczny)."""
    from .fileops import move_with_progress
    dst.unlink(missing_ok=True)
    move_with_progress(new, dst, on_progress=detail, label=label)


def _convert_one(files, target_dir, base, fmt, subdir, n_roms, tools, index,
                 dry_run, log, st, detail=None, on_converted=None,
                 deferred=None, deferred_dirs=None) -> bool:
    import shutil as _sh
    import tempfile
    from .scratch import pick_scratch_root
    _settings = tools.get("settings")
    dst_dir = (target_dir / base if subdir and n_roms > 1 else target_dir)
    # docelowa ścieżka finalnego pliku
    if fmt == "zip":
        final = (dst_dir.parent if subdir and n_roms > 1 else target_dir) \
            / f"{base}.zip"
    elif fmt == "chd":
        final = target_dir / f"{base}.chd"
    elif fmt == "rvz":
        final = target_dir / f"{base}.rvz"
    else:
        return False
    log(f"KONWERSJA→{fmt.upper()}: {base}")
    if dry_run:
        return True

    # BUDUJEMY na SCRATCH (RAM, gdy się mieści) — nie na dysku docelowym.
    # Dzięki temu D: nie potrzebuje miejsca na (źródło + nowy plik) naraz:
    # po weryfikacji KASUJEMY źródło (zwalnia D:), potem przenosimy gotowy
    # plik na miejsce. Zajętość dysku kolekcji nie rośnie.
    try:
        need = int(sum(f.stat().st_size for f in files)
                   * _FREE_FACTOR.get(fmt, 1.5))
    except OSError:
        need = 0
    scratch = pick_scratch_root(
        need, prefer=str(target_dir), log=log,
        fallback=getattr(_settings, "scratch_dir", "") or None)
    if scratch is None:
        log(f"POMIJAM {base}: brak miejsca (RAM/dysk) na ~{need/1024**3:.1f} GB")
        st.skipped += 1
        return False
    work = Path(tempfile.mkdtemp(prefix="chdbuddy_conv_", dir=str(scratch)))
    tmp_out = work / final.name

    def _dtl(done: int, total: int, text: str) -> None:
        if detail is not None:
            detail(done, total, text)

    try:
        if fmt == "zip":
            _dtl(0, 0, f"pakowanie ZIP: {base}")     # szybkie — pasek pulsuje
            r = pack_zip(files, tmp_out, log=log,
                         level=getattr(_settings, "zip_level", 6))
        elif fmt == "chd":
            main = min(files, key=lambda f: (_DISC_MAIN_PRIORITY.get(
                f.suffix.lower().lstrip("."), 9), f.name.lower()))
            chd = tools.get("chdman")
            if chd is None:
                log("  brak chdman — pomijam"); st.errors += 1; return False
            log(f"  (z {main.name})")
            _dtl(0, 0, f"kompresja CHD: {base}")
            r = disc_to_chd(
                main, tmp_out, chd, tools["settings"], log=log,
                on_progress=lambda pct, msg="": _dtl(
                    int(pct), 100, f"CHD {base}: {msg or f'{int(pct)}%'}"))
        else:  # rvz
            iso = next((f for f in files if f.suffix.lower() == ".iso"), None)
            dt = tools.get("dolphintool")
            if iso is None or dt is None:
                log(f"  RVZ: brak iso/DolphinTool — pomijam {base}")
                st.errors += 1; return False
            _dtl(0, 0, f"kompresja RVZ: {base}")      # DolphinTool bez % — pulsuje
            r = iso_to_rvz(iso, tmp_out, dt, log=log,
                           level=getattr(_settings, "rvz_level", 5),
                           block_kb=getattr(_settings, "rvz_block_kb", 128))

        if not r.ok:
            log(f"  BŁĄD konwersji {base}: {r.message}")
            st.errors += 1
            return False
        # r.dst może wskazywać plik pod inną nazwą (fixer stem) — znormalizuj
        built = r.dst if (r.dst and Path(r.dst).is_file()) else tmp_out
        if not Path(built).is_file():
            log(f"  BŁĄD: brak pliku wyjściowego {base}"); st.errors += 1
            return False
        # gotowy plik ZWERYFIKOWANY (round-trip/verify). SHA-1 zawartości
        # źródła (do indeksu) POBIERAMY PRZED skasowaniem źródła.
        data_sha1 = ""
        if fmt == "chd" and index is not None:
            try:
                row = index.lookup(min(files, key=lambda f: (
                    _DISC_MAIN_PRIORITY.get(f.suffix.lower().lstrip("."), 9),
                    f.name.lower())))
                if row is not None:
                    data_sha1 = row["sha1"] or ""
            except Exception:
                data_sha1 = ""
        # 1) UMIEŚĆ gotowy plik na miejscu (RAM/scratch → D:, cross-drive).
        #    Źródła NIE kasujemy tu od razu — patrz niżej: przy grach
        #    WIELOPŁYTOWYCH współdzielona ścieżka musi zostać dostępna dla
        #    kolejnych płyt zestawu; kasujemy DOPIERO po całej konwersji.
        _dtl(0, 0, f"przenoszę: {final.name}")
        _place_cross(Path(built), final, detail=detail,
                     label=f"przenoszę {final.name}")
        # 2) źródło: odroczone (kasuje convert_reports po WSZYSTKICH grach)
        #    albo natychmiast (gdy wołane bez listy odroczeń).
        if deferred is not None:
            deferred.extend(Path(f) for f in files)
            if subdir and n_roms > 1 and deferred_dirs is not None:
                deferred_dirs.append(target_dir / base)
        else:
            for f in files:
                try:
                    os.unlink(f)
                    if index is not None:
                        index.remove_path(f)
                except OSError:
                    pass
            if subdir and n_roms > 1:
                try:
                    (target_dir / base).rmdir()
                except OSError:
                    pass
        if index is not None:
            try:
                crc, md5, sha1 = hash_file(final)
                index.record_file(final, crc, md5, sha1)
                if data_sha1:
                    index.set_data_sha1(final, data_sha1)
            except OSError:
                pass
        # zgłoś NOWĄ ścieżkę kanoniczną — inaczej faza sprzątania uznałaby
        # świeży CHD/RVZ/ZIP za nieznany i wrzuciła go do ToSort
        if on_converted is not None:
            on_converted(final)
        return True
    finally:
        _dtl(-1, 0, "")          # schowaj pasek szczegółowy po tym pliku
        _sh.rmtree(work, ignore_errors=True)



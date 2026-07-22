"""Odbudowa kanonicznych CHD z prawidłowym cue (biblioteka Redump Cuesheets).

Problem: CHD utworzone ze złym/bez cue ma SKLEJONY układ ścieżek (metadane
mówią „1 ścieżka", a gra ma ich N — dane + audio CDDA). Zawartość bywa
poprawna (tniemy wg rozmiarów z DAT-a i sumy się zgadzają), ale kontener
jest niekanoniczny.

Odbudowa per gra:
1. wykrycie: liczba ścieżek CD w metadanych CHD != liczba ścieżek w DAT;
2. ekstrakcja ``extractcd -sb``; jeden sklejony bin => cięcie wg ROZMIARÓW
   z DAT-a; KAŻDA ścieżka weryfikowana SHA-1 z DAT-em (złe dane => stop);
3. ścieżki pod nazwami z DAT-a + cue z biblioteki (sha1 cue też z DAT-a);
4. ``chdman createcd -i <cue>`` + round-trip verify (fixer);
5. dopiero po sukcesie podmiana starego pliku i aktualizacja indeksu.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .cuelib import CueLibrary
from .models import CD_METADATA_TAGS, MediaType

LogCB = Callable[[str], None]


@dataclass
class RebuildChdStats:
    checked: int = 0
    ok_layout: int = 0        # układ ścieżek już zgodny z DAT-em
    rebuilt: int = 0
    no_cue: int = 0           # brak cue w bibliotece
    verify_failed: int = 0    # ścieżki nie przeszły sum z DAT-em
    skipped_space: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (f"sprawdzono {self.checked}, układ OK {self.ok_layout}, "
                f"ODBUDOWANO {self.rebuilt}, brak cue {self.no_cue}, "
                f"weryfikacja nieudana {self.verify_failed}, "
                f"pominięte (miejsce) {self.skipped_space}, "
                f"błędy {self.errors}")


def _scratch_tmp(chd_path: Path, need: int, log: LogCB, fallback=None):
    """Katalog roboczy: RAM dysk (gdy się mieści) → temp z ustawień (fallback)
    → dysk z zapasem. None => nigdzie nie ma miejsca. Odbudowa: ekstrakcja+nowy
    CHD idą tam, finalny plik przenosimy na miejsce (cross-drive safe)."""
    from .scratch import pick_scratch_root
    root = pick_scratch_root(need, prefer=str(chd_path.parent), log=log,
                             fallback=fallback)
    if root is None:
        return None
    return Path(tempfile.mkdtemp(prefix="chdbuddy_rebuild_", dir=str(root)))


def _mk_detail(detail, name: str):
    """Adapter: chdman woła on_progress(pct, msg); pasek UI bierze
    (done, total, text). Zwraca callback dla chdman albo None."""
    if detail is None:
        return None

    def dp(pct: float, msg: str = "") -> None:
        if pct is not None and pct >= 0:
            detail(int(pct), 100, f"CHD {name}: {msg or f'{int(pct)}%'}")
        else:
            detail(0, 0, f"CHD {name}: {msg}".rstrip(": "))
    return dp


def _place_final(new: Path, dst: Path, detail=None) -> None:
    """Przenosi gotowy plik na miejsce; os.replace na tym samym dysku
    (atomowo), kopia blokami z postępem między dyskami (RAM → fizyczny)."""
    from .fileops import move_with_progress
    dst.unlink(missing_ok=True)
    move_with_progress(new, dst, on_progress=detail,
                       label=f"przenoszę {dst.name}")


def _chd_cd_tracks(chd, path: Path) -> int:
    """Liczba ścieżek CD w metadanych CHD (0 gdy info nieczytelne)."""
    try:
        info = chd.info(path)
    except OSError:
        return 0
    return sum(1 for t in info.metadata_tags if t in CD_METADATA_TAGS)


def _write_split_by_dat(src_bin: Path, tracks, out_dir: Path,
                        log: LogCB) -> Optional[list]:
    """Tnie sklejony bin wg rozmiarów z DAT-a, weryfikując sha1 KAŻDEJ
    ścieżki w locie. Zwraca listę plików albo None (niezgodność)."""
    out = []
    with open(src_bin, "rb") as fh:
        for rom in tracks:
            h = hashlib.sha1()
            dst = out_dir / rom.name
            left = rom.size
            with open(dst, "wb") as o:
                while left > 0:
                    chunk = fh.read(min(1 << 22, left))
                    if not chunk:
                        break
                    h.update(chunk)
                    o.write(chunk)
                    left -= len(chunk)
            if left != 0 or h.hexdigest() != rom.sha1.lower():
                log(f"   ścieżka {rom.name}: suma NIE zgadza się z DAT-em")
                return None
            out.append(dst)
    return out


def rebuild_bad_chds(
    entries: Sequence,
    lib: CueLibrary,
    chd,                                   # CHDMan
    settings,
    index=None,
    *,
    extra_roots: Sequence = (),
    dry_run: bool = False,
    log: LogCB = lambda m: None,
    on_progress=None,
    detail=None,
    cancel=None,
) -> RebuildChdStats:
    """Odbudowuje CHD o złym układzie ścieżek:

    1. kanoniczne pliki ``<gra>.chd`` w katalogach docelowych `entries`;
    2. ZIDENTYFIKOWANE CHD leżące jeszcze w `extra_roots` (np. ToSort) —
       rozpoznane po odcisku kompletu (data_sha1); odbudowa W MIEJSCU,
       przenosiny/nazwę załatwia potem naprawa.
    """
    from . import presets
    st = RebuildChdStats()
    comp = presets.compression_for(settings.compression_preset, MediaType.CD)
    comp_dvd = presets.compression_for(settings.compression_preset,
                                       MediaType.DVD)
    # mapa odcisk gry -> ("cd", gra multi-track z cue) albo ("dvd", gra .iso)
    # — do rozpoznania plików w ToSort i naprawy kontenera
    from .datfile import game_profile
    by_prof: dict = {}
    for entry in entries:
        entry.load()
        for game in entry.games:
            data = game.data_roms
            cue_rom = next((r for r in game.roms
                            if r.name.lower().endswith(".cue")), None)
            if (len(data) == 1 and game.media == MediaType.DVD
                    and data[0].sha1):
                by_prof.setdefault(game_profile(data),
                                   ("dvd", game, data, None))
            elif len(data) >= 2 and cue_rom is not None and cue_rom.sha1:
                by_prof.setdefault(game_profile(data),
                                   ("cd", game, data, cue_rom))

    def _cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    for ei, entry in enumerate(entries):
        if _cancelled():
            break
        for game in entry.games:
            if _cancelled():
                break
            data = game.data_roms
            hit = by_prof.get(game_profile(data)) if data else None
            if hit is None:
                continue
            chd_path = Path(entry.target_dir) / f"{game.name}.chd"
            if not chd_path.is_file():
                continue
            if on_progress:
                on_progress(ei, len(entries), f"CHD wg cue: {game.name}")
            kind, _g, data, cue_rom = hit
            if kind == "dvd":
                _rebuild_dvd_one(chd_path, game.name, data[0], chd, settings,
                                 comp_dvd, index, st, dry_run, log, cancel,
                                 detail)
            else:
                _rebuild_one(chd_path, game.name, data, cue_rom, lib, chd,
                             settings, comp, index, st, dry_run, log, cancel,
                             detail)

    # zidentyfikowane CHD wciąż w ToSort — kontener naprawiamy w miejscu
    for root in (extra_roots or []):
        if _cancelled() or index is None:
            break
        for row in index.all_under(root):
            if _cancelled():
                break
            p = row["path"]
            if not p.lower().endswith(".chd") or not row["data_sha1"]:
                continue
            hit = by_prof.get(row["data_sha1"])
            if hit is None:
                continue
            kind, game, data, cue_rom = hit
            chd_path = Path(p)
            if not chd_path.is_file():
                continue
            if on_progress:
                on_progress(0, 0, f"CHD wg cue (ToSort): {chd_path.name}")
            if kind == "dvd":
                _rebuild_dvd_one(chd_path, game.name, data[0], chd, settings,
                                 comp_dvd, index, st, dry_run, log, cancel,
                                 detail)
            else:
                _rebuild_one(chd_path, game.name, data, cue_rom, lib, chd,
                             settings, comp, index, st, dry_run, log, cancel,
                             detail)
    return st


def _rebuild_dvd_one(chd_path: Path, game_name: str, iso_rom, chd, settings,
                     comp_dvd, index, st: RebuildChdStats, dry_run: bool,
                     log: LogCB, cancel, detail=None) -> None:
    """Gra DVD (PS2: pojedynczy .iso) spakowana JAKO CD (createcd) —
    przepakowanie kontenera na createdvd: extractcd → deframe 2352→2048 →
    weryfikacja SHA-1 obrazu z DAT-em → createdvd + round-trip → podmiana."""
    from . import fixer, imageops
    st.checked += 1
    try:
        info = chd.info(chd_path)
    except OSError:
        st.errors += 1
        return
    if not info.is_cd_typed:
        st.ok_layout += 1               # już DVD — kontener poprawny
        return
    log(f"ODBUDOWA CD→DVD: {chd_path.name} [{game_name}]")
    if dry_run:
        st.rebuilt += 1
        return
    need = int(chd_path.stat().st_size * 3.2)
    tmp = _scratch_tmp(chd_path, need, log,
                       fallback=getattr(settings, "scratch_dir", "") or None)
    if tmp is None:
        st.skipped_space += 1
        log(f"   POMIJAM: brak miejsca (RAM/dysk) na ~{need/1024**3:.1f} GB")
        return
    try:
        dp = _mk_detail(detail, chd_path.name)
        raw = tmp / (game_name + ".cue")
        res = chd.extract("extractcd", chd_path, raw, cancel_event=cancel,
                          on_progress=dp)
        if not res.ok:
            st.errors += 1
            log("   ekstrakcja nieudana")
            return
        try:
            cue = imageops.parse_cue(raw)
        except Exception as e:
            st.errors += 1
            log(f"   cue nieczytelne: {e}")
            return
        if cue.bin_path is None or not cue.bin_path.is_file():
            st.errors += 1
            log("   brak .bin po ekstrakcji")
            return
        split = tmp / "split"
        split.mkdir(exist_ok=True)
        iso = split / iso_rom.name
        imageops.bin_to_iso(cue.bin_path, cue.sector_size, iso)
        got = hashlib.sha1(iso.read_bytes()).hexdigest()
        if got != iso_rom.sha1.lower():
            st.verify_failed += 1
            log("   SHA-1 obrazu po deframe nie zgadza się z DAT-em — pomijam")
            return
        out = fixer.create_from_source(
            chd, iso, MediaType.DVD, split, settings,
            compression=comp_dvd, log=lambda m: log(f"   {m}"),
            cancel_event=cancel, on_progress=dp)
        if not out.ok:
            st.errors += 1
            log(f"   createdvd/verify: {out.message}")
            return
        new_chd = split / (iso.stem + ".chd")
        if not new_chd.is_file():
            st.errors += 1
            return
        _place_final(new_chd, chd_path, detail)
        st.rebuilt += 1
        log(f"   ✔ kontener CD→DVD podmieniony: {chd_path.name}")
        if index is not None:
            from .fileindex import hash_file
            crc, md5, sha1 = hash_file(chd_path)
            index.record_file(chd_path, crc, md5, sha1)
            index.set_data_sha1(chd_path, iso_rom.sha1.lower())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rebuild_one(chd_path: Path, game_name: str, data, cue_rom, lib, chd,
                 settings, comp, index, st: RebuildChdStats, dry_run: bool,
                 log: LogCB, cancel, detail=None) -> None:
    """Odbudowa JEDNEGO pliku CHD do kanonicznego kontenera (per gra)."""
    from . import fixer
    st.checked += 1
    n_chd = _chd_cd_tracks(chd, chd_path)
    if n_chd == len(data):
        st.ok_layout += 1
        return                              # kontener już kanoniczny
    cue_bytes = lib.load(cue_rom.sha1)
    if cue_bytes is None:
        st.no_cue += 1
        log(f"BRAK CUE w bibliotece: {game_name}")
        return
    log(f"ODBUDOWA (ścieżki w CHD: {n_chd}, w DAT: {len(data)}): "
        f"{chd_path.name} [{game_name}]")
    if dry_run:
        st.rebuilt += 1
        return
    need = int(chd_path.stat().st_size * 3.2)   # obraz+ścieżki+nowy CHD
    tmp = _scratch_tmp(chd_path, need, log,
                       fallback=getattr(settings, "scratch_dir", "") or None)
    if tmp is None:
        st.skipped_space += 1
        log(f"   POMIJAM: brak miejsca (RAM/dysk) na ~{need/1024**3:.1f} GB")
        return
    try:
        dp = _mk_detail(detail, chd_path.name)
        raw = tmp / (game_name + ".cue")
        res = chd.extract("extractcd", chd_path, raw,
                          cancel_event=cancel, extra_args=["-sb"],
                          on_progress=dp)
        if not res.ok:
            res = chd.extract("extractcd", chd_path, raw,
                              cancel_event=cancel, on_progress=dp)
        if not res.ok:
            st.errors += 1
            log("   ekstrakcja nieudana")
            return
        bins = sorted(p for p in tmp.iterdir()
                      if p.suffix.lower() == ".bin")
        # OSOBNY podkatalog na finalne ścieżki: nazwa z -sb potrafi
        # być IDENTYCZNA z nazwą z DAT-a — zapis do tego samego
        # pliku, który czytamy, obcina źródło w trakcie czytania.
        split = tmp / "split"
        split.mkdir(exist_ok=True)
        if len(bins) == 1:
            tracks = _write_split_by_dat(bins[0], data, split, log)
        elif len(bins) == len(data):
            tracks = []
            for b, rom in zip(bins, data):
                h = hashlib.sha1(b.read_bytes()).hexdigest()
                if h != rom.sha1.lower():
                    tracks = None
                    log(f"   {b.name}: suma nie zgadza się z DAT-em")
                    break
                t = split / rom.name
                os.replace(b, t)
                tracks.append(t)
        else:
            tracks = None
            log(f"   dziwny podział ({len(bins)} bin vs "
                f"{len(data)} w DAT) — pomijam")
        if not tracks:
            st.verify_failed += 1
            return
        cue_path = split / cue_rom.name
        cue_path.write_bytes(cue_bytes)
        out = fixer.create_from_source(
            chd, cue_path, MediaType.CD, split, settings,
            compression=comp, log=lambda m: log(f"   {m}"),
            cancel_event=cancel, on_progress=dp)
        if not out.ok:
            st.errors += 1
            log(f"   createcd/verify: {out.message}")
            return
        new_chd = split / (cue_path.stem + ".chd")
        if not new_chd.is_file():
            st.errors += 1
            return
        _place_final(new_chd, chd_path, detail)
        st.rebuilt += 1
        log(f"   ✔ kanoniczny CHD podmieniony: {chd_path.name}")
        if index is not None:
            from .datfile import game_profile
            from .fileindex import hash_file
            crc, md5, sha1 = hash_file(chd_path)
            index.record_file(chd_path, crc, md5, sha1)
            # odcisk KOMPLETU ścieżek (nie pojedynczej — 1S vs 5S!)
            index.set_data_sha1(chd_path, game_profile(data))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

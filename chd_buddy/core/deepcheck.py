"""Głęboka identyfikacja pojedynczego CHD względem DAT.

Dla trudnych/niejednoznacznych plików (np. zrobionych createraw/createhd, albo
o błędnie zapisanym typie) próbujemy KOLEJNO różnych metod wypakowania i po
każdej sprawdzamy SHA-1 wyników w DAT. Zwracamy pierwszą metodę, której wynik
odpowiada znanemu zrzutowi Redump/No-Intro.

Strategie (w kolejności prób):
  1. extractdvd            -> surowy obraz DVD (.iso)
  2. extractcd + deframe   -> ścieżka danych 2352->2048 (DVD spakowane jako CD)
  3. extractcd (surowe)    -> surowe ścieżki bin (zwykłe CD, hashe per track)
  4. extracthd / extractraw / extractld -> pozostałe typy nośników

Każda strategia pracuje we własnym katalogu tymczasowym, który jest kasowany
po zhashowaniu — na dysku leży najwyżej jeden zestaw wyników naraz (low-disk).
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import imageops, roundtrip
from .chdman import CHDMan
from .models import MediaType

ProgressCB = Callable[[float, str], None]
LogCB = Callable[[str], None]


def _noop_p(_p: float, _m: str) -> None:  # pragma: no cover
    pass


def _noop_l(_m: str) -> None:  # pragma: no cover
    pass


@dataclass
class DeepResult:
    ok: bool
    method: str = ""
    game: str = ""
    media: Optional[MediaType] = None
    rom_name: str = ""
    sha1: str = ""
    tried: List[str] = field(default_factory=list)


def _track_no(p: Path) -> tuple:
    """Klucz sortowania ścieżek: numer z '(Track N)' liczbowo (2 < 10)."""
    import re
    m = re.search(r"track\s*(\d+)", p.name, re.IGNORECASE)
    return (0, int(m.group(1))) if m else (1, p.name.lower())


def _hash_and_match(files: List[Path], dat_index, method: str,
                    log: LogCB) -> Optional[DeepResult]:
    """Hashuje pliki danych i porównuje ODCISK KOMPLETU z DAT-em.

    Gra jest tożsama dopiero WSZYSTKIMI ścieżkami: wydania (1S/5S) potrafią
    dzielić ścieżkę danych i różnić się tylko audio — pojedyncza suma dawała
    fałszywe dopasowania (link 5S→1S). Odcisk = game_profile(lista sum w
    kolejności ścieżek)."""
    import hashlib as _h
    data = sorted((f for f in files
                   if f.is_file()
                   and f.suffix.lower() not in (".cue", ".gdi")),
                  key=_track_no)
    if not data:
        return None
    sums = [roundtrip.sha1_file(f) for f in data]
    profile = sums[0] if len(sums) == 1 else _h.sha1(
        ",".join(s.lower() for s in sums).encode("ascii")).hexdigest()
    m = dat_index.match_profile(profile)
    if m is not None:
        log(f"  ✔ {method}: KOMPLET {len(sums)} ścieżek == DAT "
            f"'{m.game}' ({m.media.value}).")
        return DeepResult(True, method=method, game=m.game, media=m.media,
                          rom_name=m.rom_name, sha1=profile)
    return None


def _list_data_files(d: Path) -> List[Path]:
    return sorted(p for p in d.iterdir() if p.is_file())


_CUE_FILE_RE = None   # leniwe kompilowanie regexów cue


def _split_cue_tracks(cue_path: Path, log: LogCB) -> List[Path]:
    """Tnie sklejony .bin z chdman extractcd na PLIKI PER ŚCIEŻKA wg cue.

    Redump hashuje każdą ścieżkę OSOBNO (Track 1.bin, Track 2.bin…), a chdman
    wypakowuje jeden ciągły .bin — sklejony plik nigdy nie trafi w DAT przy
    płycie wielościeżkowej (dane + audio CDDA). Zwraca listę plików-ścieżek
    w kolejności (dane pierwsze), do zhashowania per plik.
    Granica ścieżki = jej PIERWSZY INDEX (00 jeśli jest, inaczej 01) — tak
    jak dzieli Redump (pregap na początku pliku ścieżki)."""
    import re
    text = cue_path.read_text(encoding="utf-8", errors="replace")
    files = re.findall(r'FILE\s+"([^"]+)"', text, re.IGNORECASE)
    if len(files) > 1:
        # cue już per-ścieżkowe (nowsze chdman --splitbin) — użyj wprost
        return [cue_path.parent / f for f in files]
    if not files:
        return []
    bin_path = cue_path.parent / files[0]
    if not bin_path.is_file():
        return []

    def _msf_frames(msf: str) -> int:
        m, s, f = (int(x) for x in msf.split(":"))
        return (m * 60 + s) * 75 + f

    # pierwsza (najniższa) INDEX każdej ścieżki = początek jej pliku
    starts: list[int] = []
    cur_first: int | None = None
    for line in text.splitlines():
        t = line.strip()
        if t.upper().startswith("TRACK"):
            if cur_first is not None:
                starts.append(cur_first)
            cur_first = None
        elif t.upper().startswith("INDEX"):
            parts = t.split()
            if len(parts) >= 3:
                fr = _msf_frames(parts[2])
                if cur_first is None or fr < cur_first:
                    cur_first = fr
    if cur_first is not None:
        starts.append(cur_first)
    if len(starts) <= 1:
        return [bin_path]              # jedna ścieżka — cały bin to Track 1

    total = bin_path.stat().st_size
    sector = 2352                       # extractcd pisze surowe 2352 B/sektor
    bounds = [s * sector for s in starts] + [total]
    out: List[Path] = []
    with open(bin_path, "rb") as src:
        for i in range(len(starts)):
            length = bounds[i + 1] - bounds[i]
            if length <= 0:
                continue
            tp = cue_path.parent / f"{cue_path.stem} (Track {i + 1}).bin"
            src.seek(bounds[i])
            with open(tp, "wb") as dst:
                left = length
                while left > 0:
                    chunk = src.read(min(1 << 22, left))
                    if not chunk:
                        break
                    dst.write(chunk)
                    left -= len(chunk)
            out.append(tp)
    log(f"  podzielono na {len(out)} ścieżek (multi-track, jak Redump)")
    return out


def _match_by_dat_sizes(bin_path: Path, dat_index, log: LogCB
                        ) -> Optional[DeepResult]:
    """Ostatnia deska ratunku dla SKLEJONEGO obrazu: tnie go wg ROZMIARÓW
    ścieżek z DAT-a (kandydaci o identycznej sumie) i weryfikuje sha1 KAŻDEJ
    ścieżki. Ratuje CHD utworzone ze złym/bez cue (układ ścieżek przepadł,
    --splitbin daje jeden bin). Wymaga zgodności wszystkich ścieżek."""
    import hashlib
    try:
        total = bin_path.stat().st_size
    except OSError:
        return None
    profiles = getattr(dat_index, "size_profiles", {})
    # chdman dopełnia ścieżkę do 4 ramek — sklejony obraz bywa o 1–3 sektory
    # WIĘKSZY niż suma z DAT-a; ogon paddingu ignorujemy przy dopasowaniu
    _PAD = 3 * 2352
    cands = []
    for t, games in profiles.items():
        if t <= total <= t + _PAD:
            cands.extend(games)
    if not cands:
        return None
    log(f"  suma rozmiarów pasuje do {len(cands)} gier z DAT — tnę wg DAT-a…")
    for game_name, tracks in cands:
        ok = True
        got_sums: list = []
        with open(bin_path, "rb") as fh:
            for size, want in tracks:
                h = hashlib.sha1()
                left = size
                while left > 0:
                    chunk = fh.read(min(1 << 22, left))
                    if not chunk:
                        ok = False
                        break
                    h.update(chunk)
                    left -= len(chunk)
                got = h.hexdigest()
                got_sums.append(got)
                if not ok or got != want:
                    ok = False
                    break
        if ok:
            profile = got_sums[0] if len(got_sums) == 1 else hashlib.sha1(
                ",".join(got_sums).encode("ascii")).hexdigest()
            m = dat_index.match_profile(profile)
            log(f"  ✔ podział wg DAT: WSZYSTKIE ścieżki zgodne z '{game_name}'.")
            return DeepResult(True, method="podział wg rozmiarów z DAT",
                              game=game_name,
                              media=m.media if m else None,
                              rom_name=m.rom_name if m else "",
                              sha1=profile)
    log("  podział wg DAT: żaden kandydat nie przeszedł pełnej weryfikacji.")
    return None


def deep_identify(
    chd: CHDMan,
    path: Path,
    dat_index,
    work_dir: Path,
    on_progress: ProgressCB = _noop_p,
    log: LogCB = _noop_l,
    cancel_event: Optional[threading.Event] = None,
    chd_info=None,
) -> DeepResult:
    """Próbuje metod ekstrakcji aż wynik zwaliduje się w DAT.

    chd_info (CHDInfo z chdman info) pozwala pominąć metody bez sensu dla
    typu kontenera: CHD typu CD (createcd — np. cała PlayStation 1) NIE
    przechodzi ścieżki extractdvd, a CHD typu DVD nie przechodzi ścieżek CD.
    Każda pominięta metoda to jedna pełna ekstrakcja mniej."""
    tried: List[str] = []

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # (nazwa, polecenie ekstrakcji, rozszerzenie -o, czy robić deframe)
    s_dvd = ("extractdvd", "extractdvd", ".iso", False)
    s_deframe = ("extractcd + deframe 2048", "extractcd", ".cue", True)
    s_cdraw = ("extractcd (surowe ścieżki)", "extractcd", ".cue", False)
    s_rest = [("extracthd", "extracthd", ".raw", False),
              ("extractraw", "extractraw", ".raw", False),
              ("extractld", "extractld", ".raw", False)]
    if chd_info is None:
        try:
            chd_info = chd.info(path)
        except Exception:                       # brak info => pełna lista
            chd_info = None
    if chd_info is not None and chd_info.is_cd_typed:
        # kontener CD: surowe ścieżki (zwykłe CD — PS1/Saturn/DC) najpierw,
        # potem deframe 2048 (DVD spakowane jako CD); extractdvd BEZ SENSU.
        strategies = [s_cdraw, s_deframe] + s_rest
        log("Kontener: CD (createcd) — pomijam extractdvd.")
    elif chd_info is not None and getattr(chd_info, "unit_bytes", 0) == 2048:
        # kontener DVD: tylko ścieżka DVD + awaryjne; ścieżki CD bez sensu.
        strategies = [s_dvd] + s_rest
        log("Kontener: DVD (createdvd) — pomijam ścieżki CD.")
    else:
        strategies = [s_dvd, s_deframe, s_cdraw] + s_rest

    for name, cmd, ext, deframe in strategies:
        if _cancelled():
            log("Przerwano.")
            break
        tried.append(name)
        log(f"Próba: {name}…")
        tmp = Path(tempfile.mkdtemp(prefix="chddeep_", dir=str(work_dir)))
        try:
            out = tmp / (path.stem + ext)
            split_native = False
            if cmd == "extractcd" and not deframe:
                # chdman >= 0.264: --splitbin pisze osobny .bin PER ŚCIEŻKA —
                # dokładnie jak dzieli Redump. Starszy chdman: fallback niżej
                # (własne cięcie po cue).
                res = chd.extract(cmd, path, out, on_progress=on_progress,
                                  cancel_event=cancel_event,
                                  extra_args=["-sb"])
                split_native = res.ok
                if not res.ok:
                    res = chd.extract(cmd, path, out, on_progress=on_progress,
                                      cancel_event=cancel_event)
            else:
                res = chd.extract(cmd, path, out, on_progress=on_progress,
                                  cancel_event=cancel_event)
            if not res.ok:
                log(f"  {name}: ekstrakcja nieudana (kod {res.returncode}), dalej.")
                continue

            if deframe:
                # extractcd -> cue + bin; deframe każdej ścieżki danych do 2048.
                try:
                    cue = imageops.parse_cue(out)
                except Exception as e:  # noqa: BLE001
                    log(f"  {name}: nie sparsowano cue ({e}), dalej.")
                    continue
                if cue.bin_path is None or not cue.bin_path.exists():
                    log(f"  {name}: brak .bin po ekstrakcji, dalej.")
                    continue
                iso = tmp / (path.stem + ".deframed.iso")
                imageops.bin_to_iso(cue.bin_path, cue.sector_size, iso)
                hit = _hash_and_match([iso], dat_index, name, log)
            elif cmd == "extractcd":
                # surowe ścieżki: Redump hashuje każdą ścieżkę OSOBNO.
                # --splitbin dał już osobne biny; starszy chdman => tniemy
                # sklejony .bin po cue sami.
                if split_native:
                    tracks = _list_data_files(tmp)
                    if len(tracks) > 2:      # cue + >1 bin = multi-track
                        log(f"  {len(tracks) - 1} ścieżek (--splitbin)")
                else:
                    tracks = _split_cue_tracks(out, log)
                hit = _hash_and_match(tracks or _list_data_files(tmp),
                                      dat_index, name, log)
                if hit is None:
                    # jeden sklejony bin (CHD ze złym/bez cue): potnij wg
                    # ROZMIARÓW z DAT-a i zweryfikuj wszystkie ścieżki
                    bins = [t for t in (tracks or [])
                            if t.suffix.lower() == ".bin"]
                    if len(bins) == 1:
                        hit = _match_by_dat_sizes(bins[0], dat_index, log)
            else:
                hit = _hash_and_match(_list_data_files(tmp), dat_index, name, log)

            if hit is not None:
                hit.tried = tried
                return hit
            log(f"  {name}: wypakowano, brak dopasowania w DAT.")
        except Exception as e:  # noqa: BLE001
            log(f"  {name}: błąd ({e}), dalej.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return DeepResult(False, tried=tried)

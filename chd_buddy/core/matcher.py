"""Matcher: indeks plików × DAT-y => stan kolekcji (jak drzewo RomVaulta).

Dla każdego ROM-a z każdego DAT-a ustala status na podstawie indeksu
(bez czytania plików — wszystko z bazy):

- HAVE          — plik leży pod kanoniczną ścieżką (target_dir/nazwa z DAT-a);
- HAVE_CHD      — pod kanoniczną ścieżką (z rozszerzeniem .chd) leży CHD,
                  którego SHA-1 ZAWARTOŚCI zgadza się z DAT-em;
- WRONG_NAME    — właściwe dane są w katalogu DAT-a, ale pod złą nazwą;
- ELSEWHERE     — właściwe dane istnieją, ale w innym katalogu (źródło/ToSort
                  /inny DAT) — rebuilder przeniesie albo podlinkuje;
- MISSING       — brak trafienia w indeksie;
- NO_HASH       — wpis DAT-a nie ma żadnego hasha (nie da się dopasować).

Dopasowanie: SHA-1 -> MD5 -> (CRC32 + rozmiar). Trafienie może też paść
WEWNĄTRZ archiwum ZIP (member != "") — rebuilder wtedy wypakowuje
z weryfikacją SHA-1. Luźne pliki mają pierwszeństwo przed archiwami.
Dla plików .chd liczy się także data_sha1 (zawartość) — tak CHD DVD/HD
trafia w Redump bez ekstrakcji.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

from .datfile import DatRom
from .datstore import DatEntry
from .fileindex import FileIndex


class RomState(Enum):
    HAVE = "have"
    HAVE_CHD = "have_chd"
    WRONG_NAME = "wrong_name"
    ELSEWHERE = "elsewhere"
    MISSING = "missing"
    NO_HASH = "no_hash"


@dataclass
class RomStatus:
    entry: DatEntry
    game: str
    rom: DatRom
    state: RomState
    source_path: str = ""       # skąd można wziąć dane (dla wrong_name/elsewhere)
    via_chd: bool = False       # gra zaspokojona przez plik .chd (cała gra!)
    via_archive: bool = False   # cała gra w jednym archiwum <gra>.zip/7z (kartridż)
    member: str = ""            # nazwa pliku WEWNĄTRZ archiwum source_path
    canonical_override: str = ""  # via_chd/archiwum: wspólna ścieżka gry
    game_multi: bool = False    # gra wieloplikowa (CD bin/cue) => podkatalog
    archive_names_ok: bool = True  # via_archive: nazwy WEWN. zgodne z DAT-em
                                   # (False => przepakuj z poprawnymi nazwami)

    @property
    def canonical_path(self) -> Path:
        """Docelowa ścieżka pliku wg DAT-a.

        Układ jak w praktyce kolekcjonerskiej: gra WIELOPLIKOWA (CD: biny+cue)
        dostaje własny podkatalog <gra>/, jednoplikowa (kartridż, iso) i CHD
        leżą płasko w katalogu DAT-a.
        """
        if self.canonical_override:
            return Path(self.canonical_override)
        if self.game_multi:
            return self.entry.target_dir / self.game / self.rom.name
        return self.entry.target_dir / self.rom.name


@dataclass
class DatReport:
    entry: DatEntry
    statuses: list[RomStatus] = field(default_factory=list)

    def count(self, *states: RomState) -> int:
        return sum(1 for s in self.statuses if s.state in states)

    @property
    def total(self) -> int:
        return len(self.statuses)

    def game_stats(self) -> tuple[int, int, int, int]:
        """Statystyki na poziomie GRY (spójne z listą gier w GUI):
        (wszystkie, komplet, do_naprawy, brak).

        Stan gry = najgorszy stan jej ROM-ów: komplet gdy wszystkie na
        miejscu; do naprawy gdy wszystkie obecne, ale któryś nie na miejscu;
        brak gdy choć jednego brakuje. To eliminuje mylące liczenie
        pojedynczych współdzielonych ścieżek (audio/cisza) gier, których i
        tak nie da się skompletować."""
        rank = {RomState.MISSING: 3, RomState.NO_HASH: 3,
                RomState.WRONG_NAME: 2, RomState.ELSEWHERE: 2,
                RomState.HAVE: 1, RomState.HAVE_CHD: 1}
        worst: dict[str, RomState] = {}
        for s in self.statuses:
            cur = worst.get(s.game)
            if cur is None or rank[s.state] > rank[cur]:
                worst[s.game] = s.state
        complete = fix = miss = 0
        for st in worst.values():
            if st in (RomState.HAVE, RomState.HAVE_CHD):
                complete += 1
            elif st in (RomState.WRONG_NAME, RomState.ELSEWHERE):
                fix += 1
            else:
                miss += 1
        return len(worst), complete, fix, miss

    def summary(self) -> str:
        total, complete, fix, miss = self.game_stats()
        pct = (complete / total * 100) if total else 0.0
        return (f"gry: {complete}/{total} ({pct:.1f}%), do naprawy {fix}, "
                f"brak {miss}")


def game_stats_from_states(game_states: dict) -> tuple[int, int, int, int]:
    """(wszystkie, komplet, do_naprawy, brak) z mapy {gra: {rom: RomState}}.
    Wspólne dla żywego raportu i wczytanego cache."""
    rank = {RomState.MISSING: 3, RomState.NO_HASH: 3,
            RomState.WRONG_NAME: 2, RomState.ELSEWHERE: 2,
            RomState.HAVE: 1, RomState.HAVE_CHD: 1}
    complete = fix = miss = 0
    for roms in game_states.values():
        worst = max(roms.values(), key=lambda st: rank[st])
        if worst in (RomState.HAVE, RomState.HAVE_CHD):
            complete += 1
        elif worst in (RomState.WRONG_NAME, RomState.ELSEWHERE):
            fix += 1
        else:
            miss += 1
    return len(game_states), complete, fix, miss


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(a) == os.path.normcase(b)


def _link_satisfies(canonical, src: str) -> bool:
    """Czy ścieżka kanoniczna to POPRAWNY symlink na znalezioną kopię
    fizyczną `src`? Wtedy gra DZIECKA jest na miejscu (HAVE), a nie „do
    naprawy" — dokładnie tak dzieci mają wyglądać po naprawie."""
    c = str(canonical)
    try:
        if not os.path.islink(c):
            return False
        target = os.readlink(c)
    except OSError:
        return False
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(c), target)
    # \\?\ prefix z mklink — znormalizuj przed porównaniem
    target = target.replace("\\\\?\\", "")
    src = str(src).replace("\\\\?\\", "")
    return _same_path(os.path.abspath(target), os.path.abspath(src)) \
        and os.path.exists(src)


def _row_full_match(row, rom) -> bool:
    """WSZYSTKIE sumy podane w DAT-cie muszą się zgadzać z wpisem indeksu
    (rozmiar, CRC32, MD5, SHA-1) — nie tylko najsilniejsza. Pola puste po
    którejkolwiek stronie nie blokują (np. członek archiwum przy szybkim
    skanie nie ma jeszcze MD5)."""
    try:
        if rom.size and row["size"] and row["size"] != rom.size:
            return False
        if rom.crc and row["crc32"] and \
                row["crc32"] != rom.crc.lower().zfill(8):
            return False
        if rom.md5 and row["md5"] and row["md5"] != rom.md5.lower():
            return False
        if rom.sha1 and row["sha1"] and row["sha1"] != rom.sha1.lower():
            return False
    except (KeyError, IndexError):
        return True
    return True


def _pick(rows: Sequence, canonical: Path, target_dir: Path):
    """Wybiera najlepszego kandydata: kanoniczny > w katalogu DAT-a > inny."""
    canon = str(canonical)
    tprefix = os.path.normcase(str(target_dir)).rstrip("\\/") + os.sep
    in_dir = None
    other = None
    for r in rows:
        if r["is_link"]:
            continue  # link nie jest kopią fizyczną
        if _same_path(r["path"], canon):
            return r, "canonical"
        if os.path.normcase(r["path"]).startswith(tprefix):
            in_dir = in_dir or r
        else:
            other = other or r
    if in_dir is not None:
        return in_dir, "in_dir"
    if other is not None:
        return other, "other"
    return None, ""


def match_rom(entry: DatEntry, game: str, rom: DatRom, index: FileIndex,
              game_multi: bool = False) -> RomStatus:
    """Status pojedynczego ROM-a z DAT-a względem indeksu (bez CHD —
    dopasowanie CHD jest na poziomie GRY, patrz match_game).

    game_multi — gra wieloplikowa luzem => podkatalog per gra."""
    rows: list = []
    if rom.sha1:
        rows = index.find_sha1(rom.sha1, include_chd_content=False)
    if not rows and rom.md5:
        rows = [r for r in index._db.execute(
            "SELECT * FROM files WHERE missing=0 AND md5=?", (rom.md5.lower(),))]
    if not rows and rom.crc and rom.size:
        rows = index.find_crc(rom.crc, rom.size)
    if not rows and not (rom.sha1 or rom.md5 or rom.crc):
        return RomStatus(entry, game, rom, RomState.NO_HASH,
                         game_multi=game_multi)

    direct = [r for r in rows if (rom.sha1 and r["sha1"] == rom.sha1.lower())
              or (not rom.sha1 and r["sha1"])]
    if not direct and not (rom.sha1 or rom.md5):
        direct = list(rows)   # dopasowanie tylko po CRC+rozmiar
    if not direct and rom.md5:
        direct = [r for r in rows if r["md5"] == rom.md5.lower()]
    # WSZYSTKIE sumy z DAT-a muszą się zgadzać, nie tylko ta, po której
    # szukaliśmy — plik z poprawnym SHA-1 ale złym rozmiarem/CRC odpada
    direct = [r for r in direct if _row_full_match(r, rom)]

    status = RomStatus(entry, game, rom, RomState.MISSING,
                       game_multi=game_multi)
    if direct:
        row, kind = _pick(direct, status.canonical_path, entry.target_dir)
        if row is not None:
            status.source_path = row["path"]
            status.state = {"canonical": RomState.HAVE,
                            "in_dir": RomState.WRONG_NAME,
                            "other": RomState.ELSEWHERE}[kind]
            # kanoniczna ścieżka jest już POPRAWNYM linkiem na tę kopię
            # (typowy stan DZIECKA po naprawie) => na miejscu, nie „napraw"
            if (status.state != RomState.HAVE
                    and _link_satisfies(status.canonical_path, row["path"])):
                status.state = RomState.HAVE
            return status

    # trafienie wewnątrz archiwum (SHA-1, potem CRC32+rozmiar z ZIP-a) —
    # też z wymogiem zgodności WSZYSTKICH dostępnych sum
    mrows: list = []
    if rom.sha1:
        mrows = [r for r in index.find_member_sha1(rom.sha1)
                 if _row_full_match(r, rom)]
    if not mrows and rom.crc and rom.size:
        mrows = [r for r in index.find_member_crc(rom.crc, rom.size)
                 if _row_full_match(r, rom)]
    if mrows:
        status.source_path = mrows[0]["archive"]
        status.member = mrows[0]["name"]
        status.state = RomState.ELSEWHERE
    return status


def _under(path: str, target_dir) -> bool:
    """Czy `path` leży w katalogu docelowym (bezpośrednio lub w podkatalogu)."""
    return os.path.normcase(path).startswith(
        os.path.normcase(str(target_dir)).rstrip("\\/") + os.sep)


def _archives_with_game(game, index: FileIndex) -> dict:
    """{ścieżka_archiwum: {rom_idx: nazwa_członka}} — archiwa (zip/7z) zawierające
    ROM-y gry (trafienie SHA-1, potem CRC32+rozmiar). Do formatu kartridżowego,
    gdzie CAŁA gra siedzi w jednym pliku <gra>.zip."""
    per: dict[str, dict[int, str]] = {}
    for i, rom in enumerate(game.roms):
        rows: list = []
        if rom.sha1:
            rows = index.find_member_sha1(rom.sha1)
        if not rows and rom.crc and rom.size:
            rows = index.find_member_crc(rom.crc, rom.size)
        for r in rows:
            if _row_full_match(r, rom):     # KOMPLET sum, nie jedna
                per.setdefault(r["archive"], {})[i] = r["name"]
    return per


def _match_game_archive(entry, game, index: FileIndex, want_ext, allow_move):
    """Format kartridżowy: cała gra przechowywana jako jedno archiwum
    ``<gra>.zip`` (albo .7z). Zwraca statusy albo None (matcher spróbuje wtedy
    luźnych plików / wypakowania / CHD).

    - archiwum w KATALOGU DOCELOWYM => HAVE (zielone; NIE wypakowujemy —
      to JEST docelowy format, wbrew nazwie .iso/.n64 z DAT-a);
    - archiwum gdzie indziej (ToSort/inny DAT), gdy `allow_move` (format
      jawnie zip/7z) => ELSEWHERE: przenieś CAŁE archiwum, nie wypakowuj.
      Dla „keep" zwracamy None — niech zadziała stara ścieżka wypakowania.
    """
    need = set(range(len(game.roms)))
    per = _archives_with_game(game, index)          # {archiwum: {rom_idx: nazwa}}
    full = [(a, m) for a, m in per.items() if need <= set(m)]
    if not full:
        return None

    def _names_ok(members: dict) -> bool:
        # nazwa WEWNĄTRZ archiwum musi zgadzać się z nazwą ROM-a z DAT-a
        return all(members.get(i, "") == game.roms[i].name for i in need)

    def _mk(archive, members, state, canonical, names_ok):
        return [RomStatus(entry, game.name, rom, state, source_path=archive,
                          member=members.get(i, ""), via_archive=True,
                          canonical_override=canonical, archive_names_ok=names_ok)
                for i, rom in enumerate(game.roms)]

    in_dir = [(a, m) for a, m in full if _under(a, entry.target_dir)]
    if in_dir:
        in_dir.sort(key=lambda am: (0 if _names_ok(am[1]) else 1, am[0].lower()))
        archive, members = in_dir[0]
        if _names_ok(members):
            # w katalogu docelowym + poprawne nazwy wewnętrzne => zielone
            return _mk(archive, members, RomState.HAVE, archive, True)
        # zawartość poprawna, ale zła nazwa wewnątrz => PRZEPAKUJ (naprawa)
        ext = want_ext or (Path(archive).suffix.lstrip(".").lower() or "zip")
        canonical = str(entry.target_dir / f"{game.name}.{ext}")
        return _mk(archive, members, RomState.WRONG_NAME, canonical, False)
    if not allow_move:
        return None
    full.sort(key=lambda am: (0 if _names_ok(am[1]) else 1, am[0].lower()))
    archive, members = full[0]
    ext = want_ext or (Path(archive).suffix.lstrip(".").lower() or "zip")
    canonical = str(entry.target_dir / f"{game.name}.{ext}")
    # kanoniczny zip DZIECKA jest już poprawnym linkiem na archiwum rodzica
    if _link_satisfies(canonical, archive):
        return _mk(archive, members, RomState.HAVE, canonical,
                   _names_ok(members))
    return _mk(archive, members, RomState.ELSEWHERE, canonical,
               _names_ok(members))


def _find_game_chd(game, index: FileIndex):
    """Szuka pliku .chd, którego zawartość odpowiada CAŁEJ grze.

    Porównanie po ODCISKU KOMPLETU ścieżek (game_profile): gra DVD = SHA-1
    obrazu iso; gra CD wielościeżkowa = syntetyczny hash WSZYSTKICH sum.
    Pojedyncza ścieżka NIE wystarcza — wydania (1S/5S) dzielą ścieżkę danych
    i różnią się tylko audio; dopasowanie po jednej sumie robiło fałszywe
    linki między różnymi zrzutami."""
    from .datfile import game_profile
    profile = game_profile(game.data_roms)
    if not profile:
        return None
    for r in index.find_sha1(profile, include_chd_content=True):
        if (not r["is_link"] and r["path"].lower().endswith(".chd")
                and r["data_sha1"] == profile.lower()):
            return r
    return None


def match_game(entry: DatEntry, game, index: FileIndex) -> list[RomStatus]:
    """Statusy wszystkich ROM-ów gry, ŚWIADOME formatu przechowywania.

    Kluczowe: format docelowy (``entry.store_format``) NADPISuje rozszerzenia
    z DAT-a. Gra kartridżowa trzymana jako ``<gra>.zip`` jest POPRAWNA (zielona),
    a nie „do wypakowania"; gra płytowa jako ``<gra>.chd`` jest poprawna wbrew
    temu, że DAT wymienia .iso/.bin. Kolejność rozpoznania:

    1. luźne pliki pod kanoniczną nazwą (HAVE) — najszybsze;
    2. format kartridżowy: cała gra w jednym archiwum <gra>.zip/7z;
    3. format płytowy: cała gra w jednym .chd (data_sha1);
    4. w ostateczności luźne/członkowie archiwum (przenieś/wypakuj).

    Układ: gra WIELOPLIKOWA luzem (bin/cue, gdi+tracki) dostaje podkatalog
    per gra (gdy entry.subdir_per_game); CHD i archiwa są płasko.
    """
    multi = len(game.roms) > 1 and getattr(entry, "subdir_per_game", True)
    statuses = [match_rom(entry, game.name, rom, index, game_multi=multi)
                for rom in game.roms]
    if all(s.state == RomState.HAVE for s in statuses):
        return statuses                       # luźne pliki już na miejscu

    fmt = getattr(entry, "store_format", "keep")
    # (2) kartridż: cała gra jako jedno archiwum. Dla „keep" też akceptujemy
    # archiwum w katalogu docelowym (nie wymuszamy wypakowania).
    if fmt in ("zip", "7z", "keep"):
        want = fmt if fmt in ("zip", "7z") else None
        arc = _match_game_archive(entry, game, index, want,
                                  allow_move=fmt in ("zip", "7z"))
        if arc is not None:
            return arc

    complete = all(s.state not in (RomState.MISSING, RomState.NO_HASH)
                   for s in statuses)
    if complete:
        return statuses
    chd_row = _find_game_chd(game, index)
    if chd_row is None:
        return statuses
    canonical = entry.target_dir / f"{game.name}.chd"
    src = chd_row["path"]
    if _same_path(src, str(canonical)) or _link_satisfies(canonical, src):
        state = RomState.HAVE_CHD
    elif os.path.normcase(src).startswith(
            os.path.normcase(str(entry.target_dir)).rstrip("\\/") + os.sep):
        state = RomState.WRONG_NAME
    else:
        state = RomState.ELSEWHERE
    for s in statuses:
        s.state = state
        s.via_chd = True
        s.member = ""
        s.source_path = src
        s.canonical_override = str(canonical)
    return statuses


def match_entry(entry: DatEntry, index: FileIndex) -> DatReport:
    entry.load()
    report = DatReport(entry)
    for game in entry.games:
        report.statuses.extend(match_game(entry, game, index))
    return report


def match_store(entries: Sequence[DatEntry], index: FileIndex,
                log: Optional[callable] = None) -> list[DatReport]:
    reports = []
    for e in entries:
        if log:
            log(f"DAT: {e.name}")
        reports.append(match_entry(e, index))
    return reports


def deep_probe_chds(
    index: FileIndex,
    entries: Sequence[DatEntry],
    chd,                        # CHDMan
    *,
    roots: Sequence[str | Path],
    work_dir: Optional[Path] = None,
    log: Optional[callable] = None,
    cancel_event=None,
    on_progress: Optional[callable] = None,   # (done, total, tekst) — OGÓLNY
    detail: Optional[callable] = None,        # (done, total, tekst) — SZCZEGÓŁ
    scratch_fallback: Optional[str] = None,   # dedykowany temp z ustawień
) -> int:
    """Identyfikuje pliki .chd względem DAT-ów i zapisuje wynik do indeksu.

    Dwustopniowo, per plik bez trafienia:
    1. TANIO — SHA-1 zawartości z nagłówka CHD (chdman info): trafia DVD
       (createdvd: data_sha1 == SHA-1 obrazu .iso).
    2. DROGO — deep_identify z chd_buddy: ekstrakcja kolejnymi metodami
       (extractdvd / extractcd+deframe dla DVD-spakowanych-jako-CD /
       surowe ścieżki CD / hd / raw / ld), aż wynik trafi w DAT.
       Obsługuje więc gry CD (bin/cue) i błędnie spakowane CHD.

    Wynik ląduje w files.data_sha1 — kosztowna ekstrakcja liczy się RAZ,
    a matcher widzi CHD jak zwykłe trafienie (cała gra).
    """
    from .datfile import DatIndex
    from .deepcheck import deep_identify

    def _log(m: str) -> None:
        if log:
            log(m)

    merged = DatIndex()
    for e in entries:
        for g in e.load().games:
            merged.add_game(g)
    # UZNAJEMY tylko ODCISK CAŁEJ GRY (komplet ścieżek). Stare wpisy z sumą
    # pojedynczej ścieżki wypadają z „known" i zostaną zidentyfikowane od
    # nowa — konieczne, bo jedna ścieżka nie odróżnia wydań (1S vs 5S).
    known = merged.by_profile

    identified = 0
    seen: set[str] = set()
    # 1. PASS: zbierz kandydatów (CHD bez identyfikacji, nie deep_fail-stale) —
    #    żeby pasek OGÓLNY pokazał realny licznik „X/Y", a nie stał na 0/0.
    candidates: list = []
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for row in index.all_under(root):
            p = row["path"]
            key = os.path.normcase(p)
            if key in seen or not p.lower().endswith(".chd"):
                continue
            seen.add(key)
            if row["data_sha1"] and row["data_sha1"] in known:
                continue          # już zidentyfikowany
            # PORAŻKA TEŻ JEST WYNIKIEM: plik już przeszedł głęboką
            # identyfikację bez dopasowania i się NIE ZMIENIŁ => nie mielimy
            # go ponownie co skan. Ponowną próbę wymusza pełny skan katalogu.
            try:
                deep_fail = row["deep_fail"]
            except (KeyError, IndexError):
                deep_fail = 0
            if deep_fail and deep_fail == row["mtime_ns"]:
                continue
            if Path(p).is_file():
                candidates.append(row)
    total_cand = len(candidates) or 1

    # 2. PASS: identyfikacja z postępem OGÓLNYM (X/Y CHD)
    for ci, row in enumerate(candidates):
            p = row["path"]
            path = Path(p)
            if cancel_event is not None and cancel_event.is_set():
                return identified
            if on_progress is not None:
                on_progress(ci, total_cand,
                            f"identyfikacja CHD ({ci + 1}/{total_cand}): "
                            f"{path.name}")
            # 1) tani nagłówek
            hit = ""
            info = None
            try:
                info = chd.info(path)
                for cand in (info.data_sha1, info.sha1):
                    if cand and cand.lower() in known:
                        hit = cand.lower()
                        break
            except OSError as e:
                _log(f"CHD info: {path.name}: {e}")
            if hit:
                index.set_data_sha1(path, hit)
                identified += 1
                _log(f"CHD OK (nagłówek): {path.name} -> {known[hit].game}")
                continue
            # 2) głęboka identyfikacja (ekstrakcja z fail-safe'ami)
            # Wypakowany obraz tylko HASHUJEMY (nie zostaje), więc może powstać
            # na DOWOLNYM dysku z miejscem — wybieramy dysk z zapasem (obok
            # pliku, jeśli ma; inaczej inny). Dzięki temu pełny dysk kolekcji
            # nie blokuje identyfikacji.
            from .scratch import pick_scratch_root
            need = max(int(path.stat().st_size * 2.2), 2 << 30)
            # RAM dysk MA PIERWSZEŃSTWO (jak przy konwersji); work_dir (jeśli
            # ustawiony) to dopiero fallback, gdy RAM niedostępny/za mały.
            wd = pick_scratch_root(
                need, prefer=(str(work_dir) if work_dir else str(path.parent)),
                log=_log, fallback=scratch_fallback)
            if wd is None:
                _log(f"CHD POMIJAM (za mało miejsca na ŻADNYM dysku): "
                     f"{path.name} — potrzeba ~{need/1024**3:.1f} GB")
                continue
            _log(f"CHD głęboko: {path.name}… (scratch: {wd})")

            # postęp ekstrakcji chdman (pct 0-100 albo -1=nieokreślony) → pasek
            # szczegółowy. Bez tego długie extractcd wyglądało na zawieszone.
            # PRIMUJEMY pasek od razu — nawet zanim chdman wypisze pierwszy %,
            # widać że trwa wypakowanie tego CHD.
            if detail is not None:
                detail(0, 0, f"wypakowuję CHD: {path.name}…")

            def _dp(pct: float, msg: str = "", _name=path.name) -> None:
                if detail is None:
                    return
                if pct is not None and pct >= 0:
                    detail(int(pct), 100, f"CHD {_name}: {msg or f'{int(pct)}%'}")
                else:
                    detail(0, 0, f"CHD {_name}: {msg}".rstrip(": "))

            r = deep_identify(chd, path, merged, wd,
                              log=lambda m: _log(f"  {m}"),
                              on_progress=_dp,
                              cancel_event=cancel_event, chd_info=info)
            if r.ok and r.sha1:
                index.set_data_sha1(path, r.sha1)
                identified += 1
                _log(f"CHD OK ({r.method}): {path.name} -> {r.game}")
            elif cancel_event is not None and cancel_event.is_set():
                return identified     # przerwane ręcznie — NIE zapisuj porażki
            else:
                index.set_deep_fail(path)   # zapamiętaj: nie próbuj ponownie
                _log(f"CHD BRAK: {path.name} — bez dopasowania "
                     f"(prób: {len(r.tried)}; zapamiętane — nie będzie "
                     f"mielony przy kolejnych skanach)")
    return identified

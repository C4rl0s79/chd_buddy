"""Rebuilder — odpowiednik „Fix" z RomVaulta, na indeksie + symlinkach.

Przetwarza raporty matchera w kolejności priorytetu DAT-ów:

- HAVE / HAVE_CHD  — plik na miejscu; rejestrujemy go jako kopię fizyczną
  (claim) dla tego hasha.
- WRONG_NAME       — zmiana nazwy w obrębie katalogu DAT-a.
- ELSEWHERE        — jeśli hash ma już kopię fizyczną w innym DAT-cie =>
  SYMLINK; w przeciwnym razie plik jest PRZENOSZONY pod ścieżkę kanoniczną
  i staje się kopią fizyczną. Pierwszy DAT (wg kolejności) trzyma plik,
  kolejne dostają linki — dokładnie hierarchia, o którą chodziło.
  Trafienie WEWNĄTRZ archiwum ZIP => WYPAKOWANIE pod ścieżkę kanoniczną
  z weryfikacją SHA-1/CRC w locie (zła zawartość nigdy nie ląduje pod
  docelową nazwą); archiwum źródłowe zostaje nietknięte.
- MISSING / NO_HASH — pomijane (raport pokaże braki).

Sprzątanie (opcjonalne): zaindeksowane pliki fizyczne w katalogach DAT-ów,
które nie są ścieżką kanoniczną żadnego ROM-a, wędrują do ToSort z
odwzorowaniem podkatalogów (jak Primary ToSort w RomVaulcie).

Bezpieczeństwo: nic nie jest nadpisywane; zajęta ścieżka docelowa (zwykłym
plikiem) to konflikt raportowany, nie kasowany. Usuwane bywają wyłącznie
symlinki. Plik zmieniony od czasu skanu (inny rozmiar) nie jest ruszany.
Indeks jest aktualizowany po każdym przeniesieniu, więc ponowny skan nie
liczy sum od nowa.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .fileindex import FileIndex
from .linker import create_link, is_link, remove_link
from .matcher import DatReport, RomState, RomStatus

LogCB = Callable[[str], None]


def _retry_locked(fn, attempts: int = 4, delay: float = 0.4):
    """Ponawia operację plikową przy chwilowej blokadzie Windows.

    WinError 32 (plik używany przez inny proces — antywirus, indexer,
    równoległy chdman) i 5 (odmowa dostępu) bywają przejściowe; PyLinks
    miał tę samą lekcję przy zapisie .lnk. Inne błędy lecą od razu.
    """
    import time
    for i in range(attempts):
        try:
            return fn()
        except OSError as e:
            if getattr(e, "winerror", None) not in (5, 32) or i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


@dataclass
class RebuildStats:
    already_ok: int = 0
    moved: int = 0
    renamed: int = 0
    linked: int = 0
    copied: int = 0
    unpacked: int = 0
    tosorted: int = 0
    deduped: int = 0
    tosort_deleted: int = 0
    substituted: int = 0
    conflicts: int = 0
    skipped: int = 0
    links_skipped: int = 0      # symlink niemożliwy => NIC nie tworzymy
    incomplete: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (f"na miejscu {self.already_ok}, przeniesiono {self.moved}, "
                f"zmieniono nazwę {self.renamed}, symlinki {self.linked}, "
                f"skopiowano {self.copied}, wypakowano {self.unpacked}, "
                f"do ToSort {self.tosorted}, kopie→symlinki {self.deduped}, "
                f"skasowano z ToSort {self.tosort_deleted}, "
                f"tłumaczenia {self.substituted}, "
                f"konflikty {self.conflicts}, pominięto {self.skipped}, "
                f"pominięte symlinki {self.links_skipped}, "
                f"niekompletne gry {self.incomplete}, błędy {self.errors}")


# Znacznik fanowskiego tłumaczenia w nazwie gry: [T-En by X v1.0], (T-En) itd.
_T_EN_RE = re.compile(r"[\[\(]t-en", re.IGNORECASE)
# Wydanie japońskie (kandydat do podmiany na tłumaczenie).
_JAPAN_RE = re.compile(r"\(japan", re.IGNORECASE)


def _base_title(name: str) -> str:
    """Tytuł bez wszystkich tagów () i [] — do parowania gry z tłumaczeniem."""
    out = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", name)
    return re.sub(r"\s+", " ", out).strip().lower()


def _claim_key(s: RomStatus) -> str:
    if s.via_chd:
        # CHD zaspokaja CAŁĄ grę — wszystkie jej ROM-y wskazują ten sam plik,
        # więc kluczem jest źródłowy .chd (jedno przeniesienie, reszta = ok)
        return "chdsrc:" + os.path.normcase(s.source_path)
    if s.via_archive:
        # archiwum <gra>.zip trzyma CAŁĄ grę — jedno przeniesienie, reszta = ok
        return "arcsrc:" + os.path.normcase(s.source_path)
    if s.rom.sha1:
        return "sha1:" + s.rom.sha1.lower()
    if s.rom.md5:
        return "md5:" + s.rom.md5.lower()
    return f"crc:{s.rom.crc.lower().zfill(8)}:{s.rom.size}"


def _size_ok(path: Path, expected: int) -> bool:
    """Plik nie zmienił się od skanu (kontrola rozmiaru przed operacją)."""
    if expected <= 0:
        return True
    try:
        return os.lstat(path).st_size == expected
    except OSError:
        return False


class Rebuilder:
    def __init__(self, index: FileIndex, *, tosort: Optional[Path] = None,
                 dry_run: bool = True, log: Optional[LogCB] = None,
                 make_links: bool = True, detail: Optional[Callable] = None,
                 zip_level: int = 6):
        self.index = index
        self.tosort = Path(os.path.abspath(tosort)) if tosort else None
        self.dry_run = dry_run
        # False => w ogóle nie próbujemy tworzyć symlinków (i NIC nie kopiujemy)
        self.make_links = make_links
        self.stats = RebuildStats()
        self._log_cb = log
        # pasek SZCZEGÓŁOWY: postęp bieżącej operacji plikowej (bajty)
        self._detail_cb = detail
        # poziom DEFLATE przy przepakowaniu ZIP (naprawa złych nazw wewn.)
        self._zip_level = max(0, min(int(zip_level), 9))
        # hash -> ścieżka kopii fizycznej (kanonicznej)
        self._claims: dict[str, Path] = {}
        # wszystkie ścieżki kanoniczne tego przebiegu (do sprzątania)
        self._canonical: set[str] = set()
        # brak uprawnień do symlinków: nie przerywaj — pomijaj linki z licznikiem
        self._links_blocked = False
        # przerwane przez użytkownika (to co zrobione — zostaje zrobione)
        self.cancelled = False
        # podmiany Japan→T-En do wykonania po głównym przebiegu
        self._pending_subst: list[tuple[Path, Path, str, str]] = []

    def _log(self, msg: str) -> None:
        if self._log_cb:
            self._log_cb(msg)

    def add_canonical(self, path) -> None:
        """Rejestruje ścieżkę jako KANONICZNĄ (wynik naprawy). Konwersja woła
        to dla utworzonych CHD/RVZ/ZIP — inaczej faza sprzątania uznałaby nowy
        plik za nieznany i wrzuciła go do ToSort."""
        self._canonical.add(os.path.normcase(str(path)))

    def _phase(self, done: int, total: int, label: str) -> None:
        """Pasek OGÓLNY dla faz po głównej pętli (ToSort, dedup)."""
        cb = getattr(self, "_on_progress", None)
        if cb is not None:
            cb(done, total, label)

    def _detail(self, done: int, total: int, label: str) -> None:
        if self._detail_cb is not None:
            self._detail_cb(done, total, label)

    def _detail_clear(self) -> None:
        if self._detail_cb is not None:
            self._detail_cb(-1, 0, "")

    # --- operacje plikowe (wszystkie honorują dry_run) ---------------------

    def _ensure_dir(self, d: Path) -> None:
        if not self.dry_run:
            d.mkdir(parents=True, exist_ok=True)

    def _move(self, src: Path, dst: Path) -> bool:
        self._ensure_dir(dst.parent)
        if self.dry_run:
            return True
        from .fileops import move_with_progress
        try:
            _retry_locked(lambda: move_with_progress(
                src, dst, on_progress=self._detail_cb,
                label=f"przenoszę {dst.name}"))
        except OSError as e:
            self.stats.errors += 1
            self._log(f"BŁĄD przenoszenia {src}: {e}")
            return False
        finally:
            self._detail_clear()
        self.index.rename(src, dst)
        return True

    def _symlink(self, link_path: Path, target: Path) -> bool:
        if not self.make_links or self._links_blocked:
            return False
        self._ensure_dir(link_path.parent)
        if self.dry_run:
            return True
        try:
            create_link(link_path, target, is_dir=False)
        except OSError as e:
            from .linker import LinkPrivilegeError
            if isinstance(e, LinkPrivilegeError):
                # nie przerywaj przebiegu — ale NIC nie kopiujemy w zamian:
                # te miejsca zostaną PUSTE, linki dołoży przebieg jako admin
                self._links_blocked = True
                self._log(f"UWAGA: {e} Symlinki POMIJAM (nic nie kopiuję) — "
                          f"uruchom jako administrator, aby je utworzyć.")
                return False
            self.stats.errors += 1
            self._log(f"BŁĄD linku {link_path}: {e}")
            return False
        return True

    def _relink_if_stale(self, link_path: Path, target: Path) -> bool:
        """Symlink `link_path` powinien wskazywać AKTUALNĄ kopię fizyczną
        `target`. Gdy wskazuje inny/nieistniejący cel — odtwórz go.

        Zwraca True, gdy link został (albo w dry-run zostałby) przepięty;
        False, gdy już był poprawny (wtedy caller liczy go jako already_ok).
        Bez uprawnień do symlinków NIC nie kopiujemy — link_skipped++.
        """
        try:
            current = os.readlink(str(link_path))
        except OSError:
            current = None
        if current is not None:
            if not os.path.isabs(current):
                current = os.path.join(os.path.dirname(str(link_path)), current)
            current = current.replace("\\\\?\\", "")
            same = (os.path.normcase(os.path.abspath(current))
                    == os.path.normcase(os.path.abspath(str(target))))
            # cel się zgadza I link nie wisi (cel istnieje) → nic nie rób
            if same and link_path.exists():
                return False
        if not target.exists():
            return False        # nie ma na co pokazać — zostaw jak jest
        self._log(f"POPRAW LINK {link_path} -> {target}")
        if self.dry_run:
            self.stats.linked += 1
            return True
        try:
            remove_link(link_path)
        except OSError as e:
            self.stats.errors += 1
            self._log(f"BŁĄD usuwania starego linku {link_path}: {e}")
            return True         # nie licz jako already_ok — link był zły
        if self._symlink(link_path, target):
            self.index.mark_link(link_path)
            self.stats.linked += 1
        else:
            self.stats.links_skipped += 1
        return True

    def _clear_dest(self, dst: Path) -> bool:
        """True gdy ścieżka docelowa jest wolna (usuwa najwyżej symlink)."""
        if not os.path.lexists(dst):
            return True
        if is_link(dst):
            if not self.dry_run:
                remove_link(dst)
            return True
        return False

    # --- główny przebieg -----------------------------------------------------

    def run(self, reports: Sequence[DatReport], clean: bool = False,
            only_complete: bool = True,
            on_progress: Optional[Callable[[int, int, str], None]] = None,
            rules: Optional[Callable[[object], dict]] = None,
            dedup_roots: Sequence[Path] = (),
            delete_placed_from: Sequence[Path] = (),
            cancel=None,
            after_place: Optional[Callable[[], None]] = None,
            converted_games: Optional[set] = None,
            ) -> RebuildStats:
        """only_complete: buduj wyłącznie gry, których WSZYSTKIE pliki są
        dostępne (jak „Only keep complete sets" w RomVaulcie) — bez tego
        wspólne ścieżki (np. cisza na płycie) zaśmiecają setki cudzych gier.
        on_progress(plik_i, plików_razem, nazwa_data) — postęp PER PLIK
        (pasek przesuwa się z każdym przetworzonym plikiem, nie co DAT).
        rules(entry) -> dict — kaskadowe reguły per katalog (dirrules);
        dedup_roots — po naprawie: fizyczne KOPIE potwierdzonych plików
        w tych korzeniach są zamieniane na symlinki (kopia fizyczna zostaje
        tylko pod ścieżką kanoniczną DAT-a rodzica)."""
        protected: list[str] = []
        done_reports: list[DatReport] = []
        tmap = (self._translation_map(reports)
                if rules and any(rules(r.entry).get("prefer_translations")
                                 for r in reports) else {})
        # OGÓLNY postęp — FAZOWY i liczony w REALNYCH PLIKACH (nie statusach
        # DAT-a). Faza „naprawa" liczy DISTINCT pliki źródłowe do ułożenia
        # (ELSEWHERE/WRONG_NAME): braki (MISSING) i to-co-już-jest (HAVE) nie
        # zawyżają. Dzięki temu przy 9 plikach w ToSort widać „naprawa 3/9",
        # a nie „34/35" z definicji całej kolekcji. Pasek trzymamy na self,
        # żeby fazy po pętli (ToSort/dedup) też go ruszały.
        self._on_progress = on_progress
        placed_sources: set[str] = set()
        _actionable = {RomState.ELSEWHERE, RomState.WRONG_NAME}
        place_total = len({os.path.normcase(s.source_path)
                           for r in reports for s in r.statuses
                           if s.state in _actionable and s.source_path}) or 1

        def _tick(name: str, force: bool = False) -> None:
            if on_progress:
                on_progress(len(placed_sources), place_total,
                            f"naprawa: {name}")

        for rep_i, rep in enumerate(reports):
            if cancel is not None and cancel.is_set():
                self.cancelled = True
                self._log(f"PRZERWANO naprawę na DAT-cie {rep_i}/{len(reports)}"
                          f" — wykonane operacje są już na dysku i zapisane "
                          f"w indeksie; wznowienie dokończy resztę.")
                break
            _tick(rep.entry.name, force=True)
            eff = rules(rep.entry) if rules else None
            if eff and eff.get("skip"):
                self._log(f"── {rep.entry.name}: POMINIĘTY (reguła skip)")
                continue
            oc = eff["only_complete"] if eff else only_complete
            if eff is not None and not eff.get("dedup_copies", True):
                protected.append(os.path.normcase(
                    str(rep.entry.target_dir)).rstrip("\\/") + os.sep)
            done_reports.append(rep)
            self._log(f"── {rep.entry.name} ({rep.entry.target_dir})")
            subst_games = (self._plan_substitutions(rep, tmap)
                           if eff and eff.get("prefer_translations") and tmap
                           else set())
            skip_games: set[str] = set()
            if oc:
                by_game: dict[str, bool] = {}
                for s in rep.statuses:
                    # brakujący .cue nie blokuje gry — bywa nieobecny w zrzucie
                    # (odtwarzalny z .bin, np. przy ekstrakcji z CHD)
                    ok = (s.state not in (RomState.MISSING, RomState.NO_HASH)
                          or s.rom.name.lower().endswith(".cue"))
                    by_game[s.game] = by_game.get(s.game, True) and ok
                skip_games = {g for g, ok in by_game.items() if not ok}
            for s in rep.statuses:
                # gra już skonwertowana PROSTO ZE ŹRÓDŁA (finał w docelowym
                # zgłoszony przez add_canonical; źródło do zbiorczego kasowania)
                # — placement pomija, inaczej układałby luźne źródło obok CHD.
                if (converted_games is not None
                        and f"{id(rep.entry)}::{s.game}" in converted_games):
                    continue
                if s.game in subst_games:
                    continue        # zamiast wersji Japan wchodzi tłumaczenie
                if s.game in skip_games:
                    if s.state in (RomState.WRONG_NAME, RomState.ELSEWHERE):
                        self.stats.incomplete += 1
                    continue
                self._process(s)
                # pasek rośnie o KAŻDY realnie ułożony plik źródłowy (distinct)
                if s.state in _actionable and s.source_path:
                    src = os.path.normcase(s.source_path)
                    if src not in placed_sources:
                        placed_sources.add(src)
                        _tick(rep.entry.name)
            _tick(rep.entry.name, force=True)
        self._apply_substitutions()
        if self.cancelled:
            # BEZPIECZEŃSTWO: po przerwaniu NIE sprzątamy i NIE dedupujemy —
            # część DAT-ów nie została przetworzona, więc ich pliki wyglądałyby
            # na „nieznane" i poleciałyby do ToSort albo zostały skasowane.
            self._log("Po przerwaniu pomijam sprzątanie do ToSort i dedup "
                      "(niepełny obraz kolekcji).")
            return self.stats
        # KONWERSJA (jeśli podano) MUSI iść PRZED dedupem/sprzątaniem: zamienia
        # luźne pliki na CHD/RVZ, więc dedup nie zlinkuje plików, które za
        # chwilę zniknęłyby w konwersji (katastrofa wiszących linków D2).
        if after_place is not None and not self.dry_run:
            after_place()
        if clean and self.tosort:
            # policz nie-kanoniczne pliki do przeniesienia → pasek „sprzątanie"
            self._clean_total = 0
            for rep in done_reports:
                for row in self.index.all_under(rep.entry.target_dir,
                                                physical_only=False):
                    if os.path.normcase(row["path"]) not in self._canonical:
                        self._clean_total += 1
            self._clean_total = self._clean_total or 1
            self._clean_done = 0
            for rep in done_reports:
                self._clean_dir(rep.entry.target_dir)
        if dedup_roots or delete_placed_from:
            self._dedup_confirmed(dedup_roots or delete_placed_from, protected,
                                  delete_roots=delete_placed_from)
        for rep in done_reports:
            self._prune_empty_dirs(rep.entry.target_dir)
        # puste podkatalogi ZOSTAJĄCE w ToSort po zabraniu plików — sprzątamy
        # (sam korzeń ToSort zostaje). Katalogów ROM-ów celowo NIE ruszamy:
        # pusty katalog systemu (układ ES) to nie śmieć.
        for root in delete_placed_from:
            if root:
                self._prune_empty_dirs(Path(root))
        return self.stats

    def _prune_empty_dirs(self, target_dir: Path) -> None:
        """Usuwa puste podkatalogi po przenosinach (rmdir — tylko puste)."""
        if self.dry_run or not target_dir.is_dir():
            return
        for dirpath, _dirs, _files in sorted(
                os.walk(target_dir), key=lambda t: len(t[0]), reverse=True):
            if os.path.normcase(dirpath) == os.path.normcase(str(target_dir)):
                continue
            try:
                os.rmdir(dirpath)   # pada, gdy katalog nie jest pusty — OK
                self._log(f"USUNIĘTO pusty katalog: {dirpath}")
            except OSError:
                pass

    # --- tłumaczenia: podmiana wersji Japan na T-En -----------------------------

    def _translation_map(self, reports: Sequence[DatReport]) -> dict:
        """Dostępne fanowskie tłumaczenia we WSZYSTKICH DAT-ach:
        tytuł bazowy -> (ścieżka kanoniczna, nazwa gry, id wpisu DAT).
        v1: tylko gry jednoplikowe (iso/CHD/kartridż)."""
        tmap: dict[str, tuple[Path, str, int]] = {}
        for rep in reports:
            by_game: dict[str, list[RomStatus]] = {}
            for s in rep.statuses:
                by_game.setdefault(s.game, []).append(s)
            for gname, sts in by_game.items():
                if not _T_EN_RE.search(gname):
                    continue
                ok = all(s.state not in (RomState.MISSING, RomState.NO_HASH)
                         or s.rom.name.lower().endswith(".cue") for s in sts)
                if not ok:
                    continue
                canonicals = {str(s.canonical_path) for s in sts
                              if not s.rom.name.lower().endswith(".cue")}
                if len(canonicals) != 1:
                    self._log(f"TŁUMACZENIE (pominięte, wieloplikowe): {gname}")
                    continue
                tmap[_base_title(gname)] = (Path(next(iter(canonicals))),
                                            gname, id(rep.entry))
        return tmap

    def _plan_substitutions(self, rep: DatReport, tmap: dict) -> set[str]:
        """Gry (Japan) tego DAT-a, za które wejdzie tłumaczenie."""
        subst: set[str] = set()
        for gname in {s.game for s in rep.statuses}:
            if _T_EN_RE.search(gname) or not _JAPAN_RE.search(gname):
                continue
            hit = tmap.get(_base_title(gname))
            if hit is None or hit[2] == id(rep.entry):
                continue
            tpath, tname, _eid = hit
            self._pending_subst.append(
                (rep.entry.target_dir / tpath.name, tpath, gname, tname))
            self.stats.substituted += 1
            self._log(f"TŁUMACZENIE: {gname} -> {tname}")
            subst.add(gname)
        return subst

    def _apply_substitutions(self) -> None:
        """Po głównym przebiegu (cele tłumaczeń już ułożone): linki T-En."""
        for link_path, target, _gname, _tname in self._pending_subst:
            if self.dry_run:
                self._log(f"LINK(T-En) {link_path} -> {target}")
                continue
            if not os.path.lexists(target):
                self._log(f"TŁUMACZENIE: cel nie istnieje (pomijam): {target}")
                continue
            if os.path.lexists(link_path):
                if is_link(link_path):
                    remove_link(link_path)      # odśwież istniejący link
                else:
                    continue                    # prawdziwy plik — nie ruszamy
            self._log(f"LINK(T-En) {link_path} -> {target}")
            if self._symlink(link_path, target):
                self.stats.linked += 1
            else:
                # bez uprawnień do symlinków NIE kopiujemy — nic tu nie powstaje
                self.stats.links_skipped += 1

    # --- kopie potwierdzonych plików -> symlinki -------------------------------

    def _dedup_confirmed(self, roots: Sequence[Path],
                         protected: Sequence[str],
                         delete_roots: Sequence[Path] = ()) -> None:
        """Plik potwierdzony pod ścieżką kanoniczną (parent) — pozostałe
        fizyczne kopie w obrębie `roots` zamieniamy na symlinki.

        Kopie leżące w `delete_roots` (np. ToSort) są KASOWANE zamiast
        linkowane — plik jest już poprawnie ułożony gdzie indziej, więc jego
        kopia w ToSort jest zbędna (weryfikacja: identyczny SHA-1 i rozmiar).

        Podmiana odwracalna (rename → symlink → dopiero usunięcie tmp);
        katalogi z regułą dedup_copies=false są chronione. Bez uprawnień
        do symlinków pass symlinkowy jest pomijany, ale kasowanie z ToSort
        (bezpieczne — kopia potwierdzona) działa dalej.
        """
        del_prefixes = [os.path.normcase(str(Path(os.path.abspath(r)))).rstrip(
            "\\/") + os.sep for r in delete_roots if r]
        if self._links_blocked and not del_prefixes:
            self._log("Kopie→symlinki: pominięte (brak uprawnień do symlinków).")
            return
        prefixes = [os.path.normcase(str(Path(os.path.abspath(r)))).rstrip("\\/")
                    + os.sep for r in roots if r]
        canonicals = {os.path.normcase(str(p)) for p in self._claims.values()}
        _uniq = sorted(set(self._claims.values()))
        _dtotal = len(_uniq) or 1
        for _di, canonical in enumerate(_uniq, 1):
            self._phase(_di, _dtotal, "dedup / sprzątanie kopii")
            row = self.index.lookup(canonical)
            if row is None or not row["sha1"] or row["is_link"] or row["missing"]:
                continue
            for r in self.index.find_sha1(row["sha1"],
                                          include_chd_content=False):
                p = r["path"]
                np = os.path.normcase(p)
                if (r["is_link"] or np in canonicals
                        or r["size"] != row["size"]):
                    continue
                if not any(np.startswith(rp) for rp in prefixes):
                    continue
                if any(np.startswith(pp) for pp in protected):
                    continue
                victim = Path(p)
                try:
                    vst = os.lstat(victim)
                except OSError:
                    continue
                if is_link(victim) or vst.st_size != row["size"]:
                    continue

                # kopia w ToSort, a plik jest już na miejscu → SKASUJ (zbędna)
                if any(np.startswith(dp) for dp in del_prefixes):
                    self._log(f"KASUJ z ToSort (już na miejscu): {victim}")
                    if not self.dry_run:
                        try:
                            _retry_locked(lambda v=victim: os.unlink(v))
                            self.index.remove_path(victim)
                        except OSError as e:
                            self.stats.errors += 1
                            self._log(f"BŁĄD kasowania {victim}: {e}")
                            continue
                    self.stats.tosort_deleted += 1
                    continue

                if self._links_blocked:
                    continue
                self._log(f"KOPIA→SYMLINK {victim} -> {canonical}")
                if self.dry_run:
                    self.stats.deduped += 1
                    continue
                tmp = victim.with_name(victim.name + ".chdbuddy_dedup_tmp")
                try:
                    _retry_locked(lambda: os.replace(victim, tmp))
                except OSError as e:
                    self.stats.errors += 1
                    self._log(f"BŁĄD rename {victim}: {e}")
                    continue
                try:
                    create_link(victim, Path(canonical), is_dir=False)
                except OSError as e:
                    os.replace(tmp, victim)     # rollback
                    from .linker import LinkPrivilegeError
                    if isinstance(e, LinkPrivilegeError):
                        self._links_blocked = True
                        self._log(f"UWAGA: {e} Przerywam zamianę kopii.")
                        return
                    self.stats.errors += 1
                    self._log(f"BŁĄD symlinku {victim}: {e}")
                    continue
                try:
                    tmp.unlink()
                except OSError as e:
                    self._log(f"UWAGA: nie usunięto tymczasowego {tmp}: {e}")
                self.index.mark_link(victim)
                self.stats.deduped += 1

    def _process(self, s: RomStatus) -> None:
        key = _claim_key(s)
        canonical = s.canonical_path
        state = s.state

        if state in (RomState.MISSING, RomState.NO_HASH):
            return

        # ta sama ścieżka kanoniczna już obsłużona w tym przebiegu (np. gra
        # wieloplikowa zaspokojona jednym CHD — wszystkie ROM-y wskazują
        # <gra>.chd) — nie powielaj operacji (dublowane LINK-i w podglądzie).
        if os.path.normcase(str(canonical)) in self._canonical:
            self.stats.already_ok += 1
            return

        if state in (RomState.HAVE, RomState.HAVE_CHD):
            self._claims.setdefault(key, canonical)
            self._canonical.add(os.path.normcase(str(canonical)))
            # Gdy to trafienie jest SYMLINKIEM, a kopia fizyczna (claim) leży
            # gdzie indziej niż wskazuje link — przepnij. Klasyczny przypadek:
            # użytkownik zmienił katalog docelowy rodzica, rodzic (przetwarzany
            # pierwszy) przeniósł swój plik do nowego miejsca w TYM przebiegu,
            # a link dziecka nadal celuje w stare miejsce → wisi.
            claim = self._claims.get(key)
            if (is_link(canonical) and claim is not None
                    and os.path.normcase(str(claim))
                    != os.path.normcase(str(canonical))):
                if self._relink_if_stale(canonical, Path(claim)):
                    return
            self.stats.already_ok += 1
            return

        # hash już ma kopię fizyczną gdzie indziej => link
        claimed = self._claims.get(key)
        # LUŹNE pliki gier przeznaczonych do konwersji (chd/rvz) NIE mogą być
        # celem linków: konwersja kasuje źródła po sukcesie i linki wiszą
        # (katastrofa D2: wspólny Track 2 dysków 2-4 wskazywał plik zjedzony
        # przez konwersję Disc 1). Zamiast linku:
        #   - ta sama biblioteka: WŁASNA kopia fizyczna (przeniesiona z
        #     własnego źródła; zniknie w konwersji tej gry),
        #   - dziecko: ODROCZONE — link powstanie do gotowego .chd/.rvz
        #     przy następnej naprawie po konwersji rodzica.
        if (claimed is not None and not s.via_chd and not s.via_archive
                and getattr(s.entry, "store_format", "keep") in ("chd", "rvz")
                and Path(claimed).suffix.lower() not in (".chd", ".rvz")):
            tprefix = os.path.normcase(
                str(s.entry.target_dir)).rstrip("\\/") + os.sep
            if os.path.normcase(str(claimed)).startswith(tprefix):
                # wspólna ścieżka gier TEJ SAMEJ biblioteki — własna kopia
                src0 = Path(s.source_path) if s.source_path else None
                if (not s.member and src0 and os.path.lexists(src0)
                        and os.path.normcase(str(src0))
                        != os.path.normcase(str(claimed))):
                    claimed = None          # przenieś WŁASNE źródło (niżej)
                else:
                    if not self._clear_dest(canonical):
                        self.stats.conflicts += 1
                        return
                    self._log(f"KOPIA (tymczasowa, do konwersji) {canonical}")
                    if not self.dry_run:
                        self._ensure_dir(canonical.parent)
                        from .fileops import copy_with_progress
                        try:
                            _retry_locked(lambda: copy_with_progress(
                                claimed, canonical, on_progress=self._detail_cb,
                                label=f"kopiuję {canonical.name}"))
                        except OSError as e:
                            self.stats.errors += 1
                            self._log(f"BŁĄD kopiowania {canonical}: {e}")
                            return
                        finally:
                            self._detail_clear()
                        row = self.index.lookup(claimed)
                        if row is not None and row["sha1"]:
                            self.index.record_file(canonical, row["crc32"],
                                                   row["md5"], row["sha1"])
                    self.stats.copied += 1
                    self._canonical.add(os.path.normcase(str(canonical)))
                    return
            else:
                # DZIECKO: poczekaj na kontener końcowy rodzica
                self.stats.links_skipped += 1
                self._log(f"ODROCZONE (link po konwersji rodzica): {canonical}")
                return
        if claimed is not None:
            if os.path.normcase(str(canonical)) == os.path.normcase(str(claimed)):
                # kolejny ROM tej samej gry wskazuje tę samą ścieżkę (CHD)
                self.stats.already_ok += 1
                return
            if os.path.lexists(canonical):
                if is_link(canonical):
                    self.stats.already_ok += 1
                else:
                    self.stats.conflicts += 1
                    self._log(f"KONFLIKT: {canonical} zajęte zwykłym plikiem")
                self._canonical.add(os.path.normcase(str(canonical)))
                return
            self._log(f"LINK   {canonical} -> {claimed}")
            # zarejestruj kanoniczną (nawet gdy link się nie uda) — rodzeństwo
            # tej samej gry wskazuje tę samą ścieżkę i nie ma się powielać
            self._canonical.add(os.path.normcase(str(canonical)))
            if self._symlink(canonical, claimed):
                self.stats.linked += 1
            else:
                # ZASADA: gdy symlinku NIE DA SIĘ utworzyć — nie robimy NIC.
                # Żadnych kopii (duplikaty zapychały dysk); w tym miejscu po
                # prostu nie ma pliku. Uruchom jako administrator, by dołożyć.
                self.stats.links_skipped += 1
            return

        # pierwszy DAT z tym plikiem => kopia fizyczna
        src = Path(s.source_path)

        # cała gra jako jedno archiwum (kartridż): przenieś całość albo
        # PRZEPAKUJ z poprawnymi nazwami wewnętrznymi (weryfikacja SHA-1/CRC).
        if s.via_archive:
            if self._place_archive(s, src, canonical):
                self._claims[key] = canonical
                self._canonical.add(os.path.normcase(str(canonical)))
            return

        if not self._clear_dest(canonical):
            self.stats.conflicts += 1
            self._log(f"KONFLIKT: {canonical} zajęte zwykłym plikiem")
            return

        if s.member:  # trafienie wewnątrz archiwum => wypakuj z weryfikacją
            self._log(f"WYPAKUJ {src} :: {s.member} -> {canonical}")
            if self._extract_member(src, s.member, canonical, s.rom):
                self.stats.unpacked += 1
                self._claims[key] = canonical
                self._canonical.add(os.path.normcase(str(canonical)))
            return

        # via_chd/via_archive: przenosimy CAŁY plik (chd/archiwum), którego
        # rozmiar != rozmiar pojedynczego ROM-a z DAT-a — pomijamy kontrolę.
        if not _size_ok(src, 0 if (s.via_chd or s.via_archive) else s.rom.size):
            self.stats.skipped += 1
            self._log(f"POMIŃ: plik zmienił się od skanu: {src}")
            return
        verb = "NAZWA " if state == RomState.WRONG_NAME else "PRZENIEŚ"
        self._log(f"{verb} {src} -> {canonical}")
        if self._move(src, canonical):
            if state == RomState.WRONG_NAME:
                self.stats.renamed += 1
            else:
                self.stats.moved += 1
            self._claims[key] = canonical
            self._canonical.add(os.path.normcase(str(canonical)))

    def _place_archive(self, s: RomStatus, src: Path, dest: Path) -> bool:
        """Umieszcza całą grę-archiwum pod ścieżką kanoniczną.

        - nazwy wewnętrzne poprawne + zgodny format => proste PRZENIESIENIE
          (bez rekompresji, zawartość już zweryfikowana po sumie przy skanie);
        - złe nazwy wewnętrzne => PRZEPAKOWANIE ZIP-a z poprawnymi nazwami i
          weryfikacją SHA-1/CRC każdego pliku (naprawa „dobry plik, zła nazwa
          w archiwum"). Nic ze złą zawartością nie ląduje pod docelową nazwą.
        """
        same = os.path.normcase(str(src)) == os.path.normcase(str(dest))
        simple = (s.archive_names_ok
                  and src.suffix.lower() == dest.suffix.lower())
        if simple:
            if same:
                self.stats.already_ok += 1
                return True
            if not self._clear_dest(dest):
                self.stats.conflicts += 1
                self._log(f"KONFLIKT: {dest} zajęte zwykłym plikiem")
                return False
            self._log(f"PRZENIEŚ archiwum {src} -> {dest}")
            if self._move(src, dest):
                self.stats.moved += 1
                return True
            return False

        # przepakowanie umiemy tylko do ZIP-a; inny cel => zwykłe przeniesienie
        if dest.suffix.lower() != ".zip":
            if same:
                self.stats.already_ok += 1
                return True
            if not self._clear_dest(dest):
                self.stats.conflicts += 1
                return False
            self._log(f"PRZENIEŚ archiwum {src} -> {dest}")
            if self._move(src, dest):
                self.stats.moved += 1
                return True
            return False

        game = next((g for g in getattr(s.entry, "games", []) or []
                     if g.name == s.game), None)
        if game is None:
            self.stats.errors += 1
            self._log(f"BŁĄD przepakowania: brak gry {s.game} w DAT-cie")
            return False
        if not same and not self._clear_dest(dest):
            self.stats.conflicts += 1
            self._log(f"KONFLIKT: {dest} zajęte zwykłym plikiem")
            return False
        self._log(f"PRZEPAKUJ {src.name} -> {dest.name} (poprawne nazwy wewn.)")
        if not self._rebuild_zip(s.source_path, src, dest, game.roms):
            return False
        self.stats.renamed += 1
        if not same and not self.dry_run:      # usuń stare (błędnie nazwane) źródło
            try:
                _retry_locked(lambda: os.unlink(src))
                self.index.remove_path(src)
            except OSError as e:
                self._log(f"UWAGA: nie usunięto starego archiwum {src}: {e}")
        return True

    def _rebuild_zip(self, archive_key: str, src: Path, dest: Path,
                     game_roms) -> bool:
        """Buduje ZIP `dest` z POPRAWNYMI nazwami wewnętrznymi. Dane bierze z
        `src` dopasowując po SUMIE (nie nazwie) i weryfikuje SHA-1/CRC każdego
        pliku przed zapisem. Zła zawartość => przerwanie (nic nie powstaje)."""
        import hashlib
        import zipfile
        import zlib
        self._ensure_dir(dest.parent)
        if self.dry_run:
            return True
        tmp = dest.with_name(dest.name + ".chdbuddy_rebuild_tmp")
        try:
            with zipfile.ZipFile(src) as zin, \
                    zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED,
                                    compresslevel=self._zip_level) as zout:
                for rom in game_roms:
                    member = self.index.member_name_in(
                        archive_key, rom.sha1, rom.crc, rom.size)
                    if member is None:
                        raise KeyError(f"brak {rom.name} w {src.name}")
                    data = zin.read(member)
                    if rom.sha1 and hashlib.sha1(data).hexdigest() != rom.sha1.lower():
                        raise ValueError(f"SHA-1 nie zgadza się: {rom.name}")
                    if rom.crc and (f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
                                    != rom.crc.lower().zfill(8)):
                        raise ValueError(f"CRC nie zgadza się: {rom.name}")
                    zout.writestr(rom.name, data)   # POPRAWNA nazwa wewnętrzna
        except Exception as e:
            self.stats.errors += 1
            self._log(f"BŁĄD przepakowania {src.name}: {e}")
            tmp.unlink(missing_ok=True)
            return False
        try:
            _retry_locked(lambda: os.replace(tmp, dest))
        except OSError as e:
            self.stats.errors += 1
            self._log(f"BŁĄD zapisu {dest}: {e}")
            tmp.unlink(missing_ok=True)
            return False
        try:
            self.index.reindex_archive(dest, full=True)
        except OSError as e:
            self._log(f"UWAGA: nie zaindeksowano {dest}: {e}")
        return True

    def _extract_member(self, archive: Path, member: str, dst: Path,
                        rom) -> bool:
        """Wypakowuje plik z archiwum (zip/7z) do pliku tymczasowego, liczy
        sumy i weryfikuje z DAT-em przed podmianą pod docelową nazwę.
        Archiwum źródłowe zostaje nietknięte."""
        self._ensure_dir(dst.parent)
        if self.dry_run:
            return True
        from .fileindex import hash_file
        tmp = dst.with_name(dst.name + ".chdbuddy_extract_tmp")
        self._detail(0, 0, f"wypakowuję {dst.name}")
        try:
            if archive.suffix.lower() == ".7z":
                self._extract_7z(archive, member, tmp)
            else:
                self._extract_zip(archive, member, tmp, self._detail_cb)
        except Exception as e:
            self.stats.errors += 1
            self._log(f"BŁĄD wypakowania {archive}::{member}: {e}")
            tmp.unlink(missing_ok=True)
            return False
        finally:
            self._detail_clear()
        try:
            crc_hex, md5_hex, sha1_hex = hash_file(tmp)
            size = os.path.getsize(tmp)
        except OSError as e:
            self.stats.errors += 1
            self._log(f"BŁĄD odczytu {tmp}: {e}")
            tmp.unlink(missing_ok=True)
            return False
        ok = ((not rom.size or size == rom.size)
              and (not rom.sha1 or sha1_hex == rom.sha1.lower())
              and (not rom.md5 or md5_hex == rom.md5.lower())
              and (not rom.crc or crc_hex == rom.crc.lower().zfill(8)))
        if not ok:
            self.stats.errors += 1
            self._log(f"WERYFIKACJA NIEUDANA: {archive}::{member} "
                      f"(sha1 {sha1_hex[:12]}… != DAT) — pomijam")
            tmp.unlink(missing_ok=True)
            return False
        try:
            _retry_locked(lambda: os.replace(tmp, dst))
        except OSError as e:
            self.stats.errors += 1
            self._log(f"BŁĄD zapisu {dst}: {e}")
            tmp.unlink(missing_ok=True)
            return False
        self.index.record_file(dst, crc_hex, md5_hex, sha1_hex)
        return True

    @staticmethod
    def _extract_zip(archive: Path, member: str, tmp: Path,
                     on_progress=None) -> None:
        import zipfile
        with zipfile.ZipFile(archive) as zf:
            total = zf.getinfo(member).file_size
            label = f"wypakowuję {tmp.name}"
            done = 0
            with zf.open(member) as fh, open(tmp, "wb") as out:
                if on_progress is not None:
                    on_progress(0, total, label)
                while True:
                    buf = fh.read(4 * 1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
                    done += len(buf)
                    if on_progress is not None:
                        on_progress(done, total, label)

    @staticmethod
    def _extract_7z(archive: Path, member: str, tmp: Path) -> None:
        import tempfile

        import py7zr
        with tempfile.TemporaryDirectory(dir=str(tmp.parent),
                                         prefix="chdbuddy_7z_") as td:
            with py7zr.SevenZipFile(archive) as zf:
                zf.extract(path=td, targets=[member])
            src = Path(td) / member
            if not src.is_file():
                raise FileNotFoundError(f"7z nie wypakował '{member}'")
            os.replace(src, tmp)

    # --- sprzątanie nieznanych do ToSort -------------------------------------

    def _clean_dir(self, target_dir: Path) -> None:
        assert self.tosort is not None
        for row in self.index.all_under(target_dir, physical_only=False):
            if os.path.normcase(row["path"]) in self._canonical:
                continue
            self._clean_done = getattr(self, "_clean_done", 0) + 1
            self._phase(self._clean_done, getattr(self, "_clean_total", 1),
                        "sprzątanie do ToSort")
            src = Path(row["path"])
            # LINK, który nie jest kanoniczną ścieżką żadnej dopasowanej gry
            # (np. błędny link 5S→1S sprzed poprawki odcisków) — USUŃ; linki
            # tworzy wyłącznie program, więc nieuzasadniony = śmieć.
            if is_link(src):
                self._log(f"USUŃ zły link: {src}")
                if not self.dry_run:
                    try:
                        remove_link(src)
                        self.index.remove_path(src)
                    except OSError as e:
                        self._log(f"  nie usunięto: {e}")
                continue
            if not src.is_file():
                continue
            rel = src.relative_to(target_dir)
            dst = self.tosort / target_dir.name / rel
            # nie nadpisuj niczego w ToSort — dołóż licznik
            n = 1
            while os.path.lexists(dst):
                dst = dst.with_name(f"{dst.stem}_{n}{dst.suffix}")
                n += 1
            self._log(f"TOSORT {src} -> {dst}")
            if self._move(src, dst):
                self.stats.tosorted += 1

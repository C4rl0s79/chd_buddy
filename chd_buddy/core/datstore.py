"""Magazyn DAT-ów — odpowiednik DatRoot/RomRoot z RomVaulta.

Drzewo katalogu z DAT-ami (dat_root) odwzorowuje się na drzewo katalogów
ROM-ów (rom_root): DAT leżący w ``dat_root/PS2/foo.dat`` dostaje katalog
docelowy ``rom_root/PS2/<nazwa>`` , gdzie <nazwa> to pole Name z nagłówka
DAT-a (fallback: nazwa pliku bez rozszerzenia). To wariant „automatycznie
wg tagu Name" z RomVaulta — najpopularniejszy.

Kolejność DAT-ów wyznacza hierarchię rodzic→dzieci przy plikach wspólnych:
pierwszy DAT z trafieniem trzyma plik fizycznie, kolejne dostają symlinki.

Priorytet parent→child dotyczy TYLKO DAT-ów tej samej PLATFORMY (te same
pliki mogą być w wielu DAT-ach jednej platformy — pełny Redump, 1G1R,
tłumaczenia). DAT-y różnych platform (PS2 vs Saturn vs N64) nigdy nie
kolidują, więc ich wzajemna kolejność jest bez znaczenia.

Priorytet w obrębie platformy (od najważniejszego):
1. Ręczny — plik ``_priorytet.txt`` w katalogu DAT-ów: jedna nazwa DAT-a
   (pliku albo z nagłówka) na linię, kolejność = hierarchia; ``#`` komentarz.
2. Domyślny — DAT tej platformy z WIĘKSZĄ liczbą ROM-ów jest nadrzędny
   (główna biblioteka, np. pełny Redump), mniejsze zestawy (1G1R/Retool/
   tłumaczenia) linkują z niego.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List

from .datfile import DatGame, parse_dat, parse_dat_header


def platform_key(name: str) -> str:
    """Nazwa DAT-a → klucz PLATFORMY (bez wariantów), np.
    'Sony - PlayStation 2 - Datfile (11719)' i
    'Sony - PlayStation 2 (Redump - Fresh1G1R - PropeR)' → 'sony playstation 2'.
    Grupuje pełny Redump, 1G1R, Retool i tłumaczenia tej samej platformy."""
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", name)         # usuń (…) i […]
    s = re.sub(r"\b(?:Datfile|Collection|Retool)\b", " ", s, flags=re.I)
    s = re.sub(r"[-_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass
class DatEntry:
    """Jeden DAT + jego katalog docelowy w drzewie ROM-ów."""
    dat_path: Path
    name: str
    target_dir: Path
    # Gry wieloplikowe (bin/cue, gdi+tracki) luzem => podkatalog per gra.
    # CHD i archiwa są zawsze płasko. Wybór regułą `subdir_per_game`.
    subdir_per_game: bool = True
    # Rozwiązany format przechowywania (keep/extract/zip/7z/chd/rvz). Ustala
    # apply_rule_targets — dzieci dziedziczą format RODZICA (są symlinkami do
    # jego plików, więc muszą mieć ten sam kontener).
    store_format: str = "keep"
    games: List[DatGame] = field(default_factory=list, repr=False)

    @property
    def rom_count(self) -> int:
        return sum(len(g.roms) for g in self.games)

    def load(self) -> "DatEntry":
        if not self.games:
            self.games = list(parse_dat(self.dat_path))
        return self


def _safe_dirname(name: str) -> str:
    """Nazwa z nagłówka DAT-a jako poprawna nazwa katalogu Windows."""
    bad = '<>:"/\\|?*'
    out = "".join((c if c not in bad else "-") for c in name).strip(" .")
    return out or "unnamed"


PRIORITY_FILENAME = "_priorytet.txt"


def save_priority(dat_root: Path, names: list[str]) -> Path:
    """Zapisuje _priorytet.txt (nazwy DAT-ów w kolejności = hierarchia).
    Ważna jest tylko kolejność W OBRĘBIE platformy."""
    p = Path(dat_root) / PRIORITY_FILENAME
    body = ("# Hierarchia DAT-ów per platforma (góra = rodzic, trzyma pliki\n"
            "# fizycznie; niżej = dzieci, dostają symlinki). Kolejność między\n"
            "# platformami bez znaczenia. Plik generowany z GUI.\n"
            + "\n".join(names) + "\n")
    p.write_text(body, encoding="utf-8")
    return p


def effective_platform_key(entry, rules=None) -> str:
    """Klucz platformy DAT-a z uwzględnieniem RĘCZNEGO przypięcia (reguła
    `platform` w _reguly.json). Alias może być kluczem platformy albo nazwą
    DAT-a rodzica — normalizujemy przez platform_key. Tak „FinalBurn Neo -
    SNES Games" może być dzieckiem platformy Nintendo SNES mimo innej nazwy."""
    if rules is not None:
        alias = rules.for_entry(entry).get("platform", "")
        if alias:
            return platform_key(str(alias))
    return platform_key(entry.name)


def group_by_platform(entries: list, rules=None) -> "dict[str, list]":
    """Grupuje wpisy po platformie (z aliasami z reguł), zachowując w każdej
    grupie kolejność priorytetu (rodzic pierwszy) z discover()."""
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(effective_platform_key(e, rules), []).append(e)
    return groups


class DatStore:
    """Odkrywa DAT-y w dat_root i mapuje je na katalogi w rom_root."""

    def __init__(self, dat_root: Path, rom_root: Path, use_cache: bool = True):
        self.dat_root = Path(os.path.abspath(dat_root))
        self.rom_root = Path(os.path.abspath(rom_root))
        self.use_cache = use_cache

    def _manual_priority(self) -> list[str]:
        p = self.dat_root / PRIORITY_FILENAME
        if not p.is_file():
            return []
        out = []
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line.lower())
        except OSError:
            pass
        return out

    def _dedupe_collisions(self, files: list[Path], log) -> list[Path]:
        """Identyczna treść DAT-a w kilku miejscach => używamy JEDNEJ kopii.

        Optymalizacja: dwa pliki mogą być identyczne tylko przy TYM SAMYM
        rozmiarze. Grupujemy po rozmiarze i hashujemy WYŁĄCZNIE pliki z
        kolidującym rozmiarem (większość DAT-ów ma unikalny rozmiar — zero
        czytania). Wygrywa kopia głębiej w strukturze; pliki nietknięte.
        """
        import hashlib
        by_size: dict[int, list[Path]] = {}
        for f in files:
            try:
                by_size.setdefault(f.stat().st_size, []).append(f)
            except OSError:
                continue

        def _sha1(p: Path) -> str:
            h = hashlib.sha1()
            with open(p, "rb") as fh:
                while True:
                    b = fh.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
            return h.hexdigest()

        chosen: list[Path] = []
        for size, group in by_size.items():
            if len(group) == 1:
                chosen.append(group[0])          # unikalny rozmiar — nie hashuj
                continue
            by_hash: dict[str, list[Path]] = {}
            for f in group:
                try:
                    by_hash.setdefault(_sha1(f), []).append(f)
                except OSError:
                    continue
            for paths in by_hash.values():
                win = min(paths, key=lambda p: (-len(p.parts), str(p).lower()))
                chosen.append(win)
                if log:
                    for p in paths:
                        if p != win:
                            log(f"KOLIZJA DAT (identyczna treść): "
                                f"{p.relative_to(self.dat_root)} — używam "
                                f"{win.relative_to(self.dat_root)}")
        return sorted(chosen)

    def discover(self, log=None, on_progress=None) -> list[DatEntry]:
        """Znajduje wszystkie *.dat (rekurencyjnie) i buduje hierarchię.

        Kolizje (identyczna treść w różnych katalogach) są scalane do jednej
        kopii. Kolejność wyniku wyznacza priorytet parent→child W OBRĘBIE
        PLATFORMY: DAT-y są grupowane po platformie (platform_key), a wewnątrz
        grupy większy (pełny Redump) jest przed mniejszymi (1G1R/tłumaczenia),
        chyba że ``_priorytet.txt`` mówi inaczej. Platformy nie kolidują między
        sobą, więc ich wzajemna kolejność jest bez znaczenia (tu: po nazwie
        platformy). Wczytuje DAT-y w całości (liczba ROM-ów).
        """
        if not self.dat_root.is_dir():
            raise NotADirectoryError(f"'{self.dat_root}' nie jest katalogiem")
        files = [f for f in sorted(self.dat_root.rglob("*.dat")) if f.is_file()]
        files = self._dedupe_collisions(files, log)

        cache = None
        cached = reparsed = 0
        if self.use_cache:
            from .datcache import DatParseCache
            cache = DatParseCache()

        entries: list[DatEntry] = []
        for i, dat in enumerate(files):
            if on_progress:
                on_progress(i, len(files), dat.name)
            if cache is not None:
                had = cache.get(dat) is not None
                raw_name, games = cache.parse(dat)     # z cache albo parsuje
                cached += 1 if had else 0
                reparsed += 0 if had else 1
            else:
                raw_name = parse_dat_header(dat).get("name") or dat.stem
                games = list(parse_dat(dat))
            name = _safe_dirname(raw_name)
            rel = dat.parent.relative_to(self.dat_root)
            e = DatEntry(dat_path=dat, name=name,
                         target_dir=self.rom_root / rel / name)
            e.games = games                            # już sparsowane
            entries.append(e)

        if cache is not None:
            cache.prune({str(Path(os.path.abspath(f))) for f in files})
            cache.save()
            if log:
                log(f"Cache DAT-ów: z cache {cached}, sparsowano od nowa "
                    f"{reparsed} (plik: {cache.path}).")

        manual = self._manual_priority()

        def _manual_rank(e: DatEntry) -> int:
            keys = (e.dat_path.name.lower(), e.dat_path.stem.lower(),
                    e.name.lower())
            for i, want in enumerate(manual):
                if want in keys:
                    return i
            return len(manual)

        # reguła parent_priority (folder oznaczony „wszystkie rodzicami")
        from .dirrules import DirRules
        rules = DirRules(self.dat_root)

        def _parent_rank(e: DatEntry) -> int:
            return 0 if rules.for_entry(e).get("parent_priority") else 1

        # RODZICE ZAWSZE PIERWSI — GLOBALNIE, nie tylko w obrębie platformy.
        # Fizyczną kopię pliku dostaje DAT przetworzony jako pierwszy, więc
        # gdyby platform_key szedł przed rolą, DAT-dziecko innej „platformy"
        # (np. „FinalBurn Neo - SNES Games" vs „Nintendo - SNES") zabierałby
        # plik fizyczny, a katalog RODZICA dostawał symlink — a w katalogu
        # rodzica mają być ZAWSZE pliki fizyczne.
        # Dalej: platforma (Z ALIASAMI z reguły `platform` — ręczne przypięcia
        # child→parent) → ręczny _priorytet.txt → większy DAT. W obrębie
        # platformy rodzic nadal wypada przed dziećmi (ranga 0 < 1), więc
        # grupowanie i dziedziczenie formatu działają jak dotąd.
        entries.sort(key=lambda e: (_parent_rank(e),
                                    effective_platform_key(e, rules),
                                    _manual_rank(e), -e.rom_count,
                                    str(e.target_dir).lower()))
        return entries

    def iter_loaded(self) -> Iterator[DatEntry]:
        for e in self.discover():
            yield e.load()

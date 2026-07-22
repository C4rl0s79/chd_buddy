"""Parser plików DAT (Logiqx XML, format Redump/No-Intro) + indeks po hashu.

Do walidacji naprawionych obrazów: po wypakowaniu obrazu z CHD liczymy jego
SHA-1 i sprawdzamy, czy odpowiada znanemu, poprawnemu zrzutowi w DAT. Trafienie
= dane są kanonicznie poprawne (nie tylko „kontener się rozpakowuje").

Klasyfikacja nośnika wg rozszerzeń ROM-ów w grze:
  * .iso                -> DVD
  * .bin / .cue / .gdi  -> CD
Reguła kciuka Redump PS2: gra z plikami bin/cue to obraz CD, z .iso to DVD.

DAT-y bywają duże (dziesiątki tysięcy wpisów), więc używamy iterparse i
czyścimy elementy w locie, żeby nie trzymać całego drzewa w pamięci.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .models import MediaType


@dataclass
class DatRom:
    name: str
    size: int = 0
    crc: str = ""
    md5: str = ""
    sha1: str = ""


@dataclass
class DatGame:
    name: str
    roms: List[DatRom] = field(default_factory=list)

    @property
    def media(self) -> MediaType:
        exts = {Path(r.name).suffix.lower().lstrip(".") for r in self.roms}
        if "iso" in exts:
            return MediaType.DVD
        if exts & {"bin", "cue", "gdi"}:
            return MediaType.CD
        return MediaType.UNKNOWN

    @property
    def data_roms(self) -> List[DatRom]:
        """ROM-y niosące dane (bez samego .cue/.gdi)."""
        return [r for r in self.roms
                if Path(r.name).suffix.lower() not in (".cue", ".gdi")]


@dataclass
class DatMatch:
    game: str
    media: MediaType
    rom_name: str


def game_profile(roms) -> str:
    """ODCISK CAŁEJ GRY: sha1 z połączonych sum wszystkich ścieżek danych.

    Jedna ścieżka => jej własny sha1 (kompatybilne z nagłówkiem DVD-CHD).
    Wiele ścieżek => syntetyczny hash listy sum W KOLEJNOŚCI. Dwa wydania
    tej samej gry (np. Panzer Dragoon 1S vs 5S) potrafią dzielić ścieżkę
    DANYCH i różnić się tylko audio — pojedyncza suma NIE identyfikuje gry,
    komplet ścieżek tak."""
    import hashlib as _h
    sums = [r.sha1.lower() for r in roms if r.sha1]
    if not sums:
        return ""
    if len(sums) == 1:
        return sums[0]
    return _h.sha1(",".join(sums).encode("ascii")).hexdigest()


class DatIndex:
    """Indeks wielu DAT-ów po SHA-1 (a także MD5/CRC) danych ROM-ów."""

    def __init__(self) -> None:
        self.by_sha1: Dict[str, DatMatch] = {}
        self.by_md5: Dict[str, DatMatch] = {}
        self.by_crc: Dict[str, DatMatch] = {}
        self.games: int = 0
        self.roms: int = 0
        # suma rozmiarów ścieżek gry -> [(nazwa, [(rozmiar, sha1), …])].
        # Do identyfikacji SKLEJONYCH obrazów multi-track (CHD zrobione ze
        # złym/bez cue — układ ścieżek przepadł): tniemy obraz wg rozmiarów
        # z DAT-a i weryfikujemy sha1 każdej ścieżki.
        self.size_profiles: Dict[int, list] = {}
        # ODCISK CAŁEJ GRY (game_profile) -> DatMatch. Identyfikacja CHD musi
        # trafiać w KOMPLET ścieżek, nie pojedynczą sumę (1S vs 5S!).
        self.by_profile: Dict[str, DatMatch] = {}

    def add_game(self, game: DatGame) -> None:
        self.games += 1
        media = game.media
        for r in game.roms:
            self.roms += 1
            m = DatMatch(game.name, media, r.name)
            if r.sha1:
                self.by_sha1[r.sha1.lower()] = m
            if r.md5:
                self.by_md5[r.md5.lower()] = m
            if r.crc:
                self.by_crc[r.crc.lower().zfill(8)] = m
        data = game.data_roms
        if len(data) > 1 and all(r.size > 0 and r.sha1 for r in data):
            total = sum(r.size for r in data)
            self.size_profiles.setdefault(total, []).append(
                (game.name, [(r.size, r.sha1.lower()) for r in data]))
        prof = game_profile(data)
        if prof:
            self.by_profile[prof] = DatMatch(
                game.name, media, data[0].name if data else "")

    def match_profile(self, profile: str) -> Optional[DatMatch]:
        """Trafienie po ODCISKU CAŁEJ GRY (komplet ścieżek)."""
        return self.by_profile.get((profile or "").lower())

    def match_sha1(self, sha1: str) -> Optional[DatMatch]:
        return self.by_sha1.get(sha1.lower())

    def match_md5(self, md5: str) -> Optional[DatMatch]:
        return self.by_md5.get(md5.lower())

    def load(self, path: Path) -> "DatIndex":
        for game in parse_dat(path):
            self.add_game(game)
        return self

    def load_many(self, paths: List[Path]) -> "DatIndex":
        for p in paths:
            self.load(p)
        return self

    @classmethod
    def from_paths(cls, paths: List[Path]) -> "DatIndex":
        idx = cls()
        expanded: List[Path] = []
        for p in paths:
            if p.is_dir():
                expanded.extend(sorted(p.glob("*.dat")))
            elif p.is_file():
                expanded.append(p)
        return idx.load_many(expanded)


def parse_dat_header(path: Path) -> dict:
    """Czyta nagłówek DAT-a (Logiqx <header>): name, description, version.

    Przerywa parsowanie po nagłówku — nie czyta całego pliku.
    """
    out = {"name": "", "description": "", "version": ""}
    try:
        for _event, elem in ET.iterparse(str(path), events=("end",)):
            if elem.tag == "header":
                for key in out:
                    node = elem.find(key)
                    if node is not None and node.text:
                        out[key] = node.text.strip()
                break
            if elem.tag in ("game", "machine"):
                break  # brak nagłówka — nie czytaj dalej
    except ET.ParseError:
        pass
    return out


def parse_dat(path: Path):
    """Generator gier z pliku DAT (Logiqx XML). Odporny na duże pliki."""
    context = ET.iterparse(str(path), events=("end",))
    for _event, elem in context:
        if elem.tag != "game" and elem.tag != "machine":
            continue
        name = elem.get("name", "")
        roms: List[DatRom] = []
        for r in elem.findall("rom"):
            try:
                size = int(r.get("size", "0") or 0)
            except ValueError:
                size = 0
            roms.append(DatRom(
                name=r.get("name", ""),
                size=size,
                crc=(r.get("crc") or "").strip(),
                md5=(r.get("md5") or "").strip(),
                sha1=(r.get("sha1") or "").strip(),
            ))
        if roms:
            yield DatGame(name=name, roms=roms)
        elem.clear()  # zwolnij pamięć

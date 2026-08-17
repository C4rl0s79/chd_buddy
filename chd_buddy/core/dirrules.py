"""Kaskadowe reguły per katalog — odpowiednik „DAT rules" z RomVaulta.

Plik ``_reguly.json`` w katalogu DAT-ów (obok ``_priorytet.txt``):

    {
      "*":                      {"only_complete": true},
      "PS2":                    {"only_complete": false},
      "Sony - PlayStation 2":   {"skip": true},
      "stary_zestaw.dat":       {"skip": true}
    }

Klucz to (od najogólniejszego): "*", ścieżka katalogu DAT-a względem
dat_root (np. "PS2" albo "konsole/PS2"), nazwa DAT-a z nagłówka, nazwa
pliku .dat. Reguły nakładają się kaskadowo — bardziej szczegółowy klucz
NADPISUJE ogólniejszy (jak w RomVaulcie: „descendant wins").

Obsługiwane reguły:
  only_complete  (bool) — buduj tylko kompletne gry (domyślnie true),
  skip           (bool) — pomiń DAT całkowicie (nie raportuj, nie buduj),
  dedup_copies   (bool) — kopie potwierdzonych plików → symlinki (domyślnie true),
  target         (str)  — katalog docelowy DAT-a względem rom_root (zamiast
                          nazwy z nagłówka), np. "ps2" dla układu
                          EmulationStation/RetroBat.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

RULES_FILENAME = "_reguly.json"

# Docelowy format przechowywania plików gry (dotyczy DAT-a RODZICA — dzieci
# dostają symlinki). "keep" = nie konwertuj; "auto" = wg typu systemu
# (płyta→CHD, GameCube/Wii→RVZ, kartridż→zostaw/ZIP).
FORMATS = ("keep", "auto", "extract", "zip", "7z", "chd", "rvz")

# Konwencja nazw katalogów per system:
#   "dat" — z pola <header><name> DAT-a (np. "Sony - PlayStation 2")
#   "es"  — EmulationStation/Batocera (np. "ps2", "psx", "dreamcast")
NAMINGS = ("dat", "es")

DEFAULT_RULES: dict[str, Any] = {
    "only_complete": True,
    "skip": False,
    "dedup_copies": True,
    "target": "",
    # Gry wieloplikowe luzem w podkatalogu per gra (CHD/zipy zawsze płasko).
    "subdir_per_game": True,
    # Podmieniaj wersje (Japan) na fanowskie tłumaczenia (T-En) z innych DAT-ów.
    "prefer_translations": False,
    # ROLA DAT-u: "collection" (zwykły cel: parent/child) albo "translations"
    # (pula fanowskich tłumaczeń — nie cel podstawowy, dostarcza wariantów do
    # podmiany innych DAT-ów; patrz core/translations.py).
    "role": "collection",
    # Format przechowywania (patrz FORMATS).
    "format": "keep",
    # Konwencja nazw katalogów per system (patrz NAMINGS).
    "naming": "dat",
    # Nadpisanie bazowego katalogu ROM-ów (pusty = główny rom_root). Pozwala
    # np. dzieciom platformy lądować na innym dysku/roocie niż rodzic.
    "rom_root": "",
    # DAT-y tego katalogu są RODZICAMI swoich platform (prawy klik na folderze).
    "parent_priority": False,
    # RĘCZNE przypięcie DAT-a do innej platformy (klucz platform_key albo
    # nazwa DAT-a rodzica). Np. "FinalBurn Neo - SNES Games" →
    # "nintendo super nintendo entertainment system": DAT staje się DZIECKIEM
    # tej platformy (hierarchia, dziedziczenie formatu), mimo innej nazwy.
    "platform": "",
}

# Skrót platformy → katalog EmulationStation/Batocera.
ES_FOLDER: dict[str, str] = {
    "PS1": "psx", "PS2": "ps2", "PS3": "ps3", "PSP": "psp", "PSVITA": "psvita",
    "DC": "dreamcast", "SATURN": "saturn", "MD": "megadrive",
    "SMS": "mastersystem", "GG": "gamegear", "NAOMI": "naomi",
    "GCN": "gamecube", "WII": "wii", "WIIU": "wiiu", "NSW": "switch",
    "N64": "n64", "SNES": "snes", "NES": "nes", "GB": "gb", "GBC": "gbc",
    "GBA": "gba", "NDS": "nds", "3DS": "3ds", "ARCADE": "arcade",
    "MAME": "mame", "NEOGEO": "neogeo", "XBOX": "xbox", "X360": "xbox360",
    "ATARI2600": "atari2600", "ATARI7800": "atari7800", "3DO": "3do",
    "PCENGINE": "pcengine",
    "NAOMI2": "naomi2", "SEGACD": "segacd", "JAGUAR": "atarijaguar",
    "ATARI5200": "atari5200", "LYNX": "lynx", "MSX": "msx", "MSX2": "msx2",
    "FBNEO": "fbneo", "AMIGA": "amiga", "C64": "c64", "PC98": "pc98",
    "SG1000": "sg1000", "SUPERGRAFX": "supergrafx", "PC88": "pc88",
    "PCFX": "pcfx", "FDS": "fds", "GAMEANDWATCH": "gameandwatch",
    "SATELLAVIEW": "satellaview", "SUFAMI": "sufami", "SEGA32X": "sega32x",
    "JAGUARCD": "atarijaguarcd", "AMIGACD32": "amigacd32",
    "ODYSSEY2": "odyssey2", "INTELLIVISION": "intellivision",
    "NEOGEOCD": "neogeocd",
}

# Systemy PŁYTOWE (format auto → CHD, poza GameCube/Wii → RVZ).
DISC_SYSTEMS = {"PS1", "PS2", "PS3", "PSP", "SATURN", "DC", "NAOMI", "3DO",
                "PCENGINE", "NEOGEOCD", "SEGACD", "MEGACD", "GCN", "WII"}


def save_rule(dat_root: Path, key: str, updates: dict, *,
              strip_defaults: bool = True) -> Path:
    """Zapisuje/aktualizuje regułę dla `key` (nazwa DAT-a/platforma/katalog)
    w _reguly.json, zachowując pozostałe wpisy.

    strip_defaults=True (domyślnie): wartości równe globalnym domyślnym są
    usuwane (plik czytelny) — dla reguł BAZOWYCH (folder/globalne).
    strip_defaults=False: zapisujemy PODANE wartości nawet gdy równe domyślnym
    (poza pustym target/rom_root = brak nadpisania) — POJEDYNCZE/zbiorcze
    nadpisania muszą przetrwać, bo reguła katalogu może ustawić wartość
    inną niż domyślna (np. folder=auto, a pojedynczy DAT ma być „keep")."""
    p = Path(dat_root) / RULES_FILENAME
    data: dict = {}
    if p.is_file():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    rule = dict(data.get(key, {}))
    for name, val in updates.items():
        if strip_defaults:
            drop = val == DEFAULT_RULES.get(name)
        else:                             # zostaw jawne; usuń tylko puste ścieżki
            drop = name in _STR_RULES and name in ("target", "rom_root") and not val
        if drop:
            rule.pop(name, None)
        else:
            rule[name] = val
    if rule:
        data[key] = rule
    else:
        data.pop(key, None)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                 encoding="utf-8")
    return p


# Sugerowany format przechowywania wg skrótu platformy (płytowe → CHD;
# GameCube/Wii → RVZ; reszta → keep/zip).
def suggest_format(system_short: str) -> str:
    s = (system_short or "").upper()
    if s in ("GCN", "WII"):
        return "rvz"
    disc = {"PS1", "PS2", "PS3", "PSP", "SATURN", "DC", "NAOMI", "MEGACD",
            "SEGACD", "PCECD", "3DO", "NEOGEOCD"}
    if s in disc:
        return "chd"
    return "keep"


# Reguły tekstowe (reszta jest boolowska).
_STR_RULES = {"target", "format", "naming", "rom_root", "platform", "role"}


def _coerce(name: str, value):
    return str(value) if name in _STR_RULES else bool(value)


def _system_short(entry) -> str:
    """Skrót platformy (PS2/DC/…) z nazwy DAT-a — do ES/format."""
    from .icons import clean_system_name
    from .shortcuts import detect_system
    clean = clean_system_name(entry.name)
    return detect_system(clean) or detect_system(entry.name) or ""


def folder_name(entry, naming: str) -> str:
    """Nazwa katalogu docelowego per system wg konwencji."""
    if naming == "es":
        short = _system_short(entry)
        es = ES_FOLDER.get(short)
        if es:
            return es
    return entry.name          # domyślnie nazwa z <header><name>


def resolve_format(fmt: str, entry) -> str:
    """Rozwiązuje format 'auto' na konkret wg typu systemu."""
    if fmt != "auto":
        return fmt
    short = _system_short(entry)
    if short in ("GCN", "WII"):
        return "rvz"
    if short in DISC_SYSTEMS:
        return "chd"
    return "zip"               # kartridż → ZIP


class DirRules:
    def __init__(self, dat_root: Path):
        self.dat_root = Path(os.path.abspath(dat_root))
        self.raw: dict[str, dict] = {}
        self.error = ""
        p = self.dat_root / RULES_FILENAME
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.raw = {str(k).lower(): v for k, v in data.items()
                                if isinstance(v, dict)}
            except (OSError, ValueError) as e:
                self.error = f"{RULES_FILENAME}: {e}"

    def _entry_keys(self, entry) -> list[str]:
        """Klucze kaskady dla DAT-a, od najogólniejszego do najszczegółowszego
        (global „*" → katalogi → nazwa z nagłówka → plik .dat)."""
        keys: list[str] = ["*"]
        try:
            rel = entry.dat_path.parent.relative_to(self.dat_root)
        except ValueError:
            rel = Path()
        # kolejne poziomy katalogów: "konsole", "konsole/PS2", …
        parts = [p for p in rel.parts]
        for i in range(1, len(parts) + 1):
            keys.append("/".join(parts[:i]).lower())
        keys.append(entry.name.lower())
        keys.append(entry.dat_path.name.lower())
        keys.append(entry.dat_path.stem.lower())
        return keys

    def for_entry(self, entry) -> dict[str, Any]:
        """Efektywne reguły dla DAT-a (DatEntry) — kaskada ogólne→szczegółowe."""
        eff = dict(DEFAULT_RULES)
        for k in self._entry_keys(entry):
            rule = self.raw.get(k)
            if rule:
                for name in DEFAULT_RULES:
                    if name in rule:
                        eff[name] = _coerce(name, rule[name])
        return eff

    def explicit_rule(self, entry, name: str):
        """Surowa wartość reguły `name` z kaskady (szczegółowy klucz wygrywa)
        albo None, gdy ŻADEN klucz jej nie ustawia. Odróżnia „nie ustawiono"
        od wartości domyślnej — do decyzji formatu per platforma."""
        val = None
        for k in self._entry_keys(entry):
            rule = self.raw.get(k)
            if rule and name in rule:
                val = _coerce(name, rule[name])
        return val


def missing_roots(entries, rules: DirRules, rom_root) -> list[tuple[str, str]]:
    """Katalogi bazowe (rom_root z reguł + główny), które NIE ISTNIEJĄ.

    Zabezpieczenie przed katastrofą: literówka w rom_root (np. „rom1" zamiast
    „roms") sprawia, że KAŻDY plik wygląda na „w złym miejscu" i naprawa
    zaczyna masowo przenosić całą kolekcję w nowe miejsce. Lepiej przerwać
    i zapytać, niż przenieść setki GB.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for e in entries:
        base = rules.for_entry(e).get("rom_root") or str(rom_root)
        key = os.path.normcase(base)
        if key in seen:
            continue
        seen.add(key)
        if not Path(base).is_dir():
            out.append((e.name, base))
    return out


def scan_roots(entries, rules: DirRules, rom_root, tosort=None) -> list[str]:
    """Katalogi, które MUSZĄ być przeskanowane: główny rom_root + KAŻDY
    katalog bazowy z reguły `rom_root` (bo tam realnie lądują pliki) + ToSort.

    Bez tego indeks nie widzi plików w katalogach wyprowadzonych regułą poza
    główny rom_root, a wpisy po plikach tam usuniętych zostają w bazie jako
    „obecne" (duchy) — matcher planuje wtedy przenosiny z nieistniejących
    ścieżek i naprawa nic nie robi.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(p) -> None:
        if not p:
            return
        path = Path(p)
        key = os.path.normcase(str(path))
        if key in seen or not path.is_dir():
            return
        seen.add(key)
        out.append(str(path))

    add(rom_root)
    for e in entries:
        add(rules.for_entry(e).get("rom_root"))
    # ToSort: pojedyncza ścieżka albo LISTA katalogów (RomVault pozwala na wiele)
    if isinstance(tosort, (list, tuple, set)):
        for t in tosort:
            add(t)
    else:
        add(tosort)
    return out


def apply_rule_targets(entries, rules: DirRules, rom_root, log=None) -> None:
    """Wylicza katalog docelowy każdego DAT-a z reguł (kaskada global→
    katalog→DAT):

    - `rom_root` (rule) nadpisuje bazę (np. inny dysk dla dzieci);
    - `target` (rule) = pełne przekierowanie względem bazy;
    - inaczej: baza / <katalog DAT-a względem dat_root> / <nazwa systemu>,
      gdzie nazwa wg `naming` (dat/es). Struktura katalogu DAT-a (1G1R/ROMS)
      rozdziela rodzica od dzieci; przy własnym rom_root rozdziela root.
    Ustawia też e.subdir_per_game.
    """
    from pathlib import Path

    from .datstore import effective_platform_key
    dat_root = rules.dat_root
    for e in entries:
        eff = rules.for_entry(e)
        base = Path(eff["rom_root"]) if eff.get("rom_root") else Path(rom_root)
        naming = eff.get("naming", "dat")
        if eff.get("target"):
            e.target_dir = base / eff["target"]
        elif naming == "es" or eff.get("rom_root"):
            # ES/RetroBat wymaga PŁASKIEGO układu: <rom_root>/<system> (np.
            # roms/gb, roms/ps2) — katalog-grupa DAT-ów (ROMS/1G1R) NIE wchodzi
            # do ścieżki fizycznej. Tak samo przy własnym rom_root (root dzieli).
            leaf = folder_name(e, naming)
            if log and naming == "es" and leaf == e.name:
                # brak mapowania ES => nazwa z DAT-a — GŁOŚNO, żeby mieszanina
                # konwencji nie była niespodzianką (dodaj alias w shortcuts.py)
                log(f"UWAGA naming=es: brak mapowania ES dla '{e.name}' — "
                    f"katalog dostanie nazwę z DAT-a")
            e.target_dir = base / leaf
        else:
            # naming=dat: struktura katalogów DAT-ów (1G1R/ROMS) mapuje się na
            # podkatalogi rom_root (rozdziela rodzica od dzieci).
            try:
                rel = e.dat_path.parent.relative_to(dat_root)
            except ValueError:
                rel = Path()
            e.target_dir = base / rel / folder_name(e, naming)
        e.subdir_per_game = bool(eff.get("subdir_per_game", True))
        e.store_format = resolve_format(eff.get("format", "keep"), e)

    # Format PER PLATFORMA: bierzemy JAWNĄ regułę formatu (folder/DAT)
    # KTÓREGOKOLWIEK DAT-a platformy — rodzic ma pierwszeństwo (entries są
    # parent-first). Dzięki temu ustawienie formatu na DOWOLNYM katalogu
    # platformy działa, a wszystkie DAT-y (rodzic+dzieci) dostają JEDEN, spójny
    # format (dzieci to symlinki do plików rodzica — ten sam kontener).
    platform_fmt: dict[str, str] = {}
    for e in entries:
        key = effective_platform_key(e, rules)        # z ręcznymi przypięciami
        exp = rules.explicit_rule(e, "format")        # None = brak reguły
        if key not in platform_fmt:
            platform_fmt[key] = resolve_format(exp, e) if exp else ""
        elif not platform_fmt[key] and exp:
            platform_fmt[key] = resolve_format(exp, e)  # format podaje dziecko
    for e in entries:
        fmt = platform_fmt.get(effective_platform_key(e, rules))
        if fmt:                                        # jawny format platformy
            e.store_format = fmt
        # inaczej zostaje wynik pierwszej pętli (brak reguły => keep)

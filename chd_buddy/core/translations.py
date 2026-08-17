"""Tłumaczenia fanowskie jako osobny TYP źródła (rola DAT-u „translations").

Dostarcza:
- parsowanie języka i etykiety z nazwy gry (`[T-En]`, `(En,Fr,De)`, `[T+Eng]`…),
- indeks WARIANTÓW tłumaczeń (tytuł bazowy → lista wariantów) z DAT-ów o roli
  `translations`,
- trwały wybór podmian (`translations.json`) — gra → tożsamość wariantu po SHA-1,
  będący ŹRÓDŁEM PRAWDY dla matchera (skan nie cofa świadomego wyboru),
- przepływ PODMIANY: oryginał kolekcji → `to sort\\translated\\<system>\\`,
  a pod NAZWĄ KANONICZNĄ gry powstaje symlink do pliku tłumaczenia; oraz
  ODTWORZENIE (restore) z powrotem.

V1: gry JEDNOPLIKOWE (kartridż / 1 CHD). Wieloplikowe (MSU-1, płyty) — później.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

LogCB = Callable[[str], None]


# --- parsowanie tytułu / języka ----------------------------------------------

def base_title(name: str) -> str:
    """Tytuł bez wszystkich tagów () i [] — do parowania gry z tłumaczeniem."""
    out = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", name)
    return re.sub(r"\s+", " ", out).strip().lower()


# normalizacja nazw języków → kod ISO-639-1 (na tyle, na ile potrzeba w nazwach)
_LANG_ALIASES = {
    "en": "en", "eng": "en", "english": "en",
    "fr": "fr", "fre": "fr", "french": "fr", "français": "fr", "francais": "fr",
    "de": "de", "ger": "de", "german": "de", "deutsch": "de",
    "es": "es", "spa": "es", "spanish": "es", "español": "es", "espanol": "es",
    "it": "it", "ita": "it", "italian": "it", "italiano": "it",
    "pt": "pt", "por": "pt", "portuguese": "pt", "português": "pt",
    "ja": "ja", "jp": "ja", "jpn": "ja", "japanese": "ja",
    "nl": "nl", "dutch": "nl",
    "sv": "sv", "swedish": "sv",
    "pl": "pl", "pol": "pl", "polish": "pl", "polski": "pl",
    "ru": "ru", "rus": "ru", "russian": "ru",
    "ko": "ko", "kor": "ko", "korean": "ko",
    "zh": "zh", "chi": "zh", "chinese": "zh",
    "ca": "ca", "catalan": "ca",
    "da": "da", "danish": "da",
    "no": "no", "norwegian": "no",
    "fi": "fi", "finnish": "fi",
}

# region → domyślny język (fallback, gdy w nazwie nie ma jawnego tagu języka).
# Japan/USA/Europe są jednoznaczne; wyjątki jawne ((En)/English/[T-Fr]) wygrywają.
_REGION_LANG = {
    "japan": "ja", "usa": "en", "europe": "en", "world": "en", "asia": "en",
    "australia": "en", "canada": "en", "uk": "en",
    "united kingdom": "en", "ireland": "en", "new zealand": "en",
    "korea": "ko", "china": "zh", "taiwan": "zh", "hong kong": "zh",
    "germany": "de", "france": "fr", "spain": "es", "italy": "it",
    "netherlands": "nl", "sweden": "sv", "norway": "no", "denmark": "da",
    "finland": "fi", "poland": "pl", "russia": "ru", "brazil": "pt",
    "portugal": "pt", "greece": "el", "scandinavia": "sv",
}

# tag tłumaczenia w [] LUB (): [T-En], [T+Eng], (T-En), [T-En by Foo], [T+Fr1.2]
_T_TAG_RE = re.compile(r"[\[\(]\s*T[-+]\s*([A-Za-z]+)", re.I)
# ogólny tag tłumaczenia (do etykiety) — [...]/(...) zaczynające się od T-/T+
_T_LABEL_RE = re.compile(r"[\[\(]\s*T[-+][^\]\)]*[\]\)]", re.I)


def _lang_from_token(tok: str) -> str:
    """Kod języka z tokenu: dokładnie (en/eng/english) albo po 3-/2-literowym
    prefiksie (np. „EnglishByFoo"→en). Nieznane → "" (NIE śmiecimy)."""
    t = tok.strip().lower()
    for cand in (t, t[:3], t[:2]):
        c = _LANG_ALIASES.get(cand)
        if c:
            return c
    return ""


def parse_langs(name: str, region_fallback: bool = True) -> List[str]:
    """Języki wykryte w nazwie. Rozpoznaje:
      - tag tłumaczenia `[T-En]/[T+Eng]/(T-Eng)` → PIERWSZY token po T-/+,
      - listy językowe w () lub [] gdzie WSZYSTKIE tokeny to znane języki
        (`(En,Fr,De)`, `(English)`, `[En]`),
      - (gdy `region_fallback` i nic jawnego) REGION → język ((Japan)→ja,
        (USA)/(Europe)→en …).
    NIE-języki (grupy, wersje) są ODRZUCANE — żadnych śmieciowych kodów.
    Zwraca kody ISO bez duplikatów, w kolejności wystąpienia."""
    out: List[str] = []

    def _add(c: str) -> None:
        if c and c not in out:
            out.append(c)

    # 1) tag tłumaczenia — pierwszy token po T-/T+ (język; reszta to grupa/wersja)
    for m in _T_TAG_RE.finditer(name):
        _add(_lang_from_token(m.group(1)))
    # 2) grupy () i [] będące CZYSTĄ listą języków (wszystkie tokeny znane)
    for grp in re.findall(r"[\(\[]([^)\]]*)[\)\]]", name):
        toks = [t for t in re.split(r"[,\s/+]+", grp) if t]
        if toks and all(t.lower() in _LANG_ALIASES for t in toks):
            for t in toks:
                _add(_LANG_ALIASES[t.lower()])
    # 3) FALLBACK po REGIONIE — tylko gdy nie było JAWNEGO tagu/listy języka.
    #    Jawne „(En)"/„English"/[T-*] wygrało wyżej.
    if region_fallback and not out:
        for grp in re.findall(r"[\(\[]([^)\]]*)[\)\]]", name):
            for part in grp.split(","):
                c = _REGION_LANG.get(part.strip().lower())
                if c:
                    _add(c)
    return out


def is_translation(name: str) -> bool:
    """Czy nazwa niesie tag fanowskiego tłumaczenia `[T-…]`/`(T-…)`."""
    return bool(_T_TAG_RE.search(name))


def translation_label(name: str) -> str:
    """Etykieta wariantu do GUI: treść tagu `[T-…]` (autor/wersja), np.
    „[T-En by Foo v1.1]". Gdy brak — pusty string."""
    m = _T_LABEL_RE.search(name)
    return m.group(0) if m else ""


# --- indeks wariantów --------------------------------------------------------

@dataclass
class TransVariant:
    """Jeden dostępny wariant tłumaczenia (gra jednoplikowa)."""
    base: str                 # tytuł bazowy (do parowania)
    game: str                 # pełna nazwa gry w DAT-cie tłumaczeń
    langs: tuple              # wykryte języki (kody)
    label: str               # etykieta [T-…] do GUI
    canonical: str           # ścieżka pliku tłumaczenia (kanoniczna w jego DAT)
    sha1: str                # SHA-1 zawartości (stabilna tożsamość wyboru)
    size: int                # rozmiar (do identyfikacji)
    dat_name: str            # nazwa DAT-u tłumaczeń
    dat_id: int              # id(entry) — do odróżnienia źródła

    @property
    def lang_str(self) -> str:
        return ",".join(self.langs) if self.langs else "?"


def build_variant_index(
        reports: Sequence, rules_fn: Optional[Callable[[object], dict]],
) -> Dict[str, List[TransVariant]]:
    """Mapa `tytuł_bazowy → [warianty]` z DAT-ów o roli `translations`.
    Tylko gry JEDNOPLIKOWE (jeden ROM danych) — dopasowanie po zawartości.
    `rules_fn(entry)` musi zwracać dict z ewentualnym kluczem `role`.
    """
    idx: Dict[str, List[TransVariant]] = {}
    for rep in reports:
        eff = rules_fn(rep.entry) if rules_fn else {}
        if (eff or {}).get("role") != "translations":
            continue
        # Język bywa TYLKO w nazwie DAT-u (np. „… [T-En] Collection"), a gry w
        # środku mają czyste nazwy → dziedziczymy język/etykietę z DAT-u, gdy w
        # nazwie gry nic nie ma. To naprawia „puste" wykrywanie języka.
        dat_langs = parse_langs(rep.entry.name)
        dat_label = translation_label(rep.entry.name)
        by_game: Dict[str, list] = {}
        for s in rep.statuses:
            by_game.setdefault(s.game, []).append(s)
        for gname, sts in by_game.items():
            data = [s for s in sts
                    if not s.rom.name.lower().endswith((".cue", ".gdi"))]
            if len(data) != 1:
                continue                      # v1: tylko jednoplikowe
            s = data[0]
            # Priorytet języka wariantu: JAWNY tag/lista w nazwie gry → język
            # TŁUMACZENIA z nazwy DAT-u → region gry (fallback). Dzięki temu
            # „Cool Game (Japan)" w „[T-En] Collection" = en (nie ja).
            langs = tuple(parse_langs(gname, region_fallback=False)
                          or dat_langs or parse_langs(gname))
            label = (translation_label(gname)
                     or (f"{gname} {dat_label}".strip() if dat_label else gname))
            v = TransVariant(
                base=base_title(gname), game=gname, langs=langs, label=label,
                canonical=str(s.canonical_path),
                sha1=(s.rom.sha1 or "").lower(), size=s.rom.size or 0,
                dat_name=rep.entry.name, dat_id=id(rep.entry))
            idx.setdefault(v.base, []).append(v)
    return idx


def variants_for(index: Dict[str, List[TransVariant]], game_name: str,
                 lang: str = "") -> List[TransVariant]:
    """Warianty pasujące do gry (po tytule bazowym), opcjonalnie filtrowane
    językiem (kod ISO). Posortowane: język pasujący pierwszy, potem nazwa."""
    hits = list(index.get(base_title(game_name), []))
    if lang:
        hits = [v for v in hits if lang in v.langs]
    hits.sort(key=lambda v: (v.lang_str, v.game.lower()))
    return hits


def all_languages(index: Dict[str, List[TransVariant]]) -> List[str]:
    """Wszystkie języki obecne w indeksie wariantów (posortowane)."""
    out: set = set()
    for vs in index.values():
        for v in vs:
            out.update(v.langs)
    return sorted(out)


# --- trwały wybór podmian (translations.json) --------------------------------

def sub_key(dat_name: str, game: str) -> str:
    """Klucz podmiany: nazwa DAT-u + nazwa gry (stabilny między przebiegami)."""
    return f"{dat_name}\t{game}"


class TranslationStore:
    """Trwały wybór podmian per gra. Plik JSON w katalogu kolekcji.
    Wpis: klucz sub_key(dat, gra) → {sha1, name, lang, src}. ŹRÓDŁO PRAWDY dla
    matchera (gra z wpisem = spełniona przez wybrane tłumaczenie)."""

    FILENAME = "translations.json"

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._subs: Dict[str, dict] = {}
        self.load()

    def load(self) -> "TranslationStore":
        self._subs = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._subs = dict(data.get("subs", {}))
        except (OSError, ValueError):
            self._subs = {}
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "subs": self._subs},
                                  ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # -- API per gra --
    def get(self, dat_name: str, game: str) -> Optional[dict]:
        return self._subs.get(sub_key(dat_name, game))

    def set(self, dat_name: str, game: str, variant: "TransVariant") -> None:
        self._subs[sub_key(dat_name, game)] = {
            "sha1": variant.sha1, "name": variant.game,
            "lang": variant.lang_str, "src": variant.canonical,
            "dat": variant.dat_name}

    def set_manual(self, dat_name: str, game: str, *, sha1: str, name: str,
                   src: str, lang: str = "") -> None:
        self._subs[sub_key(dat_name, game)] = {
            "sha1": (sha1 or "").lower(), "name": name, "lang": lang,
            "src": src, "dat": ""}

    def remove(self, dat_name: str, game: str) -> bool:
        return self._subs.pop(sub_key(dat_name, game), None) is not None

    def has(self, dat_name: str, game: str) -> bool:
        return sub_key(dat_name, game) in self._subs

    @property
    def subs(self) -> Dict[str, dict]:
        """Surowa mapa (klucz sub_key → wpis) — dla matchera."""
        return self._subs


# --- przepływ podmiany / odtworzenia -----------------------------------------

def preserve_dir_for(tosort_root: str | os.PathLike, system: str) -> Path:
    """Katalog na oryginały: `<to sort>\\translated\\<system>`."""
    safe = re.sub(r'[<>:"/\\|?*]+', "_", system).strip() or "misc"
    return Path(tosort_root) / "translated" / safe


def apply_substitution(
        canonical: Path, variant_file: Path, preserve_dir: Path, *,
        index=None, make_links: bool = True, dry_run: bool = False,
        log: LogCB = lambda m: None) -> bool:
    """Podmiana slotu kolekcji na tłumaczenie:
      1) oryginał (jeśli FIZYCZNY plik pod `canonical`) → `preserve_dir`
         (zachowanie do odtworzenia i walidacji setu); istniejący symlink
         po prostu usuwamy,
      2) symlink `canonical` (NAZWA KANONICZNA) → `variant_file`.
    Nie kasuje żadnego pliku bezpowrotnie. Zwraca True gdy podmiana zrobiona
    (albo w dry-run zapowiedziana)."""
    from .linker import create_link, is_link, remove_link, LinkPrivilegeError
    if not variant_file.exists():
        log(f"TŁUMACZENIE: brak pliku wariantu {variant_file} — pomijam")
        return False
    # 1) zabezpiecz oryginał / usuń stary link
    if os.path.lexists(canonical):
        if is_link(canonical):
            log(f"  usuwam poprzedni link: {canonical.name}")
            if not dry_run:
                remove_link(canonical)
        else:
            dest = preserve_dir / canonical.name
            log(f"  oryginał → {dest}")
            if not dry_run:
                preserve_dir.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    canonical.unlink()          # już zachowany — usuń bieżący
                    if index is not None:
                        try:
                            index.remove_path(canonical)
                        except Exception:
                            pass
                else:
                    os.replace(canonical, dest)
                    if index is not None:
                        try:
                            index.rename(canonical, dest)
                        except Exception:
                            pass
    # 2) symlink kanoniczny → wariant
    log(f"TŁUMACZENIE: {canonical.name} -> {variant_file}")
    if dry_run:
        return True
    if not make_links:
        log("  linki wyłączone — podmiana pominięta (nic nie kopiuję)")
        return False
    try:
        create_link(canonical, variant_file, is_dir=False)
    except LinkPrivilegeError as e:
        log(f"  UWAGA: {e} — uruchom jako administrator.")
        return False
    except OSError as e:
        log(f"  BŁĄD symlinku: {e}")
        return False
    if index is not None:
        try:
            index.mark_link(canonical)
        except Exception:
            pass
    return True


def restore_original(canonical: Path, preserve_dir: Path, *, index=None,
                     dry_run: bool = False,
                     log: LogCB = lambda m: None) -> bool:
    """Cofa podmianę: usuwa symlink `canonical` i przywraca zachowany oryginał
    z `preserve_dir`. Zwraca True, gdy przywrócono."""
    from .linker import is_link, remove_link
    saved = preserve_dir / canonical.name
    if not saved.exists():
        log(f"ODTWORZENIE: brak zachowanego oryginału {saved}")
        return False
    log(f"ODTWORZENIE: {canonical.name} <- {saved}")
    if dry_run:
        return True
    if os.path.lexists(canonical):
        if is_link(canonical):
            remove_link(canonical)
        else:
            canonical.unlink()
    os.replace(saved, canonical)
    if index is not None:
        try:
            index.rename(saved, canonical)
        except Exception:
            pass
    return True

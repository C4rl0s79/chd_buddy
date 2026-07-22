"""Ikony do gier — grafiki z Libretro Thumbnails / SteamGridDB → pliki .ico.

Logika przeniesiona z PyLinks (ExtraArtSources / IconManager / make_ico_bytes)
i uproszczona pod kolekcje zarządzane DAT-ami:

- Nasze pliki po `rebuild` mają KANONICZNE nazwy Redump/No-Intro, a zbiory
  Libretro Thumbnails na GitHubie są nazwane dokładnie tak samo. Ikonę
  znajdujemy więc deterministycznie, bez wyszukiwarki:
      https://raw.githubusercontent.com/libretro-thumbnails/<System>/master/
          Named_Boxarts/<Nazwa gry>.png
- Nazwa systemu bierze się wprost z nazwy katalogu DAT-a ("Sony - PlayStation 2"
  → "Sony_-_PlayStation_2"); obsługujemy też skróty (PS2, DC, SATURN…).
- Fallback: SteamGridDB (wymaga klucza API) — wyszukiwanie po oczyszczonym
  tytule (bez tagów regionu/dysku), potem kwadratowe gridy/ikony.

Wynik: <nazwa gry>.ico (wielorozmiarowe: 256…16 px) w katalogu docelowym,
domyślnie podkatalog "icons" obok ROM-ów. Istniejące .ico nie są nadpisywane
(cache na poziomie plików). Pillow wymagane tylko do konwersji na .ico.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

try:
    from PIL import Image
    PIL_OK = True
except ImportError:  # pragma: no cover
    PIL_OK = False

LIBRETRO_BASE = "https://raw.githubusercontent.com/libretro-thumbnails"
SGDB_API = "https://www.steamgriddb.com/api/v2"

# Skróty platform → repozytoria Libretro (z PyLinks).
LIBRETRO_SYSTEM_MAP: dict[str, str] = {
    "PS1": "Sony_-_PlayStation",
    "PS2": "Sony_-_PlayStation_2",
    "PS3": "Sony_-_PlayStation_3",
    "PSP": "Sony_-_PlayStation_Portable",
    "N64": "Nintendo_-_Nintendo_64",
    "SNES": "Nintendo_-_Super_Nintendo_Entertainment_System",
    "NES": "Nintendo_-_Nintendo_Entertainment_System",
    "GB": "Nintendo_-_Game_Boy",
    "GBA": "Nintendo_-_Game_Boy_Advance",
    "GBC": "Nintendo_-_Game_Boy_Color",
    "NDS": "Nintendo_-_Nintendo_DS",
    "GCN": "Nintendo_-_GameCube",
    "WII": "Nintendo_-_Wii",
    "SATURN": "Sega_-_Saturn",
    "DC": "Sega_-_Dreamcast",
    "MD": "Sega_-_Mega_Drive_-_Genesis",
    "SMS": "Sega_-_Master_System_-_Mark_III",
    "GG": "Sega_-_Game_Gear",
    "ARCADE": "MAME",
    "MAME": "MAME",
    "NEOGEO": "SNK_-_Neo_Geo",
    "NGP": "SNK_-_Neo_Geo_Pocket",
    "ATARI2600": "Atari_-_2600",
    "ATARI7800": "Atari_-_7800",
    "3DO": "3DO_Interactive_Multiplayer",
    "PCENGINE": "NEC_-_PC_Engine_-_TurboGrafx_16",
}

# Rozszerzenia plików traktowanych jako gry przy skanie katalogu.
GAME_EXTS = {"chd", "iso", "cue", "gdi", "m3u", "pbp", "zip", "7z"}

# Tag dysku: "(Disc 1)", "[CD 2]", "(Disc 1 of 3)"...
_DISC_RE = re.compile(
    r"\s*[\(\[]\s*(?:Disc|Disk|CD|Dysk)\s*\d+(?:\s*of\s*\d+)?\s*[\)\]]",
    re.IGNORECASE)
# Dowolne tagi w nawiasach (region, wersja, języki) — do zapytań SGDB.
_TAG_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")

# Libretro zastępuje te znaki w nazwach plików podkreśleniem.
_LIBRETRO_BAD = str.maketrans({c: "_" for c in '&*/:`<>?\\|"'})

# Tagi "śmieciowe" w nazwach miniatur: wersje, beta/proto/demo, daty, rewizje.
_NOISE_TAG_RE = re.compile(
    r"^(v[\d.]+|rev\s*[\w.]*|beta.*|proto.*|sample|demo.*|alt.*|"
    r"\d{4}-\d{2}-\d{2}|unl|pirate)$", re.IGNORECASE)

FetchFn = Callable[[str, dict | None], Optional[bytes]]


def _default_fetch(url: str, hdrs: dict | None = None) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={
        "User-Agent": "chd-buddy/0.2", **(hdrs or {})})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read()
    except OSError:
        return None


def _default_post(url: str, body: bytes, hdrs: dict | None = None) -> Optional[bytes]:
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": "chd-buddy/0.2", **(hdrs or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except OSError:
        return None


PostFn = Callable[[str, bytes, dict | None], Optional[bytes]]


def strip_disc_tag(title: str) -> str:
    return re.sub(r"\s{2,}", " ", _DISC_RE.sub(" ", title)).strip()


def clean_search_title(title: str) -> str:
    """Tytuł bez wszystkich tagów — do wyszukiwarek (SGDB)."""
    return re.sub(r"\s{2,}", " ", _TAG_RE.sub(" ", title)).strip()


def clean_system_name(name: str) -> str:
    """Nazwa DAT-a/katalogu → czysta nazwa platformy dla Libretro/TGDB.

    'Sony - PlayStation 2 (Redump - Fresh1G1R - PropeR)' → 'Sony - PlayStation 2'
    'Sony - PlayStation 2 - Datfile (11719)'            → 'Sony - PlayStation 2'
    'PS2' / 'Sony - PlayStation 2'                       → bez zmian
    Bez tego Libretro dostaje nieistniejącą nazwę repo i milczy (widać tylko
    SGDB, który szuka po tytule).
    """
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name)       # usuń (…) i […]
    s = re.sub(r"\s*-\s*Datfile.*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+(?:Retool|Collection)\b.*$", "", s, flags=re.IGNORECASE)
    return s.strip(" -")


def libretro_system(system: str) -> str:
    """'PS2' albo 'Sony - PlayStation 2 (Redump…)' → nazwa repo Libretro."""
    s = clean_system_name(system).strip()
    if s.upper() in LIBRETRO_SYSTEM_MAP:
        return LIBRETRO_SYSTEM_MAP[s.upper()]
    return s.replace(" ", "_")


def libretro_name(title: str) -> str:
    """Nazwa gry tak, jak nazywa pliki repozytorium libretro-thumbnails."""
    return title.translate(_LIBRETRO_BAD)


def libretro_urls(system: str, title: str) -> list[str]:
    """Kandydackie URL-e miniatur — od najlepszego (boxart) w dół.

    Próbujemy: pełna nazwa, nazwa bez tagu dysku (multi-disc ma zwykle
    miniaturę bez '(Disc N)'). To ślepe zgadywanie — lepsze wyniki daje
    LibretroIndex (lista plików repo + dopasowanie regionów).
    """
    repo = libretro_system(system)
    names: list[str] = []
    for t in (title, strip_disc_tag(title)):
        n = libretro_name(t)
        if n and n not in names:
            names.append(n)
    urls = []
    for folder in ("Named_Boxarts", "Named_Titles", "Named_Snaps"):
        for n in names:
            enc = urllib.parse.quote(n)
            urls.append(f"{LIBRETRO_BASE}/{repo}/master/{folder}/{enc}.png")
    return urls


def _base_and_tags(name: str) -> tuple[str, set[str]]:
    """'Final Fantasy X (USA, Canada)' -> ('final fantasy x', {'usa','canada'}).

    Tytuł bazowy jest normalizowany z interpunkcji ('WWF SmackDown!' ==
    'WWF SmackDown'), bo Redump i libretro-thumbnails różnią się drobiazgami.
    """
    tags: set[str] = set()
    for m in re.finditer(r"[\(\[]([^\)\]]*)[\)\]]", name):
        for tok in m.group(1).split(","):
            tok = tok.strip().lower()
            if tok:
                tags.add(tok)
    base = _TAG_RE.sub(" ", name).lower()
    base = re.sub(r"[^a-z0-9]+", " ", base).strip()
    return base, tags


class LibretroIndex:
    """Lista miniatur systemu z GitHub API + dopasowanie rozmyte.

    Nazwy Redump i libretro-thumbnails różnią się tagami regionów
    ('(USA)' vs '(USA, Canada)'), więc ślepe zgadywanie URL-i zawodzi.
    Jedno zapytanie o drzewo repo (cache w pliku JSON) daje pełną listę
    nazw, na której dopasowujemy: pełna nazwa → bez tagu dysku → tytuł
    bazowy z największym przecięciem regionów.
    """

    def __init__(self, system: str, *, fetch: FetchFn = _default_fetch,
                 cache_dir: Optional[Path] = None):
        self.repo = libretro_system(system)
        self._fetch = fetch
        if cache_dir is None:
            from .settings import app_base_dir
            cache_dir = app_base_dir() / "libretro_cache"
        self.cache_file = Path(cache_dir) / f"{self.repo}.json"
        self._names: Optional[list[str]] = None

    # --- lista plików repo -------------------------------------------------

    def names(self) -> list[str]:
        if self._names is not None:
            return self._names
        if self.cache_file.is_file():
            try:
                self._names = json.loads(self.cache_file.read_text(encoding="utf-8"))
                return self._names
            except (OSError, ValueError):
                pass
        self._names = self._fetch_tree()
        if self._names:
            try:
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                self.cache_file.write_text(
                    json.dumps(self._names, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return self._names

    def _fetch_tree(self) -> list[str]:
        api = f"https://api.github.com/repos/libretro-thumbnails/{self.repo}"
        root = self._fetch(f"{api}/git/trees/master", None)
        if not root:
            return []
        try:
            tree = json.loads(root).get("tree", [])
            sha = next(t["sha"] for t in tree if t.get("path") == "Named_Boxarts")
        except (ValueError, StopIteration, KeyError):
            return []
        sub = self._fetch(f"{api}/git/trees/{sha}", None)
        if not sub:
            return []
        try:
            data = json.loads(sub)
        except ValueError:
            return []
        return [t["path"][:-4] for t in data.get("tree", [])
                if t.get("path", "").endswith(".png")]

    # --- dopasowanie ---------------------------------------------------------

    def find(self, title: str) -> Optional[str]:
        """Zwraca nazwę miniatury (bez .png) najlepiej pasującą do tytułu."""
        names = self.names()
        if not names:
            return None
        wanted = [libretro_name(title), libretro_name(strip_disc_tag(title))]
        by_exact = {n: n for n in names}
        for w in wanted:
            if w in by_exact:
                return w
        base, tags = _base_and_tags(strip_disc_tag(title))
        if not base:
            return None
        best: Optional[str] = None
        best_score: Optional[tuple[int, int]] = None
        for n in names:
            nb, ntags = _base_and_tags(n)
            if nb != base:
                continue
            # nagroda za wspólne tagi (region); kara za nadmiarowe, przy czym
            # wersje/beta/daty bolą mocniej niż dodatkowy region — '(USA)' ma
            # trafić w '(USA, Canada)', nie w '(USA) (v2.00)' ani '(Beta)'.
            extra = ntags - tags
            penalty = sum(2 if _NOISE_TAG_RE.match(t) else 1 for t in extra)
            score = (len(tags & ntags), -penalty)
            if best_score is None or score > best_score:
                best, best_score = n, score
        return best

    def url(self, name: str, folder: str = "Named_Boxarts") -> str:
        enc = urllib.parse.quote(name)
        return f"{LIBRETRO_BASE}/{self.repo}/master/{folder}/{enc}.png"


def make_ico_bytes(img_bytes: bytes) -> bytes:
    """PNG/JPG → wielorozmiarowe .ico (256…16 px), jak w PyLinks."""
    if not PIL_OK:
        raise RuntimeError("Pillow wymagane do tworzenia .ico: pip install Pillow")
    img = Image.open(BytesIO(img_bytes)).convert("RGBA")
    # boxarty są prostokątne — dopełnij do kwadratu przezroczystym tłem
    w, h = img.size
    if w != h:
        side = max(w, h)
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(img, ((side - w) // 2, (side - h) // 2))
        img = sq
    ms = min(img.size[0], 256)
    sizes = [s for s in (256, 128, 64, 48, 32, 16) if s <= ms] or [ms]
    frames = [img.resize((s, s), Image.LANCZOS) for s in sizes]
    buf = BytesIO()
    frames[0].save(buf, format="ICO",
                   sizes=[(s, s) for s in sizes], append_images=frames[1:])
    return buf.getvalue()


# Pełna nazwa platformy (Redump/No-Intro) → skrót (odwrócenie LIBRETRO_SYSTEM_MAP).
_LIBRETRO_NAME_TO_CODE: dict[str, str] = {
    v.replace("_", " "): k for k, v in LIBRETRO_SYSTEM_MAP.items()}

# TheGamesDB: skrót platformy → numeryczne platform_id (z PyLinks).
TGDB_PLATFORM_IDS: dict[str, int] = {
    "PC": 1, "PS1": 10, "PS2": 11, "PS3": 12, "PS4": 4919, "PSP": 13,
    "PSVITA": 39, "N64": 3, "SNES": 6, "NES": 7, "GCN": 2, "WII": 9,
    "WIIU": 38, "NSW": 4971, "GB": 4, "GBC": 41, "GBA": 5, "NDS": 8,
    "3DS": 4912, "SATURN": 17, "DC": 16, "MD": 18, "SMS": 35, "GG": 20,
    "ARCADE": 23, "MAME": 23, "NAOMI": 23, "ATARI2600": 22, "ATARI7800": 30,
    "3DO": 25, "PCENGINE": 34, "NEOGEO": 24, "XBOX": 14, "X360": 15,
}


class IgdbClient:
    """IGDB (Twitch OAuth) — cover + artworks + screenshots po tytule."""

    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    API_URL = "https://api.igdb.com/v4"

    def __init__(self, client_id: str, client_secret: str,
                 fetch: FetchFn = _default_fetch, post: PostFn = _default_post):
        self.cid = (client_id or "").strip()
        self.secret = (client_secret or "").strip()
        self._fetch = fetch
        self._post = post
        self._token = ""

    def _get_token(self) -> str:
        if self._token or not (self.cid and self.secret):
            return self._token
        params = (f"client_id={urllib.parse.quote(self.cid)}"
                  f"&client_secret={urllib.parse.quote(self.secret)}"
                  f"&grant_type=client_credentials")
        raw = self._post(f"{self.TOKEN_URL}?{params}", b"",
                         {"Content-Type": "application/x-www-form-urlencoded"})
        if not raw:
            return ""
        try:
            self._token = json.loads(raw).get("access_token", "")
        except (ValueError, UnicodeDecodeError):
            self._token = ""
        return self._token

    def candidates(self, title: str, limit: int = 6) -> list[dict]:
        """Zwraca [{"label", "url" (HD), "thumb"}] — cover, art, screenshots."""
        token = self._get_token()
        if not token:
            return []
        body = (f'search "{title}"; '
                f'fields id,name,cover.url,artworks.url,screenshots.url; '
                f'limit 3;').encode()
        raw = self._post(f"{self.API_URL}/games", body, {
            "Client-ID": self.cid, "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain"})
        if not raw:
            return []
        try:
            games = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return []
        if not games:
            return []
        g = games[0]
        out: list[dict] = []

        def _add(raw_url: str, label: str) -> None:
            thumb = raw_url if raw_url.startswith("http") else "https:" + raw_url
            hd = thumb.replace("t_thumb", "t_1080p").replace(
                "t_micro", "t_1080p")
            out.append({"label": f"IGDB {label}", "url": hd, "thumb": thumb})

        if (cov := g.get("cover")) and cov.get("url"):
            _add(cov["url"], "cover")
        for i, a in enumerate((g.get("artworks") or [])[:3]):
            if a.get("url"):
                _add(a["url"], f"artwork #{i + 1}")
        for i, s in enumerate((g.get("screenshots") or [])[:3]):
            if s.get("url"):
                _add(s["url"], f"screen #{i + 1}")
        return out[:limit]


class TgdbClient:
    """TheGamesDB — box art / fan art / clear logo / banner po tytule."""

    def __init__(self, api_key: str, fetch: FetchFn = _default_fetch):
        self.key = (api_key or "").strip()
        self._fetch = fetch

    def candidates(self, title: str, platform: str = "",
                   limit: int = 8) -> list[dict]:
        if not self.key:
            return []
        p_filter = ""
        # skrót ('PS2') albo pełna nazwa ('Sony - PlayStation 2 (…)')
        clean = clean_system_name(platform)
        pid = (TGDB_PLATFORM_IDS.get(platform.upper())
               or TGDB_PLATFORM_IDS.get(_LIBRETRO_NAME_TO_CODE.get(clean, "")))
        if pid:
            p_filter = f"&filter[platform]={pid}"
        url = (f"https://api.thegamesdb.net/v1/Games/ByGameName"
               f"?apikey={self.key}&name={urllib.parse.quote(title)}"
               f"&fields=overview&include=boxart{p_filter}")
        raw = self._fetch(url, None)
        if not raw:
            return []
        try:
            resp = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return []
        if resp.get("code") != 200:
            return []
        games = resp.get("data", {}).get("games", [])
        if not games:
            return []
        gid = str(games[0].get("id", ""))
        block = resp.get("include", {}).get("boxart", {})
        base = block.get("base_url", {})
        orig = base.get("original", "https://cdn.thegamesdb.net/images/original/")
        thumb_base = base.get("thumb", orig)
        wanted = {"boxart", "fanart", "clearlogo", "banner"}
        out: list[dict] = []
        for img in block.get("data", {}).get(gid, []):
            itype = img.get("type", "")
            fn = img.get("filename", "")
            if itype not in wanted or not fn:
                continue
            if itype == "boxart" and img.get("side") not in ("", "front"):
                continue
            out.append({"label": f"TGDB {itype}",
                        "url": orig + fn, "thumb": thumb_base + fn})
            if len(out) >= limit:
                break
        return out


class SgdbClient:
    """Minimalny klient SteamGridDB (Bearer key) — fallback po tytule."""

    def __init__(self, api_key: str, fetch: FetchFn = _default_fetch):
        self.key = api_key.strip()
        self._fetch = fetch

    def _api(self, path: str) -> Optional[dict]:
        if not self.key:
            return None
        raw = self._fetch(f"{SGDB_API}{path}",
                          {"Authorization": f"Bearer {self.key}"})
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None

    def search(self, title: str) -> Optional[int]:
        enc = urllib.parse.quote(title)
        d = self._api(f"/search/autocomplete/{enc}")
        if not d or not d.get("success") or not d.get("data"):
            return None
        return int(d["data"][0]["id"])

    def grid_candidates(self, game_id: int, limit: int = 12) -> list[dict]:
        """Lista gridów/ikon do WYBORU przez użytkownika.

        Zwraca [{"label", "url" (pełny obraz), "thumb" (miniatura)}].
        """
        out: list[dict] = []
        for path, label in ((f"/grids/game/{game_id}?dimensions=512x512,1024x1024",
                             "SGDB grid kwadrat"),
                            (f"/icons/game/{game_id}", "SGDB ikona"),
                            (f"/grids/game/{game_id}", "SGDB grid")):
            d = self._api(path)
            if not d or not d.get("success"):
                continue
            for item in d.get("data", []):
                url = item.get("url", "")
                if not url or any(c["url"] == url for c in out):
                    continue
                w, h = item.get("width", 0), item.get("height", 0)
                out.append({"label": f"{label} {w}x{h}".strip(),
                            "url": url,
                            "thumb": item.get("thumb", url)})
                if len(out) >= limit:
                    return out
        return out

    def best_image(self, game_id: int) -> Optional[bytes]:
        """Kwadratowy grid (512x512/1024x1024) albo ikona — pierwszy dostępny."""
        for path in (f"/grids/game/{game_id}?dimensions=512x512,1024x1024",
                     f"/icons/game/{game_id}",
                     f"/grids/game/{game_id}"):
            d = self._api(path)
            if not d or not d.get("success"):
                continue
            for item in d.get("data", []):
                url = item.get("url", "")
                if not url:
                    continue
                b = self._fetch(url, None)
                if b:
                    return b
        return None


@dataclass
class IconStats:
    done: int = 0
    cached: int = 0
    not_found: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (f"utworzono {self.done}, było {self.cached}, "
                f"nie znaleziono {self.not_found}, błędy {self.errors}")


def _looks_like_image(b: Optional[bytes]) -> bool:
    return bool(b) and b[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0",
                                 b"\xff\xd8\xff\xe1", b"RIFF")


def _alias_target(b: Optional[bytes]) -> Optional[str]:
    """Repo libretro-thumbnails deduplikuje pliki krótkim tekstem-odsyłaczem
    ('Inna Nazwa.png'). Zwraca nazwę celu (bez .png) albo None."""
    if not b or len(b) > 300 or _looks_like_image(b):
        return None
    try:
        text = b.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if text.lower().endswith(".png") and "\n" not in text:
        return text[:-4]
    return None


def _fetch_thumb(libretro: "LibretroIndex", name: str, folder: str,
                 fetch: FetchFn, hops: int = 4) -> Optional[bytes]:
    """Pobiera miniaturę, podążając za aliasami tekstowymi repo.

    Aliasy bywają wiszące (wskazują nazwę, której już nie ma) — wtedy cel
    aliasu dopasowujemy rozmyto do listy repo i próbujemy dalej.
    """
    seen: set[str] = set()
    for _ in range(hops):
        if not name or name in seen:
            return None
        seen.add(name)
        b = fetch(libretro.url(name, folder), None)
        if _looks_like_image(b):
            return b
        target = _alias_target(b)
        if target:
            name = target
            continue
        resolved = libretro.find(name)
        if resolved and resolved not in seen:
            name = resolved
            continue
        return None
    return None


def fetch_artwork(system: str, title: str, *,
                  sgdb: Optional[SgdbClient] = None,
                  libretro: Optional[LibretroIndex] = None,
                  fetch: FetchFn = _default_fetch,
                  log: Optional[Callable[[str], None]] = None) -> Optional[bytes]:
    """Zwraca bajty PNG/JPG dla gry: najpierw Libretro, potem SGDB."""
    # 1) Libretro z indeksem repo (dopasowanie regionów)
    if libretro is not None:
        name = libretro.find(title)
        if name:
            for folder in ("Named_Boxarts", "Named_Titles", "Named_Snaps"):
                b = _fetch_thumb(libretro, name, folder, fetch)
                if b is not None:
                    if log:
                        log(f"  Libretro: {name} [{folder}]")
                    return b
    else:
        # 2) ślepe zgadywanie URL-i (offline-fallback bez GitHub API)
        for url in libretro_urls(system, title):
            b = fetch(url, None)
            if _looks_like_image(b):
                if log:
                    log(f"  Libretro: {url.rsplit('/', 2)[-2]}")
                return b
    if sgdb is not None:
        gid = sgdb.search(clean_search_title(title) or title)
        if gid:
            b = sgdb.best_image(gid)
            if _looks_like_image(b):
                if log:
                    log(f"  SGDB: id={gid}")
                return b
    return None


def artwork_candidates(
    system: str,
    title: str,
    *,
    sgdb: Optional[SgdbClient] = None,
    igdb: Optional[IgdbClient] = None,
    tgdb: Optional[TgdbClient] = None,
    fetch: FetchFn = _default_fetch,
    cache_dir: Optional[Path] = None,
    sgdb_limit: int = 12,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> list[dict]:
    """Kandydaci grafik do RĘCZNEGO wyboru (okno „Stwórz ikonę" w GUI).

    Źródła: Libretro (boxart/tytuł/snap), SteamGridDB (gridy/ikony), IGDB
    (cover/artwork/screenshot), TheGamesDB (boxart/fanart/logo/banner).
    Faza 1 — zbieramy odnośniki ze wszystkich źródeł; faza 2 — pobieramy
    miniatury z paskiem postępu on_progress(done, total, etykieta).
    Zwraca [{"label", "preview" (bajty), "url" (pełny), "full" (bajty|None)}].
    """
    refs: list[dict] = []           # {label, url, thumb, full?}
    clean = clean_search_title(title) or title

    def _prog(i: int, n: int, msg: str) -> None:
        if on_progress:
            on_progress(i, n, msg)

    # --- faza 1: zbieranie odnośników (bez pobierania obrazów) ---
    _prog(0, 0, "Libretro…")
    idx = LibretroIndex(system, fetch=fetch, cache_dir=cache_dir)
    if idx.names():
        name = idx.find(title)
        if name:
            for folder, label in (("Named_Boxarts", "Libretro boxart"),
                                  ("Named_Titles", "Libretro tytuł"),
                                  ("Named_Snaps", "Libretro snap")):
                b = _fetch_thumb(idx, name, folder, fetch)
                if b is not None:
                    refs.append({"label": f"{label}: {name}",
                                 "url": idx.url(name, folder),
                                 "thumb": None, "full": b})
    if sgdb is not None:
        _prog(0, 0, "SteamGridDB…")
        gid = sgdb.search(clean)
        if gid:
            for c in sgdb.grid_candidates(gid, limit=sgdb_limit):
                refs.append({"label": c["label"], "url": c["url"],
                             "thumb": c["thumb"], "full": None})
    if igdb is not None:
        _prog(0, 0, "IGDB…")
        for c in igdb.candidates(clean):
            refs.append({"label": c["label"], "url": c["url"],
                         "thumb": c["thumb"], "full": None})
    if tgdb is not None:
        _prog(0, 0, "TheGamesDB…")
        for c in tgdb.candidates(clean, platform=system):
            refs.append({"label": c["label"], "url": c["url"],
                         "thumb": c["thumb"], "full": None})

    # --- faza 2: pobieranie miniatur z postępem ---
    out: list[dict] = []
    total = len(refs)
    for i, r in enumerate(refs):
        _prog(i, total, f"pobieram {i + 1}/{total}: {r['label']}")
        preview = r.get("full")
        if preview is None and r.get("thumb"):
            preview = fetch(r["thumb"], None)
        if not _looks_like_image(preview):
            continue
        out.append({"label": r["label"], "preview": preview,
                    "url": r["url"], "full": r.get("full")})
    _prog(total, total, "gotowe")
    return out


def save_icon(art: dict, ico_path: Path,
              fetch: FetchFn = _default_fetch) -> bool:
    """Zapisuje wybranego kandydata jako .ico (dociąga pełny obraz z url)."""
    data = art.get("full")
    if not data:
        data = fetch(art["url"], None)
    if not _looks_like_image(data):
        data = art.get("preview")
    if not _looks_like_image(data):
        return False
    ico_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.write_bytes(make_ico_bytes(data))
    return True


_SKIP_GAME_SUBDIRS = {"icons", "shortcuts", "images", "manuals", "videos"}


def _iter_games(rom_dir: Path) -> list[str]:
    """Tytuły gier w katalogu: pliki płasko, m3u (ukrywa swoje dyski) oraz
    PODKATALOGI per gra (bin/cue) — tytułem jest nazwa podkatalogu."""
    titles: list[str] = []
    seen: set[str] = set()
    files = sorted(p for p in rom_dir.iterdir() if p.is_file()
                   and p.suffix.lower().lstrip(".") in GAME_EXTS)
    m3u_titles = {strip_disc_tag(p.stem) for p in files
                  if p.suffix.lower() == ".m3u"}
    for p in files:
        if p.suffix.lower() != ".m3u" and strip_disc_tag(p.stem) in m3u_titles:
            continue  # dysk objęty playlistą — ikona przy .m3u
        title = p.stem
        key = strip_disc_tag(title).lower()
        if key in seen:
            continue  # kolejne dyski tej samej gry
        seen.add(key)
        titles.append(title)
    for d in sorted(p for p in rom_dir.iterdir() if p.is_dir()):
        low = d.name.lower()
        if low in _SKIP_GAME_SUBDIRS or low.startswith(("chdbuddy_", "chddeep_")):
            continue
        try:
            has_game = any(f.suffix.lower().lstrip(".") in GAME_EXTS
                           for f in d.iterdir() if f.is_file())
        except OSError:
            continue
        if not has_game:
            continue
        key = strip_disc_tag(d.name).lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(d.name)
    return titles


def make_icons_for_dir(
    rom_dir: Path,
    system: str,
    *,
    out_dir: Optional[Path] = None,
    sgdb: Optional[SgdbClient] = None,
    overwrite: bool = False,
    fetch: FetchFn = _default_fetch,
    cache_dir: Optional[Path] = None,
    log: Optional[Callable[[str], None]] = None,
) -> IconStats:
    """Tworzy .ico dla każdej gry w katalogu (1 ikona na grę multi-disc)."""
    rom_dir = Path(rom_dir)
    if not rom_dir.is_dir():
        raise NotADirectoryError(f"'{rom_dir}' nie jest katalogiem")
    dest = Path(out_dir) if out_dir else rom_dir / "icons"
    stats = IconStats()
    # leniwie: listę repo pobieramy dopiero przy pierwszej brakującej ikonie
    libretro_state: list = [False, None]

    def _libretro() -> Optional[LibretroIndex]:
        if not libretro_state[0]:
            libretro_state[0] = True
            idx = LibretroIndex(system, fetch=fetch, cache_dir=cache_dir)
            # brak listy (offline/rate-limit) => zgadywanie URL-i
            libretro_state[1] = idx if idx.names() else None
        return libretro_state[1]

    def _log(msg: str) -> None:
        if log:
            log(msg)

    for title in _iter_games(rom_dir):
        base = strip_disc_tag(title) or title
        ico_path = dest / f"{base}.ico"
        if ico_path.exists() and not overwrite:
            stats.cached += 1
            continue
        _log(f"{base}")
        art = fetch_artwork(system, title, sgdb=sgdb, libretro=_libretro(),
                            fetch=fetch, log=log)
        if art is None:
            stats.not_found += 1
            _log("  brak grafiki")
            continue
        try:
            ico = make_ico_bytes(art)
        except (OSError, RuntimeError, ValueError) as e:
            stats.errors += 1
            _log(f"  BŁĄD konwersji: {e}")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        ico_path.write_bytes(ico)
        stats.done += 1
    return stats

"""Testy modułu ikon (bez sieci — fetch podstawiany)."""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from chd_buddy.core import icons
from chd_buddy.core.icons import (
    SgdbClient,
    clean_search_title,
    libretro_name,
    libretro_system,
    libretro_urls,
    make_icons_for_dir,
    strip_disc_tag,
)


def _png_bytes(w: int = 64, h: int = 40) -> bytes:
    if not icons.PIL_OK:
        pytest.skip("Pillow niezainstalowane")
    from PIL import Image
    buf = BytesIO()
    Image.new("RGBA", (w, h), (200, 30, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


# --- nazwy / URL-e -------------------------------------------------------------

def test_strip_disc_and_clean_title():
    assert strip_disc_tag("Final Fantasy VII (USA) (Disc 2)") == "Final Fantasy VII (USA)"
    assert strip_disc_tag("Gra [CD 1 of 3]") == "Gra"
    assert clean_search_title("Final Fantasy VII (USA) (Disc 2) [Rev 1]") == "Final Fantasy VII"


def test_libretro_system_mapping():
    assert libretro_system("PS2") == "Sony_-_PlayStation_2"
    assert libretro_system("ps2") == "Sony_-_PlayStation_2"
    assert libretro_system("Sony - PlayStation 2") == "Sony_-_PlayStation_2"
    assert libretro_system("Sega - Dreamcast") == "Sega_-_Dreamcast"
    # pełna nazwa DAT-a z kwalifikatorami — musi być oczyszczona
    assert libretro_system(
        "Sony - PlayStation 2 (Redump - Fresh1G1R - PropeR)") \
        == "Sony_-_PlayStation_2"
    assert libretro_system("Sony - PlayStation 2 - Datfile (11719)") \
        == "Sony_-_PlayStation_2"


def test_clean_system_name():
    from chd_buddy.core.icons import clean_system_name
    assert clean_system_name(
        "Sony - PlayStation 2 (Redump - Fresh1G1R)") == "Sony - PlayStation 2"
    assert clean_system_name(
        "Sega - Saturn - Datfile (2397)") == "Sega - Saturn"
    assert clean_system_name("Nintendo - Nintendo 64 (Retool)") \
        == "Nintendo - Nintendo 64"
    assert clean_system_name("PS2") == "PS2"


def test_libretro_name_sanitization():
    assert libretro_name("Ace Combat 04: Shattered Skies") == "Ace Combat 04_ Shattered Skies"
    assert libretro_name("Q*bert & Friends") == "Q_bert _ Friends"


def test_libretro_urls_order_and_disc_variant():
    urls = libretro_urls("PS1", "Final Fantasy VII (USA) (Disc 2)")
    assert urls[0].startswith(
        "https://raw.githubusercontent.com/libretro-thumbnails/Sony_-_PlayStation/master/Named_Boxarts/")
    # pierwszy: pełna nazwa; drugi: bez tagu dysku
    assert "Disc%202" in urls[0]
    assert "Disc" not in urls[1]
    # boxarty przed titles/snaps
    assert "Named_Boxarts" in urls[0] and "Named_Titles" in urls[2]


# --- ico -----------------------------------------------------------------------

def test_make_ico_bytes_squares_and_multisize():
    if not icons.PIL_OK:
        pytest.skip("Pillow niezainstalowane")
    from PIL import Image
    ico = icons.make_ico_bytes(_png_bytes(300, 400))
    assert ico[:4] == b"\x00\x00\x01\x00"
    img = Image.open(BytesIO(ico))
    # prostokątny boxart został dopełniony do kwadratu
    assert img.size[0] == img.size[1]


# --- SGDB ----------------------------------------------------------------------

def test_sgdb_client_search_and_image():
    png = _png_bytes()
    calls: list[str] = []

    def fake_fetch(url: str, hdrs=None):
        calls.append(url)
        if "search/autocomplete" in url:
            assert hdrs and hdrs["Authorization"] == "Bearer KLUCZ"
            return json.dumps({"success": True, "data": [{"id": 4242}]}).encode()
        if "/grids/game/4242" in url:
            return json.dumps({"success": True,
                               "data": [{"url": "https://img/x.png"}]}).encode()
        if url == "https://img/x.png":
            return png
        return None

    c = SgdbClient("KLUCZ", fetch=fake_fetch)
    gid = c.search("Final Fantasy VII")
    assert gid == 4242
    assert c.best_image(gid) == png


def test_sgdb_client_without_key_is_noop():
    c = SgdbClient("", fetch=lambda u, h=None: pytest.fail("nie wolno wołać sieci"))
    assert c.search("cokolwiek") is None


# --- make_icons_for_dir ----------------------------------------------------------

def test_make_icons_for_dir_grouping_and_cache(tmp_path: Path):
    if not icons.PIL_OK:
        pytest.skip("Pillow niezainstalowane")
    rom_dir = tmp_path / "Sony - PlayStation"
    rom_dir.mkdir()
    (rom_dir / "Final Fantasy VII (USA) (Disc 1).chd").write_bytes(b"x")
    (rom_dir / "Final Fantasy VII (USA) (Disc 2).chd").write_bytes(b"x")
    (rom_dir / "Final Fantasy VII (USA).m3u").write_text("d", encoding="utf-8")
    (rom_dir / "Wild Arms (USA).chd").write_bytes(b"x")
    (rom_dir / "readme.txt").write_text("nie gra", encoding="utf-8")

    png = _png_bytes()
    fetched: list[str] = []

    def fake_fetch(url: str, hdrs=None):
        fetched.append(url)
        if "Named_Boxarts" in url:
            return png
        return None

    st = make_icons_for_dir(rom_dir, "Sony - PlayStation", fetch=fake_fetch,
                            cache_dir=tmp_path / "lcache")
    assert st.done == 2 and st.errors == 0
    out = rom_dir / "icons"
    assert (out / "Final Fantasy VII (USA).ico").exists()   # jedna ikona na grę
    assert (out / "Wild Arms (USA).ico").exists()
    assert not (out / "Final Fantasy VII (USA) (Disc 1).ico").exists()

    # drugi przebieg: wszystko z cache, zero sieci
    fetched.clear()
    st2 = make_icons_for_dir(rom_dir, "Sony - PlayStation", fetch=fake_fetch,
                             cache_dir=tmp_path / "lcache")
    assert st2.cached == 2 and st2.done == 0
    assert fetched == []


def test_iter_games_subdir_per_game(tmp_path: Path):
    """Gry w podkatalogach per gra są widziane (tytuł = nazwa katalogu)."""
    from chd_buddy.core.icons import _iter_games
    d = tmp_path / "Dreamcast"
    (d / "Mortal Kombat Gold (USA)").mkdir(parents=True)
    (d / "Mortal Kombat Gold (USA)" / "gra.cue").write_text("F", encoding="utf-8")
    (d / "icons").mkdir()
    (d / "Solo (USA).chd").write_bytes(b"x")
    assert set(_iter_games(d)) == {"Mortal Kombat Gold (USA)", "Solo (USA)"}


def test_make_icons_not_found(tmp_path: Path):
    rom_dir = tmp_path / "PS2"
    rom_dir.mkdir()
    (rom_dir / "Nieznana Gra (Japan).iso").write_bytes(b"x")
    st = make_icons_for_dir(rom_dir, "PS2", fetch=lambda u, h=None: None,
                            cache_dir=tmp_path / "lcache")
    assert st.not_found == 1 and st.done == 0
    assert not (rom_dir / "icons").exists()


def test_alias_following(tmp_path: Path):
    """Plik-odsyłacz ('Inna Nazwa.png' w treści) prowadzi do prawdziwego PNG."""
    from chd_buddy.core.icons import LibretroIndex, fetch_artwork
    png = _png_bytes()

    def fake_fetch(url: str, hdrs=None):
        if url.endswith("/git/trees/master"):
            return json.dumps({"tree": [{"path": "Named_Boxarts", "sha": "abc"}]}).encode()
        if url.endswith("/git/trees/abc"):
            return json.dumps({"tree": [
                {"path": "Gra (USA) (v2.00).png"},
                {"path": "Gra (USA, Canada) (v1.01).png"}]}).encode()
        if "Gra%20%28USA%29%20%28v2.00%29" in url:
            return "Gra (USA) (v1.01).png".encode()   # alias WISZĄCY (celu brak)
        if "Gra%20%28USA%2C%20Canada%29%20%28v1.01%29" in url and "Named_Boxarts" in url:
            return png
        return None

    idx = LibretroIndex("PS2", fetch=fake_fetch, cache_dir=tmp_path)
    # łańcuch: v2.00 (alias) -> v1.01 (404) -> fuzzy -> (USA, Canada) (v1.01)
    art = fetch_artwork("PS2", "Gra (USA)", libretro=idx, fetch=fake_fetch)
    assert art == png


def test_artwork_candidates_and_save_icon(tmp_path: Path):
    """Kandydaci do ręcznego wyboru: Libretro + gridy SGDB (miniatury),
    zapis wybranego jako .ico (pełny obraz dociągany z url)."""
    if not icons.PIL_OK:
        pytest.skip("Pillow niezainstalowane")
    from chd_buddy.core.icons import artwork_candidates, save_icon
    png_small = _png_bytes(32, 32)
    png_full = _png_bytes(300, 300)

    def fake_fetch(url: str, hdrs=None):
        if url.endswith("/git/trees/master"):
            return json.dumps({"tree": [{"path": "Named_Boxarts", "sha": "abc"}]}).encode()
        if url.endswith("/git/trees/abc"):
            return json.dumps({"tree": [{"path": "Gra (USA).png"}]}).encode()
        if "Named_Boxarts" in url and "Gra" in url:
            return png_full
        if "search/autocomplete" in url:
            return json.dumps({"success": True, "data": [{"id": 7}]}).encode()
        if "/grids/game/7" in url:
            return json.dumps({"success": True, "data": [
                {"url": "https://img/full.png", "thumb": "https://img/thumb.png",
                 "width": 512, "height": 512}]}).encode()
        if url == "https://img/thumb.png":
            return png_small
        if url == "https://img/full.png":
            return png_full
        return None

    from chd_buddy.core.icons import SgdbClient
    sgdb = SgdbClient("KLUCZ", fetch=fake_fetch)
    cands = artwork_candidates("PS2", "Gra (USA)", sgdb=sgdb,
                               fetch=fake_fetch, cache_dir=tmp_path)
    labels = [c["label"] for c in cands]
    assert any("Libretro boxart" in l for l in labels)
    assert any("SGDB" in l for l in labels)

    sg = next(c for c in cands if "SGDB" in c["label"])
    assert sg["preview"] == png_small and sg["full"] is None
    ico = tmp_path / "out" / "Gra (USA).ico"
    assert save_icon(sg, ico, fetch=fake_fetch)     # dociąga full.png
    assert ico.read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_artwork_candidates_igdb_tgdb_and_progress(tmp_path: Path):
    """IGDB (cover/artwork przez POST+token) i TheGamesDB (boxart) jako
    dodatkowe źródła; pasek postępu dostaje sygnały pobierania."""
    if not icons.PIL_OK:
        pytest.skip("Pillow niezainstalowane")
    from chd_buddy.core.icons import (IgdbClient, TgdbClient,
                                      artwork_candidates)
    png = _png_bytes(48, 48)

    def fetch(url: str, hdrs=None):
        # bez Libretro/SGDB — tylko obrazy IGDB/TGDB
        if url.endswith("/git/trees/master"):
            return json.dumps({"tree": []}).encode()
        if "images.igdb.com" in url:
            return png
        if "thegamesdb.net" in url and "ByGameName" in url:
            return json.dumps({"code": 200, "data": {"games": [{"id": 5}]},
                               "include": {"boxart": {
                                   "base_url": {"original": "https://tgdb/o/",
                                                "thumb": "https://tgdb/t/"},
                                   "data": {"5": [{"type": "boxart",
                                                   "side": "front",
                                                   "filename": "box.jpg"}]}}}}).encode()
        if url.startswith("https://tgdb/"):
            return png
        return None

    def post(url: str, body: bytes, hdrs=None):
        if "oauth2/token" in url:
            return json.dumps({"access_token": "Tok", "expires_in": 3600}).encode()
        if url.endswith("/games"):
            return json.dumps([{"id": 1, "name": "Gra",
                                "cover": {"url": "//images.igdb.com/t_thumb/c.jpg"},
                                "artworks": [{"url": "//images.igdb.com/t_thumb/a.jpg"}]}]).encode()
        return None

    igdb = IgdbClient("cid", "secret", fetch=fetch, post=post)
    tgdb = TgdbClient("tkey", fetch=fetch)
    events: list[tuple] = []
    cands = artwork_candidates(
        "PS2", "Gra (USA)", igdb=igdb, tgdb=tgdb, fetch=fetch,
        cache_dir=tmp_path, on_progress=lambda i, n, m: events.append((i, n, m)))
    labels = [c["label"] for c in cands]
    assert any("IGDB cover" in l for l in labels)
    assert any("IGDB artwork" in l for l in labels)
    assert any("TGDB boxart" in l for l in labels)
    # HD podmiana t_thumb → t_1080p w URL-u pełnego obrazu IGDB
    igdb_cov = next(c for c in cands if "IGDB cover" in c["label"])
    assert "t_1080p" in igdb_cov["url"]
    # pasek postępu dostał sygnały (faza pobierania z total>0)
    assert any(n > 0 for i, n, m in events)


def test_libretro_index_fuzzy_region_match(tmp_path: Path):
    """Redump '(USA)' trafia w miniaturę '(USA, Canada)' przez listę repo."""
    from chd_buddy.core.icons import LibretroIndex
    names = ["Final Fantasy X (USA, Canada)", "Final Fantasy X (Europe, Australia)",
             "Final Fantasy X-2 (USA, Canada)",
             "Gran Turismo 4 (USA) (Beta) (2006-06-06)",
             "Gran Turismo 4 (USA) (v2.00)",
             "Gran Turismo 4 (USA, Canada)"]
    tree_calls: list[str] = []

    def fake_fetch(url: str, hdrs=None):
        tree_calls.append(url)
        if url.endswith("/git/trees/master"):
            return json.dumps({"tree": [{"path": "Named_Boxarts", "sha": "abc"}]}).encode()
        if url.endswith("/git/trees/abc"):
            return json.dumps({"tree": [{"path": f"{n}.png"} for n in names]}).encode()
        return None

    idx = LibretroIndex("PS2", fetch=fake_fetch, cache_dir=tmp_path)
    assert idx.find("Final Fantasy X (USA)") == "Final Fantasy X (USA, Canada)"
    assert idx.find("Final Fantasy X (Europe) (Disc 1)") == "Final Fantasy X (Europe, Australia)"
    assert idx.find("Gran Turismo 4 (USA, Canada)") == "Gran Turismo 4 (USA, Canada)"
    # wersja bez nadmiarowych tagów wygrywa z Betą o tym samym regionie
    assert idx.find("Gran Turismo 4 (USA)") == "Gran Turismo 4 (USA, Canada)"
    assert idx.find("Nieistniejąca Gra (USA)") is None
    # 'X-2' nie może wchłonąć 'X' — tytuł bazowy musi być identyczny
    assert idx.find("Final Fantasy X-2 (USA)") == "Final Fantasy X-2 (USA, Canada)"

    # cache na dysku: druga instancja nie woła GitHub API
    tree_calls.clear()
    idx2 = LibretroIndex("PS2", fetch=fake_fetch, cache_dir=tmp_path)
    assert idx2.find("Final Fantasy X (USA)") == "Final Fantasy X (USA, Canada)"
    assert tree_calls == []

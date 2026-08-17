from __future__ import annotations

from pathlib import Path

from chd_buddy.core import translations as tr
from chd_buddy.core.datstore import DatStore
from chd_buddy.core.fileindex import FileIndex
from chd_buddy.core.matcher import match_store, RomState
from tests.test_datcache import _write_dat


# --- parsowanie --------------------------------------------------------------

def test_parse_langs_and_base_title():
    assert tr.parse_langs("Game (Japan) [T-En by Foo]") == ["en"]
    assert tr.parse_langs("Game [T+Eng1.0]") == ["en"]
    assert tr.parse_langs("Game (En,Fr,De)") == ["en", "fr", "de"]
    assert tr.parse_langs("Game (English)") == ["en"]
    # region → język (fallback); jawny (En)/English wygrywa nad regionem
    assert tr.parse_langs("Game (Japan)") == ["ja"]
    assert tr.parse_langs("Game (USA)") == ["en"]
    assert tr.parse_langs("Game (USA, Europe)") == ["en"]
    assert tr.parse_langs("Game (Japan) (En)") == ["en"]      # jawne > region
    assert tr.parse_langs("De Blob (USA)") == ["en"]          # brak fałszywego 'de'
    assert tr.parse_langs("Game [T-EnByItalianTeam]") == ["en"]  # bez 'it'
    assert tr.base_title("Zelda no Densetsu (Japan) [T-En]") == "zelda no densetsu"
    assert tr.is_translation("X [T-En]") and not tr.is_translation("X (Japan)")


def test_translation_label():
    assert tr.translation_label("Game (Japan) [T-En by Foo v1.1]") \
        == "[T-En by Foo v1.1]"
    assert tr.translation_label("Game (Japan)") == ""


# --- indeks wariantów --------------------------------------------------------

def _discover(tmp_path, dat_rel, sysname, games):
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    _write_dat(dat_root / dat_rel, sysname, games)
    entries = DatStore(dat_root, rom_root).discover()
    return entries


def test_build_variant_index_only_from_translations_role(tmp_path):
    entries = _discover(
        tmp_path, "trans/t.dat", "SNES Translations",
        {"Zelda (Japan) [T-En]": {"z.sfc": b"ENGLISH" * 100},
         "Zelda (Japan) [T-Fr]": {"z.sfc": b"FRANCAIS" * 100}})
    idx = FileIndex(tmp_path / "i.sqlite3")
    reports = match_store(entries, idx)

    # rola collection → brak wariantów
    none_idx = tr.build_variant_index(reports, lambda e: {"role": "collection"})
    assert none_idx == {}

    # rola translations → warianty po tytule bazowym, z językami
    vi = tr.build_variant_index(reports, lambda e: {"role": "translations"})
    variants = tr.variants_for(vi, "Zelda (Japan)")
    langs = sorted(v.lang_str for v in variants)
    assert langs == ["en", "fr"]
    assert tr.variants_for(vi, "Zelda (Japan)", lang="fr")[0].lang_str == "fr"
    assert tr.all_languages(vi) == ["en", "fr"]


def test_variant_language_inherited_from_dat_name(tmp_path):
    """Język bywa TYLKO w nazwie DAT-u („… [T-En] Collection"), a gry mają
    czyste nazwy — wariant musi i tak dostać język z DAT-u (regresja: filtr
    języka był pusty)."""
    entries = _discover(
        tmp_path, "tren/x.dat", "Microsoft - MSX [T-En] Collection",
        {"Cool Game (Japan)": {"g.rom": b"PATCHED" * 100},
         "Other Game (Japan)": {"o.rom": b"PATCHED2" * 100}})
    idx = FileIndex(tmp_path / "i.sqlite3")
    reports = match_store(entries, idx)
    vi = tr.build_variant_index(reports, lambda e: {"role": "translations"})
    v = tr.variants_for(vi, "Cool Game (Japan)")
    assert v and v[0].lang_str == "en"           # odziedziczone z nazwy DAT-u
    assert tr.all_languages(vi) == ["en"]
    assert tr.variants_for(vi, "Cool Game (Japan)", lang="en")


# --- store -------------------------------------------------------------------

def test_store_roundtrip(tmp_path):
    p = tmp_path / tr.TranslationStore.FILENAME
    s = tr.TranslationStore(p)
    s.set_manual("SNES", "Zelda (Japan)", sha1="abc", name="Zelda [T-En]",
                 src=r"D:\t\zelda.sfc", lang="en")
    assert s.has("SNES", "Zelda (Japan)")
    s.save()
    s2 = tr.TranslationStore(p)
    rec = s2.get("SNES", "Zelda (Japan)")
    assert rec and rec["sha1"] == "abc" and rec["lang"] == "en"
    assert s2.remove("SNES", "Zelda (Japan)")
    assert not s2.has("SNES", "Zelda (Japan)")


# --- matcher honoruje podmianę ----------------------------------------------

def test_matcher_treats_substituted_game_as_have(tmp_path):
    entries = _discover(
        tmp_path, "snes/s.dat", "SNES",
        {"Zelda (Japan)": {"Zelda (Japan).sfc": b"JAPANESE" * 100}})
    idx = FileIndex(tmp_path / "i.sqlite3")
    e = entries[0]
    # symuluj podmieniony slot: plik pod nazwą kanoniczną istnieje (treść inna)
    canon = e.target_dir / "Zelda (Japan).sfc"
    canon.parent.mkdir(parents=True, exist_ok=True)
    canon.write_bytes(b"ENGLISH-PATCHED")      # treść ≠ sumy z DAT

    # bez subs → NIE jest HAVE (zła treść / wrong)
    rep0 = match_store(entries, idx)[0]
    assert not all(s.state == RomState.HAVE for s in rep0.statuses)

    # z subs → HAVE + is_translation (skan nie cofa świadomego wyboru)
    st = tr.TranslationStore(tmp_path / "translations.json")
    st.set_manual(e.name, "Zelda (Japan)", sha1="deadbeef",
                  name="Zelda [T-En]", src=str(canon), lang="en")
    rep = match_store(entries, idx, subs=st.subs)[0]
    assert all(s.state == RomState.HAVE for s in rep.statuses)
    assert all(s.is_translation for s in rep.statuses)


# --- przepływ podmiany / odtworzenia (symlink zamockowany) ------------------

def test_apply_and_restore_substitution(tmp_path, monkeypatch):
    import chd_buddy.core.linker as linker

    def fake_create_link(link_path, target, is_dir):
        # symulacja symlinku: prawdziwy plik-znacznik wskazujący cel
        Path(link_path).write_text(f"->{target}", encoding="utf-8")
    monkeypatch.setattr(linker, "create_link", fake_create_link)

    coll = tmp_path / "roms" / "snes"; coll.mkdir(parents=True)
    canon = coll / "Zelda (Japan).sfc"
    canon.write_bytes(b"ORYGINAL-JP")           # oryginał w kolekcji
    variant = tmp_path / "roms" / "trans" / "Zelda [T-En].sfc"
    variant.parent.mkdir(parents=True); variant.write_bytes(b"ENGLISH")
    preserve = tr.preserve_dir_for(tmp_path / "ts", "SNES")

    ok = tr.apply_substitution(canon, variant, preserve, make_links=True)
    assert ok
    # oryginał ZACHOWANY (do walidacji/odtworzenia), nie skasowany
    saved = preserve / "Zelda (Japan).sfc"
    assert saved.is_file() and saved.read_bytes() == b"ORYGINAL-JP"
    # slot pod nazwą kanoniczną wskazuje wariant
    assert canon.exists() and canon.read_text().endswith("Zelda [T-En].sfc")

    # ODTWORZENIE cofa podmianę
    ok2 = tr.restore_original(canon, preserve)
    assert ok2
    assert canon.read_bytes() == b"ORYGINAL-JP"
    assert not saved.exists()


def test_apply_substitution_preserves_dir_layout(tmp_path):
    d = tr.preserve_dir_for(r"D:\emu\to sort", "Arcade - Sega - Naomi")
    assert d.name == "Arcade - Sega - Naomi"
    assert d.parent.name == "translated"

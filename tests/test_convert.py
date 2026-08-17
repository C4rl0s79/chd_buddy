"""Testy konwersji formatów (ZIP end-to-end; CHD/RVZ wymagają narzędzi)."""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

from chd_buddy.core.convert import (
    ConvertStats,
    convert_reports,
    current_format,
    pack_zip,
)
from chd_buddy.core.datstore import DatStore
from chd_buddy.core.fileindex import FileIndex
from chd_buddy.core.matcher import match_store
from tests.test_datcache import _write_dat


def test_pack_zip_verifies(tmp_path: Path):
    a = tmp_path / "a.nes"; a.write_bytes(b"KARTRIDZ-A" * 100)
    b = tmp_path / "b.nes"; b.write_bytes(b"KARTRIDZ-B" * 100)
    dst = tmp_path / "gra.zip"
    r = pack_zip([a, b], dst)
    assert r.ok and dst.is_file()
    with zipfile.ZipFile(dst) as z:
        assert hashlib.sha1(z.read("a.nes")).hexdigest() == \
            hashlib.sha1(a.read_bytes()).hexdigest()
    assert not dst.with_name(dst.name + ".chdbuddy_tmp.zip").exists()


def test_pack_zip_honors_level(tmp_path: Path):
    """Poziom DEFLATE działa: 0 (store) daje WIĘKSZY plik niż 9 (maks) dla
    danych ściśliwych. Oba nadal poprawnie się rozpakowują."""
    data = (b"KOMPRESOWALNE-DANE-" * 5000)      # dobrze ściśliwe
    a = tmp_path / "a.bin"; a.write_bytes(data)
    z0 = tmp_path / "store.zip"
    z9 = tmp_path / "max.zip"
    assert pack_zip([a], z0, level=0).ok
    assert pack_zip([a], z9, level=9).ok
    assert z0.stat().st_size > z9.stat().st_size    # 0=store większy niż 9=maks
    with zipfile.ZipFile(z9) as z:
        assert z.read("a.bin") == data              # maks nadal poprawny


def test_compression_settings_persist(tmp_path, monkeypatch):
    from chd_buddy.core.settings import Settings
    monkeypatch.setattr(Settings, "path", classmethod(
        lambda cls: tmp_path / "settings.json"))
    s = Settings()
    assert s.zip_level == 6 and s.rvz_level == 5 and s.rvz_block_kb == 128
    s.zip_level = 9; s.rvz_level = 19; s.rvz_block_kb = 256
    s.compression_preset = "max"
    s.save()
    s2 = Settings.load()
    assert s2.zip_level == 9 and s2.rvz_level == 19 and s2.rvz_block_kb == 256
    assert s2.compression_preset == "max"


def test_current_format():
    assert current_format([Path("x.chd")]) == "chd"
    assert current_format([Path("x.zip")]) == "zip"
    assert current_format([Path("x.rvz")]) == "rvz"
    assert current_format([Path("a.bin"), Path("a.cue")]) == "loose"


def test_convert_reports_loose_to_zip(tmp_path: Path):
    """Kartridż luzem → ZIP przy regule format=zip; źródło skasowane,
    plik wpisany do indeksu, weryfikacja SHA-1."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data = b"SUPER-MARIO" * 100
    _write_dat(dat_root / "nes.dat",
               "Nintendo - Nintendo Entertainment System",
               {"Super Mario Bros (USA)": {"Super Mario Bros (USA).nes": data}})
    # plik luzem w katalogu docelowym
    game_dir = rom_root / "Nintendo - Nintendo Entertainment System"
    game_dir.mkdir(parents=True)
    loose = game_dir / "Super Mario Bros (USA).nes"
    loose.write_bytes(data)

    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)

    def rules_fn(entry):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES)
        r["format"] = "zip"
        return r

    st = convert_reports(reports, rules_fn, {"settings": None}, index=idx)
    assert st.converted == 1 and st.errors == 0
    zip_path = game_dir / "Super Mario Bros (USA).zip"
    assert zip_path.is_file()
    assert not loose.exists()                  # źródło skasowane
    with zipfile.ZipFile(zip_path) as z:
        assert z.read("Super Mario Bros (USA).nes") == data
    assert idx.lookup(zip_path) is not None     # w indeksie
    assert idx.lookup(loose) is None


def test_convert_reports_calls_on_converted(tmp_path: Path):
    """Konwersja ZGŁASZA utworzony plik przez on_converted — rebuilder
    rejestruje go jako kanoniczny, więc faza sprzątania NIE wrzuci świeżego
    CHD/ZIP do ToSort (raportowany błąd: „skonwertowano do CHD → ToSort")."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data = b"MARIO" * 100
    _write_dat(dat_root / "nes.dat", "Nintendo - Nintendo Entertainment System",
               {"Mario (USA)": {"Mario (USA).nes": data}})
    d = rom_root / "Nintendo - Nintendo Entertainment System"
    d.mkdir(parents=True)
    (d / "Mario (USA).nes").write_bytes(data)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r

    produced: list = []
    st = convert_reports(reports, rules_fn, {"settings": None}, index=idx,
                         on_converted=produced.append)
    assert st.converted == 1
    zip_path = d / "Mario (USA).zip"
    assert produced == [zip_path]                 # zgłoszony jako kanoniczny
    assert zip_path.is_file()


def test_convert_defers_source_deletion_until_all_done(tmp_path: Path):
    """Źródła kasowane DOPIERO po przerobieniu WSZYSTKICH gier — w trakcie
    konwersji zostają (współdzielone ścieżki gier wielopłytowych muszą być
    dostępne dla kolejnych płyt). Po całości znikają."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    g1 = b"DISC-ONE" * 200
    g2 = b"DISC-TWO" * 200
    _write_dat(dat_root / "ps.dat", "Sony - PlayStation",
               {"Gra 1": {"Gra 1.iso": g1}, "Gra 2": {"Gra 2.iso": g2}})
    d = rom_root / "Sony - PlayStation"
    d.mkdir(parents=True)
    (d / "Gra 1.iso").write_bytes(g1)
    (d / "Gra 2.iso").write_bytes(g2)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r

    src1 = d / "Gra 1.iso"
    src2 = d / "Gra 2.iso"
    seen_when_converted: list = []

    def on_conv(final):
        # w chwili zgłoszenia gotowego pliku ŹRÓDŁA jeszcze istnieją (odroczone)
        seen_when_converted.append((src1.exists(), src2.exists()))

    st = convert_reports(reports, rules_fn, {"settings": None}, index=idx,
                         on_converted=on_conv)
    assert st.converted == 2
    # podczas KAŻDej konwersji oba źródła jeszcze były (nic nie skasowane w locie)
    assert seen_when_converted == [(True, True), (True, True)]
    # po CAŁOŚCI — źródła skasowane
    assert not src1.exists() and not src2.exists()
    assert (d / "Gra 1.zip").is_file() and (d / "Gra 2.zip").is_file()


def test_convert_from_source_loose_to_zip(tmp_path: Path, monkeypatch):
    """Konwersja PROSTO ZE ŹRÓDŁA: plik w ToSort → ZIP w docelowym. Docelowy
    NIGDY nie dostaje luźnego pliku; źródło z ToSort skasowane po całości."""
    import chd_buddy.core.convert as cv
    import chd_buddy.core.scratch as sc
    from chd_buddy.core.convert import convert_from_source
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    tosort = tmp_path / "tosort"
    scratch = tmp_path / "ram"; scratch.mkdir()
    monkeypatch.setattr(sc, "pick_scratch_root",
                        lambda need, prefer=None, log=None, fallback=None: scratch)
    data = b"KARTRIDZ" * 200
    _write_dat(dat_root / "nes.dat",
               "Nintendo - Nintendo Entertainment System",
               {"Gra (USA)": {"Gra (USA).nes": data}})
    tosort.mkdir()
    src = tosort / "Gra (USA).nes"
    src.write_bytes(data)                       # ŹRÓDŁO w ToSort, nie w docelowym
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r

    target = rom_root / "Nintendo - Nintendo Entertainment System"
    st, done, to_purge = convert_from_source(reports, rules_fn,
                                             {"settings": None}, index=idx)
    assert st.converted == 1
    zip_path = target / "Gra (USA).zip"
    assert zip_path.is_file()                    # FINAŁ w docelowym
    with zipfile.ZipFile(zip_path) as z:
        assert z.read("Gra (USA).nes") == data
    assert not (target / "Gra (USA).nes").exists()   # docelowy NIE dostał luźnego
    assert done                                      # gra oznaczona (placement pominie)
    # źródło UNIKALNE (jednopłytowe) → skasowane OD RAZU, nie odroczone
    assert not src.exists()
    assert src not in to_purge and not to_purge
    assert not any(scratch.iterdir())                # nic nie zostało na scratchu


def _can_symlink(tmp_path: Path) -> bool:
    t = tmp_path / "_t"; t.write_text("x")
    l = tmp_path / "_l"
    try:
        os.symlink(t, l); return True
    except OSError:
        return False
    finally:
        l.unlink(missing_ok=True); t.unlink(missing_ok=True)


def test_convert_from_source_child_links_not_copies(tmp_path, monkeypatch):
    """Ta sama gra w DAT-cie RODZICA i DZIECKA (1G1R, inna nazwa) — rodzic robi
    plik fizyczny (konwersja), a dziecko idzie ścieżką SYMLINKA (nie druga
    kopia). Sprawdzamy logikę bez uprawnień do symlinków (mock link-helpera)."""
    import chd_buddy.core.scratch as sc
    import chd_buddy.core.convert as cv
    from chd_buddy.core.convert import convert_from_source
    from chd_buddy.core.matcher import match_entry
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    tosort = tmp_path / "ts"
    scratch = tmp_path / "ram"; scratch.mkdir()
    monkeypatch.setattr(sc, "pick_scratch_root",
                        lambda need, prefer=None, log=None, fallback=None: scratch)
    data = b"WSPOLNA-TRESC" * 200
    _write_dat(dat_root / "Full" / "ps.dat", "Sony - PlayStation",
               {"The Game (USA)": {"The Game (USA).bin": data}})
    _write_dat(dat_root / "1G1R" / "ps.dat", "Sony - PlayStation",
               {"The Game": {"The Game.bin": data}})
    tosort.mkdir()
    src = tosort / "The Game (USA).bin"
    src.write_bytes(data)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    parent = next(e for e in entries if "Full" in str(e.dat_path))
    child = next(e for e in entries if "1G1R" in str(e.dat_path))
    reports = [match_entry(parent, idx), match_entry(child, idx)]  # rodzic pierwszy

    links: list = []
    monkeypatch.setattr(cv, "_link_child_to_parent",
                        lambda cf, pf, *a, **k: (links.append((cf, pf)), True)[1])

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r

    st, done, to_purge = convert_from_source(reports, rules_fn,
                                             {"settings": None}, index=idx,
                                             make_links=True)
    p_zip = parent.target_dir / "The Game (USA).zip"
    assert p_zip.is_file()                                # rodzic FIZYCZNIE zrobiony
    assert st.converted == 1                             # tylko RODZIC konwertowany
    assert len(done) == 2                                # obie gry obsłużone
    # dziecko poszło przez LINK z własną nazwą → do pliku rodzica
    assert len(links) == 1
    child_final, parent_final = links[0]
    assert child_final == child.target_dir / "The Game.zip"   # WŁASNA nazwa
    assert parent_final == p_zip                              # do pliku rodzica


def test_convert_from_source_defers_only_shared(tmp_path: Path, monkeypatch):
    """Źródło WSPÓŁDZIELONE przez 2 gry (ta sama ścieżka audio) → odroczone
    (w to_purge, kasowane dopiero purge); UNIKALNE ścieżki → kasowane od razu."""
    import chd_buddy.core.scratch as sc
    from chd_buddy.core.convert import convert_from_source, purge_source_files
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"; tosort = tmp_path / "ts"
    scratch = tmp_path / "ram"; scratch.mkdir()
    monkeypatch.setattr(sc, "pick_scratch_root",
                        lambda need, prefer=None, log=None, fallback=None: scratch)
    shared = b"WSPOLNE-AUDIO" * 300        # identyczna ścieżka w obu grach
    uniq1 = b"DANE-DISC-1" * 300
    uniq2 = b"DANE-DISC-2" * 300
    _write_dat(dat_root / "ps.dat", "Sony - PlayStation",
               {"Gra (Disc 1)": {"Gra (Disc 1) (Track 1).bin": uniq1,
                                 "Gra (Track 2).bin": shared},
                "Gra (Disc 2)": {"Gra (Disc 2) (Track 1).bin": uniq2,
                                 "Gra (Track 2).bin": shared}})
    tosort.mkdir()
    (tosort / "Gra (Disc 1) (Track 1).bin").write_bytes(uniq1)
    (tosort / "Gra (Disc 2) (Track 1).bin").write_bytes(uniq2)
    shared_src = tosort / "Gra (Track 2).bin"
    shared_src.write_bytes(shared)         # JEDEN plik, dopasowany przez obie gry
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r

    st, done, to_purge = convert_from_source(reports, rules_fn,
                                             {"settings": None}, index=idx)
    assert st.converted == 2
    # unikalne ścieżki obu dysków skasowane od razu
    assert not (tosort / "Gra (Disc 1) (Track 1).bin").exists()
    assert not (tosort / "Gra (Disc 2) (Track 1).bin").exists()
    # WSPÓŁDZIELONA ścieżka JESZCZE istnieje (odroczona) i jest w to_purge
    assert shared_src.exists()
    assert any(os.path.normcase(str(p)) == os.path.normcase(str(shared_src))
               for p in to_purge)
    purge_source_files(to_purge, index=idx)
    assert not shared_src.exists()          # skasowana dopiero na końcu


def test_convert_from_source_verifies_and_skips_on_mismatch(tmp_path, monkeypatch):
    """Gdy zawartość źródła NIE zgadza się z DAT-em → gra pominięta (fallback),
    finał NIE powstaje, źródło nietknięte."""
    import chd_buddy.core.scratch as sc
    from chd_buddy.core.convert import convert_from_source
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"; tosort = tmp_path / "ts"
    scratch = tmp_path / "ram"; scratch.mkdir()
    monkeypatch.setattr(sc, "pick_scratch_root",
                        lambda need, prefer=None, log=None, fallback=None: scratch)
    good = b"DOBRE" * 100
    _write_dat(dat_root / "nes.dat", "Nintendo - Nintendo Entertainment System",
               {"Gra (USA)": {"Gra (USA).nes": good}})
    tosort.mkdir()
    src = tosort / "Gra (USA).nes"
    src.write_bytes(good)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)
    # zepsuj plik PO skanie (indeks ma stare sumy, ale gather liczy na nowo)
    src.write_bytes(b"ZLE" * 100)

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r

    st, done, to_purge = convert_from_source(reports, rules_fn,
                                             {"settings": None}, index=idx)
    assert st.converted == 0 and not done and not to_purge
    assert not (rom_root / "Nintendo - Nintendo Entertainment System"
                / "Gra (USA).zip").exists()
    assert src.exists()                          # źródło nietknięte (fallback)


def test_purge_redundant_tosort_tracks(tmp_path: Path):
    """Gra JUŻ zrobiona na CHD (w docelowym) — jej luźne ścieżki wciąż w ToSort
    są kasowane; ale SHA-1 potrzebne NIEzaspokojonej grze są chronione."""
    from chd_buddy.core.convert import _purge_redundant_tosort_tracks
    dat_root = tmp_path / "dats"
    tosort = tmp_path / "ts"
    data = b"SCIEZKA-DANE" * 100
    _write_dat(dat_root / "ps.dat", "Sony - PlayStation",
               {"Gra": {"Gra.bin": data}})
    tosort.mkdir()
    loose = tosort / "Gra.bin"
    loose.write_bytes(data)                       # luźna kopia w ToSort
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort)
    game = DatStore(dat_root, tmp_path / "roms").discover()[0].load().games[0]
    del_pref = [os.path.normcase(str(tosort)).rstrip("\\/") + os.sep]
    sha1 = game.roms[0].sha1.lower()

    # CHRONIONE: SHA-1 potrzebne niezaspokojonej grze → nie kasuj
    n = _purge_redundant_tosort_tracks(game, idx, del_pref, {sha1},
                                       lambda m: None)
    assert n == 0 and loose.exists()

    # bez ochrony → skasowane z ToSort
    n = _purge_redundant_tosort_tracks(game, idx, del_pref, set(),
                                       lambda m: None)
    assert n == 1 and not loose.exists()

    # plik POZA ToSort (inny prefix) — nietykalny
    other = tmp_path / "gdzie_indziej"
    other.mkdir()
    (other / "Gra.bin").write_bytes(data)
    idx.scan(other)
    n = _purge_redundant_tosort_tracks(game, idx, del_pref, set(),
                                       lambda m: None)
    assert n == 0 and (other / "Gra.bin").exists()


def test_purge_redundant_tosort_archive(tmp_path: Path):
    """ZIP w ToSort zawierający grę JUŻ zrobioną na CHD jest kasowany; ale ZIP,
    którego członek jest potrzebny NIEzaspokojonej grze — chroniony."""
    from chd_buddy.core.convert import _purge_redundant_tosort_tracks
    dat_root = tmp_path / "dats"
    tosort = tmp_path / "ts"; tosort.mkdir()
    data = b"BIN-DANE" * 100
    _write_dat(dat_root / "ps.dat", "Sony - PlayStation",
               {"Gra": {"Gra.bin": data}})
    # źródło jako ZIP w ToSort
    zpath = tosort / "Gra.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Gra.bin", data)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort, full=True)
    game = DatStore(dat_root, tmp_path / "roms").discover()[0].load().games[0]
    del_pref = [os.path.normcase(str(tosort)).rstrip("\\/") + os.sep]
    sha1 = game.roms[0].sha1.lower()

    # CHRONIONE: sha1 potrzebne niezaspokojonej grze → ZIP zostaje
    assert _purge_redundant_tosort_tracks(game, idx, del_pref, {sha1},
                                          lambda m: None) == 0
    assert zpath.exists()
    # bez ochrony → ZIP skasowany
    assert _purge_redundant_tosort_tracks(game, idx, del_pref, set(),
                                          lambda m: None) == 1
    assert not zpath.exists()


def test_relink_verified_duplicate_replaces_physical_with_link(tmp_path, monkeypatch):
    """DZIECKO z WŁASNYM fizycznym CHD identycznym z rodzicem → fizyczna kopia
    skasowana, w jej miejscu symlink do rodzica; wpis w indeksie = link."""
    if not _can_symlink(tmp_path):
        import pytest
        pytest.skip("brak uprawnień do symlinków")
    from chd_buddy.core.convert import _relink_verified_duplicate
    from chd_buddy.core.datfile import game_profile
    dat_root = tmp_path / "dats"
    data = b"TRESC-GRY" * 500
    _write_dat(dat_root / "ps.dat", "Sony - PlayStation",
               {"Gra": {"Gra.bin": data}})
    game = DatStore(dat_root, tmp_path / "roms").discover()[0].load().games[0]
    prof = game_profile(game.data_roms)

    pdir = tmp_path / "Full" / "Sony - PlayStation"; pdir.mkdir(parents=True)
    cdir = tmp_path / "1G1R" / "Sony - PlayStation"; cdir.mkdir(parents=True)
    parent = pdir / "Gra.chd"; parent.write_bytes(b"CHD-RODZICA" * 100)
    child = cdir / "Gra.chd"; child.write_bytes(b"CHD-DZIECKA-BLAD" * 100)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(tmp_path / "Full"); idx.scan(tmp_path / "1G1R")
    idx.set_data_sha1(parent, prof)            # rodzic zweryfikowany po zawartości
    idx.set_data_sha1(child, prof)

    ok = _relink_verified_duplicate(child, parent, prof, idx, True, [False],
                                    False, lambda m: None)
    assert ok is True
    assert os.path.islink(child)                       # dziecko = symlink
    assert Path(os.readlink(child)) == parent or child.resolve() == parent.resolve()
    assert parent.is_file() and not os.path.islink(parent)   # rodzic nietknięty
    row = idx.lookup(child)
    assert row is not None and row["is_link"]           # w indeksie = link


def test_relink_verified_duplicate_safe_without_links(tmp_path):
    """Gdy symlinków NIE można zrobić (albo dry_run) → fizyczna kopia dziecka
    NIE jest kasowana (żadnych strat)."""
    from chd_buddy.core.convert import _relink_verified_duplicate
    parent = tmp_path / "parent.chd"; parent.write_bytes(b"P" * 100)
    child = tmp_path / "child.chd"; child.write_bytes(b"C" * 100)
    # brak uprawnień/wyłączone → nic nie kasujemy, zwraca False
    assert _relink_verified_duplicate(child, parent, None, None, False,
                                      [False], False, lambda m: None) is False
    assert child.is_file() and not os.path.islink(child)
    # dry_run → też nic nie ruszamy, ale zwraca True (podgląd)
    assert _relink_verified_duplicate(child, parent, None, None, True,
                                      [False], True, lambda m: None) is True
    assert child.is_file() and not os.path.islink(child)
    # rodzic NIE istnieje → pomiń (nie skasuj jedynej kopii)
    gone = tmp_path / "nie_ma.chd"
    assert _relink_verified_duplicate(child, gone, None, None, True,
                                      [False], False, lambda m: None) is False
    assert child.is_file()


def test_purge_child_loose_duplicates(tmp_path):
    """Luźne tory gry w katalogu DOCELOWYM (błędnie wypakowane) są kasowane, bo
    gra jest na CHD; plik .chd i sha1 chronione (needed) zostają."""
    from chd_buddy.core.convert import _purge_child_loose_duplicates
    dat_root = tmp_path / "dats"
    b1 = b"TOR-JEDEN" * 100
    b2 = b"TOR-DWA" * 100
    _write_dat(dat_root / "ps.dat", "Sony - PlayStation",
               {"Gra": {"Gra.bin": b1, "Gra.cue": b2}})
    game = DatStore(dat_root, tmp_path / "roms").discover()[0].load().games[0]
    tdir = tmp_path / "1G1R" / "Sony - PlayStation"; tdir.mkdir(parents=True)
    (tdir / "Gra.bin").write_bytes(b1)          # błędnie wypakowany tor
    (tdir / "Gra.cue").write_bytes(b2)
    (tdir / "Gra.chd").write_bytes(b"CHD" * 50)  # właściwy plik — NIE ruszać
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tmp_path / "1G1R")

    # dry_run: liczy, nic nie kasuje
    n = _purge_child_loose_duplicates(game, tdir, idx, set(), lambda m: None,
                                      dry_run=True)
    assert n == 2 and (tdir / "Gra.bin").exists()

    # chroniony sha1 jednego toru → skasowany tylko drugi
    sha1_bin = next(r.sha1.lower() for r in game.roms if r.name.endswith(".bin"))
    n = _purge_child_loose_duplicates(game, tdir, idx, {sha1_bin},
                                      lambda m: None, dry_run=False)
    assert n == 1
    assert (tdir / "Gra.bin").exists()          # chroniony — został
    assert not (tdir / "Gra.cue").exists()      # skasowany
    assert (tdir / "Gra.chd").is_file()         # .chd nietknięty

    # bez ochrony → reszta luźnych skasowana, .chd zostaje
    n = _purge_child_loose_duplicates(game, tdir, idx, set(), lambda m: None,
                                      dry_run=False)
    assert n == 1 and not (tdir / "Gra.bin").exists()
    assert (tdir / "Gra.chd").is_file()


def test_purge_redundant_tosort_archive_crc_only(tmp_path: Path):
    """ZIP w ToSort z członkami BEZ SHA-1 (skan SZYBKI — tylko CRC32) jest i
    tak kasowany, gdy gra jest już na CHD. Regresja: kasowanie szukało wyłącznie
    po SHA-1, więc ZIP-y wciągnięte skanem szybkim nigdy nie znikały."""
    from chd_buddy.core.convert import _purge_redundant_tosort_tracks
    dat_root = tmp_path / "dats"
    tosort = tmp_path / "ts"; tosort.mkdir()
    data = b"BIN-DANE-3DO" * 100
    _write_dat(dat_root / "3do.dat", "Panasonic - 3DO Interactive Multiplayer",
               {"Gra": {"Gra.bin": data}})
    zpath = tosort / "Gra.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Gra.bin", data)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort, full=False)
    game = DatStore(dat_root, tmp_path / "roms").discover()[0].load().games[0]
    # SYMULACJA STAREJ bazy „tylko CRC" (sprzed zmiany: nowe archiwa skanujemy
    # teraz PEŁNIE). Zerujemy SHA-1/MD5 członka, by sprawdzić fallback po CRC.
    idx._db.execute("UPDATE members SET sha1='', md5='' WHERE archive=?",
                    (str(zpath),))
    idx._db.commit()
    mem = idx._db.execute("SELECT sha1, crc32 FROM members WHERE archive=?",
                          (str(zpath),)).fetchone()
    assert (mem["sha1"] or "") == "" and mem["crc32"], "człon powinien mieć sam CRC"
    del_pref = [os.path.normcase(str(tosort)).rstrip("\\/") + os.sep]
    crc = game.roms[0].crc.lower().zfill(8); size = game.roms[0].size

    # CHRONIONE po CRC+rozmiar (potrzebne niezaspokojonej grze) → ZIP zostaje
    assert _purge_redundant_tosort_tracks(
        game, idx, del_pref, set(), lambda m: None,
        needed_crc={(crc, size)}) == 0
    assert zpath.exists()
    # bez ochrony → ZIP skasowany mimo braku SHA-1 u członka
    assert _purge_redundant_tosort_tracks(
        game, idx, del_pref, set(), lambda m: None) == 1
    assert not zpath.exists()


def test_convert_reports_auto_format(tmp_path: Path):
    """format=auto: kartridż→zip (bez narzędzi CHD/RVZ)."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data = b"ZELDA" * 100
    _write_dat(dat_root / "snes.dat",
               "Nintendo - Super Nintendo Entertainment System",
               {"Zelda (USA)": {"Zelda (USA).sfc": data}})
    d = rom_root / "Nintendo - Super Nintendo Entertainment System"
    d.mkdir(parents=True)
    (d / "Zelda (USA).sfc").write_bytes(data)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)

    def rules_fn(entry):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "auto"
        return r

    st = convert_reports(reports, rules_fn, {"settings": None}, index=idx)
    assert st.converted == 1
    assert (d / "Zelda (USA).zip").is_file()


def test_convert_reports_dry_run(tmp_path: Path):
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data = b"X" * 100
    _write_dat(dat_root / "nes.dat", "Nintendo - Nintendo Entertainment System",
               {"G": {"G.nes": data}})
    d = rom_root / "Nintendo - Nintendo Entertainment System"
    d.mkdir(parents=True); (d / "G.nes").write_bytes(data)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r

    st = convert_reports(reports, rules_fn, {"settings": None}, index=idx,
                         dry_run=True)
    assert st.converted == 1
    assert (d / "G.nes").exists()               # nic nie zmienione
    assert not (d / "G.zip").exists()


def test_purge_temp_artifacts(tmp_path):
    """Śmieci po przerwanej konwersji (chdbuddy_*, *.rtcheck.*) są kasowane —
    skaner je ignoruje, więc bez tego zostawały na dysku na zawsze."""
    from chd_buddy.core.convert import purge_temp_artifacts
    d = tmp_path / "ps2"
    (d / "chdbuddy_abc123").mkdir(parents=True)
    (d / "chdbuddy_abc123" / "Gra.chd.rtcheck.img").write_bytes(b"x" * 4096)
    (d / "Gra.chd.rtcheck.img").write_bytes(b"y" * 2048)
    (d / "Gra.chd").write_bytes(b"z" * 1024)          # prawdziwy plik
    n, size = purge_temp_artifacts([d])
    assert n >= 2 and size >= 2048
    assert (d / "Gra.chd").is_file()                  # nie ruszamy prawdziwych
    assert not (d / "Gra.chd.rtcheck.img").exists()
    assert not (d / "chdbuddy_abc123").exists()


def test_convert_skips_when_not_enough_free_space(tmp_path, monkeypatch):
    """Konwersja NIE startuje, gdy brakuje miejsca — inaczej pada w połowie
    i zostawia wypakowany obraz zapychający dysk."""
    import chd_buddy.core.convert as cv
    import chd_buddy.core.scratch as sc
    src = tmp_path / "Gra.iso"
    src.write_bytes(b"x" * (1 << 20))
    st = cv.ConvertStats()
    # NIGDZIE nie ma miejsca (RAM ani dysk) => scratch None => pomiń
    monkeypatch.setattr(sc, "pick_scratch_root", lambda need, prefer=None, log=None, fallback=None: None)
    called = {"n": 0}
    monkeypatch.setattr(cv, "disc_to_chd",
                        lambda *a, **k: called.__setitem__("n", 1))
    ok = cv._convert_one([src], tmp_path, "Gra", "chd", False, 1,
                         {"settings": None}, None, False, lambda m: None, st)
    assert ok is False and st.skipped == 1
    assert called["n"] == 0                # konwersja nawet nie ruszyła
    assert src.is_file()                   # źródło nietknięte


def test_convert_builds_on_scratch_deletes_source_then_moves(tmp_path, monkeypatch):
    """Konwersja buduje na SCRATCH, KASUJE źródło (zwalnia dysk docelowy),
    dopiero potem przenosi gotowy plik na miejsce — zajętość D: nie rośnie."""
    import chd_buddy.core.convert as cv
    import chd_buddy.core.scratch as sc
    target = tmp_path / "rom1" / "psx"
    target.mkdir(parents=True)
    scratch = tmp_path / "ram"
    scratch.mkdir()
    src = target / "Gra.iso"
    src.write_bytes(b"ORYGINAL" * 1000)
    order = []
    monkeypatch.setattr(sc, "pick_scratch_root", lambda need, prefer=None, log=None, fallback=None: scratch)

    def fake_pack(files, dst, log=lambda m: None, level=6, **kw):
        # zapisuje wyjscie na SCRATCH; zrodlo musi jeszcze istniec
        assert str(scratch) in str(dst)
        assert all(Path(f).exists() for f in files), "zrodlo skasowane za wczesnie!"
        Path(dst).write_bytes(b"SKONWERTOWANE")
        order.append("build")
        return cv.ConvertResult(True, dst=Path(dst))

    monkeypatch.setattr(cv, "pack_zip", fake_pack)
    st = cv.ConvertStats()
    ok = cv._convert_one([src], target, "Gra", "zip", False, 1,
                         {"settings": None}, None, False,
                         lambda m: order.append(f"log:{m}") if False else None, st)
    assert ok is True
    # zrodlo skasowane, finalny plik na MIEJSCU (nie na scratch)
    assert not src.exists()
    final = target / "Gra.zip"
    assert final.is_file() and final.read_bytes() == b"SKONWERTOWANE"
    # nic nie zostalo na scratch
    assert not any(scratch.iterdir())


def test_zip_incompatible_methods_detects(tmp_path: Path):
    """Wykrywanie niezgodnej kompresji ZIP (poza STORE/DEFLATE)."""
    from chd_buddy.core.convert import zip_incompatible_methods
    data = b"KOMPRESOWALNE" * 500
    ok = tmp_path / "ok.zip"
    with zipfile.ZipFile(ok, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.bin", data)
    assert zip_incompatible_methods(ok) == set()      # DEFLATE — zgodny
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w", zipfile.ZIP_BZIP2) as z:  # BZIP2 = niezgodny
        z.writestr("a.bin", data)
    assert zipfile.ZIP_BZIP2 in zip_incompatible_methods(bad)


def test_repack_zip_to_deflate_preserves_content(tmp_path: Path):
    """Repack niezgodnego ZIP-a (BZIP2) → DEFLATE: zawartość i nazwy zachowane,
    po repacku metoda zgodna."""
    from chd_buddy.core.convert import (repack_zip_to_deflate,
                                        zip_incompatible_methods)
    a = b"PLIK-A" * 1000; b = b"PLIK-B" * 800
    zp = tmp_path / "gra.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_BZIP2) as z:
        z.writestr("gra.z64", a); z.writestr("gra.txt", b)
    assert zip_incompatible_methods(zp)               # przed: niezgodny
    r = repack_zip_to_deflate(zp, level=6)
    assert r.ok
    assert zip_incompatible_methods(zp) == set()      # po: zgodny (deflate)
    with zipfile.ZipFile(zp) as z:
        assert z.read("gra.z64") == a and z.read("gra.txt") == b
        assert all(i.compress_type in (0, 8) for i in z.infolist())


def test_repack_zip_noop_when_already_deflate(tmp_path: Path):
    from chd_buddy.core.convert import repack_zip_to_deflate
    zp = tmp_path / "d.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("x.bin", b"DANE" * 100)
    before = zp.read_bytes()
    r = repack_zip_to_deflate(zp, level=6)
    assert r.ok and zp.read_bytes() == before         # nietknięty (już zgodny)


def test_repack_incompatible_zips_walk(tmp_path: Path):
    """repack_incompatible_zips przechodzi drzewo i naprawia tylko niezgodne."""
    from chd_buddy.core.convert import (repack_incompatible_zips,
                                        zip_incompatible_methods)
    d = tmp_path / "roms" / "n64"; d.mkdir(parents=True)
    with zipfile.ZipFile(d / "bad.zip", "w", zipfile.ZIP_BZIP2) as z:
        z.writestr("bad.z64", b"A" * 2000)
    with zipfile.ZipFile(d / "good.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("good.z64", b"B" * 2000)
    n, err = repack_incompatible_zips([tmp_path / "roms"], level=6)
    assert n == 1 and err == 0
    assert zip_incompatible_methods(d / "bad.zip") == set()

    # dry_run: liczy, nie zmienia
    with zipfile.ZipFile(d / "bad2.zip", "w", zipfile.ZIP_BZIP2) as z:
        z.writestr("b2.z64", b"C" * 2000)
    n2, _ = repack_incompatible_zips([tmp_path / "roms"], dry_run=True)
    assert n2 == 1
    assert zip_incompatible_methods(d / "bad2.zip")   # nadal niezgodny (podgląd)


def test_pack_zip_method_zstd_if_available(tmp_path: Path):
    """Gdy Python wspiera ZSTD w ZIP — pack_zip method='zstd' go użyje."""
    zst = getattr(zipfile, "ZIP_ZSTANDARD", None)
    if zst is None:
        import pytest
        pytest.skip("Python bez ZIP_ZSTANDARD (<3.14)")
    from chd_buddy.core.convert import pack_zip
    data = b"DANE-DO-ZSTD" * 500
    f = tmp_path / "a.bin"; f.write_bytes(data)
    dst = tmp_path / "z.zip"
    assert pack_zip([f], dst, method="zstd").ok
    with zipfile.ZipFile(dst) as z:
        assert z.infolist()[0].compress_type == zst
        assert z.read("a.bin") == data


def test_chd_synthesizes_cue_for_bare_bins(tmp_path, monkeypatch):
    """Gołe .bin bez cue → SYNTETYZUJEMY cue (tor 1 = dane, reszta AUDIO) i
    wołamy createcd na CUE, nigdy na gołym binie (anty-zawieszenie). Kolejność
    torów wg '(Track N)'."""
    import chd_buddy.core.convert as cv
    from chd_buddy.core.convert import _convert_one, ConvertStats
    target = tmp_path / "naomi2"; target.mkdir()
    b1 = target / "gra (Track 1).bin"; b1.write_bytes(b"BIN1" * 100)
    b2 = target / "gra (Track 2).bin"; b2.write_bytes(b"BIN2" * 100)
    seen = {}

    def fake_disc_to_chd(main, dst, chdman, settings, *, log=None,
                         on_progress=None):
        seen["main"] = Path(main).name
        seen["cue_text"] = Path(main).read_text(encoding="utf-8")
        Path(dst).write_bytes(b"CHD")
        return cv.ConvertResult(True, dst=Path(dst))
    monkeypatch.setattr(cv, "disc_to_chd", fake_disc_to_chd)
    import chd_buddy.core.scratch as sc
    scratch = tmp_path / "ram"; scratch.mkdir()
    monkeypatch.setattr(sc, "pick_scratch_root",
                        lambda need, prefer=None, log=None, fallback=None: scratch)
    tools = {"settings": None, "chdman": object()}
    st = ConvertStats()
    # game=None → strażnik treści pominięty; sprawdzamy samą syntezę cue
    ok = _convert_one([b1, b2], target, "gra", "chd", False, 2, tools, None,
                      False, lambda m: None, st)
    assert ok is True
    assert seen["main"].endswith(".cue")               # createcd z CUE, nie z binu
    assert "MODE1/2352" in seen["cue_text"]            # tor 1 = dane
    assert "AUDIO" in seen["cue_text"]                 # tor 2 = audio
    # kolejność: Track 1 przed Track 2
    assert (seen["cue_text"].index("gra (Track 1).bin")
            < seen["cue_text"].index("gra (Track 2).bin"))
    # tymczasowy cue posprzątany (nie zostaje w docelowym)
    assert not list(target.glob("*.chdbuddy_synth.cue"))


def _stub_deep_ok(monkeypatch, profile):
    """Podstawia deep_identify tak, by strażnik treści potwierdził tożsamość."""
    import chd_buddy.core.deepcheck as _dc
    monkeypatch.setattr(_dc, "deep_identify",
                        lambda *a, **k: _dc.DeepResult(ok=True, sha1=profile))


def test_disc_chd_assembled_from_scattered_tracks_without_cue(
        tmp_path, monkeypatch):
    """SKŁADANIE gry-płyty z ROZPROSZONYCH torów: jeden tor luźno, drugi w ZIP,
    BEZ cue nigdzie. Program zbiera oba po treści, SYNTETYZUJE cue i buduje CHD
    (createcd z cue). Kontener≠gra, gra rozproszona po plikach/archiwach."""
    import zipfile
    import chd_buddy.core.convert as cv
    import chd_buddy.core.scratch as sc
    from chd_buddy.core.convert import convert_from_source
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.matcher import match_store
    from chd_buddy.core.datfile import game_profile
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    tosort = tmp_path / "ts"; tosort.mkdir()
    scratch = tmp_path / "ram"; scratch.mkdir()
    monkeypatch.setattr(sc, "pick_scratch_root",
                        lambda need, prefer=None, log=None, fallback=None: scratch)
    b1 = b"MAIN-DATA" * 500      # największy tor DANYCH (luźno)
    b2 = b"AUDIO-TRK" * 100      # drugi tor (w ZIP)
    cue_dat = b'FILE "gra (Track 1).bin" BINARY\n'    # cue jest w DAT…
    _write_dat(dat_root / "naomi2" / "n.dat", "Arcade - Sega - Naomi 2",
               {"gra": {"gra.cue": cue_dat, "gra (Track 1).bin": b1,
                        "gra (Track 2).bin": b2}})
    # …ale ŹRÓDŁA: Track 1 LUŹNO, Track 2 w ZIP, cue NIGDZIE
    (tosort / "gra (Track 1).bin").write_bytes(b1)
    with zipfile.ZipFile(tosort / "inne.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("gra (Track 2).bin", b2)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort, full=True)
    entries = DatStore(dat_root, rom_root).discover()
    for e in entries:
        e.store_format = "chd"

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "chd"; return r

    seen = {}

    def fake_disc_to_chd(main, dst, chdman, settings, *, log=None,
                         on_progress=None):
        seen["main"] = Path(main).name
        seen["cue_text"] = Path(main).read_text(encoding="utf-8")
        seen["siblings"] = sorted(p.name for p in Path(main).parent.iterdir())
        Path(dst).write_bytes(b"CHD-OK")
        return cv.ConvertResult(True, dst=Path(dst))
    monkeypatch.setattr(cv, "disc_to_chd", fake_disc_to_chd)
    prof = game_profile(entries[0].games[0].data_roms)
    _stub_deep_ok(monkeypatch, prof)                # strażnik potwierdza treść
    reports = match_store(entries, idx)
    tools = {"settings": type("S", (), {"scratch_dir": "", "threads": 0})(),
             "chdman": object()}
    st, done, _ = convert_from_source(reports, rules_fn, tools, index=idx,
                                      dry_run=False)
    chd = entries[0].target_dir / "gra.chd"
    assert chd.is_file() and chd.read_bytes() == b"CHD-OK"
    assert st.converted == 1 and done
    assert seen["main"].endswith(".cue")            # cue ZSYNTETYZOWANY
    # oba tory zebrane obok cue (z ich DAT-owymi nazwami)
    assert "gra (Track 1).bin" in seen["siblings"]
    assert "gra (Track 2).bin" in seen["siblings"]


def test_disc_archive_to_chd_uses_internal_cue(tmp_path, monkeypatch):
    """CHD budowany WPROST z archiwum płyty: wypakowuje cue+biny (nazwy jak w
    środku) i woła createcd na CUE (nie na gołym binie), z binami obok."""
    import zipfile
    import chd_buddy.core.convert as cv
    from chd_buddy.core.convert import disc_archive_to_chd
    arch = tmp_path / "Gra (World).zip"
    with zipfile.ZipFile(arch, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Gra (World).cue", 'FILE "Gra (World) (Track 1).bin" BINARY\n')
        z.writestr("Gra (World) (Track 1).bin", b"BIN1" * 100)
        z.writestr("Gra (World) (Track 2).bin", b"BIN2" * 100)
    seen = {}
    def fake_disc_to_chd(main, dst, chdman, settings, *, log=None, on_progress=None):
        # createcd dostaje CUE, a biny są obok (nazwy z cue)
        seen["main"] = main.name
        seen["siblings"] = sorted(p.name for p in main.parent.iterdir())
        Path(dst).write_bytes(b"CHD")
        return cv.ConvertResult(True, dst=Path(dst))
    monkeypatch.setattr(cv, "disc_to_chd", fake_disc_to_chd)
    scratch = tmp_path / "ram"; scratch.mkdir()
    (tmp_path / "out").mkdir()
    r = disc_archive_to_chd(arch, tmp_path / "out" / "Gra (World).chd",
                            object(), None, scratch=scratch, log=lambda m: None)
    assert r is not None and r.ok
    assert seen["main"] == "Gra (World).cue"                 # createcd z CUE
    assert "Gra (World) (Track 1).bin" in seen["siblings"]   # biny obok cue
    assert "Gra (World) (Track 2).bin" in seen["siblings"]


def test_disc_archive_to_chd_no_cue_returns_none(tmp_path, monkeypatch):
    """Archiwum bez cue/gdi/iso → None (nie próbujemy createcd na gołym binie)."""
    import zipfile
    import chd_buddy.core.convert as cv
    from chd_buddy.core.convert import disc_archive_to_chd
    arch = tmp_path / "golebiny.zip"
    with zipfile.ZipFile(arch, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Track 1.bin", b"X" * 50); z.writestr("Track 2.bin", b"Y" * 50)
    monkeypatch.setattr(cv, "disc_to_chd",
                        lambda *a, **k: pytest_fail())
    scratch = tmp_path / "ram"; scratch.mkdir()
    r = disc_archive_to_chd(arch, tmp_path / "o.chd", object(), None,
                            scratch=scratch, log=lambda m: None)
    assert r is None


def pytest_fail():
    raise AssertionError("disc_to_chd nie powinno być wołane bez cue")


def test_convert_from_source_builds_chd_from_archive_when_dat_cue_mismatch(
        tmp_path, monkeypatch):
    """Gra-płyta→CHD: DAT-owy cue ma INNE nazwy niż cue w archiwum (→ cue wg DAT
    „MISSING"), ale program buduje CHD z cue, który JEST w archiwum. Bez gołego
    binu, bez zawieszenia."""
    import zipfile
    import chd_buddy.core.convert as cv
    import chd_buddy.core.scratch as sc
    from chd_buddy.core.convert import convert_from_source
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.matcher import match_store
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    tosort = tmp_path / "ts"; tosort.mkdir()
    scratch = tmp_path / "ram"; scratch.mkdir()
    monkeypatch.setattr(sc, "pick_scratch_root",
                        lambda need, prefer=None, log=None, fallback=None: scratch)
    b1 = b"TRACK-ONE" * 100; b2 = b"TRACK-TWO" * 100
    cue_dat = 'FILE "gra - (Track 1).bin" BINARY\n'.encode()   # cue z DAT (dash)
    cue_src = 'FILE "gra (Track 1).bin" BINARY\n'.encode()     # cue w zipie (inny)
    _write_dat(dat_root / "naomi2" / "n.dat", "Arcade - Sega - Naomi 2",
               {"gra": {"gra.cue": cue_dat, "gra (Track 1).bin": b1,
                        "gra (Track 2).bin": b2}})
    # ToSort: archiwum z INNYM cue (nazwy pasujące do binów) + biny
    z = tosort / "gra.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("gra.cue", cue_src)          # inny content → wg DAT „missing"
        zf.writestr("gra (Track 1).bin", b1)
        zf.writestr("gra (Track 2).bin", b2)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort, full=True)
    entries = DatStore(dat_root, rom_root).discover()
    for e in entries:
        e.store_format = "chd"

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "chd"; return r

    def fake_disc_to_chd(main, dst, chdman, settings, *, log=None, on_progress=None):
        assert main.suffix.lower() == ".cue"     # createcd z CUE, nie z binu
        Path(dst).write_bytes(b"CHD-CONTENT")
        return cv.ConvertResult(True, dst=Path(dst))
    monkeypatch.setattr(cv, "disc_to_chd", fake_disc_to_chd)
    # STRAŻNIK TREŚCI: atrapa deep_identify potwierdza tożsamość zbudowanego CHD
    # z grą (w realu ekstrahuje i liczy game_profile).
    import chd_buddy.core.deepcheck as _dc
    from chd_buddy.core.datfile import game_profile as _gp
    _prof = _gp(entries[0].games[0].data_roms)
    monkeypatch.setattr(_dc, "deep_identify",
                        lambda *a, **k: _dc.DeepResult(ok=True, sha1=_prof))
    reports = match_store(entries, idx)
    tools = {"settings": type("S", (), {"scratch_dir": "", "threads": 0})(),
             "chdman": object()}
    st, done, _ = convert_from_source(reports, rules_fn, tools, index=idx,
                                      dry_run=False)
    chd = entries[0].target_dir / "gra.chd"
    assert chd.is_file() and chd.read_bytes() == b"CHD-CONTENT"   # CHD z archiwum
    assert st.converted == 1 and done


def test_disc_archive_chd_rejects_wrong_content(tmp_path, monkeypatch):
    """STRAŻNIK: gdy zbudowany CHD ma treść INNEJ gry (cudze archiwum trafione
    przez współdzielony tor), NIE umieszczamy pliku i NIE kasujemy źródła.
    Regresja: Virtua Striker 3 zapisywany jako „Dynamic Golf" itd."""
    import zipfile
    import chd_buddy.core.convert as cv
    import chd_buddy.core.scratch as sc
    import chd_buddy.core.deepcheck as _dc
    from chd_buddy.core.convert import convert_from_source
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.matcher import match_store
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    tosort = tmp_path / "ts"; tosort.mkdir()
    scratch = tmp_path / "ram"; scratch.mkdir()
    monkeypatch.setattr(sc, "pick_scratch_root",
                        lambda need, prefer=None, log=None, fallback=None: scratch)
    b1 = b"MAIN-DATA-TRACK" * 100      # największy tor DANYCH — UNIKALNY dla gry
    shared = b"SHARED"                 # tor współdzielony (mały)
    cue_dat = 'FILE "gra - (Track 1).bin" BINARY\n'.encode()
    cue_src = 'FILE "gra (Track 1).bin" BINARY\n'.encode()
    _write_dat(dat_root / "naomi2" / "n.dat", "Arcade - Sega - Naomi 2",
               {"gra": {"gra.cue": cue_dat, "gra (Track 1).bin": b1,
                        "gra (Track 2).bin": shared}})
    # Archiwum ma NAJWIĘKSZY tor danych gry, ale zbudowany CHD to inna treść.
    z = tosort / "gra.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("gra.cue", cue_src)
        zf.writestr("gra (Track 1).bin", b1)
        zf.writestr("gra (Track 2).bin", shared)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort, full=True)
    entries = DatStore(dat_root, rom_root).discover()
    for e in entries:
        e.store_format = "chd"

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "chd"; return r

    def fake_disc_to_chd(main, dst, chdman, settings, *, log=None,
                         on_progress=None):
        Path(dst).write_bytes(b"WRONG-GAME-CONTENT")
        return cv.ConvertResult(True, dst=Path(dst))
    monkeypatch.setattr(cv, "disc_to_chd", fake_disc_to_chd)
    # deep_identify NIE potwierdza tożsamości (treść inna) → ok=False
    monkeypatch.setattr(_dc, "deep_identify",
                        lambda *a, **k: _dc.DeepResult(ok=False))
    reports = match_store(entries, idx)
    tools = {"settings": type("S", (), {"scratch_dir": "", "threads": 0})(),
             "chdman": object()}
    st, done, _ = convert_from_source(reports, rules_fn, tools, index=idx,
                                      dry_run=False)
    chd = entries[0].target_dir / "gra.chd"
    assert not chd.exists()             # NIE umieszczono złej treści
    assert st.converted == 0 and not done
    assert z.is_file()                  # źródło NIE skasowane

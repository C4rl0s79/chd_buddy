"""Testy konwersji formatów (ZIP end-to-end; CHD/RVZ wymagają narzędzi)."""
from __future__ import annotations

import hashlib
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
    # źródło NIE skasowane od razu — zwrócone do zbiorczego kasowania na końcu
    assert src.exists() and src in to_purge
    from chd_buddy.core.convert import purge_source_files
    purge_source_files(to_purge, index=idx)
    assert not src.exists()                          # skasowane na końcu
    assert not any(scratch.iterdir())                # nic nie zostało na scratchu


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

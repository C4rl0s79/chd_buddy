"""Testy dopasowania ŚWIADOMEGO formatu przechowywania.

Sedno poprawki: format docelowy (zip dla kartridży, chd dla płyt) nadpisuje
rozszerzenia z DAT-a. Gra kartridżowa trzymana jako ``<gra>.zip`` w katalogu
docelowym jest POPRAWNA (HAVE/zielona), a NIE „do wypakowania"."""
from __future__ import annotations

import hashlib
import zipfile
import zlib
from pathlib import Path

from chd_buddy.core.datstore import DatStore
from chd_buddy.core.dirrules import DirRules, apply_rule_targets
from chd_buddy.core.fileindex import FileIndex
from chd_buddy.core.matcher import RomState, match_entry
from tests.test_rebuilder import _write_dat


def _zip_with(path: Path, member: str, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(member, content)


def _setup(tmp_path: Path, fmt: str):
    """Kartridż SNES: DAT wymienia .sfc, plik trzymany jako <gra>.zip."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    content = b"KARTRIDZ-DANE" * 20
    attrs = {"Super Mario (USA)": {"Super Mario (USA).sfc": content}}
    _write_dat(dat_root / "snes.dat",
               "Nintendo - Super Nintendo Entertainment System", attrs)
    (dat_root / "_reguly.json").write_text(
        '{"Nintendo - Super Nintendo Entertainment System": {"format": "%s"}}'
        % fmt, encoding="utf-8")
    entries = DatStore(dat_root, rom_root).discover()
    apply_rule_targets(entries, DirRules(dat_root), rom_root)
    return dat_root, rom_root, entries[0], content


def test_zip_in_target_is_have(tmp_path: Path):
    """Kartridż jako <gra>.zip W KATALOGU DOCELOWYM = HAVE (nie wypakowuj)."""
    _dr, rom_root, entry, content = _setup(tmp_path, "zip")
    assert entry.store_format == "zip"
    # zapisz grę jako archiwum pod nazwą gry w katalogu docelowym
    _zip_with(entry.target_dir / "Super Mario (USA).zip",
              "Super Mario (USA).sfc", content)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)
    rep = match_entry(entry, idx)
    tot, comp, fix, miss = rep.game_stats()
    assert (tot, comp, fix, miss) == (1, 1, 0, 0)      # zielone, zero naprawy
    s = rep.statuses[0]
    assert s.state == RomState.HAVE and s.via_archive
    idx.close()


def test_zip_elsewhere_moves_whole_archive(tmp_path: Path):
    """Kartridż jako <gra>.zip w ToSort (format zip) = ELSEWHERE, ale
    PRZENIEŚ całe archiwum — nie wypakowuj do luźnego pliku."""
    _dr, rom_root, entry, content = _setup(tmp_path, "zip")
    tosort = rom_root.parent / "tosort"
    _zip_with(tosort / "Super Mario (USA).zip", "Super Mario (USA).sfc", content)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(tosort)
    rep = match_entry(entry, idx)
    s = rep.statuses[0]
    assert s.state == RomState.ELSEWHERE
    assert s.via_archive and s.archive_names_ok        # nazwa wewn. poprawna
    assert Path(s.source_path).name == "Super Mario (USA).zip"
    assert s.canonical_path == entry.target_dir / "Super Mario (USA).zip"
    idx.close()


def test_zip_wrong_internal_name_is_fixable(tmp_path: Path):
    """Zawartość poprawna (suma), ale ZŁA NAZWA wewnątrz archiwum => nie HAVE,
    lecz WRONG_NAME (do przepakowania) — nie bazujemy na nazwie pliku zip."""
    _dr, rom_root, entry, content = _setup(tmp_path, "zip")
    # archiwum w katalogu docelowym, ale plik w środku ma ZŁĄ nazwę
    _zip_with(entry.target_dir / "mario.zip", "zla_nazwa.sfc", content)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)
    rep = match_entry(entry, idx)
    s = rep.statuses[0]
    assert s.state == RomState.WRONG_NAME
    assert s.via_archive and not s.archive_names_ok
    assert s.member == "zla_nazwa.sfc"                 # znamy złą nazwę wewn.
    idx.close()


def test_keep_format_still_extracts_from_archive(tmp_path: Path):
    """Format „keep": archiwum GDZIE INDZIEJ nadal wypakowywane (stara ścieżka
    — via_archive tylko dla jawnego zip/7z albo archiwum na miejscu)."""
    _dr, rom_root, entry, content = _setup(tmp_path, "keep")
    tosort = rom_root.parent / "tosort"
    _zip_with(tosort / "paczka.zip", "Super Mario (USA).sfc", content)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(tosort)
    rep = match_entry(entry, idx)
    s = rep.statuses[0]
    assert s.state == RomState.ELSEWHERE
    assert not s.via_archive and s.member == "Super Mario (USA).sfc"
    idx.close()


def test_rebuild_fixes_wrong_internal_name(tmp_path: Path):
    """Rebuilder PRZEPAKOWUJE zip z poprawną nazwą wewnętrzną (weryfikacja
    SHA-1 danych po sumie, nie po nazwie); zła zawartość by nie przeszła."""
    from chd_buddy.core.matcher import match_entry
    from chd_buddy.core.rebuilder import Rebuilder
    _dr, rom_root, entry, content = _setup(tmp_path, "zip")
    _zip_with(entry.target_dir / "mario.zip", "zla_nazwa.sfc", content)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root, full=True)          # full => member SHA-1 policzony
    rep = match_entry(entry, idx)
    assert rep.statuses[0].state == RomState.WRONG_NAME

    rb = Rebuilder(idx, dry_run=False)
    rb.run([rep], only_complete=True)
    # powstał zip z POPRAWNĄ nazwą wewnętrzną
    fixed = entry.target_dir / "Super Mario (USA).zip"
    assert fixed.is_file()
    with zipfile.ZipFile(fixed) as zf:
        names = zf.namelist()
    assert names == ["Super Mario (USA).sfc"]
    # ponowny raport => zielone
    rep2 = match_entry(entry, idx)
    assert rep2.statuses[0].state == RomState.HAVE
    idx.close()


def test_format_rule_on_any_folder_applies_platform_wide(tmp_path: Path):
    """Reguła formatu ustawiona na DOWOLNYM katalogu platformy (nawet katalogu
    DZIECI) obowiązuje CAŁĄ platformę — rodzic bez własnej reguły ją przejmuje."""
    from chd_buddy.core.datstore import DatStore, platform_key
    from chd_buddy.core.dirrules import DirRules, apply_rule_targets, save_rule
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    # rodzic (większy) w ROMS bez reguły; dziecko (mniejsze) w 1G1R
    _write_dat(dat_root / "ROMS" / "full.dat", "Sony - PlayStation 2 - Datfile",
               {f"G{i}": {f"g{i}.iso": bytes([i]) * 40} for i in range(3)})
    _write_dat(dat_root / "1G1R" / "1g1r.dat",
               "Sony - PlayStation 2 (Redump - 1G1R)", {"G0": {"g0.iso": b"\x00" * 40}})
    save_rule(dat_root, "1G1R", {"format": "auto"})   # format na folderze DZIECI
    entries = DatStore(dat_root, rom_root).discover()
    apply_rule_targets(entries, DirRules(dat_root), rom_root)
    fmts = {("RODZIC" if "Datfile" in e.name else "dziecko"): e.store_format
            for e in entries if platform_key(e.name) == "sony playstation 2"}
    assert fmts == {"RODZIC": "chd", "dziecko": "chd"}   # oba, spójnie


def test_missing_rom_root_is_detected(tmp_path: Path):
    """Bezpiecznik: rom_root wskazujący NIEISTNIEJĄCY katalog (literówka
    „rom1" zamiast „roms") musi być wykryty PRZED naprawą — inaczej każdy plik
    wygląda na źle położony i naprawa przenosi całą kolekcję."""
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.dirrules import DirRules, missing_roots, save_rule
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    rom_root.mkdir(parents=True)
    _write_dat(dat_root / "ROMS" / "ps2.dat", "Sony - PlayStation 2",
               {"G": {"g.iso": b"x" * 40}})
    entries = DatStore(dat_root, rom_root).discover()
    assert missing_roots(entries, DirRules(dat_root), rom_root) == []   # OK
    # literówka: katalog nie istnieje
    save_rule(dat_root, "ROMS", {"rom_root": str(tmp_path / "rom1")})
    bad = missing_roots(entries, DirRules(dat_root), rom_root)
    assert len(bad) == 1 and bad[0][1].endswith("rom1")


def test_scan_roots_includes_rom_root_overrides(tmp_path: Path):
    """Skan MUSI objąć katalogi wyprowadzone regułą rom_root poza główny
    rom_root — inaczej indeks nie widzi tam plików i trzyma duchy po
    skasowanych (matcher planuje wtedy przenosiny z nieistniejących ścieżek)."""
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.dirrules import DirRules, apply_rule_targets, save_rule, scan_roots
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    alt = tmp_path / "rom1"
    tosort = tmp_path / "tosort"
    for d in (rom_root, alt, tosort):
        d.mkdir(parents=True)
    _write_dat(dat_root / "ROMS" / "ps2.dat", "Sony - PlayStation 2",
               {"G": {"g.iso": b"x" * 40}})
    save_rule(dat_root, "ROMS", {"rom_root": str(alt)})
    entries = DatStore(dat_root, rom_root).discover()
    rules = DirRules(dat_root)
    apply_rule_targets(entries, rules, rom_root)
    roots = [Path(r) for r in scan_roots(entries, rules, rom_root, tosort)]
    assert rom_root in roots and alt in roots and tosort in roots


def test_parent_claims_physical_before_child(tmp_path: Path):
    """RODZIC (folder z parent_priority) jest przetwarzany PRZED dzieckiem
    także wtedy, gdy DAT-y mają RÓŻNE platform_key — inaczej dziecko zabiera
    plik fizyczny, a w katalogu rodzica ląduje symlink."""
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.dirrules import save_rule
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    # dziecko ma nazwę alfabetycznie WCZEŚNIEJSZĄ niż rodzic
    _write_dat(dat_root / "1G1R" / "fb.dat", "FinalBurn Neo - SNES Games",
               {"G": {"g.sfc": b"x" * 40}})
    _write_dat(dat_root / "ROMS" / "snes.dat",
               "Nintendo - Super Nintendo Entertainment System",
               {"G": {"g.sfc": b"x" * 40}})
    save_rule(dat_root, "ROMS", {"parent_priority": True})
    entries = DatStore(dat_root, rom_root).discover()
    assert "ROMS" in str(entries[0].dat_path)      # rodzic PIERWSZY
    assert "1G1R" in str(entries[1].dat_path)


def test_child_only_game_stays_physical_in_child(tmp_path: Path):
    """Decyzja użytkownika: gra występująca WYŁĄCZNIE w DAT-cie dziecka (brak
    jej u rodzica) istnieje FIZYCZNIE w katalogu dziecka — nie jako link,
    bo nie ma do czego linkować."""
    import zlib, hashlib
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.dirrules import DirRules, apply_rule_targets, save_rule
    from chd_buddy.core.fileindex import FileIndex
    from chd_buddy.core.matcher import match_store
    from chd_buddy.core.rebuilder import Rebuilder

    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    src = tmp_path / "tosort"
    src.mkdir(parents=True)
    wspolna = b"WSPOLNA" * 40
    tylko_dziecko = b"TYLKO-DZIECKO" * 40
    # rodzic zna tylko grę wspólną; dziecko zna obie
    _write_dat(dat_root / "ROMS" / "p.dat", "Sony - PlayStation 2",
               {"Wspolna": {"Wspolna.iso": wspolna}})
    _write_dat(dat_root / "1G1R" / "c.dat", "Sony - PlayStation 2 (Retool)",
               {"Wspolna": {"Wspolna.iso": wspolna},
                "TylkoDziecko": {"TylkoDziecko.iso": tylko_dziecko}})
    save_rule(dat_root, "ROMS", {"parent_priority": True})
    (src / "Wspolna.iso").write_bytes(wspolna)
    (src / "TylkoDziecko.iso").write_bytes(tylko_dziecko)

    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(src)
    entries = DatStore(dat_root, rom_root).discover()
    rules = DirRules(dat_root)
    apply_rule_targets(entries, rules, rom_root)
    reports = match_store(entries, idx)
    Rebuilder(idx, dry_run=False, make_links=False).run(reports)

    parent = [e for e in entries if e.name == "Sony - PlayStation 2"][0]
    child = [e for e in entries if "Retool" in e.name][0]
    # gra wspólna: FIZYCZNIE u rodzica
    pf = parent.target_dir / "Wspolna.iso"
    assert pf.is_file() and not pf.is_symlink()
    # gra tylko-dziecka: FIZYCZNIE u dziecka (nie ma czego linkować)
    cf = child.target_dir / "TylkoDziecko.iso"
    assert cf.is_file() and not cf.is_symlink()
    assert cf.read_bytes() == tylko_dziecko
    idx.close()


def test_child_inherits_parent_format(tmp_path: Path):
    """Dziecko platformy dziedziczy format RODZICA (bez własnego wyboru)."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data = b"P" * 40
    # rodzic (większy) = pełny Redump PS2, format chd; dziecko 1G1R format auto
    _write_dat(dat_root / "full.dat", "Sony - PlayStation 2 - Datfile",
               {f"G{i}": {f"g{i}.iso": bytes([i]) * 40} for i in range(3)})
    _write_dat(dat_root / "1g1r.dat", "Sony - PlayStation 2 (Redump - 1G1R)",
               {"G0": {"g0.iso": data}})
    (dat_root / "_reguly.json").write_text(
        '{"Sony - PlayStation 2 - Datfile": {"format": "chd"},'
        ' "Sony - PlayStation 2 (Redump - 1G1R)": {"format": "zip"}}',
        encoding="utf-8")
    entries = DatStore(dat_root, rom_root).discover()
    apply_rule_targets(entries, DirRules(dat_root), rom_root)
    parent = [e for e in entries if "Datfile" in e.name][0]
    child = [e for e in entries if "1G1R" in e.name][0]
    assert parent.store_format == "chd"
    assert child.store_format == "chd"        # dziecko dziedziczy, mimo „zip"


def test_scan_roots_accepts_multiple_tosorts(tmp_path: Path):
    """Wiele katalogów ToSort (jak w RomVaulcie): wszystkie wchodzą do skanu."""
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.dirrules import DirRules, apply_rule_targets, scan_roots
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    ts1 = tmp_path / "tosort1"
    ts2 = tmp_path / "tosort2"
    for d in (rom_root, ts1, ts2):
        d.mkdir(parents=True)
    _write_dat(dat_root / "a.dat", "System A", {"G": {"g.iso": b"x" * 40}})
    entries = DatStore(dat_root, rom_root).discover()
    rules = DirRules(dat_root)
    apply_rule_targets(entries, rules, rom_root)
    roots = [Path(r) for r in scan_roots(entries, rules, rom_root,
                                         [str(ts1), str(ts2), ""])]
    assert ts1 in roots and ts2 in roots and rom_root in roots


def test_settings_tosort_dirs_property(tmp_path: Path):
    """tosort_dirs = główny + dodatkowe, bez duplikatów i pustych."""
    from chd_buddy.core.settings import Settings
    s = Settings()
    s.tosort_dir = str(tmp_path / "main")
    s.tosort_extra = [str(tmp_path / "extra"), s.tosort_dir, ""]
    assert s.tosort_dirs == [str(tmp_path / "main"), str(tmp_path / "extra")]


def test_platform_alias_pins_child_to_parent(tmp_path: Path):
    """H (FinalBurn Neo): reguła `platform` przypina DAT o INNEJ nazwie jako
    dziecko platformy rodzica — hierarchia (rodzic pierwszy) i dziedziczenie
    formatu działają, mimo że platform_key nazw się różni."""
    from chd_buddy.core.datstore import (DatStore, effective_platform_key,
                                         group_by_platform)
    from chd_buddy.core.dirrules import DirRules, apply_rule_targets, save_rule
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    _write_dat(dat_root / "ROMS" / "snes.dat",
               "Nintendo - Super Nintendo Entertainment System",
               {"G": {"g.sfc": b"x" * 40}})
    _write_dat(dat_root / "1G1R" / "fb.dat", "FinalBurn Neo - SNES Games",
               {"G": {"g.sfc": b"x" * 40}})
    save_rule(dat_root, "ROMS", {"parent_priority": True, "format": "zip"})
    # PRZYPIĘCIE: FinalBurn staje się dzieckiem platformy SNES
    save_rule(dat_root, "FinalBurn Neo - SNES Games",
              {"platform": "nintendo super nintendo entertainment system"},
              strip_defaults=False)

    entries = DatStore(dat_root, rom_root).discover()
    rules = DirRules(dat_root)
    fb = [e for e in entries if "FinalBurn" in e.name][0]
    snes = [e for e in entries if "Nintendo" in e.name][0]
    # ten sam efektywny klucz => jedna grupa, SNES (parent_priority) pierwszy
    assert (effective_platform_key(fb, rules)
            == effective_platform_key(snes, rules))
    groups = group_by_platform(entries, rules)
    grp = groups["nintendo super nintendo entertainment system"]
    assert [e.name for e in grp][0].startswith("Nintendo")   # RODZIC
    assert len(grp) == 2
    # dziedziczenie formatu przez przypiętą platformę
    apply_rule_targets(entries, rules, rom_root)
    assert snes.store_format == "zip" and fb.store_format == "zip"
    # odpięcie (alias = "" usuwa regułę) => znowu osobna platforma
    save_rule(dat_root, "FinalBurn Neo - SNES Games", {"platform": ""})
    rules2 = DirRules(dat_root)
    assert effective_platform_key(fb, rules2) == "finalburn neo snes games"


def test_deep_fail_is_remembered(tmp_path: Path, monkeypatch):
    """Nieudana głęboka identyfikacja CHD jest ZAPAMIĘTYWANA — kolejny skan
    nie mieli tego samego (niezmienionego) pliku ponownie; zmiana pliku
    albo pełny re-hash kasują znacznik i próba wraca."""
    import chd_buddy.core.matcher as m
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.fileindex import FileIndex
    dat_root = tmp_path / "dats"
    root = tmp_path / "roms"
    root.mkdir(parents=True)
    _write_dat(dat_root / "a.dat", "Sony - PlayStation",
               {"G": {"g.bin": b"x" * 40}})
    chd = root / "Nieznany.chd"
    chd.write_bytes(b"MOCK-CHD" * 64)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(root)
    entries = DatStore(dat_root, root).discover()

    calls = {"deep": 0}

    class _R:
        ok = False; sha1 = ""; method = ""; game = ""; tried = ["a", "b"]

    def fake_deep(chdman, path, merged, wd, log=None, cancel_event=None,
                  chd_info=None, on_progress=None):
        calls["deep"] += 1
        return _R()

    class _Info:
        data_sha1 = ""; sha1 = ""

    class _Chd:
        def info(self, p): return _Info()

    monkeypatch.setattr("chd_buddy.core.deepcheck.deep_identify", fake_deep)
    m.deep_probe_chds(idx, entries, _Chd(), roots=[root])
    assert calls["deep"] == 1                  # pierwsza próba wykonana
    m.deep_probe_chds(idx, entries, _Chd(), roots=[root])
    assert calls["deep"] == 1                  # porażka ZAPAMIĘTANA — bez mielenia
    # zmiana pliku => znacznik nieaktualny => próba wraca
    import time
    time.sleep(0.01)
    chd.write_bytes(b"INNY-CHD" * 64)
    idx.scan(root)                             # re-hash zmienionego (deep_fail=0)
    m.deep_probe_chds(idx, entries, _Chd(), roots=[root])
    assert calls["deep"] == 2
    idx.close()


def test_deep_probe_forwards_progress_to_detail(tmp_path: Path, monkeypatch):
    """Postęp ekstrakcji chdman (pct/nieokreślony) dociera do paska
    szczegółowego — inaczej długie extractcd wygląda na zawieszone."""
    import chd_buddy.core.matcher as m
    dat_root = tmp_path / "dats"
    root = tmp_path / "roms" / "psx"
    root.mkdir(parents=True)
    _write_dat(dat_root / "a.dat", "Sony - PlayStation",
               {"G": {"g.bin": b"x" * 40}})
    (root / "Nieznany.chd").write_bytes(b"MOCK-CHD" * 64)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(root)
    entries = DatStore(dat_root, root).discover()

    class _R:
        ok = False; sha1 = ""; method = ""; game = ""; tried = ["a"]

    def fake_deep(chdman, path, merged, wd, log=None, cancel_event=None,
                  chd_info=None, on_progress=None):
        # symuluj to, co robi chdman._stream: linie procentowe + token
        assert on_progress is not None
        on_progress(45.0, "45.0% complete")
        on_progress(-1.0, "Extracting, ...")
        return _R()

    class _Info:
        data_sha1 = ""; sha1 = ""

    class _Chd:
        def info(self, p): return _Info()

    monkeypatch.setattr("chd_buddy.core.deepcheck.deep_identify", fake_deep)

    overall: list = []
    detail: list = []
    m.deep_probe_chds(idx, entries, _Chd(), roots=[root],
                      on_progress=lambda d, t, s: overall.append((d, t, s)),
                      detail=lambda d, t, s: detail.append((d, t, s)))
    # pasek OGÓLNY dostał etykietę z nazwą pliku
    assert any("Nieznany.chd" in s for _d, _t, s in overall)
    # ... i jest DETERMINOWANY (X/Y CHD), nie zawieszony na 0/0
    assert any(t > 0 for _d, t, _s in overall), "pasek ogólny stoi na 0/0"
    assert all(0 <= d <= t for d, t, _s in overall if t > 0)
    # pasek SZCZEGÓŁOWY dostał procent (45/100) i tryb nieokreślony (0/0)
    assert (45, 100) in [(d, t) for d, t, _s in detail]
    assert (0, 0) in [(d, t) for d, t, _s in detail]
    idx.close()


def test_deep_identify_skips_dvd_for_cd_container(tmp_path: Path, monkeypatch):
    """CHD typu CD (createcd — np. PS1): extractdvd jest POMIJANY; kolejność
    zaczyna się od surowych ścieżek CD. Kontener DVD pomija ścieżki CD."""
    from chd_buddy.core import deepcheck
    from chd_buddy.core.models import CHDInfo

    attempted = []

    class _Chd:
        def extract(self, cmd, src, dst, on_progress=None, cancel_event=None):
            attempted.append(cmd)
            class R: ok = False; returncode = 1
            return R()
        def info(self, p):
            raise OSError("nie wolac")

    chd_file = tmp_path / "gra.chd"
    chd_file.write_bytes(b"x")

    # kontener CD
    info_cd = CHDInfo(path=chd_file, metadata_tags=["CHT2"])
    assert info_cd.is_cd_typed
    deepcheck.deep_identify(_Chd(), chd_file, None, tmp_path, chd_info=info_cd)
    assert "extractdvd" not in attempted
    assert attempted[0] == "extractcd"          # surowe ścieżki najpierw

    # kontener DVD (unit 2048)
    attempted.clear()
    info_dvd = CHDInfo(path=chd_file, unit_bytes=2048)
    assert not info_dvd.is_cd_typed
    deepcheck.deep_identify(_Chd(), chd_file, None, tmp_path, chd_info=info_dvd)
    assert attempted[0] == "extractdvd"
    assert "extractcd" not in attempted


def test_split_cue_tracks_multitrack(tmp_path: Path):
    """Sklejony .bin z chdman extractcd jest cięty per ścieżka wg cue —
    Redump hashuje każdą ścieżkę OSOBNO (SotN: dane + audio CDDA)."""
    from chd_buddy.core.deepcheck import _split_cue_tracks
    sector = 2352
    t1 = b"D" * (sector * 10)          # dane: 10 sektorów
    t2 = b"A" * (sector * 4)           # audio: 4 sektory
    (tmp_path / "gra.bin").write_bytes(t1 + t2)
    (tmp_path / "gra.cue").write_text(
        'FILE "gra.bin" BINARY\n'
        '  TRACK 01 MODE2/2352\n'
        '    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n'
        '    INDEX 00 00:00:10\n'
        '    INDEX 01 00:00:12\n', encoding="utf-8")
    out = _split_cue_tracks(tmp_path / "gra.cue", lambda m: None)
    assert len(out) == 2
    assert out[0].read_bytes() == t1   # granica = pierwszy INDEX (00) ścieżki 2
    assert out[1].read_bytes() == t2


def test_split_cue_single_track_returns_whole_bin(tmp_path: Path):
    from chd_buddy.core.deepcheck import _split_cue_tracks
    data = b"X" * (2352 * 5)
    (tmp_path / "solo.bin").write_bytes(data)
    (tmp_path / "solo.cue").write_text(
        'FILE "solo.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
        encoding="utf-8")
    out = _split_cue_tracks(tmp_path / "solo.cue", lambda m: None)
    assert len(out) == 1 and out[0].read_bytes() == data


def test_match_by_dat_sizes_glued_image(tmp_path: Path):
    """CHD zrobione bez cue = sklejony obraz. Tniemy wg ROZMIARÓW ścieżek
    z DAT-a i weryfikujemy sha1 każdej — identyfikacja bez pliku cue."""
    import hashlib
    from chd_buddy.core.datfile import DatGame, DatIndex, DatRom
    from chd_buddy.core.deepcheck import _match_by_dat_sizes
    t1 = b"DANE" * 2352
    t2 = b"AUDIO!" * 1176
    idx = DatIndex()
    idx.add_game(DatGame("Gra (USA)", [
        DatRom("Gra (USA).cue", 100, sha1="c" * 40),
        DatRom("Gra (USA) (Track 1).bin", len(t1),
               sha1=hashlib.sha1(t1).hexdigest()),
        DatRom("Gra (USA) (Track 2).bin", len(t2),
               sha1=hashlib.sha1(t2).hexdigest()),
    ]))
    glued = tmp_path / "glued.bin"
    glued.write_bytes(t1 + t2)
    r = _match_by_dat_sizes(glued, idx, lambda m: None)
    assert r is not None and r.ok
    assert r.game == "Gra (USA)"
    # odcisk KOMPLETU ścieżek (pojedyncza suma nie odróżnia wydań 1S/5S)
    from chd_buddy.core.datfile import game_profile
    from chd_buddy.core.datfile import DatRom as _DR
    want = hashlib.sha1(",".join(
        [hashlib.sha1(t1).hexdigest(), hashlib.sha1(t2).hexdigest()]
    ).encode("ascii")).hexdigest()
    assert r.sha1 == want
    # zła zawartość => brak dopasowania (wszystkie ścieżki muszą przejść)
    bad = tmp_path / "bad.bin"
    bad.write_bytes(t1 + b"X" * len(t2))
    assert _match_by_dat_sizes(bad, idx, lambda m: None) is None


def test_prune_ghosts_marks_vanished_roots(tmp_path: Path):
    """Wpisy pod korzeniem, który ZNIKNĄŁ, są oznaczane missing — inaczej
    matcher planuje przenosiny z nieistniejących ścieżek."""
    import shutil as _sh
    from chd_buddy.core.fileindex import FileIndex
    root = tmp_path / "stare_roms"
    root.mkdir()
    (root / "gra.iso").write_bytes(b"x" * 100)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(root)
    _sh.rmtree(root)                       # user kasuje caly katalog
    n = idx.prune_ghosts()
    assert n == 1
    rows = idx.find_crc(__import__("zlib").crc32(b"x" * 100).__format__("08x"), 100)
    assert rows == []                      # duch nie jest juz zrodlem
    idx.close()


def test_adopt_moved_file_no_rehash(tmp_path: Path):
    """Plik przeniesiony ręcznie (ta sama nazwa+rozmiar+mtime) przejmuje
    wpis z bazy BEZ ponownego haszowania — przenosiny setek GB nie kosztują
    godzin."""
    import os as _os
    from chd_buddy.core.fileindex import FileIndex
    a = tmp_path / "a"; b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    f = a / "Gra (USA).chd"
    f.write_bytes(b"CHDDANE" * 1000)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    st1 = idx.scan(a)
    assert st1.hashed == 1
    old = idx.lookup(f)
    idx.set_data_sha1(f, "d" * 40)         # np. wynik glebokiej identyfikacji
    _os.replace(f, b / "Gra (USA).chd")    # reczne przeniesienie
    st2 = idx.scan(b)
    assert st2.adopted == 1 and st2.hashed == 0   # ZERO czytania danych
    new = idx.lookup(b / "Gra (USA).chd")
    assert new["sha1"] == old["sha1"]
    assert new["data_sha1"] == "d" * 40            # wiedza podaza za plikiem
    idx.close()


def test_prune_ghosts_keeps_members_for_adoption(tmp_path: Path):
    """Członkowie archiwum missing=1 ZOSTAJĄ (pamięć adopcji przy przenosinach);
    kasowani dopiero, gdy wiersz archiwum zniknął z bazy całkiem."""
    import os as _os
    from chd_buddy.core.fileindex import FileIndex
    a = tmp_path / "a"; b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    _zip_with(a / "Gra.zip", "Gra.sfc", b"X" * 500)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(a)
    assert idx.find_member_crc(
        f"{zlib.crc32(b'X'*500) & 0xFFFFFFFF:08x}", 500)
    _os.replace(a / "Gra.zip", b / "Gra.zip")     # przenosiny reczne
    idx.prune_ghosts()                            # stary wpis => missing=1
    n_members = idx._db.execute(
        "SELECT COUNT(*) FROM members").fetchone()[0]
    assert n_members == 1                         # pamiec adopcji zostala
    st = idx.scan(b)
    assert st.adopted == 1                        # przejety wraz z czlonkami
    assert idx.find_member_crc(
        f"{zlib.crc32(b'X'*500) & 0xFFFFFFFF:08x}", 500)
    idx.close()


def test_repair_prunes_empty_tosort_dirs(tmp_path: Path):
    """Puste podkatalogi w ToSort po zabraniu plików są sprzątane
    (sam korzeń ToSort zostaje)."""
    from chd_buddy.core.matcher import match_store
    from chd_buddy.core.rebuilder import Rebuilder
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    tosort = tmp_path / "tosort"
    data = b"GRA" * 40
    _write_dat(dat_root / "a.dat", "System A", {"Gra": {"Gra.iso": data}})
    sub = tosort / "stare" / "gleboko"
    sub.mkdir(parents=True)
    (sub / "Gra.iso").write_bytes(data)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    rb = Rebuilder(idx, tosort=tosort, dry_run=False)
    rb.run(match_store(entries, idx), delete_placed_from=[tosort])
    assert (rom_root / "System A" / "Gra.iso").is_file()
    assert not sub.exists()                       # puste podkatalogi zniknely
    assert tosort.is_dir()                        # korzen zostal
    idx.close()


def test_chd_profile_distinguishes_revisions_1s_5s(tmp_path: Path):
    """SEDNO sprawy Panzer Dragoon: (1S) i (5S) DZIELĄ ścieżkę danych, różnią
    się tylko audio. CHD z odciskiem 1S NIE MOŻE zaspokoić gry 5S — żadnych
    linków między różnymi zrzutami."""
    import hashlib as _h
    from chd_buddy.core.datfile import game_profile
    from chd_buddy.core.matcher import RomState, match_store
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data_trk = b"WSPOLNE-DANE" * 30           # wspolna sciezka danych!
    audio_1s = b"AUDIO-1S" * 30
    audio_5s = b"AUDIO-5S" * 30
    _write_dat(dat_root / "sat.dat", "Saturn", {
        "Panzer (USA) (1S)": {"Panzer (USA) (1S).cue": b"cue1",
                              "Panzer (USA) (1S) (Track 01).bin": data_trk,
                              "Panzer (USA) (1S) (Track 02).bin": audio_1s},
        "Panzer (USA) (5S)": {"Panzer (USA) (5S).cue": b"cue5",
                              "Panzer (USA) (5S) (Track 01).bin": data_trk,
                              "Panzer (USA) (5S) (Track 02).bin": audio_5s}})
    src = tmp_path / "zrzuty"; src.mkdir()
    (src / "panzer.chd").write_bytes(b"chd-1s")
    idx = FileIndex(tmp_path / "idx.sqlite3")

    class _R:                                  # ROM-podobne do game_profile
        def __init__(s, h): s.sha1 = h
    prof_1s = game_profile([_R(_h.sha1(data_trk).hexdigest()),
                            _R(_h.sha1(audio_1s).hexdigest())])
    idx.scan(src, chd_prober=lambda p: prof_1s)

    entries = DatStore(dat_root, rom_root).discover()
    rep = match_store(entries, idx)[0]
    by_game = {}
    for s in rep.statuses:
        by_game.setdefault(s.game, []).append(s)
    st_1s = {s.state for s in by_game["Panzer (USA) (1S)"]}
    st_5s = {s.state for s in by_game["Panzer (USA) (5S)"]}
    assert st_1s == {RomState.ELSEWHERE}       # CHD pasuje TYLKO do 1S
    assert all(s.via_chd for s in by_game["Panzer (USA) (1S)"])
    assert RomState.ELSEWHERE not in st_5s     # 5S NIE jest zaspokojone
    assert not any(s.via_chd for s in by_game["Panzer (USA) (5S)"])


def test_all_dat_sums_must_agree(tmp_path: Path):
    """Wiarygodny = zgadzają się WSZYSTKIE sumy z DAT-a. Plik o poprawnym
    SHA-1, ale innym rozmiarze/CRC niż deklaruje DAT — odpada."""
    from chd_buddy.core.matcher import match_entry
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    src = tmp_path / "zrzuty"; src.mkdir()
    data = b"GRA" * 40
    (src / "Gra.iso").write_bytes(data)
    # DAT z poprawnym sha1, ale SFAŁSZOWANYM rozmiarem (niespójny wpis)
    import hashlib as _h, zlib as _z
    dat_root.mkdir(parents=True)
    (dat_root / "a.dat").write_text(
        '<?xml version="1.0"?><datafile><header><name>System A</name></header>'
        f'<game name="Gra"><rom name="Gra.iso" size="{len(data) + 999}" '
        f'crc="{_z.crc32(data) & 0xFFFFFFFF:08x}" '
        f'sha1="{_h.sha1(data).hexdigest()}"/></game></datafile>',
        encoding="utf-8")
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(src)
    entries = DatStore(dat_root, rom_root).discover()
    rep = match_entry(entries[0], idx)
    # sha1 pasuje, rozmiar NIE => plik nie jest uznany za źródło
    assert rep.statuses[0].state == RomState.MISSING
    idx.close()


def test_match_by_dat_sizes_tolerates_chdman_padding(tmp_path: Path):
    """Sklejony obraz z chdman bywa o 1–3 sektory WIĘKSZY (padding ścieżki
    do 4 ramek) — dopasowanie wg rozmiarów z DAT-a toleruje ogon."""
    import hashlib as _h
    from chd_buddy.core.datfile import DatGame, DatIndex, DatRom
    from chd_buddy.core.deepcheck import _match_by_dat_sizes
    sector = 2352
    t1 = b"D" * (sector * 8)
    t2 = b"A" * (sector * 3)              # 3 sektory => padding +1 do 4 ramek
    idx = DatIndex()
    idx.add_game(DatGame("Gra (USA)", [
        DatRom("Gra (USA).cue", 100, sha1="c" * 40),
        DatRom("Gra (USA) (Track 1).bin", len(t1),
               sha1=_h.sha1(t1).hexdigest()),
        DatRom("Gra (USA) (Track 2).bin", len(t2),
               sha1=_h.sha1(t2).hexdigest()),
    ]))
    glued = tmp_path / "glued.bin"
    glued.write_bytes(t1 + t2 + b"\x00" * sector)   # +1 sektor paddingu
    r = _match_by_dat_sizes(glued, idx, lambda m: None)
    assert r is not None and r.ok and r.game == "Gra (USA)"


def test_no_links_between_loose_files_of_chd_games(tmp_path: Path):
    """KATASTROFA D2: dyski współdzielą ścieżkę; przy formacie CHD NIE wolno
    linkować luźnych plików (konwersja kasuje źródła => wiszące linki).
    Każdy dysk dostaje WŁASNĄ kopię fizyczną; dziecko jest ODROCZONE."""
    from chd_buddy.core.matcher import match_store
    from chd_buddy.core.rebuilder import Rebuilder
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    tosort = tmp_path / "tosort"
    wspolna = b"WSPOLNY-TRACK2" * 40
    d1t1, d2t1 = b"D1T1" * 40, b"D2T1" * 40
    for dat_dir, nm in ((dat_root / "ROMS" / "dc.dat", "Sega - Dreamcast"),
                       (dat_root / "1G1R" / "dc.dat",
                        "Sega - Dreamcast (Retool)")):
        _write_dat(dat_dir, nm, {
            "D2 (Disc 1)": {"D2 (Disc 1) (Track 1).bin": d1t1,
                            "D2 (Disc 1) (Track 2).bin": wspolna},
            "D2 (Disc 2)": {"D2 (Disc 2) (Track 1).bin": d2t1,
                            "D2 (Disc 2) (Track 2).bin": wspolna}})
    from chd_buddy.core.dirrules import save_rule
    save_rule(dat_root, "ROMS", {"parent_priority": True, "format": "chd",
                                 "subdir_per_game": False})
    for g, files in (("D2 (Disc 1)", {"D2 (Disc 1) (Track 1).bin": d1t1,
                                      "D2 (Disc 1) (Track 2).bin": wspolna}),
                     ("D2 (Disc 2)", {"D2 (Disc 2) (Track 1).bin": d2t1,
                                      "D2 (Disc 2) (Track 2).bin": wspolna})):
        d = tosort / g
        d.mkdir(parents=True)
        for n, c in files.items():
            (d / n).write_bytes(c)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    from chd_buddy.core.dirrules import DirRules
    rules = DirRules(dat_root)
    apply_rule_targets(entries, rules, rom_root)
    assert entries[0].store_format == "chd"
    rb = Rebuilder(idx, tosort=tosort, dry_run=False)
    st = rb.run(match_store(entries, idx), rules=rules.for_entry,
                delete_placed_from=[tosort])
    parent_dir = entries[0].target_dir
    # WSZYSTKIE pliki rodzica FIZYCZNE (zero linków między luźnymi!)
    for n in ("D2 (Disc 1) (Track 2).bin", "D2 (Disc 2) (Track 2).bin"):
        p = parent_dir / n
        assert p.is_file() and not p.is_symlink(), n
        assert p.read_bytes() == wspolna
    # dziecko ODROCZONE — zero linków do luźnych plików
    child_dir = entries[1].target_dir
    assert not list(child_dir.glob("*")) if child_dir.exists() else True
    assert st.links_skipped >= 1          # odroczone dziecko policzone
    assert st.linked == 0
    idx.close()


def test_dat_dialog_shows_auto_format_from_folder(tmp_path: Path):
    """Okno ustawień DAT-a MUSI mieć opcję 'auto' — inaczej format 'auto'
    z reguły katalogu spada na 'keep' (findData(-1)) i wygląda jakby się nie
    propagował."""
    from chd_buddy.ui.dat_settings_dialog import _FORMAT_LABELS
    keys = [k for k, _l in _FORMAT_LABELS]
    assert "auto" in keys, "combo formatu bez 'auto' — reguła katalogu przepada"
    # wszystkie formaty z reguł muszą być wybieralne w oknie
    from chd_buddy.core.dirrules import FORMATS
    for f in FORMATS:
        assert f in keys, f"brak formatu {f} w oknie DAT-a"


def test_convert_runs_before_dedup(tmp_path: Path):
    """after_place (konwersja) MUSI iść przed _dedup_confirmed — inaczej dedup
    linkuje luźne pliki, które konwersja zaraz pochłania (wiszące linki D2)."""
    from chd_buddy.core.matcher import match_store
    from chd_buddy.core.rebuilder import Rebuilder
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    tosort = tmp_path / "tosort"; tosort.mkdir()
    _write_dat(dat_root / "a.dat", "System A", {"Gra": {"Gra.iso": b"X" * 400}})
    (tosort / "Gra.iso").write_bytes(b"X" * 400)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    order = []
    rb = Rebuilder(idx, tosort=tosort, dry_run=False)
    orig_dedup = rb._dedup_confirmed
    rb._dedup_confirmed = lambda *a, **k: order.append("dedup")
    rb.run(match_store(entries, idx),
           dedup_roots=[rom_root, tosort],
           after_place=lambda: order.append("convert"))
    assert order == ["convert", "dedup"], order
    idx.close()


def test_multitrack_chd_child_gets_single_link(tmp_path: Path):
    """Gra wieloplikowa (bin/cue) zaspokojona jednym CHD rodzica => DZIECKO
    dostaje JEDEN link <gra>.chd, nie po jednym na każdy ROM."""
    import hashlib as _h
    from chd_buddy.core.datfile import game_profile
    from chd_buddy.core.matcher import match_store, RomState
    from chd_buddy.core.rebuilder import Rebuilder
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    t1, t2, cue = b"T1" * 50, b"T2" * 50, b"CUE"
    game = {"D (Disc 1) (Track 1).bin": t1, "D (Disc 1) (Track 2).bin": t2,
            "D (Disc 1).cue": cue}
    _write_dat(dat_root / "ROMS" / "dc.dat", "Sega - Dreamcast",
               {"D (Disc 1)": game})
    _write_dat(dat_root / "1G1R" / "dc.dat", "Sega - Dreamcast (Retool)",
               {"D (Disc 1)": game})
    from chd_buddy.core.dirrules import save_rule, DirRules
    save_rule(dat_root, "ROMS", {"parent_priority": True, "format": "chd",
                                 "subdir_per_game": False})
    src = tmp_path / "src"; src.mkdir()
    (src / "d.chd").write_bytes(b"chd-container")
    idx = FileIndex(tmp_path / "idx.sqlite3")
    prof = game_profile([type("R", (), {"sha1": _h.sha1(t1).hexdigest()})(),
                         type("R", (), {"sha1": _h.sha1(t2).hexdigest()})()])
    idx.scan(src, chd_prober=lambda p: prof)
    entries = DatStore(dat_root, rom_root).discover()
    rules = DirRules(dat_root); apply_rule_targets(entries, rules, rom_root)
    rb = Rebuilder(idx, dry_run=False)
    st = rb.run(match_store(entries, idx), rules=rules.for_entry)
    # DOKŁADNIE jedna operacja linku dla wspólnej ścieżki <gra>.chd (nie 3);
    # linked gdy symlinki dostępne, inaczej links_skipped — suma == 1
    assert st.linked + st.links_skipped == 1, st
    child_dir = [e for e in entries if "Retool" in e.name][0].target_dir
    if child_dir.exists():
        assert len(list(child_dir.glob("*.chd"))) <= 1
    idx.close()


def test_remove_broken_links(tmp_path: Path):
    """Zerwane symlinki (cel przeniesiony/skasowany) są usuwane; poprawne
    linki i zwykłe pliki zostają nietknięte."""
    import os as _os
    from chd_buddy.core.linker import remove_broken_links, create_link
    root = tmp_path / "rom"
    root.mkdir()
    real = root / "real.chd"
    real.write_bytes(b"data")
    good = root / "good.chd"
    try:
        create_link(good, real, is_dir=False)
    except Exception:
        import pytest
        pytest.skip("brak uprawnień do symlinków")
    gone = root / "gone_target.chd"
    gone.write_bytes(b"x")
    bad = root / "bad.chd"
    create_link(bad, gone, is_dir=False)
    gone.unlink()                             # cel znika => bad zerwany
    n = remove_broken_links([root])
    assert n == 1
    assert not bad.exists() and not _os.path.lexists(bad)
    assert good.exists() and real.exists()    # poprawny link i plik zostają


def test_pick_scratch_root_uses_drive_with_space(tmp_path, monkeypatch):
    """Wybór scratch: gdy preferowany katalog jest PEŁNY, bierze inny dysk
    z zapasem; gdy nigdzie nie ma miejsca — None."""
    import chd_buddy.core.scratch as sc
    full = tmp_path / "full"; big = tmp_path / "big"
    full.mkdir(); big.mkdir()
    free_map = {str(full): 1 << 20, str(big): 100 << 30}
    monkeypatch.setattr(sc, "_free",
                        lambda p: free_map.get(str(p), 0))
    monkeypatch.setattr(sc, "_candidate_roots",
                        lambda prefer: [str(full), str(big)])
    # potrzeba 10 GB: preferowany full ma za mało => big
    got = sc.pick_scratch_root(10 << 30, prefer=str(full))
    assert got is not None and str(big) in str(got)
    # preferowany ma dość => zostaje preferowany
    got2 = sc.pick_scratch_root(1 << 10, prefer=str(full))
    assert str(full) in str(got2)
    # nigdzie nie ma => None
    monkeypatch.setattr(sc, "_free", lambda p: 1 << 10)
    assert sc.pick_scratch_root(10 << 30, prefer=str(full)) is None


def test_scratch_prefers_ramdisk_then_falls_back(tmp_path, monkeypatch):
    """Scratch: RAM dysk gdy plik się mieści; gdy nie — dysk fizyczny z
    zapasem; RAM nieaktywny — pomijany."""
    import chd_buddy.core.scratch as sc
    from chd_buddy.core import ramdisk
    ram = tmp_path / "ram"; disk = tmp_path / "disk"
    ram.mkdir(); disk.mkdir()
    free = {str(ram): 30 << 30, str(disk): 500 << 30}
    monkeypatch.setattr(sc, "_free", lambda p: free.get(str(Path(p)), 0))
    monkeypatch.setattr(sc, "_candidate_roots", lambda prefer: [str(disk)])
    monkeypatch.setattr(ramdisk, "active_root", lambda: ram)
    # mieści się w RAM => RAM
    got = sc.pick_scratch_root(2 << 30, prefer=str(disk))
    assert str(ram) in str(got)
    # 100 GB > 30 GB RAM => dysk fizyczny
    got2 = sc.pick_scratch_root(100 << 30, prefer=str(disk))
    assert str(disk) in str(got2)
    # RAM nieaktywny => od razu dysk
    monkeypatch.setattr(ramdisk, "active_root", lambda: None)
    got3 = sc.pick_scratch_root(2 << 30, prefer=str(disk))
    assert str(disk) in str(got3)

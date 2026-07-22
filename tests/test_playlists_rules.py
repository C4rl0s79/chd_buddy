"""Testy playlist .m3u, reguł per katalog i zamiany kopii na symlinki."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chd_buddy.core.dirrules import DirRules
from chd_buddy.core.playlists import generate_m3u, scan_m3u


# --- m3u -------------------------------------------------------------------

def test_m3u_flat_layout(tmp_path: Path):
    d = tmp_path / "psx"
    d.mkdir()
    (d / "Gra (USA) (Disc 1).chd").write_bytes(b"a")
    (d / "Gra (USA) (Disc 2).chd").write_bytes(b"b")
    (d / "Solo (USA).chd").write_bytes(b"c")
    st = generate_m3u(tmp_path)
    assert st.created == 1
    m3u = d / "Gra (USA).m3u"
    assert m3u.read_text(encoding="utf-8") == \
        "Gra (USA) (Disc 1).chd\nGra (USA) (Disc 2).chd\n"
    # LF, bez BOM
    raw = m3u.read_bytes()
    assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    # drugi przebieg nie nadpisuje
    st2 = generate_m3u(tmp_path)
    assert st2.created == 0 and st2.skipped == 1


def test_m3u_subdir_layout_and_cue_priority(tmp_path: Path):
    d = tmp_path / "saturn"
    for n in (1, 2):
        sub = d / f"Gra (Disc {n})"
        sub.mkdir(parents=True)
        (sub / "game.cue").write_text("FILE", encoding="utf-8")
        (sub / "game.bin").write_bytes(b"x")
    groups = scan_m3u(tmp_path)
    assert len(groups) == 1
    g = groups[0]
    assert g.mode == "subdir"
    assert g.discs == ["Gra (Disc 1)/game.cue", "Gra (Disc 2)/game.cue"]


def test_m3u_skips_managed_dirs(tmp_path: Path):
    d = tmp_path / "psx" / "icons"
    d.mkdir(parents=True)
    (d / "Gra (Disc 1).chd").write_bytes(b"a")
    (d / "Gra (Disc 2).chd").write_bytes(b"b")
    assert scan_m3u(tmp_path) == []


# --- reguły per katalog -------------------------------------------------------

class _FakeEntry:
    def __init__(self, dat_path: Path, name: str):
        self.dat_path = dat_path
        self.name = name


def test_dirrules_cascade(tmp_path: Path):
    (tmp_path / "_reguly.json").write_text(json.dumps({
        "*": {"only_complete": True},
        "PS2": {"only_complete": False},
        "Stary Zestaw": {"skip": True},
        "konsole/ps1": {"dedup_copies": False},
    }), encoding="utf-8")
    rules = DirRules(tmp_path)
    assert not rules.error

    # ogólna reguła
    e1 = _FakeEntry(tmp_path / "x.dat", "Cokolwiek")
    eff = rules.for_entry(e1)
    assert (eff["only_complete"], eff["skip"], eff["dedup_copies"]) == \
        (True, False, True)
    # katalog nadpisuje "*"
    e2 = _FakeEntry(tmp_path / "PS2" / "y.dat", "Pelny PS2")
    assert rules.for_entry(e2)["only_complete"] is False
    # nazwa DAT-a nadpisuje katalog
    e3 = _FakeEntry(tmp_path / "PS2" / "z.dat", "Stary Zestaw")
    r3 = rules.for_entry(e3)
    assert r3["skip"] is True and r3["only_complete"] is False
    # zagnieżdżony katalog
    e4 = _FakeEntry(tmp_path / "konsole" / "ps1" / "a.dat", "PS1")
    assert rules.for_entry(e4)["dedup_copies"] is False


def test_naming_es_and_rom_root_override(tmp_path: Path):
    """Konwencja ES (ps2) + osobny rom_root dla dziecka; kaskada folder→DAT."""
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.dirrules import DirRules, apply_rule_targets, save_rule
    from tests.test_rebuilder import _write_dat
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    _write_dat(dat_root / "ROMS" / "ps2.dat", "Sony - PlayStation 2 - Datfile",
               {"G": {"g.iso": b"x" * 40}})
    _write_dat(dat_root / "1G1R" / "ps2.dat",
               "Sony - PlayStation 2 (Redump - 1G1R)", {"G": {"g.iso": b"x" * 40}})

    # folder ROMS: konwencja ES; DAT 1G1R: osobny rom_root
    save_rule(dat_root, "ROMS", {"naming": "es"})
    child_root = tmp_path / "nas_child"
    save_rule(dat_root, "Sony - PlayStation 2 (Redump - 1G1R)",
              {"rom_root": str(child_root), "naming": "es"})

    entries = DatStore(dat_root, rom_root).discover()
    rules = DirRules(dat_root)
    apply_rule_targets(entries, rules, rom_root)
    full = [e for e in entries if "Datfile" in e.name][0]
    g1r = [e for e in entries if "1G1R" in e.name][0]
    # pełny (folder ROMS, ES) → roms/ps2 PŁASKO: ES/RetroBat wymaga
    # <rom_root>/<system>, katalog-grupa ROMS nie wchodzi do ścieżki fizycznej
    assert full.target_dir == rom_root / "ps2"
    # 1G1R z własnym rom_root → nas_child/ps2 (płasko, inny root)
    assert g1r.target_dir == child_root / "ps2"


def test_format_auto_by_system(tmp_path: Path):
    from chd_buddy.core.dirrules import resolve_format
    from tests.test_rebuilder import _write_dat
    from chd_buddy.core.datstore import DatStore
    dat_root = tmp_path / "d"
    _write_dat(dat_root / "ps2.dat", "Sony - PlayStation 2", {"G": {"g.iso": b"x"}})
    _write_dat(dat_root / "gc.dat", "Nintendo - GameCube", {"G": {"g.iso": b"y"}})
    _write_dat(dat_root / "snes.dat",
               "Nintendo - Super Nintendo Entertainment System",
               {"G": {"g.sfc": b"z"}})
    ents = {e.name: e for e in DatStore(dat_root, tmp_path / "r").discover()}
    assert resolve_format("auto", ents["Sony - PlayStation 2"]) == "chd"
    assert resolve_format("auto", ents["Nintendo - GameCube"]) == "rvz"
    assert resolve_format(
        "auto", ents["Nintendo - Super Nintendo Entertainment System"]) == "zip"


def test_parent_priority_folder_overrides_size(tmp_path: Path):
    """Folder z parent_priority robi swoje DAT-y rodzicami mimo mniejszego
    rozmiaru niż DAT innej platformy w innym folderze — per platforma."""
    from chd_buddy.core.datstore import DatStore, platform_key
    from chd_buddy.core.dirrules import save_rule
    from tests.test_rebuilder import _write_dat
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    # PS2: duży w ROMS, mały w 1G1R; oznaczamy 1G1R jako rodziców
    _write_dat(dat_root / "ROMS" / "ps2.dat", "Sony - PlayStation 2 - Datfile",
               {f"G{i}": {f"g{i}.iso": bytes([i]) * 40} for i in range(3)})
    _write_dat(dat_root / "1G1R" / "ps2.dat",
               "Sony - PlayStation 2 (Redump - 1G1R)", {"G0": {"g0.iso": b"\x00" * 40}})
    save_rule(dat_root, "1G1R", {"parent_priority": True})

    entries = DatStore(dat_root, rom_root).discover()
    ps2 = [e for e in entries if platform_key(e.name) == "sony playstation 2"]
    assert "1G1R" in str(ps2[0].dat_path)      # 1G1R rodzicem mimo mniejszego
    assert "ROMS" in str(ps2[1].dat_path)


def test_folder_bulk_then_individual_override(tmp_path: Path):
    """Workflow usera: reguła KATALOGU obowiązuje wszystkie DAT-y w środku;
    potem pojedynczy DAT można nadpisać i jego wpis wygrywa (kaskada)."""
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.dirrules import save_rule
    from tests.test_rebuilder import _write_dat
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    _write_dat(dat_root / "ROMS" / "ps2.dat", "Sony - PlayStation 2",
               {"G": {"g.iso": b"x" * 40}})
    _write_dat(dat_root / "ROMS" / "snes.dat",
               "Nintendo - Super Nintendo Entertainment System",
               {"G": {"g.sfc": b"y" * 40}})
    # 1) zbiorczo dla całego katalogu ROMS
    save_rule(dat_root, "ROMS", {"naming": "es", "only_complete": False})
    entries = {e.name: e for e in DatStore(dat_root, rom_root).discover()}
    rules = DirRules(dat_root)
    for e in entries.values():                       # obowiązuje wszystkie
        assert rules.for_entry(e)["naming"] == "es"
        assert rules.for_entry(e)["only_complete"] is False
    # 2) nadpisz POJEDYNCZY DAT (jak edycja jednego / kilku zaznaczonych) —
    # nadpisanie na wartość domyślną (naming=dat) MUSI przetrwać mimo folderu
    save_rule(dat_root, "Sony - PlayStation 2", {"naming": "dat"},
              strip_defaults=False)
    rules = DirRules(dat_root)                        # przeładuj reguły
    assert rules.for_entry(entries["Sony - PlayStation 2"])["naming"] == "dat"
    assert rules.for_entry(
        entries["Nintendo - Super Nintendo Entertainment System"]
    )["naming"] == "es"                              # inne nietknięte
    # only_complete z katalogu wciąż działa dla nadpisanego DAT-a
    assert rules.for_entry(
        entries["Sony - PlayStation 2"])["only_complete"] is False


def test_dirrules_target_redirect(tmp_path: Path):
    """Reguła target: DAT buduje w istniejącym katalogu (np. ps2 zamiast
    'Sony - PlayStation 2') — układ EmulationStation zostaje nienaruszony."""
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.dirrules import apply_rule_targets
    from tests.test_rebuilder import _write_dat
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    _write_dat(dat_root / "a.dat", "Sony - PlayStation 2",
               {"Gra": {"Gra.iso": b"X" * 50}})
    (dat_root / "_reguly.json").write_text(json.dumps({
        "Sony - PlayStation 2": {"target": "ps2"},
    }), encoding="utf-8")
    entries = DatStore(dat_root, rom_root).discover()
    assert entries[0].target_dir == rom_root / "Sony - PlayStation 2"
    apply_rule_targets(entries, DirRules(dat_root), rom_root)
    assert entries[0].target_dir == rom_root / "ps2"


def test_dirrules_missing_file_defaults(tmp_path: Path):
    rules = DirRules(tmp_path)
    e = _FakeEntry(tmp_path / "x.dat", "X")
    assert rules.for_entry(e)["only_complete"] is True


def test_dirrules_broken_json(tmp_path: Path):
    (tmp_path / "_reguly.json").write_text("{zepsuty", encoding="utf-8")
    rules = DirRules(tmp_path)
    assert rules.error
    e = _FakeEntry(tmp_path / "x.dat", "X")
    from chd_buddy.core.dirrules import DEFAULT_RULES
    assert rules.for_entry(e) == DEFAULT_RULES


# --- kopie potwierdzonych -> symlinki -------------------------------------------

def test_prefer_translations_substitutes_japan(tmp_path: Path, monkeypatch):
    """Reguła prefer_translations: wersja (Japan) w zestawie 1G1R jest
    zastępowana linkiem do dostępnego tłumaczenia [T-En] z innego DAT-a."""
    import chd_buddy.core.rebuilder as rb_mod
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.fileindex import FileIndex
    from chd_buddy.core.matcher import match_store
    from chd_buddy.core.rebuilder import Rebuilder
    from tests.test_rebuilder import _write_dat

    fake_links: dict[str, str] = {}

    def fake_create_link(link_path, target, is_dir):
        Path(link_path).parent.mkdir(parents=True, exist_ok=True)
        Path(link_path).touch()
        fake_links[os.path.normcase(str(link_path))] = str(target)

    monkeypatch.setattr(rb_mod, "create_link", fake_create_link)
    monkeypatch.setattr(rb_mod, "is_link",
                        lambda p: os.path.normcase(str(p)) in fake_links)

    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    jp = b"WERSJA-JAPONSKA" * 30
    ten = b"WERSJA-TLUMACZONA" * 30
    _write_dat(dat_root / "1g1r.dat", "Zestaw 1G1R",
               {"Napple Tale (Japan)": {"Napple Tale (Japan).iso": jp}})
    _write_dat(dat_root / "ten.dat", "Tlumaczenia T-En",
               {"Napple Tale (Japan) [T-En by Cargodin v1.0]":
                {"Napple Tale (Japan) [T-En by Cargodin v1.0].iso": ten}})
    (dat_root / "_reguly.json").write_text(json.dumps({
        "Zestaw 1G1R": {"prefer_translations": True},
    }), encoding="utf-8")

    src = tmp_path / "zrzuty"
    src.mkdir()
    (src / "jp.iso").write_bytes(jp)
    (src / "ten.iso").write_bytes(ten)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(src)

    from chd_buddy.core.dirrules import DirRules, apply_rule_targets
    entries = DatStore(dat_root, rom_root).discover()
    rules = DirRules(dat_root)
    apply_rule_targets(entries, rules, rom_root)
    st = Rebuilder(idx, dry_run=False).run(match_store(entries, idx),
                                           rules=rules.for_entry)
    assert st.substituted == 1 and st.errors == 0

    # tłumaczenie ułożone w swoim DAT-cie
    ten_canon = rom_root / "Tlumaczenia T-En" / \
        "Napple Tale (Japan) [T-En by Cargodin v1.0].iso"
    assert ten_canon.read_bytes() == ten
    # w 1G1R NIE ma wersji japońskiej…
    g1 = rom_root / "Zestaw 1G1R"
    assert not (g1 / "Napple Tale (Japan).iso").exists()
    # …jest za to link do tłumaczenia
    link = g1 / "Napple Tale (Japan) [T-En by Cargodin v1.0].iso"
    assert fake_links[os.path.normcase(str(link))] == str(ten_canon)
    # oryginał japoński został w źródle (nietknięty)
    assert (src / "jp.iso").read_bytes() == jp


def test_rebuild_dedup_confirmed_copies(tmp_path: Path, monkeypatch):
    """Po naprawie: identyczna kopia w ToSort zamieniana na symlink do
    kanonicznej kopii rodzica (symlink podstawiony — bez uprawnień)."""
    import chd_buddy.core.rebuilder as rb_mod
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.fileindex import FileIndex
    from chd_buddy.core.matcher import match_store
    from chd_buddy.core.rebuilder import Rebuilder
    from tests.test_rebuilder import _write_dat

    fake_links: dict[str, str] = {}

    def fake_create_link(link_path, target, is_dir):
        Path(link_path).parent.mkdir(parents=True, exist_ok=True)
        Path(link_path).touch()
        fake_links[os.path.normcase(str(link_path))] = str(target)

    monkeypatch.setattr(rb_mod, "create_link", fake_create_link)
    monkeypatch.setattr(rb_mod, "is_link",
                        lambda p: os.path.normcase(str(p)) in fake_links)

    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    tosort = tmp_path / "tosort"
    tosort.mkdir()
    data = b"POTWIERDZONE" * 40
    _write_dat(dat_root / "a.dat", "Zestaw", {"Gra": {"Gra.iso": data}})

    canonical = rom_root / "Zestaw" / "Gra.iso"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(data)                # potwierdzony plik na miejscu
    kopia = tosort / "duplikat.iso"
    kopia.write_bytes(data)                    # fizyczna kopia w ToSort

    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)
    idx.scan(tosort)

    entries = DatStore(dat_root, rom_root).discover()
    rb = Rebuilder(idx, tosort=tosort, dry_run=False)
    st = rb.run(match_store(entries, idx),
                dedup_roots=[rom_root, tosort])
    assert st.deduped == 1 and st.errors == 0
    # kopia stała się "symlinkiem" do kanonicznej, kanoniczna nietknięta
    assert fake_links[os.path.normcase(str(kopia))] == str(canonical)
    assert canonical.read_bytes() == data
    assert not kopia.with_name("duplikat.iso.chdbuddy_dedup_tmp").exists()


def test_delete_placed_from_tosort(tmp_path: Path):
    """Plik w ToSort, którego potwierdzona kopia jest już na miejscu,
    jest KASOWANY (weryfikacja SHA-1); plik kanoniczny nietknięty."""
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.fileindex import FileIndex
    from chd_buddy.core.matcher import match_store
    from chd_buddy.core.rebuilder import Rebuilder
    from tests.test_rebuilder import _write_dat

    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    tosort = tmp_path / "tosort"
    tosort.mkdir()
    data = b"POTWIERDZONE" * 40
    _write_dat(dat_root / "a.dat", "Zestaw", {"Gra": {"Gra.iso": data}})
    canonical = rom_root / "Zestaw" / "Gra.iso"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(data)                 # już na miejscu
    junk = tosort / "kopia_gry.iso"
    junk.write_bytes(data)                       # zbędna kopia w ToSort

    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)
    idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    st = Rebuilder(idx, tosort=tosort, dry_run=False).run(
        match_store(entries, idx), delete_placed_from=[tosort])
    assert st.tosort_deleted == 1 and st.errors == 0
    assert not junk.exists()                     # skasowana
    assert canonical.read_bytes() == data        # oryginał nietknięty
    assert idx.lookup(junk) is None              # usunięta z indeksu


def test_rebuild_dedup_respects_protected_rule(tmp_path: Path, monkeypatch):
    """Reguła dedup_copies=false chroni katalog przed zamianą kopii."""
    import chd_buddy.core.rebuilder as rb_mod
    from chd_buddy.core.datstore import DatStore
    from chd_buddy.core.dirrules import DirRules
    from chd_buddy.core.fileindex import FileIndex
    from chd_buddy.core.matcher import match_store
    from chd_buddy.core.rebuilder import Rebuilder
    from tests.test_rebuilder import _write_dat

    monkeypatch.setattr(rb_mod, "create_link",
                        lambda *a, **k: pytest.fail("nie wolno linkować"))

    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data = b"CHRONIONE" * 40
    _write_dat(dat_root / "a.dat", "Zestaw A", {"Gra": {"Gra.iso": data}})
    _write_dat(dat_root / "b.dat", "Zestaw B", {"Gra": {"Gra.iso": data}})
    (tmp_path / "dats" / "_reguly.json").write_text(
        json.dumps({"Zestaw B": {"dedup_copies": False}}), encoding="utf-8")

    a = rom_root / "Zestaw A" / "Gra.iso"
    b = rom_root / "Zestaw B" / "Gra.iso"
    for p in (a, b):
        p.parent.mkdir(parents=True)
        p.write_bytes(data)

    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    rules = DirRules(dat_root)
    st = Rebuilder(idx, dry_run=False).run(
        match_store(entries, idx), rules=rules.for_entry,
        dedup_roots=[rom_root])
    # b jest ścieżką kanoniczną Zestawu B (HAVE) => i tak nietykane;
    # ale nawet gdyby nie było — reguła chroni katalog. Zero podmian.
    assert st.deduped == 0 and st.errors == 0
    assert b.read_bytes() == data and not b.name.endswith("tmp")
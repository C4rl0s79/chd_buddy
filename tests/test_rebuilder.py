"""Testy DatStore + matcher + rebuilder (silnik w stylu RomVaulta)."""
from __future__ import annotations

import hashlib
import os
import zlib
from pathlib import Path

import pytest

from chd_buddy.core.datstore import DatStore
from chd_buddy.core.fileindex import FileIndex
from chd_buddy.core.linker import is_link
from chd_buddy.core.matcher import RomState, match_store
from chd_buddy.core.rebuilder import Rebuilder


def _can_symlink(tmp_path: Path) -> bool:
    t = tmp_path / "_pt.txt"
    t.write_text("x", encoding="utf-8")
    l = tmp_path / "_pl.txt"
    try:
        os.symlink(t, l)
    except OSError:
        return False
    finally:
        l.unlink(missing_ok=True)
        t.unlink(missing_ok=True)
    return True


def _rom_attrs(content: bytes) -> str:
    return (f'size="{len(content)}" crc="{zlib.crc32(content) & 0xFFFFFFFF:08x}" '
            f'md5="{hashlib.md5(content).hexdigest()}" '
            f'sha1="{hashlib.sha1(content).hexdigest()}"')


def _write_dat(path: Path, name: str, games: dict[str, dict[str, bytes]]) -> None:
    """games: {nazwa_gry: {nazwa_romu: zawartość}}"""
    parts = ['<?xml version="1.0"?><datafile>',
             f"<header><name>{name}</name></header>"]
    for game, roms in games.items():
        parts.append(f'<game name="{game}">')
        for rom_name, content in roms.items():
            parts.append(f'<rom name="{rom_name}" {_rom_attrs(content)}/>')
        parts.append("</game>")
    parts.append("</datafile>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")


@pytest.fixture()
def world(tmp_path: Path):
    """Mały świat: DatRoot z 2 DAT-ami (wspólny plik), RomRoot, ToSort, indeks."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    tosort = tmp_path / "tosort"
    shared = b"SHARED-GAME-DATA" * 64
    only_a = b"ONLY-IN-A" * 64
    _write_dat(dat_root / "PS2" / "a.dat", "Zestaw A",
               {"Gra Wspólna": {"Gra Wspólna.iso": shared},
                "Gra A": {"Gra A.iso": only_a}})
    _write_dat(dat_root / "PS2" / "b.dat", "Zestaw B",
               {"Gra Wspólna": {"Gra Wspólna.iso": shared}})
    src = tmp_path / "zrzuty"
    src.mkdir()
    (src / "wspolna_zla_nazwa.iso").write_bytes(shared)
    (src / "gra_a.iso").write_bytes(only_a)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(src)
    return {"dat_root": dat_root, "rom_root": rom_root, "tosort": tosort,
            "src": src, "idx": idx, "shared": shared, "only_a": only_a}


def test_datstore_discovery_and_mapping(world):
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    assert [e.name for e in entries] == ["Zestaw A", "Zestaw B"]
    assert entries[0].target_dir == world["rom_root"] / "PS2" / "Zestaw A"


def test_datstore_collision_identical_content(tmp_path: Path):
    """Identyczny DAT w dwóch katalogach => jedna kopia (głębsza wygrywa),
    kolizja logowana, pliki na dysku nietknięte."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data = b"WSPOLNY" * 20
    _write_dat(dat_root / "kopia.dat", "Zestaw X", {"Gra": {"Gra.iso": data}})
    (dat_root / "ROMS").mkdir()
    import shutil as _sh
    _sh.copy2(dat_root / "kopia.dat", dat_root / "ROMS" / "kopia.dat")

    logs: list[str] = []
    entries = DatStore(dat_root, rom_root).discover(log=logs.append)
    assert len(entries) == 1
    assert entries[0].dat_path == dat_root / "ROMS" / "kopia.dat"  # głębsza
    assert any("KOLIZJA DAT" in m for m in logs)
    assert (dat_root / "kopia.dat").exists()      # nic nie skasowane


def test_matcher_states(world):
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    reports = match_store(entries, world["idx"])
    # wszystko istnieje, ale poza katalogami docelowymi
    for rep in reports:
        for s in rep.statuses:
            assert s.state == RomState.ELSEWHERE, (s.rom.name, s.state)


def test_rebuild_moves_and_links(world):
    if not _can_symlink(world["src"]):
        pytest.skip("brak uprawnień do symlinków (tryb dewelopera/admin)")
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    reports = match_store(entries, idx)
    rb = Rebuilder(idx, tosort=world["tosort"], dry_run=False)
    st = rb.run(reports)

    a_dir = world["rom_root"] / "PS2" / "Zestaw A"
    b_dir = world["rom_root"] / "PS2" / "Zestaw B"
    # DAT A (priorytet) dostał pliki fizyczne pod kanonicznymi nazwami
    assert (a_dir / "Gra Wspólna.iso").read_bytes() == world["shared"]
    assert not is_link(a_dir / "Gra Wspólna.iso")
    assert (a_dir / "Gra A.iso").read_bytes() == world["only_a"]
    # DAT B dostał symlink do kopii w A
    assert is_link(b_dir / "Gra Wspólna.iso")
    assert (b_dir / "Gra Wspólna.iso").read_bytes() == world["shared"]
    assert st.moved == 2 and st.linked == 1 and st.errors == 0
    # źródło jest puste (pliki przeniesione, nie skopiowane)
    assert list(world["src"].iterdir()) == []
    # indeks zna nowe ścieżki bez ponownego skanu
    row = idx.lookup(a_dir / "Gra Wspólna.iso")
    assert row is not None and row["missing"] == 0

    # drugi przebieg jest idempotentny
    reports2 = match_store(entries, idx)
    rb2 = Rebuilder(idx, dry_run=False)
    st2 = rb2.run(reports2)
    assert st2.moved == 0 and st2.linked == 0 and st2.errors == 0
    assert st2.already_ok == 4  # 3 fizyczne + 1 link


def test_parent_moved_repoints_child_link(world):
    """Zmiana katalogu docelowego rodzica → w tym samym przebiegu rodzic
    przenosi swój plik fizyczny do NOWEGO miejsca, a link dziecka MUSI się
    przepiąć na nowy cel (inaczej wisi). To był raportowany błąd: „przenoszę
    patenty do nowego katalogu, ale przestają działać linki"."""
    if not _can_symlink(world["src"]):
        pytest.skip("brak uprawnień do symlinków (tryb dewelopera/admin)")
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    reports = match_store(entries, idx)
    Rebuilder(idx, dry_run=False).run(reports)

    a_dir = world["rom_root"] / "PS2" / "Zestaw A"
    b_dir = world["rom_root"] / "PS2" / "Zestaw B"
    assert not is_link(a_dir / "Gra Wspólna.iso")     # rodzic = fizyczny
    assert is_link(b_dir / "Gra Wspólna.iso")          # dziecko = link
    old_target = os.path.abspath(os.readlink(str(b_dir / "Gra Wspólna.iso")))
    assert os.path.normcase(old_target) == os.path.normcase(str(a_dir / "Gra Wspólna.iso"))

    # użytkownik zmienia katalog docelowy rodzica (jak w ustawieniach DAT-a)
    new_a = world["rom_root"] / "PS2" / "Zestaw A NOWY"
    entries[0].target_dir = new_a

    reports2 = match_store(entries, idx)
    st = Rebuilder(idx, dry_run=False).run(reports2)

    # rodzic przeniesiony do nowego katalogu
    assert (new_a / "Gra Wspólna.iso").read_bytes() == world["shared"]
    assert not is_link(new_a / "Gra Wspólna.iso")
    assert not (a_dir / "Gra Wspólna.iso").exists()    # stare miejsce puste
    # link dziecka przepięty na NOWY cel i DZIAŁA (czyta zawartość)
    link = b_dir / "Gra Wspólna.iso"
    assert is_link(link)
    new_target = os.path.abspath(os.readlink(str(link)))
    assert os.path.normcase(new_target) == os.path.normcase(str(new_a / "Gra Wspólna.iso"))
    assert link.read_bytes() == world["shared"]        # nie wisi
    assert st.linked >= 1 and st.errors == 0


def test_relink_if_stale_logic(tmp_path, monkeypatch):
    """Sama logika przepięcia linku (bez uprawnień do symlinków w CI):
    stary/zerwany cel → odtwórz na aktualny; już poprawny → nic nie rób."""
    import chd_buddy.core.rebuilder as rb_mod
    idx = FileIndex(tmp_path / "i.sqlite3")
    rb = Rebuilder(idx, dry_run=False)

    new = tmp_path / "new" / "g.iso"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"DATA")
    link = tmp_path / "child" / "g.iso"
    link.parent.mkdir(parents=True)
    link.write_bytes(b"placeholder")   # udaje istniejący reparse-point

    removed = {"n": 0}
    created = {"args": None}

    def fake_remove(p):
        removed["n"] += 1
        Path(p).unlink(missing_ok=True)

    def fake_create(lp, tgt, is_dir=False):
        created["args"] = (str(lp), str(tgt))
        Path(lp).write_bytes(b"LINK")

    monkeypatch.setattr(rb_mod, "remove_link", fake_remove)
    monkeypatch.setattr(rb_mod, "create_link", fake_create)

    # 1) link celuje w STARE miejsce → przepnij na nowe
    monkeypatch.setattr(os, "readlink", lambda p: str(tmp_path / "old" / "g.iso"))
    assert rb._relink_if_stale(link, new) is True
    assert removed["n"] == 1
    assert os.path.normcase(created["args"][1]) == os.path.normcase(str(new))
    assert rb.stats.linked == 1

    # 2) link już wskazuje właściwy cel → nic nie rób (liczony jako already_ok)
    monkeypatch.setattr(os, "readlink", lambda p: str(new))
    created["args"] = None
    assert rb._relink_if_stale(link, new) is False
    assert created["args"] is None

    # 3) cel jeszcze nie istnieje → zostaw link jak jest
    monkeypatch.setattr(os, "readlink", lambda p: str(tmp_path / "x" / "g.iso"))
    assert rb._relink_if_stale(link, tmp_path / "brak" / "g.iso") is False


def test_retry_locked_recovers_from_transient_lock():
    """WinError 32 (plik chwilowo używany) jest ponawiany, inne błędy nie."""
    from chd_buddy.core.rebuilder import _retry_locked
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            e = OSError("zajęty")
            e.winerror = 32
            raise e
        return "ok"

    assert _retry_locked(flaky, attempts=4, delay=0.01) == "ok"
    assert calls["n"] == 3

    def hard_fail():
        e = OSError("nie ma")
        e.winerror = 2
        raise e

    with pytest.raises(OSError):
        _retry_locked(hard_fail, attempts=3, delay=0.01)


def test_rebuild_progress_counts_real_files_not_statuses(world):
    """Pasek OGÓLNY fazy „naprawa" liczy REALNE pliki źródłowe do ułożenia
    (distinct source), a NIE wszystkie statusy ROM z DAT-ów. Bez tego przy
    9 plikach w ToSort pokazywało „34/35" z definicji całej kolekcji."""
    import os
    from chd_buddy.core.matcher import RomState
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    reports = match_store(entries, idx)
    n_statuses = sum(len(r.statuses) for r in reports)
    expected = len({os.path.normcase(s.source_path)
                    for r in reports for s in r.statuses
                    if s.state in (RomState.ELSEWHERE, RomState.WRONG_NAME)
                    and s.source_path}) or 1
    assert expected < n_statuses          # realnych plików MNIEJ niż statusów
    calls: list[tuple[int, int, str]] = []
    Rebuilder(idx, dry_run=True).run(
        reports, on_progress=lambda i, n, t: calls.append((i, n, t)))
    naprawa = [(i, n, t) for i, n, t in calls if t.startswith("naprawa")]
    assert naprawa, "brak sygnałów fazy naprawy"
    # total = liczba realnych plików (nie statusów)
    assert all(n == expected for _i, n, _t in naprawa)
    assert naprawa[-1][0] == expected     # osiąga komplet realnych plików


def test_rebuild_dry_run_changes_nothing(world):
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    reports = match_store(entries, idx)
    st = Rebuilder(idx, dry_run=True).run(reports)
    assert st.moved == 2 and st.linked == 1
    assert not world["rom_root"].exists()
    assert (world["src"] / "gra_a.iso").exists()


def test_rebuild_rename_wrong_name(world, tmp_path: Path):
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    # plik z dobrą zawartością już w katalogu DAT-u, ale pod złą nazwą;
    # usuwamy też wspólny plik ze źródła, żeby scenariusz nie wymagał symlinków
    a_dir = world["rom_root"] / "PS2" / "Zestaw A"
    a_dir.mkdir(parents=True)
    bad = a_dir / "źle nazwana.iso"
    bad.write_bytes(world["only_a"])
    (world["src"] / "gra_a.iso").unlink()
    (world["src"] / "wspolna_zla_nazwa.iso").unlink()
    idx.scan(world["src"])
    idx.scan(world["rom_root"])

    reports = match_store(entries, idx)
    s = [s for r in reports for s in r.statuses if s.rom.name == "Gra A.iso"][0]
    assert s.state == RomState.WRONG_NAME
    st = Rebuilder(idx, dry_run=False).run(reports)
    assert st.renamed == 1
    assert (a_dir / "Gra A.iso").read_bytes() == world["only_a"]
    assert not bad.exists()


def test_rebuild_clean_moves_unknown_to_tosort(world):
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    a_dir = world["rom_root"] / "PS2" / "Zestaw A"
    a_dir.mkdir(parents=True)
    junk = a_dir / "smieć.bin"
    junk.write_bytes(b"JUNK" * 10)
    # bez plików źródłowych scenariusz nie wymaga symlinków
    for f in list(world["src"].iterdir()):
        f.unlink()
    idx.scan(world["src"])
    idx.scan(world["rom_root"])

    reports = match_store(entries, idx)
    rb = Rebuilder(idx, tosort=world["tosort"], dry_run=False)
    calls: list[tuple[int, int, str]] = []
    rb.run(reports, clean=True,
           on_progress=lambda i, n, t: calls.append((i, n, t)))
    assert not junk.exists()
    moved = world["tosort"] / "Zestaw A" / "smieć.bin"
    assert moved.exists() and moved.read_bytes() == b"JUNK" * 10
    # FAZA sprzątania rusza pasek ogólny (inaczej stał podczas mielenia ToSort)
    sprzat = [(i, n) for i, n, t in calls if t.startswith("sprzątanie")]
    assert sprzat, "brak sygnałów fazy sprzątania do ToSort"
    assert sprzat[-1][0] >= 1                  # policzył przeniesiony plik


def test_add_canonical_keeps_converted_out_of_tosort(world):
    """Plik zgłoszony jako kanoniczny (np. świeży CHD z konwersji) NIE jest
    traktowany jako nieznany — faza sprzątania go NIE przenosi do ToSort."""
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    a_dir = world["rom_root"] / "PS2" / "Zestaw A"
    a_dir.mkdir(parents=True)
    # świeży „CHD" po konwersji (nie jest ścieżką kanoniczną z raportu)
    chd = a_dir / "Gra Wspólna.chd"
    chd.write_bytes(b"CHD-PO-KONWERSJI" * 100)
    for f in list(world["src"].iterdir()):
        f.unlink()
    idx.scan(world["src"]); idx.scan(world["rom_root"])
    reports = match_store(entries, idx)

    rb = Rebuilder(idx, tosort=world["tosort"], dry_run=False)
    rb.add_canonical(chd)                          # tak robi callback konwersji
    rb.run(reports, clean=True)
    assert chd.exists()                            # ZOSTAJE na miejscu
    assert not (world["tosort"] / "Zestaw A" / "Gra Wspólna.chd").exists()


def test_convert_from_source_then_placement_skips(tmp_path, monkeypatch):
    """Integracja: konwersja PROSTO ZE ŹRÓDŁA robi ZIP w docelowym, a placement
    (rb.run) POMIJA tę grę (converted_games) i nie układa luźnego źródła; faza
    sprzątania nie wrzuca finału do ToSort."""
    import zipfile
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
    (tosort / "Gra (USA).nes").write_bytes(data)     # źródło w ToSort
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r

    rb = Rebuilder(idx, tosort=tosort, dry_run=False)
    st_c, done, to_purge = convert_from_source(
        reports, rules_fn, {"settings": None}, index=idx,
        on_converted=rb.add_canonical)
    assert st_c.converted == 1 and done
    target = rom_root / "Nintendo - Nintendo Entertainment System"
    zip_path = target / "Gra (USA).zip"
    assert zip_path.is_file()

    # placement: pomija skonwertowaną grę; sprzątanie nie rusza finału
    rb.run(reports, clean=True, rules=rules_fn,
           delete_placed_from=[tosort], converted_games=done)
    # KONIEC: kasujemy źródła po całości
    from chd_buddy.core.convert import purge_source_files
    purge_source_files(to_purge, index=idx)
    assert zip_path.is_file()                        # finał ZOSTAJE w docelowym
    assert not (target / "Gra (USA).nes").exists()   # brak luźnego w docelowym
    assert not (tosort / "Nintendo - Nintendo Entertainment System"
                / "Gra (USA).zip").exists()          # nie wrzucony do ToSort
    assert not (tosort / "Gra (USA).nes").exists()   # źródło skasowane na końcu
    with zipfile.ZipFile(zip_path) as z:
        assert z.read("Gra (USA).nes") == data


def test_rebuild_link_logic_without_privilege(world, monkeypatch):
    """Weryfikuje hierarchię kopia fizyczna → linki bez uprawnień do symlinków.

    Podstawiamy create_link/is_link w module rebuildera: 'link' to pusty plik
    zapamiętany w zbiorze. Sprawdzamy claimy i idempotencję niezależnie od
    trybu dewelopera.
    """
    import chd_buddy.core.rebuilder as rb_mod
    fake_links: dict[str, str] = {}

    def fake_create_link(link_path, target, is_dir):
        Path(link_path).parent.mkdir(parents=True, exist_ok=True)
        Path(link_path).touch()
        fake_links[os.path.normcase(str(link_path))] = str(target)

    monkeypatch.setattr(rb_mod, "create_link", fake_create_link)
    monkeypatch.setattr(
        rb_mod, "is_link",
        lambda p: os.path.normcase(str(p)) in fake_links)

    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    st = Rebuilder(idx, dry_run=False).run(match_store(entries, idx))
    assert st.moved == 2 and st.linked == 1 and st.errors == 0

    a = world["rom_root"] / "PS2" / "Zestaw A" / "Gra Wspólna.iso"
    b = world["rom_root"] / "PS2" / "Zestaw B" / "Gra Wspólna.iso"
    assert a.read_bytes() == world["shared"]           # kopia fizyczna w A
    assert fake_links[os.path.normcase(str(b))] == str(a)  # B linkuje do A

    # drugi przebieg: nic do zrobienia
    st2 = Rebuilder(idx, dry_run=False).run(match_store(entries, idx))
    assert st2.moved == 0 and st2.linked == 0 and st2.errors == 0


def test_rebuild_unpacks_from_zip_with_verification(world):
    """Gra w ZIP-ie (jak zrzuty Redump): dopasowanie po CRC z centralnego
    katalogu, wypakowanie z weryfikacją SHA-1, archiwum nietknięte."""
    import zipfile
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    # usuń luźne źródła; zostaw grę A tylko w archiwum + śmieć w tym samym zipie
    (world["src"] / "gra_a.iso").unlink()
    (world["src"] / "wspolna_zla_nazwa.iso").unlink()
    zpath = world["src"] / "paczka.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Gra A.iso", world["only_a"])
        zf.writestr("readme.txt", "nie gra")
    idx.scan(world["src"])

    reports = match_store(entries, idx)
    s = [s for r in reports for s in r.statuses if s.rom.name == "Gra A.iso"][0]
    assert s.state == RomState.ELSEWHERE
    assert s.member == "Gra A.iso" and s.source_path == str(zpath)

    rb = Rebuilder(idx, dry_run=False)
    st = rb.run(reports)
    assert st.unpacked == 1 and st.errors == 0
    out = world["rom_root"] / "PS2" / "Zestaw A" / "Gra A.iso"
    assert out.read_bytes() == world["only_a"]
    assert zpath.exists()                       # archiwum zostaje
    # wypakowany plik jest w indeksie => drugi przebieg nic nie robi
    reports2 = match_store(entries, idx)
    st2 = Rebuilder(idx, dry_run=False).run(reports2)
    assert st2.unpacked == 0 and st2.already_ok >= 1


def test_rebuild_unpacks_from_7z(world):
    """Gra w archiwum 7z: indeks z nagłówka (py7zr), wypakowanie z weryfikacją."""
    py7zr = pytest.importorskip("py7zr")
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    (world["src"] / "gra_a.iso").unlink()
    (world["src"] / "wspolna_zla_nazwa.iso").unlink()
    zpath = world["src"] / "paczka.7z"
    with py7zr.SevenZipFile(zpath, "w") as zf:
        zf.writestr(world["only_a"], "Gra A.iso")
    idx.scan(world["src"])

    reports = match_store(entries, idx)
    s = [s for r in reports for s in r.statuses if s.rom.name == "Gra A.iso"][0]
    assert s.state == RomState.ELSEWHERE
    assert s.member == "Gra A.iso" and s.source_path == str(zpath)

    st = Rebuilder(idx, dry_run=False).run(reports)
    assert st.unpacked == 1 and st.errors == 0
    out = world["rom_root"] / "PS2" / "Zestaw A" / "Gra A.iso"
    assert out.read_bytes() == world["only_a"]
    assert zpath.exists()


def test_rebuild_zip_verification_rejects_bad_content(world):
    """Zła zawartość (CRC sfałszowany na zgodny, ale SHA-1 inny) NIE jest
    dopasowana — bo NOWE archiwa skanujemy PEŁNIE (SHA-1 zawartości), więc
    kłamstwo w CRC nie przechodzi. Gra = brak, nic nie ląduje."""
    import zipfile
    import zlib
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    for f in list(world["src"].iterdir()):
        f.unlink()
    zpath = world["src"] / "podrobka.zip"
    fake = b"INNE DANE" * 64
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("Gra A.iso", fake)
    idx.scan(world["src"])                 # pełny skan członków → realny SHA-1
    # sfałszuj CRC+rozmiar na zgodne z DAT-em — ale SHA-1 (policzony) zdradza
    good = world["only_a"]
    idx._db.execute(
        "UPDATE members SET crc32=?, size=? WHERE archive=?",
        (f"{zlib.crc32(good) & 0xFFFFFFFF:08x}", len(good), str(zpath)))
    idx._db.commit()

    reports = match_store(entries, idx)
    s = [s for r in reports for s in r.statuses if s.rom.name == "Gra A.iso"][0]
    assert s.state == RomState.MISSING     # SHA-1 nie pasuje → NIE dopasowane
    st = Rebuilder(idx, dry_run=False).run(reports)
    assert not (world["rom_root"] / "PS2" / "Zestaw A" / "Gra A.iso").exists()


def test_no_copy_when_symlink_impossible(world, monkeypatch):
    """ZASADA: gdy symlinku NIE DA SIĘ utworzyć — NIC nie powstaje.
    Żadnych kopii (duplikaty zapychały dysk); miejsce zostaje puste."""
    import chd_buddy.core.rebuilder as rb_mod
    from chd_buddy.core.linker import LinkPrivilegeError

    def deny(*_a, **_k):
        raise LinkPrivilegeError("brak uprawnień (test)")

    monkeypatch.setattr(rb_mod, "create_link", deny)
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    st = Rebuilder(idx, dry_run=False).run(match_store(entries, idx))
    assert st.errors == 0 and st.linked == 0
    assert st.copied == 0                      # NIC nie skopiowane
    assert st.links_skipped == 1               # pominięty symlink policzony
    b = world["rom_root"] / "PS2" / "Zestaw B" / "Gra Wspólna.iso"
    assert not b.exists()                      # miejsce po prostu PUSTE


def test_make_links_false_skips_links_entirely(world):
    """Wyłączona opcja symlinków: nie próbujemy linkować i nic nie kopiujemy."""
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    st = Rebuilder(idx, dry_run=False, make_links=False).run(
        match_store(entries, idx))
    assert st.linked == 0 and st.copied == 0 and st.links_skipped >= 1
    assert not (world["rom_root"] / "PS2" / "Zestaw B" / "Gra Wspólna.iso").exists()


def test_subdir_per_game_layout_and_rule(tmp_path: Path):
    """Gra wieloplikowa luzem => podkatalog per gra (domyślnie);
    reguła subdir_per_game=false wraca do układu płaskiego;
    gra jednoplikowa i CHD zawsze płasko."""
    import json as _json
    from chd_buddy.core.dirrules import DirRules, apply_rule_targets
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    t1, t2 = b"T1" * 60, b"T2" * 60
    solo = b"SOLO" * 60
    _write_dat(dat_root / "s.dat", "Saturn",
               {"Gra CD (Europe)": {"Gra CD (Europe) (Track 1).bin": t1,
                                    "Gra CD (Europe) (Track 2).bin": t2},
                "Solo (USA)": {"Solo (USA).iso": solo}})
    src = tmp_path / "zrzuty"
    src.mkdir()
    (src / "a.bin").write_bytes(t1)
    (src / "b.bin").write_bytes(t2)
    (src / "c.iso").write_bytes(solo)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(src)

    entries = DatStore(dat_root, rom_root).discover()
    st = Rebuilder(idx, dry_run=False).run(match_store(entries, idx))
    assert st.moved == 3 and st.errors == 0
    d = rom_root / "Saturn"
    assert (d / "Gra CD (Europe)" / "Gra CD (Europe) (Track 1).bin").exists()
    assert (d / "Gra CD (Europe)" / "Gra CD (Europe) (Track 2).bin").exists()
    assert (d / "Solo (USA).iso").exists()          # jednoplikowa płasko
    # drugi przebieg: wszystko na miejscu
    st2 = Rebuilder(idx, dry_run=False).run(match_store(entries, idx))
    assert st2.moved == 0 and st2.renamed == 0 and st2.already_ok == 3

    # reguła subdir_per_game=false => powrót do płaskiego (rename do góry)
    (dat_root / "_reguly.json").write_text(
        _json.dumps({"Saturn": {"subdir_per_game": False}}), encoding="utf-8")
    entries2 = DatStore(dat_root, rom_root).discover()
    rules = DirRules(dat_root)
    apply_rule_targets(entries2, rules, rom_root)
    st3 = Rebuilder(idx, dry_run=False).run(match_store(entries2, idx),
                                            rules=rules.for_entry)
    assert st3.renamed == 2
    assert (d / "Gra CD (Europe) (Track 1).bin").exists()
    assert not (d / "Gra CD (Europe)").exists()     # pusty katalog sprzątnięty


def test_rebuild_only_complete_games(world, tmp_path: Path):
    """Gra z brakującym plikiem nie jest budowana (domyślnie), chyba że
    only_complete=False — jak „Only keep complete sets" w RomVaulcie."""
    dat_root = tmp_path / "dats2"
    rom_root = tmp_path / "roms2"
    have = b"MAM-TEN-PLIK" * 50
    missing = b"TEGO-NIE-MAM" * 50
    _write_dat(dat_root / "x.dat", "Zestaw X",
               {"Gra Dwuplikowa": {"Track 01.bin": have,
                                   "Track 02.bin": missing}})
    src = tmp_path / "zrzuty2"
    src.mkdir()
    (src / "track1.bin").write_bytes(have)
    idx = FileIndex(tmp_path / "idx2.sqlite3")
    idx.scan(src)

    entries = DatStore(dat_root, rom_root).discover()
    st = Rebuilder(idx, dry_run=False).run(match_store(entries, idx))
    assert st.moved == 0 and st.incomplete == 1
    assert not rom_root.exists()

    st2 = Rebuilder(idx, dry_run=False).run(match_store(entries, idx),
                                            only_complete=False)
    assert st2.moved == 1
    # gra wieloplikowa luzem => podkatalog per gra
    assert (rom_root / "Zestaw X" / "Gra Dwuplikowa" / "Track 01.bin").exists()


def test_matcher_chd_content_hit(world, tmp_path: Path):
    """Plik .chd trafia w DAT przez SHA-1 zawartości (data_sha1)."""
    idx = world["idx"]
    # usuń źródłowe pliki, zostaw "CHD" z zawartością Gry A w data_sha1
    for f in list(world["src"].iterdir()):
        f.unlink()
    chd = world["src"] / "gra_a.chd"
    chd.write_bytes(b"fake-chd-container")
    content_sha1 = hashlib.sha1(world["only_a"]).hexdigest()
    idx.scan(world["src"], chd_prober=lambda p: content_sha1)

    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    reports = match_store(entries, idx)
    s = [s for r in reports for s in r.statuses if s.rom.name == "Gra A.iso"][0]
    assert s.state == RomState.ELSEWHERE and s.via_chd
    assert s.canonical_path.name == "Gra A.chd"


def test_game_stats_counts_games_not_shared_roms(tmp_path: Path):
    """Współdzielona ścieżka gry, której NIE mam, nie może zawyżać
    'do naprawy' — statystyki liczą całe gry, nie pojedyncze ROM-y."""
    from chd_buddy.core.matcher import match_entry
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    shared = b"AUDIO-CISZA" * 40
    # mam grę A w całości; gry B nie mam, ale dzieli ścieżkę audio z A
    _write_dat(dat_root / "dc.dat", "Dreamcast",
               {"Gra A (USA)": {"Gra A (USA) (Track 1).bin": b"DANE-A" * 40,
                                "Gra A (USA) (Track 2).bin": shared},
                "Gra B (USA)": {"Gra B (USA) (Track 1).bin": b"DANE-B" * 40,
                                "Gra B (USA) (Track 2).bin": shared}})
    src = tmp_path / "roms" / "Dreamcast" / "Gra A (USA)"
    src.mkdir(parents=True)
    (src / "Gra A (USA) (Track 1).bin").write_bytes(b"DANE-A" * 40)
    (src / "Gra A (USA) (Track 2).bin").write_bytes(shared)
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)

    entries = DatStore(dat_root, rom_root).discover()
    rep = match_entry(entries[0], idx)
    total, complete, fix, miss = rep.game_stats()
    assert total == 2
    assert complete == 1        # Gra A na miejscu
    assert fix == 0             # Gra B NIE liczy się jako "do naprawy"…
    assert miss == 1            # …tylko jako brak (nie mam Track 1)


def test_save_priority_and_settings_roundtrip(tmp_path: Path):
    """Zapis hierarchii (_priorytet.txt) i reguł per DAT (_reguly.json)."""
    from chd_buddy.core.datstore import save_priority, group_by_platform
    from chd_buddy.core.dirrules import DirRules, save_rule
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    _write_dat(dat_root / "full.dat", "Sony - PlayStation 2 - Datfile",
               {"A": {"a.iso": b"x" * 40}, "B": {"b.iso": b"y" * 40}})
    _write_dat(dat_root / "g1r.dat", "Sony - PlayStation 2 (Redump - 1G1R)",
               {"A": {"a.iso": b"x" * 40}})
    entries = DatStore(dat_root, rom_root).discover()

    groups = group_by_platform(entries)
    assert len(groups) == 1                       # obie na jednej platformie
    ps2 = next(iter(groups.values()))
    assert ps2[0].rom_count > ps2[1].rom_count    # rodzic = większy

    # ustaw mniejszy jako rodzic ręcznie
    save_priority(dat_root, [ps2[1].name, ps2[0].name])
    entries2 = DatStore(dat_root, rom_root).discover()
    assert entries2[0].name == ps2[1].name        # ręczny priorytet działa

    # reguła per DAT
    p = save_rule(dat_root, "Sony - PlayStation 2 - Datfile",
                  {"format": "chd", "subdir_per_game": False})
    eff = DirRules(dat_root).for_entry(entries2[-1])  # pełny (Datfile)
    assert eff["format"] == "chd" and eff["subdir_per_game"] is False
    # domyślne wartości nie zaśmiecają pliku
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "only_complete" not in data["Sony - PlayStation 2 - Datfile"]


def test_suggest_format_disc_vs_cartridge():
    from chd_buddy.core.dirrules import suggest_format
    assert suggest_format("PS2") == "chd"
    assert suggest_format("SATURN") == "chd"
    assert suggest_format("GCN") == "rvz"        # GameCube → RVZ
    assert suggest_format("WII") == "rvz"
    assert suggest_format("SNES") == "keep"      # kartridż


def test_platform_key_groups_variants():
    """Warianty tej samej platformy dają ten sam klucz; różne platformy nie."""
    from chd_buddy.core.datstore import platform_key
    ps2 = {
        platform_key("Sony - PlayStation 2 - Datfile (11719)"),
        platform_key("Sony - PlayStation 2 (Redump - Fresh1G1R - PropeR)"),
        platform_key("Sony - PlayStation 2 (Retool)"),
    }
    assert len(ps2) == 1
    assert platform_key("Sega - Saturn - Datfile (2397)") != next(iter(ps2))
    assert platform_key("Nintendo - Nintendo 64 (Retool)") == "nintendo nintendo 64"


def test_priority_per_platform_not_global(tmp_path: Path):
    """Priorytet parent→child liczony PER PLATFORMA: w obrębie PS2 większy
    jest rodzicem; wielki DAT Saturna NIE jest rodzicem małego PS2."""
    from chd_buddy.core.datstore import platform_key
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    # PS2: pełny (2 gry) + 1G1R (1 gra); Saturn: wielki (5 gier)
    _write_dat(dat_root / "ROMS" / "ps2full.dat", "Sony - PlayStation 2 - Datfile",
               {f"PS2 Gra {i}": {f"g{i}.iso": bytes([i]) * 40} for i in range(2)})
    _write_dat(dat_root / "1G1R" / "ps2.dat",
               "Sony - PlayStation 2 (Redump - Fresh1G1R)",
               {"PS2 Gra 0": {"g0.iso": bytes([0]) * 40}})
    _write_dat(dat_root / "ROMS" / "saturn.dat", "Sega - Saturn - Datfile",
               {f"Sat Gra {i}": {f"s{i}.iso": bytes([100 + i]) * 40}
                for i in range(5)})

    entries = DatStore(dat_root, rom_root).discover()
    # w obrębie PS2: pełny (2) przed 1G1R (1)
    ps2 = [e for e in entries if platform_key(e.name) == "sony playstation 2"]
    assert [e.rom_count for e in ps2] == [2, 1]
    assert "Datfile" in ps2[0].name           # rodzic = pełny
    # Saturn (5 gier) nie wpycha się między warianty PS2
    ps2_idx = [i for i, e in enumerate(entries)
               if platform_key(e.name) == "sony playstation 2"]
    assert ps2_idx == [ps2_idx[0], ps2_idx[0] + 1]  # PS2 obok siebie


def test_dat_priority_bigger_is_parent(tmp_path: Path):
    """W obrębie platformy większy DAT (pełna biblioteka) jest rodzicem;
    _priorytet.txt pozwala ręcznie odwrócić hierarchię tej platformy."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    a = b"WSPOLNY-PLIK" * 30
    # obie warianty TEJ SAMEJ platformy (Sony - PlayStation 2)
    _write_dat(dat_root / "maly.dat", "Sony - PlayStation 2 (Redump - Fresh1G1R)",
               {"Gra": {"Gra.iso": a}})
    _write_dat(dat_root / "duzy.dat", "Sony - PlayStation 2 - Datfile",
               {"Gra": {"Gra.iso": a}, "Inna": {"Inna.iso": b"X" * 40}})
    entries = DatStore(dat_root, rom_root).discover()
    assert [e.name for e in entries] == [
        "Sony - PlayStation 2 - Datfile",
        "Sony - PlayStation 2 (Redump - Fresh1G1R)"]        # pełny = rodzic

    (dat_root / "_priorytet.txt").write_text(
        "# rodzic:\nSony - PlayStation 2 (Redump - Fresh1G1R)\n",
        encoding="utf-8")
    entries2 = DatStore(dat_root, rom_root).discover()
    assert [e.name for e in entries2] == [
        "Sony - PlayStation 2 (Redump - Fresh1G1R)",        # ręcznie na rodzica
        "Sony - PlayStation 2 - Datfile"]


def test_chd_satisfies_whole_multitrack_game(tmp_path: Path):
    """CHD zaspokaja CAŁĄ grę CD (wiele binów + cue): jeden plik <gra>.chd,
    jedna operacja przeniesienia, rodzeństwo liczone jako na miejscu."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    t1, t2 = b"TRACK-1" * 50, b"TRACK-2" * 50
    cue = b"FILE bin"
    _write_dat(dat_root / "sat.dat", "Saturn",
               {"Gra CD (Europe)": {"Gra CD (Europe) (Track 1).bin": t1,
                                    "Gra CD (Europe) (Track 2).bin": t2,
                                    "Gra CD (Europe).cue": cue}})
    src = tmp_path / "zrzuty"
    src.mkdir()
    (src / "jakis_chd.chd").write_bytes(b"chd-container")
    idx = FileIndex(tmp_path / "idx.sqlite3")
    # głęboka identyfikacja podkłada ODCISK KOMPLETU ścieżek (nie pojedynczą
    # sumę — wydania 1S/5S dzielą ścieżkę danych i różnią się tylko audio)
    profile = hashlib.sha1(",".join(
        [hashlib.sha1(t1).hexdigest(), hashlib.sha1(t2).hexdigest()]
    ).encode("ascii")).hexdigest()
    idx.scan(src, chd_prober=lambda p: profile)

    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)
    sts = reports[0].statuses
    assert all(s.via_chd and s.state == RomState.ELSEWHERE for s in sts)
    assert {str(s.canonical_path) for s in sts} == {
        str(rom_root / "Saturn" / "Gra CD (Europe).chd")}

    st = Rebuilder(idx, dry_run=False).run(reports)
    assert st.moved == 1 and st.errors == 0 and st.conflicts == 0
    assert (rom_root / "Saturn" / "Gra CD (Europe).chd").read_bytes() == \
        b"chd-container"
    # drugi przebieg: wszystko na miejscu
    st2 = Rebuilder(idx, dry_run=False).run(match_store(entries, idx))
    assert st2.moved == 0 and st2.errors == 0
    assert st2.already_ok == 3   # trzy ROM-y gry wskazują ten sam CHD


def test_missing_cue_does_not_block_game(tmp_path: Path):
    """DAT wymaga .cue, ale w źródle go nie ma — gra i tak jest budowana
    (cue bywa generowany przy ekstrakcji z CHD)."""
    dat_root = tmp_path / "dats"
    rom_root = tmp_path / "roms"
    data = b"BIN-DATA" * 40
    _write_dat(dat_root / "x.dat", "Zestaw",
               {"Gra": {"Gra (Track 1).bin": data, "Gra.cue": b"FILE"}})
    src = tmp_path / "zrzuty"
    src.mkdir()
    (src / "cos.bin").write_bytes(data)     # jest bin, nie ma cue
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(src)

    entries = DatStore(dat_root, rom_root).discover()
    st = Rebuilder(idx, dry_run=False).run(match_store(entries, idx))
    assert st.moved == 1 and st.incomplete == 0
    assert (rom_root / "Zestaw" / "Gra" / "Gra (Track 1).bin").exists()


def test_scan_cancel_keeps_progress_and_no_missing(tmp_path, monkeypatch):
    """Przerwany skan: to co policzone ZOSTAJE w bazie, a plików NIE
    oznaczamy jako brakujących (nie obeszliśmy całego drzewa)."""
    import threading
    from chd_buddy.core.fileindex import FileIndex
    d = tmp_path / "roms"
    d.mkdir()
    for i in range(8):
        (d / f"g{i}.bin").write_bytes(bytes([i]) * 4096)
    idx = FileIndex(tmp_path / "i.sqlite3")
    idx.scan(d)                                  # pełny skan bazowy
    n_all = len(idx.all_under(d))
    assert n_all == 8

    cancel = threading.Event()
    seen = {"n": 0}

    def on_file(n, p):
        seen["n"] = n
        if n >= 3:
            cancel.set()                         # przerwij w połowie

    st = idx.scan(d, full=True, on_file=on_file, cancel=cancel)
    assert st.cancelled is True
    assert st.missing == 0                       # nic nie oznaczone jako brak
    assert len(idx.all_under(d)) == 8            # wpisy zachowane
    idx.close()


def test_rebuild_cancel_skips_clean_and_dedup(world):
    """Przerwana naprawa NIE sprząta do ToSort i NIE dedupuje — inaczej
    nieprzetworzone jeszcze pliki zostałyby uznane za nieznane."""
    import threading
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    reports = match_store(entries, idx)
    cancel = threading.Event()
    cancel.set()                                 # przerwij od razu
    rb = Rebuilder(idx, tosort=world["src"], dry_run=False)
    st = rb.run(reports, clean=True, dedup_roots=[world["rom_root"]],
                delete_placed_from=[world["src"]], cancel=cancel)
    assert rb.cancelled is True
    assert st.tosorted == 0 and st.deduped == 0 and st.tosort_deleted == 0


def test_empty_marker_rom_created_and_matched(tmp_path: Path):
    """Gra MSU-1: obok .sfc jest pusty plik-znacznik .msu (size=0, crc="-",
    bez sum). Matcher: MISSING gdy go brak, HAVE gdy leży (0 bajtów). Rebuilder
    TWORZY pusty plik → gra staje się kompletna (bez tego DAT MSU-1 nigdy nie
    dawał się odtworzyć)."""
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    src = tmp_path / "src"; src.mkdir()
    sfc = b"SNES-ROM-DANE" * 100
    # DAT ręcznie — pusty .msu ma size=0 i crc="-" (BEZ md5/sha1)
    (dat_root / "msu").mkdir(parents=True)
    (dat_root / "msu" / "msu.dat").write_text(
        '<?xml version="1.0"?><datafile><header><name>msu</name></header>'
        '<game name="gra (usa) (msu1)">'
        f'<rom name="gra (usa) (msu1).sfc" {_rom_attrs(sfc)}/>'
        '<rom name="gra (usa) (msu1).msu" size="0" crc="-"/>'
        '</game></datafile>', encoding="utf-8")
    # źródło: tylko .sfc (pod właściwą nazwą, w podkatalogu gry docelowej)
    gdir = rom_root / "msu" / "gra (usa) (msu1)"
    gdir.mkdir(parents=True)
    (gdir / "gra (usa) (msu1).sfc").write_bytes(sfc)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)
    g = reports[0]
    # PRZED: .msu = CREATABLE (pusty marker do utworzenia, NIE „brak"),
    # .sfc obecny (gdzieś)
    by = {s.rom.name: s.state for s in g.statuses}
    assert by["gra (usa) (msu1).msu"] == RomState.CREATABLE
    assert by["gra (usa) (msu1).sfc"] != RomState.MISSING
    # gra NIE liczy się jako „brak" — pusty marker to „do naprawy"
    _tot, _comp, _fix, _miss = g.game_stats()
    assert _miss == 0 and _fix >= 1

    rb = Rebuilder(idx, tosort=None, dry_run=False)
    st = rb.run(reports)
    assert st.created == 1
    # pusty plik-znacznik powstał w ścieżce KANONICZNEJ .msu
    msu = g.statuses[[s.rom.name for s in g.statuses].index(
        "gra (usa) (msu1).msu")].canonical_path
    assert Path(msu).is_file() and Path(msu).stat().st_size == 0

    # PO: przeskanuj i dopasuj ponownie — gra KOMPLETNA (wszystko HAVE)
    idx.scan(rom_root)
    reports2 = match_store(DatStore(dat_root, rom_root).discover(), idx)
    by2 = {s.rom.name: s.state for s in reports2[0].statuses}
    assert by2["gra (usa) (msu1).msu"] == RomState.HAVE
    assert all(s.state == RomState.HAVE for s in reports2[0].statuses)


def test_empty_marker_dry_run_creates_nothing(tmp_path: Path):
    """Podgląd (dry_run) NIE tworzy pustego pliku-znacznika."""
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    (dat_root / "msu").mkdir(parents=True)
    sfc = b"X" * 50
    (dat_root / "msu" / "msu.dat").write_text(
        '<?xml version="1.0"?><datafile><header><name>msu</name></header>'
        '<game name="g (msu1)">'
        f'<rom name="g (msu1).sfc" {_rom_attrs(sfc)}/>'
        '<rom name="g (msu1).msu" size="0" crc="-"/>'
        '</game></datafile>', encoding="utf-8")
    gdir = rom_root / "msu" / "g (msu1)"; gdir.mkdir(parents=True)
    (gdir / "g (msu1).sfc").write_bytes(sfc)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)
    reports = match_store(DatStore(dat_root, rom_root).discover(), idx)
    rb = Rebuilder(idx, tosort=None, dry_run=True)
    st = rb.run(reports)
    assert st.created == 1                       # policzone w podglądzie
    assert not (gdir / "g (msu1).msu").exists()  # ale plik NIE powstał


def test_claim_key_uses_all_dat_sums():
    """Dedup łączy dwa ROM-y w jedną kopię fizyczną (link) TYLKO, gdy zgadzają
    się WSZYSTKIE sumy z DAT (size+CRC+MD5+SHA-1). Sam zgodny SHA-1 przy innym
    CRC/rozmiarze → RÓŻNE klucze (osobne kopie), nie błędny link."""
    from chd_buddy.core.datfile import DatRom
    from chd_buddy.core.matcher import RomStatus, RomState
    from chd_buddy.core.rebuilder import _claim_key

    def st(name, size, crc, md5, sha1):
        return RomStatus(None, "g", DatRom(name, size, crc, md5, sha1),
                         RomState.ELSEWHERE)

    a = st("a.pcm", 100, "aabbccdd", "d0"*16, "11"*20)
    b = st("b.pcm", 100, "aabbccdd", "d0"*16, "11"*20)   # WSZYSTKO równe
    assert _claim_key(a) == _claim_key(b)                # → jedna kopia + link

    # ten sam SHA-1, ale INNY rozmiar → różne klucze (osobne kopie)
    c = st("c.pcm", 999, "aabbccdd", "d0"*16, "11"*20)
    assert _claim_key(a) != _claim_key(c)
    # ten sam SHA-1, ale INNY CRC → różne klucze
    d = st("d.pcm", 100, "99999999", "d0"*16, "11"*20)
    assert _claim_key(a) != _claim_key(d)
    # ten sam SHA-1, ale INNY MD5 → różne klucze
    e = st("e.pcm", 100, "aabbccdd", "ff"*16, "11"*20)
    assert _claim_key(a) != _claim_key(e)
    # klucz zawiera wszystkie sumy
    assert "size=100" in _claim_key(a) and "crc=aabbccdd" in _claim_key(a)
    assert "md5=" in _claim_key(a) and "sha1=" in _claim_key(a)


def test_intra_dat_duplicate_is_copy_not_symlink(tmp_path: Path):
    """PARENT bez symlinków: dwie gry w JEDNYM DAT-cie o identycznej treści →
    OBIE dostają fizyczną kopię (dedup wewnątrz kolekcji NIE linkuje). Symlink
    zostaje tylko dziecko→rodzic (inny katalog docelowy — patrz world)."""
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    tosort = tmp_path / "ts"; tosort.mkdir()
    shared = b"WSPOLNA-TRESC-DWOCH-GIER" * 40
    _write_dat(dat_root / "S" / "s.dat", "Zestaw S",
               {"Gra One": {"Gra One.rom": shared},
                "Gra Two": {"Gra Two.rom": shared}})
    (tosort / "gra_one.rom").write_bytes(shared)
    (tosort / "gra_two.rom").write_bytes(shared)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)
    rb = Rebuilder(idx, tosort=tosort, dry_run=False)
    st = rb.run(reports)

    d = rom_root / "S" / "Zestaw S"
    one = d / "Gra One.rom"; two = d / "Gra Two.rom"
    assert one.is_file() and two.is_file()
    assert not is_link(one) and not is_link(two)     # OBIE fizyczne, żaden link
    assert one.read_bytes() == shared and two.read_bytes() == shared
    assert st.linked == 0                            # zero symlinków w parent


def test_intra_dat_dedup_phase_keeps_physical(tmp_path: Path):
    """Faza dedupu też NIE zamienia kopii na symlinki WEWNĄTRZ jednej kolekcji
    (parent): dwie identyczne kopie w tym samym DAT-cie zostają fizyczne."""
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    shared = b"IDENTYCZNA" * 100
    _write_dat(dat_root / "S" / "s.dat", "Zestaw S",
               {"A": {"A.rom": shared}, "B": {"B.rom": shared}})
    # obie już fizycznie na miejscu (dwie kopie)
    d = rom_root / "S" / "Zestaw S"; d.mkdir(parents=True)
    (d / "A.rom").write_bytes(shared)
    (d / "B.rom").write_bytes(shared)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)
    rb = Rebuilder(idx, dry_run=False)
    st = rb.run(reports, dedup_roots=[rom_root])
    assert not is_link(d / "A.rom") and not is_link(d / "B.rom")  # obie fizyczne
    assert st.deduped == 0                           # dedup nic nie polinkował


def test_purge_redundant_tosort_archives_any_format(tmp_path: Path):
    """Zbędne ARCHIWUM w ToSort (cała zawartość już w kolekcji) kasowane
    NIEZALEŻNIE od formatu (gra luźna, nie CHD). Śmieci .srm nie blokują;
    archiwum z UNIKALNYM plikiem zostaje; nieznana zawartość (brak członków
    w indeksie) nietykalna."""
    import zipfile
    from chd_buddy.core.rebuilder import Rebuilder
    rom_root = tmp_path / "roms"; tosort = tmp_path / "ts"; tosort.mkdir()
    a = b"SFC-DANE" * 200; b = b"PCM-DANE" * 300; c = b"UNIKAT" * 100
    # target: a i b luźno (już w kolekcji)
    tdir = rom_root / "snes-msu1" / "Gra (MSU1)"; tdir.mkdir(parents=True)
    (tdir / "Gra (MSU1).sfc").write_bytes(a)
    (tdir / "Gra (MSU1)-1.pcm").write_bytes(b)
    # ToSort: ZIP redundantny (a+b + śmieć .srm + pusty .msu) i ZIP z unikatem
    zdup = tosort / "Gra (MSU1).zip"
    with zipfile.ZipFile(zdup, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Gra (MSU1).sfc", a)
        z.writestr("Gra (MSU1)-1.pcm", b)
        z.writestr("Gra (MSU1).srm", b"SAVE-USERA")   # śmieć spoza DAT
        z.writestr("Gra (MSU1).msu", b"")             # pusty marker
    zuniq = tosort / "Inna (MSU1).zip"
    with zipfile.ZipFile(zuniq, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Inna (MSU1).sfc", c)              # NIE ma w target
        z.writestr("Inna (MSU1)-1.pcm", b)            # to jest w target
    idx = FileIndex(tmp_path / "idx.sqlite3")
    idx.scan(rom_root)
    idx.scan(tosort, full=True)                       # członkowie z SHA-1

    rb = Rebuilder(idx, dry_run=False)
    rb._purge_redundant_tosort_archives([tosort])
    assert not zdup.exists()          # cała zawartość ROM w target → skasowany
    assert zuniq.exists()             # ma unikat (.sfc) → został
    assert idx.lookup(zdup) is None   # znikł też z indeksu
    assert rb.stats.tosort_deleted == 1


def test_purge_redundant_tosort_archives_dry_run(tmp_path: Path):
    """dry_run: liczy, ale NIE kasuje archiwum."""
    import zipfile
    from chd_buddy.core.rebuilder import Rebuilder
    rom_root = tmp_path / "roms"; tosort = tmp_path / "ts"; tosort.mkdir()
    a = b"X" * 500
    tdir = rom_root / "sys" / "G"; tdir.mkdir(parents=True)
    (tdir / "G.rom").write_bytes(a)
    z = tosort / "G.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zz:
        zz.writestr("G.rom", a)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root); idx.scan(tosort, full=True)
    rb = Rebuilder(idx, dry_run=True)
    rb._purge_redundant_tosort_archives([tosort])
    assert z.exists()                 # dry_run nic nie kasuje
    assert rb.stats.tosort_deleted == 1


def test_repair_normalizes_zip_compression_to_deflate(tmp_path: Path):
    """Zmiana ustawienia „metoda ZIP = deflate" egzekwowana przy naprawie:
    docelowy ZIP z inną kompresją (BZIP2/ZSTD) jest przepakowany na DEFLATE.
    ZIP już-deflate — nietknięty."""
    import zipfile
    from chd_buddy.core.convert import zip_needs_repack
    rom_root = tmp_path / "roms"
    d = rom_root / "n64"; d.mkdir(parents=True)
    a = b"ROM-N64" * 3000
    with zipfile.ZipFile(d / "Gra (USA).zip", "w", zipfile.ZIP_BZIP2) as z:
        z.writestr("Gra (USA).z64", a)          # inna metoda (BZIP2)
    with zipfile.ZipFile(d / "OK (USA).zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("OK (USA).z64", b"X" * 100)  # już deflate
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)

    rb = Rebuilder(idx, dry_run=False, zip_method="deflate")
    rb._normalize_target_zip_compression([d / "Gra (USA).zip",
                                          d / "OK (USA).zip"])
    assert rb.stats.repacked == 1
    assert not zip_needs_repack(d / "Gra (USA).zip", "deflate")   # naprawiony
    assert not zip_needs_repack(d / "OK (USA).zip", "deflate")    # był OK
    with zipfile.ZipFile(d / "Gra (USA).zip") as z:
        assert z.read("Gra (USA).z64") == a                        # treść OK


def test_normalize_skips_incomplete_game_zip(tmp_path: Path):
    """Bezpiecznik: normalizacja/repack NIE rusza zipa gry NIEKOMPLETNEJ
    (np. arcade parent bez własnych ROM-ów). `_complete_zip_canonicals`
    zwraca tylko kanoniczne gier w pełni obecnych."""
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    good = b"KOMPLET" * 100
    _write_dat(dat_root / "sys" / "s.dat", "Sys",
               {"Pelna": {"Pelna.rom": good},
                "Braki": {"Braki-a.rom": b"A" * 50, "Braki-b.rom": b"B" * 50}})
    d = rom_root / "sys" / "Sys"; d.mkdir(parents=True)
    # Pelna: kompletna (zip w docelowym); Braki: tylko 1 z 2 plików
    import zipfile
    with zipfile.ZipFile(d / "Pelna.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Pelna.rom", good)
    (d / "Braki").mkdir()
    (d / "Braki" / "Braki-a.rom").write_bytes(b"A" * 50)   # brak Braki-b
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)
    rb = Rebuilder(idx, dry_run=False, zip_method="deflate")
    canon = rb._complete_zip_canonicals(reports)
    names = {Path(p).name for p in canon}
    assert "Pelna.zip" in names           # kompletna → kwalifikuje się
    assert not any("Braki" in n for n in names)   # niekompletna → pominięta


def test_repair_normalizes_zip_compression_to_zstd_if_available(tmp_path: Path):
    """Symetrycznie: gdy user wybierze „metoda ZIP = zstd", naprawa przepakowuje
    docelowe DEFLATE- y na ZSTD (wszystkie zmiany DAT odzwierciedlane)."""
    import zipfile
    if getattr(zipfile, "ZIP_ZSTANDARD", None) is None:
        pytest.skip("Python bez ZIP_ZSTANDARD (<3.14)")
    from chd_buddy.core.convert import zip_needs_repack
    rom_root = tmp_path / "roms"; d = rom_root / "n64"; d.mkdir(parents=True)
    a = b"ROM" * 5000
    with zipfile.ZipFile(d / "G.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("G.z64", a)                  # deflate → ma zostać zstd
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)
    rb = Rebuilder(idx, dry_run=False, zip_method="zstd")
    rb._normalize_target_zip_compression([d / "G.zip"])
    assert rb.stats.repacked == 1
    assert not zip_needs_repack(d / "G.zip", "zstd")
    with zipfile.ZipFile(d / "G.zip") as z:
        assert z.infolist()[0].compress_type == zipfile.ZIP_ZSTANDARD
        assert z.read("G.z64") == a


def test_match_cache_same_results_and_faster(world):
    """Cache dopasowania w pamięci daje IDENTYCZNE stany co ścieżka SQL
    (find_sha1/find_crc/find_md5/find_member_*), tylko bez zapytań na grę."""
    idx = world["idx"]
    entries = DatStore(world["dat_root"], world["rom_root"]).discover()
    # bez cache (SQL)
    rep_sql = match_store(entries, idx)
    states_sql = [[(s.rom.name, s.state) for s in r.statuses] for r in rep_sql]
    # z cache w pamięci
    idx.build_match_cache()
    try:
        rep_cache = match_store(entries, idx)
    finally:
        idx.drop_match_cache()
    states_cache = [[(s.rom.name, s.state) for s in r.statuses]
                    for r in rep_cache]
    assert states_cache == states_sql
    assert idx._mcache is None                 # zwolniony


def test_find_md5_matches(tmp_path: Path):
    """find_md5 znajduje plik po MD5 (fallback), z cache i bez."""
    rom = tmp_path / "g.rom"; rom.write_bytes(b"DANE-MD5" * 50)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tmp_path)
    row = idx.lookup(rom)
    md5 = row["md5"]
    assert idx.find_md5(md5) and idx.find_md5(md5)[0]["path"] == str(rom)
    idx.build_match_cache()
    try:
        assert idx.find_md5(md5)[0]["path"] == str(rom)   # z cache to samo
        assert idx.find_md5("0" * 32) == []
    finally:
        idx.drop_match_cache()


def test_merged_superset_archive_repacks_and_keeps_source(tmp_path: Path):
    """ARCHIWUM-NADZBIÓR w ToSort (np. MAME merged: ROM-y gry + cudze) → naprawa
    WYPAKOWUJE tylko ROM-y gry do docelowego ZIP-a i NIE KASUJE źródła
    (zawiera ROM-y innych gier). To był błąd: przeniesienie całości + usunięcie
    źródła „jakby ukończył naprawę"."""
    import zipfile
    from chd_buddy.core.matcher import match_store, RomState
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    tosort = tmp_path / "ts"; tosort.mkdir()
    a1 = b"GRA-A-ROM-1" * 100; a2 = b"GRA-A-ROM-2" * 100
    x1 = b"INNA-GRA-ROM" * 100
    _write_dat(dat_root / "sys" / "s.dat", "Sys",
               {"gra_a": {"a1.rom": a1, "a2.rom": a2}})
    # merged: ROM-y gra_a (poprawne nazwy) + CUDZY rom (nadzbiór)
    merged = tosort / "merged.zip"
    with zipfile.ZipFile(merged, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a1.rom", a1); z.writestr("a2.rom", a2)
        z.writestr("clone/x1.rom", x1)          # plik innej gry → nadzbiór
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort, full=True)
    entries = DatStore(dat_root, rom_root).discover()

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r
    for e in entries:
        e.store_format = "zip"
    reports = match_store(entries, idx)
    # gra_a rozpoznana jako nadzbiór (via_archive, superset)
    sts = reports[0].statuses
    assert all(s.via_archive and s.archive_superset for s in sts)

    rb = Rebuilder(idx, dry_run=False, log=lambda m: None)
    rb.run(reports, rules=rules_fn)
    dest = entries[0].target_dir / "gra_a.zip"
    assert dest.is_file()
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
        assert names == {"a1.rom", "a2.rom"}     # TYLKO ROM-y gry, bez cudzego
        assert z.read("a1.rom") == a1 and z.read("a2.rom") == a2
    assert merged.is_file()                       # ŹRÓDŁO merged NIE skasowane
    with zipfile.ZipFile(merged) as z:
        assert "clone/x1.rom" in z.namelist()     # cudzy rom nietknięty


def test_incomplete_target_archive_overwritten_from_merged(tmp_path: Path):
    """Cel ma ZŁY/niekompletny ZIP, a kompletne ROM-y są w nadzbiorze (merged)
    w ToSort → naprawa NADPISUJE cel poprawnym zestawem (wypakowanym z merged),
    a źródło merged ZOSTAJE (ma ROM-y innych gier). To był błąd: „plik był już
    na miejscu i program nie mógł go nadpisać"."""
    import zipfile
    from chd_buddy.core.matcher import match_store
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    tosort = tmp_path / "ts"; tosort.mkdir()
    a1 = b"GRA-ROM-1" * 100; a2 = b"GRA-ROM-2" * 100; x = b"CUDZY" * 100
    _write_dat(dat_root / "sys" / "s.dat", "Sys",
               {"gra": {"r1.rom": a1, "r2.rom": a2}})
    # CEL: istniejący ZŁY gra.zip (brakuje r2, ma śmieć)
    tdir = rom_root / "sys" / "Sys"; tdir.mkdir(parents=True)
    with zipfile.ZipFile(tdir / "gra.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("r1.rom", a1)                 # tylko połowa + brak r2
        z.writestr("smiec.rom", b"ZLE")
    # ŹRÓDŁO: merged w ToSort z KOMPLETEM gra + cudzy plik
    with zipfile.ZipFile(tosort / "merged.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("r1.rom", a1); z.writestr("r2.rom", a2)
        z.writestr("other/x.rom", x)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root); idx.scan(tosort, full=True)
    entries = DatStore(dat_root, rom_root).discover()
    for e in entries:
        e.store_format = "zip"

    def rules_fn(e):
        from chd_buddy.core.dirrules import DEFAULT_RULES
        r = dict(DEFAULT_RULES); r["format"] = "zip"; return r
    reports = match_store(entries, idx)
    Rebuilder(idx, dry_run=False, log=lambda m: None).run(reports, rules=rules_fn)

    with zipfile.ZipFile(tdir / "gra.zip") as z:
        assert set(z.namelist()) == {"r1.rom", "r2.rom"}   # NADPISANY, kompletny
        assert z.read("r2.rom") == a2 and "smiec.rom" not in z.namelist()
    assert (tosort / "merged.zip").is_file()               # źródło merged zostało


def test_clean_keeps_m3u_playlists(tmp_path: Path):
    """Sprzątanie do ToSort NIE rusza playlist .m3u (nie ma ich w DAT, ale są
    potrzebne i program sam je generuje)."""
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    tosort = tmp_path / "ts"; tosort.mkdir()
    data = b"GRA-DISC" * 100
    _write_dat(dat_root / "ps" / "p.dat", "Sony - PlayStation",
               {"Gra (Disc 1)": {"Gra (Disc 1).chd": data}})
    d = rom_root / "ps" / "Sony - PlayStation"; d.mkdir(parents=True)
    (d / "Gra (Disc 1).chd").write_bytes(data)      # gra na miejscu (HAVE)
    m3u = d / "Gra.m3u"; m3u.write_text("Gra (Disc 1).chd\n", encoding="utf-8")
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(rom_root)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)
    rb = Rebuilder(idx, tosort=tosort, dry_run=False, log=lambda m: None)
    rb.run(reports, clean=True)
    assert m3u.is_file()                            # playlista ZOSTAŁA
    assert not (tosort / "Sony - PlayStation" / "Gra.m3u").exists()


def test_parser_reads_mame_clone_merge():
    """Parser jest świadomy MAME: czyta cloneof/romof gry oraz merge ROM-a."""
    import tempfile
    from chd_buddy.core.datfile import parse_dat
    xml = ('<?xml version="1.0"?><datafile>'
           '<machine name="darkseal1" cloneof="darkseal" romof="darkseal">'
           '<rom name="fz_04-4.j12" size="131072" crc="a1a985a9"/>'
           '<rom name="fz_00-2.h12" merge="ga_00.h12" size="131072" crc="fbf3ac63"/>'
           '</machine></datafile>')
    with tempfile.NamedTemporaryFile("w", suffix=".dat", delete=False,
                                     encoding="utf-8") as f:
        f.write(xml); p = f.name
    games = list(parse_dat(Path(p)))
    os.unlink(p)
    assert len(games) == 1
    g = games[0]
    assert g.cloneof == "darkseal" and g.romof == "darkseal"
    merges = {r.name: r.merge for r in g.roms}
    assert merges["fz_00-2.h12"] == "ga_00.h12"      # współdzielony z parentem
    assert merges["fz_04-4.j12"] == ""               # własny ROM klonu


def test_dedup_never_deletes_file_moved_to_tosort_this_run(tmp_path: Path):
    """UTRATA DANYCH (arcade klony): clean przeniósł plik roms→ToSort, a dedup
    skasował go jako „już na miejscu" — choć to była jego JEDYNA kopia. Guard:
    plik przeniesiony do ToSort w TYM przebiegu NIE jest kasowany przez dedup."""
    roms = tmp_path / "roms"; roms.mkdir()
    ts = tmp_path / "ts"; (ts / "sys").mkdir(parents=True)
    data = b"KLON-ARCADE" * 100
    canon = roms / "game.zip"; canon.write_bytes(data)   # „kanoniczny" (claim)
    moved = ts / "sys" / "klon.zip"; moved.write_bytes(data)  # ta sama treść
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(roms); idx.scan(ts)

    rb = Rebuilder(idx, dry_run=False, log=lambda m: None)
    rb._claims["k"] = canon
    rb._moved_to_tosort.add(os.path.normcase(str(moved)))   # clean go przeniósł
    rb._dedup_confirmed([ts], [], delete_roots=[ts])
    assert moved.is_file()                                  # NIE skasowany

    # KONTROLA: bez oznaczenia „moved this run" — zwykła kopia w ToSort jest
    # kasowana (kanoniczny fizycznie istnieje, więc to bezpieczne)
    rb2 = Rebuilder(idx, dry_run=False, log=lambda m: None)
    rb2._claims["k"] = canon
    rb2._dedup_confirmed([ts], [], delete_roots=[ts])
    assert not moved.exists()                               # skasowany (dup)


def test_dedup_skips_when_canonical_file_missing(tmp_path: Path):
    """Guard: gdy plik kanoniczny NIE istnieje fizycznie (ghost w indeksie),
    dedup NIE kasuje kopii z ToSort (mogłaby to być jedyna kopia)."""
    roms = tmp_path / "roms"; roms.mkdir()
    ts = tmp_path / "ts"; (ts / "sys").mkdir(parents=True)
    data = b"TRESC" * 100
    canon = roms / "game.zip"; canon.write_bytes(data)
    dup = ts / "sys" / "kopia.zip"; dup.write_bytes(data)
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(roms); idx.scan(ts)
    canon.unlink()                                          # ghost: plik zniknął
    rb = Rebuilder(idx, dry_run=False, log=lambda m: None)
    rb._claims["k"] = canon
    rb._dedup_confirmed([ts], [], delete_roots=[ts])
    assert dup.is_file()                                    # NIE skasowany



def test_restore_from_mega_zip_of_renamed_files(tmp_path: Path):
    """Scenariusz usera: jeden ZIP z plikami RÓŻNYCH systemów pod LOSOWYMI
    nazwami → program identyfikuje po TREŚCI i rozkłada każdy do właściwego
    DAT-a z POPRAWNĄ nazwą. Treść nieznana → ToSort."""
    import zipfile
    from chd_buddy.core.matcher import match_store
    dat_root = tmp_path / "dats"; rom_root = tmp_path / "roms"
    tosort = tmp_path / "ts"; tosort.mkdir()
    mario = b"SUPER-MARIO-NES" * 40
    zelda = b"ZELDA-SNES-DANE" * 40
    junk = b"NIEZNANE-NIGDZIE" * 40
    _write_dat(dat_root / "nes" / "n.dat", "Nintendo - NES",
               {"Super Mario Bros (USA)": {"Super Mario Bros (USA).nes": mario}})
    _write_dat(dat_root / "snes" / "s.dat", "Nintendo - SNES",
               {"Zelda (USA)": {"Zelda (USA).sfc": zelda}})
    # jeden ZIP w ToSort: pliki obu systemów pod LOSOWYMI nazwami + śmieć
    mega = tosort / "mega_losowe.zip"
    with zipfile.ZipFile(mega, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("aaa111.xyz", mario)     # to jest Mario (NES)
        z.writestr("bbb222.qqq", zelda)     # to jest Zelda (SNES)
        z.writestr("smiec.dat", junk)       # nieznane
    idx = FileIndex(tmp_path / "idx.sqlite3"); idx.scan(tosort, full=True)
    entries = DatStore(dat_root, rom_root).discover()
    reports = match_store(entries, idx)
    rb = Rebuilder(idx, tosort=tosort, dry_run=False, log=lambda m: None)
    rb.run(reports, clean=True)

    nes = rom_root / "nes" / "Nintendo - NES" / "Super Mario Bros (USA).nes"
    snes = rom_root / "snes" / "Nintendo - SNES" / "Zelda (USA).sfc"
    assert nes.is_file() and nes.read_bytes() == mario   # NES: poprawna nazwa+miejsce
    assert snes.is_file() and snes.read_bytes() == zelda  # SNES: j.w.
    assert mega.is_file()                                 # źródło (nadzbiór) zostaje

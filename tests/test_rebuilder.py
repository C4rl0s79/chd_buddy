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
    """CRC się zgadza (kolizja/celowo), ale SHA-1 nie => plik NIE ląduje."""
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
    idx.scan(world["src"])
    # sfałszuj wpis membera tak, by CRC+rozmiar pasowały do DAT-a
    good = world["only_a"]
    idx._db.execute(
        "UPDATE members SET crc32=?, size=? WHERE archive=?",
        (f"{zlib.crc32(good) & 0xFFFFFFFF:08x}", len(good), str(zpath)))
    idx._db.commit()

    reports = match_store(entries, idx)
    s = [s for r in reports for s in r.statuses if s.rom.name == "Gra A.iso"][0]
    assert s.state == RomState.ELSEWHERE and s.member
    st = Rebuilder(idx, dry_run=False).run(reports)
    assert st.errors == 1 and st.unpacked == 0
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

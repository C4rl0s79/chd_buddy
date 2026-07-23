"""Trwały indeks plików (SQLite) + skaner przyrostowy.

Cel: nie liczyć sum kontrolnych w kółko. Raz zeskanowany plik jest pamiętany
w bazie (ścieżka, rozmiar, mtime, CRC32/MD5/SHA-1); dopóki rozmiar i mtime się
nie zmienią, przy kolejnych skanach sumy NIE są przeliczane. Dzięki temu
katalogi docelowe (także na NAS) można skanować tanio, a pliki raz rozpoznane
"istnieją" dla wszystkich DAT-ów aż do fizycznego przeniesienia.

Zasady:
- Skanujemy WSZYSTKIE wskazane korzenie (źródłowe i docelowe) do jednej bazy.
- Symlinki/junctions są rejestrowane jako linki (is_link=1) i nigdy nie są
  haszowane ani rozwijane — fizyczna tożsamość należy do celu linku.
- Plik nieobecny przy skanie korzenia dostaje missing=1 (nie jest usuwany
  z bazy — NAS może być chwilowo odpięty); pojawi się znów => missing=0.
- --full wymusza ponowne policzenie sum mimo zgodnego (rozmiar, mtime).
- Dla .chd można podać próbnik (chd_prober), który zwraca SHA-1 ZAWARTOŚCI
  z nagłówka CHD (chdman info) — to nim CHD trafia w DAT-y bez ekstrakcji.

Ścieżki przechowywane są absolutnie (os.path.abspath, bez rozwiązywania
symlinków). Windows nie rozróżnia wielkości liter, ale nasz własny skan
produkuje spójne ścieżki, więc UNIQUE po ścieżce wystarcza.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat as stat_mod
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .settings import app_base_dir

INDEX_DB_FILENAME = "chd_buddy_index.sqlite3"

# Próbnik zawartości CHD: path -> hex SHA-1 zawartości ("" gdy nieznane).
ChdProber = Callable[[Path], str]
# Callback postępu: (liczba widzianych plików, aktualna ścieżka).
FileCB = Callable[[int, Path], None]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    crc32 TEXT NOT NULL DEFAULT '',
    md5 TEXT NOT NULL DEFAULT '',
    sha1 TEXT NOT NULL DEFAULT '',
    data_sha1 TEXT NOT NULL DEFAULT '',
    is_link INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    scanned_at TEXT NOT NULL DEFAULT '',
    -- mtime_ns pliku w chwili NIEUDANEJ głębokiej identyfikacji CHD:
    -- porażka też jest wynikiem — nie mielimy tego samego pliku co skan.
    deep_fail INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_sha1 ON files(sha1);
CREATE INDEX IF NOT EXISTS idx_files_crc32 ON files(crc32);
CREATE INDEX IF NOT EXISTS idx_files_data_sha1 ON files(data_sha1);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    archive TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    crc32 TEXT NOT NULL DEFAULT '',
    md5 TEXT NOT NULL DEFAULT '',
    sha1 TEXT NOT NULL DEFAULT '',
    UNIQUE(archive, name)
);
CREATE INDEX IF NOT EXISTS idx_members_crc ON members(crc32, size);
CREATE INDEX IF NOT EXISTS idx_members_sha1 ON members(sha1);
"""

# Archiwa, których zawartość indeksujemy (CRC32+rozmiar z metadanych —
# bez dekompresji; SHA-1 weryfikowany przy wypakowaniu).
# .7z wymaga opcjonalnego py7zr (pip install .[archives]).
ARCHIVE_EXTS = {"zip", "7z"}

_HASH_CHUNK = 4 * 1024 * 1024  # 4 MiB — duże bloki opłacają się na NAS/SMB


def default_db_path() -> Path:
    """Domyślna lokalizacja bazy — przenośnie, obok exe/projektu."""
    return app_base_dir() / INDEX_DB_FILENAME


def hash_file(path: Path) -> tuple[str, str, str]:
    """Liczy (crc32, md5, sha1) w jednym przebiegu po pliku."""
    crc = 0
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
            md5.update(chunk)
            sha1.update(chunk)
    return f"{crc & 0xFFFFFFFF:08x}", md5.hexdigest(), sha1.hexdigest()


def is_reparse_stat(st: os.stat_result) -> bool:
    """Czy wpis to reparse point (symlink/junction) — po lstat."""
    if stat_mod.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse)


# Artefakty robocze kombajnu — NIGDY nie indeksowane (żywe pliki tymczasowe
# naprawy/ekstrakcji/dedupu nie mogą stać się kandydatami dopasowania).
_TEMP_DIR_PREFIXES = ("chdbuddy_", "chddeep_", "chd_buddy_")
_TEMP_FILE_MARKERS = (".rtcheck.", ".chdbuddy_extract_tmp",
                      ".chdbuddy_dedup_tmp", ".chd_tmp", ".json.tmp")


def _is_temp_artifact(name: str, is_dir: bool) -> bool:
    low = name.lower()
    if is_dir:
        return low.startswith(_TEMP_DIR_PREFIXES)
    return (low.startswith("chdbuddy_tmp_")
            or any(m in low for m in _TEMP_FILE_MARKERS))


def _walk(root: Path) -> Iterator[tuple[Path, os.stat_result, bool]]:
    """Rekurencyjny scandir; yielduje (ścieżka, lstat, czy_link).

    W linkowane katalogi NIE wchodzi (yielduje je jako linki) — inaczej
    zdeduplikowana kolekcja byłaby liczona wielokrotnie. Katalogi i pliki
    tymczasowe kombajnu są pomijane w całości.
    """
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _is_temp_artifact(e.name, e.is_dir(follow_symlinks=False)):
                    continue
                link = is_reparse_stat(st)
                if e.is_dir(follow_symlinks=False):
                    if link:
                        yield Path(e.path), st, True
                    else:
                        stack.append(Path(e.path))
                else:
                    yield Path(e.path), st, link


@dataclass
class ScanStats:
    seen: int = 0          # wszystkie napotkane wpisy (pliki + linki)
    hashed: int = 0        # policzone od nowa
    unchanged: int = 0     # zgodny (rozmiar, mtime) => sumy z bazy
    links: int = 0         # zarejestrowane symlinki/junctions
    filtered: int = 0      # pominięte filtrem rozszerzeń
    missing: int = 0       # oznaczone jako nieobecne pod korzeniem
    errors: int = 0        # błędy odczytu
    bytes_hashed: int = 0
    adopted: int = 0       # przeniesione pliki przejęte z bazy (bez czytania)
    cancelled: bool = False   # przerwany przez użytkownika (postęp zapisany)

    def summary(self) -> str:
        if self.cancelled:
            return (f"PRZERWANY — plików {self.seen}, policzono {self.hashed} "
                    f"({self.bytes_hashed / 2**30:.2f} GiB), zapisane")
        return (f"plików {self.seen}, policzono {self.hashed} "
                f"({self.bytes_hashed / 2**30:.2f} GiB), bez zmian {self.unchanged}, "
                f"przejęte po przenosinach {self.adopted}, "
                f"linki {self.links}, brakujące {self.missing}, błędy {self.errors}")


@dataclass
class DupGroup:
    sha1: str
    size: int
    paths: list[str] = field(default_factory=list)


class FileIndex:
    """Baza tożsamości plików + skan przyrostowy."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        # migracja starych baz (CREATE IF NOT EXISTS nie dodaje kolumn)
        try:
            self._db.execute(
                "ALTER TABLE files ADD COLUMN deep_fail INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass                       # kolumna już jest
        self._db.commit()

    # --- cykl życia ---------------------------------------------------------

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "FileIndex":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- skanowanie ---------------------------------------------------------

    def scan(
        self,
        root: Path,
        *,
        full: bool = False,
        exts: Optional[set[str]] = None,
        chd_prober: Optional[ChdProber] = None,
        on_file: Optional[FileCB] = None,
        log: Optional[Callable[[str], None]] = None,
        cancel=None,
    ) -> ScanStats:
        """Skanuje drzewo `root` przyrostowo do bazy.

        exts — zbiór rozszerzeń bez kropki (lowercase); None = wszystkie pliki.
        UWAGA: plik zaindeksowany wcześniej, a teraz odfiltrowany rozszerzeniem,
        zostanie oznaczony jako missing (filtr zawęża "widziane" wpisy).

        cancel — threading.Event: przerwanie w dowolnym momencie. Wszystko, co
        zdążyliśmy policzyć, JEST ZAPISANE (commit partiami), więc kolejny skan
        kontynuuje, a nie liczy od zera. Przerwany skan NIE oznacza plików jako
        brakujących (nie widzieliśmy całego drzewa).
        """
        root = Path(os.path.abspath(root))
        if not root.is_dir():
            raise NotADirectoryError(f"'{root}' nie jest katalogiem")
        stats = ScanStats()
        now = datetime.now().isoformat(timespec="seconds")
        cur = self._db.cursor()
        seen: list[str] = []
        pending = 0

        cancelled = False
        for path, st, link in _walk(root):
            if cancel is not None and cancel.is_set():
                cancelled = True
                break                      # to, co policzone, zostaje w bazie
            key = str(path)
            stats.seen += 1
            if on_file:
                on_file(stats.seen, path)

            if link:
                stats.links += 1
                seen.append(key)
                cur.execute(
                    "INSERT INTO files(path, size, mtime_ns, is_link, missing, scanned_at) "
                    "VALUES (?, 0, ?, 1, 0, ?) "
                    "ON CONFLICT(path) DO UPDATE SET is_link=1, missing=0, "
                    "  mtime_ns=excluded.mtime_ns, scanned_at=excluded.scanned_at",
                    (key, st.st_mtime_ns, now),
                )
                pending += 1
            else:
                if exts is not None and path.suffix.lower().lstrip(".") not in exts:
                    stats.filtered += 1
                    continue
                seen.append(key)
                row = cur.execute(
                    "SELECT size, mtime_ns, sha1, data_sha1, missing FROM files WHERE path=?",
                    (key,),
                ).fetchone()
                fresh = (row is not None and not full
                         and row["size"] == st.st_size
                         and row["mtime_ns"] == st.st_mtime_ns
                         and row["sha1"] != "")
                if fresh:
                    stats.unchanged += 1
                    if row["missing"]:
                        cur.execute("UPDATE files SET missing=0 WHERE path=?", (key,))
                        pending += 1
                    # backfill zawartości CHD, jeśli teraz mamy próbnik
                    if chd_prober and not row["data_sha1"] and path.suffix.lower() == ".chd":
                        ds = self._probe_chd(chd_prober, path, log)
                        if ds:
                            cur.execute("UPDATE files SET data_sha1=? WHERE path=?", (ds, key))
                            pending += 1
                    # backfill członków archiwum (baza sprzed tej funkcji)
                    if path.suffix.lower().lstrip(".") in ARCHIVE_EXTS:
                        n = cur.execute("SELECT COUNT(*) FROM members WHERE archive=?",
                                        (key,)).fetchone()[0]
                        if n == 0:
                            pending += self._index_members(cur, key, path, log)
                else:
                    # PLIK PRZENIESIONY (np. ręcznie w Eksploratorze)?
                    # Wiedza podąża za treścią: ta sama nazwa + rozmiar +
                    # mtime_ns, a stara ścieżka już nie istnieje => przejmij
                    # sumy/data_sha1/deep_fail/członków BEZ czytania danych.
                    if row is None and not full:
                        if self._adopt_moved(cur, key, path, st, now):
                            stats.adopted += 1
                            pending += 1
                            continue
                    try:
                        crc, md5, sha1 = hash_file(path)
                    except OSError as e:
                        stats.errors += 1
                        if log:
                            log(f"BŁĄD odczytu: {path} ({e})")
                        continue
                    ds = ""
                    if chd_prober and path.suffix.lower() == ".chd":
                        ds = self._probe_chd(chd_prober, path, log)
                    cur.execute(
                        "INSERT INTO files(path, size, mtime_ns, crc32, md5, sha1, "
                        "                  data_sha1, is_link, missing, scanned_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?) "
                        "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                        "  mtime_ns=excluded.mtime_ns, crc32=excluded.crc32, "
                        "  md5=excluded.md5, sha1=excluded.sha1, "
                        "  data_sha1=excluded.data_sha1, is_link=0, missing=0, "
                        "  scanned_at=excluded.scanned_at, deep_fail=0",
                        (key, st.st_size, st.st_mtime_ns, crc, md5, sha1, ds, now),
                    )
                    stats.hashed += 1
                    stats.bytes_hashed += st.st_size
                    pending += 1
                    if path.suffix.lower().lstrip(".") in ARCHIVE_EXTS:
                        cur.execute("DELETE FROM members WHERE archive=?", (key,))
                        # skan pełny: wypakuj i policz SHA-1 członków (weryfikacja
                        # zawartości + trwały odcisk); skan szybki: tylko CRC32
                        # z centralnego katalogu (bez dekompresji).
                        pending += self._index_members(cur, key, path, log,
                                                       full=full)

            if pending >= 200:  # commituj partiami — długi skan NAS nie przepada
                self._db.commit()
                pending = 0

        if cancelled:
            # NIE oznaczamy brakujących — nie obeszliśmy całego drzewa
            self._db.commit()
            stats.cancelled = True
            if log:
                log(f"PRZERWANO skan {root} — zapisano {stats.hashed} "
                    f"policzonych plików (kolejny skan dokończy resztę).")
            return stats
        stats.missing = self._mark_missing(root, seen)
        self._db.commit()
        return stats

    def prune_ghosts(self, log=None) -> int:
        """Oznacza missing=1 wpisy, których PLIK już nie istnieje.

        Skan robi to tylko dla katalogów, które ODWIEDZA — gdy cały korzeń
        zniknął (np. użytkownik skasował stare `roms`), jego wpisy zostawały
        w bazie jako „obecne" i matcher planował przenosiny z nieistniejących
        ścieżek. Tani lstat po wszystkich aktywnych wpisach."""
        n = 0
        cur = self._db.cursor()
        for row in cur.execute(
                "SELECT path FROM files WHERE missing=0").fetchall():
            if not os.path.lexists(row["path"]):
                self._db.execute(
                    "UPDATE files SET missing=1 WHERE path=?", (row["path"],))
                n += 1
        # sieroty: członkowie archiwów, których wiersz-archiwum już NIE MA
        # w bazie. UWAGA: członków archiwów missing=1 ZOSTAWIAMY — to pamięć
        # dla adopcji (przeniesione archiwum przejmuje ich bez ponownego
        # czytania); z dopasowań i tak wypadają (JOIN po missing=0).
        m = self._db.execute(
            "DELETE FROM members WHERE archive NOT IN "
            "(SELECT path FROM files)").rowcount
        if n or m:
            self._db.commit()
            if log:
                log(f"Indeks: oznaczono {n} duchów"
                    + (f", usunięto {m} osieroconych członków archiwów"
                       if m else "") + ".")
        return n

    @staticmethod
    def _adopt_moved(cur, key: str, path: Path, st, now: str) -> bool:
        """Przejmuje wpis PRZENIESIONEGO pliku bez ponownego liczenia sum.

        Dopasowanie: identyczna NAZWA pliku + rozmiar + mtime_ns (mtime w
        nanosekundach przeżywa move/rename na tym samym i między woluminami
        NTFS), a stara ścieżka już nie wskazuje pliku. Przenosi też członków
        archiwum i pamięć o nieudanej identyfikacji CHD (deep_fail).
        Dzięki temu ręczne przenosiny setek GB nie kosztują godzin haszowania.
        """
        cands = cur.execute(
            "SELECT * FROM files WHERE size=? AND mtime_ns=? AND is_link=0 "
            "AND sha1<>'' AND path<>?",
            (st.st_size, st.st_mtime_ns, key)).fetchall()
        name = path.name.lower()
        matches = []
        for c in cands:
            if Path(c["path"]).name.lower() != name:
                continue
            if os.path.lexists(c["path"]):
                continue                     # stary plik wciąż istnieje — to kopia
            matches.append(c)
        if len(matches) != 1:
            return False                     # niejednoznaczne => policz normalnie
        old = matches[0]
        cur.execute(
            "INSERT INTO files(path, size, mtime_ns, crc32, md5, sha1, "
            "                  data_sha1, is_link, missing, scanned_at, deep_fail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
            "  mtime_ns=excluded.mtime_ns, crc32=excluded.crc32, "
            "  md5=excluded.md5, sha1=excluded.sha1, "
            "  data_sha1=excluded.data_sha1, is_link=0, missing=0, "
            "  scanned_at=excluded.scanned_at, deep_fail=excluded.deep_fail",
            (key, st.st_size, st.st_mtime_ns, old["crc32"], old["md5"],
             old["sha1"], old["data_sha1"], now, old["deep_fail"]))
        cur.execute("UPDATE members SET archive=? WHERE archive=?",
                    (key, old["path"]))
        cur.execute("DELETE FROM files WHERE path=?", (old["path"],))
        return True

    @staticmethod
    def _index_members(cur, key: str, path: Path, log, full: bool = False) -> int:
        """Indeksuje zawartość archiwum.

        Szybko (full=False): tylko metadane — CRC32+rozmiar z centralnego
        katalogu ZIP-a / nagłówka 7z (bez dekompresji).
        Pełne (full=True): DODATKOWO wypakowuje każdy plik i liczy MD5+SHA-1
        (weryfikacja zawartości + trwały odcisk do dopasowania po SHA-1).
        Gdy policzony CRC nie zgadza się z centralnym katalogiem — ostrzeżenie
        (archiwum uszkodzone), ale wpis i tak trafia z realnymi sumami.

        7z wymaga py7zr — bez niego archiwum zostaje zwykłym plikiem.
        """
        # (name -> (size, crc, md5, sha1)); md5/sha1 puste przy skanie szybkim
        meta: dict[str, tuple[int, str, str, str]] = {}
        try:
            if path.suffix.lower() == ".7z":
                try:
                    import py7zr
                except ImportError:
                    if log:
                        log(f"7z pominięte (brak py7zr — pip install py7zr): {path.name}")
                    return 0
                with py7zr.SevenZipFile(path) as zf:
                    for i in zf.list():
                        if i.is_directory:
                            continue
                        crc = f"{i.crc32 & 0xFFFFFFFF:08x}" if i.crc32 else ""
                        meta[i.filename] = (i.uncompressed or 0, crc, "", "")
                if full:
                    FileIndex._hash_7z_members(path, meta, log)
            else:
                import zipfile
                with zipfile.ZipFile(path) as zf:
                    for i in zf.infolist():
                        if i.is_dir():
                            continue
                        meta[i.filename] = (i.file_size,
                                            f"{i.CRC & 0xFFFFFFFF:08x}", "", "")
                    if full:
                        FileIndex._hash_zip_members(zf, meta, log, path)
        except Exception as e:  # uszkodzone archiwum nie może ubić skanu
            if log:
                log(f"ARCHIWUM nieczytelne: {path} ({e})")
            return 0
        rows = [(key, name, size, crc, md5, sha1)
                for name, (size, crc, md5, sha1) in meta.items()]
        cur.executemany(
            "INSERT INTO members(archive, name, size, crc32, md5, sha1) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(archive, name) DO UPDATE SET size=excluded.size, "
            "  crc32=excluded.crc32, md5=excluded.md5, sha1=excluded.sha1", rows)
        return len(rows)

    @staticmethod
    def _hash_zip_members(zf, meta: dict, log, path: Path) -> None:
        """Wypakowuje strumieniowo każdy plik ZIP-a i liczy CRC/MD5/SHA-1."""
        for name in list(meta):
            crc = 0
            md5 = hashlib.md5()
            sha1 = hashlib.sha1()
            size = 0
            try:
                with zf.open(name) as fh:
                    while True:
                        b = fh.read(_HASH_CHUNK)
                        if not b:
                            break
                        crc = zlib.crc32(b, crc)
                        md5.update(b)
                        sha1.update(b)
                        size += len(b)
            except Exception as e:
                if log:
                    log(f"ARCHIWUM: nie wypakowano {path.name}::{name} ({e})")
                continue
            crc_hex = f"{crc & 0xFFFFFFFF:08x}"
            if log and meta[name][1] and meta[name][1] != crc_hex:
                log(f"UWAGA CRC: {path.name}::{name} centralny {meta[name][1]} "
                    f"!= policzony {crc_hex} (uszkodzone?)")
            meta[name] = (size, crc_hex, md5.hexdigest(), sha1.hexdigest())

    @staticmethod
    def _hash_7z_members(path: Path, meta: dict, log) -> None:
        """Wypakowuje 7z (solid) i liczy CRC/MD5/SHA-1 członków."""
        import py7zr
        try:
            with py7zr.SevenZipFile(path) as zf:
                data = zf.readall()          # {name: BytesIO}
        except Exception as e:
            if log:
                log(f"ARCHIWUM 7z: nie wypakowano {path.name} ({e})")
            return
        for name, bio in data.items():
            if name not in meta:
                continue
            blob = bio.read()
            meta[name] = (len(blob), f"{zlib.crc32(blob) & 0xFFFFFFFF:08x}",
                          hashlib.md5(blob).hexdigest(),
                          hashlib.sha1(blob).hexdigest())

    @staticmethod
    def _probe_chd(prober: ChdProber, path: Path, log) -> str:
        try:
            return prober(path) or ""
        except Exception as e:  # próbnik nie może ubić skanu
            if log:
                log(f"CHD prober: {path.name}: {e}")
            return ""

    def _mark_missing(self, root: Path, seen: Iterable[str]) -> int:
        """Oznacza missing=1 wpisy pod `root`, których skan nie zobaczył."""
        prefix = str(root).rstrip("\\/") + os.sep
        cur = self._db.cursor()
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS _seen(path TEXT PRIMARY KEY)")
        cur.execute("DELETE FROM _seen")
        cur.executemany("INSERT OR IGNORE INTO _seen(path) VALUES (?)",
                        ((s,) for s in seen))
        cur.execute(
            "UPDATE files SET missing=1 WHERE missing=0 AND substr(path, 1, ?) = ? "
            "AND path NOT IN (SELECT path FROM _seen)",
            (len(prefix), prefix),
        )
        return cur.rowcount

    # --- zapytania ------------------------------------------------------------

    def lookup(self, path: Path | str) -> Optional[sqlite3.Row]:
        key = str(Path(os.path.abspath(path)))
        return self._db.execute("SELECT * FROM files WHERE path=?", (key,)).fetchone()

    def find_sha1(self, sha1: str, include_chd_content: bool = True) -> list[sqlite3.Row]:
        """Pliki o danym SHA-1 (opcjonalnie także trafienia w zawartość CHD)."""
        q = "SELECT * FROM files WHERE missing=0 AND (sha1=?"
        args: list[str] = [sha1.lower()]
        if include_chd_content:
            q += " OR data_sha1=?"
            args.append(sha1.lower())
        q += ")"
        return self._db.execute(q, args).fetchall()

    def duplicate_groups(self, min_size: int = 1) -> list[DupGroup]:
        """Grupy identycznych plików fizycznych (ten sam SHA-1 i rozmiar)."""
        groups: list[DupGroup] = []
        rows = self._db.execute(
            "SELECT sha1, size FROM files "
            "WHERE missing=0 AND is_link=0 AND sha1 != '' AND size >= ? "
            "GROUP BY sha1, size HAVING COUNT(*) > 1 ORDER BY size DESC",
            (min_size,),
        ).fetchall()
        for r in rows:
            paths = [p["path"] for p in self._db.execute(
                "SELECT path FROM files WHERE sha1=? AND size=? AND missing=0 AND is_link=0 "
                "ORDER BY path",
                (r["sha1"], r["size"]),
            )]
            groups.append(DupGroup(sha1=r["sha1"], size=r["size"], paths=paths))
        return groups

    def find_crc(self, crc32: str, size: int) -> list[sqlite3.Row]:
        """Pliki o danym CRC32 i rozmiarze (fallback, gdy DAT nie ma SHA-1)."""
        return self._db.execute(
            "SELECT * FROM files WHERE missing=0 AND crc32=? AND size=?",
            (crc32.lower().zfill(8), size),
        ).fetchall()

    def find_member_crc(self, crc32: str, size: int) -> list[sqlite3.Row]:
        """Pliki WEWNĄTRZ archiwów o danym CRC32+rozmiarze (archiwum obecne)."""
        return self._db.execute(
            "SELECT m.* FROM members m JOIN files f ON f.path = m.archive "
            "WHERE f.missing=0 AND m.crc32=? AND m.size=?",
            (crc32.lower().zfill(8), size),
        ).fetchall()

    def find_member_sha1(self, sha1: str) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT m.* FROM members m JOIN files f ON f.path = m.archive "
            "WHERE f.missing=0 AND m.sha1=?",
            (sha1.lower(),),
        ).fetchall()

    def member_name_in(self, archive: str, sha1: str, crc: str,
                        size: int) -> Optional[str]:
        """Nazwa pliku WEWNĄTRZ danego archiwum, dopasowana po SUMIE (SHA-1,
        potem CRC32+rozmiar). Do przepakowania: bierzemy dane po sumie, a nie
        po nazwie (nazwa w źródle może być błędna)."""
        if sha1:
            r = self._db.execute(
                "SELECT name FROM members WHERE archive=? AND sha1=?",
                (archive, sha1.lower())).fetchone()
            if r:
                return r["name"]
        if crc and size:
            r = self._db.execute(
                "SELECT name FROM members WHERE archive=? AND crc32=? AND size=?",
                (archive, crc.lower().zfill(8), size)).fetchone()
            if r:
                return r["name"]
        return None

    def reindex_archive(self, path: Path | str, full: bool = True) -> None:
        """Przeindeksowuje świeżo utworzone/zmienione archiwum: wpis pliku
        (własne sumy) + członkowie (z SHA-1 gdy full)."""
        p = Path(os.path.abspath(path))
        key = str(p)
        crc, md5, sha1 = hash_file(p)
        st = os.lstat(p)
        now = datetime.now().isoformat(timespec="seconds")
        cur = self._db.cursor()
        cur.execute(
            "INSERT INTO files(path, size, mtime_ns, crc32, md5, sha1, "
            "                  is_link, missing, scanned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
            "  mtime_ns=excluded.mtime_ns, crc32=excluded.crc32, "
            "  md5=excluded.md5, sha1=excluded.sha1, is_link=0, missing=0, "
            "  scanned_at=excluded.scanned_at",
            (key, st.st_size, st.st_mtime_ns, crc, md5, sha1, now))
        cur.execute("DELETE FROM members WHERE archive=?", (key,))
        self._index_members(cur, key, p, None, full=full)
        self._db.commit()

    def record_file(self, path: Path | str, crc32: str, md5: str, sha1: str) -> None:
        """Rejestruje świeżo utworzony plik (np. wypakowany z archiwum)."""
        p = Path(os.path.abspath(path))
        st = os.lstat(p)
        now = datetime.now().isoformat(timespec="seconds")
        self._db.execute(
            "INSERT INTO files(path, size, mtime_ns, crc32, md5, sha1, "
            "                  is_link, missing, scanned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
            "  mtime_ns=excluded.mtime_ns, crc32=excluded.crc32, "
            "  md5=excluded.md5, sha1=excluded.sha1, is_link=0, missing=0, "
            "  scanned_at=excluded.scanned_at",
            (str(p), st.st_size, st.st_mtime_ns, crc32.lower(), md5.lower(),
             sha1.lower(), now))
        self._db.commit()

    def all_under(self, root: Path | str, physical_only: bool = True) -> list[sqlite3.Row]:
        """Wszystkie zaindeksowane wpisy pod katalogiem `root`."""
        prefix = str(Path(os.path.abspath(root))).rstrip("\\/") + os.sep
        q = "SELECT * FROM files WHERE missing=0 AND substr(path, 1, ?) = ?"
        if physical_only:
            q += " AND is_link=0"
        return self._db.execute(q, (len(prefix), prefix)).fetchall()

    def rename(self, old: Path | str, new: Path | str) -> None:
        """Aktualizuje ścieżkę wpisu po przeniesieniu/zmianie nazwy pliku."""
        old_key = str(Path(os.path.abspath(old)))
        new_key = str(Path(os.path.abspath(new)))
        self._db.execute("DELETE FROM files WHERE path=?", (new_key,))
        self._db.execute("UPDATE files SET path=? WHERE path=?", (new_key, old_key))
        self._db.commit()

    def set_deep_fail(self, path: Path | str) -> None:
        """Zapamiętuje NIEUDANĄ głęboką identyfikację CHD (przy bieżącym
        mtime). Dopóki plik się nie zmieni (i nie wymusisz pełnego skanu),
        nie próbujemy ekstrakcji ponownie — porażka też jest wynikiem."""
        key = str(Path(os.path.abspath(path)))
        try:
            m = os.lstat(key).st_mtime_ns
        except OSError:
            return
        self._db.execute("UPDATE files SET deep_fail=? WHERE path=?", (m, key))
        self._db.commit()

    def set_data_sha1(self, path: Path | str, sha1: str) -> None:
        """Zapisuje SHA-1 ZAWARTOŚCI pliku CHD (z nagłówka albo głębokiej
        identyfikacji) — trwale, więc kosztowna ekstrakcja liczy się raz."""
        key = str(Path(os.path.abspath(path)))
        self._db.execute("UPDATE files SET data_sha1=? WHERE path=?",
                         (sha1.lower(), key))
        self._db.commit()

    def remove_path(self, path: Path | str) -> None:
        """Usuwa wpis pliku z indeksu (po skasowaniu pliku z dysku)."""
        key = str(Path(os.path.abspath(path)))
        self._db.execute("DELETE FROM files WHERE path=?", (key,))
        self._db.execute("DELETE FROM members WHERE archive=?", (key,))
        self._db.commit()

    def mark_link(self, path: Path | str) -> None:
        """Po zastąpieniu pliku symlinkiem: wpis staje się linkiem."""
        key = str(Path(os.path.abspath(path)))
        self._db.execute("UPDATE files SET is_link=1 WHERE path=?", (key,))
        self._db.commit()

    def stats(self) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*) AS total, "
            "  SUM(CASE WHEN is_link=1 THEN 1 ELSE 0 END) AS links, "
            "  SUM(CASE WHEN missing=1 THEN 1 ELSE 0 END) AS missing, "
            "  SUM(CASE WHEN is_link=0 AND missing=0 THEN size ELSE 0 END) AS bytes "
            "FROM files"
        ).fetchone()
        return {"total": row["total"] or 0, "links": row["links"] or 0,
                "missing": row["missing"] or 0, "bytes": row["bytes"] or 0}

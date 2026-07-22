"""Symlinki: bezpieczny mirror drzewa ROM-ów + deduplikacja linkami.

Port logiki z retrobat_safe_linker.ps1 (mirror katalogów systemów z serwera
do lokalnego drzewa RetroBata) + nowa deduplikacja: identyczne fizycznie pliki
(wg indeksu SQLite) są zastępowane symlinkami do jednej kopii.

Twarda zasada bezpieczeństwa (jak w skrypcie PS1): moduł USUWA wyłącznie
reparse pointy (symlinki/junctions). Zwykłego pliku ani katalogu nie ruszy —
najwyżej go pominie i policzy w statystykach.

Symlinki na Windows wymagają uprawnienia (tryb dewelopera albo uruchomienie
jako administrator) — brak uprawnień zgłaszamy czytelnie jako
LinkPrivilegeError zamiast tajemniczego WinError 1314.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from .fileindex import FileIndex, is_reparse_stat

# Katalogi lokalnych materiałów RetroBata — nigdy nie linkowane/nie czyszczone.
DEFAULT_EXCLUDES = ("images", "manuals", "videos")

LogCB = Callable[[str], None]

_PRIVILEGE_HINT = (
    "Brak uprawnień do tworzenia symlinków. Włącz tryb dewelopera "
    "(Ustawienia → System → Dla deweloperów) albo uruchom program "
    "jako administrator."
)


class LinkPrivilegeError(OSError):
    """WinError 1314 — proces nie ma prawa tworzyć symlinków."""


def is_link(path: Path) -> bool:
    """Czy ścieżka jest reparse pointem (symlink/junction)."""
    try:
        return is_reparse_stat(os.lstat(path))
    except OSError:
        return False


def create_link(link_path: Path, target: Path, is_dir: bool) -> None:
    """Tworzy symlink absolutny link_path -> target."""
    try:
        os.symlink(str(target), str(link_path), target_is_directory=is_dir)
    except OSError as e:
        if getattr(e, "winerror", None) == 1314:
            raise LinkPrivilegeError(_PRIVILEGE_HINT) from e
        raise


def remove_link(path: Path) -> bool:
    """Usuwa wpis TYLKO jeśli jest linkiem. Zwraca True gdy usunięto."""
    if not is_link(path):
        return False
    if path.is_dir():
        os.rmdir(path)   # rmdir na symlinku katalogu usuwa link, nie zawartość
    else:
        os.unlink(path)
    return True


def remove_broken_links(roots, index=None, log: Optional[LogCB] = None) -> int:
    """Usuwa ZERWANE symlinki (cel nie istnieje) w podanych korzeniach.

    Powstają, gdy plik-cel zostaje przeniesiony/skonwertowany/skasowany po
    utworzeniu linku (np. luźna ścieżka rodzica zjedzona przez konwersję do
    CHD). Zerwany link nic nie wskazuje i psuje konwersje/skróty — kasujemy
    go; poprawny link zostanie odtworzony przy naprawie, gdy cel wróci."""
    removed = 0
    for root in roots:
        if not root:
            continue
        r = Path(root)
        if not r.is_dir():
            continue
        for dirpath, _dn, filenames in os.walk(r):
            for name in filenames:
                p = Path(dirpath) / name
                # exists() podąża za linkiem → False gdy zerwany
                if is_link(p) and not p.exists():
                    try:
                        remove_link(p)
                    except OSError as e:
                        if log:
                            log(f"nie usunięto zerwanego linku {p}: {e}")
                        continue
                    if index is not None:
                        try:
                            index.remove_path(p)
                        except OSError:
                            pass
                    removed += 1
                    if log:
                        log(f"USUNIĘTO zerwany link: {p}")
    return removed


# --- Mirror drzewa (serwer -> lokalny RetroBat) ------------------------------

@dataclass
class MirrorStats:
    created: int = 0
    removed_stale: int = 0
    skipped_existing: int = 0   # link już jest (bez --force nie ruszamy)
    skipped_normal: int = 0     # zwykły plik/katalog w celu — nietykalny
    errors: int = 0

    def summary(self) -> str:
        return (f"utworzono {self.created}, usunięto nieaktualne {self.removed_stale}, "
                f"istniejące {self.skipped_existing}, pominięte zwykłe {self.skipped_normal}, "
                f"błędy {self.errors}")


def mirror_tree(
    source_root: Path,
    target_root: Path,
    *,
    exclude_names: Sequence[str] = DEFAULT_EXCLUDES,
    rebuild: bool = False,
    force: bool = False,
    dry_run: bool = False,
    log: Optional[LogCB] = None,
) -> MirrorStats:
    """Mirroruje katalogi systemów: target/<system>/<wpis> -> source/<system>/<wpis>.

    Katalogi systemów (poziom 1) są tworzone jako PRAWDZIWE katalogi, ich
    zawartość (poziom 2: pliki i podkatalogi) jako symlinki. Tryby:
    - sync (domyślny): usuń nieaktualne linki, dodaj brakujące;
    - rebuild: najpierw usuń WSZYSTKIE zarządzane linki, potem utwórz od nowa.
    """
    src = Path(os.path.abspath(source_root))
    dst = Path(os.path.abspath(target_root))
    if not src.is_dir():
        raise NotADirectoryError(f"'{src}' nie jest katalogiem")
    excl = {e.lower() for e in exclude_names}
    stats = MirrorStats()

    def _log(msg: str) -> None:
        if log:
            log(msg)

    if not dry_run:
        dst.mkdir(parents=True, exist_ok=True)

    for system in sorted(p for p in src.iterdir() if p.is_dir()):
        if system.name.lower() in excl:
            continue
        sys_dst = dst / system.name
        if not dry_run:
            sys_dst.mkdir(exist_ok=True)

        # 1) czyszczenie: rebuild = wszystkie linki; sync = tylko osierocone
        if sys_dst.is_dir():
            for entry in sys_dst.iterdir():
                if entry.name.lower() in excl or not is_link(entry):
                    continue
                stale = rebuild or not (system / entry.name).exists()
                if not stale:
                    continue
                _log(f"USUŃ LINK  {entry}")
                if not dry_run:
                    try:
                        remove_link(entry)
                    except OSError as e:
                        stats.errors += 1
                        _log(f"BŁĄD usuwania {entry}: {e}")
                        continue
                stats.removed_stale += 1

        # 2) tworzenie brakujących linków
        for entry in sorted(system.iterdir()):
            if entry.name.lower() in excl:
                continue
            to = sys_dst / entry.name
            try:
                exists = os.path.lexists(to)
            except OSError:
                exists = False
            if exists:
                if is_link(to):
                    if force:
                        _log(f"NADPISZ    {to}")
                        if not dry_run:
                            remove_link(to)
                            create_link(to, entry, entry.is_dir())
                        stats.created += 1
                    else:
                        stats.skipped_existing += 1
                else:
                    stats.skipped_normal += 1
                    _log(f"POMIŃ ZWYKŁY  {to}")
                continue
            _log(f"LINK       {to} -> {entry}")
            if not dry_run:
                try:
                    create_link(to, entry, entry.is_dir())
                except LinkPrivilegeError:
                    raise
                except OSError as e:
                    stats.errors += 1
                    _log(f"BŁĄD linku {to}: {e}")
                    continue
            stats.created += 1

    return stats


# --- Deduplikacja: jedna kopia fizyczna + symlinki ----------------------------

@dataclass
class DedupAction:
    keep: str
    replace: list[str] = field(default_factory=list)
    size: int = 0
    sha1: str = ""


@dataclass
class DedupStats:
    groups: int = 0
    replaced: int = 0
    skipped: int = 0
    errors: int = 0
    bytes_freed: int = 0

    def summary(self) -> str:
        return (f"grup {self.groups}, zastąpiono linkami {self.replaced}, "
                f"pominięto {self.skipped}, błędy {self.errors}, "
                f"odzyskano {self.bytes_freed / 2**30:.2f} GiB")


def _keeper_score(path: str, prefer_roots: Sequence[str]) -> tuple[int, int]:
    """Im niższy wynik, tym lepszy kandydat na kopię fizyczną."""
    for i, root in enumerate(prefer_roots):
        norm = os.path.normcase(str(Path(os.path.abspath(root)))).rstrip("\\/") + os.sep
        if os.path.normcase(path).startswith(norm):
            return (i, len(path))
    return (len(prefer_roots), len(path))


def plan_dedup(
    index: FileIndex,
    *,
    prefer_roots: Sequence[str] = (),
    min_size: int = 1,
) -> list[DedupAction]:
    """Buduje plan: w każdej grupie duplikatów zostaje jedna kopia fizyczna.

    prefer_roots — kolejność katalogów preferowanych jako miejsce kopii
    fizycznej (np. katalog "głównego" DAT-a); reszta grupy pójdzie na linki.
    """
    actions: list[DedupAction] = []
    for g in index.duplicate_groups(min_size=min_size):
        ordered = sorted(g.paths, key=lambda p: _keeper_score(p, prefer_roots))
        actions.append(DedupAction(
            keep=ordered[0], replace=ordered[1:], size=g.size, sha1=g.sha1,
        ))
    return actions


def apply_dedup(
    actions: Iterable[DedupAction],
    *,
    index: Optional[FileIndex] = None,
    dry_run: bool = False,
    log: Optional[LogCB] = None,
) -> DedupStats:
    """Wykonuje plan deduplikacji: duplikat -> symlink do kopii fizycznej.

    Podmiana jest odwracalna w trakcie: oryginał najpierw idzie pod nazwę
    tymczasową (rename w obrębie katalogu), potem powstaje link; gdy link się
    nie uda — oryginał wraca. Usunięcie tymczasowego następuje dopiero po
    udanym utworzeniu linku.
    """
    stats = DedupStats()

    def _log(msg: str) -> None:
        if log:
            log(msg)

    for a in actions:
        stats.groups += 1
        keep = Path(a.keep)
        try:
            keep_st = os.lstat(keep)
        except OSError:
            stats.skipped += len(a.replace)
            _log(f"POMIŃ GRUPĘ: kopia fizyczna zniknęła: {keep}")
            continue
        if is_reparse_stat(keep_st) or (a.size and keep_st.st_size != a.size):
            stats.skipped += len(a.replace)
            _log(f"POMIŃ GRUPĘ: kopia fizyczna zmieniona/jest linkiem: {keep}")
            continue

        for victim_s in a.replace:
            victim = Path(victim_s)
            try:
                vst = os.lstat(victim)
            except OSError:
                stats.skipped += 1
                continue
            if is_reparse_stat(vst):
                stats.skipped += 1          # już jest linkiem
                continue
            if a.size and vst.st_size != a.size:
                stats.skipped += 1
                _log(f"POMIŃ: plik zmienił się od skanu: {victim}")
                continue

            _log(f"DEDUP  {victim} -> {keep}")
            if dry_run:
                stats.replaced += 1
                stats.bytes_freed += a.size
                continue

            tmp = victim.with_name(victim.name + ".chdbuddy_dedup_tmp")
            try:
                os.replace(victim, tmp)
            except OSError as e:
                stats.errors += 1
                _log(f"BŁĄD rename {victim}: {e}")
                continue
            try:
                create_link(victim, keep, is_dir=False)
            except OSError as e:
                # rollback — oryginał wraca na miejsce
                os.replace(tmp, victim)
                if isinstance(e, LinkPrivilegeError):
                    raise
                stats.errors += 1
                _log(f"BŁĄD linku {victim}: {e}")
                continue
            try:
                os.unlink(tmp)
            except OSError as e:
                stats.errors += 1
                _log(f"UWAGA: nie usunięto pliku tymczasowego {tmp}: {e}")
            stats.replaced += 1
            stats.bytes_freed += a.size
            if index is not None:
                index.mark_link(victim)

    return stats

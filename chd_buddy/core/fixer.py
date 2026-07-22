"""Sekwencyjny silnik naprawy CHD.

Reguła biznesowa: NIGDY nie zaczynaj kolejnego pliku, dopóki poprzedni nie
został poprawnie odtworzony, zweryfikowany i podmieniony. Dzięki temu batch
wielu TB działa przy kilku GB wolnego miejsca.

Dwie operacje:
  recompress_file  – chdman copy (ten sam typ), tania w miejscu (peak≈old+new).
  retype_file      – extract -> (bin->iso) -> createdvd -> verify -> swap.

Podmiana atomowa: nowy plik powstaje jako .new, po weryfikacji oryginał ->.bak,
.new -> oryginał, na końcu .bak usuwany. Błąd na każdym etapie => rollback.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import diskbudget, imageops, roundtrip
from .chdman import CHDMan, CHDManResult
from .models import CHDInfo, DiskBudget, MediaType
from .settings import Settings

ProgressCB = Callable[[float, str], None]
LogCB = Callable[[str], None]


@dataclass
class FixOutcome:
    ok: bool
    path: Path
    action: str
    message: str = ""
    budget: Optional[DiskBudget] = None
    new_size: int = 0
    old_size: int = 0
    rolled_back: bool = False
    quarantined: bool = False
    dat_game: str = ""


def _noop_progress(_p: float, _m: str) -> None:  # pragma: no cover
    pass


def _noop_log(_m: str) -> None:  # pragma: no cover
    pass


def _source_companions(src: Path) -> list:
    """Wszystkie pliki źródłowe do usunięcia razem ze `src`.

    Dla .cue/.gdi dołącza pliki ścieżek (bin/img/raw/iso) wskazane w środku
    plus sam plik indeksu. Dla pojedynczego obrazu — tylko on sam.
    """
    import re

    files = [src]
    suf = src.suffix.lower()
    try:
        if suf == ".cue":
            text = src.read_text(errors="ignore")
            for m in re.finditer(r'FILE\s+"([^"]+)"', text):
                p = src.parent / m.group(1)
                if p.exists():
                    files.append(p)
        elif suf == ".gdi":
            for line in src.read_text(errors="ignore").splitlines()[1:]:
                for tok in line.replace('"', " ").split():
                    if tok.lower().endswith((".bin", ".raw", ".iso", ".img")):
                        p = src.parent / tok
                        if p.exists():
                            files.append(p)
    except OSError:
        pass
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _quarantine(path: Path, target_dir: Path, log: LogCB = _noop_log) -> Path:
    """Przenosi plik (i jego companiony) do katalogu kwarantanny.

    Jeśli katalog jest na tym samym woluminie — to atomowy rename (bez kopii,
    bez zużycia miejsca). Zwraca nową ścieżkę pliku głównego.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / path.name
    # nie nadpisuj istniejącego w kwarantannie
    if dest.exists():
        stem, suf = dest.stem, dest.suffix
        i = 1
        while dest.exists():
            dest = target_dir / f"{stem}.{i}{suf}"
            i += 1
    try:
        os.replace(path, dest)          # rename w obrębie woluminu
    except OSError:
        shutil.move(str(path), str(dest))  # fallback: między woluminami
    log(f"Kwarantanna: {path.name} → {dest}")
    return dest


def _atomic_replace(original: Path, new_file: Path, keep_backup: bool = False,
                    log: LogCB = _noop_log) -> Optional[Path]:
    """Zamienia original na new_file. Zwraca ścieżkę backupu jeśli zachowano."""
    backup = original.with_suffix(original.suffix + ".bak")
    if backup.exists():
        backup.unlink()
    if original.exists():
        os.replace(original, backup)  # atomowo w obrębie woluminu
    try:
        os.replace(new_file, original)
    except OSError:
        # rollback: przywróć oryginał
        if backup.exists():
            os.replace(backup, original)
        raise
    if keep_backup:
        return backup
    backup.unlink(missing_ok=True)
    return None


def _rollback(original: Path, new_file: Path, log: LogCB) -> None:
    new_file.unlink(missing_ok=True)
    backup = original.with_suffix(original.suffix + ".bak")
    if not original.exists() and backup.exists():
        os.replace(backup, original)
        log(f"Rollback: przywrócono {original.name} z .bak")


# --- Rekompresja (chdman copy) ----------------------------------------------

def recompress_file(
    chd: CHDMan,
    path: Path,
    settings: Settings,
    compression: Optional[str] = None,
    info: Optional[CHDInfo] = None,
    on_progress: ProgressCB = _noop_progress,
    log: LogCB = _noop_log,
    cancel_event: Optional[threading.Event] = None,
) -> FixOutcome:
    """Rekompresuje CHD w miejscu (ten sam typ nośnika)."""
    if info is None:
        info = chd.info(path)
    work_dir = settings.resolved_work_dir(path)
    budget = diskbudget.budget_recompress(
        info, work_dir,
        safety_factor=settings.new_chd_safety_factor,
        reserve=settings.min_free_reserve_bytes,
    )
    if not budget.fits:
        return FixOutcome(
            ok=False, path=path, action="recompress", budget=budget,
            message=(f"Za mało miejsca: potrzeba ~{budget.required_peak_bytes // 2**20} MB, "
                     f"wolne {budget.free_bytes // 2**20} MB"),
        )

    old_size = path.stat().st_size
    new_file = path.with_suffix(".chd.new")
    new_file.unlink(missing_ok=True)
    log(f"Rekompresja: {path.name}")
    res = chd.copy(path, new_file, compression=compression,
                   threads=settings.threads, on_progress=on_progress,
                   cancel_event=cancel_event)
    if not res.ok:
        _rollback(path, new_file, log)
        return FixOutcome(False, path, "recompress",
                          message=f"copy nie powiódł się (kod {res.returncode})")

    if settings.verify_after_create:
        log("Weryfikacja nowego pliku…")
        vr = chd.verify(new_file, on_progress=on_progress, cancel_event=cancel_event)
        if not vr.ok:
            _rollback(path, new_file, log)
            return FixOutcome(False, path, "recompress",
                              message="weryfikacja nowego CHD nie powiodła się")

    new_size = new_file.stat().st_size
    try:
        _atomic_replace(path, new_file, keep_backup=False, log=log)
    except OSError as e:
        _rollback(path, new_file, log)
        return FixOutcome(False, path, "recompress", message=f"podmiana nieudana: {e}",
                          rolled_back=True)

    return FixOutcome(True, path, "recompress",
                      message=f"OK ({old_size // 2**20}→{new_size // 2**20} MB)",
                      budget=budget, new_size=new_size, old_size=old_size)


# --- Naprawa typu nośnika (retype) ------------------------------------------

def retype_file(
    chd: CHDMan,
    path: Path,
    target_media: MediaType,
    settings: Settings,
    compression: Optional[str] = None,
    info: Optional[CHDInfo] = None,
    on_progress: ProgressCB = _noop_progress,
    log: LogCB = _noop_log,
    cancel_event: Optional[threading.Event] = None,
    dat_index=None,
    quarantine_dir: Optional[Path] = None,
) -> FixOutcome:
    """Naprawia błędny typ (np. DVD spakowane jako CD) przez extract+recreate."""
    if info is None:
        info = chd.info(path)
    current = info.detected_media
    work_dir = settings.resolved_work_dir(path)
    budget = diskbudget.budget_retype(
        info, work_dir,
        aggressive=settings.aggressive_low_disk,
        safety_factor=settings.new_chd_safety_factor,
        reserve=settings.min_free_reserve_bytes,
    )
    if not budget.fits:
        return FixOutcome(
            False, path, "retype", budget=budget,
            message=(f"Za mało miejsca na retype: potrzeba "
                     f"~{budget.required_peak_bytes // 2**20} MB, "
                     f"wolne {budget.free_bytes // 2**20} MB"),
        )

    old_size = path.stat().st_size
    tmp = Path(tempfile.mkdtemp(prefix="chdbuddy_", dir=str(work_dir)))
    new_file = path.with_suffix(".chd.new")
    new_file.unlink(missing_ok=True)
    try:
        # 1) Ekstrakcja obecnego (błędnego) typu.
        extract_cmd = current.extract_cmd or "extractcd"
        if current == MediaType.CD:
            cue_out = tmp / (path.stem + ".cue")
            log(f"Ekstrakcja ({extract_cmd}): {path.name}")
            er = chd.extract(extract_cmd, path, cue_out, on_progress=on_progress,
                             cancel_event=cancel_event)
            if not er.ok:
                raise RuntimeError(f"extract nie powiódł się (kod {er.returncode})")
            cue = imageops.parse_cue(cue_out)
            if not imageops.is_safe_single_data_track(cue) and not settings.aggressive_low_disk:
                raise RuntimeError(
                    "obraz nie jest pojedynczą ścieżką danych MODE1 — "
                    "retype wstrzymany (wymaga ręcznego potwierdzenia)"
                )
            if cue.bin_path is None or not cue.bin_path.exists():
                raise RuntimeError("nie znaleziono pliku .bin po ekstrakcji")
            # 2) bin -> iso (2048 B) do createdvd.
            iso_src = tmp / (path.stem + ".iso")
            log("Konwersja bin→iso…")
            imageops.bin_to_iso(cue.bin_path, cue.sector_size, iso_src)
        else:
            raise RuntimeError(f"retype z {current.value} nie jest jeszcze wspierany")

        # 2b) Policz SHA-1 obrazu ŹRÓDŁOWEGO przed pakowaniem (dla DAT/round-trip),
        #     następnie zwolnij miejsce po surowym .bin — nie jest już potrzebny.
        src_hash: Optional[str] = None
        if settings.verify_roundtrip or dat_index is not None:
            log("Liczenie SHA-1 obrazu źródłowego…")
            src_hash = roundtrip.sha1_file(iso_src, on_progress=on_progress)
        try:
            if cue.bin_path is not None:
                cue.bin_path.unlink(missing_ok=True)
        except OSError:
            pass

        # 2c) BRAMKA DAT: obraz musi odpowiadać znanemu zrzutowi (Redump).
        #     Brak dopasowania => oryginał do kwarantanny, NIE pakujemy dalej.
        dat_game = ""
        if dat_index is not None and src_hash is not None:
            match = dat_index.match_sha1(src_hash)
            if match is None:
                target = quarantine_dir or (path.parent / settings.quarantine_dir_name)
                moved = _quarantine(path, target, log)
                return FixOutcome(
                    False, path, "retype", quarantined=True,
                    message=(f"brak dopasowania w DAT (sha1={src_hash[:12]}…) — "
                             f"oryginał przeniesiony do kwarantanny: {moved}"),
                )
            dat_game = match.game
            log(f"DAT: dopasowano '{match.game}' ({match.media.value}).")
            # DAT potwierdził dane logiczne — round-trip po pakowaniu zbędny,
            # wystarczy tani verify integralności kontenera.

        # (opcjonalnie) zwolnij miejsce zajęte przez oryginał w trybie agresywnym.
        backup_kept: Optional[Path] = None
        if settings.aggressive_low_disk:
            backup_kept = path.with_suffix(path.suffix + ".bak")
            os.replace(path, backup_kept)
            log("Tryb agresywny: oryginał przeniesiony do .bak (zwolnione miejsce)")

        # 3) Utwórz nowy CHD właściwym poleceniem.
        create_cmd = target_media.create_cmd
        log(f"Tworzenie ({create_cmd})…")
        cr = chd.create(create_cmd, iso_src, new_file, compression=compression,
                        threads=settings.threads, on_progress=on_progress,
                        cancel_event=cancel_event)
        if not cr.ok:
            if backup_kept and backup_kept.exists():
                os.replace(backup_kept, path)  # przywróć oryginał
            raise RuntimeError(f"create ({create_cmd}) nie powiódł się")

        # 4) Weryfikacja PRZED nieodwracalną podmianą.
        #    Round-trip (dowód bajt-w-bajt) jest OBOWIĄZKOWY, gdy tylko mamy
        #    hash źródła — także po trafieniu w DAT. Powód: DAT potwierdza, że
        #    WEJŚCIE do createdvd jest poprawne, ale NIE że createdvd→extractdvd
        #    odtwarza je bezstratnie. Tylko round-trip to gwarantuje. Dopiero po
        #    nim wolno skasować oryginał. Błąd => przywrócenie oryginału.
        if src_hash is not None:
            # Zwolnij miejsce po obrazie źródłowym przed re-ekstrakcją, żeby
            # w danym momencie na dysku leżał tylko jeden obraz (low-disk).
            iso_src.unlink(missing_ok=True)
            log("Walidacja round-trip: wypakowanie nowego CHD i porównanie hash…")
            check_hash = roundtrip.extract_and_hash(
                chd, new_file, target_media, tmp,
                on_progress=on_progress, cancel_event=cancel_event)
            if check_hash != src_hash:
                if backup_kept and backup_kept.exists():
                    os.replace(backup_kept, path)  # przywróć oryginał z .bak
                raise RuntimeError(
                    "round-trip: dane po pakowaniu nie zgadzają się ze źródłem "
                    f"(src={src_hash[:12]}… != new={check_hash[:12]}…) — "
                    "createdvd NIE jest bezstratny dla tego pliku, oryginał zachowany")
            log("Round-trip OK — dane identyczne bajt-w-bajt"
                + (f" (DAT: {dat_game})." if dat_game else "."))
        elif settings.verify_after_create:
            log("Weryfikacja integralności kontenera…")
            vr = chd.verify(new_file, on_progress=on_progress, cancel_event=cancel_event)
            if not vr.ok:
                if backup_kept and backup_kept.exists():
                    os.replace(backup_kept, path)
                raise RuntimeError("weryfikacja nowego CHD nie powiodła się")
        elif settings.verify_after_create:
            log("Weryfikacja nowego CHD…")
            vr = chd.verify(new_file, on_progress=on_progress, cancel_event=cancel_event)
            if not vr.ok:
                if backup_kept and backup_kept.exists():
                    os.replace(backup_kept, path)
                raise RuntimeError("weryfikacja nowego CHD nie powiodła się")

        new_size = new_file.stat().st_size
        # 5) Podmiana.
        if backup_kept:
            # oryginał już w .bak — wystarczy przenieść new_file na oryginał
            os.replace(new_file, path)
            backup_kept.unlink(missing_ok=True)
        else:
            _atomic_replace(path, new_file, keep_backup=False, log=log)

        return FixOutcome(True, path, "retype",
                          message=(f"OK, typ {current.value}→{target_media.value} "
                                   f"({old_size // 2**20}→{new_size // 2**20} MB)"),
                          budget=budget, new_size=new_size, old_size=old_size,
                          dat_game=dat_game)
    except Exception as e:
        _rollback(path, new_file, log)
        return FixOutcome(False, path, "retype", message=str(e), rolled_back=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- Konwersja nowego źródła do CHD -----------------------------------------

def create_from_source(
    chd: CHDMan,
    src: Path,
    media: MediaType,
    dst_dir: Path,
    settings: Settings,
    compression: Optional[str] = None,
    on_progress: ProgressCB = _noop_progress,
    log: LogCB = _noop_log,
    cancel_event: Optional[threading.Event] = None,
    delete_source: bool = False,
) -> FixOutcome:
    """Tworzy nowy CHD ze źródła (cue/gdi/iso/img) właściwym poleceniem."""
    create_cmd = media.create_cmd
    if not create_cmd:
        return FixOutcome(False, src, "create", message=f"nieznany typ nośnika: {media.value}")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / (src.stem + ".chd")
    log(f"Tworzenie CHD ({create_cmd}): {src.name} → {dst.name}")
    res = chd.create(create_cmd, src, dst, compression=compression,
                     threads=settings.threads, on_progress=on_progress,
                     cancel_event=cancel_event)
    if not res.ok:
        dst.unlink(missing_ok=True)
        return FixOutcome(False, src, "create",
                          message=f"{create_cmd} nie powiódł się (kod {res.returncode})")
    if settings.verify_after_create:
        log("Weryfikacja…")
        vr = chd.verify(dst, on_progress=on_progress, cancel_event=cancel_event)
        if not vr.ok:
            # Weryfikacja padła — NIE ruszamy źródeł, zostawiamy dowód.
            dst.unlink(missing_ok=True)
            return FixOutcome(False, src, "create", message="weryfikacja nie powiodła się")
    size = dst.stat().st_size

    # Usunięcie źródeł dopiero PO potwierdzonym sukcesie (create + verify).
    removed = 0
    if delete_source:
        for f in _source_companions(src):
            try:
                f.unlink(missing_ok=True)
                removed += 1
            except OSError as e:
                log(f"Nie udało się usunąć {f.name}: {e}")
        if removed:
            log(f"Usunięto źródła: {removed} plik(ów).")

    msg = f"OK ({size // 2**20} MB)"
    if removed:
        msg += f", usunięto {removed} plik(ów) źródłowych"
    return FixOutcome(True, src, "create", message=msg, new_size=size)

"""Aktualizacja gry do NOWSZEJ wersji: plik staje się źródłem prawdy, a DAT
go dogania (odwrotność zwykłego dopasowania). Podmienia plik(i) w katalogu
docelowym i AKTUALIZUJE wpis w DAT-cie (własnym, edytowalnym).

Zasady (potwierdzone z userem):
- dopasowanie po NAZWIE gry, ale z podglądem i możliwością korekty (GUI),
- stara wersja z katalogu docelowego → ToSort (nic nie ginie),
- celowana edycja DAT: podmieniamy TYLKO <rom> wybranej gry, reszta pliku
  zostaje bajt-w-bajt (plus kopia .bak dla bezpieczeństwa).
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .datfile import DatRom
from .fileindex import hash_file

LogCB = Callable[[str], None]


def roms_from_files(files, *, on_progress=None) -> List[DatRom]:
    """Liczy sumy nowej wersji: dla każdego pliku DatRom(name=nazwa, sumy)."""
    out: List[DatRom] = []
    files = list(files)
    for i, f in enumerate(files):
        f = Path(f)
        if on_progress:
            on_progress(i, len(files), f.name)
        crc, md5, sha1 = hash_file(f)
        out.append(DatRom(name=f.name, size=f.stat().st_size,
                          crc=crc, md5=md5, sha1=sha1))
    return out


def _xml_attr(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _rom_line(rom: DatRom, indent: str) -> str:
    """Linia <rom .../> w stylu DAT-a (name, size, crc, md5, sha1)."""
    parts = [f'name="{_xml_attr(rom.name)}"', f'size="{rom.size}"']
    if rom.crc:
        parts.append(f'crc="{rom.crc.lower()}"')
    if rom.md5:
        parts.append(f'md5="{rom.md5.lower()}"')
    if rom.sha1:
        parts.append(f'sha1="{rom.sha1.lower()}"')
    return f"{indent}<rom {' '.join(parts)}/>"


_ROM_RE = re.compile(r"[^\S\r\n]*<rom\b[^>]*/>[ \t]*\r?\n?", re.IGNORECASE)


def replace_game_roms_text(text: str, game_name: str,
                           new_roms: List[DatRom]) -> str:
    """Zwraca DAT z podmienionymi <rom> WYBRANEJ gry (reszta bez zmian).
    Rzuca KeyError, gdy gry nie ma. Zachowuje wcięcie i pozycję (nowe <rom>
    wchodzą w miejsce starych, tuż przed </game>/</machine>)."""
    esc = _xml_attr(game_name)
    # blok <game|machine name="..."> ... </game|machine>
    block_re = re.compile(
        r'(<(game|machine)\b[^>]*\bname="' + re.escape(esc)
        + r'"[^>]*>)(.*?)(</\2>)', re.IGNORECASE | re.DOTALL)
    m = block_re.search(text)
    if not m:
        raise KeyError(f"gra '{game_name}' nie znaleziona w DAT")
    open_tag, _tag, body, close_tag = m.group(1), m.group(2), m.group(3), m.group(4)
    # wcięcie z pierwszej istniejącej linii <rom>, inaczej wcięcie zamknięcia +\t
    rm = re.search(r"([^\S\r\n]*)<rom\b", body, re.IGNORECASE)
    indent = rm.group(1) if rm else "\t\t"
    body_no_roms = _ROM_RE.sub("", body)
    new_block = "".join(_rom_line(r, indent) + "\n" for r in new_roms)
    # wstaw nowe <rom> na końcu ciała (przed </...>), zachowując istniejące
    # nie-rom elementy (description/year/manufacturer)
    body_no_roms = body_no_roms.rstrip("\r\n")
    close_indent_m = re.search(r"([^\S\r\n]*)$", text[:m.start(4)])
    close_indent = close_indent_m.group(1) if close_indent_m else "\t"
    new_body = body_no_roms + "\n" + new_block + close_indent
    return text[:m.start()] + open_tag + new_body + close_tag + text[m.end():]


def update_dat_file(dat_path: Path, game_name: str, new_roms: List[DatRom], *,
                    dry_run: bool = False, backup: bool = True,
                    log: LogCB = lambda m: None) -> bool:
    """Podmienia <rom> gry w pliku DAT. Kopia .bak (raz). Zwraca True gdy OK."""
    dat_path = Path(dat_path)
    text = dat_path.read_text(encoding="utf-8")
    try:
        new_text = replace_game_roms_text(text, game_name, new_roms)
    except KeyError as e:
        log(f"DAT: {e}")
        return False
    if new_text == text:
        log(f"DAT: brak zmian dla '{game_name}'")
        return True
    if dry_run:
        log(f"DAT (podgląd): zaktualizowano wpis '{game_name}' "
            f"({len(new_roms)} ROM-ów)")
        return True
    if backup:
        bak = dat_path.with_suffix(dat_path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(dat_path, bak)
            log(f"DAT: kopia zapasowa {bak.name}")
    tmp = dat_path.with_name(dat_path.name + ".chdbuddy_tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, dat_path)
    log(f"DAT: zaktualizowano wpis '{game_name}' ({len(new_roms)} ROM-ów)")
    return True


@dataclass
class UpdatePlan:
    game_name: str
    dat_path: Path
    target_dir: Path
    store_format: str
    subdir: bool
    old_files: List[Path] = field(default_factory=list)   # do ToSort
    new_files: List[Path] = field(default_factory=list)    # źródło nowej wersji
    new_roms: List[DatRom] = field(default_factory=list)   # policzone sumy
    same_format: bool = True    # nowe pliki pasują do formatu docelowego


def _canonical_dest(plan: UpdatePlan, new_file: Path) -> Path:
    """Gdzie w docelowym ma trafić plik nowej wersji."""
    multi = plan.subdir and len(plan.new_files) > 1
    base = plan.target_dir / plan.game_name if multi else plan.target_dir
    return base / new_file.name


def apply_update(plan: UpdatePlan, *, index=None, tosort: Optional[Path],
                 dry_run: bool = False, delete_old: bool = False,
                 log: LogCB = lambda m: None) -> bool:
    """Wykonuje plan: stara wersja → ToSort (albo kasuj), nowe pliki → docelowy
    (gdy format się zgadza), edycja DAT. Zwraca True gdy OK."""
    # 1) STARA wersja z docelowego → ToSort (bezpiecznie) albo kasuj
    for old in plan.old_files:
        old = Path(old)
        if not old.is_file():
            continue
        if delete_old:
            log(f"KASUJ starą: {old}")
            if not dry_run:
                try:
                    old.unlink()
                    if index is not None:
                        index.remove_path(old)
                except OSError as e:
                    log(f"  nie skasowano {old}: {e}")
        else:
            if tosort is None:
                log("UWAGA: brak ToSort — starej wersji nie ruszam")
                continue
            dst = Path(tosort) / plan.target_dir.name / old.name
            n = 1
            while (not dry_run) and os.path.lexists(dst):
                dst = dst.with_name(f"{dst.stem}_{n}{dst.suffix}")
                n += 1
            log(f"STARA → ToSort: {old} -> {dst}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(old), str(dst))
                    if index is not None:
                        index.remove_path(old)
                except OSError as e:
                    log(f"  nie przeniesiono {old}: {e}")

    # 2) NOWE pliki → katalog docelowy (tylko gdy format się zgadza; inaczej
    #    zostają w źródle i user uruchomi Napraw, który je skonwertuje)
    if plan.same_format:
        for nf in plan.new_files:
            nf = Path(nf)
            dest = _canonical_dest(plan, nf)
            log(f"NOWA → docelowy: {nf} -> {dest}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(nf, dest)   # KOPIA — źródło zostaje nietknięte
                    if index is not None:
                        r = next((r for r in plan.new_roms
                                  if r.name == nf.name), None)
                        if r is not None:
                            index.record_file(dest, r.crc, r.md5, r.sha1)
                except OSError as e:
                    log(f"  BŁĄD kopiowania {nf}: {e}")
                    return False
    else:
        log("UWAGA: nowe pliki są w innym formacie niż docelowy — zostają w "
            "źródle; uruchom Napraw, by skonwertować do formatu docelowego.")

    # 3) DAT — podmień wpis gry na nowe sumy
    return update_dat_file(plan.dat_path, plan.game_name, plan.new_roms,
                           dry_run=dry_run, log=log)

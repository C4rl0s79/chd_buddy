"""Poziomy skanowania i naprawy — adaptacja modelu RomVaulta do naszego
przypadku (formaty: pliki luzem, ZIP/7z, CHD, RVZ).

RomVault steruje tym, ile ufać sumom z cache, a ile przeliczać od nowa.
U nas dochodzi specyfika CHD: żeby CHD trafił w DAT (który podaje sumy
obrazu iso/bin), trzeba znać SHA-1 ZAWARTOŚCI CHD — tanio z nagłówka
(chdman info, działa dla DVD) albo drogo przez ekstrakcję (CD bin/cue,
DVD-spakowane-jako-CD, błędnie spakowane).

── Poziomy SKANOWANIA (co trafia do indeksu) ───────────────────────────
QUICK   — tylko nowe/zmienione pliki (rozmiar+mtime). Luzem: CRC+MD5+SHA1.
          Archiwa: CRC z nagłówka (bez dekompresji). CHD: tylko suma całego
          pliku (NIE dopasuje płyt do DAT). Najszybszy — inwentaryzacja.
NORMAL  — jak QUICK + dla CHD SHA-1 ZAWARTOŚCI z nagłówka (DVD trafia w DAT).
          Sumy liczone raz i cache'owane. Zalecany.
FULL    — przelicza WSZYSTKO od nowa (wykrywa uszkodzenia/bitrot) oraz
          identyfikuje CHD EKSTRAKCJĄ (CD bin/cue, DVD-jako-CD). Najwolniejszy.

── Poziomy NAPRAWY ─────────────────────────────────────────────────────
QUICK   — tylko układa to, co już rozpoznane (przenieś/przemianuj/link).
          Wypakowanie z archiwum weryfikowane po CRC+rozmiarze.
NORMAL  — wypakowanie weryfikowane pełnym SHA-1; CHD retype z round-trip
          (bajt-w-bajt). Pliki już na miejscu — zaufane. Zalecany.
FULL    — dodatkowo RE-WERYFIKUJE pliki już na miejscu (przelicza ich sumy
          względem DAT — łapie ciche uszkodzenia). Wymaga skanu FULL.
"""
from __future__ import annotations

from enum import Enum


class ScanLevel(Enum):
    QUICK = "quick"
    NORMAL = "normal"
    FULL = "full"


class FixLevel(Enum):
    QUICK = "quick"
    NORMAL = "normal"
    FULL = "full"


# (etykieta GUI, opis)
SCAN_LEVEL_INFO = {
    ScanLevel.QUICK: ("Szybki", "Nowe/zmienione pliki; CHD po całym pliku "
                                 "(nie dopasuje płyt do DAT). Najszybszy."),
    ScanLevel.NORMAL: ("Normalny (zalecany)", "Pełne sumy + SHA-1 zawartości "
                       "CHD z nagłówka (DVD trafia w DAT). Cache'owane."),
    ScanLevel.FULL: ("Pełny / głęboki", "Przelicza wszystko od nowa (wykrywa "
                     "uszkodzenia) + identyfikuje CHD ekstrakcją (CD, "
                     "DVD-jako-CD). Najwolniejszy."),
}

FIX_LEVEL_INFO = {
    FixLevel.QUICK: ("Szybki", "Układa rozpoznane; wypakowanie po CRC+rozmiar."),
    FixLevel.NORMAL: ("Normalny (zalecany)", "Wypakowanie z pełną walidacją "
                      "SHA-1; CHD round-trip. Pliki na miejscu zaufane."),
    FixLevel.FULL: ("Pełny / weryfikuj", "Dodatkowo re-weryfikuje pliki już "
                    "na miejscu (łapie uszkodzenia). Wolny."),
}


def scan_settings(level: ScanLevel) -> tuple[bool, str]:
    """Zwraca (full_rehash, chd_mode). chd_mode: 'none'|'header'|'deep'."""
    if level is ScanLevel.QUICK:
        return False, "none"
    if level is ScanLevel.FULL:
        return True, "deep"
    return False, "header"          # NORMAL


def fix_verify_in_place(level: FixLevel) -> bool:
    """Czy re-weryfikować pliki już na miejscu (poziom FULL)."""
    return level is FixLevel.FULL

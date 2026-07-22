"""Pipeline audytu CHD.

Trzy poziomy sprawdzania (opisane w koncepcji):
  1. Integralność kontenera — chdman verify (opcjonalne, kosztowne).
  2. Poprawność typu — analiza CHDInfo: DVD-sized obraz z profilem CD => suspect.
  3. Round-trip/DAT — poza tym modułem (roadmap): testowa ekstrakcja + hash/DAT.

Ten moduł nie modyfikuje plików. Zwraca AuditResult, na podstawie którego
fixer.py buduje kolejkę napraw.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from .chdman import CHDMan
from .models import (
    AuditResult,
    AuditVerdict,
    CHDInfo,
    MediaType,
)
from .settings import Settings


def classify_info(info: CHDInfo, cd_max_logical: int) -> tuple[AuditVerdict, MediaType, str]:
    """Klasyfikuje CHD wyłącznie na podstawie metadanych (bez verify)."""
    if info.version == 0 and not info.compression and info.logical_bytes == 0:
        return AuditVerdict.UNREADABLE, MediaType.UNKNOWN, "nie udało się odczytać nagłówka CHD"

    detected = info.detected_media
    if info.is_cd_typed and info.logical_bytes > cd_max_logical:
        msg = (
            f"CHD ma profil CD (unit={info.unit_bytes}B, kodeki={','.join(info.compression)}), "
            f"ale rozmiar logiczny {info.logical_bytes / 1024 / 1024:.0f} MB przekracza "
            f"pojemność CD — prawdopodobnie DVD spakowane jako CD."
        )
        return AuditVerdict.SUSPECT_WRONG_TYPE, MediaType.DVD, msg

    return AuditVerdict.OK, detected, "profil zgodny z wykrytym typem nośnika"


def audit_chd(
    chd: CHDMan,
    path: Path,
    settings: Settings,
    do_verify: bool = False,
    on_progress: Optional[Callable[[float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> AuditResult:
    """Audytuje pojedynczy plik CHD."""
    if path.suffix.lower() != ".chd":
        return AuditResult(path, AuditVerdict.NOT_CHD, message="to nie jest plik .chd")

    info = chd.info(path)
    verdict, expected, msg = classify_info(info, settings.cd_max_logical_bytes)
    result = AuditResult(
        path=path,
        verdict=verdict,
        info=info,
        detected_media=info.detected_media,
        expected_media=expected,
        message=msg,
    )

    if do_verify and verdict != AuditVerdict.UNREADABLE:
        res = chd.verify(path, on_progress=on_progress, cancel_event=cancel_event)
        result.verify_ok = res.ok
        if not res.ok:
            # verify ma pierwszeństwo nad podejrzeniem typu
            result.verdict = AuditVerdict.VERIFY_FAILED
            tail = res.stdout.strip().splitlines()[-3:] if res.stdout else []
            result.message = "verify nie powiódł się: " + " | ".join(tail)

    return result


def audit_batch(
    chd: CHDMan,
    paths: list[Path],
    settings: Settings,
    do_verify: bool = False,
    on_item: Optional[Callable[[AuditResult], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> list[AuditResult]:
    """Audytuje listę plików; zwraca wyniki i wywołuje on_item po każdym."""
    results: list[AuditResult] = []
    for p in paths:
        if cancel_event is not None and cancel_event.is_set():
            break
        r = audit_chd(chd, p, settings, do_verify=do_verify, cancel_event=cancel_event)
        results.append(r)
        if on_item is not None:
            on_item(r)
    return results

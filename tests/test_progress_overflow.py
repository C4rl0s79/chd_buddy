"""Postęp bajtowy dużych plików (CHD/ISO > 2,1 GB) NIE może przepełnić
32-bit int paska Qt (był OverflowError). Skalowanie mieści wartości w int32
zachowując proporcję."""
from __future__ import annotations

from chd_buddy.ui.progress_dialog import ProgressDialog

_INT32_MAX = 2**31 - 1


def test_scale_small_values_unchanged():
    assert ProgressDialog._scale(50, 100) == (50, 100)
    assert ProgressDialog._scale(0, 9) == (0, 9)
    # dokładnie na progu — bez zmian
    smax = ProgressDialog._SAFE_MAX
    assert ProgressDialog._scale(smax, smax) == (smax, smax)


def test_scale_large_bytes_fit_int32():
    # 8 GB plik, w połowie — kiedyś rzucało OverflowError w Qt
    total = 8 * 1024**3
    done = total // 2
    d, t = ProgressDialog._scale(done, total)
    assert 0 <= d <= t <= _INT32_MAX
    # proporcja zachowana (~50%)
    assert abs(d / t - 0.5) < 0.001


def test_scale_full_and_zero():
    total = 5 * 1024**3
    d, t = ProgressDialog._scale(total, total)     # 100%
    assert d == t and t <= _INT32_MAX
    d0, t0 = ProgressDialog._scale(0, total)        # 0%
    assert d0 == 0 and t0 <= _INT32_MAX


def test_scale_never_exceeds_int32_for_huge_values():
    # skrajnie duży „total" (np. suma wielu płyt) — musi się zmieścić
    for total in (3 * 1024**3, 50 * 1024**3, 2**40):
        d, t = ProgressDialog._scale(total, total)
        assert d <= _INT32_MAX and t <= _INT32_MAX

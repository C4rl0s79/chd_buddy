"""Alias punktu wejścia GUI ROM Kombajnu.

`py -m chd_buddy.suite` i `py -m chd_buddy.main` uruchamiają TO SAMO okno
(SuiteWindow / ROM Kombajn). Cała logika startu (crash-handler, auto-UAC dla
symlinków, język, high-DPI) jest w `chd_buddy.main` — tu tylko delegujemy,
żeby nie utrzymywać dwóch launcherów. Klasyczne narzędzie CHD jest wbudowane
w kombajn (menu Narzędzia), nie jest osobną aplikacją.
"""
from __future__ import annotations

import sys

if __package__ in (None, ""):
    import os

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from chd_buddy.main import main

    raise SystemExit(main())


def main() -> int:
    from .main import main as _main       # jedno źródło prawdy
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())

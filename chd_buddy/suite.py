"""Punkt wejścia GUI ROM Kombajnu (okno z zakładkami).

Uruchomienie:  py -m chd_buddy.suite   (albo chd-buddy-suite po instalacji)
Klasyczne narzędzie CHD pozostaje dostępne przez chd-buddy-gui oraz z menu
Narzędzia wewnątrz kombajnu.
"""
from __future__ import annotations

import sys

if __package__ in (None, ""):
    import os

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from chd_buddy.suite import main

    raise SystemExit(main())


def main() -> int:
    from .main import _install_crash_handlers

    _install_crash_handlers()

    from PySide6.QtWidgets import QApplication

    from .core.settings import Settings
    from .ui.suite_window import SuiteWindow

    app = QApplication(sys.argv)
    app.setApplicationName("ROM Kombajn")
    settings = Settings.load()
    win = SuiteWindow(settings)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

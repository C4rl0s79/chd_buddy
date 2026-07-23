"""Punkt wejścia GUI CHD Buddy.

Można uruchomić na dwa sposoby:
  * jako moduł (zalecane):   py -m chd_buddy.main
  * bezpośrednio plikiem:    py main.py     (obsłużone bootstrapem niżej)
"""
from __future__ import annotations

import sys

if __package__ in (None, ""):
    # Uruchomiono plik bezpośrednio (np. `py main.py`) — brak kontekstu pakietu.
    # Dokładamy katalog nadrzędny do sys.path i przeładowujemy jako pakiet.
    import os

    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(_pkg_dir)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from chd_buddy.main import main  # import absolutny

    raise SystemExit(main())


def _crash_log_path() -> "os.PathLike | str":
    """Ścieżka pliku crash-logu — obok aplikacji, z fallbackiem do temp."""
    import os
    import tempfile

    base = os.path.dirname(os.path.abspath(sys.argv[0] or __file__))
    candidate = os.path.join(base, "chd_buddy_crash.log")
    try:
        with open(candidate, "a"):
            pass
        return candidate
    except OSError:
        return os.path.join(tempfile.gettempdir(), "chd_buddy_crash.log")


def _install_crash_handlers() -> None:
    """Łapie zarówno wyjątki Pythona, jak i twarde błędy C (segfault/abort).

    Bez tego nieobsłużony wyjątek w slocie Qt lub use-after-free w warstwie
    C++ zamyka proces bez śladu. Zapisujemy wszystko do pliku i, jeśli się da,
    pokazujemy okno z komunikatem.
    """
    import datetime
    import faulthandler
    import traceback

    path = _crash_log_path()

    # faulthandler: zrzuca stos także przy fatalnych błędach C (SIGSEGV itp.).
    try:
        _fh = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        _fh.write(f"\n=== start {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        faulthandler.enable(file=_fh, all_threads=True)
    except OSError:
        _fh = None

    def _hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"\n--- wyjątek {datetime.datetime.now():%H:%M:%S} ---\n{msg}\n")
        except OSError:
            pass
        sys.stderr.write(msg)
        try:  # pokaż okno, jeśli aplikacja Qt jeszcze żyje
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None, "CHD Buddy — błąd",
                    f"Wystąpił błąd:\n\n{exc}\n\nSzczegóły zapisano w:\n{path}")
        except Exception:
            pass

    sys.excepthook = _hook

    # Wyjątki w wątkach (np. QRunnable) — Python 3.8+.
    try:
        import threading

        def _thook(args):
            _hook(args.exc_type, args.exc_value, args.exc_traceback)

        threading.excepthook = _thook
    except Exception:
        pass


def _maybe_elevate(settings) -> bool:
    """Auto-restart jako administrator (UAC), gdy włączone i jeszcze nie admin.

    Zwraca True, gdy udało się wystartować podniesiony proces — WÓWCZAS bieżący
    (zwykły) proces powinien się natychmiast zakończyć. False = działamy dalej
    bez admina (Windows off, wyłączone ustawieniem, już admin, `--no-elevate`
    albo użytkownik odrzucił UAC).
    """
    import os

    if os.name != "nt":
        return False
    if not getattr(settings, "auto_elevate", True):
        return False
    if "--no-elevate" in sys.argv:
        return False
    from .core.elevate import is_admin, relaunch_as_admin
    if is_admin():
        return False
    return relaunch_as_admin()


def main() -> int:
    _install_crash_handlers()

    from .core.settings import Settings

    settings = Settings.load()
    # Język UI wg ustawień (polski = domyślny, brak tłumaczenia = fallback PL).
    from .core import i18n
    i18n.set_language(settings.language)
    # PRZED startem GUI: spróbuj podnieść uprawnienia (symlinki bez trybu dev).
    # Gdy podniesiony proces wystartował — kończymy ten (zwykły), by nie było
    # dwóch okien; odmowa UAC => lecimy dalej bez admina.
    if _maybe_elevate(settings):
        return 0

    from PySide6.QtCore import Qt, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication

    from .ui.suite_window import SuiteWindow

    # Przechwytujemy komunikaty Qt (qWarning/qCritical/qFatal) do stderr+log.
    def _qt_msg(mode, ctx, message):
        sys.stderr.write(f"[Qt] {message}\n")
        try:
            with open(_crash_log_path(), "a", encoding="utf-8",
                      errors="replace") as f:
                f.write(f"[Qt] {message}\n")
        except OSError:
            pass

    qInstallMessageHandler(_qt_msg)

    # High-DPI: w Qt6 skalowanie jest domyślnie włączone; zostawiamy zaokrąglanie.
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("ROM Kombajn")
    win = SuiteWindow(settings)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

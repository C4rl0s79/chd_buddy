"""ROM Kombajn — główne okno pakietu (drzewo kolekcji á la RomVault).

Zakładki:
  * Kolekcja (DAT) — DatRoot→RomRoot, raport per DAT z kolorami
    (zielony = komplet, żółty = do naprawy, czerwony = braki) i Napraw
    (podgląd/wykonaj) z ToSort;
  * Indeks — skan przyrostowy katalogów do bazy, duplikaty, dedup symlinkami;
  * Ikony i skróty — boxarty .ico + skróty .lnk per emulator.

Wszystkie operacje chodzą w QThreadPool (GUI nie zamiera); FileIndex
(SQLite) jest otwierany WEWNĄTRZ wątku roboczego (check_same_thread).
Klasyczne narzędzie CHD otwiera się z menu jako osobne okno.
"""
from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QComboBox,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.settings import Settings
from ..core.i18n import tr

# marker leniwego rozwijania (węzeł ma prawdziwą treść dopiero po kliknięciu)
_LAZY = "__lazy__"

# kolory statusów (pastelowe tła — czytelne w jasnym i ciemnym motywie)
_GREEN = QColor(46, 160, 67, 60)
_YELLOW = QColor(210, 153, 34, 60)
_RED = QColor(248, 81, 73, 60)


def _pulse(log: Callable[[str], None], progress=None, every: int = 200):
    """Callback postępu skanu: pasek nieokreślony + log co `every` plików."""
    def cb(n: int, path: Path) -> None:
        if progress is not None and n % 20 == 0:
            progress(0, 0, f"skan… {n} plików ({path.name})")
        if n % every == 0:
            log(f"  … {n} plików ({path.name})")
    return cb


def _chd_prober(settings, log):
    """Prober SHA-1 zawartości CHD z nagłówka (chdman info) — tani, DVD."""
    from ..core.chdman import CHDMan, CHDManNotFound
    try:
        chd = CHDMan(settings.chdman_path or None)
    except CHDManNotFound as e:
        log(f"CHD: pominięto sondę nagłówka — {e}")
        return None

    def prober(p: Path) -> str:
        try:
            i = chd.info(p)
            return i.data_sha1 or i.sha1 or ""
        except OSError:
            return ""
    return prober


def _deep_probe_gui(idx, entries, settings, chd_mode: str,
                    roots, log: Callable[[str], None], cancel=None,
                    on_progress=None, detail=None) -> None:
    """Identyfikacja CHD wg trybu: 'deep' = ekstrakcja (CD/DVD-jako-CD).
    ('header'/'none' są obsłużone przez prober przy samym skanie).
    on_progress/detail — postęp ogólny i szczegółowy (ekstrakcja chdman)."""
    if chd_mode != "deep":
        return
    from ..core.chdman import CHDMan, CHDManNotFound
    from ..core.matcher import deep_probe_chds
    try:
        chd = CHDMan(settings.chdman_path or None)
    except CHDManNotFound as e:
        log(f"CHD: pominięto identyfikację — {e}")
        return
    n = deep_probe_chds(
        idx, entries, chd, roots=[r for r in roots if r],
        work_dir=Path(settings.work_dir) if settings.work_dir else None,
        log=log, cancel_event=cancel, on_progress=on_progress, detail=detail,
        scratch_fallback=settings.scratch_dir or None)
    if detail is not None:
        detail(-1, 0, "")                 # schowaj pasek szczegółowy po CHD
    log(f"CHD zidentyfikowane (ekstrakcja): {n}")


class _FnSignals(QObject):
    log = Signal(str)
    # object (nie int!) — postęp BAJTOWY dużych plików (CHD/ISO > 2,1 GB)
    # przekracza C++ int i emit Signal(int) rzuca OverflowError. object
    # przepuszcza Python int bez konwersji; skalowanie do paska robi UI.
    progress = Signal(object, object, str)   # OGÓLNY: done, total (0=nieokreślony)
    detail = Signal(object, object, str)     # SZCZEGÓŁOWY: bieżący plik
    done = Signal(object, str)               # wynik, błąd ("" gdy ok)


class FnWorker(QRunnable):
    """Uruchamia funkcję w puli wątków; log i postęp przez sygnały.

    Funkcja dostaje (log, progress); progress(done, total, tekst) —
    total=0 oznacza pasek nieokreślony (samo „coś się dzieje").
    """

    def __init__(self, fn: Optional[Callable]):
        super().__init__()
        self.fn = fn                 # można ustawić po utworzeniu (przed start)
        self.signals = _FnSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn(self.signals.log.emit, self.signals.progress.emit)
        except Exception as e:  # nie ubijaj GUI wyjątkiem wątku
            self.signals.done.emit(None, f"{e}\n{traceback.format_exc()}")
            return
        self.signals.done.emit(result, "")


class _PathRow(QWidget):
    """Etykieta + pole ścieżki + przycisk wyboru katalogu."""

    def __init__(self, label: str, value: str = "",
                 on_change: Optional[Callable[[str], None]] = None):
        super().__init__()
        self._on_change = on_change
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(label))
        self.edit = QLineEdit(value)
        self.edit.editingFinished.connect(self._changed)
        lay.addWidget(self.edit, 1)
        btn = QPushButton("…")
        btn.setFixedWidth(28)
        btn.clicked.connect(self._browse)
        lay.addWidget(btn)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("Wybierz katalog"),
                                             self.edit.text() or "")
        if d:
            self.edit.setText(os.path.normpath(d))
            self._changed()

    def _changed(self) -> None:
        if self._on_change:
            self._on_change(self.edit.text().strip())

    @property
    def path(self) -> str:
        return self.edit.text().strip()


class SuiteWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.pool = QThreadPool.globalInstance()
        self._busy = False
        self._workers: list[FnWorker] = []   # referencje na czas życia zadań
        self.setWindowTitle(tr("ROM Kombajn — chd_buddy"))
        self.resize(1080, 720)

        central = QWidget()
        root = QVBoxLayout(central)
        self.row_workspace = _PathRow("Warsztat (katalog główny):",
                                      self.settings.workspace_dir,
                                      self._apply_workspace)
        self.row_workspace.setToolTip(tr(
            "Jeden katalog z podkatalogami: Emulatory, roms, bios, dat, "
            "to sort — ustawienie go wypełnia wszystkie ścieżki."))
        root.addWidget(self.row_workspace)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 3)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("")
        root.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText(tr("Log operacji…"))
        root.addWidget(self.log, 1)
        self.setCentralWidget(central)

        self._build_menu()
        self._build_tab_collection()
        self._build_tab_index()
        self._build_tab_art()
        self._build_tab_bios()
        self._build_tab_update()

        # AUTO-WCZYTANIE na starcie: DAT-y z cache + ostatni raport z pamięci
        if self.row_dats.path and Path(self.row_dats.path).is_dir():
            from PySide6.QtCore import QTimer
            QTimer.singleShot(150, self._collection_load_dats)

        # RAM DYSK na operacje tymczasowe — tworzymy w tle (inicjalizacja trwa),
        # by był gotowy zanim ruszy skan; usuwamy przy zamknięciu programu.
        if self.settings.ramdisk_enabled:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(50, self._init_ramdisk)

    def _init_ramdisk(self) -> None:
        # tworzymy w tle BEZ blokady przycisków/okna postępu (nie przeszkadza
        # w auto-wczytaniu DAT-ów); log leci na żywo do dolnego panelu
        from ..core import ramdisk
        if not ramdisk.available():
            self._log("RAM dysk: brak ImDisk — operacje tymczasowe pójdą na "
                      "dysk z wolnym miejscem. (zainstaluj ImDisk, by trzymać "
                      "je w RAM)")
            return
        size = int(self.settings.ramdisk_size_gb or 30)
        letter = (self.settings.ramdisk_letter or "R")[:1]
        # SYNCHRONICZNIE: jeśli RAM dysk z poprzedniej sesji już istnieje,
        # zarejestruj go OD RAZU — inaczej pierwsza naprawa/konwersja mogłaby
        # zjechać na dysk fizyczny, zanim create() w tle ustawi _ACTIVE.
        if ramdisk.reuse_if_exists(letter):
            self._log(f"RAM dysk: wykryto istniejący {letter}: — używam.")
        worker = FnWorker(
            lambda log, prog: ramdisk.create(size_gb=size, letter=letter,
                                             log=log))
        worker.signals.log.connect(self._log)
        self._workers.append(worker)
        self.pool.start(worker)

    def closeEvent(self, event) -> None:
        # usuń ulotny RAM dysk przy zamknięciu (dane tymczasowe znikają z nim)
        try:
            from ..core import ramdisk
            letter = (self.settings.ramdisk_letter or "R")[:1]
            ramdisk.remove(letter=letter, log=self._log)
        except Exception:
            pass
        super().closeEvent(event)

    def _apply_workspace(self, ws: str) -> None:
        if not ws:
            return
        if not Path(ws).is_dir():
            self._log(f"Warsztat: katalog {ws} nie istnieje.")
            return
        for msg in self.settings.apply_workspace(ws):
            self._log(f"Warsztat — {msg}")
        self.settings.save()
        # odśwież pola w zakładkach
        self.row_dats.edit.setText(self.settings.dat_root)
        self.row_roms.edit.setText(self.settings.rom_root)
        self.row_tosort.edit.setText(self.settings.tosort_dir)
        self.row_art.edit.setText(self.settings.rom_root)
        self.row_emus.edit.setText(self.settings.emulators_dir)
        self.row_bios.edit.setText(self.settings.bios_dir)

    # ── infrastruktura ────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    def _save_setting(self, name: str, value: object) -> None:
        setattr(self.settings, name, value)
        self.settings.save()

    def _run(self, fn: Callable, on_done: Callable[[object], None],
             *, title: str = "") -> None:
        """Odpala funkcję w tle; blokuje przyciski i EDYCJĘ DAT-ów na czas pracy.

        `fn` dostaje (log, progress) — a jeśli przyjmuje trzeci argument,
        także `cancel` (threading.Event) do przerwania w dowolnym momencie.
        Gdy podano `title`, pokazujemy OSOBNE OKNO POSTĘPU z logiem na żywo
        i przyciskiem Przerwij.
        """
        import inspect
        import threading
        if self._busy:
            QMessageBox.information(self, tr("Kombajn"),
                                    tr("Poczekaj na zakończenie bieżącej operacji."))
            return
        self._busy = True
        self._set_buttons_enabled(False)
        self.progress.setRange(0, 0)          # nieokreślony do 1. sygnału
        self.progress.setFormat(tr("pracuję…"))
        cancel = threading.Event()
        self._cancel_event = cancel
        dlg = None
        if title:
            from .progress_dialog import ProgressDialog
            dlg = ProgressDialog(self, title, cancel)
            dlg.show()

        # ile argumentów przyjmuje fn: (log, prog) | +cancel | +detail
        try:
            nparams = len(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            nparams = 2
        worker = FnWorker(None)
        self._workers.append(worker)
        detail_emit = worker.signals.detail.emit
        if nparams >= 4:      # (log, prog, cancel, detail)
            worker.fn = lambda log, prog: fn(log, prog, cancel, detail_emit)
        elif nparams == 3:    # (log, prog, cancel)
            worker.fn = lambda log, prog: fn(log, prog, cancel)
        else:                 # (log, prog)
            worker.fn = fn

        def _finish(result: object, err: str) -> None:
            self._busy = False
            self._cancel_event = None
            self._set_buttons_enabled(True)
            self.progress.setRange(0, 100)
            self.progress.setValue(100 if not err else 0)
            self.progress.setFormat(tr("gotowe") if not err else tr("błąd"))
            self._workers.remove(worker)
            if dlg is not None:
                dlg.finish(err)
            if err:
                self._log(f"BŁĄD: {err}")
                QMessageBox.warning(self, tr("Kombajn"), tr("Operacja nie powiodła się:") + f"\n{err}")
                return
            if cancel.is_set():
                self._log("Operacja PRZERWANA — postęp zapisany, można wznowić.")
            on_done(result)

        worker.signals.log.connect(self._log)
        worker.signals.progress.connect(self._on_progress)
        if dlg is not None:
            worker.signals.log.connect(dlg.append_log)
            worker.signals.progress.connect(dlg.set_progress)
            worker.signals.detail.connect(dlg.set_detail)
        worker.signals.done.connect(_finish)
        self.pool.start(worker)

    def _on_progress(self, done: int, total: int, text: str) -> None:
        if total > 0:
            # QProgressBar bierze 32-bit int — skaluj wielkie wartości (bajty)
            if total > 1_000_000:
                done = int(done * 1_000_000 / total)
                total = 1_000_000
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress.setFormat(f"{text}  (%v/%m)")
        else:
            self.progress.setRange(0, 0)      # tryb nieokreślony
            self.progress.setFormat(text)

    def _set_buttons_enabled(self, on: bool) -> None:
        for b in self._action_buttons:
            b.setEnabled(on)

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu(tr("Narzędzia"))
        act_glob = QAction(tr("Ustawienia globalne DAT-ów (format / nazwy / "
                              "rom_root)…"), self)
        act_glob.triggered.connect(lambda: self._folder_settings("*"))
        m.addAction(act_glob)
        act_keys = QAction(tr("Klucze API grafik (SGDB / IGDB / TheGamesDB)…"),
                           self)
        act_keys.triggered.connect(self._edit_api_keys)
        m.addAction(act_keys)
        act_ram = QAction(tr("RAM dysk (operacje tymczasowe)…"), self)
        act_ram.triggered.connect(self._edit_ramdisk)
        m.addAction(act_ram)
        act_comp = QAction(tr("Kompresja (CHD / ZIP / RVZ)…"), self)
        act_comp.triggered.connect(self._edit_compression)
        m.addAction(act_comp)
        act_lang = QAction(tr("Język / Language…"), self)
        act_lang.triggered.connect(self._edit_language)
        m.addAction(act_lang)
        m.addSeparator()
        act = QAction(tr("Klasyczne narzędzie CHD…"), self)
        act.triggered.connect(self._open_chd_tool)
        m.addAction(act)

    def _edit_api_keys(self) -> None:
        """Prosty dialog z kluczami API źródeł grafik."""
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Klucze API — źródła grafik"))
        dlg.resize(560, 220)
        form = QFormLayout(dlg)
        e_sgdb = QLineEdit(self.settings.sgdb_api_key)
        e_igdb_id = QLineEdit(self.settings.igdb_client_id)
        e_igdb_sec = QLineEdit(self.settings.igdb_client_secret)
        e_tgdb = QLineEdit(self.settings.tgdb_api_key)
        form.addRow(tr("SteamGridDB — klucz:"), e_sgdb)
        form.addRow(tr("IGDB — Client ID:"), e_igdb_id)
        form.addRow(tr("IGDB — Client Secret:"), e_igdb_sec)
        form.addRow(tr("TheGamesDB — klucz:"), e_tgdb)
        note = QLabel(tr("IGDB: dev.twitch.tv → Application (Client ID + Secret). "
                      "TheGamesDB: forums.thegamesdb.net. Puste = źródło "
                      "wyłączone. Libretro działa bez klucza."))
        note.setWordWrap(True)
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec():
            self.settings.sgdb_api_key = e_sgdb.text().strip()
            self.settings.igdb_client_id = e_igdb_id.text().strip()
            self.settings.igdb_client_secret = e_igdb_sec.text().strip()
            self.settings.tgdb_api_key = e_tgdb.text().strip()
            self.settings.save()
            self._log("Klucze API zapisane.")

    def _edit_ramdisk(self) -> None:
        """Ustawienia RAM dysku (ImDisk) na operacje tymczasowe."""
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
                                       QSpinBox)
        from ..core import ramdisk
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("RAM dysk — operacje tymczasowe"))
        dlg.resize(560, 240)
        form = QFormLayout(dlg)
        chk_en = QCheckBox(tr("Używaj RAM dysku (ImDisk) do wypakowania/przepakowania"))
        chk_en.setChecked(bool(self.settings.ramdisk_enabled))
        sp_size = QSpinBox()
        sp_size.setRange(2, 512)
        sp_size.setSuffix(" GB")
        sp_size.setValue(int(self.settings.ramdisk_size_gb or 30))
        e_letter = QLineEdit((self.settings.ramdisk_letter or "R")[:1])
        e_letter.setMaxLength(1)
        e_letter.setFixedWidth(40)
        e_scratch = _PathRow(tr("Katalog tymczasowy (fallback):"),
                             self.settings.scratch_dir)
        e_scratch.setToolTip(tr(
            "Gdy RAM dysk jest nieaktywny albo za mały na daną operację — "
            "pliki tymczasowe idą TUTAJ (a nie na dysk kolekcji). Puste = "
            "automatyczny wybór dysku z największym zapasem."))
        avail = tr("ImDisk wykryty") if ramdisk.available() else \
            tr("ImDisk NIEwykryty — operacje pójdą na dysk fizyczny")
        note = QLabel(
            f"{avail}.\n\nRAM dysk trzyma pliki tymczasowe (wypakowany obraz, "
            "przepakowanie CHD) w pamięci — nie zapycha dysku kolekcji i nic "
            "nie zostaje po przerwaniu. Kolejność scratcha: RAM dysk → katalog "
            "tymczasowy (fallback) → automat. Rozmiar musi zmieścić NAJWIĘKSZE "
            "pojedyncze wypakowanie (duże PS2/DVD ≈ 8 GB). Za duży rozmiar na "
            "mało RAM = błąd „za mało pamięci” (wtedy zmniejsz). Zmiana działa "
            "po restarcie programu.")
        note.setWordWrap(True)
        form.addRow(chk_en)
        form.addRow(tr("Rozmiar:"), sp_size)
        form.addRow(tr("Litera dysku:"), e_letter)
        form.addRow(e_scratch)
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec():
            self.settings.ramdisk_enabled = chk_en.isChecked()
            self.settings.ramdisk_size_gb = int(sp_size.value())
            self.settings.ramdisk_letter = (e_letter.text().strip() or "R")[:1].upper()
            self.settings.scratch_dir = e_scratch.path
            self.settings.save()
            self._log(f"RAM dysk: zapisano (włączony={self.settings.ramdisk_enabled}, "
                      f"{self.settings.ramdisk_size_gb} GB, "
                      f"{self.settings.ramdisk_letter}:). Zmiana po restarcie.")

    def _edit_compression(self) -> None:
        """Poziomy kompresji CHD / ZIP / RVZ (używane przy naprawie/konwersji)."""
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
                                       QComboBox, QSpinBox)
        from ..core import presets
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Kompresja (CHD / ZIP / RVZ)"))
        dlg.resize(560, 260)
        form = QFormLayout(dlg)
        cmb = QComboBox()
        cmb.addItems(presets.PRESET_NAMES)
        cmb.setCurrentText(self.settings.compression_preset)
        cmb.setToolTip(tr("CHD: dobór kodeków chdman. default = chdman decyduje; "
                          "max = najlepsza kompresja (wolniej); fast = szybciej; "
                          "none = bez kompresji."))
        sp_zip = QSpinBox(); sp_zip.setRange(0, 9)
        sp_zip.setValue(int(self.settings.zip_level))
        sp_zip.setToolTip(tr("ZIP: poziom DEFLATE. 0 = bez kompresji (szybko), "
                             "6 = domyślny, 9 = najmniejszy plik (wolniej)."))
        sp_rvz = QSpinBox(); sp_rvz.setRange(1, 22)
        sp_rvz.setValue(int(self.settings.rvz_level))
        sp_rvz.setToolTip(tr("RVZ (GameCube/Wii): poziom zstd 1–22. 5 = domyślny; "
                             "wyżej = mniejszy plik, wolniej."))
        sp_blk = QSpinBox(); sp_blk.setRange(32, 2048); sp_blk.setSingleStep(32)
        sp_blk.setSuffix(" KB"); sp_blk.setValue(int(self.settings.rvz_block_kb))
        sp_blk.setToolTip(tr("RVZ: rozmiar bloku (128 KB = domyślny)."))
        form.addRow(tr("Preset kompresji CHD:"), cmb)
        form.addRow(tr("Poziom ZIP (0–9):"), sp_zip)
        form.addRow(tr("Poziom RVZ (zstd 1–22):"), sp_rvz)
        form.addRow(tr("Blok RVZ:"), sp_blk)
        note = QLabel(tr("Poziomy działają przy naprawie/konwersji do formatu "
                         "docelowego. CHD to wybór kodeków (nie liczba). Zmiana "
                         "działa od następnej konwersji."))
        note.setWordWrap(True)
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec():
            self.settings.compression_preset = cmb.currentText()
            self.settings.zip_level = int(sp_zip.value())
            self.settings.rvz_level = int(sp_rvz.value())
            self.settings.rvz_block_kb = int(sp_blk.value())
            self.settings.save()
            self._log(tr("Kompresja zapisana:") +
                      f" CHD={self.settings.compression_preset}, "
                      f"ZIP={self.settings.zip_level}, "
                      f"RVZ=zstd{self.settings.rvz_level}/"
                      f"{self.settings.rvz_block_kb}KB.")

    def _edit_language(self) -> None:
        """Wybór języka interfejsu (zmiana po restarcie)."""
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
                                       QComboBox)
        from ..core import i18n
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Język / Language"))
        dlg.resize(460, 160)
        form = QFormLayout(dlg)
        cmb = QComboBox()
        for code, label in i18n.LANGUAGES.items():
            cmb.addItem(label, code)
        cur = i18n.get_language()
        idx = cmb.findData(cur)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow(tr("Język / Language"), cmb)
        note = QLabel(tr("Wybierz język interfejsu. Zmiana zadziała po "
                         "restarcie programu."))
        note.setWordWrap(True)
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec():
            code = cmb.currentData() or "pl"
            if code != self.settings.language:
                self.settings.language = code
                self.settings.save()
                self._log(tr("Język zapisany. Uruchom program ponownie, aby "
                             "zastosować."))
                QMessageBox.information(self, tr("Język / Language"),
                                        tr("Język zapisany. Uruchom program "
                                           "ponownie, aby zastosować."))

    def _open_chd_tool(self) -> None:
        from .main_window import MainWindow
        self._chd_win = MainWindow(self.settings)
        self._chd_win.show()

    # ── zakładka: Kolekcja (DAT) ──────────────────────────────────────────

    def _build_tab_collection(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.row_dats = _PathRow(tr("Katalog DAT-ów:"), self.settings.dat_root,
                                 lambda v: self._save_setting("dat_root", v))
        self.row_roms = _PathRow(tr("Katalog ROM-ów:"), self.settings.rom_root,
                                 lambda v: self._save_setting("rom_root", v))
        self.row_tosort = _PathRow(tr("ToSort (nieznane):"), self.settings.tosort_dir,
                                   lambda v: self._save_setting("tosort_dir", v))
        lay.addWidget(self.row_dats)
        lay.addWidget(self.row_roms)
        lay.addWidget(self.row_tosort)

        btns = QHBoxLayout()
        self.btn_load_dats = QPushButton(tr("📋 Wczytaj DAT-y"))
        self.btn_load_dats.setToolTip(tr("Pokaż drzewo DAT-ów i liczby ROM-ów "
                                      "BEZ skanowania kolekcji (szybkie)."))
        self.btn_load_dats.clicked.connect(self._collection_load_dats)
        self.btn_report = QPushButton(tr("🔍 Skanuj i raportuj"))
        self.btn_report.setToolTip(tr("Skanuje kolekcję i pokazuje per DAT: ile "
                                   "plików na miejscu, ile do naprawy, ile "
                                   "brak — statystyki widać wprost przy DAT-cie."))
        self.btn_report.clicked.connect(self._collection_report)
        self.btn_find = QPushButton(tr("🔎 Znajdź naprawy"))
        self.btn_find.setToolTip(tr(
            "Liczy PLAN naprawy z wyników ostatniego skanu — nic nie zmienia "
            "na dysku. Pokazuje dokładnie co i skąd zostanie przeniesione, "
            "przepakowane albo podlinkowane."))
        self.btn_find.clicked.connect(lambda: self._collection_fix(dry=True))
        self.btn_fix = QPushButton(tr("🔧 Napraw (wykonaj)"))
        self.btn_fix.setToolTip(tr("Wykonuje plan z ostatniego skanu — NIE skanuje "
                                "ponownie. Walidacja SHA-1 przy wypakowaniu i "
                                "round-trip przy CHD. Można przerwać w każdej "
                                "chwili; zrobione zostaje zrobione."))
        self.btn_fix.clicked.connect(lambda: self._collection_fix(dry=False))
        self.chk_clean = QCheckBox(tr("nieznane → ToSort"))
        self.chk_incomplete = QCheckBox(tr("buduj też niekompletne gry"))
        self.chk_del_tosort = QCheckBox(tr("usuń z ToSort pliki już na miejscu"))
        self.chk_del_tosort.setToolTip(tr(
            "Plik w ToSort, którego potwierdzona kopia jest już we właściwej "
            "lokalizacji (identyczny SHA-1), jest KASOWANY jako zbędny."))
        self.chk_convert = QCheckBox(tr("konwertuj do formatu docelowego"))
        self.chk_convert.setToolTip(tr(
            "Po naprawie przepakowuje pliki do formatu z reguł (kartridż→ZIP, "
            "płyta→CHD, GameCube/Wii→RVZ). Każda konwersja weryfikowana; "
            "źródło kasowane dopiero po sukcesie. Wymaga chdman/DolphinTool."))
        self.chk_dedup = QCheckBox(tr("kopie potwierdzonych → symlinki"))
        self.chk_dedup.setToolTip(tr(
            "Po naprawie fizyczne KOPIE potwierdzonych plików (w drzewie "
            "ROM-ów i ToSort) są zamieniane na symlinki — kopia fizyczna "
            "zostaje tylko w katalogu DAT-a rodzica."))
        # Opcje naprawy — TRWAŁE: wczytaj z ustawień i zapisuj przy zmianie.
        self._fix_opt_chks = {
            "fix_clean": self.chk_clean,
            "fix_incomplete": self.chk_incomplete,
            "fix_del_tosort": self.chk_del_tosort,
            "fix_convert": self.chk_convert,
            "fix_dedup": self.chk_dedup,
        }
        for _attr, _chk in self._fix_opt_chks.items():
            _chk.setChecked(bool(getattr(self.settings, _attr)))
            _chk.toggled.connect(
                lambda on, a=_attr: self._save_setting(a, on))
        from ..core.levels import FIX_LEVEL_INFO, FixLevel
        # Poziom skanu ZNIKNĄŁ — model jest automatyczny: plik ZNANY z cache
        # (ta sama ścieżka+rozmiar+mtime) jest brany z bazy, a plik NOWY albo
        # zmieniony jest zawsze liczony w pełni i — gdy to CHD — identyfikowany
        # głęboko. Wymuszenie pełnego przeliczenia to osobny przycisk poniżej.
        self.btn_force_scan = QPushButton(tr("🔄 Wymuś pełny skan katalogu…"))
        self.btn_force_scan.setToolTip(tr(
            "Przelicza sumy WSZYSTKICH plików we wskazanym katalogu, także już "
            "znanych z cache. Normalnie niepotrzebne — nowe pliki i tak są "
            "liczone w pełni automatycznie."))
        self.btn_force_scan.clicked.connect(self._force_full_scan)
        self.cmb_fix = QComboBox()
        for lvl in FixLevel:
            label, tip = FIX_LEVEL_INFO[lvl]
            self.cmb_fix.addItem(label, lvl)
            self.cmb_fix.setItemData(self.cmb_fix.count() - 1, tip,
                                     Qt.ItemDataRole.ToolTipRole)
        self.cmb_fix.setCurrentIndex(1)      # NORMAL domyślnie
        self.btn_cue_rebuild = QPushButton(tr("🔧 Odbuduj CHD wg cue"))
        self.btn_cue_rebuild.setToolTip(
            "CHD ze sklejonym układem ścieżek (zrobione ze złym/bez cue) są "
            "przebudowywane na kanoniczne: ścieżki weryfikowane SHA-1 z "
            "DAT-em, cue z biblioteki dat\\cues (paczki Redump Cuesheets — "
            "mogą zostać w zipach). Stary plik podmieniany dopiero po "
            "pełnej weryfikacji (createcd + round-trip).")
        self.btn_cue_rebuild.clicked.connect(self._rebuild_chds_cue)
        for b in (self.btn_load_dats, self.btn_report, self.btn_find,
                  self.btn_fix):
            btns.addWidget(b)
        btns.addWidget(self.btn_cue_rebuild)
        btns.addWidget(self.btn_force_scan)
        btns.addWidget(QLabel(tr("naprawa:")))
        btns.addWidget(self.cmb_fix)
        btns.addWidget(self.chk_clean)
        btns.addWidget(self.chk_incomplete)
        btns.addWidget(self.chk_del_tosort)
        btns.addWidget(self.chk_convert)
        # przycisk poziomów kompresji TUŻ przy opcji konwersji (widoczny wprost)
        self.btn_comp = QPushButton(tr("⚙ poziomy…"))
        self.btn_comp.setToolTip(tr("Poziomy kompresji CHD / ZIP / RVZ używane "
                                    "przy konwersji do formatu docelowego."))
        self.btn_comp.clicked.connect(self._edit_compression)
        btns.addWidget(self.btn_comp)
        btns.addWidget(self.chk_dedup)
        btns.addStretch()
        lay.addLayout(btns)

        # --- symlinki: stan uprawnień + jawna opcja + podniesienie (UAC) -----
        from ..core.elevate import symlink_status
        can_link, link_msg = symlink_status()
        srow = QHBoxLayout()
        self.chk_links = QCheckBox(tr("twórz symlinki dla DAT-ów dzieci"))
        self.chk_links.setChecked(can_link)
        self.chk_links.setEnabled(can_link)
        self.chk_links.setToolTip(tr(
            "DAT-y dzieci dostają symlinki do plików rodzica (jedna kopia "
            "fizyczna). Gdy symlinków NIE DA SIĘ utworzyć, nic nie jest "
            "kopiowane — te miejsca zostają puste."))
        self.lbl_links = QLabel(link_msg)
        self.lbl_links.setWordWrap(True)
        self.lbl_links.setStyleSheet(
            "color: %s;" % ("green" if can_link else "#b00"))
        self.btn_admin = QPushButton(tr("🛡 Uruchom jako administrator"))
        self.btn_admin.setToolTip(tr(
            "Restart programu z podniesionymi uprawnieniami (UAC), by móc "
            "tworzyć symlinki. Alternatywa: włącz tryb dewelopera Windows."))
        self.btn_admin.clicked.connect(self._relaunch_admin)
        self.btn_admin.setVisible(not can_link)
        # AUTO-ADMIN: przy starcie program prosi o podniesienie (UAC). Trwałe.
        self.chk_admin_auto = QCheckBox(tr("uruchamiaj jako administrator (auto)"))
        self.chk_admin_auto.setChecked(bool(self.settings.auto_elevate))
        self.chk_admin_auto.setToolTip(tr(
            "Przy każdym starcie program prosi o podniesienie uprawnień (UAC), "
            "aby móc tworzyć symlinki bez trybu dewelopera. Odmowa UAC = "
            "program działa dalej bez admina. Zmiana od następnego startu."))
        self.chk_admin_auto.toggled.connect(
            lambda on: self._save_setting("auto_elevate", on))
        srow.addWidget(self.chk_links)
        srow.addWidget(self.chk_admin_auto)
        srow.addWidget(self.btn_admin)
        srow.addWidget(self.lbl_links, 1)
        lay.addLayout(srow)

        hier = QHBoxLayout()
        self.btn_hier = QPushButton(tr("⚙ Zależności DAT-ów (rodzic/dziecko)…"))
        self.btn_hier.setToolTip(tr("Osobne okno: DAT-y pogrupowane po PLATFORMIE. "
                                 "W obrębie platformy ustalasz, który DAT jest "
                                 "rodzicem (trzyma pliki fizycznie), a które "
                                 "dziećmi (symlinki). Zapisuje _priorytet.txt."))
        self.btn_hier.clicked.connect(self._open_hierarchy_dialog)
        self.btn_datsettings = QPushButton(tr("⚙ Ustawienia zaznaczonego DAT-a…"))
        self.btn_datsettings.setToolTip(tr("Katalog docelowy, układ (podkatalog/"
                                        "płasko), format przechowywania, reguły."))
        self.btn_datsettings.clicked.connect(self._open_dat_settings)
        hier.addWidget(self.btn_hier)
        hier.addWidget(self.btn_datsettings)
        hier.addStretch()
        lay.addLayout(hier)

        # 3 panele obok siebie (RomVault): DAT-y | gry | pliki
        split = QSplitter(Qt.Orientation.Horizontal)

        self.tree = QTreeWidget()      # panel 1: DAT-y (grupy katalogów)
        self.tree.setHeaderLabels(["DAT / katalog", "gier", "komplet",
                                   "do naprawy", "brak"])
        self.tree.setColumnWidth(0, 340)
        for c in range(1, 5):
            self.tree.setColumnWidth(c, 70)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        # zaznaczanie wielu DAT-ów (Ctrl/Shift) do zbiorczej edycji ustawień
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemExpanded.connect(self._tree_expanded)
        self.tree.currentItemChanged.connect(self._on_dat_selected)
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        self._filling = False

        # panel 2: gry wybranego DAT-a + wybór sortowania
        mid = QWidget()
        mid_lay = QVBoxLayout(mid)
        mid_lay.setContentsMargins(0, 0, 0, 0)
        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel(tr("Sortuj gry:")))
        self.game_sort = QComboBox()
        self.game_sort.addItem(tr("alfabetycznie"), "alpha")
        self.game_sort.addItem(tr("rozmiar (malejąco)"), "size")
        self.game_sort.currentIndexChanged.connect(
            lambda _i: self._on_dat_selected(self.tree.currentItem(), None))
        sort_row.addWidget(self.game_sort)
        sort_row.addWidget(QLabel(tr("Pokaż:")))
        self.game_filter = QComboBox()
        self.game_filter.addItem(tr("wszystkie"), "all")
        self.game_filter.addItem(tr("✅ tylko komplet"), "complete")
        self.game_filter.addItem(tr("🔧 tylko do naprawy"), "fix")
        self.game_filter.addItem(tr("⛔ tylko brakujące"), "missing")
        self.game_filter.addItem(tr("◐ niekompletne (brak części plików)"), "partial")
        self.game_filter.setToolTip(tr(
            "Filtruje listę gier po stanie z ostatniego skanu."))
        self.game_filter.currentIndexChanged.connect(
            lambda _i: self._on_dat_selected(self.tree.currentItem(), None))
        sort_row.addWidget(self.game_filter)
        sort_row.addStretch()
        mid_lay.addLayout(sort_row)
        self.game_list = QTreeWidget()
        self.game_list.setHeaderLabels(["Gra", "plików", "status"])
        self.game_list.setColumnWidth(0, 320)
        self.game_list.setRootIsDecorated(False)
        self.game_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.game_list.customContextMenuRequested.connect(self._game_menu)
        self.game_list.currentItemChanged.connect(self._on_game_selected)
        mid_lay.addWidget(self.game_list, 1)

        self.rom_list = QTreeWidget()   # panel 3: pliki wybranej gry + sumy
        self.rom_list.setHeaderLabels(["Plik", "rozmiar", "CRC / SHA-1",
                                       "status", "naprawa (skąd)"])
        self.rom_list.setColumnWidth(0, 280)
        self.rom_list.setColumnWidth(3, 80)
        self.rom_list.setRootIsDecorated(False)

        split.addWidget(self.tree)
        split.addWidget(mid)
        split.addWidget(self.rom_list)
        split.setSizes([420, 380, 420])
        lay.addWidget(split, 1)
        self.tabs.addTab(w, tr("Kolekcja (DAT)"))

        # pamięć raportu: id(DatEntry) -> DatReport (statusy per ROM)
        self._reports_by_id: dict = {}
        # PRZEPIS: raporty z ostatniego skanu (naprawa z nich korzysta,
        # nie skanuje ponownie) + wynik ostatniego „Znajdź naprawy"
        self._reports: Optional[list] = None
        self._plan = None
        self._cancel_event = None
        # wczytany z cache stan raportu: dat_abspath -> {gra: {rom: RomState}}
        self._saved_states: dict = {}
        self._saved_at: Optional[str] = None

    def _dat_key(self, entry) -> str:
        return str(Path(os.path.abspath(entry.dat_path)))

    def _game_statuses_for(self, entry):
        """{gra: {rom_lower: status}} — z żywego raportu ALBO z cache.
        status ma pola .state/.source_path/.member/.via_chd (kolory + akcja)."""
        rep = self._reports_by_id.get(id(entry))
        if rep is not None:
            m: dict = {}
            for s in rep.statuses:
                m.setdefault(s.game, {})[s.rom.name.lower()] = s
            return m
        return self._saved_states.get(self._dat_key(entry))

    def _game_states_for(self, entry):
        """Mapa {gra: {rom_lower: RomState}} (same stany — do kolorów/liczb)."""
        statuses = self._game_statuses_for(entry)
        if not statuses:
            return statuses
        return {g: {rn: s.state for rn, s in roms.items()}
                for g, roms in statuses.items()}

    def _rom_statuses_for(self, entry):
        """{gra: {rom_lower: status}} — do pokazania AKCJI naprawy (żywy
        raport lub cache; cache carries source/member od wersji 2)."""
        return self._game_statuses_for(entry)

    @staticmethod
    def _display_order(items, dat_root: Path, key):
        """Kolejność WYŚWIETLANIA: alfabetycznie w obrębie katalogu
        (grupy zostają razem). Priorytet parent→child jest niezależny —
        wynika z reguł/wielkości, nie z kolejności w drzewie."""
        def sort_key(x):
            entry = key(x)
            try:
                rel = str(entry.dat_path.parent.relative_to(dat_root)).lower()
            except ValueError:
                rel = ""
            return (rel, entry.name.lower())
        return sorted(items, key=sort_key)

    def _group_parent(self, dat_root: Path, dat_path: Path,
                      groups: dict) -> Optional[QTreeWidgetItem]:
        """Węzeł-grupa odzwierciedlający katalog DAT-a względem dat_root
        (tworzony w razie potrzeby). None => DAT leży w korzeniu."""
        try:
            parts = dat_path.parent.relative_to(dat_root).parts
        except ValueError:
            parts = ()
        parent = None
        key: tuple = ()
        for part in parts:
            key = key + (part,)
            node = groups.get(key)
            if node is None:
                node = QTreeWidgetItem([f"📁 {part}", "", "", "", ""])
                f = node.font(0)
                f.setBold(True)
                node.setFont(0, f)
                # checkbox grupy: zaznacza/odznacza WSZYSTKIE DAT-y w środku
                # (AutoTristate: klik na grupie ustawia dzieci, stan dzieci
                # ustawia grupę — pełne/częściowe/puste)
                node.setFlags(node.flags() | Qt.ItemFlag.ItemIsUserCheckable
                              | Qt.ItemFlag.ItemIsAutoTristate)
                node.setCheckState(0, Qt.CheckState.Checked)
                # ścieżka folderu (klucz reguły) jako string na węźle grupy
                node.setData(0, Qt.ItemDataRole.UserRole, "/".join(key))
                if parent is None:
                    self.tree.addTopLevelItem(node)
                else:
                    parent.addChild(node)
                groups[key] = node
            parent = node
        return parent

    def _tree_expanded(self, item: QTreeWidgetItem) -> None:
        """Grupy-katalogi w lewym panelu są rozwinięte od razu; DAT-y są
        liśćmi (gry pokazują się w środkowym panelu po kliknięciu DAT-a)."""
        return

    # ── panel 1 → 2: gry wybranego DAT-a ──────────────────────────────────

    @staticmethod
    def _game_state_maps(report):
        """Z DatReport: {gra: {nazwa_romu: RomState}} oraz {gra: najgorszy}."""
        from ..core.matcher import RomState
        rank = {RomState.MISSING: 3, RomState.NO_HASH: 3,
                RomState.WRONG_NAME: 2, RomState.ELSEWHERE: 2,
                RomState.HAVE: 1, RomState.HAVE_CHD: 1}
        maps: dict = {}
        worst: dict = {}
        for s in report.statuses:
            maps.setdefault(s.game, {})[s.rom.name.lower()] = s.state
            cur = worst.get(s.game)
            if cur is None or rank[s.state] > rank[cur]:
                worst[s.game] = s.state
        return maps, worst

    def _sorted_games(self, games):
        """Gry posortowane wg wyboru użytkownika, ZAWSZE z grupowaniem
        wielodyskowym: dyski tej samej gry sąsiadują (po numerze dysku)."""
        from ..core.playlists import DISC_RE, strip_disc
        mode = self.game_sort.currentData()

        def disc_no(g):
            m = DISC_RE.search(g.name)
            return int(m.group(1)) if m else 0

        grouped: dict = {}
        for g in games:
            grouped.setdefault(strip_disc(g.name).lower(), []).append(g)
        if mode == "size":
            keys = sorted(grouped, key=lambda k: -sum(
                sum(r.size for r in g.roms) for g in grouped[k]))
        else:
            keys = sorted(grouped)
        out = []
        for k in keys:
            out.extend(sorted(grouped[k], key=disc_no))
        return out

    def _on_dat_selected(self, current, _previous) -> None:
        from ..core.datstore import DatEntry
        self.game_list.clear()
        self.rom_list.clear()
        entry = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        if not isinstance(entry, DatEntry):
            return
        from ..core.matcher import RomState
        maps = self._game_states_for(entry) or {}
        statuses = self._rom_statuses_for(entry)   # pełne RomStatus (akcje)
        rank = {RomState.MISSING: 3, RomState.NO_HASH: 3,
                RomState.WRONG_NAME: 2, RomState.ELSEWHERE: 2,
                RomState.HAVE: 1, RomState.HAVE_CHD: 1}
        worst = {g: max(roms.values(), key=lambda st: rank[st])
                 for g, roms in maps.items()}
        want = self.game_filter.currentData()
        self.game_list.setUpdatesEnabled(False)
        LIMIT = 50000
        shown = 0
        for game in self._sorted_games(entry.games):
            if shown >= LIMIT:
                self.game_list.addTopLevelItem(QTreeWidgetItem(
                    ["… (dalsze pozycje ukryte — zawęź filtrem)", "", ""]))
                break
            stt = worst.get(game.name)
            roms = maps.get(game.name) or {}
            have_any = any(s in (RomState.HAVE, RomState.HAVE_CHD)
                           for s in roms.values())
            if stt in (RomState.HAVE, RomState.HAVE_CHD):
                col, note, kind = _GREEN, "komplet", "complete"
            elif stt in (RomState.WRONG_NAME, RomState.ELSEWHERE):
                col, note, kind = _YELLOW, "do naprawy", "fix"
            elif stt in (RomState.MISSING, RomState.NO_HASH):
                if have_any:      # część plików jest, część brakuje
                    col, note, kind = _YELLOW, "niekompletna", "partial"
                else:
                    col, note, kind = _RED, "brak", "missing"
            else:
                col, note, kind = None, "", "unknown"
            if want != "all" and kind != want:
                continue
            shown += 1
            it = QTreeWidgetItem([game.name, str(len(game.roms)), note])
            it.setData(0, Qt.ItemDataRole.UserRole, game)
            it.setData(0, Qt.ItemDataRole.UserRole + 1, maps.get(game.name))
            it.setData(0, Qt.ItemDataRole.UserRole + 2,
                       statuses.get(game.name) if statuses else None)
            if col is not None:
                for c in range(3):
                    it.setBackground(c, QBrush(col))
            self.game_list.addTopLevelItem(it)
        self.game_list.setUpdatesEnabled(True)

    # ── panel 2 → 3: pliki wybranej gry z sumami kontrolnymi ──────────────

    @staticmethod
    def _fix_action(s) -> str:
        """Opis, NA CZYM polega naprawa danego pliku (skąd trafi na miejsce)."""
        from ..core.matcher import RomState
        if s is None or s.state in (RomState.HAVE, RomState.HAVE_CHD):
            return ""
        if s.state in (RomState.MISSING, RomState.NO_HASH):
            return "brak źródła — nie da się naprawić"
        src = Path(s.source_path)
        if getattr(s, "via_archive", False):
            if not getattr(s, "archive_names_ok", True):
                return (f"przepakuj {src.name} — zła nazwa w archiwum "
                        f"(zawartość OK)")
            return f"przenieś całe archiwum {src.name} (bez wypakowania)"
        if s.member:
            return f"wypakuj z {src.name} :: {s.member}"
        if s.via_chd:
            return f"z CHD {src.name} (przemianuj na kanoniczną nazwę)"
        if s.state == RomState.WRONG_NAME:
            return f"przemianuj / przenieś z {src.name} (ten sam katalog)"
        return f"przenieś z {s.source_path}"

    def _on_game_selected(self, current, _previous) -> None:
        from ..core.datfile import DatGame
        from ..core.matcher import RomState
        self.rom_list.clear()
        game = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        if not isinstance(game, DatGame):
            return
        state_map = current.data(0, Qt.ItemDataRole.UserRole + 1)
        status_map = current.data(0, Qt.ItemDataRole.UserRole + 2)
        for rom in game.roms:
            size = f"{rom.size:,} B".replace(",", " ") if rom.size else ""
            sums = "  ".join(x for x in (
                f"CRC {rom.crc}" if rom.crc else "",
                f"SHA1 {rom.sha1[:16]}…" if rom.sha1 else "") if x)
            it = QTreeWidgetItem([f"📄 {rom.name}", size, sums, "", ""])
            it.setToolTip(0, f"{rom.name}\nrozmiar: {rom.size} B\n"
                             f"CRC32: {rom.crc}\nMD5: {rom.md5}\n"
                             f"SHA-1: {rom.sha1}")
            if state_map is not None:
                st = state_map.get(rom.name.lower())
                if st in (RomState.HAVE, RomState.HAVE_CHD):
                    col, txt = _GREEN, "jest"
                elif st in (RomState.WRONG_NAME, RomState.ELSEWHERE):
                    col, txt = _YELLOW, "do naprawy"
                else:
                    col, txt = _RED, "brak"
                it.setText(3, txt)
                if status_map is not None:
                    action = self._fix_action(status_map.get(rom.name.lower()))
                    it.setText(4, action)
                    if action:
                        it.setToolTip(4, action)
                for c in range(5):
                    it.setBackground(c, QBrush(col))
            self.rom_list.addTopLevelItem(it)

    def _game_menu(self, pos) -> None:
        from ..core.datfile import DatGame
        it = self.game_list.itemAt(pos)
        if it is None:
            return
        game = it.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(game, DatGame):
            return
        entry = self._current_entry()
        if entry is None:
            return
        menu = QMenu(self)
        act = menu.addAction(tr("🖼 Stwórz ikonę:") + f" {game.name}…")
        act.triggered.connect(lambda: self._icon_for_game(entry, game.name))
        menu.exec(self.game_list.viewport().mapToGlobal(pos))

    def _current_entry(self):
        from ..core.datstore import DatEntry
        it = self.tree.currentItem()
        d = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        return d if isinstance(d, DatEntry) else None

    def _collection_load_dats(self) -> None:
        """Szybkie wczytanie samego drzewa DAT-ów (bez skanowania ROM-ów)."""
        dats = self.row_dats.path
        if not dats or not Path(dats).is_dir():
            QMessageBox.warning(self, tr("Kombajn"),
                                tr("Wskaż istniejący katalog DAT-ów."))
            return
        roms = self.row_roms.path or dats

        def job(log: Callable[[str], None], progress):
            from ..core.datstore import DatStore
            from ..core.dirrules import DirRules, apply_rule_targets
            entries = DatStore(dats, roms).discover(
                log=log, on_progress=lambda i, n, t:
                    progress(i, n, f"wczytuję DAT: {t}"))
            rules = DirRules(dats)
            # skip NIE usuwa DAT-a z drzewa — zostaje SZARY z odznaczonym
            # checkboxem (inaczej nie da się go z powrotem włączyć!);
            # ze skanowania wyklucza go dopiero raport.
            skipped = {e.name for e in entries if rules.for_entry(e)["skip"]}
            apply_rule_targets(entries, rules, roms, log=log)
            log(f"Wczytano {len(entries)} DAT-ów"
                + (f" (wyłączonych skip: {len(skipped)} — szare, "
                   f"zaznacz checkbox by włączyć)" if skipped else ""))
            return entries

        self._run(job, self._fill_dats_loaded)

    def _fill_dats_loaded(self, entries) -> None:
        """Wczytanie bez skanu: lewy panel DAT-ów; kliknij DAT → gry → pliki.
        Jeśli jest zapamiętany raport — pokaż ostatni znany stan (kolory)."""
        self._reports_by_id = {}
        self._entries = entries
        from ..core.datcache import load_report_states
        self._saved_at, self._saved_states = load_report_states()
        has_saved = bool(self._saved_states)
        self._fill_dats(entries, with_stats=has_saved)
        extra = (f" Pokazuję ostatni skan z {self._saved_at} — Skanuj i "
                 f"raportuj odświeży." if has_saved else
                 " Przycisk Skanuj i raportuj doda statusy jest/brak.")
        self._log(f"Wczytano {len(entries)} DAT-ów.{extra}")

    # ── osobne okno: zależności rodzic → dzieci PER PLATFORMA ─────────────

    def _open_hierarchy_dialog(self) -> None:
        entries = getattr(self, "_entries", None)
        if not entries:
            QMessageBox.information(self, tr("Zależności"),
                                   tr("Najpierw wczytaj DAT-y (przycisk Wczytaj "
                                   "DAT-y albo Skanuj i raportuj)."))
            return
        from .hierarchy_dialog import HierarchyDialog
        dlg = HierarchyDialog(self, entries, Path(self.row_dats.path),
                              Path(self.row_roms.path))
        if dlg.exec():
            self._log(f"Zależności/katalogi zapisane. Kolejny raport/naprawa "
                      f"użyje nowych ustawień.")

    def _open_dat_settings(self) -> None:
        entry = self._current_entry()
        if entry is None:
            QMessageBox.information(self, tr("Ustawienia DAT-a"),
                                   tr("Zaznacz DAT w lewym panelu."))
            return
        from .dat_settings_dialog import DatSettingsDialog
        dlg = DatSettingsDialog(self, entry, Path(self.row_dats.path),
                                Path(self.row_roms.path))
        if dlg.exec():
            self._log(f"Ustawienia DAT-a {entry.name} zapisane w _reguly.json.")

    # ── menu kontekstowe: ikony per gra / per DAT ─────────────────────────

    def _tree_menu(self, pos) -> None:
        from ..core.datfile import DatGame
        from ..core.datstore import DatEntry
        if self._busy:
            # G: w trakcie skanu/naprawy NIE wolno zmieniać ustawień DAT-ów —
            # plan i wyniki dotyczą stanu sprzed zmiany.
            QMessageBox.information(
                self, tr("Trwa operacja"),
                tr("W trakcie skanowania/naprawy nie można zmieniać ustawień "
                   "DAT-ów.\nPo zmianie ustawień zrób ponownie skan (szybki) "
                   "i Znajdź naprawy."))
            return
        it = self.tree.itemAt(pos)
        if it is None:
            return
        data = it.data(0, Qt.ItemDataRole.UserRole)
        # węzeł ToSort => własne menu (skan / dodanie / usunięcie katalogu)
        if isinstance(data, tuple) and data and data[0] == "tosort":
            _tag, ts_path, ts_idx = data
            menu = QMenu(self)
            a_scan = menu.addAction(tr("🔄 Wymuś pełny skan tego katalogu"))
            a_scan.triggered.connect(
                lambda: self._force_full_scan_path(ts_path))
            a_add = menu.addAction(tr("➕ Dodaj kolejny katalog ToSort…"))
            a_add.triggered.connect(self._add_tosort_extra)
            if ts_idx == 0:
                a_move = menu.addAction(tr("📁 Zmień lokalizację głównego ToSort…"))
                a_move.triggered.connect(self._change_primary_tosort)
            else:
                a_del = menu.addAction(tr("➖ Usuń ten katalog z listy ToSort"))
                a_del.triggered.connect(
                    lambda: self._remove_tosort_extra(ts_path))
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        # zaznaczono kilka DAT-ów? => zbiorcza edycja ustawień
        sel = [d for d in (i.data(0, Qt.ItemDataRole.UserRole)
                           for i in self.tree.selectedItems())
               if isinstance(d, DatEntry)]
        if len(sel) > 1:
            menu = QMenu(self)
            a = menu.addAction(tr("⚙ Ustawienia") + f" {len(sel)} " + tr("zaznaczonych DAT-ów…"))
            a.triggered.connect(lambda: self._multi_dat_settings(sel))
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        menu = QMenu(self)
        if isinstance(data, str):                 # węzeł-katalog (grupa)
            a_folder = menu.addAction(tr("⚙ Ustawienia katalogu") + f" {data} "
                                      + tr("(wszystkie DAT-y)…"))
            a_folder.triggered.connect(lambda: self._folder_settings(data))
            a_parent = menu.addAction(tr("⭐ Wszystkie DAT-y tu = rodzice platform"))
            a_parent.triggered.connect(lambda: self._folder_all_parents(data))
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if not isinstance(data, DatEntry):
            return
        menu = QMenu(self)
        a_set = menu.addAction(tr("⚙ Ustawienia DAT-a…"))
        a_set.triggered.connect(lambda: self._dat_settings_for(data))
        a_hier = menu.addAction(tr("⚙ Zależności (rodzic/dziecko)…"))
        a_hier.triggered.connect(self._open_hierarchy_dialog)
        menu.addSeparator()
        a_scan = menu.addAction(tr("🔄 Wymuś pełny skan katalogu tego DAT-a"))
        a_scan.triggered.connect(
            lambda: self._force_full_scan_path(str(data.target_dir)))
        a_ico = menu.addAction(tr("🖼 Generuj ikony dla całego DAT-a"))
        a_ico.triggered.connect(lambda: self._icons_for_dat(data))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _folder_settings(self, folder_key: str) -> None:
        from .folder_settings_dialog import FolderSettingsDialog
        dlg = FolderSettingsDialog(self, folder_key, Path(self.row_dats.path),
                                   Path(self.row_roms.path))
        if dlg.exec():
            self._log(f"Ustawienia katalogu {folder_key} zapisane "
                      f"(_reguly.json). Skanuj i raportuj, by zastosować.")

    def _folder_all_parents(self, folder_key: str) -> None:
        from ..core.dirrules import save_rule
        try:
            save_rule(Path(self.row_dats.path), folder_key,
                      {"parent_priority": True})
        except OSError as e:
            self._log(f"BŁĄD zapisu: {e}")
            return
        self._log(f"Katalog {folder_key}: wszystkie DAT-y = rodzice swoich "
                  f"platform. Skanuj i raportuj, by zastosować.")

    def _dat_settings_for(self, entry) -> None:
        from .dat_settings_dialog import DatSettingsDialog
        from ..core.datstore import effective_platform_key
        from ..core.dirrules import DirRules
        # rodzic platformy = pierwszy wpis tej platformy w kolejności discover
        # (self._entries posortowane rodzic→dzieci). Format blokujemy dziecku
        # TYLKO gdy RODZIC ma jawną regułę formatu (wtedy dziecko = symlink,
        # ten sam kontener). Gdy rodzic nie ma reguły, dziecko może format ustalić.
        rules = DirRules(Path(self.row_dats.path))
        pk = effective_platform_key(entry, rules)
        same = [e for e in getattr(self, "_entries", [])
                if effective_platform_key(e, rules) == pk]
        is_child = bool(same) and same[0] is not entry
        parent = same[0] if same else entry
        parent_has_fmt = rules.explicit_rule(parent, "format") is not None
        lock_format = is_child and parent_has_fmt
        dlg = DatSettingsDialog(self, entry, Path(self.row_dats.path),
                                Path(self.row_roms.path),
                                is_child=lock_format,
                                inherited_format=parent.store_format)
        if dlg.exec():
            self._log(f"Ustawienia DAT-a {entry.name} zapisane. "
                      f"Skanuj i raportuj, by zastosować.")

    def _rebuild_chds_cue(self) -> None:
        """Odbudowa CHD ze sklejonym układem ścieżek wg cue z dat\\cues."""
        paths = self._collection_paths()
        if paths is None:
            return
        dats, roms = paths
        entries = [e for e in (self._entries or [])
                   if getattr(e, "store_format", "") == "chd"]
        if not entries:
            QMessageBox.information(
                self, tr("Odbudowa CHD"),
                tr("Najpierw Wczytaj DAT-y / Skanuj — potrzebna lista DAT-ów "
                "z formatem CHD."))
            return
        cues_dir = Path(dats) / "cues"
        if not cues_dir.is_dir():
            QMessageBox.warning(
                self, tr("Odbudowa CHD"),
                tr("Brak biblioteki cue:") + f" {cues_dir}\n"
                + tr("Wrzuć tam paczki Redump Cuesheets (mogą zostać w zipach)."))
            return
        ok = QMessageBox.question(
            self, tr("Odbudowa CHD wg cue"),
            tr("Dla każdej gry z formatem CHD: jeśli plik <gra>.chd ma INNĄ "
            "liczbę ścieżek niż DAT (sklejony — zrobiony ze złym/bez cue), "
            "zostanie PRZEBUDOWANY: ścieżki zweryfikowane SHA-1 z DAT-em, "
            "cue z biblioteki, createcd + round-trip. Stary plik podmieniany "
            "dopiero po pełnej weryfikacji. Można przerwać w każdej chwili.")
            + "\n\n" + tr("Wykonać?"))
        if ok != QMessageBox.StandardButton.Yes:
            return
        db = self.settings.index_db_path or None
        settings = self.settings

        def job(log: Callable[[str], None], progress, cancel, detail):
            from ..core.chdman import CHDMan, CHDManNotFound
            from ..core.chdrebuild import rebuild_bad_chds
            from ..core.cuelib import CueLibrary
            from ..core.fileindex import FileIndex
            try:
                chd = CHDMan(settings.chdman_path or None)
            except CHDManNotFound as e:
                log(f"BŁĄD: {e}")
                return None
            lib = CueLibrary(cues_dir, log=log)
            if not len(lib):
                log("Biblioteka cue jest pusta — nic do zrobienia.")
                return None
            with FileIndex(Path(db) if db else None) as idx:
                # także ZIDENTYFIKOWANE CHD leżące jeszcze w ToSort —
                # kontener naprawiany w miejscu, przenosiny robi naprawa
                extra = [t for t in settings.tosort_dirs
                         if t and Path(t).is_dir()]
                return rebuild_bad_chds(entries, lib, chd, settings, idx,
                                        extra_roots=extra, log=log,
                                        on_progress=progress, detail=detail,
                                        cancel=cancel)

        def done(st) -> None:
            if st is not None:
                self._log(f"[ODBUDOWA CHD] {st.summary()}")

        self._run(job, done, title=tr("Odbudowa CHD wg cue"))

    def _force_full_scan(self) -> None:
        """Wymuszone PEŁNE przeliczenie sum we wskazanym katalogu (także dla
        plików już znanych z cache). Normalny skan tego nie robi — nowe pliki
        i tak liczy w pełni, a znanym ufa."""
        start = self.row_roms.path or self.row_tosort.path or ""
        d = QFileDialog.getExistingDirectory(
            self, tr("Katalog do pełnego przeliczenia"), start)
        if d:
            self._force_full_scan_path(d)

    def _force_full_scan_path(self, d: str) -> None:
        """Pełny skan KONKRETNEGO katalogu (prawy klik na ToSort/DAT-cie)."""
        if not d or not Path(d).is_dir():
            QMessageBox.warning(self, tr("Pełny skan"),
                                tr("Katalog nie istnieje:") + f"\n{d}")
            return
        ok = QMessageBox.question(
            self, tr("Pełny skan"),
            tr("Przeliczyć sumy WSZYSTKICH plików w:") + f"\n{d}\n\n"
            + tr("Może to potrwać długo (czyta każdy plik). Pliki nie są "
                 "zmieniane — aktualizowany jest tylko indeks. Można przerwać "
                 "w każdej chwili (postęp zapisany)."))
        if ok != QMessageBox.StandardButton.Yes:
            return
        db = self.settings.index_db_path or None
        settings = self.settings

        def job(log: Callable[[str], None], progress, cancel):
            from ..core.fileindex import FileIndex
            with FileIndex(Path(db) if db else None) as idx:
                prober = _chd_prober(settings, log)
                st = idx.scan(Path(d), full=True, chd_prober=prober,
                              on_file=_pulse(log, progress), cancel=cancel)
                log(f"PEŁNY skan {d}: {st.summary()}")
                return st.summary()

        self._run(job, lambda s: self._log(f"[PEŁNY SKAN] {s}"),
                  title=f"Pełny skan: {d}")

    # ── zarządzanie katalogami ToSort (jak w RomVaulcie) ──────────────────

    def _add_tosort_extra(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, tr("Dodatkowy katalog ToSort"), self.row_tosort.path or "")
        if not d:
            return
        if d in self.settings.tosort_dirs:
            QMessageBox.information(self, tr("ToSort"),
                                    tr("Ten katalog już jest na liście."))
            return
        self.settings.tosort_extra = list(self.settings.tosort_extra or [])
        self.settings.tosort_extra.append(d)
        self.settings.save()
        self._log(f"Dodano katalog ToSort: {d}")
        self._refresh_tree_only()

    def _remove_tosort_extra(self, path: str) -> None:
        # usuwamy TYLKO z listy — katalog i pliki zostają nietknięte
        self.settings.tosort_extra = [
            t for t in (self.settings.tosort_extra or []) if t != path]
        self.settings.save()
        self._log(f"Usunięto z listy ToSort (pliki nietknięte): {path}")
        self._refresh_tree_only()

    def _change_primary_tosort(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, tr("Główny katalog ToSort (tam trafiają nieznane)"),
            self.row_tosort.path or "")
        if not d:
            return
        self.row_tosort.edit.setText(d)
        self._save_setting("tosort_dir", d)
        self._log(f"Główny ToSort: {d}")
        self._refresh_tree_only()

    def _refresh_tree_only(self) -> None:
        """Przerysowuje drzewo z bieżących danych (bez skanowania)."""
        if getattr(self, "_entries", None):
            self._fill_dats(self._entries,
                            with_stats=bool(self._reports_by_id
                                            or self._saved_states))

    def _relaunch_admin(self) -> None:
        """Restart programu z podniesionymi uprawnieniami (UAC), by tworzyć
        symlinki. Bez tego miejsca DAT-ów dzieci zostają puste (nic nie
        kopiujemy)."""
        from ..core.elevate import relaunch_as_admin
        ok = QMessageBox.question(
            self, tr("Administrator"),
            tr("Program zostanie uruchomiony ponownie z uprawnieniami "
            "administratora (pojawi się monit UAC), aby móc tworzyć symlinki.\n"
            "Bieżące okno zostanie zamknięte. Kontynuować?"))
        if ok != QMessageBox.StandardButton.Yes:
            return
        if relaunch_as_admin():
            self.close()
        else:
            QMessageBox.warning(
                self, tr("Administrator"),
                tr("Nie udało się uruchomić z podniesionymi uprawnieniami "
                "(odmowa UAC?).\n\nAlternatywa bez admina: włącz TRYB "
                "DEWELOPERA w Windows — Ustawienia → System → Dla deweloperów "
                "→ Tryb dewelopera. Wtedy symlinki działają na zwykłym koncie."))

    def _multi_dat_settings(self, entries: list) -> None:
        from .multi_dat_settings_dialog import MultiDatSettingsDialog
        dlg = MultiDatSettingsDialog(self, entries, Path(self.row_dats.path))
        if dlg.exec():
            self._log(f"Zapisano ustawienia dla {len(entries)} DAT-ów. "
                      f"Skanuj i raportuj, by zastosować.")

    def _art_keys(self) -> dict:
        """Klucze API wszystkich źródeł grafik (z ustawień)."""
        s = self.settings
        return {"sgdb": s.sgdb_api_key,
                "igdb_id": s.igdb_client_id,
                "igdb_secret": s.igdb_client_secret,
                "tgdb": s.tgdb_api_key}

    def _icon_for_game(self, entry, game: str) -> None:
        from ..core.icons import clean_system_name, strip_disc_tag
        from .icon_picker import IconPickDialog
        base = strip_disc_tag(game) or game
        out_base = self.row_icons_out.path if hasattr(self, "row_icons_out") \
            else ""
        icons_dir = (Path(out_base) / entry.name if out_base
                     else Path(entry.target_dir) / "icons")
        ico = icons_dir / f"{base}.ico"
        # czysta nazwa platformy → Libretro/TGDB rozpoznają system (nie tylko SGDB)
        system = clean_system_name(entry.name)
        dlg = IconPickDialog(self, system, game, self._art_keys(), ico)
        if dlg.exec():
            self._log(f"Ikona zapisana: {ico}")

    def _icons_for_dat(self, entry) -> None:
        target = Path(entry.target_dir)
        if not target.is_dir():
            QMessageBox.information(
                self, tr("Ikony"), tr("Katalog") + f" {target} " + tr("jeszcze nie istnieje — "
                               "najpierw Napraw kolekcję."))
            return
        sgdb_key = self.settings.sgdb_api_key
        system = entry.name

        def job(log: Callable[[str], None], progress):
            from ..core.icons import SgdbClient, make_icons_for_dir
            progress(0, 0, f"ikony: {system}")
            sgdb = SgdbClient(sgdb_key) if sgdb_key else None
            return make_icons_for_dir(target, system, sgdb=sgdb, log=log)

        self._run(job, lambda st: self._log(f"Ikony ({system}): {st.summary()}"))

    def _collection_paths(self) -> Optional[tuple[Path, Path]]:
        dats, roms = self.row_dats.path, self.row_roms.path
        if not dats or not Path(dats).is_dir():
            QMessageBox.warning(self, tr("Kombajn"), tr("Wskaż istniejący katalog DAT-ów."))
            return None
        if not roms:
            QMessageBox.warning(self, tr("Kombajn"), tr("Wskaż katalog główny ROM-ów."))
            return None
        return Path(dats), Path(roms)

    def _collection_report(self) -> None:
        paths = self._collection_paths()
        if paths is None:
            return
        dats, roms = paths
        tosorts = self.settings.tosort_dirs      # główny + dodatkowe
        # Model automatyczny: znane pliki z cache (szybko), nowe/zmienione
        # liczone w pełni + głęboka identyfikacja CHD bez data_sha1.
        full, chd_mode = False, "deep"
        db = self.settings.index_db_path or None
        settings = self.settings

        def job(log: Callable[[str], None], progress, cancel, detail):
            from ..core.datstore import DatStore
            from ..core.fileindex import FileIndex
            from ..core.matcher import match_entry
            with FileIndex(Path(db) if db else None) as idx:
                prober = (_chd_prober(settings, log)
                          if chd_mode in ("header", "deep") else None)
                progress(0, 0, "wczytywanie DAT-ów…")
                all_entries = DatStore(dats, roms).discover(log=log)
                from ..core.dirrules import DirRules, apply_rule_targets
                rules = DirRules(dats)
                apply_rule_targets(all_entries, rules, roms, log=log)
                # WŁĄCZONE DAT-y (skip=false); wyłączone zostają w drzewie,
                # ale nie są skanowane ani dopasowywane
                enabled = [e for e in all_entries
                           if not rules.for_entry(e)["skip"]]
                disabled = len(all_entries) - len(enabled)
                if disabled:
                    log(f"Wyłączonych DAT-ów (nie skanuję): {disabled}")
                # SKANUJ tylko katalogi docelowe włączonych DAT-ów + ToSort
                # (katalogi w roms nieprzypisane do DAT-a są pomijane)
                roots = []
                seen: set[str] = set()
                for e in enabled:
                    d = str(e.target_dir)
                    if d not in seen and Path(d).is_dir():
                        seen.add(d)
                        roots.append(d)
                for ts in tosorts:               # wszystkie katalogi ToSort
                    if ts and Path(ts).is_dir() and ts not in seen:
                        seen.add(ts)
                        roots.append(ts)
                for ri, r in enumerate(roots):
                    if cancel.is_set():
                        break
                    progress(ri, len(roots), f"skan: {r}")
                    log(f"Skan: {r}")
                    st = idx.scan(Path(r), full=full, chd_prober=prober,
                                  on_file=_pulse(log, progress), cancel=cancel)
                    log(f"  {st.summary()}")
                log(f"DAT-ów: {len(enabled)} włączonych z {len(all_entries)}")
                # DUCHY: wpisy pod korzeniami, które ZNIKNĘŁY (np. skasowane
                # stare roms) — bez tego matcher planuje przenosiny z
                # nieistniejących ścieżek („plik zmienił się od skanu").
                idx.prune_ghosts(log)
                if not cancel.is_set():
                    _deep_probe_gui(idx, enabled, settings, chd_mode, roots,
                                    log, cancel=cancel, on_progress=progress,
                                    detail=detail)
                reports = []
                for i, e in enumerate(enabled):
                    if cancel.is_set():
                        log(f"PRZERWANO dopasowanie na {i}/{len(enabled)} "
                            f"DAT-ów — wyniki cząstkowe zachowane.")
                        break
                    progress(i, len(enabled), f"dopasowanie: {e.name}")
                    reports.append(match_entry(e, idx))
                progress(len(reports), len(enabled), "dopasowanie")
                return all_entries, reports

        self._run(job, self._fill_reports, title="Skanowanie kolekcji")

    def _fill_reports(self, result) -> None:
        """Wypełnia lewy panel DAT-ami ZE STATYSTYKAMI. Wyłączone DAT-y są
        widoczne (bez statystyk), włączone — z liczbami komplet/naprawa/brak."""
        all_entries, reports = result
        self._reports_by_id = {id(r.entry): r for r in reports}
        self._entries = all_entries
        # PRZEPIS dla naprawy: pełne raporty zostają w pamięci, więc „Napraw"
        # NIE skanuje niczego ponownie — parsuje tylko to, co tu policzone.
        self._reports = reports
        self._plan = None            # plan (dry-run) unieważniony nowym skanem
        self._saved_states = {}      # świeży raport zastępuje cache w widoku
        self._fill_dats(all_entries, with_stats=True)
        # zapisz stan raportu (trwale) — po ponownym otwarciu widać ostatni skan
        try:
            from ..core.datcache import save_report_states
            save_report_states(reports)
        except Exception as e:      # zapis cache nie może ubić raportu
            self._log(f"UWAGA: nie zapisano cache raportu: {e}")
        # łączne podsumowanie na poziomie GRY (spójne z listą i kolumnami)
        stats = [r.game_stats() for r in reports]
        complete = sum(s[1] for s in stats)
        fix = sum(s[2] for s in stats)
        miss = sum(s[3] for s in stats)
        self._log(f"Raport: {len(reports)} DAT-ów — gry: komplet {complete}, "
                  f"do naprawy {fix}, brak {miss} (zapamiętane). Kliknij DAT, "
                  f"by zobaczyć gry; kliknij grę, by zobaczyć pliki i sumy.")

    def _fill_dats(self, entries, with_stats: bool) -> None:
        """Lewy panel: grupy-katalogi → DAT-y (liście) z CHECKBOXEM (odznacz =
        nie skanuj tego DAT-a). Statystyki per DAT z raportu; wyłączone szare."""
        from ..core.dirrules import DirRules
        self._filling = True
        self.tree.clear()
        self.game_list.clear()
        self.rom_list.clear()
        dat_root = Path(self.row_dats.path)
        rules = DirRules(dat_root) if dat_root.is_dir() else None
        # ToSort-y jako pozycje drzewa (jak w RomVaulcie): główny + dodatkowe.
        # Prawy klik: wymuś pełny skan / dodaj kolejny katalog / usuń z listy.
        for i, ts in enumerate(self.settings.tosort_dirs):
            exists = Path(ts).is_dir()
            n_files = ""
            if exists:
                try:
                    n_files = str(sum(1 for x in Path(ts).iterdir()))
                except OSError:
                    n_files = "?"
            label = ("🗃 ToSort" if i == 0 else "🗃 ToSort (dodatkowy)")
            it = QTreeWidgetItem(
                [f"{label}: {ts}" + ("" if exists else "  [BRAK KATALOGU]"),
                 n_files, "", "", ""])
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
            it.setData(0, Qt.ItemDataRole.UserRole, ("tosort", ts, i))
            if not exists:
                it.setForeground(0, QBrush(QColor(190, 60, 60)))
            self.tree.addTopLevelItem(it)
        groups: dict = {}
        for e in self._display_order(entries, dat_root, lambda x: x):
            skip = rules.for_entry(e)["skip"] if rules else False
            states = self._game_states_for(e) if with_stats else None
            if states:
                from ..core.matcher import game_stats_from_states
                total, complete, fix, miss = game_stats_from_states(states)
                item = QTreeWidgetItem([e.name, str(total), str(complete),
                                        str(fix), str(miss)])
                color = _GREEN if miss == 0 and fix == 0 else (
                    _YELLOW if complete or fix else _RED)
                if not skip:
                    for c in range(5):
                        item.setBackground(c, QBrush(color))
            else:
                item = QTreeWidgetItem([e.name, str(len(e.games)), "", "", ""])
            item.setData(0, Qt.ItemDataRole.UserRole, e)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked if skip
                               else Qt.CheckState.Checked)
            if skip:
                item.setForeground(0, QBrush(QColor(150, 150, 150)))
            parent = self._group_parent(dat_root, e.dat_path, groups)
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
        for node in groups.values():
            node.setExpanded(True)
        # początkowy stan checkboxów GRUP z dzieci (pełne/częściowe/puste);
        # od tej chwili AutoTristate utrzymuje to samo przy klikaniu
        def _agg(node) -> Qt.CheckState:
            states = set()
            for i in range(node.childCount()):
                ch = node.child(i)
                st = (_agg(ch) if ch.childCount()
                      else ch.checkState(0))
                states.add(st)
            if states == {Qt.CheckState.Checked}:
                out = Qt.CheckState.Checked
            elif states == {Qt.CheckState.Unchecked}:
                out = Qt.CheckState.Unchecked
            else:
                out = Qt.CheckState.PartiallyChecked
            node.setCheckState(0, out)
            return out
        for key, node in groups.items():
            if len(key) == 1:              # tylko korzenie grup (rekurencja zejdzie)
                _agg(node)
        self._filling = False

    def _on_tree_item_changed(self, item, column) -> None:
        """Zmiana checkboxa DAT-a → zapis reguły skip (odznaczony = pomijany)."""
        if getattr(self, "_filling", False):
            return
        from ..core.datstore import DatEntry
        from ..core.dirrules import save_rule
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(entry, DatEntry):
            return
        skip = item.checkState(0) == Qt.CheckState.Unchecked
        try:
            save_rule(Path(self.row_dats.path), entry.name, {"skip": skip})
        except OSError as e:
            self._log(f"BŁĄD zapisu reguły: {e}")
            return
        col = QColor(150, 150, 150) if skip else QColor()
        item.setForeground(0, QBrush(col))
        self._log(f"DAT {entry.name}: {'WYŁĄCZONY (nie skanuję)' if skip else 'włączony'}"
                  f" — Skanuj i raportuj, by zastosować.")

    def _collection_fix(self, dry: bool = False) -> None:
        paths = self._collection_paths()
        if paths is None:
            return
        dats, roms = paths
        tosort = self.row_tosort.path            # główny (tam trafiają nieznane)
        tosorts = self.settings.tosort_dirs      # wszystkie (skan/dedup/kasowanie)
        clean = self.chk_clean.isChecked()
        only_complete = not self.chk_incomplete.isChecked()
        dedup = self.chk_dedup.isChecked()
        del_tosort = self.chk_del_tosort.isChecked()
        convert = self.chk_convert.isChecked()
        make_links = self.chk_links.isChecked()
        # jak wyżej — model automatyczny, bez globalnego poziomu skanu
        full, chd_mode = False, "deep"
        settings = self.settings
        if clean and not tosort:
            QMessageBox.warning(self, tr("Kombajn"),
                                tr("Sprzątanie nieznanych wymaga katalogu ToSort."))
            return
        # C: naprawa NIE skanuje — parsuje PRZEPIS z ostatniego skanu.
        reports = self._reports
        if not reports:
            QMessageBox.information(
                self, tr("Brak przepisu"),
                tr("Najpierw uruchom: Skanuj i raportuj. Naprawa korzysta z "
                "wyników tego skanu (co gdzie leży, jakie ma sumy) i sama "
                "niczego nie skanuje."))
            return
        entries = self._entries or []
        # Nieistniejące katalogi bazowe: literówka ALBO celowo wyczyszczony
        # układ (user przenosi wszystko do ToSort i każe programowi odtworzyć
        # strukturę). Pokazujemy listę i PYTAMY zamiast ślepo blokować.
        from ..core.dirrules import DirRules as _DR, missing_roots as _mr
        bad = _mr(entries, _DR(dats), roms)
        if bad:
            listing = "\n".join(f"   {p}" for _n, p in bad)
            ok = QMessageBox.question(
                self, tr("Katalogi bazowe nie istnieją"),
                tr("Te katalogi bazowe (rom_root) nie istnieją:") + "\n\n" + listing +
                "\n\n" + tr("Jeśli to LITERÓWKA w regule — wybierz Nie i popraw "
                "ustawienia.\nJeśli celowo je wyczyściłeś (pliki są w ToSort) "
                "— wybierz Tak: katalogi zostaną UTWORZONE, a naprawa "
                "odtworzy strukturę i przeniesie pliki z ToSort."))
            if ok != QMessageBox.StandardButton.Yes:
                return
            for _n, p in bad:
                try:
                    Path(p).mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    QMessageBox.warning(self, tr("Kombajn"),
                                        tr("Nie utworzono") + f" {p}:\n{e}")
                    return
            self._log("Utworzono katalogi bazowe: "
                      + ", ".join(p for _n, p in bad))
        if not dry:
            links_note = (tr("wspólne pliki dostaną symlinki")
                          if make_links else
                          tr("symlinki WYŁĄCZONE — miejsca DAT-ów dzieci zostaną "
                          "PUSTE (nic nie jest kopiowane)"))
            ok = QMessageBox.question(
                self, tr("Napraw kolekcję"),
                tr("Pliki zostaną przeniesione/przemianowane wg DAT-ów "
                   "(z walidacją sum kontrolnych), ") + links_note +
                (tr(", nieznane trafią do ToSort") if clean else "") +
                tr(".\nOperację można PRZERWAĆ w każdej chwili — zrobione "
                   "zostaje zrobione.\n\nWykonać?"))
            if ok != QMessageBox.StandardButton.Yes:
                return
        db = self.settings.index_db_path or None

        def job(log: Callable[[str], None], progress, cancel, detail):
            from ..core.fileindex import FileIndex
            from ..core.rebuilder import Rebuilder
            from ..core.dirrules import DirRules, missing_roots, scan_roots
            with FileIndex(Path(db) if db else None) as idx:
                rules = DirRules(dats)
                if rules.error:
                    log(f"UWAGA: {rules.error}")
                # BEZPIECZNIK: nieistniejący rom_root => naprawa przeniosłaby
                # całą kolekcję. Przerywamy PRZED dotknięciem plików.
                bad = missing_roots(entries, rules, roms)
                if bad:
                    log("PRZERWANO — nieistniejące katalogi bazowe (rom_root):")
                    for name, path in bad:
                        log(f"   {path}   (reguła dla: {name})")
                    return None
                sroots = scan_roots(entries, rules, roms, tosorts)
                if not dry:
                    from ..core.convert import purge_temp_artifacts
                    from ..core.linker import remove_broken_links
                    n_t, sz_t = purge_temp_artifacts(sroots, log=log)
                    if n_t:
                        log(f"Sprzątnięto {n_t} śmieci po przerwanych "
                            f"konwersjach ({sz_t/1024**3:.2f} GB odzyskane).")
                    # zerwane symlinki (cel przeniesiony/skonwertowany) —
                    # psują konwersje; usuwamy, odtworzą się przy naprawie
                    n_b = remove_broken_links(sroots, index=idx, log=log)
                    if n_b:
                        log(f"Usunięto {n_b} zerwanych symlinków.")
                log(f"{'PODGLĄD' if dry else 'NAPRAWA'} z przepisu: "
                    f"{len(reports)} DAT-ów (bez ponownego skanowania).")
                dedup_roots = ([Path(r) for r in sroots] if dedup else [])
                # „usuń z ToSort pliki już na miejscu" działa dla WSZYSTKICH
                # katalogów ToSort (kopia potwierdzona gdzie indziej = zbędna)
                del_from = ([Path(t) for t in tosorts if t]
                            if del_tosort else [])
                rb = Rebuilder(idx, tosort=Path(tosort) if tosort else None,
                               dry_run=dry, log=log, make_links=make_links,
                               detail=detail, zip_level=settings.zip_level)

                def _make_tools():
                    from ..core.convert import detect_dolphintool
                    emu = settings.emulators_dir
                    tools = {"settings": settings, "chdman": None}
                    try:
                        from ..core.chdman import CHDMan
                        tools["chdman"] = CHDMan(settings.chdman_path or None)
                    except Exception:
                        tools["chdman"] = None
                    tools["dolphintool"] = (detect_dolphintool(Path(emu))
                                            if emu and Path(emu).is_dir() else None)
                    return tools

                # KONWERSJA PROSTO ZE ŹRÓDŁA (najpierw): dla gier, których
                # źródłem są luźne pliki/członki archiwum — zbiera je na RAM,
                # kompresuje na RAM, do docelowego trafia TYLKO finał; źródła
                # kasowane po WSZYSTKICH grach. Placement pomija te gry.
                converted_games: set = set()
                src_to_purge: list = []
                if convert and not cancel.is_set():
                    from ..core.convert import convert_from_source
                    cst0, converted_games, src_to_purge = convert_from_source(
                        reports, rules.for_entry, _make_tools(), index=idx,
                        dry_run=dry, log=log, cancel=cancel, detail=detail,
                        on_progress=progress, on_converted=rb.add_canonical,
                        delete_roots=del_from)
                    log(f"Konwersja ze źródła: {cst0.summary()} "
                        f"({len(converted_games)} gier na RAM, docelowy dostał "
                        f"tylko finał).")

                # KONWERSJA „w miejscu" (fallback) — dla gier, których nie dało
                # się zrobić prosto ze źródła (placement ułożył je luźno);
                # PO placemencie, PRZED dedupem/sprzątaniem.
                def _do_convert():
                    if not (convert and not cancel.is_set()):
                        return
                    from ..core.convert import convert_reports
                    cst = convert_reports(reports, rules.for_entry, _make_tools(),
                                          index=idx, log=log, cancel=cancel,
                                          on_progress=lambda i, n, t:
                                              progress(i, n, f"konwersja: {t}"),
                                          detail=detail,
                                          on_converted=rb.add_canonical)
                    log(f"Konwersja w miejscu: {cst.summary()}")

                # rebuilder sam etykietuje fazy (naprawa/sprzątanie/dedup) —
                # przekazujemy postęp 1:1, bez doklejania własnego prefiksu
                stats = rb.run(reports, clean=clean, only_complete=only_complete,
                               rules=rules.for_entry, dedup_roots=dedup_roots,
                               delete_placed_from=del_from, cancel=cancel,
                               after_place=_do_convert,
                               converted_games=converted_games,
                               on_progress=progress)
                # KONIEC: dopiero teraz kasujemy oryginalne źródła gier
                # skonwertowanych PROSTO ZE ŹRÓDŁA (współdzielone ścieżki
                # wielopłytowe były dostępne przez cały placement/fallback).
                if src_to_purge and not dry and not rb.cancelled:
                    from ..core.convert import purge_source_files
                    purge_source_files(src_to_purge, index=idx, log=log,
                                       dry_run=dry)
                return stats

        def done(stats) -> None:
            if stats is None:           # bezpiecznik przerwał — nic nie ruszono
                QMessageBox.warning(
                    self, tr("Naprawa przerwana"),
                    tr("Naprawa NIE została wykonana: reguła rom_root wskazuje "
                    "nieistniejący katalog (szczegóły w logu).\nNic nie "
                    "zostało zmienione."))
                return
            tag = "PODGLĄD" if dry else "WYKONANO"
            self._log(f"[{tag}] {stats.summary()}")
            if getattr(stats, "links_skipped", 0):
                self._log(
                    f"UWAGA: pominięto {stats.links_skipped} symlinków (brak "
                    f"uprawnień lub opcja wyłączona) — NIC nie skopiowano, te "
                    f"miejsca są puste. Uruchom jako administrator i powtórz.")
            if dry:
                self._plan = stats
                QMessageBox.information(
                    self, tr("Znajdź naprawy"),
                    tr("Plan naprawy (nic nie zmieniono):") + f"\n\n{stats.summary()}\n\n"
                    + tr("Szczegóły w logu i w oknie postępu. Jeśli plan wygląda "
                    "dobrze — kliknij: Napraw (wykonaj)."))
                return
            if getattr(stats, "cancelled", False):
                self._log("Naprawa PRZERWANA — zrobione operacje zostają. "
                          "Zrób skan i ponów, by dokończyć resztę.")
                return
            self._collection_report()   # odśwież statystyki (walidacja skanem)

        self._run(job, done,
                  title=(tr("Znajdź naprawy (podgląd)") if dry
                         else tr("Naprawa kolekcji")))

    # ── zakładka: Indeks ──────────────────────────────────────────────────

    def _build_tab_index(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(tr(
            "Zakładka ZAAWANSOWANA — indeks to wewnętrzna baza sum kontrolnych "
            "(zwykle nie musisz jej ustawiać ręcznie; przycisk Skanuj i "
            "raportuj na zakładce Kolekcja robi to sam).\n"
            "Katalogi poniżej służą do skanu na żądanie i deduplikacji "
            "(kolejność = priorytet kopii fizycznej):")))
        self.roots_list = QListWidget()
        for r in self.settings.index_roots:
            self.roots_list.addItem(r)
        lay.addWidget(self.roots_list, 1)

        row = QHBoxLayout()
        btn_add = QPushButton(tr("➕ Dodaj katalog"))
        btn_add.clicked.connect(self._index_add_root)
        btn_del = QPushButton(tr("➖ Usuń zaznaczony"))
        btn_del.clicked.connect(self._index_del_root)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        row.addStretch()
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.btn_scan = QPushButton(tr("🔍 Skanuj przyrostowo"))
        self.btn_scan.clicked.connect(self._index_scan)
        self.chk_full = QCheckBox(tr("pełny re-skan (licz sumy od nowa)"))
        self.chk_chd = QCheckBox(tr("SHA-1 zawartości CHD (chdman)"))
        row2.addWidget(self.btn_scan)
        row2.addWidget(self.chk_full)
        row2.addWidget(self.chk_chd)
        row2.addStretch()
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        self.btn_dupes = QPushButton(tr("👥 Pokaż duplikaty"))
        self.btn_dupes.clicked.connect(self._index_dupes)
        self.btn_dedup_dry = QPushButton(tr("🔗 Dedup (podgląd)"))
        self.btn_dedup_dry.clicked.connect(lambda: self._index_dedup(dry=True))
        self.btn_dedup = QPushButton(tr("🔗 Dedup (wykonaj)"))
        self.btn_dedup.clicked.connect(lambda: self._index_dedup(dry=False))
        row3.addWidget(self.btn_dupes)
        row3.addWidget(self.btn_dedup_dry)
        row3.addWidget(self.btn_dedup)
        row3.addStretch()
        lay.addLayout(row3)

        self.lbl_index_stats = QLabel("—")
        lay.addWidget(self.lbl_index_stats)
        self.tabs.addTab(w, tr("Indeks"))

    def _index_roots(self) -> list[str]:
        return [self.roots_list.item(i).text()
                for i in range(self.roots_list.count())]

    def _index_save_roots(self) -> None:
        self.settings.index_roots = self._index_roots()
        self.settings.save()

    def _index_add_root(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("Katalog do indeksu"))
        if d:
            self.roots_list.addItem(os.path.normpath(d))
            self._index_save_roots()

    def _index_del_root(self) -> None:
        for it in self.roots_list.selectedItems():
            self.roots_list.takeItem(self.roots_list.row(it))
        self._index_save_roots()

    def _index_scan(self) -> None:
        roots = self._index_roots()
        if not roots:
            QMessageBox.warning(self, tr("Kombajn"), tr("Dodaj przynajmniej jeden katalog."))
            return
        full = self.chk_full.isChecked()
        with_chd = self.chk_chd.isChecked()
        db = self.settings.index_db_path or None
        chdman_path = self.settings.chdman_path or None

        def job(log: Callable[[str], None], progress):
            from ..core.fileindex import FileIndex
            prober = None
            if with_chd:
                from ..core.chdman import CHDMan
                chd = CHDMan(chdman_path)

                def prober(p: Path) -> str:
                    i = chd.info(p)
                    return i.data_sha1 or i.sha1 or ""
            with FileIndex(Path(db) if db else None) as idx:
                for i, r in enumerate(roots):
                    progress(i, len(roots), f"skan: {r}")
                    log(f"Skanuję: {r}")
                    st = idx.scan(Path(r), full=full, chd_prober=prober,
                                  log=log, on_file=_pulse(log, progress))
                    log(f"  {st.summary()}")
                return idx.stats()

        def done(stats: dict) -> None:
            self.lbl_index_stats.setText(
                f"Wpisów: {stats['total']}  |  linki: {stats['links']}  |  "
                f"brakujące: {stats['missing']}  |  "
                f"dane: {stats['bytes'] / 2**30:.2f} GiB")

        self._run(job, done)

    def _index_dupes(self) -> None:
        db = self.settings.index_db_path or None

        def job(log: Callable[[str], None], progress):
            from ..core.fileindex import FileIndex
            with FileIndex(Path(db) if db else None) as idx:
                groups = idx.duplicate_groups()
                for g in groups[:200]:
                    log(f"[{g.size / 2**20:8.1f} MB] ×{len(g.paths)}")
                    for p in g.paths:
                        log(f"    {p}")
                wasted = sum(g.size * (len(g.paths) - 1) for g in groups)
                return len(groups), wasted

        def done(res) -> None:
            n, wasted = res
            self._log(f"Duplikatów: {n} grup, do odzyskania "
                      f"{wasted / 2**30:.2f} GiB (Dedup zastąpi je symlinkami).")

        self._run(job, done)

    def _index_dedup(self, dry: bool) -> None:
        prefer = self._index_roots()
        if not dry:
            ok = QMessageBox.question(
                self, tr("Deduplikacja"),
                tr("Duplikaty zostaną zastąpione symlinkami do jednej kopii "
                "fizycznej (odwracalna podmiana, nic nie jest kasowane "
                "bezpowrotnie).")
                + "\n\n" + tr("Wykonać?"))
            if ok != QMessageBox.StandardButton.Yes:
                return
        db = self.settings.index_db_path or None

        def job(log: Callable[[str], None], progress):
            from ..core.fileindex import FileIndex
            from ..core.linker import apply_dedup, plan_dedup
            with FileIndex(Path(db) if db else None) as idx:
                actions = plan_dedup(idx, prefer_roots=prefer)
                return apply_dedup(actions, index=idx, dry_run=dry, log=log)

        def done(stats) -> None:
            mode = "PODGLĄD" if dry else "WYKONANO"
            self._log(f"[{mode}] {stats.summary()}")

        self._run(job, done)

    # ── zakładka: Ikony i skróty ──────────────────────────────────────────

    def _build_tab_art(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.row_art = _PathRow(tr("Katalog gier:"), self.settings.rom_root)
        lay.addWidget(self.row_art)
        self.row_emus = _PathRow(tr("Katalog emulatorów:"), self.settings.emulators_dir,
                                 lambda v: self._save_setting("emulators_dir", v))
        lay.addWidget(self.row_emus)
        self.row_icons_out = _PathRow(
            tr("Ikony → (puste = obok gier, w \\icons):"),
            self.settings.icons_out_dir,
            lambda v: self._save_setting("icons_out_dir", v))
        lay.addWidget(self.row_icons_out)
        self.row_lnk_out = _PathRow(
            tr("Skróty → (puste = obok gier, w \\shortcuts):"),
            self.settings.shortcuts_out_dir,
            lambda v: self._save_setting("shortcuts_out_dir", v))
        lay.addWidget(self.row_lnk_out)

        row = QHBoxLayout()
        self.chk_tree = QCheckBox(tr("podkatalogi = systemy (RomRoot)"))
        self.chk_tree.setChecked(True)
        self.chk_over = QCheckBox(tr("nadpisuj istniejące"))
        row.addWidget(self.chk_tree)
        row.addWidget(self.chk_over)
        row.addStretch()
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.btn_icons = QPushButton(tr("🖼 Twórz ikony"))
        self.btn_icons.clicked.connect(self._art_icons)
        self.btn_lnk = QPushButton(tr("🔗 Twórz skróty .lnk"))
        self.btn_lnk.clicked.connect(self._art_shortcuts)
        self.btn_m3u = QPushButton(tr("🎵 Generuj playlisty .m3u"))
        self.btn_m3u.clicked.connect(self._art_m3u)
        self.btn_emu_detect = QPushButton(tr("🎮 Wykryj systemy i emulatory"))
        self.btn_emu_detect.clicked.connect(self._emu_table_fill)
        row2.addWidget(self.btn_icons)
        row2.addWidget(self.btn_lnk)
        row2.addWidget(self.btn_m3u)
        row2.addWidget(self.btn_emu_detect)
        row2.addStretch()
        lay.addLayout(row2)

        lay.addWidget(QLabel(tr("Emulator per system (standalone albo rdzeń "
                             "RetroArch); wybór zapisuje się w ustawieniach:")))
        self.tbl_emus = QTableWidget(0, 2)
        self.tbl_emus.setHorizontalHeaderLabels([tr("System (katalog)"), tr("Emulator")])
        self.tbl_emus.horizontalHeader().setStretchLastSection(True)
        self.tbl_emus.setColumnWidth(0, 320)
        self.tbl_emus.verticalHeader().setVisible(False)
        lay.addWidget(self.tbl_emus, 1)
        self.tabs.addTab(w, tr("Ikony i skróty"))

    def _art_m3u(self) -> None:
        base = self.row_art.path
        if not base or not Path(base).is_dir():
            QMessageBox.warning(self, tr("Kombajn"), tr("Wskaż istniejący katalog gier."))
            return
        overwrite = self.chk_over.isChecked()

        def job(log: Callable[[str], None], progress):
            from ..core.playlists import generate_m3u
            progress(0, 0, "playlisty .m3u…")
            return generate_m3u(Path(base), overwrite=overwrite, log=log)

        self._run(job, lambda st: self._log(f"Playlisty .m3u: {st.summary()}"))

    def _emu_table_fill(self) -> None:
        from ..core.shortcuts import detect_system, emulator_options, find_emulators
        base = self.row_art.path
        emu_root = self.row_emus.path
        if not base or not Path(base).is_dir() or not emu_root \
                or not Path(emu_root).is_dir():
            QMessageBox.warning(self, tr("Kombajn"),
                                tr("Wskaż katalog gier i katalog emulatorów."))
            return
        installed = find_emulators(Path(emu_root))
        self._log(f"Emulatory: {', '.join(sorted(installed)) or 'brak'}")
        rows: list[tuple[str, str]] = []      # (system, nazwa katalogu)
        seen: set[str] = set()
        for d in sorted(p for p in Path(base).iterdir() if p.is_dir()):
            system = detect_system(d.name)
            if system and system not in seen:
                seen.add(system)
                rows.append((system, d.name))
        self.tbl_emus.setRowCount(0)
        for system, dirname in rows:
            options = emulator_options(system, installed)
            if not options:
                continue
            r = self.tbl_emus.rowCount()
            self.tbl_emus.insertRow(r)
            it = QTableWidgetItem(f"{system}   ({dirname})")
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.tbl_emus.setItem(r, 0, it)
            combo = QComboBox()
            combo.addItem(tr("(domyślny:") + f" {options[0].label})", "")
            for o in options:
                combo.addItem(o.label, o.id)
            saved = self.settings.system_emulators.get(system, "")
            if saved:
                i = combo.findData(saved)
                if i > 0:
                    combo.setCurrentIndex(i)
            combo.currentIndexChanged.connect(
                lambda _i, s=system, c=combo: self._emu_choice_changed(s, c))
            self.tbl_emus.setCellWidget(r, 1, combo)
        if self.tbl_emus.rowCount() == 0:
            self._log("Nie wykryto systemów (nazwy katalogów nie mapują się).")

    def _emu_choice_changed(self, system: str, combo) -> None:
        choice = combo.currentData()
        if choice:
            self.settings.system_emulators[system] = choice
        else:
            self.settings.system_emulators.pop(system, None)
        self.settings.save()
        self._log(f"{system}: emulator = "
                  f"{combo.currentText() if choice else 'domyślny'}")

    def _art_dirs(self) -> Optional[list[Path]]:
        base = self.row_art.path
        if not base or not Path(base).is_dir():
            QMessageBox.warning(self, tr("Kombajn"), tr("Wskaż istniejący katalog gier."))
            return None
        if self.chk_tree.isChecked():
            return sorted(d for d in Path(base).iterdir() if d.is_dir())
        return [Path(base)]

    def _art_icons(self) -> None:
        dirs = self._art_dirs()
        if dirs is None:
            return
        overwrite = self.chk_over.isChecked()
        sgdb_key = self.settings.sgdb_api_key
        out_base = self.row_icons_out.path

        def job(log: Callable[[str], None], progress):
            from ..core.icons import SgdbClient, make_icons_for_dir
            sgdb = SgdbClient(sgdb_key) if sgdb_key else None
            total = 0
            for i, d in enumerate(dirs):
                progress(i, len(dirs), f"ikony: {d.name}")
                log(f"── {d.name}")
                # katalog wyjściowy: wskazany (per system) albo obok gier
                out = (Path(out_base) / d.name if out_base else None)
                st = make_icons_for_dir(d, d.name, out_dir=out, sgdb=sgdb,
                                        overwrite=overwrite, log=log)
                log(f"  {st.summary()}"
                    + (f"  → {out}" if out else ""))
                total += st.done
            return total

        self._run(job, lambda n: self._log(f"Ikony gotowe: {n} nowych."))

    def _art_shortcuts(self) -> None:
        dirs = self._art_dirs()
        if dirs is None:
            return
        emu_root = self.row_emus.path
        if not emu_root or not Path(emu_root).is_dir():
            QMessageBox.warning(self, tr("Kombajn"), tr("Wskaż katalog emulatorów."))
            return
        overwrite = self.chk_over.isChecked()
        overrides = dict(self.settings.system_emulators)
        out_base = self.row_lnk_out.path
        icons_base = self.row_icons_out.path

        def job(log: Callable[[str], None], progress):
            from ..core.shortcuts import (build_plan, create_shortcuts,
                                          detect_system, find_emulators)
            installed = find_emulators(Path(emu_root))
            log(f"Emulatory: {', '.join(sorted(installed)) or 'brak'}")
            total = 0
            for i, d in enumerate(dirs):
                progress(i, len(dirs), f"skróty: {d.name}")
                system = detect_system(d.name) or d.name
                out = Path(out_base) / d.name if out_base else None
                icons = Path(icons_base) / d.name if icons_base else None
                plan, why = build_plan(d, system, installed,
                                       override=overrides.get(system, ""),
                                       out_dir=out, icons_dir=icons)
                if why:
                    log(f"── {d.name}: POMIŃ ({why})")
                    continue
                log(f"── {d.name} [{system}]: gier {len(plan)}"
                    + (f"  → {out}" if out else ""))
                st = create_shortcuts(plan, overwrite=overwrite, log=log)
                log(f"  {st.summary()}")
                total += st.created
            return total

        self._run(job, lambda n: self._log(f"Skróty gotowe: {n} nowych."))

    # ── zakładka: BIOS ─────────────────────────────────────────────────────

    def _build_tab_bios(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.row_bios = _PathRow(tr("Katalog BIOS-ów (skan po MD5):"),
                                 self.settings.bios_dir,
                                 lambda v: self._save_setting("bios_dir", v))
        lay.addWidget(self.row_bios)
        lay.addWidget(QLabel(tr("Emulatory (zaznaczone dostaną BIOS-y wg "
                             "manifestu bios_manifest.json):")))
        self.bios_list = QListWidget()
        from ..core.bios import load_manifest
        for emu in sorted(load_manifest()["emulators"]):
            it = QListWidgetItem(emu)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.bios_list.addItem(it)
        lay.addWidget(self.bios_list, 1)

        row = QHBoxLayout()
        self.btn_bios_install = QPushButton(tr("📦 Skanuj i instaluj do emulatorów"))
        self.btn_bios_install.clicked.connect(self._bios_install)
        self.btn_bios_import = QPushButton(tr("📥 Import System.dat"))
        self.btn_bios_import.clicked.connect(self._bios_import)
        row.addWidget(self.btn_bios_install)
        row.addWidget(self.btn_bios_import)
        row.addStretch()
        lay.addLayout(row)
        self.tabs.addTab(w, tr("BIOS"))

    def _bios_selected(self) -> list[str]:
        return [self.bios_list.item(i).text()
                for i in range(self.bios_list.count())
                if self.bios_list.item(i).checkState() == Qt.CheckState.Checked]

    def _bios_install(self) -> None:
        src = self.row_bios.path
        emu_root = self.row_emus.path or self.settings.emulators_dir
        if not src or not Path(src).is_dir():
            QMessageBox.warning(self, tr("BIOS"), tr("Wskaż istniejący katalog BIOS-ów."))
            return
        if not emu_root or not Path(emu_root).is_dir():
            QMessageBox.warning(self, tr("BIOS"), tr("Wskaż katalog emulatorów "
                                              "(zakładka Ikony i skróty)."))
            return
        only = self._bios_selected()
        if not only:
            QMessageBox.warning(self, tr("BIOS"), tr("Zaznacz przynajmniej jeden emulator."))
            return

        def job(log: Callable[[str], None], progress):
            from ..core.bios import bios_run
            progress(0, 0, "BIOS: skan + instalacja…")
            return bios_run(Path(src), emu_root=Path(emu_root), only=only,
                            log=log)

        self._run(job, lambda st: self._log(f"BIOS: {st.summary()}"))

    def _bios_import(self) -> None:
        p, _f = QFileDialog.getOpenFileName(
            self, tr("Wybierz libretro System.dat"), "",
            tr("DAT (*.dat);;Wszystkie pliki (*.*)"))
        if not p:
            return
        from ..core.bios import import_system_dat, load_manifest, save_manifest
        m = load_manifest()
        added = import_system_dat(Path(p), m)
        save_manifest(m)
        self._log(f"Import System.dat: dodano {added} definicji plików "
                  f"do manifestu.")

    # ── zakładka: Aktualizacje ─────────────────────────────────────────────

    def _build_tab_update(self) -> None:
        from ..core.updater import EMULATORS_UPDATE, load_versions
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(tr(
            "Aktualizacja aplikacji emulatorów. Rdzenie RetroArch aktualizuj "
            "w samym RetroArch (Online Updater) — tu tylko aplikacja. "
            "Sprawdź wersje wypełnia kolumnę Dostępna i zaznacza te z "
            "aktualizacją; potem Aktualizuj zaznaczone.")))
        self.tbl_upd = QTableWidget(0, 4)
        self.tbl_upd.setHorizontalHeaderLabels(
            [tr("✓ / Emulator"), tr("Zainstalowana"), tr("Dostępna"), tr("Źródło")])
        self.tbl_upd.horizontalHeader().setStretchLastSection(True)
        self.tbl_upd.setColumnWidth(0, 170)
        self.tbl_upd.setColumnWidth(1, 200)
        self.tbl_upd.setColumnWidth(2, 200)
        self.tbl_upd.verticalHeader().setVisible(False)
        versions = load_versions()
        for key, cfg in EMULATORS_UPDATE.items():
            r = self.tbl_upd.rowCount()
            self.tbl_upd.insertRow(r)
            name = QTableWidgetItem(key)
            name.setFlags((name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                          & ~Qt.ItemFlag.ItemIsEditable)
            name.setCheckState(Qt.CheckState.Unchecked)
            self.tbl_upd.setItem(r, 0, name)
            for c, text in ((1, versions.get(key, "—")), (2, "?"),
                            (3, cfg.get("repo", cfg["type"]))):
                it = QTableWidgetItem(text)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.tbl_upd.setItem(r, c, it)
        lay.addWidget(self.tbl_upd, 1)

        row = QHBoxLayout()
        self.btn_upd_check = QPushButton(tr("🔎 Sprawdź wersje"))
        self.btn_upd_check.clicked.connect(lambda: self._update_run(check=True))
        self.btn_upd_go = QPushButton(tr("⬆ Aktualizuj zaznaczone"))
        self.btn_upd_go.clicked.connect(lambda: self._update_run(check=False))
        self.btn_upd_force = QPushButton(tr("⟳ Wymuś (zaznaczone)"))
        self.btn_upd_force.clicked.connect(
            lambda: self._update_run(check=False, force=True))
        row.addWidget(self.btn_upd_check)
        row.addWidget(self.btn_upd_go)
        row.addWidget(self.btn_upd_force)
        row.addStretch()
        lay.addLayout(row)
        self.tabs.addTab(w, tr("Aktualizacje"))

    def _upd_checked_keys(self) -> list[str]:
        return [self.tbl_upd.item(r, 0).text()
                for r in range(self.tbl_upd.rowCount())
                if self.tbl_upd.item(r, 0).checkState() == Qt.CheckState.Checked]

    def _update_run(self, check: bool, force: bool = False) -> None:
        emu_root = self.row_emus.path or self.settings.emulators_dir
        if not emu_root or not Path(emu_root).is_dir():
            QMessageBox.warning(self, tr("Aktualizacje"),
                                tr("Wskaż katalog emulatorów."))
            return
        keys = None if check else (self._upd_checked_keys() or None)
        if not check:
            if keys is None:
                QMessageBox.information(self, tr("Aktualizacje"),
                    tr("Zaznacz (checkbox) emulatory do aktualizacji. "
                    "Najpierw Sprawdź wersje pokaże, które mają nowsze."))
                return
            ok = QMessageBox.question(
                self, tr("Aktualizacje"),
                tr("Zaktualizować:") + f" {', '.join(keys)}?\n"
                + tr("Configi i save'y są chronione (preserve), ale warto "
                     "mieć kopię."))
            if ok != QMessageBox.StandardButton.Yes:
                return

        def job(log: Callable[[str], None], progress):
            from ..core.updater import run_updates
            return run_updates(Path(emu_root), keys, check_only=check,
                               force=force, log=log,
                               on_progress=lambda i, n, t:
                                   progress(i, n, f"aktualizacja: {t}"))

        def done(result) -> None:
            _n, available = result
            from ..core.updater import load_versions as _lv
            versions = _lv()
            n_upd = 0
            for r in range(self.tbl_upd.rowCount()):
                key = self.tbl_upd.item(r, 0).text()
                self.tbl_upd.item(r, 1).setText(versions.get(key, "—"))
                if key in available:
                    avail, has = available[key]
                    self.tbl_upd.item(r, 2).setText(avail)
                    if check and has:
                        n_upd += 1
                        self.tbl_upd.item(r, 0).setCheckState(
                            Qt.CheckState.Checked)   # auto-zaznacz do aktualizacji
                        for c in range(4):
                            self.tbl_upd.item(r, c).setBackground(QBrush(_YELLOW))
                    elif check:
                        for c in range(4):
                            self.tbl_upd.item(r, c).setBackground(QBrush(_GREEN))
            if check:
                self._log(f"Sprawdzono: {n_upd} emulatorów ma nowszą wersję "
                          f"(zaznaczone na żółto). Kliknij Aktualizuj zaznaczone.")
            else:
                self._log("Aktualizacja zakończona.")

        self._run(job, done)

    # ── właściwości pomocnicze ────────────────────────────────────────────

    @property
    def _action_buttons(self) -> list[QPushButton]:
        return [self.btn_load_dats, self.btn_report, self.btn_find,
                self.btn_force_scan, self.btn_cue_rebuild,
                self.btn_fix, self.btn_scan, self.btn_dupes, self.btn_dedup_dry,
                self.btn_dedup, self.btn_icons, self.btn_lnk, self.btn_m3u,
                self.btn_bios_install, self.btn_upd_check, self.btn_upd_go,
                self.btn_upd_force]

"""Ustawienia przenośne (portable) zapisywane obok pliku wykonywalnego.

Zgodnie z wymaganiem: NIE w AppData. Gdy aplikacja jest zamrożona
(PyInstaller), katalog bazowy to folder z .exe; w trybie deweloperskim
to katalog projektu. Jeśli miejsce docelowe jest tylko-do-odczytu,
robimy fallback na katalog obok skryptu / bieżący i logujemy ostrzeżenie.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

SETTINGS_FILENAME = "chd_buddy_settings.json"


def app_base_dir() -> Path:
    """Katalog bazowy aplikacji (obok exe / w projekcie)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # __file__ = .../chd_buddy/core/settings.py -> root = dwa poziomy wyżej
    return Path(__file__).resolve().parents[2]


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".chd_buddy_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


@dataclass
class Settings:
    # Ścieżki do narzędzi (puste => szukaj w PATH).
    chdman_path: str = ""
    seven_zip_path: str = ""
    # Katalog wyjściowy dla nowych CHD (puste => obok źródła).
    output_dir: str = ""
    # Katalog TYMCZASOWY (scratch) na wypakowanie/kompresję. Wybór:
    #   RAM dysk (gdy aktywny i się mieści) → ten katalog (fallback) → auto
    #   (dysk z największym zapasem). Pusty => auto po RAM.
    scratch_dir: str = ""
    # Katalog roboczy na extract/temp (puste => obok przetwarzanego pliku).
    work_dir: str = ""
    # Kompresja: "default" => niech chdman wybierze wg polecenia.
    compression_preset: str = "default"   # CHD: kodeki (patrz presets.py)
    # ZIP: poziom DEFLATE 0–9 (0=bez kompresji, 6=domyślny zlib, 9=maks).
    zip_level: int = 6
    # RVZ (DolphinTool): poziom zstd 1–22 (5=domyślny) i rozmiar bloku w KB.
    rvz_level: int = 5
    rvz_block_kb: int = 128
    threads: int = 0                 # 0 => auto (chdman)
    verify_after_create: bool = True
    # Round-trip: po createdvd wypakuj obraz i porównaj SHA-1 ze źródłem.
    # Silniejsze niż verify (dowód danych, nie tylko kontenera) — domyślnie ON,
    # bo naprawa in-place kasuje oryginał. Wyłącz tylko gdy masz kopie zapasowe.
    verify_roundtrip: bool = True
    # Walidacja DAT (Redump): po deframe sprawdź SHA-1 obrazu w DAT. Trafienie =
    # kanonicznie poprawny zrzut. Brak trafienia => oryginał do kwarantanny.
    dat_dir: str = ""              # katalog z plikami .dat (pusty = wyłączone)
    quarantine_dir_name: str = "nieznane"  # podkatalog na niezweryfikowane
    # Usuwaj pliki źródłowe (cue/gdi/bin/iso) po UDANEJ konwersji do CHD.
    # Domyślnie OFF (operacja nieodwracalna); włącz świadomie przy low-disk.
    delete_source_after_convert: bool = False
    # Tryb naprawy typu przy małej ilości miejsca.
    aggressive_low_disk: bool = False   # usuwaj oryginał po udanym extract
    # Margines bezpieczeństwa miejsca (mnożnik szacowanego rozmiaru nowego CHD).
    new_chd_safety_factor: float = 1.15
    # Minimalny bufor wolnego miejsca do zachowania (bajty).
    min_free_reserve_bytes: int = 512 * 1024 * 1024
    # Próg logiczny (bajty) uznania CD-typed za podejrzany DVD.
    cd_max_logical_bytes: int = 950 * 1024 * 1024
    # Baza indeksu plików (puste => obok exe/projektu).
    index_db_path: str = ""
    # Klucz API SteamGridDB (fallback ikon, gdy Libretro nie ma miniatury).
    sgdb_api_key: str = ""
    # IGDB (Twitch): dodatkowe źródło grafik (cover/artwork/screenshot).
    igdb_client_id: str = ""
    igdb_client_secret: str = ""
    # TheGamesDB: klucz API (box art / fan art / clear logo / banner).
    tgdb_api_key: str = ""
    # Katalog główny emulatorów (do skrótów .lnk), np. D:\emu\Emulatory.
    emulators_dir: str = ""
    # Wybór emulatora per system: {"PS1": "DuckStation",
    #  "SATURN": "RetroArch:mednafen_saturn"}; brak wpisu = domyślny.
    system_emulators: dict = field(default_factory=dict)
    # Katalog-warsztat: jeden korzeń z podkatalogami Emulatory/roms/bios/dat/
    # "to sort" — ustawienie go wypełnia pozostałe ścieżki.
    workspace_dir: str = ""
    # Katalog z plikami BIOS (źródło skanu BiosManagera).
    bios_dir: str = ""
    # Katalogi wyjściowe (puste => obok gier, w podkatalogu icons/shortcuts).
    icons_out_dir: str = ""
    shortcuts_out_dir: str = ""
    # Kombajn: drzewo DAT-ów, drzewo ROM-ów, ToSort i katalogi indeksowane.
    dat_root: str = ""
    rom_root: str = ""
    # GŁÓWNY ToSort (Primary ToSort jak w RomVaulcie) — tam trafiają nieznane
    # pliki przy sprzątaniu.
    tosort_dir: str = ""
    # DODATKOWE katalogi ToSort (fizycznie gdzie indziej). Są skanowane i
    # przeszukiwane jak główny, ale nic do nich nie wrzucamy.
    tosort_extra: list = field(default_factory=list)
    index_roots: list = field(default_factory=list)
    # RAM dysk (ImDisk) na operacje tymczasowe: wypakowanie/przepakowanie CHD.
    # Ulotny — nie zapycha dysku kolekcji, nic nie zostaje po przerwaniu.
    ramdisk_enabled: bool = True
    ramdisk_size_gb: int = 30
    ramdisk_letter: str = "R"
    # Przy starcie proś o podniesienie do administratora (UAC), by móc tworzyć
    # symlinki bez trybu dewelopera. Odmowa UAC => program działa bez admina.
    auto_elevate: bool = True
    # Opcje naprawy Kombajnu (checkboxy w pasku) — trwałe między sesjami.
    fix_clean: bool = False          # nieznane → ToSort
    fix_incomplete: bool = False     # buduj też niekompletne gry
    fix_del_tosort: bool = True      # usuń z ToSort pliki już na miejscu
    fix_convert: bool = False        # konwertuj do formatu docelowego
    fix_dedup: bool = True           # kopie potwierdzonych → symlinki

    @property
    def tosort_dirs(self) -> list:
        """Wszystkie katalogi ToSort: główny (pierwszy) + dodatkowe."""
        out = [d for d in [self.tosort_dir] if d]
        for d in self.tosort_extra or []:
            if d and d not in out:
                out.append(d)
        return out

    # Interfejs
    language: str = "pl"
    _base_dir: str = field(default="", repr=False)

    # --- I/O ---------------------------------------------------------------

    @classmethod
    def path(cls) -> Path:
        base = app_base_dir()
        if not _is_writable(base):
            # fallback do katalogu tymczasowego użytkownika
            base = Path(tempfile.gettempdir()) / "chd_buddy"
        return base / SETTINGS_FILENAME

    @classmethod
    def load(cls) -> "Settings":
        p = cls.path()
        if not p.exists():
            s = cls()
            s._base_dir = str(p.parent)
            return s
        try:
            data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            s = cls()
            s._base_dir = str(p.parent)
            return s
        known = {f for f in cls().__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known and not k.startswith("_")}
        s = cls(**filtered)
        s._base_dir = str(p.parent)
        return s

    def save(self) -> Path:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        # zapis atomowy
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        return p

    # --- Wygodne akcesory --------------------------------------------------

    def apply_workspace(self, ws: str) -> list[str]:
        """Ustawia katalog-warsztat i wypełnia ścieżki z jego podkatalogów.

        Rozpoznawane podkatalogi (pierwszy istniejący wariant nazwy):
        Emulatory/emulatory, roms/ROMS/Romy, bios/BIOS, dat/DAT-y/daty,
        "to sort"/tosort/ToSort. Zwraca listę komunikatów (co ustawiono).
        """
        self.workspace_dir = ws
        base = Path(ws)
        out: list[str] = []

        def _pick(attr: str, names: tuple[str, ...], label: str) -> None:
            for n in names:
                d = base / n
                if d.is_dir():
                    setattr(self, attr, str(d))
                    out.append(f"{label}: {d}")
                    return
            out.append(f"{label}: (brak podkatalogu {'/'.join(names)})")

        _pick("emulators_dir", ("Emulatory", "emulatory", "emulators"),
              "emulatory")
        _pick("rom_root", ("roms", "ROMS", "romy", "Romy"), "ROM-y")
        _pick("bios_dir", ("bios", "BIOS"), "BIOS")
        _pick("dat_root", ("dat", "DAT", "daty", "dats"), "DAT-y")
        _pick("tosort_dir", ("to sort", "tosort", "ToSort", "to_sort"),
              "ToSort")
        return out

    def resolved_output_dir(self, src: Path) -> Path:
        if self.output_dir:
            return Path(self.output_dir)
        return src.parent

    def resolved_work_dir(self, src: Path) -> Path:
        # Jawny „Katalog roboczy" ma pierwszeństwo (świadomy override).
        if self.work_dir:
            return Path(self.work_dir)
        # Inaczej dedykowany temp: RAM dysk (gdy aktywny) → scratch_dir
        # (fallback) → obok pliku. Kontrola miejsca downstream (diskbudget)
        # złapie gdyby RAM był za mały.
        try:
            from .ramdisk import active_root
            ram = active_root()
            if ram is not None:
                return ram
        except Exception:
            pass
        if self.scratch_dir and Path(self.scratch_dir).is_dir():
            return Path(self.scratch_dir)
        return src.parent

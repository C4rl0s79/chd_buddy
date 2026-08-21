"""Lekka lokalizacja UI: polski tekst źródłowy jest KLUCZEM.

Zasada: w kodzie zostają polskie napisy owinięte w `tr(...)`. Gdy język = "en",
`tr` zwraca angielski odpowiednik ze słownika `_EN`; gdy brak wpisu (albo język
= "pl") — zwraca oryginał. Dzięki temu:
  * polski działa bez żadnego słownika (zero ryzyka regresji),
  * angielski dokłada się przyrostowo (brak tłumaczenia = polski fallback),
  * nie trzeba Qt Linguist / plików .ts/.qm.

Zmiana języka w GUI zapisuje ustawienie i prosi o restart — UI jest budowany
ręcznie, więc pełny retranslate w locie byłby kruchy; restart jest pewny.
"""
from __future__ import annotations

# Dostępne języki: kod -> etykieta w menu wyboru.
LANGUAGES: dict[str, str] = {"pl": "Polski", "en": "English"}

_LANG = "pl"


def set_language(lang: str | None) -> None:
    global _LANG
    code = (lang or "pl").lower()
    _LANG = "en" if code.startswith("en") else "pl"


def get_language() -> str:
    return _LANG


def tr(text: str) -> str:
    """Tłumaczy `text` na aktywny język (fallback: tekst źródłowy = polski)."""
    if _LANG == "pl":
        return text
    return _EN.get(text, text)


# --- słownik EN: polski tekst źródłowy -> angielski --------------------------
_EN: dict[str, str] = {
    # okno / menu
    "ROM Kombajn — chd_buddy": "ROM Combine — chd_buddy",
    "Narzędzia": "Tools",
    "Ustawienia globalne DAT-ów (format / nazwy / rom_root)…":
        "Global DAT settings (format / naming / rom_root)…",
    "Klucze API grafik (SGDB / IGDB / TheGamesDB)…":
        "Artwork API keys (SGDB / IGDB / TheGamesDB)…",
    "RAM dysk (operacje tymczasowe)…": "RAM disk (temporary operations)…",
    "Kompresja (CHD / ZIP / RVZ)…": "Compression (CHD / ZIP / RVZ)…",
    "Kompresja (CHD / ZIP / RVZ)": "Compression (CHD / ZIP / RVZ)",
    "CHD: dobór kodeków chdman. default = chdman decyduje; "
    "max = najlepsza kompresja (wolniej); fast = szybciej; "
    "none = bez kompresji.":
        "CHD: chdman codec choice. default = chdman decides; max = best "
        "compression (slower); fast = faster; none = no compression.",
    "ZIP: poziom DEFLATE. 0 = bez kompresji (szybko), "
    "6 = domyślny, 9 = najmniejszy plik (wolniej).":
        "ZIP: DEFLATE level. 0 = no compression (fast), 6 = default, "
        "9 = smallest file (slower).",
    "RVZ (GameCube/Wii): poziom zstd 1–22. 5 = domyślny; "
    "wyżej = mniejszy plik, wolniej.":
        "RVZ (GameCube/Wii): zstd level 1–22. 5 = default; higher = smaller "
        "file, slower.",
    "RVZ: rozmiar bloku (128 KB = domyślny).":
        "RVZ: block size (128 KB = default).",
    "Preset kompresji CHD:": "CHD compression preset:",
    "Poziom ZIP (0–9):": "ZIP level (0–9):",
    "Poziom RVZ (zstd 1–22):": "RVZ level (zstd 1–22):",
    "Blok RVZ:": "RVZ block:",
    "Poziomy działają przy naprawie/konwersji do formatu "
    "docelowego. CHD to wybór kodeków (nie liczba). Zmiana "
    "działa od następnej konwersji.":
        "Levels apply when fixing/converting to the target format. CHD is a "
        "codec choice (not a number). Change takes effect from the next "
        "conversion.",
    "Kompresja zapisana:": "Compression saved:",
    "⚙ poziomy…": "⚙ levels…",
    "Poziomy kompresji CHD / ZIP / RVZ używane "
    "przy konwersji do formatu docelowego.":
        "CHD / ZIP / RVZ compression levels used when converting to the "
        "target format.",
    "Język / Language…": "Language / Język…",
    "Klasyczne narzędzie CHD…": "Classic CHD tool…",

    # zakładki
    "Kolekcja (DAT)": "Collection (DAT)",
    "Indeks": "Index",
    "Ikony i skróty": "Icons & shortcuts",
    "BIOS": "BIOS",
    "Aktualizacje": "Updates",

    # etykiety ścieżek
    "Warsztat (katalog główny):": "Workspace (main folder):",
    "Katalog DAT-ów:": "DAT folder:",
    "Katalog ROM-ów:": "ROM folder:",
    "ToSort (nieznane):": "ToSort (unknown):",

    # przyciski kolekcji
    "📋 Wczytaj DAT-y": "📋 Load DATs",
    "🔍 Skanuj i raportuj": "🔍 Scan and report",
    "🔎 Znajdź naprawy": "🔎 Find fixes",
    "🔧 Napraw (wykonaj)": "🔧 Fix (apply)",
    "🔧 Odbuduj CHD wg cue": "🔧 Rebuild CHD from cue",
    "🔄 Wymuś pełny skan katalogu…": "🔄 Force full folder scan…",
    "⚙ Zależności DAT-ów (rodzic/dziecko)…":
        "⚙ DAT dependencies (parent/child)…",
    "⚙ Ustawienia zaznaczonego DAT-a…": "⚙ Selected DAT settings…",

    # checkboxy naprawy
    "nieznane → ToSort": "unknown → ToSort",
    "buduj też niekompletne gry": "build incomplete games too",
    "usuń z ToSort pliki już na miejscu": "delete ToSort files already in place",
    "konwertuj do formatu docelowego": "convert to target format",
    "kopie potwierdzonych → symlinki": "confirmed copies → symlinks",
    "twórz symlinki dla DAT-ów dzieci": "create symlinks for child DATs",
    "uruchamiaj jako administrator (auto)": "run as administrator (auto)",
    "naprawa:": "fix level:",

    # symlinki / admin
    "🛡 Uruchom jako administrator": "🛡 Run as administrator",

    # okno postępu
    "Ogółem (pliki):": "Overall (files):",
    "Start…": "Start…",
    "⛔ Przerwij": "⛔ Cancel",
    "Zamknij": "Close",
    "Gotowe": "Done",
    "BŁĄD — szczegóły w logu": "ERROR — see log",
    "PRZERWANE — postęp zapisany, można wznowić":
        "CANCELLED — progress saved, can resume",
    "Przerywam po bieżącej operacji… (postęp zapisany)":
        "Cancelling after current operation… (progress saved)",
    "== ŻĄDANIE PRZERWANIA — kończę bieżącą operację ==":
        "== CANCEL REQUESTED — finishing current operation ==",

    # dialog naprawy
    "Napraw kolekcję": "Fix collection",
    "Znajdź naprawy (podgląd)": "Find fixes (preview)",
    "Naprawa kolekcji": "Fixing collection",
    "Wykonać?": "Proceed?",

    # dialog RAM dysku
    "RAM dysk — operacje tymczasowe": "RAM disk — temporary operations",
    "Używaj RAM dysku (ImDisk) do wypakowania/przepakowania":
        "Use RAM disk (ImDisk) for extract/repack",
    "Rozmiar:": "Size:",
    "Litera dysku:": "Drive letter:",
    "Katalog tymczasowy (fallback):": "Temp folder (fallback):",
    "Gdy RAM dysk jest nieaktywny albo za mały na daną operację — "
    "pliki tymczasowe idą TUTAJ (a nie na dysk kolekcji). Puste = "
    "automatyczny wybór dysku z największym zapasem.":
        "When the RAM disk is inactive or too small for an operation — temp "
        "files go HERE (not to the collection disk). Empty = auto-pick the "
        "disk with the most free space.",
    "ImDisk wykryty": "ImDisk detected",
    "ImDisk NIEwykryty — operacje pójdą na dysk fizyczny":
        "ImDisk NOT detected — operations will use the physical disk",

    # dialog języka
    "Język / Language": "Language / Język",
    "Wybierz język interfejsu. Zmiana zadziała po restarcie programu.":
        "Choose the interface language. Takes effect after restarting the program.",
    "Język zapisany. Uruchom program ponownie, aby zastosować.":
        "Language saved. Restart the program to apply.",

    # --- zakładka Indeks ---
    "Zakładka ZAAWANSOWANA — indeks to wewnętrzna baza sum kontrolnych "
    "(zwykle nie musisz jej ustawiać ręcznie; przycisk Skanuj i "
    "raportuj na zakładce Kolekcja robi to sam).\n"
    "Katalogi poniżej służą do skanu na żądanie i deduplikacji "
    "(kolejność = priorytet kopii fizycznej):":
        "ADVANCED tab — the index is an internal checksum database "
        "(you normally don't set it by hand; the Scan and report button on "
        "the Collection tab does it for you).\n"
        "The folders below are for on-demand scanning and deduplication "
        "(order = physical-copy priority):",
    "➕ Dodaj katalog": "➕ Add folder",
    "➖ Usuń zaznaczony": "➖ Remove selected",
    "🔍 Skanuj przyrostowo": "🔍 Incremental scan",
    "pełny re-skan (licz sumy od nowa)": "full re-scan (recompute checksums)",
    "SHA-1 zawartości CHD (chdman)": "SHA-1 of CHD contents (chdman)",
    "👥 Pokaż duplikaty": "👥 Show duplicates",
    "🔗 Dedup (podgląd)": "🔗 Dedup (preview)",
    "🔗 Dedup (wykonaj)": "🔗 Dedup (apply)",
    "Katalog do indeksu": "Folder to index",
    "Dodaj przynajmniej jeden katalog.": "Add at least one folder.",
    "Deduplikacja": "Deduplication",

    # --- zakładka Ikony i skróty ---
    "Katalog gier:": "Games folder:",
    "Katalog emulatorów:": "Emulators folder:",
    "Ikony → (puste = obok gier, w \\icons):":
        "Icons → (empty = next to games, in \\icons):",
    "Skróty → (puste = obok gier, w \\shortcuts):":
        "Shortcuts → (empty = next to games, in \\shortcuts):",
    "podkatalogi = systemy (RomRoot)": "subfolders = systems (RomRoot)",
    "nadpisuj istniejące": "overwrite existing",
    "🖼 Twórz ikony": "🖼 Create icons",
    "🔗 Twórz skróty .lnk": "🔗 Create .lnk shortcuts",
    "🎵 Generuj playlisty .m3u": "🎵 Generate .m3u playlists",
    "🎮 Wykryj systemy i emulatory": "🎮 Detect systems and emulators",
    "Emulator per system (standalone albo rdzeń "
    "RetroArch); wybór zapisuje się w ustawieniach:":
        "Emulator per system (standalone or RetroArch "
        "core); the choice is saved in settings:",
    "System (katalog)": "System (folder)",
    "Emulator": "Emulator",
    "Wskaż istniejący katalog gier.": "Point to an existing games folder.",
    "Wskaż katalog gier i katalog emulatorów.":
        "Point to the games folder and the emulators folder.",
    "Wskaż katalog emulatorów.": "Point to the emulators folder.",

    # --- zakładka BIOS ---
    "Katalog BIOS-ów (skan po MD5):": "BIOS folder (scan by MD5):",
    "Emulatory (zaznaczone dostaną BIOS-y wg "
    "manifestu bios_manifest.json):":
        "Emulators (checked ones get BIOS files per "
        "bios_manifest.json manifest):",
    "📦 Skanuj i instaluj do emulatorów": "📦 Scan and install to emulators",
    "📥 Import System.dat": "📥 Import System.dat",
    "Wskaż istniejący katalog BIOS-ów.": "Point to an existing BIOS folder.",
    "Wskaż katalog emulatorów (zakładka Ikony i skróty).":
        "Point to the emulators folder (Icons & shortcuts tab).",
    "Zaznacz przynajmniej jeden emulator.": "Check at least one emulator.",
    "Wybierz libretro System.dat": "Choose libretro System.dat",
    "DAT (*.dat);;Wszystkie pliki (*.*)": "DAT (*.dat);;All files (*.*)",

    # --- zakładka Aktualizacje ---
    "Aktualizacja aplikacji emulatorów. Rdzenie RetroArch aktualizuj "
    "w samym RetroArch (Online Updater) — tu tylko aplikacja. "
    "Sprawdź wersje wypełnia kolumnę Dostępna i zaznacza te z "
    "aktualizacją; potem Aktualizuj zaznaczone.":
        "Update emulator applications. Update RetroArch cores in RetroArch "
        "itself (Online Updater) — here only the app. Check versions fills "
        "the Available column and checks those with an update; then Update "
        "selected.",
    "✓ / Emulator": "✓ / Emulator",
    "Zainstalowana": "Installed",
    "Dostępna": "Available",
    "Źródło": "Source",
    "🔎 Sprawdź wersje": "🔎 Check versions",
    "⬆ Aktualizuj zaznaczone": "⬆ Update selected",
    "⟳ Wymuś (zaznaczone)": "⟳ Force (selected)",

    # --- dialog ustawień DAT-a ---
    "zostaw jak jest (bez konwersji)": "keep as is (no conversion)",
    "AUTO wg systemu (płyta→CHD, GC/Wii→RVZ, kartridż→ZIP)":
        "AUTO by system (disc→CHD, GC/Wii→RVZ, cartridge→ZIP)",
    "wypakowane w podkatalogach": "extracted in subfolders",
    "ZIP (spakowane)": "ZIP (packed)",
    "7z (spakowane)": "7z (packed)",
    "CHD (systemy płytowe)": "CHD (disc systems)",
    "RVZ (GameCube / Wii)": "RVZ (GameCube / Wii)",
    "Ustawienia DAT-a:": "DAT settings:",
    "puste = wg struktury (rom_root/…/nazwa)":
        "empty = by structure (rom_root/…/name)",
    "Wybierz…": "Browse…",
    "Katalog docelowy (względem ROM-ów):": "Target folder (relative to ROMs):",
    "gry wieloplikowe w podkatalogu per gra "
    "(odznacz = płasko)":
        "multi-file games in a per-game subfolder (uncheck = flat)",
    "Układ:": "Layout:",
    "Sugeruj": "Suggest",
    "CHD dla płyt; RVZ dla GameCube/Wii; reszta: zostaw.":
        "CHD for discs; RVZ for GameCube/Wii; rest: keep.",
    "Format przechowywania (dziedziczony z rodzica):":
        "Storage format (inherited from parent):",
    "Format przechowywania (rodzic):": "Storage format (parent):",
    "pomiń ten DAT (nie raportuj / nie buduj)":
        "skip this DAT (don't report / don't build)",
    "buduj tylko kompletne gry": "build only complete games",
    "podmieniaj wersje (Japan) na tłumaczenia [T-En]":
        "replace (Japan) versions with translations [T-En]",
    "To DAT-DZIECKO swojej platformy — jego pliki to "
    "symlinki do plików RODZICA, więc format jest "
    "dziedziczony i niezmienialny tutaj. Zmień go w "
    "ustawieniach DAT-a rodzica (albo w oknie hierarchii).":
        "This is a CHILD DAT of its platform — its files are symlinks to the "
        "PARENT's files, so the format is inherited and not editable here. "
        "Change it in the parent DAT's settings (or in the hierarchy window).",
    "Format (chd/rvz/zip) to docelowy sposób przechowywania "
    "RODZICA — dzieci platformy dziedziczą go automatycznie "
    "(są symlinkami). Konwersja przy naprawie jest osobnym "
    "krokiem; teraz zapisujesz preferencję.":
        "Format (chd/rvz/zip) is the PARENT's target storage — platform "
        "children inherit it automatically (they are symlinks). Conversion "
        "during fixing is a separate step; you're saving the preference now.",
    "Katalog docelowy": "Target folder",
    "Ustawienia": "Settings",
    "Nie zapisano:": "Not saved:",

    # --- dialog ustawień KATALOGU DAT-ów ---
    "Ustawienia globalne (wszystkie DAT-y)": "Global settings (all DATs)",
    "Ustawienia katalogu:": "Folder settings:",
    "obowiązuje wszystkie DAT-y "
    "w tym katalogu; pojedynczy DAT może nadpisać.":
        "applies to all DATs in this folder; an individual DAT can override.",
    "wszystkie DAT-y tego katalogu są RODZICAMI "
    "swoich platform (trzymają pliki fizyczne)":
        "all DATs in this folder are PARENTS of their platforms "
        "(they hold the physical files)",
    "Rola:": "Role:",
    "zostaw jak jest": "keep as is",
    "Format przechowywania:": "Storage format:",
    "z DAT-a (<header><name>, np. Sony - PlayStation 2)":
        "from DAT (<header><name>, e.g. Sony - PlayStation 2)",
    "EmulationStation (np. ps2, psx, dreamcast)":
        "EmulationStation (e.g. ps2, psx, dreamcast)",
    "Nazwy katalogów per system:": "Folder names per system:",
    "puste = główny katalog ROM-ów": "empty = main ROM folder",
    "Osobny rom_root (np. dla dzieci):": "Separate rom_root (e.g. for children):",
    "gry wieloplikowe w podkatalogu per gra":
        "multi-file games in a per-game subfolder",
    "podmieniaj (Japan) na tłumaczenia [T-En]":
        "replace (Japan) with translations [T-En]",
    "Osobny katalog ROM-ów": "Separate ROM folder",

    # --- zbiorcza edycja wielu DAT-ów ---
    "Ustawienia": "Settings",
    "zaznaczonych DAT-ów": "selected DATs",
    "Zbiorcza edycja {n} DAT-ów.": "Bulk editing {n} DATs.",
    "Zaznacz pole wyboru zmien obok ustawien, ktore chcesz zapisac "
    "dla WSZYSTKICH wybranych — reszta ustawien kazdego DAT-a zostaje "
    "nietknieta.":
        "Tick the 'change' checkbox next to the settings you want to save for "
        "ALL selected — the rest of each DAT's settings stays untouched.",
    "z DAT-a (<header><name>)": "from DAT (<header><name>)",
    "EmulationStation (ps2, psx, dreamcast…)":
        "EmulationStation (ps2, psx, dreamcast…)",
    "Kompletność:": "Completeness:",
    "Dedup:": "Dedup:",
    "Tłumaczenia:": "Translations:",
    "Pomiń:": "Skip:",
    "pomiń te DAT-y (nie raportuj / nie buduj)":
        "skip these DATs (don't report / don't build)",
    "zmień": "change",

    # --- okno hierarchii (rodzic/dziecko) ---
    "Zależności DAT-ów — rodzic → dzieci (per platforma)":
        "DAT dependencies — parent → children (per platform)",
    "WSZYSTKIE platformy (także z jednym DAT-em). W obrębie platformy: "
    "góra = RODZIC (pliki fizyczne), niżej = dzieci (symlinki). "
    "DAT o innej nazwie (np. FinalBurn Neo - SNES Games) możesz "
    "PRZYPIĄĆ do platformy rodzica przyciskiem 🔗 — stanie się jej "
    "dzieckiem (hierarchia + wspólny format).":
        "ALL platforms (including single-DAT ones). Within a platform: "
        "top = PARENT (physical files), below = children (symlinks). "
        "A differently-named DAT (e.g. FinalBurn Neo - SNES Games) can be "
        "PINNED to the parent platform with the 🔗 button — it becomes its "
        "child (hierarchy + shared format).",
    "Platforma / DAT": "Platform / DAT",
    "ROM-ów": "ROMs",
    "rola": "role",
    "katalog docelowy": "target folder",
    "Platform: {n} (z hierarchią: {m}; pojedyncze "
    "zwinięte). 🔗 przypina DAT do innej platformy, "
    "✂ przywraca własną.":
        "Platforms: {n} (with hierarchy: {m}; single ones collapsed). "
        "🔗 pins a DAT to another platform, ✂ restores its own.",
    "▲ wyżej": "▲ up",
    "▼ niżej": "▼ down",
    "⭐ Ustaw jako rodzic": "⭐ Set as parent",
    "🔗 Przypnij do platformy…": "🔗 Pin to platform…",
    "Wybrany DAT staje się DZIECKIEM wskazanej platformy — dostaje "
    "symlinki do plików jej rodzica i dziedziczy format, mimo że "
    "nazwa DAT-a jest inna (np. FinalBurn Neo → Nintendo SNES).":
        "The selected DAT becomes a CHILD of the chosen platform — it gets "
        "symlinks to its parent's files and inherits the format, even though "
        "the DAT name is different (e.g. FinalBurn Neo → Nintendo SNES).",
    "✂ Odepnij (własna platforma)": "✂ Unpin (own platform)",
    "📁 Zmień katalog docelowy…": "📁 Change target folder…",
    "Przypnij do platformy": "Pin to platform",
    "Stanie się DZIECKIEM platformy:": "It will become a CHILD of platform:",
    "Katalog docelowy dla:": "Target folder for:",
    "Ścieżka względem katalogu ROM-ów (np. ps2):":
        "Path relative to the ROM folder (e.g. ps2):",
    "Zależności": "Dependencies",
    "RODZIC": "PARENT",
    "dziecko": "child",
    "DAT": "DAT",
    "-ów": "s",

    # --- menu kontekstowe (prawy klik) ---
    "🖼 Stwórz ikonę:": "🖼 Create icon:",
    "🔄 Wymuś pełny skan tego katalogu": "🔄 Force full scan of this folder",
    "➕ Dodaj kolejny katalog ToSort…": "➕ Add another ToSort folder…",
    "📁 Zmień lokalizację głównego ToSort…": "📁 Change main ToSort location…",
    "➖ Usuń ten katalog z listy ToSort": "➖ Remove this folder from ToSort list",
    "⚙ Ustawienia": "⚙ Settings",
    "zaznaczonych DAT-ów…": "selected DATs…",
    "⚙ Ustawienia katalogu": "⚙ Folder settings",
    "(wszystkie DAT-y)…": "(all DATs)…",
    "⭐ Wszystkie DAT-y tu = rodzice platform":
        "⭐ All DATs here = platform parents",
    "⚙ Ustawienia DAT-a…": "⚙ DAT settings…",
    "⚙ Zależności (rodzic/dziecko)…": "⚙ Dependencies (parent/child)…",
    "🔄 Wymuś pełny skan katalogu tego DAT-a":
        "🔄 Force full scan of this DAT's folder",
    "🖼 Generuj ikony dla całego DAT-a": "🖼 Generate icons for the whole DAT",

    # --- okno postępu (czas) ---
    "czas:": "time:",

    # --- klasyczne narzędzie CHD: okno ustawień ---
    "Wybierz plik": "Choose file",
    "Wybierz katalog": "Choose folder",
    "Weryfikuj po utworzeniu": "Verify after creation",
    "Tryb low-disk (usuwaj oryginał po extract)":
        "Low-disk mode (delete original after extract)",
    "Walidacja round-trip (gdy brak DAT)": "Round-trip validation (when no DAT)",
    "Usuń źródła po udanej konwersji (cue/gdi/bin/iso)":
        "Delete sources after successful conversion (cue/gdi/bin/iso)",
    "folder ze wszystkimi .dat (PS1/PS2/DC/Saturn) — dopasowanie po SHA-1":
        "folder with all .dat files (PS1/PS2/DC/Saturn) — matched by SHA-1",
    "nazwa podkatalogu na niezweryfikowane, np. nieznane":
        "subfolder name for unverified files, e.g. unknown",
    "chdman:": "chdman:",
    "7-Zip:": "7-Zip:",
    "Katalog wyjściowy:": "Output folder:",
    "Katalog roboczy:": "Work folder:",
    "Folder DAT:": "DAT folder:",
    "Kwarantanna (podkatalog):": "Quarantine (subfolder):",
    "Preset kompresji:": "Compression preset:",
    "Wątki:": "Threads:",

    # --- okno wyboru ikony ---
    "Ikona:": "Icon:",
    "Szukam grafik dla:": "Searching artwork for:",
    "Zapisz ikonę do:": "Save icon to:",
    "Zmień folder…": "Change folder…",
    "szukam grafik…": "searching artwork…",
    "💾 Zapisz ikonę": "💾 Save icon",
    "gotowe": "done",
    "Błąd pobierania kandydatów:": "Error fetching candidates:",
    "Nie znaleziono żadnych grafik "
    "(Libretro/SGDB/IGDB/TGDB).":
        "No artwork found (Libretro/SGDB/IGDB/TGDB).",
    "Kandydatów: {n} — wybierz i Zapisz (dwuklik też "
    "działa).":
        "Candidates: {n} — pick one and Save (double-click works too).",
    "Katalog docelowy ikony": "Icon target folder",
    "Ikona": "Icon",
    "Zaznacz grafikę.": "Select an image.",
    "Nie udało się zapisać:": "Could not save:",
    "Nie udało się pobrać pełnego obrazu.":
        "Could not download the full image.",

    # --- klasyczne narzędzie CHD (główne okno) ---
    "CHD Buddy": "ROM Helper",
    "📁  Przeciągnij tu pliki/foldery (CHD lub źródła)\n"
    "lub kliknij „Dodaj…”":
        "📁  Drag files/folders here (CHD or sources)\n"
        "or click “Add…”",
    "Audyt (wykryj złe CHD)": "Audit (detect bad CHD)",
    "Konwertuj źródła → CHD": "Convert sources → CHD",
    "Rekompresja w miejscu": "Recompress in place",
    "Napraw typ (DVD-jako-CD)": "Fix type (DVD-as-CD)",
    "Tryb:": "Mode:",
    "Dodaj…": "Add…",
    "Log operacji…": "Operation log…",
    "▶ Start": "▶ Start",
    "🔧 Napraw wykryte": "🔧 Fix detected",
    "Napraw (retype) obrazy oznaczone w audycie jako "
    "podejrzane — bez ponownego skanowania.":
        "Fix (retype) images flagged suspicious in the audit — without "
        "re-scanning.",
    "🔍 Sprawdź dokładnie (DAT)": "🔍 Deep check (DAT)",
    "Zaznacz jeden plik: próbuje różnych metod "
    "wypakowania (extractdvd/cd/hd/raw/ld + deframe) "
    "aż wynik zwaliduje się w DAT. Wymaga folderu DAT.":
        "Select one file: tries different extraction methods "
        "(extractdvd/cd/hd/raw/ld + deframe) until the result validates "
        "against the DAT. Requires a DAT folder.",
    "⏹ Przerwij": "⏹ Cancel",
    "🗑 Wyczyść": "🗑 Clear",
    "⚙ Ustawienia": "⚙ Settings",
    "Wybierz pliki": "Choose files",
    "Obsługiwane (*.chd *.cue *.gdi *.iso *.img *.toc *.nrg);;Wszystkie (*)":
        "Supported (*.chd *.cue *.gdi *.iso *.img *.toc *.nrg);;All (*)",
    "Brak wykrytych": "Nothing detected",
    "Najpierw uruchom audyt — nie ma obrazów "
    "oznaczonych jako podejrzane.":
        "Run the audit first — there are no images flagged as suspicious.",
    "Zaznacz plik": "Select a file",
    "Zaznacz w tabeli dokładnie jeden plik CHD.":
        "Select exactly one CHD file in the table.",
    "Brak działającego chdman": "No working chdman",
    "Wskaż poprawny chdman.exe w Ustawieniach.":
        "Point to a valid chdman.exe in Settings.",
    "Brak DAT": "No DAT",
    "Głęboka walidacja wymaga wczytanego DAT. "
    "Ustaw folder DAT w Ustawieniach.":
        "Deep validation requires a loaded DAT. Set the DAT folder in Settings.",
    "Nie znaleziono działającego chdman. Sprawdź ścieżkę "
    "w Ustawieniach oraz czy antywirus nie usunął/wyzerował "
    "chdman.exe (dodaj wykluczenie dla chdman.exe i folderu ROM).":
        "No working chdman found. Check the path in Settings and whether "
        "antivirus removed/zeroed chdman.exe (add an exclusion for chdman.exe "
        "and the ROM folder).",

    # --- tooltipy i filtry zakładki Kolekcja ---
    "Jeden katalog z podkatalogami: Emulatory, roms, bios, dat, "
    "to sort — ustawienie go wypełnia wszystkie ścieżki.":
        "One folder with subfolders: Emulatory, roms, bios, dat, "
        "to sort — setting it fills in all the paths.",
    "Pokaż drzewo DAT-ów i liczby ROM-ów "
    "BEZ skanowania kolekcji (szybkie).":
        "Show the DAT tree and ROM counts WITHOUT scanning the collection "
        "(fast).",
    "Skanuje kolekcję i pokazuje per DAT: ile "
    "plików na miejscu, ile do naprawy, ile "
    "brak — statystyki widać wprost przy DAT-cie.":
        "Scans the collection and shows per DAT: how many files are in place, "
        "how many need fixing, how many are missing — stats shown right at "
        "the DAT.",
    "Liczy PLAN naprawy z wyników ostatniego skanu — nic nie zmienia "
    "na dysku. Pokazuje dokładnie co i skąd zostanie przeniesione, "
    "przepakowane albo podlinkowane.":
        "Computes the fix PLAN from the last scan — changes nothing on disk. "
        "Shows exactly what and from where will be moved, repacked or linked.",
    "Wykonuje plan z ostatniego skanu — NIE skanuje "
    "ponownie. Walidacja SHA-1 przy wypakowaniu i "
    "round-trip przy CHD. Można przerwać w każdej "
    "chwili; zrobione zostaje zrobione.":
        "Executes the plan from the last scan — does NOT re-scan. SHA-1 "
        "validation on extract and round-trip for CHD. Can be cancelled "
        "anytime; what's done stays done.",
    "Plik w ToSort, którego potwierdzona kopia jest już we właściwej "
    "lokalizacji (identyczny SHA-1), jest KASOWANY jako zbędny.":
        "A ToSort file whose confirmed copy is already in the right location "
        "(identical SHA-1) is DELETED as redundant.",
    "Po naprawie przepakowuje pliki do formatu z reguł (kartridż→ZIP, "
    "płyta→CHD, GameCube/Wii→RVZ). Każda konwersja weryfikowana; "
    "źródło kasowane dopiero po sukcesie. Wymaga chdman/DolphinTool.":
        "After fixing, repacks files to the format from the rules "
        "(cartridge→ZIP, disc→CHD, GameCube/Wii→RVZ). Each conversion is "
        "verified; the source is deleted only after success. Requires "
        "chdman/DolphinTool.",
    "Po naprawie fizyczne KOPIE potwierdzonych plików (w drzewie "
    "ROM-ów i ToSort) są zamieniane na symlinki — kopia fizyczna "
    "zostaje tylko w katalogu DAT-a rodzica.":
        "After fixing, physical COPIES of confirmed files (in the ROM tree "
        "and ToSort) are replaced with symlinks — the physical copy stays "
        "only in the parent DAT's folder.",
    "Przelicza sumy WSZYSTKICH plików we wskazanym katalogu, także już "
    "znanych z cache. Normalnie niepotrzebne — nowe pliki i tak są "
    "liczone w pełni automatycznie.":
        "Recomputes checksums of ALL files in the chosen folder, including "
        "those already known from cache. Normally unnecessary — new files "
        "are fully computed automatically anyway.",
    "DAT-y dzieci dostają symlinki do plików rodzica (jedna kopia "
    "fizyczna). Gdy symlinków NIE DA SIĘ utworzyć, nic nie jest "
    "kopiowane — te miejsca zostają puste.":
        "Child DATs get symlinks to the parent's files (one physical copy). "
        "When symlinks CANNOT be created, nothing is copied — those spots "
        "stay empty.",
    "Restart programu z podniesionymi uprawnieniami (UAC), by móc "
    "tworzyć symlinki. Alternatywa: włącz tryb dewelopera Windows.":
        "Restart the program elevated (UAC) to be able to create symlinks. "
        "Alternative: enable Windows developer mode.",
    "Przy każdym starcie program prosi o podniesienie uprawnień (UAC), "
    "aby móc tworzyć symlinki bez trybu dewelopera. Odmowa UAC = "
    "program działa dalej bez admina. Zmiana od następnego startu.":
        "At every start the program asks for elevation (UAC) so it can create "
        "symlinks without developer mode. Declining UAC = the program keeps "
        "running without admin. Change takes effect from the next start.",
    "Osobne okno: DAT-y pogrupowane po PLATFORMIE. "
    "W obrębie platformy ustalasz, który DAT jest "
    "rodzicem (trzyma pliki fizycznie), a które "
    "dziećmi (symlinki). Zapisuje _priorytet.txt.":
        "Separate window: DATs grouped by PLATFORM. Within a platform you set "
        "which DAT is the parent (holds physical files) and which are "
        "children (symlinks). Saves _priorytet.txt.",
    "Katalog docelowy, układ (podkatalog/"
    "płasko), format przechowywania, reguły.":
        "Target folder, layout (subfolder/flat), storage format, rules.",
    "Sortuj gry:": "Sort games:",
    "alfabetycznie": "alphabetically",
    "rozmiar (malejąco)": "size (descending)",
    "Pokaż:": "Show:",
    "wszystkie": "all",
    "✅ tylko komplet": "✅ complete only",
    "🔧 tylko do naprawy": "🔧 to-fix only",
    "⛔ tylko brakujące": "⛔ missing only",
    "◐ niekompletne (brak części plików)":
        "◐ incomplete (some files missing)",
    "Filtruje listę gier po stanie z ostatniego skanu.":
        "Filters the game list by status from the last scan.",

    # --- komunikaty / okna dialogowe suite_window ---
    "pracuję…": "working…",
    "błąd": "error",
    "Poczekaj na zakończenie bieżącej operacji.":
        "Wait for the current operation to finish.",
    "Operacja nie powiodła się:": "Operation failed:",
    "Wskaż istniejący katalog DAT-ów.": "Point to an existing DAT folder.",
    "Wskaż katalog główny ROM-ów.": "Point to the main ROM folder.",
    "Najpierw wczytaj DAT-y (przycisk Wczytaj "
    "DAT-y albo Skanuj i raportuj).":
        "Load the DATs first (Load DATs button or Scan and report).",
    "Zaznacz DAT w lewym panelu.": "Select a DAT in the left panel.",
    "Trwa operacja": "Operation in progress",
    "W trakcie skanowania/naprawy nie można zmieniać ustawień "
    "DAT-ów.\nPo zmianie ustawień zrób ponownie skan (szybki) "
    "i Znajdź naprawy.":
        "You can't change DAT settings during a scan/fix.\nAfter changing "
        "settings, re-run the (fast) scan and Find fixes.",
    "Odbudowa CHD": "CHD rebuild",
    "Najpierw Wczytaj DAT-y / Skanuj — potrzebna lista DAT-ów "
    "z formatem CHD.":
        "Load DATs / Scan first — a list of DATs with CHD format is needed.",
    "Brak biblioteki cue:": "No cue library:",
    "Wrzuć tam paczki Redump Cuesheets (mogą zostać w zipach).":
        "Put Redump Cuesheets packs there (may stay zipped).",
    "Odbudowa CHD wg cue": "CHD rebuild from cue",
    "Dla każdej gry z formatem CHD: jeśli plik <gra>.chd ma INNĄ "
    "liczbę ścieżek niż DAT (sklejony — zrobiony ze złym/bez cue), "
    "zostanie PRZEBUDOWANY: ścieżki zweryfikowane SHA-1 z DAT-em, "
    "cue z biblioteki, createcd + round-trip. Stary plik podmieniany "
    "dopiero po pełnej weryfikacji. Można przerwać w każdej chwili.":
        "For each CHD-format game: if the <game>.chd file has a DIFFERENT "
        "track count than the DAT (glued — made with a bad/no cue), it will "
        "be REBUILT: tracks verified by SHA-1 against the DAT, cue from the "
        "library, createcd + round-trip. The old file is replaced only after "
        "full verification. Can be cancelled anytime.",
    "Katalog do pełnego przeliczenia": "Folder for full recompute",
    "Pełny skan": "Full scan",
    "Katalog nie istnieje:": "Folder does not exist:",
    "Przeliczyć sumy WSZYSTKICH plików w:":
        "Recompute checksums of ALL files in:",
    "Może to potrwać długo (czyta każdy plik). Pliki nie są "
    "zmieniane — aktualizowany jest tylko indeks. Można przerwać "
    "w każdej chwili (postęp zapisany).":
        "This may take a while (reads every file). Files are not changed — "
        "only the index is updated. Can be cancelled anytime (progress saved).",
    "Dodatkowy katalog ToSort": "Additional ToSort folder",
    "ToSort": "ToSort",
    "Ten katalog już jest na liście.": "This folder is already on the list.",
    "Główny katalog ToSort (tam trafiają nieznane)":
        "Main ToSort folder (where unknown files go)",
    "Administrator": "Administrator",
    "Program zostanie uruchomiony ponownie z uprawnieniami "
    "administratora (pojawi się monit UAC), aby móc tworzyć symlinki.\n"
    "Bieżące okno zostanie zamknięte. Kontynuować?":
        "The program will restart with administrator privileges (a UAC prompt "
        "will appear) to be able to create symlinks.\nThe current window will "
        "close. Continue?",
    "Nie udało się uruchomić z podniesionymi uprawnieniami "
    "(odmowa UAC?).\n\nAlternatywa bez admina: włącz TRYB "
    "DEWELOPERA w Windows — Ustawienia → System → Dla deweloperów "
    "→ Tryb dewelopera. Wtedy symlinki działają na zwykłym koncie.":
        "Could not start with elevated privileges (UAC declined?).\n\n"
        "Alternative without admin: enable DEVELOPER MODE in Windows — "
        "Settings → System → For developers → Developer Mode. Then symlinks "
        "work on a regular account.",
    "Ikony": "Icons",
    "Katalog": "Folder",
    "jeszcze nie istnieje — "
    "najpierw Napraw kolekcję.":
        "does not exist yet — fix the collection first.",
    "Sprzątanie nieznanych wymaga katalogu ToSort.":
        "Cleaning up unknowns requires a ToSort folder.",
    "Brak przepisu": "No recipe",
    "Najpierw uruchom: Skanuj i raportuj. Naprawa korzysta z "
    "wyników tego skanu (co gdzie leży, jakie ma sumy) i sama "
    "niczego nie skanuje.":
        "Run Scan and report first. Fixing uses that scan's results (what is "
        "where, what checksums) and doesn't scan on its own.",
    "Katalogi bazowe nie istnieją": "Base folders don't exist",
    "Te katalogi bazowe (rom_root) nie istnieją:":
        "These base folders (rom_root) don't exist:",
    "Jeśli to LITERÓWKA w regule — wybierz Nie i popraw "
    "ustawienia.\nJeśli celowo je wyczyściłeś (pliki są w ToSort) "
    "— wybierz Tak: katalogi zostaną UTWORZONE, a naprawa "
    "odtworzy strukturę i przeniesie pliki z ToSort.":
        "If it's a TYPO in a rule — choose No and fix the settings.\nIf you "
        "cleared them on purpose (files are in ToSort) — choose Yes: the "
        "folders will be CREATED, and fixing will rebuild the structure and "
        "move files from ToSort.",
    "Nie utworzono": "Not created",
    "wspólne pliki dostaną symlinki": "shared files will get symlinks",
    "symlinki WYŁĄCZONE — miejsca DAT-ów dzieci zostaną "
    "PUSTE (nic nie jest kopiowane)":
        "symlinks DISABLED — child DAT spots will stay EMPTY (nothing is "
        "copied)",
    "Napraw kolekcję": "Fix collection",
    "Pliki zostaną przeniesione/przemianowane wg DAT-ów "
    "(z walidacją sum kontrolnych), ":
        "Files will be moved/renamed per the DATs (with checksum "
        "validation), ",
    ", nieznane trafią do ToSort": ", unknown files go to ToSort",
    ".\nOperację można PRZERWAĆ w każdej chwili — zrobione "
    "zostaje zrobione.\n\nWykonać?":
        ".\nThe operation can be CANCELLED anytime — what's done stays done."
        "\n\nProceed?",
    "Naprawa przerwana": "Fix cancelled",
    "Naprawa NIE została wykonana: reguła rom_root wskazuje "
    "nieistniejący katalog (szczegóły w logu).\nNic nie "
    "zostało zmienione.":
        "The fix was NOT performed: a rom_root rule points to a non-existent "
        "folder (details in the log).\nNothing was changed.",
    "Znajdź naprawy": "Find fixes",
    "Plan naprawy (nic nie zmieniono):": "Fix plan (nothing changed):",
    "Szczegóły w logu i w oknie postępu. Jeśli plan wygląda "
    "dobrze — kliknij: Napraw (wykonaj).":
        "Details in the log and the progress window. If the plan looks good — "
        "click: Fix (apply).",
    "Dodaj przynajmniej jeden katalog.": "Add at least one folder.",
    "Duplikaty zostaną zastąpione symlinkami do jednej kopii "
    "fizycznej (odwracalna podmiana, nic nie jest kasowane "
    "bezpowrotnie).":
        "Duplicates will be replaced with symlinks to one physical copy "
        "(reversible swap, nothing is deleted permanently).",
    "Wskaż istniejący katalog gier.": "Point to an existing games folder.",
    "Wskaż katalog gier i katalog emulatorów.":
        "Point to the games folder and the emulators folder.",
    "Wskaż katalog emulatorów.": "Point to the emulators folder.",
    "Wskaż istniejący katalog BIOS-ów.": "Point to an existing BIOS folder.",
    "Wskaż katalog emulatorów "
    "(zakładka Ikony i skróty).":
        "Point to the emulators folder (Icons & shortcuts tab).",
    "Zaznacz przynajmniej jeden emulator.": "Check at least one emulator.",
    "Wybierz libretro System.dat": "Choose libretro System.dat",
    "DAT (*.dat);;Wszystkie pliki (*.*)": "DAT (*.dat);;All files (*.*)",
    "Aktualizacje": "Updates",
    "Wskaż katalog emulatorów.": "Point to the emulators folder.",
    "Zaznacz (checkbox) emulatory do aktualizacji. "
    "Najpierw Sprawdź wersje pokaże, które mają nowsze.":
        "Check (tick) the emulators to update. Run Check versions first to "
        "see which have newer ones.",
    "Zaktualizować:": "Update:",
    "Configi i save'y są chronione (preserve), ale warto "
    "mieć kopię.":
        "Configs and saves are protected (preserve), but a backup is "
        "recommended.",

    # --- dialog kluczy API grafik ---
    "Klucze API — źródła grafik": "API keys — artwork sources",
    "SteamGridDB — klucz:": "SteamGridDB — key:",
    "IGDB — Client ID:": "IGDB — Client ID:",
    "IGDB — Client Secret:": "IGDB — Client Secret:",
    "TheGamesDB — klucz:": "TheGamesDB — key:",
    "IGDB: dev.twitch.tv → Application (Client ID + Secret). "
    "TheGamesDB: forums.thegamesdb.net. Puste = źródło "
    "wyłączone. Libretro działa bez klucza.":
        "IGDB: dev.twitch.tv → Application (Client ID + Secret). "
        "TheGamesDB: forums.thegamesdb.net. Empty = source disabled. "
        "Libretro works without a key.",
    "(domyślny:": "(default:",

    # wspólne
    "Anuluj": "Cancel",
    "Kombajn": "Combine",
    "Zapisz": "Save",
    "OK": "OK",
}

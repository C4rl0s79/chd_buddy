# ROM Helper

> Dawniej **CHD Buddy**. Pakiet/CLI pozostają `chd_buddy` / `chd-buddy`.

Desktopowe narzędzie (Python) do **audytu, konwersji i bezpiecznej naprawy**
plików CHD z naciskiem na poprawność typu nośnika, sekwencyjną rekompresję
z atomową podmianą i pracę przy **bardzo małej ilości wolnego miejsca**.

## Co już działa (v0.2)

**Backend (przetestowany):**
- Wrapper `chdman` z **poprawnym** parsowaniem postępu (obsługa `\r`, format
  `Compressing, XX.X% complete...`) i anulowaniem procesu.
- Parser `chdman info -v` → `CHDInfo` (wersja, unit size, kodeki, tagi meta).
- **Detekcja błędnego typu**: DVD spakowane jako CD wykrywane po kombinacji
  „profil CD (unit 2448 / kodeki `cd*` / tagi `CHT2`) + rozmiar logiczny > pojemność CD".
- Detekcja typu źródła `.iso` (rozmiar + PVD ISO9660 + wykrywanie UDF).
- Presety kompresji **zależne od typu** (CD wymaga `cdlz,cdzl,cdfl`, DVD/HD `lzma,zlib,huff,flac`).
- **Sekwencyjny fixer** z atomową podmianą i rollbackiem:
  - `recompress` — `chdman copy` w miejscu (peak ≈ stary+nowy, bez pełnego extractu);
  - `retype` — extract → `bin`→`iso` → `createdvd` → verify → swap;
  - `create` — konwersja źródeł właściwym poleceniem wg typu.
- **Budżet dyskowy** (preflight) — tryb bezpieczny i agresywny (low-disk).
  Dla DVD 4.7 GB retype agresywny szczytuje ~4.9 GB → batch 3 TB działa przy 10 GB wolnego.
- **CLI** (headless): `info`, `audit --csv`, `fix --recompress/--retype`, `convert`.
- **GUI** (PySide6): drag&drop, tabela zadań, log, kolejka **ściśle sekwencyjna**.

## Weryfikacja poprawności `chdman`

`chdman` ma osobne polecenia: `createcd/createdvd/createhd/createraw/createld`,
`extractcd/extractdvd/extracthd`, `verify`, `info`, `copy`. `verify` sprawdza
integralność kontenera; poprawność *typu* i round-trip DAT to osobne warstwy.

## Instalacja

```bash
pip install -e .[gui,dev]      # GUI + testy
chd-buddy --help               # CLI
chd-buddy-gui                  # GUI  (lub: python -m chd_buddy.main)
```

`chdman` musi być w PATH albo wskazany w ustawieniach (przenośnych, obok exe).

## Wymagane narzędzia zewnętrzne

CHD Buddy jest **wrapperem** wokół zewnętrznych narzędzi — sam ich nie zawiera.
Zainstaluj/wskaż te, których używasz (ścieżki konfigurowalne w Ustawieniach,
przenośnie obok `.exe`):

| Narzędzie | Do czego | Wymagane? |
|---|---|---|
| **chdman** (MAME) | wszystkie operacje na CHD: `info/verify/create*/extract*/copy`, identyfikacja zawartości | **Tak** (rdzeń programu) — w PATH lub w ustawieniach |
| **7-Zip** (`7z.exe`) | odczyt/rozpakowanie archiwów `.7z` (skan kolekcji, aktualizacje emulatorów) | Zalecane, gdy używasz `.7z` |
| **DolphinTool** (`dolphin-tool.exe`) | konwersja i weryfikacja **RVZ** (GameCube/Wii) | Tylko dla RVZ |

`chdman` i `dolphin-tool` pochodzą odpowiednio z pakietu **MAME** oraz z
**Dolphina**. Na Windows symlinki (dedup/mirror) wymagają trybu dewelopera lub
uruchomienia jako administrator.

**Zależności Pythona** (opcjonalne ekstra przy `pip install`):
`PySide6` (GUI, `.[gui]`), `requests` (aktualizator emulatorów), `Pillow`
(ikony, `.[icons]`), `py7zr` (archiwa 7z bez `7z.exe`, `.[archives]`).

## Przykłady CLI

```bash
chd-buddy audit /roms/ps2 --csv audit.csv     # znajdź podejrzane CHD
chd-buddy audit game.chd --verify             # + integralność
chd-buddy fix /roms/ps2 --retype --yes        # napraw DVD-jako-CD
chd-buddy fix /roms --recompress --preset max # rekompresja w miejscu (low-disk)
chd-buddy convert /new --output /chd          # źródła → CHD
```

## Indeks plików, duplikaty i symlinki (nowe — fundament „kombajnu")

Trwały indeks (SQLite) pamięta sumy kontrolne raz zeskanowanych plików —
dopóki rozmiar i mtime się nie zmienią, nic nie jest przeliczane. Skanuj
katalogi źródłowe **i docelowe** do jednej bazy; plik raz rozpoznany
„istnieje" dla wszystkich DAT-ów aż do fizycznego przeniesienia.

```bash
chd-buddy index D:\ROMS Z:\ROMS            # skan przyrostowy do bazy
chd-buddy index Z:\ROMS --full             # wymuś pełne przeliczenie sum
chd-buddy index Z:\ROMS --chd-content      # dla .chd też SHA-1 zawartości (chdman)
chd-buddy dupes                            # pokaż identyczne pliki (SHA-1+rozmiar)
chd-buddy dedup --prefer Z:\ROMS\redump    # duplikaty → symlinki (podgląd)
chd-buddy dedup --prefer Z:\ROMS\redump --yes   # wykonaj
chd-buddy mirror Z:\ROMS C:\RetroBat\roms  # mirror symlinkami (jak retrobat_safe_linker.ps1)
```

Zasady bezpieczeństwa: dedup podmienia plik odwracalnie (rename → link →
dopiero potem kasuje tymczasowy; rollback przy błędzie) i pomija pliki
zmienione po skanie; mirror usuwa WYŁĄCZNIE symlinki, zwykłych plików nie
dotyka (wykluczenia: `images,manuals,videos`). Symlinki na Windows wymagają
trybu dewelopera albo uruchomienia jako administrator (czytelny komunikat
zamiast WinError 1314).

## Drzewo DAT-ów + raport + rebuild (model RomVaulta)

Katalog DAT-ów (DatRoot) odwzorowuje się na drzewo ROM-ów (RomRoot):
DAT w ``dats/PS2/foo.dat`` dostaje katalog ``roms/PS2/<Name z nagłówka>``.

Hierarchia parent→children: pierwszy DAT z danym plikiem trzyma go
fizycznie, kolejne dostają symlinki. Domyślnie rodzicem jest DAT
z WIĘKSZĄ liczbą ROM-ów (główna biblioteka, np. pełny Redump), a mniejsze
zestawy (1G1R/Retool) linkują z niego. Ręczne sterowanie: plik
``_priorytet.txt`` w katalogu DAT-ów — jedna nazwa DAT-a na linię,
kolejność = hierarchia.

Zakres skanowania: systemy płytowe (CD/DVD) — dane luzem w podkatalogach,
archiwa ZIP/7z i pliki CHD w katalogu głównym; systemy kartridżowe — dane
luzem oraz ZIP/7z. Wnętrza ZIP czyta centralny katalog (stdlib), 7z —
nagłówek (opcjonalne py7zr, ``pip install .[archives]``).

GUI (zakładka Kolekcja): hierarchię DAT-ów układa się strzałkami
▲/▼ (zapis do ``_priorytet.txt``); prawy klik na grze = „Stwórz ikonę"
z oknem wyboru grafik (Libretro + SteamGridDB, miniatury), prawy klik na
DAT = wsadowe generowanie ikon; pasek postępu pokazuje etap pracy.

CHD: identyfikacja dwustopniowa (``--chd-deep`` / checkbox w GUI):
1. tanio — SHA-1 zawartości z nagłówka (DVD: createdvd ⇒ data_sha1 ==
   SHA-1 obrazu .iso);
2. drogo — ekstrakcja metodami chd_buddy: extractdvd, extractcd+deframe
   (DVD spakowane jako CD!), surowe ścieżki CD, hd/raw/ld — aż wynik
   trafi w DAT. Wynik zapisuje się w indeksie, więc liczy się RAZ.
Trafiony CHD zaspokaja CAŁĄ grę (gry CD bin/cue mają wiele ROM-ów, gra
DVD jeden .iso) i ląduje jako ``<nazwa gry>.chd``. Brakujący ``.cue``
nie blokuje kompletności gry (odtwarzalny z ``.bin``).

```bash
chd-buddy report  --dats D:\dats --roms Z:\ROMS [--missing]
chd-buddy rebuild --dats D:\dats --roms Z:\ROMS --tosort Z:\ToSort --clean       # podgląd
chd-buddy rebuild --dats D:\dats --roms Z:\ROMS --tosort Z:\ToSort --clean --yes # wykonaj
```

Rebuild: przenosi/przemianowuje rozpoznane pliki pod kanoniczne nazwy
z DAT-a, linkuje wspólne pliki między DAT-ami, nieznane odsyła do ToSort
(z odwzorowaniem podkatalogów). Niczego nie nadpisuje; usuwa wyłącznie
symlinki; pliki zmienione po skanie pomija. Indeks jest aktualizowany po
każdym ruchu, więc kolejny skan nie liczy sum od nowa.

Reguły kaskadowe per katalog: plik ``_reguly.json`` w katalogu DAT-ów —
klucze "*" / ścieżka katalogu / nazwa DAT-a (szczegółowszy wygrywa, jak
w RomVaulcie); reguły: ``only_complete``, ``skip``, ``dedup_copies``,
``target`` (buduj w istniejącym katalogu, np. "ps2"), ``subdir_per_game``
(gry wieloplikowe luzem w podkatalogu per gra), ``prefer_translations``
(wersje ``(Japan)`` zastępowane linkiem do dostępnego fanowskiego
tłumaczenia ``[T-En]`` z innego DAT-a — sensowne dla zestawów 1G1R).

Kopie potwierdzonych plików → symlinki (domyślnie przy naprawie;
``--keep-copies`` wyłącza, per katalog reguła ``dedup_copies: false``):
po naprawie fizyczne KOPIE plików potwierdzonych pod ścieżką kanoniczną
(w drzewie ROM-ów i ToSort) są zamieniane na symlinki — kopia fizyczna
zostaje wyłącznie w katalogu DAT-a rodzica. Bez uprawnień do symlinków
pass jest pomijany w całości.

Playlisty .m3u dla gier multi-disc: ``chd-buddy m3u <katalog>`` albo
przycisk w GUI — układ płaski (dyski w jednym katalogu) i podkatalogowy
(bin/cue), zapis LF/UTF-8 bez BOM (Batocera/RetroArch).

Archiwa ZIP (zrzuty Redump) są skanowane OD ŚRODKA: CRC32+rozmiar każdego
pliku pochodzi z centralnego katalogu ZIP-a (bez dekompresji), a rebuild
wypakowuje trafienia z weryfikacją SHA-1 w locie — zła zawartość nigdy nie
ląduje pod docelową nazwą; archiwum źródłowe zostaje nietknięte. `report`
i `rebuild` same odświeżają indeks (drzewo ROM-ów + `--tosort`). Domyślnie
budowane są tylko KOMPLETNE gry (`--incomplete` wyłącza) — wspólne ścieżki
(cisza na płycie) nie zaśmiecają setek cudzych zestawów. Bez uprawnień do
symlinków duplikaty są kopiowane (ponowny przebieg jako administrator
zamieni je na linki przy pełnym rebuildzie).

## Ikony gier (Libretro Thumbnails + SteamGridDB)

Kolekcje po `rebuild` mają kanoniczne nazwy Redump/No-Intro — te same, których
używa repozytorium libretro-thumbnails. Program pobiera listę miniatur systemu
(1 zapytanie GitHub API, cache w ``libretro_cache/``), dopasowuje tytuł
rozmyto (regiony ``(USA)`` ↔ ``(USA, Canada)``, tagi Beta/v2.00 karane,
aliasy repo rozwiązywane) i zapisuje wielorozmiarowe ``.ico`` (256…16 px,
prostokątne boxarty dopełniane do kwadratu). Fallback: SteamGridDB
(klucz w ustawieniach: ``sgdb_api_key``). Wymaga Pillow (``pip install .[icons]``).

```bash
chd-buddy icons "Z:\ROMS\PS2\Sony - PlayStation 2"     # system z nazwy katalogu
chd-buddy icons Z:\ROMS --tree                          # każdy podkatalog = system
chd-buddy icons D:\roms\ps2 --system PS2 --out D:\icons --overwrite
```

Gry multi-disc dostają jedną ikonę (``Gra (USA).ico`` przy ``.m3u`` /
zgrupowanych dyskach). Istniejące ``.ico`` nie są nadpisywane bez
``--overwrite`` — drugi przebieg nie dotyka sieci.

## Skróty .lnk (składnia per emulator)

Rejestr emulatorów zna wzorce exe i składnię uruchamiania (PCSX2/DuckStation:
``-batch -fullscreen -- <plik>``; PPSSPP/Flycast/ares/Xenia: ścieżka
pozycyjna; Dolphin ``-e``; Cemu/Citron/Eden/shadPS4 ``-g``; xemu
``-dvd_path``; MAME ``<set> -rompath``; RetroArch ``-L cores\<core>.dll``). System wykrywany z nazwy katalogu (DAT-y Redump +
skróty EmulationStation). Multi-disc: emulatory z obsługą .m3u dostają
playlistę, pozostałe Disc 1. Ikony z ``<rom_dir>\icons`` podpinane
automatycznie. Tworzenie .lnk wsadowo przez WScript.Shell z tymczasową nazwą
ASCII + atomową podmianą (COM przekręca nietypowe znaki — lekcja z PyLinks).

```bash
chd-buddy shortcuts D:\emu\roms --tree            # RomRoot, podkatalog = system
chd-buddy shortcuts D:\emu\roms\ps2 --overwrite   # jeden system
```

Katalog emulatorów: ``--emus D:\emu\Emulatory`` albo ``emulators_dir``
w ustawieniach. Wynik: ``<rom_dir>\shortcuts\<Gra>.lnk``.

## GUI kombajnu (ROM Kombajn)

```bash
py -m chd_buddy.suite        # albo: chd-buddy-suite (po instalacji)
```

Okno z zakładkami: **Kolekcja (DAT)** — drzewo DAT-ów á la RomVault
(zielony = komplet, żółty = do naprawy, czerwony = braki; dzieci = konkretne
gry) + Napraw (podgląd/wykonaj, nieznane → ToSort); **Indeks** — skan
przyrostowy katalogów, duplikaty, dedup symlinkami (kolejność katalogów =
priorytet kopii fizycznej); **Ikony i skróty** — boxarty .ico i skróty .lnk
dla całego drzewa. Operacje chodzą w tle (GUI nie zamiera), ścieżki są
zapamiętywane w ustawieniach. Klasyczne narzędzie CHD: menu *Narzędzia*.

## Warsztat, BIOS-y i aktualizacje emulatorów

Katalog-warsztat: jeden korzeń z podkatalogami ``Emulatory``, ``roms``,
``bios``, ``dat``, ``to sort`` — wpisanie go w GUI (pole na górze okna)
wypełnia wszystkie ścieżki. 

BIOS-y (wchłonięty BiosManager): identyfikacja po MD5 (nazwa bez
znaczenia; skan też wnętrz zip/7z), manifest ``bios_manifest.json``
(definicje plików + reguły per emulator + ścieżki instalacji portable),
instalacja prosto do katalogów emulatorów albo eksport, import hashy
z libretro System.dat.

```bash
chd-buddy bios --input D:\emu\bios --install          # do katalogów emulatorów
chd-buddy bios --input D:\emu\bios --export D:\wyj    # do D:\wyj\bios\<emu>\
chd-buddy bios --import-dat System.dat                # rozszerz manifest
```

Aktualizator (wchłonięty emu_updater v2.2): GitHub Releases / Gitea /
dolphin-emu.org / buildbot.libretro.com (RetroArch + wszystkie rdzenie)
/ strona Edenu; wersje w ``emu_versions.json``; configi i save'y
chronione listami ``preserve``. Wymaga ``requests`` (i 7-Zipa dla .7z).

```bash
chd-buddy update --check                 # tylko sprawdź
chd-buddy update --only pcsx2 retroarch  # aktualizuj wybrane
```

W GUI: zakładki **BIOS** (lista emulatorów + instalacja) i
**Aktualizacje** (tabela wersji, sprawdź/aktualizuj/wymuś).

## Poziomy skanowania i naprawy (model RomVaulta, pod nasze formaty)

Adaptacja RomVault-owych poziomów do naszego przypadku (pliki luzem,
ZIP/7z, **CHD**, **RVZ**) — patrz `core/levels.py`.

Skanowanie:
- **Szybki** — nowe/zmienione pliki; CHD po całym pliku (nie dopasuje płyt
  do DAT). Inwentaryzacja.
- **Normalny** (domyślny) — pełne CRC+MD5+SHA-1; archiwa z nagłówka; CHD —
  SHA-1 zawartości z nagłówka (DVD trafia w DAT). Sumy cache'owane.
- **Pełny/głęboki** — przelicza wszystko od nowa (wykrywa uszkodzenia) +
  identyfikuje CHD ekstrakcją (CD bin/cue, DVD-jako-CD).

Naprawa:
- **Szybki** — układa rozpoznane; wypakowanie po CRC+rozmiarze.
- **Normalny** (domyślny) — wypakowanie z pełną walidacją SHA-1; CHD retype
  z round-trip; pliki na miejscu zaufane.
- **Pełny/weryfikuj** — dodatkowo re-weryfikuje pliki już na miejscu
  (wymaga skanu Pełnego — łapie ciche uszkodzenia).

W GUI: dwa listy rozwijane „skan" i „naprawa" na zakładce Kolekcja.
CLI: ``report/rebuild --scan-level quick|normal|full``.

## Konwersja formatów przechowywania

Reguła ``format`` (per DAT/katalog/globalnie, wartość ``auto`` decyduje wg
systemu) + checkbox „konwertuj do formatu docelowego" przy naprawie:
- kartridż → **ZIP** (stdlib), płyta → **CHD** (chdman), GameCube/Wii →
  **RVZ** (DolphinTool);
- każda konwersja WERYFIKOWANA (ZIP: SHA-1 członków po odczycie; CHD:
  round-trip; RVZ: DolphinTool verify) — źródło kasowane dopiero po sukcesie;
- konwertowane są tylko fizyczne, luźne pliki gry (nie symlinki, nie już
  w formacie docelowym); dzieci platform pozostają symlinkami.

## Walidacja DAT (Redump) + kwarantanna

Najbezpieczniejszy tryb naprawy typu: obraz musi odpowiadać znanemu zrzutowi
z DAT, zanim oryginał zostanie podmieniony.

```bash
chd-buddy fix /roms/ps2 --retype --dat D:\dats\ps2 --quarantine-dir /roms/ps2/nieznane --yes
```

Przepływ dla każdego pliku:
1. `extractcd` błędnego CD-CHD → `bin`;
2. deframe `2352→2048` → `iso`;
3. **SHA-1 obrazu sprawdzany w DAT**;
   - **trafienie** → `createdvd` → verify kontenera → atomowa podmiana in-place;
   - **brak trafienia** → oryginał przenoszony do `nieznane/` (rename w obrębie
     dysku, bez kopii), nic nie jest pakowane, **zero utraty danych**.

DAT sam wyznacza typ nośnika: gra z `.iso` = DVD, z `.bin/.cue` = CD. Dzięki
temu ewentualne błędy ekstrakcji (np. obrazy robione `createraw`) nie mogą po
cichu uszkodzić kolekcji — po prostu nie trafią w DAT i wylądują w kwarantannie.

Bez `--dat` naprawa nadal działa, opierając się na walidacji **round-trip**
(SHA-1 obrazu przed pakowaniem == SHA-1 po wypakowaniu z nowego CHD). Round-trip
dowodzi, że pakowanie było przezroczyste; DAT dodatkowo dowodzi, że sam zrzut
jest kanonicznie poprawny.

## Docelowe platformy

- **PS2** — mieszanka CD (bin/cue) i DVD (iso); `--retype` naprawia „DVD-jako-CD".
- **PS1** — obrazy robione `createcd`, `extractcd` działa poprawnie.
- **Dreamcast / Saturn** — źródła w bin/cue → tylko poprawne spakowanie do CHD
  przez `convert` (`createcd`), bez retype.

## Ograniczenia i następne kroki

- **Retype (deframing MODE1/2352)** — zwalidowany na realnym pliku PS2:
  wypakowane ISO zgadza się z Redump w RomVault (bajt-w-bajt). Ścieżka
  MODE1/2048 to kopia 1:1; obrazy wielościeżkowe są wstrzymywane bez trybu
  agresywnego. Batch chroniony bramką DAT + kwarantanną.
- **Walidacja DAT** — zaimplementowana (`datfile.py` + `roundtrip.py`), wpięta
  jako twarda bramka w `retype`. Kolejny krok: walidacja wielościeżkowa dla
  konwersji CD (Dreamcast/Saturn/PS1 bin/cue → CHD) w `convert`.
- Archiwa (`.7z/.zip/.rar`) — rozpakowywanie w workerze (opcjonalne zależności).
- Pakowanie: `pyinstaller --noconfirm chd_buddy.spec` (portable, ustawienia obok exe).

## Struktura

```
chd_buddy/
├── main.py                 # entry GUI
├── cli.py                  # entry CLI
├── core/
│   ├── models.py           # enumy + dataclassy
│   ├── settings.py         # ustawienia przenośne (obok exe)
│   ├── presets.py          # kompresja per typ nośnika
│   ├── chdman.py           # wrapper + parser info + parsowanie postępu
│   ├── detector.py         # typ nośnika (iso/chd)
│   ├── scanner.py          # skan wejścia + companiony cue/gdi
│   ├── diskbudget.py       # preflight miejsca
│   ├── imageops.py         # cue parsing + bin→iso deframing
│   ├── audit.py            # klasyfikacja CHD
│   └── fixer.py            # sekwencyjna naprawa + atomowa podmiana
├── ui/
│   ├── worker.py           # QRunnable ↔ sygnały
│   ├── main_window.py      # okno główne + kolejka
│   └── settings_dialog.py
└── tests/test_core.py
```

## Testy

```bash
pytest -q
```

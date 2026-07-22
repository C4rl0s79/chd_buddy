"""Aktualizator emulatorów — wchłonięty emu_updater v2.2.

Źródła: GitHub Releases, Gitea/Forgejo, dolphin-emu.org (przez tagi GitHuba
+ CDN), buildbot.libretro.com (RetroArch + WSZYSTKIE zainstalowane rdzenie),
strona wydań Edenu. Wersje zapamiętywane w emu_versions.json obok ustawień.
Archiwa .7z/SFX wymagają 7-Zipa w PATH; `preserve` chroni configi/save'y.

Wymaga: pip install requests.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Optional

from .settings import app_base_dir

LogCB = Callable[[str], None]

VERSIONS_FILENAME = "emu_versions.json"
BUILDBOT = "https://buildbot.libretro.com"

EMULATORS_UPDATE: dict[str, dict] = {
    "eden": {"type": "eden",
             "asset": r"Eden-Windows-.*amd64-clang-pgo\.zip$", "dir": "Eden"},
    "citron": {"type": "github", "repo": "citron-neo/emulator",
               "asset": r"citron.*(msvc|windows.*x64)\.zip$",
               "dir": "Citron", "strip_root": True},
    "dolphin": {"type": "dolphin", "dir": "Dolphin"},
    # Rdzenie aktualizuje sam RetroArch (Online Updater) — my tylko aplikację.
    "retroarch": {"type": "retroarch", "dir": "RetroArch",
                  "update_cores": False, "update_app": True},
    "pcsx2": {"type": "github", "repo": "PCSX2/pcsx2",
              "asset": r"pcsx2-.*-windows-x64-Qt\.7z$",
              "dir": "PCSX2", "prerelease": True},
    "duckstation": {"type": "github", "repo": "stenzek/duckstation",
                    "asset": r"duckstation-windows-x64-release\.zip$",
                    "dir": "DuckStation"},
    "rpcs3": {"type": "github", "repo": "RPCS3/rpcs3-binaries-win",
              "asset": r"rpcs3-.*_win64.*\.7z$", "dir": "RPCS3"},
    "ppsspp": {"type": "github", "repo": "hrydgard/ppsspp",
               "asset": r"PPSSPP-v.*-Windows-x64\.zip$", "dir": "PPSSPP"},
    "cemu": {"type": "github", "repo": "cemu-project/Cemu",
             "asset": r"cemu-.*-windows-x64\.zip$",
             "dir": "Cemu", "strip_root": True},
    "xemu": {"type": "github", "repo": "xemu-project/xemu",
             "asset": r"xemu-win-x86_64-release\.zip$", "dir": "xemu"},
    "xenia-canary": {"type": "github",
                     "repo": "xenia-canary/xenia-canary-releases",
                     "asset": r"xenia_canary_windows_?\.zip$", "dir": "Xenia"},
    "flycast": {"type": "github", "repo": "flyinghead/flycast",
                "asset": r"flycast-win64-.*\.zip$", "dir": "Flycast"},
    "melonds": {"type": "github", "repo": "melonDS-emu/melonDS",
                "asset": r"melonDS-.*-windows-x86_64\.zip$", "dir": "melonDS"},
    "mgba": {"type": "github", "repo": "mgba-emu/mgba",
             "asset": r"mGBA-.*-win64\.7z$", "dir": "mGBA", "strip_root": True},
    "azahar": {"type": "github", "repo": "azahar-emu/azahar",
               "asset": r"azahar-windows-msvc-[\d.]+\.zip$",
               "dir": "Azahar", "strip_root": True},
    "vita3k": {"type": "github", "repo": "Vita3K/Vita3K",
               "asset": r"windows-latest\.zip$", "dir": "Vita3K"},
    "shadps4": {"type": "github", "repo": "shadps4-emu/shadPS4",
                "asset": r"shadps4-win64-(qt|sdl)-?.*\.zip$", "dir": "shadPS4"},
    "ares": {"type": "github", "repo": "ares-emulator/ares",
             "asset": r"ares-windows-x64\.zip$", "dir": "ares",
             "strip_root": True},
    "snes9x": {"type": "github", "repo": "snes9xgit/snes9x",
               "asset": r"snes9x-.*-win32-x64\.zip$", "dir": "Snes9x"},
    "mame": {"type": "github", "repo": "mamedev/mame",
             "asset": r"mame\d+b_x64\.exe$", "dir": "MAME", "sfx": True},
}


def _requests():
    try:
        import requests
    except ImportError as e:
        raise RuntimeError("Aktualizator wymaga pakietu requests "
                           "(pip install requests)") from e
    return requests


def _sessions():
    requests = _requests()
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0 Safari/537.36")
    gh = requests.Session()
    gh.headers["Accept"] = "application/vnd.github+json"
    gh.headers["User-Agent"] = "chd-buddy-updater/1.0"
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        gh.headers["Authorization"] = f"Bearer {token}"
    return s, gh


def versions_path() -> Path:
    return app_base_dir() / VERSIONS_FILENAME


def load_versions() -> dict:
    p = versions_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_versions(v: dict) -> None:
    versions_path().write_text(json.dumps(v, indent=2, ensure_ascii=False),
                               encoding="utf-8")


def _download(session, url: str, dest: Path, log: LogCB) -> None:
    with session.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = last = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and done - last > total // 10:
                    last = done
                    log(f"    pobieranie: {done * 100 // total}% "
                        f"({done >> 20} MB)")


def _find_7z() -> Optional[str]:
    for cand in ("7z", r"C:\Program Files\7-Zip\7z.exe",
                 r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if shutil.which(cand) or Path(cand).exists():
            return cand
    return None


def _extract(archive: Path, target: Path, strip_root: bool = False,
             preserve: Optional[list[str]] = None) -> None:
    preserve = preserve or []
    target.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="chdbuddy_upd_"))
    try:
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        else:  # .7z / SFX .exe
            sevenzip = _find_7z()
            if not sevenzip:
                raise RuntimeError("Brak 7-Zipa (potrzebny do .7z) — "
                                   "zainstaluj i dodaj do PATH")
            subprocess.run([sevenzip, "x", str(archive), f"-o{tmp}", "-y"],
                           check=True, capture_output=True)
        src = tmp
        if strip_root:
            entries = list(tmp.iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                src = entries[0]
        for root, _dirs, files in os.walk(src):
            rel_root = Path(root).relative_to(src)
            for fname in files:
                rel = rel_root / fname
                if any(fnmatch.fnmatch(str(rel).replace("\\", "/"), pat)
                       for pat in preserve):
                    continue
                dst = target / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(root) / fname, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- handlery per źródło -----------------------------------------------------------

def _gh_latest(gh, repo: str, prerelease: bool) -> Optional[dict]:
    base = f"https://api.github.com/repos/{repo}/releases"
    if prerelease:
        r = gh.get(base, params={"per_page": 10}, timeout=30)
        r.raise_for_status()
        return next((rel for rel in r.json() if not rel.get("draft")), None)
    r = gh.get(base + "/latest", timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _handle_github(key, cfg, emu_root, versions, check_only, force, log,
                   report=None) -> bool:
    _s, gh = _sessions()
    rel = _gh_latest(gh, cfg["repo"], cfg.get("prerelease", False))
    if not rel:
        log("    brak wydań")
        return False
    tag = rel.get("tag_name") or rel.get("name")
    if tag in ("latest", "continuous", "preview", "nightly"):
        tag = f"{tag}@{(rel.get('published_at') or '')[:10]}"
    if report:
        report(key, tag)
    if tag == versions.get(key) and not force:
        log(f"    aktualna wersja: {tag}")
        return False
    rx = re.compile(cfg["asset"], re.IGNORECASE)
    asset = next((a for a in rel.get("assets", []) if rx.search(a["name"])), None)
    if not asset:
        log(f"    UWAGA: brak assetu do '{cfg['asset']}' w {tag}; dostępne: "
            f"{[a['name'] for a in rel.get('assets', [])][:20]}")
        return False
    log(f"    {versions.get(key) or '(brak)'} -> {tag}  [{asset['name']}]")
    if check_only:
        return True
    target = emu_root / cfg["dir"]
    with tempfile.TemporaryDirectory(prefix="chdbuddy_dl_") as td:
        arch = Path(td) / asset["name"]
        _download(gh, asset["browser_download_url"], arch, log)
        log(f"    rozpakowuję do {target}")
        _extract(arch, target, cfg.get("strip_root", False), cfg.get("preserve"))
    versions[key] = tag
    save_versions(versions)
    log(f"    OK — {tag}")
    return True


def _handle_dolphin(key, cfg, emu_root, versions, check_only, force, log,
                    report=None) -> bool:
    _s, gh = _sessions()
    r = gh.get("https://api.github.com/repos/dolphin-emu/dolphin/tags",
               params={"per_page": 30}, timeout=30)
    r.raise_for_status()
    tags = [t["name"] for t in r.json()
            if re.fullmatch(r"\d{4}[a-z]?", t["name"])]
    if not tags:
        log("    nie znalazłem tagów wydań dolphin-emu/dolphin")
        return False
    ver = max(tags)
    if report:
        report(key, ver)
    if ver == versions.get(key) and not force:
        log(f"    aktualna wersja: {ver}")
        return False
    log(f"    {versions.get(key) or '(brak)'} -> {ver}")
    if check_only:
        return True
    s, _gh2 = _sessions()
    target = emu_root / cfg["dir"]
    url = f"https://dl.dolphin-emu.org/releases/{ver}/dolphin-{ver}-x64.7z"
    with tempfile.TemporaryDirectory(prefix="chdbuddy_dl_") as td:
        arch = Path(td) / f"dolphin-{ver}-x64.7z"
        _download(s, url, arch, log)
        log(f"    rozpakowuję do {target}")
        _extract(arch, target, strip_root=True, preserve=cfg.get("preserve"))
    versions[key] = ver
    save_versions(versions)
    log(f"    OK — {ver}")
    return True


def _handle_eden(key, cfg, emu_root, versions, check_only, force, log,
                 report=None) -> bool:
    s, _gh = _sessions()
    html = s.get("https://git.eden-emu.dev/eden-emu/eden/releases",
                 timeout=30).text
    links = re.findall(r'https://stable\.eden-emu\.dev/[^"\'<>\s)]+', html)
    m = re.search(r'https://stable\.eden-emu\.dev/(v[\d.]+(?:-rc\d+)?)/', html)
    if not links or not m:
        log("    nie umiem odczytać strony wydań Edenu (zmiana układu?)")
        return False
    tag = m.group(1)
    if report:
        report(key, tag)
    if tag == versions.get(key) and not force:
        log(f"    aktualna wersja: {tag}")
        return False
    rx = re.compile(cfg["asset"], re.IGNORECASE)
    url = next((u for u in links
                if f"/{tag}/" in u and rx.search(u.rsplit("/", 1)[-1])), None)
    if not url:
        log(f"    brak pliku do '{cfg['asset']}' w {tag}")
        return False
    log(f"    {versions.get(key) or '(brak)'} -> {tag}  "
        f"[{url.rsplit('/', 1)[-1]}]")
    if check_only:
        return True
    target = emu_root / cfg["dir"]
    with tempfile.TemporaryDirectory(prefix="chdbuddy_dl_") as td:
        arch = Path(td) / url.rsplit("/", 1)[-1]
        _download(s, url, arch, log)
        _extract(arch, target, strip_root=True, preserve=cfg.get("preserve"))
    versions[key] = tag
    save_versions(versions)
    log(f"    OK — {tag}")
    return True


def _handle_retroarch(key, cfg, emu_root, versions, check_only, force, log,
                      report=None) -> bool:
    s, _gh = _sessions()
    target = emu_root / cfg["dir"]
    changed = False
    if cfg.get("update_app", True):
        html = s.get(f"{BUILDBOT}/stable/", timeout=30).text
        vers = re.findall(r'href="[^"]*?/?(\d+\.\d+(?:\.\d+)?)/?"', html)
        ver = (max(set(vers), key=lambda v: [int(x) for x in v.split(".")])
               if vers else None)
        if report and ver:
            report(key, ver)
        if not ver:
            log("    nie mogę odczytać wersji z buildbota")
        elif ver != versions.get(key) or force:
            log(f"    aplikacja: {versions.get(key) or '(brak)'} -> {ver}")
            if not check_only:
                url = f"{BUILDBOT}/stable/{ver}/windows/x86_64/RetroArch.7z"
                with tempfile.TemporaryDirectory(prefix="chdbuddy_dl_") as td:
                    arch = Path(td) / "RetroArch.7z"
                    _download(s, url, arch, log)
                    log(f"    rozpakowuję do {target}")
                    _extract(arch, target, strip_root=True,
                             preserve=["retroarch.cfg", "config/*", "saves/*",
                                       "states/*", "playlists/*",
                                       "thumbnails/*", "system/*", "cores/*"])
                versions[key] = ver
                save_versions(versions)
            changed = True
        else:
            log(f"    aplikacja aktualna: {ver}")
    if cfg.get("update_cores", True):
        cores_dir = target / "cores"
        cores = (sorted(p.name for p in cores_dir.glob("*_libretro.dll"))
                 if cores_dir.is_dir() else [])
        if not cores:
            log("    brak zainstalowanych rdzeni — pomijam")
            return changed
        log(f"    rdzenie: {len(cores)} zainstalowanych"
            + (" (aktualizacja = ponowne pobranie)" if check_only else ""))
        if check_only:
            return changed
        ok = fail = 0
        for i, name in enumerate(cores):
            url = f"{BUILDBOT}/nightly/windows/x86_64/latest/{name}.zip"
            try:
                with tempfile.TemporaryDirectory(prefix="chdbuddy_core_") as td:
                    arch = Path(td) / f"{name}.zip"
                    with s.get(url, stream=True, timeout=120) as r:
                        r.raise_for_status()
                        with open(arch, "wb") as f:
                            for chunk in r.iter_content(chunk_size=1 << 20):
                                f.write(chunk)
                    with zipfile.ZipFile(arch) as z:
                        z.extractall(cores_dir)
                ok += 1
                log(f"      + {name} ({i + 1}/{len(cores)})")
            except Exception as e:
                fail += 1
                log(f"      ! {name}: {e}")
        log(f"    rdzenie zaktualizowane: {ok}, błędy: {fail}")
        changed = changed or ok > 0
    return changed


_HANDLERS = {
    "github": _handle_github,
    "dolphin": _handle_dolphin,
    "retroarch": _handle_retroarch,
    "eden": _handle_eden,
}


def run_updates(emu_root: Path, keys: Optional[list[str]] = None, *,
                check_only: bool = False, force: bool = False,
                log: LogCB = lambda m: None,
                on_progress: Optional[Callable[[int, int, str], None]] = None,
                ) -> tuple[int, dict]:
    """Sprawdza/aktualizuje emulatory. Zwraca (liczba_zmian, dostępne),
    gdzie dostępne = {key: (wersja_dostępna, jest_aktualizacja)}."""
    emu_root = Path(emu_root)
    keys = keys or list(EMULATORS_UPDATE)
    versions = load_versions()
    available: dict[str, tuple[str, bool]] = {}

    def report(k: str, avail: str) -> None:
        available[k] = (avail, avail != versions.get(k))

    updated = 0
    for i, key in enumerate(keys):
        cfg = EMULATORS_UPDATE.get(key)
        if not cfg:
            log(f"[{key}] nieznany emulator")
            continue
        if on_progress:
            on_progress(i, len(keys), key)
        log(f"[{key}] sprawdzam ({cfg['type']}) …")
        try:
            if _HANDLERS[cfg["type"]](key, cfg, emu_root, versions,
                                      check_only, force, log, report):
                updated += 1
        except Exception as e:
            log(f"    BŁĄD: {e}")
    n_upd = sum(1 for _v, has in available.values() if has)
    log(f"Gotowe. {'Do aktualizacji' if check_only else 'Zaktualizowano'}: "
        f"{updated if not check_only else n_upd}/{len(keys)}")
    return updated, available

"""CHD Buddy — interfejs wiersza poleceń (headless).

Przykłady:
  chd-buddy info game.chd
  chd-buddy audit /roms/ps2 --verify --csv audit.csv
  chd-buddy fix /roms/ps2 --recompress --preset max
  chd-buddy fix /roms/ps2 --retype --yes
  chd-buddy convert /roms/new --output /chd
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .core import fixer, presets
from .core.audit import audit_chd
from .core.chdman import CHDMan, CHDManNotFound
from .core.models import AuditVerdict, MediaType
from .core.scanner import scan_paths
from .core.settings import Settings


def _make_chdman(settings: Settings) -> CHDMan:
    return CHDMan(settings.chdman_path or None)


def _iter_chd_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() == ".chd" else []
    return sorted(p for p in root.rglob("*.chd") if p.is_file())


def _progress_printer(label: str):
    def cb(pct: float, msg: str) -> None:
        if pct >= 0:
            sys.stdout.write(f"\r{label}: {pct:5.1f}%   ")
            sys.stdout.flush()
    return cb


def cmd_info(args, settings: Settings) -> int:
    chd = _make_chdman(settings)
    info = chd.info(Path(args.path))
    print(f"Plik:          {info.path}")
    print(f"Wersja CHD:    {info.version}")
    print(f"Rozmiar log.:  {info.logical_bytes:,} B ({info.logical_bytes/2**20:.0f} MB)")
    print(f"Unit size:     {info.unit_bytes} B")
    print(f"Kompresja:     {', '.join(info.compression) or '-'}")
    print(f"Tagi meta:     {', '.join(info.metadata_tags) or '-'}")
    print(f"Wykryty typ:   {info.detected_media.value}  (cd_typed={info.is_cd_typed})")
    print(f"Ratio:         {info.compression_ratio*100:.1f}%")
    return 0


def cmd_audit(args, settings: Settings) -> int:
    chd = _make_chdman(settings)
    files = _iter_chd_files(Path(args.path))
    if not files:
        print("Nie znaleziono plików .chd", file=sys.stderr)
        return 2
    rows = []
    suspects = 0
    for f in files:
        r = audit_chd(chd, f, settings, do_verify=args.verify)
        flag = {
            AuditVerdict.OK: "OK",
            AuditVerdict.SUSPECT_WRONG_TYPE: "SUSPECT",
            AuditVerdict.VERIFY_FAILED: "VERIFY-FAIL",
            AuditVerdict.UNREADABLE: "UNREADABLE",
        }.get(r.verdict, r.verdict.value)
        if r.needs_fix:
            suspects += 1
        print(f"[{flag:11}] {f.name}  -> {r.message}")
        rows.append({
            "path": str(f),
            "verdict": r.verdict.value,
            "detected": r.detected_media.value,
            "expected": r.expected_media.value,
            "logical_mb": (r.info.logical_bytes // 2**20) if r.info else 0,
            "verify_ok": r.verify_ok,
            "message": r.message,
        })
    print(f"\nPodsumowanie: {len(files)} plików, {suspects} do naprawy.")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"Zapisano raport CSV: {args.csv}")
    return 0


def cmd_fix(args, settings: Settings) -> int:
    chd = _make_chdman(settings)
    files = _iter_chd_files(Path(args.path))
    if not files:
        print("Nie znaleziono plików .chd", file=sys.stderr)
        return 2
    if args.preset:
        settings.compression_preset = args.preset
    if args.aggressive:
        settings.aggressive_low_disk = True
    if args.no_roundtrip:
        settings.verify_roundtrip = False

    # Indeks DAT (opcjonalny) — do walidacji naprawionych obrazów.
    dat_index = None
    quarantine_dir = Path(args.quarantine_dir) if args.quarantine_dir else None
    dat_src = None if args.no_dat else (args.dat or settings.dat_dir)
    if dat_src:
        from .core.datfile import DatIndex
        dat_index = DatIndex.from_paths([Path(dat_src)])
        print(f"DAT: wczytano {dat_index.games} gier / {len(dat_index.by_sha1)} hashy SHA-1.")

    ok = fail = blocked = skipped = quarantined = 0
    for f in files:
        info = chd.info(f)
        verdict, expected, msg = _classify(chd, f, settings, info)
        if args.retype and verdict == AuditVerdict.SUSPECT_WRONG_TYPE:
            comp = presets.compression_for(settings.compression_preset, expected)
            if not args.yes and not _confirm(f"Retype {f.name} ({info.detected_media.value}→{expected.value})?"):
                skipped += 1; continue
            out = fixer.retype_file(chd, f, expected, settings, compression=comp,
                                    info=info, on_progress=_progress_printer("retype"),
                                    log=lambda m: print(f"\n  {m}"),
                                    dat_index=dat_index, quarantine_dir=quarantine_dir)
        elif args.recompress:
            comp = presets.compression_for(settings.compression_preset, info.detected_media)
            out = fixer.recompress_file(chd, f, settings, compression=comp, info=info,
                                        on_progress=_progress_printer("recompress"),
                                        log=lambda m: print(f"\n  {m}"))
        else:
            skipped += 1; continue

        print()
        if out.ok:
            ok += 1
            tag = f" [DAT: {out.dat_game}]" if out.dat_game else ""
            print(f"  ✔ {f.name}: {out.message}{tag}")
        elif out.quarantined:
            quarantined += 1; print(f"  ⚠ {f.name}: {out.message}")
        elif out.budget is not None and not out.budget.fits:
            blocked += 1; print(f"  ⛔ {f.name}: {out.message}")
        else:
            fail += 1; print(f"  ✗ {f.name}: {out.message}")

    print(f"\nGotowe. OK={ok} FAIL={fail} BLOCKED={blocked} "
          f"KWARANTANNA={quarantined} SKIP={skipped}")
    return 0 if fail == 0 else 1


def cmd_convert(args, settings: Settings) -> int:
    chd = _make_chdman(settings)
    if getattr(args, "delete_source", False):
        settings.delete_source_after_convert = True
    items = scan_paths([Path(args.path)])
    if not items:
        print("Nie znaleziono źródeł do konwersji", file=sys.stderr)
        return 2
    out_dir = Path(args.output) if args.output else None
    ok = fail = 0
    for it in items:
        media = it.media_type if it.media_type != MediaType.UNKNOWN else MediaType.CD
        dst_dir = out_dir or settings.resolved_output_dir(it.path)
        comp = presets.compression_for(settings.compression_preset, media)
        print(f"→ {it.path.name}  [{media.value}, pewność {it.confidence:.0%}]")
        out = fixer.create_from_source(chd, it.path, media, dst_dir, settings,
                                       compression=comp,
                                       on_progress=_progress_printer("create"),
                                       log=lambda m: print(f"\n  {m}"),
                                       delete_source=settings.delete_source_after_convert)
        print()
        if out.ok:
            ok += 1; print(f"  ✔ {out.message}")
        else:
            fail += 1; print(f"  ✗ {out.message}")
    print(f"\nGotowe. OK={ok} FAIL={fail}")
    return 0 if fail == 0 else 1


def _open_index(args, settings: Settings):
    from .core.fileindex import FileIndex, default_db_path
    db = Path(args.db) if getattr(args, "db", None) else (
        Path(settings.index_db_path) if settings.index_db_path else default_db_path())
    return FileIndex(db)


def cmd_index(args, settings: Settings) -> int:
    idx = _open_index(args, settings)
    exts = None
    if args.ext:
        exts = {e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()}
    prober = None
    if args.chd_content:
        chd = _make_chdman(settings)
        def prober(p: Path) -> str:
            i = chd.info(p)
            return i.data_sha1 or i.sha1 or ""

    def on_file(n: int, p: Path) -> None:
        if n % 50 == 0:
            sys.stdout.write(f"\r  {n} plików…  ")
            sys.stdout.flush()

    total_ok = True
    with idx:
        for root in args.roots:
            r = Path(root)
            print(f"Skanuję: {r}")
            try:
                st = idx.scan(r, full=args.full, exts=exts, chd_prober=prober,
                              on_file=on_file, log=lambda m: print(f"\n  {m}"))
            except NotADirectoryError as e:
                print(f"  BŁĄD: {e}", file=sys.stderr)
                total_ok = False
                continue
            print(f"\r  {st.summary()}")
        s = idx.stats()
        print(f"\nBaza: {idx.db_path}")
        print(f"Wpisów: {s['total']} (linki {s['links']}, brakujące {s['missing']}), "
              f"dane fizyczne {s['bytes'] / 2**30:.2f} GiB")
    return 0 if total_ok else 2


def cmd_dupes(args, settings: Settings) -> int:
    with _open_index(args, settings) as idx:
        groups = idx.duplicate_groups(min_size=args.min_size)
        if not groups:
            print("Brak duplikatów w indeksie.")
            return 0
        wasted = 0
        for g in groups:
            wasted += g.size * (len(g.paths) - 1)
            print(f"[{g.size / 2**20:8.1f} MB] sha1={g.sha1[:12]}…  ×{len(g.paths)}")
            for p in g.paths:
                print(f"    {p}")
        print(f"\nGrup: {len(groups)}, do odzyskania: {wasted / 2**30:.2f} GiB "
              f"(uruchom 'dedup', by zastąpić duplikaty symlinkami)")
    return 0


def cmd_dedup(args, settings: Settings) -> int:
    from .core.linker import LinkPrivilegeError, apply_dedup, plan_dedup
    with _open_index(args, settings) as idx:
        actions = plan_dedup(idx, prefer_roots=args.prefer or (),
                             min_size=args.min_size)
        if not actions:
            print("Brak duplikatów w indeksie.")
            return 0
        dry = not args.yes
        if dry:
            print("PODGLĄD (bez --yes nic nie zmieniam):")
        try:
            st = apply_dedup(actions, index=idx, dry_run=dry, log=print)
        except LinkPrivilegeError as e:
            print(f"Błąd: {e}", file=sys.stderr)
            return 3
        print(f"\n{st.summary()}")
    return 0 if st.errors == 0 else 1


def cmd_mirror(args, settings: Settings) -> int:
    from .core.linker import DEFAULT_EXCLUDES, LinkPrivilegeError, mirror_tree
    excludes = (tuple(e.strip() for e in args.exclude.split(",") if e.strip())
                if args.exclude else DEFAULT_EXCLUDES)
    try:
        st = mirror_tree(Path(args.source), Path(args.target),
                         exclude_names=excludes, rebuild=args.rebuild,
                         force=args.force, dry_run=args.dry_run,
                         log=print if (args.verbose or args.dry_run) else None)
    except LinkPrivilegeError as e:
        print(f"Błąd: {e}", file=sys.stderr)
        return 3
    except NotADirectoryError as e:
        print(f"Błąd: {e}", file=sys.stderr)
        return 2
    mode = "PODGLĄD" if args.dry_run else ("REBUILD" if args.rebuild else "SYNC")
    print(f"[{mode}] {st.summary()}")
    return 0 if st.errors == 0 else 1


def _discover_dats(args):
    from .core.datstore import DatStore
    store = DatStore(Path(args.dats), Path(args.roms))
    entries = store.discover(log=print)
    if not entries:
        print(f"Nie znaleziono plików .dat w {args.dats}", file=sys.stderr)
    return entries


def _scan_sources(idx, args, settings=None) -> None:
    """Przed dopasowaniem odśwież indeks wg poziomu skanu (ROM-y + ToSort)."""
    full, chd_mode = _scan_level(args)
    prober = None
    if chd_mode in ("header", "deep") and settings is not None:
        chd = _make_chdman(settings)

        def prober(p: Path) -> str:
            i = chd.info(p)
            return i.data_sha1 or i.sha1 or ""
    for root in (args.roms, getattr(args, "tosort", None)):
        if root and Path(root).is_dir():
            st = idx.scan(Path(root), full=full, chd_prober=prober)
            print(f"skan [{chd_mode}] {root}: {st.summary()}")


def _scan_level(args):
    """Poziom skanu z --scan-level (quick/normal/full) → (full, chd_mode)."""
    from .core.levels import ScanLevel, scan_settings
    name = getattr(args, "scan_level", None)
    if name:
        return scan_settings(ScanLevel(name))
    # zgodność wstecz: --full / --chd-deep
    full = getattr(args, "full", False)
    chd = "deep" if getattr(args, "chd_deep", False) else "header"
    return full, chd


def _maybe_deep_probe(idx, entries, args, settings: Settings) -> None:
    """--chd-deep: identyfikuj pliki .chd względem DAT-ów (nagłówek, potem
    ekstrakcja z fail-safe'ami chd_buddy). Wyniki zapisują się w indeksie."""
    if not getattr(args, "chd_deep", False):
        return
    from .core.matcher import deep_probe_chds
    chd = _make_chdman(settings)
    n = deep_probe_chds(
        idx, entries, chd,
        roots=[r for r in (args.roms, getattr(args, "tosort", None)) if r],
        work_dir=Path(settings.work_dir) if settings.work_dir else None,
        log=print)
    print(f"CHD zidentyfikowane: {n}")


def _load_rules(args):
    from .core.dirrules import DirRules
    rules = DirRules(Path(args.dats))
    if rules.error:
        print(f"UWAGA: {rules.error}", file=sys.stderr)
    return rules


def cmd_report(args, settings: Settings) -> int:
    from .core.matcher import RomState, match_store
    entries = _discover_dats(args)
    if not entries:
        return 2
    rules = _load_rules(args)
    skipped = [e.name for e in entries if rules.for_entry(e)["skip"]]
    if skipped:
        print(f"Pominięte regułą skip: {', '.join(skipped)}")
        entries = [e for e in entries if e.name not in set(skipped)]
    from .core.dirrules import apply_rule_targets
    apply_rule_targets(entries, rules, Path(args.roms))
    with _open_index(args, settings) as idx:
        _scan_sources(idx, args, settings)
        _maybe_deep_probe(idx, entries, args, settings)
        idx.build_match_cache()          # dopasowanie w RAM (sekundy, nie minuty)
        try:
            reports = match_store(entries, idx)
        finally:
            idx.drop_match_cache()
    grand_total = grand_have = 0
    for rep in reports:
        have = rep.count(RomState.HAVE, RomState.HAVE_CHD)
        grand_total += rep.total
        grand_have += have
        print(f"{rep.entry.name}")
        print(f"   {rep.summary()}   -> {rep.entry.target_dir}")
        if args.missing:
            for s in rep.statuses:
                if s.state == RomState.MISSING:
                    print(f"   BRAK: {s.game} :: {s.rom.name}")
    pct = (grand_have / grand_total * 100) if grand_total else 0.0
    print(f"\nRazem: {grand_have}/{grand_total} ({pct:.1f}%) w {len(reports)} DAT-ach.")
    return 0


def cmd_rebuild(args, settings: Settings) -> int:
    from .core.linker import LinkPrivilegeError
    from .core.matcher import match_store
    from .core.rebuilder import Rebuilder
    entries = _discover_dats(args)
    if not entries:
        return 2
    dry = not args.yes
    if dry:
        print("PODGLĄD (bez --yes nic nie zmieniam):")
    rules = _load_rules(args)
    from .core.dirrules import apply_rule_targets
    apply_rule_targets(entries, rules, Path(args.roms))
    with _open_index(args, settings) as idx:
        _scan_sources(idx, args, settings)
        _maybe_deep_probe(idx, entries, args, settings)
        idx.build_match_cache()          # dopasowanie w RAM (sekundy, nie minuty)
        try:
            reports = match_store(entries, idx)
        finally:
            idx.drop_match_cache()
        dedup_roots = ([] if args.keep_copies else
                       [r for r in (args.roms, args.tosort) if r])
        rb = Rebuilder(idx, tosort=Path(args.tosort) if args.tosort else None,
                       dry_run=dry, log=print)
        try:
            st = rb.run(reports, clean=args.clean,
                        only_complete=not args.incomplete,
                        rules=rules.for_entry,
                        dedup_roots=[Path(r) for r in dedup_roots])
        except LinkPrivilegeError as e:
            print(f"Błąd: {e}", file=sys.stderr)
            return 3
    print(f"\n{st.summary()}")
    return 0 if st.errors == 0 else 1


def cmd_bios(args, settings: Settings) -> int:
    from .core.bios import bios_run, import_system_dat, load_manifest, save_manifest
    if args.import_dat:
        m = load_manifest()
        added = import_system_dat(Path(args.import_dat), m)
        save_manifest(m)
        print(f"Import System.dat: dodano {added} definicji plików.")
        return 0
    src = args.input or settings.bios_dir
    if not src or not Path(src).is_dir():
        print("Wskaż katalog z BIOS-ami: --input (albo bios_dir w ustawieniach)",
              file=sys.stderr)
        return 2
    emu_root = args.emus or settings.emulators_dir
    st = bios_run(Path(src),
                  emu_root=Path(emu_root) if args.install and emu_root else None,
                  out_dir=Path(args.export) if args.export else None,
                  only=args.only, log=print)
    print(st.summary())
    return 0


def cmd_update(args, settings: Settings) -> int:
    from .core.updater import EMULATORS_UPDATE, run_updates
    if args.list:
        for k, v in EMULATORS_UPDATE.items():
            print(f"  {k:15s} [{v['type']:9s}] {v.get('repo', v['type']):40s} "
                  f"-> {v['dir']}")
        return 0
    emu_root = args.emus or settings.emulators_dir
    if not emu_root or not Path(emu_root).is_dir():
        print("Wskaż katalog emulatorów: --emus (albo emulators_dir "
              "w ustawieniach)", file=sys.stderr)
        return 2
    _updated, available = run_updates(Path(emu_root), args.only,
                                      check_only=args.check, force=args.force,
                                      log=print)
    if args.check:
        for k, (ver, has) in sorted(available.items()):
            if has:
                print(f"  AKTUALIZACJA: {k} → {ver}")
    return 0


def cmd_m3u(args, settings: Settings) -> int:
    from .core.playlists import generate_m3u
    try:
        st = generate_m3u(Path(args.root), overwrite=args.overwrite,
                          dry_run=args.dry_run, log=print)
    except NotADirectoryError as e:
        print(f"Błąd: {e}", file=sys.stderr)
        return 2
    print(st.summary())
    return 0


def cmd_icons(args, settings: Settings) -> int:
    from .core.icons import SgdbClient, make_icons_for_dir
    key = args.sgdb_key or settings.sgdb_api_key
    sgdb = SgdbClient(key) if key else None
    rom_dir = Path(args.rom_dir)
    if args.tree:
        dirs = sorted(d for d in rom_dir.iterdir() if d.is_dir())
    else:
        dirs = [rom_dir]
    total_errors = 0
    for d in dirs:
        system = args.system or d.name
        print(f"── {d.name}  [system: {system}]")
        try:
            st = make_icons_for_dir(
                d, system, out_dir=Path(args.out) if args.out else None,
                sgdb=sgdb, overwrite=args.overwrite, log=lambda m: print(f"  {m}"))
        except NotADirectoryError as e:
            print(f"  BŁĄD: {e}", file=sys.stderr)
            total_errors += 1
            continue
        print(f"  {st.summary()}")
        total_errors += st.errors
    return 0 if total_errors == 0 else 1


def cmd_shortcuts(args, settings: Settings) -> int:
    from .core.shortcuts import build_plan, create_shortcuts, detect_system, find_emulators
    emu_root = args.emus or settings.emulators_dir
    if not emu_root:
        print("Podaj katalog emulatorów: --emus D:\\emu\\Emulatory "
              "(albo ustaw emulators_dir w ustawieniach)", file=sys.stderr)
        return 2
    installed = find_emulators(Path(emu_root))
    if not installed:
        print(f"Nie znaleziono żadnego emulatora w {emu_root}", file=sys.stderr)
        return 2
    print(f"Emulatory: {', '.join(sorted(installed))}")
    rom_dir = Path(args.rom_dir)
    dirs = (sorted(d for d in rom_dir.iterdir() if d.is_dir())
            if args.tree else [rom_dir])
    total_failed = 0
    for d in dirs:
        system = args.system or detect_system(d.name) or d.name
        plan, why = build_plan(
            d, system, installed,
            override=settings.system_emulators.get(system, ""),
            out_dir=Path(args.out) if args.out else None,
            icons_dir=Path(args.icons) if args.icons else None)
        if why:
            print(f"── {d.name}: POMIŃ ({why})")
            continue
        print(f"── {d.name}  [system: {system}, gier: {len(plan)}]")
        st = create_shortcuts(plan, overwrite=args.overwrite,
                              dry_run=args.dry_run, log=lambda m: print(f"  {m}"))
        print(f"  {st.summary()}")
        total_failed += st.failed
    return 0 if total_failed == 0 else 1


def _classify(chd, path, settings, info):
    from .core.audit import classify_info
    return classify_info(info, settings.cd_max_logical_bytes)


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [t/N] ").strip().lower() in ("t", "tak", "y", "yes")
    except EOFError:
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chd-buddy", description="Audyt, konwersja i naprawa plików CHD")
    p.add_argument("--chdman", help="ścieżka do chdman (domyślnie z PATH)")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("info", help="pokaż metadane CHD")
    pi.add_argument("path")
    pi.set_defaults(func=cmd_info)

    pa = sub.add_parser("audit", help="audytuj kolekcję CHD")
    pa.add_argument("path")
    pa.add_argument("--verify", action="store_true", help="dodatkowo chdman verify (wolne)")
    pa.add_argument("--csv", help="zapisz raport CSV")
    pa.set_defaults(func=cmd_audit)

    pf = sub.add_parser("fix", help="napraw CHD (rekompresja/retype)")
    pf.add_argument("path")
    pf.add_argument("--recompress", action="store_true", help="rekompresja w miejscu (copy)")
    pf.add_argument("--retype", action="store_true", help="napraw błędny typ (np. DVD-jako-CD)")
    pf.add_argument("--preset", choices=presets.PRESET_NAMES, help="preset kompresji")
    pf.add_argument("--aggressive", action="store_true", help="tryb low-disk (usuwaj oryginał po extract)")
    pf.add_argument("--no-roundtrip", action="store_true",
                    help="wyłącz walidację round-trip (szybciej, mniej bezpiecznie)")
    pf.add_argument("--dat", help="plik .dat lub katalog z DAT-ami (walidacja Redump)")
    pf.add_argument("--no-dat", action="store_true",
                    help="wyłącz bramkę DAT (tylko round-trip) — dla obrazów "
                         "spatchowanych/fanowskich tłumaczeń, które nigdy nie "
                         "trafią w Redump")
    pf.add_argument("--quarantine-dir", help="katalog na niezweryfikowane oryginały")
    pf.add_argument("--yes", action="store_true", help="nie pytaj o potwierdzenie")
    pf.set_defaults(func=cmd_fix)

    pc = sub.add_parser("convert", help="konwertuj źródła (cue/iso/img) do CHD")
    pc.add_argument("path")
    pc.add_argument("--output", help="katalog wyjściowy")
    pc.add_argument("--preset", choices=presets.PRESET_NAMES)
    pc.add_argument("--delete-source", action="store_true",
                    help="usuń pliki źródłowe po udanej konwersji")
    pc.set_defaults(func=cmd_convert)

    px = sub.add_parser("index", help="skanuj katalogi do trwałego indeksu (przyrostowo)")
    px.add_argument("roots", nargs="+", help="katalogi do zeskanowania (źródłowe i docelowe)")
    px.add_argument("--db", help="plik bazy indeksu (domyślnie obok programu)")
    px.add_argument("--full", action="store_true",
                    help="wymuś ponowne policzenie sum (ignoruj rozmiar+mtime)")
    px.add_argument("--ext", help="tylko te rozszerzenia, po przecinku (np. chd,iso,bin,cue)")
    px.add_argument("--chd-content", action="store_true",
                    help="dla .chd pobierz też SHA-1 zawartości z chdman (do trafień w DAT)")
    px.set_defaults(func=cmd_index)

    pd = sub.add_parser("dupes", help="pokaż duplikaty wg indeksu (ten sam SHA-1)")
    pd.add_argument("--db", help="plik bazy indeksu")
    pd.add_argument("--min-size", type=int, default=1, help="minimalny rozmiar pliku (B)")
    pd.set_defaults(func=cmd_dupes)

    pdd = sub.add_parser("dedup", help="zastąp duplikaty symlinkami (jedna kopia fizyczna)")
    pdd.add_argument("--db", help="plik bazy indeksu")
    pdd.add_argument("--prefer", action="append", metavar="KATALOG",
                     help="katalog preferowany na kopię fizyczną (można podać wielokrotnie, "
                          "kolejność = priorytet)")
    pdd.add_argument("--min-size", type=int, default=1)
    pdd.add_argument("--yes", action="store_true",
                     help="wykonaj podmiany (bez tego tylko podgląd)")
    pdd.set_defaults(func=cmd_dedup)

    pm = sub.add_parser("mirror", help="mirror drzewa ROM-ów symlinkami (serwer → RetroBat)")
    pm.add_argument("source", help="katalog źródłowy na serwerze (np. Z:\\ROMS)")
    pm.add_argument("target", help="katalog docelowy (np. C:\\RetroBat\\roms)")
    pm.add_argument("--rebuild", action="store_true",
                    help="usuń wszystkie zarządzane linki i utwórz od nowa")
    pm.add_argument("--force", action="store_true", help="nadpisuj istniejące linki")
    pm.add_argument("--exclude", help="wykluczenia po przecinku (domyślnie images,manuals,videos)")
    pm.add_argument("--dry-run", action="store_true", help="tylko podgląd")
    pm.add_argument("--verbose", action="store_true", help="loguj każdą operację")
    pm.set_defaults(func=cmd_mirror)

    pr = sub.add_parser("report", help="stan kolekcji względem drzewa DAT-ów (jak RomVault)")
    pr.add_argument("--dats", required=True, help="katalog z DAT-ami (DatRoot)")
    pr.add_argument("--roms", required=True, help="katalog główny ROM-ów (RomRoot)")
    pr.add_argument("--db", help="plik bazy indeksu")
    pr.add_argument("--tosort", help="katalog ToSort — też skanowany jako źródło")
    pr.add_argument("--missing", action="store_true", help="wypisz brakujące ROM-y")
    pr.add_argument("--chd-deep", action="store_true",
                    help="identyfikuj CHD ekstrakcją (CD bin/cue, DVD-jako-CD); "
                         "wynik zapisywany w indeksie — liczy się raz")
    pr.add_argument("--scan-level", choices=["quick", "normal", "full"],
                    help="poziom skanowania (RomVault-style): quick=szybki, "
                         "normal=pełne sumy+nagłówek CHD, full=przelicz wszystko "
                         "+ ekstrakcja CHD")
    pr.set_defaults(func=cmd_report)

    prb = sub.add_parser("rebuild", help="napraw kolekcję: przenieś/przemianuj/podlinkuj wg DAT-ów")
    prb.add_argument("--dats", required=True, help="katalog z DAT-ami (DatRoot)")
    prb.add_argument("--roms", required=True, help="katalog główny ROM-ów (RomRoot)")
    prb.add_argument("--db", help="plik bazy indeksu")
    prb.add_argument("--tosort", help="katalog ToSort na pliki nieznane")
    prb.add_argument("--clean", action="store_true",
                     help="przenieś nieznane pliki z katalogów DAT-ów do ToSort")
    prb.add_argument("--incomplete", action="store_true",
                     help="buduj też gry niekompletne (domyślnie tylko takie, "
                          "których wszystkie pliki są dostępne)")
    prb.add_argument("--chd-deep", action="store_true",
                     help="identyfikuj CHD ekstrakcją przed naprawą")
    prb.add_argument("--keep-copies", action="store_true",
                     help="NIE zamieniaj fizycznych kopii potwierdzonych "
                          "plików na symlinki (domyślnie zamieniamy — "
                          "kopia fizyczna zostaje w katalogu rodzica)")
    prb.add_argument("--yes", action="store_true",
                     help="wykonaj operacje (bez tego tylko podgląd)")
    prb.set_defaults(func=cmd_rebuild)

    pb = sub.add_parser("bios", help="BIOS-y: skan po MD5 + instalacja/eksport wg manifestu")
    pb.add_argument("--input", help="katalog z BIOS-ami (domyślnie bios_dir z ustawień)")
    pb.add_argument("--install", action="store_true",
                    help="instaluj prosto do katalogów emulatorów")
    pb.add_argument("--emus", help="katalog emulatorów (domyślnie z ustawień)")
    pb.add_argument("--export", help="eksportuj do <katalog>/bios/<emulator>/")
    pb.add_argument("--only", nargs="+", metavar="EMU", help="tylko te emulatory")
    pb.add_argument("--import-dat", metavar="SYSTEM_DAT",
                    help="zaimportuj hashe z libretro System.dat do manifestu")
    pb.set_defaults(func=cmd_bios)

    pu = sub.add_parser("update", help="aktualizuj emulatory (GitHub/buildbot/…)")
    pu.add_argument("--check", action="store_true", help="tylko sprawdź wersje")
    pu.add_argument("--force", action="store_true", help="wymuś ponowną instalację")
    pu.add_argument("--only", nargs="+", metavar="EMU")
    pu.add_argument("--emus", help="katalog emulatorów (domyślnie z ustawień)")
    pu.add_argument("--list", action="store_true", help="lista obsługiwanych")
    pu.set_defaults(func=cmd_update)

    pm3 = sub.add_parser("m3u", help="generuj playlisty .m3u dla gier multi-disc")
    pm3.add_argument("root", help="katalog gier (rekurencyjnie)")
    pm3.add_argument("--overwrite", action="store_true", help="nadpisuj istniejące .m3u")
    pm3.add_argument("--dry-run", action="store_true", help="tylko podgląd")
    pm3.set_defaults(func=cmd_m3u)

    pic = sub.add_parser("icons", help="twórz ikony .ico gier (Libretro Thumbnails + SteamGridDB)")
    pic.add_argument("rom_dir", help="katalog z grami jednego systemu (albo RomRoot z --tree)")
    pic.add_argument("--system", help="system, np. PS2 albo 'Sony - PlayStation 2' "
                                      "(domyślnie nazwa katalogu)")
    pic.add_argument("--tree", action="store_true",
                     help="traktuj podkatalogi jako systemy (RomRoot)")
    pic.add_argument("--out", help="katalog wyjściowy na .ico (domyślnie <rom_dir>\\icons)")
    pic.add_argument("--overwrite", action="store_true", help="nadpisuj istniejące .ico")
    pic.add_argument("--sgdb-key", help="klucz API SteamGridDB (fallback; domyślnie z ustawień)")
    pic.set_defaults(func=cmd_icons)

    psh = sub.add_parser("shortcuts", help="twórz skróty .lnk z właściwą składnią emulatora")
    psh.add_argument("rom_dir", help="katalog gier jednego systemu (albo RomRoot z --tree)")
    psh.add_argument("--emus", help="katalog główny emulatorów (np. D:\\emu\\Emulatory; "
                                    "domyślnie z ustawień)")
    psh.add_argument("--system", help="wymuś system (np. PS2; domyślnie z nazwy katalogu)")
    psh.add_argument("--tree", action="store_true",
                     help="traktuj podkatalogi jako systemy (RomRoot)")
    psh.add_argument("--out", help="katalog na .lnk (domyślnie <rom_dir>\\shortcuts)")
    psh.add_argument("--icons", help="katalog z .ico (domyślnie <rom_dir>\\icons)")
    psh.add_argument("--overwrite", action="store_true", help="nadpisuj istniejące .lnk")
    psh.add_argument("--dry-run", action="store_true", help="tylko podgląd")
    psh.set_defaults(func=cmd_shortcuts)
    return p


def main(argv: list[str] | None = None) -> int:
    # konsole Windows (cp852/cp1250) nie znają części znaków — nie wywalaj się
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.load()
    if getattr(args, "chdman", None):
        settings.chdman_path = args.chdman
    if getattr(args, "preset", None):
        settings.compression_preset = args.preset
    try:
        return args.func(args, settings)
    except CHDManNotFound as e:
        print(f"Błąd: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"Błąd uruchomienia chdman ('{settings.chdman_path or 'chdman'}'): {e}\n"
              f"Wskaż poprawny plik chdman.exe przez --chdman lub w ustawieniach.",
              file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

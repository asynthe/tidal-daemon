"""tidal-daemon — label and artist discographies, matched to a music service and kept downloaded.

    tidal-daemon serve                daemon: re-checks followed sources, downloads the queue
    tidal-daemon tui                  open the TUI (connects to the daemon)
    tidal-daemon login                log in to the backend (tidaler by default)
    tidal-daemon list                 labels and artists, with progress
    tidal-daemon add <url>            add an artist/label from a service artist link or Bandcamp page
    tidal-daemon follow <name>        watch it for new releases and download them (--off to stop)
    tidal-daemon scrape <name>        refresh the release list
    tidal-daemon check <name>         refresh + match against the backend, asking when unsure
    tidal-daemon dl <name>            download matched releases not downloaded yet
    ... check|dl <name> --only TEXT     just releases whose artist/title has TEXT
    tidal-daemon mark <name> owned|missing|review --only TEXT
    tidal-daemon link <name> <album-link> --only TEXT    attach an album search missed
    tidal-daemon shop <name>          not on the backend: free vs paid on Bandcamp
    tidal-daemon scan <name>          find albums you added to the folder; mark them owned
    tidal-daemon verify <name>        check downloads: every track there, readable, FLAC
    tidal-daemon tidy <name>          fix names with invisible characters or too-long paths
    tidal-daemon covers [--apply]     embed sharper cover art (the original, or cover.* in the folder)

Everything lives in one SQLite file, .tidal-daemon/music.db, inside the music folder. Music lands in
<archive>/(<name>)/ (or the source's `folder`); the folders hold music only.
"""

from __future__ import annotations

import argparse
import shutil
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import db, library
from .backends import BackendError, parse_any_link
from .downloads import download, groups_of
from .library import (
    DECISION_FIELDS,
    get_source,
    key,
    label_dir,
    load_entries,
    picked,
    save_entries,
    say,
    status,
    status_label,
    summarize,
    tally,
)
from .local import album_folder, check_album, scan_local
from .matching import apply_match, describe, fetch_album, resolve, score
from .sources import bandcamp_name, bandcamp_price, refresh
from .text import safe_folder, slug


# --------------------------------------------------------------------------- sources


def cmd_login(args):
    raise SystemExit(library.backend().login())


def cmd_list(args):
    for name, src in sorted(library.SOURCES.items()):
        e = load_entries(name)
        stat = f"{len(e)} releases: {tally(e)}" if e else "-"
        target = src.get("site") or src.get("artist") or src.get("path") or src.get("name") or ""
        star = "*" if src.get("follow") else " "
        print(f"{star} {name:<14} {src.get('kind', '?'):<8} {target[:34]:<34} {stat}")
    if any(s.get("follow") for s in library.SOURCES.values()):
        print("\n* followed: the daemon re-checks it and downloads new releases")


def _print_releases(entries: list[dict]) -> None:
    by_type: dict[str, list[dict]] = {}
    for e in entries:
        by_type.setdefault(e.get("type") or "?", []).append(e)
    for kind, items in by_type.items():
        print(f"\n{kind} ({len(items)})")
        for e in sorted(items, key=lambda e: e.get("date") or "", reverse=True):
            print(f"  {(e.get('date') or '')[:10]:<10}  {e['artist']} - {e['title']}")


def add_source(url: str, name: str | None = None, key_: str | None = None, label: bool = False, mp3_whitelist: bool = False) -> str:
    """Add a source from a link; returns its key. Shared with the daemon."""
    url = url.strip()
    s = library.settings()
    link = parse_any_link(url, s)
    if link and link[1] == "artist":
        b, _, artist_id = link
        artist = b.artist(artist_id)
        name = name or artist.name
        cfg = {"kind": "artist", "backend": b.name, "artist": artist.name, "artist_id": artist.id, "include": ["albums", "ep_singles"]}
    elif "bandcamp.com" in url:
        import re

        sub = re.search(r"https?://([a-z0-9-]+)\.bandcamp\.com", url, re.I)
        if not sub:
            sys.exit(f"not a Bandcamp page: {url}")
        site = f"https://{sub.group(1).lower()}.bandcamp.com"
        name = name or bandcamp_name(site) or sub.group(1)
        cfg = {"kind": "bandcamp", "site": site, "types": ["album", "track"]}
    else:
        sys.exit("give an artist link from a service (e.g. https://.../artist/<id>) or a Bandcamp page (<name>.bandcamp.com)")
    k = key_ or slug(name) or "artist"
    if k in library.SOURCES:
        sys.exit(f"{k!r} already exists — pass --key to add it under another name")
    cfg = {"name": name, **({} if label else {"folder": library.settings()["artist_folder"].format(name=safe_folder(name))}), **cfg}
    if mp3_whitelist:
        cfg["allow_lossy"] = True
    # A folder of yours the daemon listed as local-only becomes this source;
    # its albums get matched to the new releases (and stay yours).
    folder = cfg.get("folder") or library.settings()["label_folder"].format(name=safe_folder(name))
    shared = library.artists_dir() is not None and library.archive() / folder == library.artists_dir()
    for other, src in list(library.SOURCES.items()):
        # in the shared ARTISTS folder every artist has the same folder: match the name too
        if src.get("kind") == "local" and src.get("folder") == folder and (not shared or slug(src.get("name", "")) == slug(name)):
            db.delete_source(other)
            library.load_sources()
    db.put_source(k, cfg)
    library.load_sources()
    say(f"added {k} -> {label_dir(k)}")
    return k


def cmd_add(args):
    k = add_source(args.url, args.name, args.key, args.label, args.mp3_whitelist)
    library.lock_label(k)
    entries, _ = refresh(k, [])
    save_entries(k, entries)
    _print_releases(entries)
    if args.follow:
        _follow(k, True)
    nxt = f"tidal-daemon follow {k}  (or: tidal-daemon dl {k})" if entries and all(status(e) == "matched" for e in entries) else f"tidal-daemon check {k}"
    print(f"\n{len(entries)} releases. Next: {nxt}")


def _follow(name: str, on: bool, everything: bool = False) -> None:
    db.set_follow(name, on)
    if on and everything:
        matched = [key(e) for e in load_entries(name) if status(e) == "matched"]
        db.set_wanted(name, matched, True)
        say(f"queued {len(matched)} matched releases")
    if on and not library.SOURCES[name].get("last_scan"):
        db.set_last_scan(name)  # what's there now is the baseline; only later releases are "new"


def cmd_follow(args):
    get_source(args.name)
    _follow(args.name, not args.off, everything=args.all)
    say(f"{args.name}: {'not followed' if args.off else 'followed — the daemon checks it for new releases and downloads them'}")


def cmd_mp3_whitelist(args):
    """Mark a source as never having a lossless release available, so its
    albums never get flagged/failed for not being FLAC (check/verify/scan/dl)."""
    src = get_source(args.name)
    on = not args.off
    db.put_source(args.name, {**src, "allow_lossy": on})
    library.load_sources()
    say(f"{args.name}: {'MP3/lossy allowed, never flagged' if on else 'back to FLAC-only (will be flagged again)'}")


def cmd_scrape(args):
    say(f"scraping {args.name} ({get_source(args.name).get('kind')})")
    entries, new = refresh(args.name)
    save_entries(args.name, entries)
    say(f"  {len(entries)} releases, {len(new)} new")


def cmd_check(args):
    """Refresh the list, then match it against the backend. Resumable."""
    src = get_source(args.name)
    b = library.backend()
    say(f"checking {args.name} ({src.get('kind')})")
    before = [] if args.force and not args.only else None  # -f with --only keeps the rest
    entries, new = refresh(args.name, before)
    say(f"  {len(entries) - len(new)} known, {len(new)} new")

    def save():
        """Persist after every decision, so ^C or `q` never loses review work."""
        save_entries(args.name, entries)

    scan_local(args.name, entries)
    save()
    resolve(b, entries, label=src.get("label", ""), threshold=args.threshold, auto=args.auto, delay=args.delay, force=args.force, save=save, only=args.only)
    summarize(args.name, entries)


def cmd_scan(args):
    get_source(args.name)
    entries = load_entries(args.name)
    if not entries:
        sys.exit(f"no releases for {args.name} — run `tidal-daemon scrape {args.name}` first")
    if not label_dir(args.name).exists():
        sys.exit(f"{label_dir(args.name)} doesn't exist yet")
    if scan_local(args.name, entries):
        summarize(args.name, entries)
    else:
        say("nothing new")


# --------------------------------------------------------------------------- decisions


def cmd_mark(args):
    """owned (have it, never download), missing (not on the backend) or review
    (forget every decision, ask again on the next check)."""
    get_source(args.name)
    entries = load_entries(args.name)
    hits = [e for e in entries if picked(e, args.only)]
    if not hits:
        sys.exit("no releases match --only")
    for e in hits:
        say(f"  {status(e):<8} {e['artist']} - {e['title']}")
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("not a terminal — pass -y to confirm")
        if input(f"mark these {len(hits)} as {args.state}? [y/N]: ").strip().lower() != "y":
            sys.exit("nothing changed")
    for e in hits:
        if args.state == "owned":
            e["owned"] = True
        else:
            for f in DECISION_FIELDS:
                e.pop(f, None)
            if args.state == "missing":
                e["skipped"] = True
                e["note"] = f"no {library.backend().label} release"
        if args.note:
            e["note"] = args.note
    summarize(args.name, entries)


def cmd_link(args):
    """Attach an album to one release by hand, for when search missed it."""
    get_source(args.name)
    link = parse_any_link(args.link, library.settings())
    b, album_id = (link[0], link[2]) if link and link[1] == "album" else (library.backend(), None)
    if album_id is None:
        hit = b.parse_link(args.link)
        album_id = hit[1] if hit and hit[0] == "album" else None
    if not album_id:
        sys.exit(f"not an album link: {args.link}")
    entries = load_entries(args.name)
    hits = [e for e in entries if picked(e, args.only)]
    if len(hits) != 1:
        for e in hits:
            say(f"  {status(e):<8} {e['artist']} - {e['title']}")
        sys.exit(f"--only must pick exactly one release, got {len(hits)} — narrow it down")
    rec = hits[0]
    album = fetch_album(b, album_id)
    if album is None:
        sys.exit(1)
    say(f"  {rec['artist']} - {rec['title']}  [{status(rec)}]")
    say(f"  -> {describe(album)}")
    for f in DECISION_FIELDS:
        if f != "bandcamp":
            rec.pop(f, None)
    rec.update(score=round(score(rec, album), 3), guess=f"{album.artist} - {album.title}", guess_id=album.id)
    apply_match(rec, b, album, confirmed=True)
    summarize(args.name, entries)


def cmd_shop(args):
    """For releases not on the backend: what's free on Bandcamp, and what costs."""
    get_source(args.name)
    entries = load_entries(args.name)
    todo = [e for e in entries if status(e) == "missing" and picked(e, args.only)]
    if not todo:
        sys.exit("no releases marked missing" + (" match --only" if args.only else ""))
    fetch_n = [e for e in todo if "bandcamp.com/" in (e.get("source_url") or "") and (args.force or not e.get("bandcamp"))]
    for i, e in enumerate(fetch_n, 1):
        say(f"[{i}/{len(fetch_n)}] {e['artist']} - {e['title']}")
        try:
            e["bandcamp"] = {**bandcamp_price(e["source_url"]), "checked": f"{datetime.now():%Y-%m-%d}"}
        except SystemExit as ex:  # one bad page shouldn't stop the rest
            e["bandcamp"] = {"error": str(ex)}
        library.save_entry(args.name, e)
        time.sleep(args.delay)

    free, paid, other = [], [], []
    for e in todo:
        bc = e.get("bandcamp") or {}
        (free if bc.get("free") else paid if "price" in bc else other).append(e)

    def line(e):
        return f"{e['artist']} - {e['title']}\n      {e['source_url']}"

    print(f"\nFREE on Bandcamp ({len(free)})")
    for e in free:
        bc = e["bandcamp"]
        print(f"  {bc['kind']}{' (asks for email)' if bc.get('email') else ''}: {line(e)}")
    print(f"\nTO BUY ({len(paid)})")
    totals: dict[str, float] = {}
    for e in sorted(paid, key=lambda e: -(e["bandcamp"]["price"] or 0)):
        bc = e["bandcamp"]
        cur = bc.get("currency") or "?"
        totals[cur] = totals.get(cur, 0) + (bc["price"] or 0)
        print(f"  {bc['price']:>6.2f} {cur}  {line(e)}")
    if totals:
        print("  total: " + ", ".join(f"{v:.2f} {k}" for k, v in totals.items()))
    if other:
        print(f"\nNO BANDCAMP PRICE ({len(other)}) — find these elsewhere")
        for e in other:
            print(f"  {e['artist']} - {e['title']}  [{(e.get('bandcamp') or {}).get('error') or 'not a Bandcamp release'}]")
    print(f'\nOnce you have one: tidal-daemon mark {args.name} owned --only "<title>"')


# --------------------------------------------------------------------------- downloads


def cmd_dl(args):
    """Download matched releases that aren't downloaded yet."""
    from rich.console import Group
    from rich.live import Live
    from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
    from rich.table import Table

    get_source(args.name)
    entries = load_entries(args.name)
    if not entries:
        sys.exit(f"no releases for {args.name} — run `tidal-daemon check {args.name}` first")
    if scan_local(args.name, entries):
        save_entries(args.name, entries)
    state = library.release_state(args.name)

    def todo(e):
        if not picked(e, args.only):
            return False
        if args.failed:
            return bool(e.get("failed"))
        done = state.get(key(e), {}).get("downloaded") and not e.get("failed")
        return args.redo or not done

    groups = [g for g in groups_of(entries) if any(todo(e) for e in g)]
    if not groups:
        sys.exit("no failed downloads to retry" if args.failed else "nothing to download (all matched releases are done)")

    s = library.settings()
    abort = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: abort.set())
    signal.signal(signal.SIGTERM, lambda *_: abort.set())

    cols = (TextColumn("[progress.description]{task.description}"), BarColumn(), TaskProgressColumn())
    overall = Progress(*cols, expand=True)
    task = overall.add_task(f"Downloading album... [0/{len(groups)}]", total=len(groups))
    colours = {"done": "green", "warn": "yellow", "error": "red"}

    def printer(msg: str, level: str) -> None:
        if level in colours:
            overall.print(f"[{colours[level]}]{msg}[/{colours[level]}]", markup=True)
        else:
            overall.print(msg, markup=False)

    class Bars:
        """The backend's per-album bar (once it has one) above the overall bar."""

        def __rich__(self):
            grid = Table.grid()
            p = library.backend().progress()
            if p is not None:
                grid.add_row(p)
            grid.add_row(overall)
            return grid

    say(f"{len(groups)} albums -> {label_dir(args.name)}")
    try:
        with Live(Group(Bars()), refresh_per_second=10, vertical_overflow="visible"):
            report = download(
                args.name,
                groups,
                abort=abort,
                printer=printer,
                retries=args.retries if args.retries is not None else s["retries"],
                retry_wait=args.retry_wait if args.retry_wait is not None else s["retry_wait"],
                pause=args.pause if args.pause is not None else s["pause"],
                on_album=lambda i, n, label: overall.update(task, completed=i - 1, description=f"Downloading album... [{i}/{n}]"),
            )
            overall.update(task, completed=len(groups))
    except BackendError as e:
        sys.exit(str(e))

    if report.interrupted:
        say("\nstopped — run it again to continue; finished albums are skipped")
    if report.failures:
        say(f"\n{len(report.failures)} failed:")
        for e, ex in report.failures:
            say(f"  {e['artist']} - {e['title']}  ({type(ex).__name__}: {ex})")
        say(f"recorded on the releases; retry just these: tidal-daemon dl {args.name} --failed")
    elif not report.interrupted:
        say("\nall albums downloaded")
    if report.lossy:
        say(f"\n[!] {len(report.lossy)} not FLAC (no lossless version?):")
        for e, bad in report.lossy:
            say(f"  {e['artist']} - {e['title']}  ({bad})")


def cmd_verify(args):
    """Audit downloaded albums: every track present, readable, FLAC. Problems
    are recorded as "failed" (so `dl --failed` retries them); albums that
    pass have any old failure cleared."""
    get_source(args.name)
    entries = load_entries(args.name)
    dest = label_dir(args.name)
    ffmpeg = shutil.which("ffmpeg") if args.deep else None
    if args.deep and not ffmpeg:
        sys.exit("--deep needs ffmpeg on PATH")
    groups = [g for g in groups_of(entries) if picked(g[0], args.only)]

    ok = bad = absent = 0
    for n, group in enumerate(groups, 1):
        e = group[0]
        label = f"{e['artist']} - {e['title']}"
        if album_folder(dest, e) is None:
            absent += 1
            continue
        print(f"\r[{n}/{len(groups)}] checking...", end="", file=sys.stderr, flush=True)
        try:
            problems, record, _ = check_album(args.name, group, ffmpeg=ffmpeg, fix=args.fix)
        except Exception as ex:  # no song list right now: leave the album as it was
            say(f"\r  can't check {label}: {ex}")
            continue

        keys = []
        for x in group:
            if problems:
                x["failed"] = {"when": f"{datetime.now():%Y-%m-%d %H:%M}", "error": "; ".join(problems), **record}
            else:
                x.pop("failed", None)
            keys.append(key(x))
        db.set_downloaded(args.name, keys, None if problems else db.now())
        if problems:
            bad += 1
            say(f"\r[!] {label}: {'; '.join(problems)}" + (" (broken files deleted)" if record.get("broken") and args.fix else ""))
            for m in record.get("missing") or []:
                say(f"      missing: {m}")
        else:
            ok += 1
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    save_entries(args.name, entries)

    scope = [e for e in entries if picked(e, args.only)]
    n = {st: [e for e in scope if status(e) == st] for st in ("matched", "owned", "missing", "review")}

    def on_disk(e):
        local = (e.get("note") or "").removeprefix("local: ")
        return (local and (dest / local).is_dir()) or album_folder(dest, e) is not None

    here = sum(1 for e in n["owned"] if on_disk(e))
    rows = [
        (status_label("matched") + "s", n["matched"], f"{ok} complete, {bad} with problems, {absent} not downloaded yet"),
        ("owned", n["owned"], f"{here} in the folder, {len(n['owned']) - here} kept elsewhere"),
        (status_label("missing"), n["missing"], f"prices: tidal-daemon shop {args.name}"),
        ("to review", n["review"], f"match them: tidal-daemon check {args.name}"),
    ]
    say(f"\n{library.display_name(args.name)}: {len(scope)} releases")
    for title, items, detail in rows:
        say(f"  {title:<18}{len(items):>5}   {detail if items else ''}".rstrip())
    if bad:
        hint = "" if args.fix else " (add --fix first if files are unreadable: downloads skip files that exist)"
        say(f"re-download them: tidal-daemon dl {args.name} --failed{hint}")


# --------------------------------------------------------------------------- daemon / tui


def _yes(question: str) -> bool:
    try:
        return input(f"{question}  Y / N: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_setup(args):
    """The same questions the TUI asks on a fresh start, in the terminal."""
    arch, artists, dbp = library.archive(), library.artists_dir(), library.db_path()
    if not arch.is_dir():
        if not _yes(f"Create directory '{library.show(arch)}'?"):
            sys.exit("Nothing created. Choose another music folder with: tidal-daemon setup --archive <folder>")
        arch.mkdir(parents=True)
    if artists and not artists.is_dir() and _yes(f"Create directory '{library.show(artists)}'?"):
        artists.mkdir(parents=True)
    if dbp.exists():
        print(f"database: {library.show(dbp)}")
        return
    if not _yes(f"Create database '{library.show(dbp)}'?"):
        sys.exit("No database created; tidal-daemon needs it to work.")
    library.init(create=True)
    print(f"created {library.show(dbp)}: an empty library, add labels and artists from the TUI")


def cmd_tidy(args):
    """Rename album folders/files with invisible characters or too-long
    paths, then re-check those albums so the database matches the disk."""
    from .local import tidy_names

    get_source(args.name)
    entries = load_entries(args.name)
    renamed = tidy_names(args.name, entries, dry_run=args.dry_run)
    if not renamed:
        say("nothing to tidy")
        return
    for old, new in renamed:
        say(f"  {old!r}\n    -> {new!r}")
    if args.dry_run:
        say(f"{len(renamed)} album(s) would be renamed (run without --dry-run to do it)")
        return
    save_entries(args.name, entries)
    folders = {new for _, new in renamed}
    for group in groups_of(entries):
        if group[0].get("folder") in folders:
            try:
                check_album(args.name, group)
                for x in group:
                    library.save_entry(args.name, x)
            except Exception as ex:
                say(f"  can't re-check {group[0]['title']}: {ex}")
    say(f"{len(renamed)} album(s) tidied and re-checked")


def album_folders() -> list[tuple[Path, str | None]]:
    """Every album folder in the archive, with the artist its ARTISTS folder
    names (None under a label, OST, ...). Albums sit one level below
    (<Label>)/, ARTISTS/ (ARTISTS/<Artist>/ in the older layout), OST/ and so on."""
    root, artists = library.archive(), library.artists_dir()
    out = []
    for top in sorted(p for p in root.iterdir() if p.is_dir()):
        if artists is not None and top.resolve() == artists.resolve():
            owners = library.folder_owners()
            for d in sorted(p for p in top.iterdir() if p.is_dir()):
                if library.folder_artists(d.name):  # ARTISTS/<Artist - Album>
                    out.append((d, library.display_name(owners.get(d.name, "")) or None))
                else:
                    out += [(a, d.name) for a in sorted(d.iterdir()) if a.is_dir()]
        else:
            out += [(a, None) for a in sorted(top.iterdir()) if a.is_dir()]
    return out


def cmd_rename_files(args):
    """One naming standard for every album: "01 - Artist 1; Artist 2 - Title".
    Without --apply nothing changes: the full list goes to a report file."""
    from .naming import apply_album, plan_album

    root = library.archive()
    plans = [plan_album(folder, artist) for folder, artist in album_folders()]
    plans = [p for p in plans if p.renames or p.skipped or p.note]
    to_do = [p for p in plans if p.renames]
    skipped = [p for p in plans if p.skipped]
    notes = [p for p in plans if p.note]
    files = sum(len(p.renames) for p in to_do)

    report = library.data_dir() / "rename-plan.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{files} files to rename in {len(to_do)} albums; {len(skipped)} albums left alone", ""]
    for p in to_do:
        lines.append(str(p.folder.relative_to(root)))
        lines += [f"    {a.name}\n -> {b.name}" for a, b in p.renames]
        lines.append("")
    lines += ["== left alone ==", *(f"{p.folder.relative_to(root)}: {p.skipped}" for p in skipped), ""]
    lines += ["== artist folder vs tags (not renamed for this, just worth a look) ==", *(f"{p.folder.relative_to(root)}: {p.note}" for p in notes)]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    say(f"{files} files to rename in {len(to_do)} albums, {len(skipped)} albums left alone, {len(notes)} worth a look (artist folder vs tags)")
    say(f"full list: {library.show(report)}")
    if not args.apply:
        say("nothing changed; run again with --apply to rename")
        return

    owners = {library.label_dir(n).resolve(): n for n in library.SOURCES if not library.is_shared(n)}
    shared = library.folder_owners()
    arts = library.artists_dir()
    done_albums = done_files = 0
    for p in to_do:
        in_shared = arts is not None and p.folder.parent.resolve() == arts.resolve()
        name = shared.get(p.folder.name) if in_shared else owners.get(p.folder.parent.resolve())
        if name is not None and not library.lock_label(name, wait=True):
            say(f"  busy, skipped (run again later): {p.folder.relative_to(root)}")
            continue
        try:
            mapping = apply_album(p)
            done_albums += 1
            done_files += len(mapping)
            if name is not None:  # keep the stored song list's file names in step
                for old, new in mapping.items():
                    db.conn().execute("UPDATE tracks SET file=? WHERE source=? AND file=?", (new, name, old))
        finally:
            if name is not None:
                library.unlock_label(name)
    say(f"renamed {done_files} files in {done_albums} albums")


def cmd_flatten_artists(args):
    """ARTISTS/<Artist>/<Artist - Album>  ->  ARTISTS/<Artist - Album>: every
    artist's albums side by side. Dry run unless --apply; the daemon must be
    stopped (it would see the folders move half-way)."""
    root, arts = library.archive(), library.artists_dir()
    if arts is None or not arts.is_dir():
        sys.exit("no ARTISTS folder")
    rel = arts.relative_to(root).as_posix()
    if args.apply and not library.lock_label("_daemon", wait=True):
        sys.exit("stop the daemon first (q in the TUI quits it, or Ctrl+C where `serve` runs), then run this again")
    moves, problems, sources = [], [], []
    taken = {p.name.casefold() for p in arts.iterdir()}
    for k, src in sorted(library.SOURCES.items()):
        folder = str(src.get("folder", "")).replace("\\", "/")
        if not folder.startswith(rel + "/"):
            continue
        d = root / folder
        sources.append(k)
        if not d.is_dir():
            continue
        for a in sorted(d.iterdir()):
            if a.is_file():
                problems.append(f"{a.relative_to(root)}: a file, not an album folder (stays)")
            elif a.name.casefold() in taken:
                problems.append(f"{a.relative_to(root)}: {rel}/{a.name} already exists (stays)")
            else:
                taken.add(a.name.casefold())
                moves.append((k, a, arts / a.name))
    for k, a, t in moves:
        say(f"  {a.relative_to(root)}  ->  {t.relative_to(root)}")
    for x in problems:
        say(f"  [!] {x}")
    say(f"{len(moves)} album folders from {len(sources)} artist folders; {len(problems)} left where they are")
    if not args.apply:
        say("nothing changed; run again with --apply (with the daemon stopped)")
        return
    for k in sources:
        library.lock_label(k)
    for k, a, t in moves:
        a.rename(t)
    for k in sources:
        old = root / library.SOURCES[k]["folder"]
        cfg = {**library.SOURCES[k], "folder": rel}
        db.put_source(k, cfg)
        if old.is_dir() and not any(old.iterdir()):
            old.rmdir()
    db.set_setting("artist_folder", rel)
    library.load_sources()
    say(f"moved {len(moves)} album folders; artists added from now on go straight into {rel}")


def _adb() -> str:
    """adb from PATH, else where winget puts Google's platform tools."""
    import glob
    import os

    found = shutil.which("adb") or next(iter(glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools*\platform-tools\adb.exe"))), None)
    if not found:
        sys.exit("adb not found: winget install Google.PlatformTools")
    return found


def cmd_sync_phone(args):
    """Mirror the archive's FLAC files (and their folders) onto an Android
    phone over adb, like `rsync -r --delete --include=*.flac`: files of the
    same size are skipped, and anything else in the phone folder (other
    music, images, ...) is deleted. Dry run unless --apply."""
    import subprocess

    adb, dest = _adb(), args.to.rstrip("/")
    if not dest.startswith("/"):  # Git Bash turns /sdcard/... into C:/Program Files/Git/sdcard/...
        sys.exit(f"--to must be a path on the phone like /sdcard/Music, not {dest!r} (in Git Bash write //sdcard/Music)")
    run = lambda *a, **kw: subprocess.run([adb, *a], capture_output=True, **kw)
    devices = [x for x in run("devices").stdout.decode().splitlines()[1:] if x.strip()]
    if not any(x.endswith("\tdevice") for x in devices):
        sys.exit(f"no phone ready ({'; '.join(devices) or 'none connected'}): plug it in, allow USB debugging")
    root = library.archive()
    mine = {f.relative_to(root).as_posix(): f for f in root.rglob("*") if f.is_file() and f.suffix.lower() == ".flac"}
    out = run("shell", f"mkdir -p '{dest}' && find '{dest}' -type f -exec stat -c '%s %n' {{}} +").stdout.decode("utf-8", "replace")
    phone = {}
    for line in out.splitlines():
        size, _, path = line.partition(" ")
        if path.startswith(dest + "/") and size.isdigit():
            phone[path[len(dest) + 1:]] = int(size)
    push = [rel for rel, f in sorted(mine.items()) if phone.get(rel) != f.stat().st_size]
    delete = sorted(rel for rel in phone if rel not in mine)
    gb = sum(mine[r].stat().st_size for r in push) / 1e9
    same = len(mine) - len(push)
    say(f"phone {dest}: {len(phone)} files there; {same} already up to date")
    say(f"  copy   {len(push)} FLAC files ({gb:.1f} GB)")
    say(f"  delete {len(delete)} files that aren't FLACs from the archive ({sum(phone[r] for r in delete) / 1e9:.1f} GB)")
    for r in delete[:15]:
        say(f"    - {r}")
    if len(delete) > 15:
        say(f"    ... and {len(delete) - 15} more")
    if not args.apply:
        say("nothing changed; run again with --apply")
        return
    if delete:
        q = lambda p: "'" + p.replace("'", r"'\''") + "'"  # single-quoted for sh
        script = library.data_dir() / "phone-delete.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("".join(f"rm -f {q(dest + '/' + r)}\n" for r in delete), encoding="utf-8", newline="\n")
        run("push", str(script), "/data/local/tmp/tidal-daemon-delete.sh", check=True)
        run("shell", "sh /data/local/tmp/tidal-daemon-delete.sh; rm /data/local/tmp/tidal-daemon-delete.sh", check=True)
        say(f"deleted {len(delete)}")
    done = 0.0
    for i, rel in enumerate(push, 1):
        r = run("push", str(mine[rel]), f"{dest}/{rel}")
        if r.returncode != 0:
            say(f"  [!] {rel}: {r.stderr.decode('utf-8', 'replace').strip()[-200:]}")
            continue
        done += mine[rel].stat().st_size / 1e9
        say(f"  [{i}/{len(push)}  {done:.1f}/{gb:.1f} GB]  {rel}")
    run("shell", f"find '{dest}' -mindepth 1 -type d -empty -delete")
    # let music apps see the new files without a reboot
    run("shell", "content call --method scan_volume --uri content://media --arg external_primary")
    say("phone in step with the archive")


def cmd_link_artists(args):
    """Local artists (folders under ARTISTS/ the daemon listed as yours) get
    their backend artist: the whole discography shows up to download, and the
    albums you have stay checked. A candidate is only taken when its
    discography contains albums you have, so same-name artists don't mix."""
    from .local import adopt_folders, album_tags, audio_files
    from .text import norm, ratio

    b = library.backend()
    locals_ = {k: s for k, s in library.SOURCES.items() if s.get("kind") == "local" and str(s.get("folder", "")).startswith(
        str(library.settings()["artist_folder"]).split("{")[0])}
    if args.only:
        locals_ = {k: s for k, s in locals_.items() if any(o.casefold() in s["name"].casefold() for o in args.only)}
    if not locals_:
        sys.exit("no local artists to link")
    linked, unsure = [], []
    for lkey, src in sorted(locals_.items(), key=lambda kv: kv[1]["name"].casefold()):
        name = src["name"]
        mine = []
        for d in library.album_dirs(lkey):
            files = audio_files(d)
            if files:
                mine.append(norm(album_tags(d, files)[1] or d.name))
        best, exact = None, []
        try:
            for cand in b.find_artists(name, limit=6):
                if ratio(name, cand.name) < 0.85:
                    continue
                albums = b.artist_albums(cand.id, {"albums", "ep_singles", "other"})
                if norm(cand.name) == norm(name) and albums:
                    exact.append(cand)
                titles = {norm(a.title) for _, a in albums}
                overlap = sum(1 for t in mine if t in titles or any(x.startswith(t) or t.startswith(x) for x in titles if len(x) > 3 and len(t) > 3))
                if overlap and (best is None or overlap > best[0]):
                    best = (overlap, cand)
        except Exception as ex:
            say(f"  {name}: lookup failed ({ex})")
            continue
        if best is None and len(exact) == 1 and not args.sure:
            # The only artist of exactly that name, but none of your albums
            # are on the service (removed, or never there): link by name.
            best = (0, exact[0])
        if best is None:
            unsure.append(name)
            why = "several artists with that name, none with your albums" if len(exact) > 1 else "no artist with that name and releases"
            say(f"  {name}: {why}; left local")
            continue
        overlap, cand = best
        how = f"{overlap} of your {len(mine)} albums in their discography" if overlap else "by name (none of your albums are on " + b.label + ")"
        say(f"  {name}  ->  {b.label} {cand.name} ({cand.id}): {how}")
        linked.append(name)
        if not args.apply:
            continue
        new_key = slug(name) or "artist"
        while new_key in library.SOURCES:
            new_key += "-"
        if not library.lock_label(lkey, wait=True):
            say("    busy, skipped (try again later)")
            continue
        try:
            cfg = {"name": name, "folder": src["folder"], "kind": "artist", "backend": b.name, "artist": cand.name,
                   "artist_id": cand.id, "include": ["albums", "ep_singles"]}  # fmt: skip
            db.put_source(new_key, cfg)  # same folder: the daemon won't re-list it as local
            library.load_sources()
            library.lock_label(new_key)
            entries, _ = refresh(new_key, [])
            adopt_folders(new_key, entries)  # your albums: matched and checked, or kept as extras
            save_entries(new_key, entries)
            db.delete_source(lkey)
            library.load_sources()
            have = sum(1 for e in entries if e.get("origin") == "local")
            say(f"    {len(entries)} releases; {have} of them yours")
        finally:
            library.unlock_label(lkey)
            library.unlock_label(new_key)
    say(f"\n{len(linked)} linked{'' if args.apply else ' (dry run: nothing changed, add --apply)'}, {len(unsure)} left local"
        + (f": {', '.join(unsure)}" if unsure else ""))
    if unsure:
        say("to link one by hand: Search (3), find the artist, enter: it merges with the folder you have")


def cmd_covers(args):
    """Embed the sharpest cover there is (the service's original, or a
    bigger cover.* in the folder) into every album whose embedded art is small."""
    from .covers import MAX, upgrade_all

    folders = [d for d, _ in album_folders() if not args.only or any(t.casefold() in d.name.casefold() for t in args.only)]
    session = None
    if not args.local:
        try:
            session = library.backend("tidaler").session
        except BackendError as ex:
            sys.exit(f"{ex} (or pass --local to only use covers in the folders)")
    say(f"{len(folders)} album folders; embedding up to {MAX}px{'' if args.apply else ' (dry run: nothing changed, add --apply)'}")
    done, left, failed = upgrade_all(folders, session, say, args.apply)
    say(f"\n{done} {'upgraded' if args.apply else 'would be upgraded'}, {left} already sharp or nothing better found, {len(failed)} failed")
    for f in failed:
        say(f"  [!] {f}")


def cmd_serve(args):
    from .daemon import serve

    serve(port=args.port)


def cmd_tui(args):
    from .tui import run

    run(host=args.host, port=args.port)


# --------------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(prog="tidal-daemon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def only(p, required=False, help_="only releases whose artist or title contains TEXT (repeatable)"):
        p.add_argument("--only", action="append", metavar="TEXT", required=required, help=help_)

    p = sub.add_parser("serve", help="run the daemon")
    p.add_argument("--port", type=int, help="local API port (default: the port setting, 8737)")
    p.set_defaults(fn=cmd_serve, locks=False)

    p = sub.add_parser("tui", help="open the TUI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_tui, locks=False)

    p = sub.add_parser("setup", help="create the music folder and its database (music.db)")
    p.add_argument("--archive", help="use this music folder (saved for next time)")
    p.set_defaults(fn=cmd_setup, locks=False)

    sub.add_parser("login", help="log in to the backend").set_defaults(fn=cmd_login, locks=False)
    sub.add_parser("list", help="labels and artists, with progress").set_defaults(fn=cmd_list, locks=False)

    p = sub.add_parser("add", help="add an artist/label from a link and list its releases")
    p.add_argument("url")
    p.add_argument("--name", help="display name (default: the artist's name)")
    p.add_argument("--key", help="short name for commands (default: from the name)")
    p.add_argument("--label", action="store_true", help="file it as a label, (<name>)/, instead of ARTISTS/<name>/")
    p.add_argument("--follow", action="store_true", help="follow it: future releases get downloaded (tick existing ones in the TUI, or `follow --all`)")
    p.add_argument("--mp3-whitelist", action="store_true", help="no lossless release exists for this source: never flag/fail its albums for not being FLAC")
    p.set_defaults(fn=cmd_add, locks=False)

    p = sub.add_parser("follow", help="watch for new releases (the daemon downloads them)")
    p.add_argument("name")
    p.add_argument("--off", action="store_true", help="stop following")
    p.add_argument("--all", action="store_true", help="also queue every release matched so far")
    p.set_defaults(fn=cmd_follow, locks=False)

    p = sub.add_parser("mp3-whitelist", help="mark a source as never having a lossless release (never flagged for not being FLAC)")
    p.add_argument("name")
    p.add_argument("--off", action="store_true", help="back to FLAC-only (flagged again)")
    p.set_defaults(fn=cmd_mp3_whitelist, locks=False)

    p = sub.add_parser("scrape", help="refresh the release list")
    p.add_argument("name")
    p.set_defaults(fn=cmd_scrape)

    p = sub.add_parser("check", help="refresh + match, resuming where it stopped")
    p.add_argument("name")
    p.add_argument("-f", "--force", action="store_true", help="re-check everything, discarding past decisions")
    p.add_argument("--threshold", type=float, default=0.80, help="min score marked as a likely match")
    p.add_argument("--auto", type=float, default=0.97, help="accept without asking at or above this score")
    p.add_argument("--delay", type=float, default=0.6, help="seconds between searches")
    only(p)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("dl", help="download matched releases not downloaded yet")
    p.add_argument("name")
    only(p)
    p.add_argument("--pause", type=float, help="seconds between albums (default: setting, 10)")
    p.add_argument("--retries", type=int, help="retries per album (default: setting, 3)")
    p.add_argument("--retry-wait", type=float, help="seconds before each retry (default: setting, 10)")
    p.add_argument("--failed", action="store_true", help="only albums a previous download couldn't finish")
    p.add_argument("--redo", action="store_true", help="also re-check albums already downloaded")
    p.set_defaults(fn=cmd_dl)

    p = sub.add_parser("mark", help="set releases as owned, missing or back to review")
    p.add_argument("name")
    p.add_argument("state", choices=["owned", "missing", "review"])
    only(p, required=True, help_="releases whose artist or title contains TEXT (repeatable)")
    p.add_argument("--note", help="why, kept on the release")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser("verify", help="audit downloads: all tracks there, readable, FLAC")
    p.add_argument("name")
    only(p)
    p.add_argument("--deep", action="store_true", help="also decode every file with ffmpeg (slow, thorough)")
    p.add_argument("--fix", action="store_true", help="delete unreadable files so `dl --failed` re-downloads them")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("link-artists", help="link local artist folders to their service artist (dry run unless --apply)")
    p.add_argument("--apply", action="store_true", help="really link (default: only show what would happen)")
    p.add_argument("--only", action="append", metavar="TEXT", help="only artists whose name contains TEXT (repeatable)")
    p.add_argument("--sure", action="store_true", help="only links confirmed by albums you have (no by-name links)")
    p.set_defaults(fn=cmd_link_artists, locks=False)

    p = sub.add_parser("sync-phone", help="mirror the archive's FLAC files onto an Android phone over adb (dry run unless --apply)")
    p.add_argument("--to", default="/sdcard/Music", help="folder on the phone (default /sdcard/Music); anything else in it is deleted")
    p.add_argument("--apply", action="store_true", help="really copy and delete")
    p.set_defaults(fn=cmd_sync_phone, locks=False)

    p = sub.add_parser("flatten-artists", help="ARTISTS/<Artist>/<Album> -> ARTISTS/<Artist - Album> (dry run unless --apply)")
    p.add_argument("--apply", action="store_true", help="really move (with the daemon stopped)")
    p.set_defaults(fn=cmd_flatten_artists, locks=False)

    p = sub.add_parser("rename-files", help='one naming standard everywhere: "01 - Artist 1; Artist 2 - Title" (dry run unless --apply)')
    p.add_argument("--apply", action="store_true", help="really rename (default: only write the list)")
    p.set_defaults(fn=cmd_rename_files, locks=False)

    p = sub.add_parser("covers", help="embed sharper cover art: the service's original or a bigger cover in the folder (dry run unless --apply)")
    only(p)
    p.add_argument("--local", action="store_true", help="don't ask the service, only use cover files in the album folders")
    p.add_argument("--apply", action="store_true", help="really re-embed")
    p.set_defaults(fn=cmd_covers, locks=False)

    p = sub.add_parser("tidy", help="fix folder/file names with invisible characters or too-long paths")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true", help="only show what would be renamed")
    p.set_defaults(fn=cmd_tidy)

    p = sub.add_parser("scan", help="find albums you added to the folder and mark them owned")
    p.add_argument("name")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("link", help="attach an album link to one release (when search missed it)")
    p.add_argument("name")
    p.add_argument("link", help="album link or id")
    only(p, required=True, help_="picks the release (must leave exactly one)")
    p.set_defaults(fn=cmd_link)

    p = sub.add_parser("shop", help="not on the backend: free vs paid on Bandcamp")
    p.add_argument("name")
    only(p)
    p.add_argument("-f", "--force", action="store_true", help="re-check prices already fetched")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between Bandcamp pages")
    p.set_defaults(fn=cmd_shop)

    args = ap.parse_args()
    if getattr(args, "archive", None):
        library.save_config(archive=args.archive)
    # The TUI is a client, the daemon waits for setup, and setup/login don't need the library.
    if args.cmd not in ("tui", "serve", "setup", "login"):
        try:
            library.init()
        except library.NoDatabase as e:
            sys.exit(f"No database at {library.show(e.args[0])} yet. Run `tidal-daemon setup` (or open `tidal-daemon tui`) to create it.")
    if getattr(args, "locks", True):
        get_source(args.name)
        library.lock_label(args.name)
    try:
        args.fn(args)
    except BackendError as e:
        sys.exit(str(e))

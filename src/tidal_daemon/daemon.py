"""The tidal-daemon daemon.

One process that keeps the library up to date on its own:

  * re-checks followed labels and artists every `follow_hours`; new releases
    are matched (auto-accepting only confident matches) and queued
  * works through the download queue (ticked, matched, not yet downloaded),
    one album at a time, with the same retries and checks as `tidal-daemon dl`
  * serves a small JSON API on 127.0.0.1 so the TUI (or anything else) can
    connect, look around and tell it what to do

Jobs that change a source take its lock, so the daemon and a CLI command
never work on the same source at once; the daemon just waits its turn.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, library
from .downloads import download, groups_of
from .backends import parse_any_link
from .local import adopt_folders, album_folder, audio_files, check_album, inventory, read_source_tag, tidy_names, write_source_tag
from .text import slug
from .library import key, status
from .matching import not_on_service, resolve
from .sources import bandcamp_tracks, collapse_versions, refresh

RETRY_FAILED_AFTER = timedelta(hours=1)


class Daemon:
    def __init__(self, port: int):
        self.port = port
        self.stop = threading.Event()
        self.wake = threading.Event()  # skip the idle wait: new work arrived
        self.abort = threading.Event()  # stop the album in flight (shutdown only)
        self.jobs: queue.Queue = queue.Queue()
        self.reconcile_now = True  # check the archive against the database at start

    # ------------------------------------------------------------------ logging / state

    def log(self, msg: str, level: str = "info", source: str | None = None, detail: str | None = None) -> None:
        msg = msg.strip()
        if not msg:
            return
        print(f"{datetime.now():%H:%M:%S} {level:<5} {source or '-'}: {msg}", flush=True)
        if level != "debug" and db.PATH is not None:
            db.log(source, level, msg, detail)

    def busy(self, what: str | None) -> None:
        if db.PATH is not None:
            db.set_state("current", what)
            db.set_state("heartbeat", db.now())

    def open_library(self) -> bool:
        """Open music.db once it exists (until then the daemon only answers setup)."""
        if db.PATH is not None:
            return True
        try:
            library.init()
        except library.NoDatabase:
            return False
        db.set_state("pid", os.getpid())
        db.set_state("started", db.now())
        self.log(f"library open: {library.show(library.db_path())}")
        self.reconcile_now = True
        return True

    # ------------------------------------------------------------------ work

    def scan(self, name: str) -> None:
        """Re-read a source; match and queue what's new."""
        if not library.lock_label(name, wait=True):
            self.log("busy (another job has it), will retry", "warn", name)
            return
        try:
            self.busy(f"checking {library.display_name(name)}")
            src = library.get_source(name)
            entries, new = refresh(name)
            if any(status(e) == "review" for e in new):
                resolve(library.backend(), new, label=src.get("label", ""), threshold=library.settings()["auto_match"], interactive=False)
            library.save_entries(name, entries)
            skip_atmos = library.settings()["skip_atmos"]
            queued = (
                [key(e) for e in new if status(e) == "matched" and not (skip_atmos and e.get("audio") == "atmos")]
                if src.get("follow")
                else []
            )
            db.set_wanted(name, queued, True)
            for e in new:
                st = "queued" if key(e) in queued else {"matched": "matched", "review": "needs a match: tidal-daemon check " + name}.get(status(e), status(e))
                self.log(f"new release: {e['artist']} - {e['title']} ({st})", "new", name)
            self.log(f"checked: {len(entries)} releases, {len(new)} new", "info", name)
            db.set_last_scan(name)
        except (Exception, SystemExit) as ex:
            self.log(f"check failed: {ex}", "error", name, traceback.format_exc())
            db.set_last_scan(name)  # don't hammer a broken source; try again next interval
        finally:
            library.unlock_label(name)
            self.busy(None)

    def scan_due(self) -> None:
        hours = float(library.settings()["follow_hours"])
        for name, src in list(library.SOURCES.items()):
            if self.stop.is_set():
                return
            if not src.get("follow"):
                continue
            last = src.get("last_scan")
            if last is None or datetime.fromisoformat(last) < datetime.now() - timedelta(hours=hours):
                self.scan(name)

    def next_download(self):
        """(source, group) for the first queued album that isn't waiting out a
        recent failure and whose source is free."""
        for row in db.queue():
            e = json.loads(row["entry"])
            if e.get("audio") == "atmos" and library.settings()["skip_atmos"]:
                continue
            failed = e.get("failed") or {}
            if failed.get("when"):
                try:
                    if datetime.strptime(failed["when"], "%Y-%m-%d %H:%M") > datetime.now() - RETRY_FAILED_AFTER:
                        continue
                except ValueError:
                    pass
            name = row["source"]
            if name not in library.SOURCES:
                continue
            other = library.elsewhere(name).get(row["rkey"])
            if other and other[1] in ("downloaded", "yours"):  # e.g. the label's copy: don't download it twice
                db.set_wanted(name, [row["rkey"]], False)
                self.log(f"{e.get('artist')} - {e.get('title')}: already in {other[0]}, not downloaded again", "info", name)
                continue
            if other and other[1] in ("listed", "queued"):  # a label's release: it goes in the label's folder
                db.set_wanted(other[2], [other[3]], True)
                db.set_wanted(name, [row["rkey"]], False)
                self.log(f"{e.get('artist')} - {e.get('title')}: a {other[0]} release, downloading it into {other[0]}", "info", name)
                self.wake.set()
                continue
            entries = library.load_entries(name)
            for group in groups_of(entries):
                if any(key(x) == row["rkey"] for x in group):
                    return name, group
        return None

    def tidy(self, name: str) -> None:
        """Rename folders/files with invisible characters or too-long paths
        (older downloads; new ones are named cleanly), then re-check those
        albums so the database matches the disk."""
        if library.SOURCES.get(name, {}).get("kind") == "local":
            return  # all yours: never renamed
        if not library.lock_label(name, wait=True):
            return  # busy (downloading): next round
        try:
            entries = library.load_entries(name)
            renamed = tidy_names(name, entries)
            if not renamed:
                return
            library.save_entries(name, entries)
            folders = {new for _, new in renamed}
            for group in groups_of(entries):
                if group[0].get("folder") in folders:
                    try:
                        check_album(name, group)
                        for x in group:
                            library.save_entry(name, x)
                    except Exception as ex:
                        self.log(f"can't re-check {group[0]['title']}: {ex}", "warn", name)
            for old, new in renamed:
                self.log(f"renamed {library.show(Path(old))!r} -> {new!r} (invisible characters / path too long)", "info", name)
        except Exception as ex:
            self.log(f"tidy failed: {ex}", "warn", name, traceback.format_exc())
        finally:
            library.unlock_label(name)

    def tag_sources(self, name: str) -> None:
        """SOURCE files for albums tidal-daemon downloaded before the setting
        existed (never for albums you brought in yourself, never overwriting)."""
        tag = library.settings().get("source_tag")
        if not tag:
            return
        dest, n = library.label_dir(name), 0
        for r in db.release_rows(name):
            if r["status"] != "matched" or not r["downloaded"]:
                continue
            e = json.loads(r["entry"])
            if e.get("origin") == "local":
                continue
            n += write_source_tag(album_folder(dest, e), tag)
        if n:
            self.log(f"wrote SOURCE ({tag}) into {n} album folder(s)", "info", name)

    def compare_tracklists(self, name: str, limit: int = 40) -> None:
        """Bandcamp releases: read the label's own track list once, and note
        (short) the tracks the matched album lacks, e.g. a compilation the
        service carries without some tracks. At most `limit` pages a pass."""
        if library.SOURCES[name].get("kind") != "bandcamp":
            return
        b, fetched, flagged = library.backend(), 0, []
        for r in db.release_rows(name):
            if self.stop.is_set() or fetched >= limit:
                break
            e = json.loads(r["entry"])
            if r["status"] != "matched" or not r["match_id"] or e.get("short_for") == r["match_id"]:
                continue
            try:
                if "tracklist" not in e:
                    e["tracklist"] = bandcamp_tracks(e["source_url"])
                    fetched += 1
                ours = [t["title"] for t in db.album_tracks(name, r["backend"], r["match_id"])]
                ours = ours or [t.title for t in b.album_tracks(r["match_id"])]
            except Exception as ex:
                self.log(f"track list of {e['artist']} - {e['title']}: {ex}", "debug", name)
                continue
            e.update(short=not_on_service(e["tracklist"], ours), short_for=r["match_id"])
            library.save_entry(name, e)
            if e["short"]:
                flagged.append(f"{e['title']} ({', '.join(e['short'])})")
        for f in flagged:
            self.log(f"{b.label}'s version lacks tracks: {f}", "warn", name)

    def discover_local(self) -> None:
        """Label folders and ARTISTS/<name> folders with albums but no source
        become local sources: your own music, listed as owned."""
        # A source added from the CLI while the daemon runs is only in music.db:
        # without a reload, its folder looks unclaimed and gets a second, local source.
        library.load_sources()
        root = library.archive()
        artists = library.artists_dir()
        taken = {library.label_dir(n).resolve() for n in library.SOURCES}
        candidates = [d for d in root.iterdir() if d.is_dir() and (artists is None or d.resolve() != artists.resolve())]
        if artists is not None and artists.is_dir():
            # ARTISTS/<Artist - Album>: albums no artist claims become that artist's (local) listing
            owners = library.folder_owners()
            new_artists = {}
            for d in artists.iterdir():
                names = library.folder_artists(d.name)
                if d.is_dir() and names and d.name not in owners and d.resolve() not in taken and audio_files(d):
                    new_artists.setdefault(names[0].casefold(), names[0])
            for name in new_artists.values():
                key = "local-" + (slug(name) or "folder")
                while key in library.SOURCES:
                    key += "-"
                db.put_source(key, {"name": name, "kind": "local", "folder": artists.relative_to(root).as_posix()})
                library.load_sources()
                self.log(f"found albums of yours by {name} in {artists.name}: listed as {name} (local)", "new", key)
            # older layout: ARTISTS/<Artist>/<Album>
            candidates += [d for d in artists.iterdir() if d.is_dir() and not library.folder_artists(d.name)]
        for d in candidates:
            if d.resolve() in taken or not any(audio_files(a) for a in d.iterdir() if a.is_dir()):
                continue
            rel = d.relative_to(root).as_posix()
            name = d.name[1:-1] if d.name.startswith("(") and d.name.endswith(")") else d.name
            key = "local-" + (slug(name) or "folder")
            while key in library.SOURCES:
                key += "-"
            db.put_source(key, {"name": name, "kind": "local", "folder": rel})
            library.load_sources()
            self.log(f"found your folder {rel}: listed as {name} (local)", "new", key)

    def adopt(self, name: str) -> None:
        """Albums you put into a source's folder: owned, with their SOURCE."""
        if not library.lock_label(name, wait=True):
            return
        try:
            src = library.SOURCES.get(name, {})
            if src.get("kind") == "local":
                entries, new = refresh(name)
                if new or len(entries) != len(db.release_rows(name)) or any(
                    e.get("source_tag") != json.loads(r["entry"]).get("source_tag")
                    for e, r in zip(entries, db.release_rows(name))
                ):
                    library.save_entries(name, entries)
                    if new:
                        self.log(f"{len(new)} album(s) of yours listed", "info", name)
                return
            entries = library.load_entries(name)
            changed = adopt_folders(name, entries)
            if changed:
                library.save_entries(name, entries)
                for c in changed[:20]:
                    self.log(c, "info", name)
        except Exception as ex:
            self.log(f"listing your albums failed: {ex}", "warn", name, traceback.format_exc())
        finally:
            library.unlock_label(name)

    def reconcile(self) -> None:
        """Tidy names, then: albums marked downloaded whose folder is gone
        (moved, deleted) are marked not downloaded and unticked: a fresh start
        means picking again, not re-downloading the whole library behind your back."""
        if not library.archive().is_dir():
            return  # missing archive (unplugged drive?): wait for setup, change nothing
        self.discover_local()
        for name in list(library.SOURCES):
            self.tidy(name)
            self.tag_sources(name)
            self.adopt(name)
            self.compare_tracklists(name)
        for name in list(library.SOURCES):
            if library.SOURCES[name].get("kind") != "local":  # local sources are re-read from disk anyway
                lost = []
                for r in db.release_rows(name):
                    e = json.loads(r["entry"])
                    if e.get("origin") == "local" and e.get("folder") and album_folder(library.label_dir(name), e) is None:
                        lost.append(e)
                for e in lost:  # your own copy is gone: back to a plain release (downloadable if matched)
                    for k in (*library.OWN_FIELDS, "folder"):
                        e.pop(k, None)
                    library.save_entry(name, e)
                if lost:
                    self.log(f"{len(lost)} of your own album(s) no longer on disk: no longer marked as yours", "warn", name)
            if library.SOURCES[name].get("kind") == "artist" and library.lock_label(name, wait=True):
                try:
                    entries = library.load_entries(name)
                    kept = collapse_versions(name, entries)
                    if len(kept) < len(entries):
                        library.save_entries(name, kept)
                        self.log(f"{len(entries) - len(kept)} duplicate version(s) hidden (kept the best copy of each)", "info", name)
                finally:
                    library.unlock_label(name)
            gone = []
            for r in db.release_rows(name):
                if r["downloaded"] and r["status"] == "matched":
                    e = json.loads(r["entry"])
                    if album_folder(library.label_dir(name), e) is None:
                        gone.append((r["rkey"], e, r["backend"], r["match_id"]))
            if not gone:
                continue
            for rkey, e, backend, album_id in gone:
                e.pop("folder", None)
                library.save_entry(name, e)
                db.conn().execute("DELETE FROM tracks WHERE source=? AND backend=? AND album_id=?", (name, backend, album_id))
            keys = [g[0] for g in gone]
            db.set_downloaded(name, keys, None)
            db.set_wanted(name, keys, False)
            self.log(f"{len(gone)} downloaded album(s) no longer on disk: marked not downloaded and unticked", "warn", name)

    def download_one(self) -> bool:
        if not library.archive().is_dir():
            db.set_state("needs_setup", True)
            return False  # never create the archive on our own; the TUI asks first
        db.set_state("needs_setup", False)
        nxt = self.next_download()
        if nxt is None:
            return False
        name, group = nxt
        if not library.lock_label(name, wait=True):
            return False
        e = group[0]
        label = f"{e['artist']} - {e['title']}"
        # Track progress for clients: files already there (a resumed album)
        # plus each "Downloaded item" line the backend reports.
        folder = album_folder(library.label_dir(name), e)
        theirs = read_source_tag(folder)
        if folder is not None and theirs and theirs != library.settings().get("source_tag"):
            # a folder of yours from elsewhere (bandcamp.com, ...) already has this album:
            # downloading would mix two copies in one folder, so it counts as yours instead
            try:
                e.update(owned=True, origin="local", folder=folder.name, source_tag=theirs)
                for x in group:
                    library.save_entry(name, {**x, **{k: e[k] for k in ("owned", "origin", "folder", "source_tag")}})
                db.set_wanted(name, [key(x) for x in group], False)
                self.log(f"{label}: you already have it ({theirs}) in {folder.name}; not downloaded", "info", name)
            finally:
                library.unlock_label(name)
            return True
        progress = {"source": name, "label": label, "done": len(audio_files(folder)) if folder else 0, "total": e.get("tracks")}

        def printer(msg: str, level: str) -> None:
            if msg.startswith("Downloaded item"):
                progress["done"] += 1
                db.set_state("progress", progress)
            # Per-track lines go to stdout only; downloads.py records the
            # album's outcome as an event itself.
            self.log(msg, {"info": "debug", "done": "debug", "error": "debug"}.get(level, level), name)

        try:
            self.busy(f"downloading {label}")
            db.set_state("progress", progress)
            s = library.settings()
            download(
                name,
                [group],
                abort=self.abort,
                printer=printer,
                retries=int(s["retries"]),
                retry_wait=float(s["retry_wait"]),
                pause=0,
            )
        except (Exception, SystemExit) as ex:
            self.log(f"download failed: {label}: {ex}", "error", name, traceback.format_exc())
        finally:
            db.set_state("progress", None)
            library.unlock_label(name)
            self.busy(None)
        return True

    def verify(self, name: str, rkey: str | None = None) -> None:
        """Check downloaded albums song by song (one, or all of a source)."""
        if not library.lock_label(name, wait=True):
            self.log("busy (another job has it), will retry", "warn", name)
            return
        try:
            entries = library.load_entries(name)
            groups = [g for g in groups_of(entries) if rkey is None or any(key(x) == rkey for x in g)]
            ok = bad = 0
            for group in groups:
                e = group[0]
                label = f"{e['artist']} - {e['title']}"
                if album_folder(library.label_dir(name), e) is None:
                    continue
                self.busy(f"checking files: {label}")
                try:
                    problems, record, _ = check_album(name, group)
                except Exception as ex:
                    self.log(f"can't check {label}: {ex}", "warn", name)
                    continue
                for x in group:
                    if problems:
                        x["failed"] = {"when": f"{datetime.now():%Y-%m-%d %H:%M}", "error": "; ".join(problems), **record}
                    else:
                        x.pop("failed", None)
                    library.save_entry(name, x)
                db.set_downloaded(name, [key(x) for x in group], None if problems else db.now())
                if problems:
                    bad += 1
                    self.log(f"{label}: {'; '.join(problems)}", "error", name)
                else:
                    ok += 1
            self.log(f"files checked: {ok} complete, {bad} with problems", "done" if not bad else "warn", name)
        finally:
            library.unlock_label(name)
            self.busy(None)

    def run_jobs(self) -> None:
        while not self.jobs.empty():
            job, *args = self.jobs.get()
            try:
                if job == "scan":
                    self.scan(args[0])
                elif job == "verify":
                    self.verify(*args)
                elif job == "add":
                    from .cli import add_source

                    url, as_label, follow = args
                    link = parse_any_link(url, library.settings())
                    have = library.find_artist_source(link[0].name, link[2]) if link and link[1] == "artist" else None
                    if have:  # asked twice (enter pressed again while adding): nothing to do
                        self.log(f"{library.display_name(have)} is already in your library", "info", have)
                        continue
                    self.busy(f"adding {url}")
                    k = add_source(url, label=as_label)
                    self.scan(k)
                    if follow:  # future releases only; you pick from what's there now
                        db.set_follow(k, True)
                        library.load_sources()
                    self.log(f"added {library.display_name(k)}", "new", k)
            except (Exception, SystemExit) as ex:
                self.log(f"{job} failed: {ex}", "error", None, traceback.format_exc())
            finally:
                self.busy(None)

    # ------------------------------------------------------------------ main loop

    def run(self) -> None:
        # One daemon per library: two would download the same queue twice.
        if not library.lock_label("_daemon", wait=True):
            raise SystemExit("a tidal-daemon is already running for this library (`tidal-daemon tui` connects to it)")
        library.say = lambda m: self.log(m, "debug")  # scraper/matcher chatter: stdout only
        try:
            server = _Server(("127.0.0.1", self.port), _handler(self))
        except OSError as e:
            raise SystemExit(f"port {self.port} is taken ({e}); set another with --port")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.log(f"daemon up on http://127.0.0.1:{self.port}")
        if not self.open_library():
            self.log(f"no library yet at {library.show(library.db_path())}: waiting for setup (open `tidal-daemon tui`)")
        try:
            last_reconcile = 0.0
            while not self.stop.is_set():
                if not self.open_library():
                    self.wake.wait(2)
                    self.wake.clear()
                    continue
                library.load_sources()
                if self.reconcile_now or time.monotonic() - last_reconcile > 300:
                    self.reconcile_now = False
                    self.reconcile()
                    last_reconcile = time.monotonic()
                self.busy(None)
                self.run_jobs()
                self.scan_due()
                worked = False
                if not library.settings()["paused"] and not self.stop.is_set():
                    worked = self.download_one()
                if worked:
                    self.stop.wait(float(library.settings()["pause"]))
                else:
                    self.wake.wait(15)
                    self.wake.clear()
        except KeyboardInterrupt:
            pass
        finally:
            self.abort.set()
            server.shutdown()
            if db.PATH is not None:
                db.set_state("current", None)
                db.set_state("pid", None)
            self.log("daemon stopped")


# --------------------------------------------------------------------------- API


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that's in use,
    # silently splitting requests between the two; refuse instead.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def _sources_overview() -> list[dict]:
    counts: dict[str, dict] = {}
    for r in db.conn().execute(
        "SELECT source, status, COUNT(*) n, "
        "SUM(wanted=1 AND status='matched' AND downloaded IS NULL) queued, "
        "SUM(downloaded IS NOT NULL) done FROM releases GROUP BY source, status"
    ):
        c = counts.setdefault(r["source"], {"total": 0, "queued": 0, "downloaded": 0})
        c[r["status"]] = r["n"]
        c["total"] += r["n"]
        c["queued"] += r["queued"] or 0
        c["downloaded"] += r["done"] or 0
    out = []
    for k, src in db.sources().items():
        out.append(
            {
                "key": k,
                "name": src.get("name") or k,
                "kind": src.get("kind"),
                "folder": src.get("folder"),
                "backend": src.get("backend"),
                "artist_id": str(src["artist_id"]) if src.get("artist_id") else None,
                "follow": src.get("follow"),
                "last_scan": src.get("last_scan"),
                "counts": counts.get(k, {"total": 0, "queued": 0, "downloaded": 0}),
            }
        )
    return out


def _releases(source: str) -> list[dict]:
    counts = db.track_counts(source)
    other = library.elsewhere(source)
    out = []
    for r in db.release_rows(source):
        e = json.loads(r["entry"])
        c = counts.get((r["backend"], r["match_id"])) or {}
        out.append(
            {
                "rkey": r["rkey"],
                "status": r["status"],
                "wanted": bool(r["wanted"]),
                "downloaded": r["downloaded"],
                "artist": e.get("artist"),
                "title": e.get("title"),
                "type": e.get("type"),
                "tracks": e.get("tracks"),
                "audio": e.get("audio"),
                "source_tag": e.get("source_tag"),
                "origin": e.get("origin"),
                "present": c.get("present"),
                "needed": (c["total"] - (c["unavailable"] or 0)) if c else None,
                "date": e.get("date") or e.get("released"),
                "failed": (e.get("failed") or {}).get("error"),
                "short": e.get("short") or None,  # the label's tracks the service's version lacks
                "note": e.get("note"),
                "url": e.get("source_url"),
                "elsewhere": other.get(r["rkey"]),  # [source name, downloaded | yours | queued | listed, key, rkey]
            }
        )
    return out


def _tracks(source: str, rkey: str, fetch: bool) -> dict:
    """An album's songs and whether each is on disk. Built on first request
    (fetch=1) from the service's song list and the files in its folder."""
    row = next((r for r in db.release_rows(source) if r["rkey"] == rkey), None)
    if row is None or row["status"] != "matched":
        return {"tracks": [], "note": "not on the service" if row else "unknown release"}
    rows = db.album_tracks(source, row["backend"], row["match_id"])
    if not rows and fetch:
        e = json.loads(row["entry"])
        b = library.backend(row["backend"])
        folder = album_folder(library.label_dir(source), e)
        songs, _ = inventory(b.album_tracks(row["match_id"]), folder, e.get("unavailable"))
        db.set_tracks(source, row["backend"], row["match_id"], songs)
        rows = db.album_tracks(source, row["backend"], row["match_id"])
    e = json.loads(row["entry"])
    return {"tracks": rows, "folder": e.get("folder"), "short": e.get("short") or []}


def _handler(d: Daemon):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if db.PATH is None and u.path not in ("/status", "/setup", "/settings"):
                    return self._send({"error": "setup needed: no music.db yet"}, 503)
                if u.path == "/status" and db.PATH is None:
                    s = library.settings()
                    self._send(
                        {"needs_setup": True, "current": "waiting for setup", "heartbeat": None, "paused": False, "queue": 0,
                         "progress": None, "archive": s["archive"], "backend": s["backend"]}  # fmt: skip
                    )
                elif u.path == "/status":
                    s = library.settings()
                    self._send(
                        {
                            "current": db.get_state("current"),
                            "heartbeat": db.get_state("heartbeat"),
                            "paused": bool(s["paused"]),
                            "queue": db.queue_count(),
                            "progress": db.get_state("progress"),
                            "archive": s["archive"],
                            "backend": s["backend"],
                            "skip_atmos": bool(s["skip_atmos"]),
                            "source_tag": s.get("source_tag") or "",
                        }
                    )
                elif u.path == "/sources":
                    self._send(_sources_overview())
                elif u.path == "/releases":
                    self._send(_releases(q["source"]))
                elif u.path == "/queue":
                    names = {k: src.get("name") or k for k, src in db.sources().items()}
                    queued = []
                    for r in db.queue():
                        e = json.loads(r["entry"])
                        queued.append(
                            {
                                "source": r["source"],
                                "source_name": names.get(r["source"], r["source"]),
                                "rkey": r["rkey"],
                                "artist": e.get("artist"),
                                "title": e.get("title"),
                                "tracks": e.get("tracks"),
                                "failed": (e.get("failed") or {}).get("error"),
                            }
                        )
                    recent = [ev for ev in db.events(0, 400) if ev["level"] in ("done", "error")][-30:]
                    self._send({"current": db.get_state("progress"), "queued": queued, "recent": recent})
                elif u.path == "/setup":
                    arch, artists, dbp = library.archive(), library.artists_dir(), library.db_path()
                    self._send(
                        {
                            "archive": library.show(arch),
                            "archive_exists": arch.is_dir(),
                            "artists": library.show(artists) if artists else None,
                            "artists_exists": bool(artists and artists.is_dir()),
                            "database": library.show(dbp),
                            "database_exists": dbp.exists(),
                        }
                    )
                elif u.path == "/settings":
                    cur = library.settings()
                    self._send(
                        {
                            "settings": [{"key": k, "kind": kind, "help": help_, "value": cur.get(k)} for k, kind, help_ in library.SETTINGS],
                            "naming_fields": library.NAMING_FIELDS,
                        }
                    )
                elif u.path == "/tracks":
                    self._send(_tracks(q["source"], q["rkey"], fetch=q.get("fetch") == "1"))
                elif u.path == "/search_artists":
                    b = library.backend()
                    self._send([{"id": a.id, "name": a.name, "url": a.url, "hint": a.hint, "backend": b.label} for a in b.find_artists(q["q"], limit=15)])
                elif u.path == "/events":
                    self._send(db.events(int(q.get("after", 0))))
                else:
                    self._send({"error": "not found"}, 404)
            except Exception as ex:
                self._send({"error": str(ex)}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                if db.PATH is None and u.path not in ("/setup", "/shutdown") and not (u.path == "/settings" and body.get("key") == "archive"):
                    return self._send({"error": "setup needed: no music.db yet"}, 503)
                if u.path == "/want":
                    db.set_wanted(body["source"], body["rkeys"], body["wanted"])
                elif u.path == "/want_all":
                    rows = db.release_rows(body["source"])
                    if body["wanted"]:
                        keys = [r["rkey"] for r in rows if r["status"] == "matched"]
                    else:
                        keys = [r["rkey"] for r in rows if r["downloaded"] is None]
                    db.set_wanted(body["source"], keys, body["wanted"])
                elif u.path == "/follow":
                    db.set_follow(body["source"], body["follow"])
                    if body["follow"] and not db.sources()[body["source"]].get("last_scan"):
                        db.set_last_scan(body["source"])  # today's list is the baseline
                elif u.path == "/scan":
                    d.jobs.put(("scan", body["source"]))
                elif u.path == "/verify":
                    d.jobs.put(("verify", body["source"], body.get("rkey")))
                elif u.path == "/add":
                    d.jobs.put(("add", body["url"], bool(body.get("label")), bool(body.get("follow"))))
                elif u.path == "/settings":
                    try:
                        value = library.set_setting(body["key"], body["value"])
                    except (ValueError, TypeError) as ex:
                        return self._send({"error": str(ex)}, 400)
                    d.log(f"setting {body['key']} = {value!r}", "info")
                elif u.path == "/setup":
                    what = body.get("create")
                    if what == "database":
                        if not library.archive().is_dir():
                            return self._send({"error": "create the music folder first"}, 400)
                        if not library.db_path().exists():
                            library.init(create=True)  # a new, empty library: starting from zero
                            d.log(f"created {library.show(library.db_path())}", "info")
                    else:
                        target = {"archive": library.archive(), "artists": library.artists_dir()}.get(what)
                        if target is None:
                            return self._send({"error": "create: archive, artists or database"}, 400)
                        target.mkdir(parents=True, exist_ok=True)
                        d.log(f"created {library.show(target)}", "info")
                    d.reconcile_now = True
                elif u.path == "/pause":
                    db.set_setting("paused", bool(body["paused"]))
                elif u.path == "/shutdown":
                    d.stop.set()
                else:
                    return self._send({"error": "not found"}, 404)
                d.wake.set()
                self._send({"ok": True})
            except Exception as ex:
                self._send({"error": str(ex)}, 400)

    return Handler


def serve(port: int | None = None) -> None:
    d = Daemon(port or int(library.settings()["port"]))
    d.run()

"""The library database: one SQLite file holding everything tidal-daemon knows.

    settings   archive path, quality, backend, daemon options
    sources    labels and artists (their scraper config, follow flag)
    releases   every release of every source: the decisions made about it
               (entry, as JSON), plus what the queue needs as columns
    tracks     every song of every matched album, and whether its file is
               on disk (present), not (missing) or never will be (unavailable)
    ignored    folders `scan` was told to leave alone
    events     what happened: new releases, downloads, failures (with tracebacks)
    state      the daemon's heartbeat and current job

Safe to use from several processes at once (CLI, daemon): WAL mode, and
every writer updates single rows rather than rewriting whole tables.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sources (
    key       TEXT PRIMARY KEY,
    config    TEXT NOT NULL,            -- JSON: name, kind, folder, site, artist_id, ...
    follow    INTEGER NOT NULL DEFAULT 0,
    last_scan TEXT,
    added     TEXT
);
CREATE TABLE IF NOT EXISTS releases (
    source     TEXT NOT NULL,
    rkey       TEXT NOT NULL,           -- stable identity across re-scrapes
    pos        INTEGER,                 -- order on the source's page
    entry      TEXT NOT NULL,           -- JSON: release fields + decisions
    status     TEXT,                    -- matched | owned | missing | review
    backend    TEXT,
    match_id   TEXT,
    wanted     INTEGER NOT NULL DEFAULT 0,   -- ticked for download
    downloaded TEXT,                    -- when it was last completed
    first_seen TEXT,
    PRIMARY KEY (source, rkey)
);
CREATE INDEX IF NOT EXISTS releases_queue ON releases (wanted, downloaded);
CREATE TABLE IF NOT EXISTS tracks (
    source   TEXT NOT NULL,
    backend  TEXT NOT NULL,
    album_id TEXT NOT NULL,
    disc     INTEGER NOT NULL,
    num      INTEGER NOT NULL,
    title    TEXT,
    artist   TEXT,
    duration INTEGER,
    isrc     TEXT,
    track_id TEXT,
    file     TEXT,                      -- relative to the album folder
    state    TEXT,                      -- present | missing | unavailable
    checked  TEXT,
    PRIMARY KEY (source, backend, album_id, disc, num)
);
CREATE TABLE IF NOT EXISTS ignored (source TEXT, folder TEXT, PRIMARY KEY (source, folder));
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    source  TEXT,
    level   TEXT,                       -- info | new | done | warn | error
    message TEXT,
    detail  TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""

PATH: Path | None = None
_local = threading.local()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def open_db(path: Path) -> None:
    global PATH
    PATH = path
    _local.__dict__.clear()
    c = conn()
    # WAL is stored in the file: set once here. Setting it on every new
    # connection takes a lock, so API threads would wait behind long writes.
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)


def adopt_backends(known: list[str], default: str) -> None:
    """Move everything stored under a backend name that's no longer
    registered (it was renamed) to the default backend."""
    c = conn()
    q = ",".join("?" * len(known))
    stale = c.execute(
        f"SELECT 1 FROM releases WHERE backend NOT IN ({q}) OR json_extract(entry, '$.backend') NOT IN ({q}) UNION ALL SELECT 1 FROM tracks WHERE backend NOT IN ({q}) "
        f"UNION ALL SELECT 1 FROM sources WHERE json_extract(config, '$.backend') NOT IN ({q}) "
        f"UNION ALL SELECT 1 FROM settings WHERE key='backend' AND json_extract(value, '$') NOT IN ({q}) LIMIT 1",
        known * 5,
    ).fetchone()
    if not stale:  # the usual case: no write lock taken
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute(f"UPDATE settings SET value=? WHERE key='backend' AND json_extract(value, '$') NOT IN ({q})", (json.dumps(default), *known))
        c.execute(f"UPDATE sources SET config=json_set(config, '$.backend', ?) WHERE json_extract(config, '$.backend') NOT IN ({q})", (default, *known))
        c.execute(f"UPDATE releases SET backend=? WHERE backend NOT IN ({q})", (default, *known))
        c.execute(f"UPDATE releases SET entry=json_set(entry, '$.backend', ?) WHERE json_extract(entry, '$.backend') NOT IN ({q})", (default, *known))
        c.execute(f"UPDATE OR REPLACE tracks SET backend=? WHERE backend NOT IN ({q})", (default, *known))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def conn() -> sqlite3.Connection:
    """One connection per thread (sqlite3 connections aren't shareable)."""
    c = getattr(_local, "c", None)
    if c is None:
        if PATH is None:
            raise RuntimeError("database not opened")
        c = sqlite3.connect(PATH, timeout=30, isolation_level=None)  # autocommit
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        _local.c = c
    return c


# --------------------------------------------------------------------------- settings / state


def setting(key: str, default=None):
    r = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value) -> None:
    conn().execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))


def settings() -> dict:
    return {r["key"]: json.loads(r["value"]) for r in conn().execute("SELECT * FROM settings")}


def get_state(key: str, default=None):
    r = conn().execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_state(key: str, value) -> None:
    conn().execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))


# --------------------------------------------------------------------------- sources


def sources() -> dict[str, dict]:
    """key -> config, with `follow` and `last_scan` folded in."""
    out = {}
    for r in conn().execute("SELECT * FROM sources ORDER BY key"):
        cfg = json.loads(r["config"])
        cfg["follow"] = bool(r["follow"])
        cfg["last_scan"] = r["last_scan"]
        out[r["key"]] = cfg
    return out


def put_source(key: str, cfg: dict) -> None:
    cfg = {k: v for k, v in cfg.items() if k not in ("follow", "last_scan")}
    conn().execute(
        "INSERT INTO sources (key, config, added) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET config=excluded.config",
        (key, json.dumps(cfg, ensure_ascii=False), now()),
    )


def set_follow(key: str, follow: bool) -> None:
    conn().execute("UPDATE sources SET follow=? WHERE key=?", (int(follow), key))


def set_last_scan(key: str, when: str | None = None) -> None:
    conn().execute("UPDATE sources SET last_scan=? WHERE key=?", (when or now(), key))


def delete_source(key: str) -> None:
    c = conn()
    c.execute("DELETE FROM releases WHERE source=?", (key,))
    c.execute("DELETE FROM ignored WHERE source=?", (key,))
    c.execute("DELETE FROM sources WHERE key=?", (key,))


# --------------------------------------------------------------------------- releases


def release_rows(source: str) -> list[sqlite3.Row]:
    return conn().execute("SELECT * FROM releases WHERE source=? ORDER BY pos", (source,)).fetchall()


def has_releases(source: str) -> bool:
    return conn().execute("SELECT 1 FROM releases WHERE source=? LIMIT 1", (source,)).fetchone() is not None


def _cols(entry: dict, status: str) -> tuple:
    mid = entry.get("match_id")
    return (
        json.dumps(entry, ensure_ascii=False),
        status,
        entry.get("backend") if mid else None,
        str(mid) if mid else None,
    )


def replace_releases(source: str, items: list[tuple[str, dict, str]]) -> set[str]:
    """Make the source's releases exactly `items` [(rkey, entry, status)],
    keeping each surviving row's wanted/downloaded/first_seen. Returns the
    rkeys that are new."""
    c = conn()
    old = {r["rkey"] for r in c.execute("SELECT rkey FROM releases WHERE source=?", (source,))}
    keep = set()
    c.execute("BEGIN IMMEDIATE")
    try:
        for pos, (rkey, entry, status) in enumerate(items):
            keep.add(rkey)
            c.execute(
                "INSERT INTO releases (source, rkey, pos, entry, status, backend, match_id, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source, rkey) DO UPDATE SET "
                "pos=excluded.pos, entry=excluded.entry, status=excluded.status, "
                "backend=excluded.backend, match_id=excluded.match_id",
                (source, rkey, pos, *_cols(entry, status), now()),
            )
        for rkey in old - keep:
            c.execute("DELETE FROM releases WHERE source=? AND rkey=?", (source, rkey))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return keep - old


def update_release(source: str, rkey: str, entry: dict, status: str) -> None:
    conn().execute(
        "UPDATE releases SET entry=?, status=?, backend=?, match_id=? WHERE source=? AND rkey=?",
        (*_cols(entry, status), source, rkey),
    )


def set_wanted(source: str, rkeys: list[str], wanted: bool) -> None:
    c = conn()
    c.executemany(
        "UPDATE releases SET wanted=? WHERE source=? AND rkey=?", [(int(wanted), source, k) for k in rkeys]
    )


def set_downloaded(source: str, rkeys: list[str], when: str | None) -> None:
    conn().executemany(
        "UPDATE releases SET downloaded=? WHERE source=? AND rkey=?", [(when, source, k) for k in rkeys]
    )


def queue_count() -> int:
    return conn().execute(
        "SELECT COUNT(*) FROM releases WHERE wanted=1 AND status='matched' AND downloaded IS NULL"
    ).fetchone()[0]


def queue() -> list[sqlite3.Row]:
    """Ticked, matched, not yet downloaded, oldest-ticked source first."""
    return conn().execute(
        "SELECT * FROM releases WHERE wanted=1 AND status='matched' AND downloaded IS NULL ORDER BY source, pos"
    ).fetchall()


# --------------------------------------------------------------------------- tracks


def set_tracks(source: str, backend: str, album_id: str, rows: list[dict]) -> None:
    """Replace an album's song list (as `local.inventory` builds it)."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("DELETE FROM tracks WHERE source=? AND backend=? AND album_id=?", (source, backend, album_id))
        c.executemany(
            "INSERT OR REPLACE INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (source, backend, album_id, r["disc"], r["num"], r["title"], r["artist"], r["duration"], r["isrc"], r["track_id"], r["file"], r["state"], now())
                for r in rows
            ],
        )
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def album_tracks(source: str, backend: str, album_id: str) -> list[dict]:
    return [
        dict(r)
        for r in conn().execute(
            "SELECT * FROM tracks WHERE source=? AND backend=? AND album_id=? ORDER BY disc, num", (source, backend, album_id)
        )
    ]


def track_counts(source: str) -> dict[tuple[str, str], dict]:
    """(backend, album_id) -> {present, missing, unavailable, total} for a source's albums."""
    out = {}
    for r in conn().execute(
        "SELECT backend, album_id, SUM(state='present') present, SUM(state='missing') missing, "
        "SUM(state='unavailable') unavailable, COUNT(*) total FROM tracks WHERE source=? GROUP BY backend, album_id",
        (source,),
    ):
        out[(r["backend"], r["album_id"])] = {k: r[k] for k in ("present", "missing", "unavailable", "total")}
    return out


# --------------------------------------------------------------------------- ignored / events


def ignored(source: str) -> set[str]:
    return {r["folder"] for r in conn().execute("SELECT folder FROM ignored WHERE source=?", (source,))}


def ignore(source: str, folder: str) -> None:
    conn().execute("INSERT OR IGNORE INTO ignored VALUES (?, ?)", (source, folder))


def log(source: str | None, level: str, message: str, detail: str | None = None) -> None:
    conn().execute(
        "INSERT INTO events (ts, source, level, message, detail) VALUES (?, ?, ?, ?, ?)",
        (now(), source, level, message, detail),
    )


def events(after: int = 0, limit: int = 200) -> list[dict]:
    rows = conn().execute(
        "SELECT id, ts, source, level, message FROM events WHERE id>? ORDER BY id DESC LIMIT ?", (after, limit)
    ).fetchall()
    return [dict(r) for r in reversed(rows)]

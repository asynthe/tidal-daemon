"""The library: settings, sources (labels/artists) and their releases.

The database is .tidal-daemon/music.db inside the music folder (the
archive), so the library's record lives with the music. Where that folder is comes from a
small config file in the user's profile (CONFIG_FILE); everything else
(the database, locks, logs, the backend's login) is in .tidal-daemon.

Releases live in the database (see db.py). In memory a release is a flat
dict ("entry"): its scraped fields plus the decisions made about it. The
database is the only record: folders hold music and nothing else.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import db
from .backends import BACKENDS, Backend, get_backend

ROOT = Path(__file__).resolve().parents[2]  # the project folder (src/tidal_daemon/ -> .)
WINDOWS = os.name == "nt"
DB_NAME = "music.db"
DATA_DIR = ".tidal-daemon"  # inside the music folder: the database, locks, logs, the backend's login
# Where the database lived before it moved into the music folder; only its
# music folder setting is read, once, to know where to look.
OLD_DB = Path(os.environ.get("TIDAL_DAEMON_OLD_DB") or ROOT / "library.db")
CONFIG_FILE = Path(
    os.environ.get("TIDAL_DAEMON_CONFIG")
    or (
        Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        if WINDOWS
        else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    )
    / "tidal-daemon"
    / "config.json"
)


class NoDatabase(Exception):
    """music.db doesn't exist yet: `tidal-daemon tui` (or `setup`) creates it."""

DEFAULTS = {
    "archive": "~/archive/music",
    "label_folder": "({name})",  # under archive; {name} = the label's name
    "artist_folder": "ARTISTS",  # under archive: all artists' albums together ("ARTISTS/{name}": a folder each)
    "album_folder": "{album_artist} - {album}{explicit}",  # inside a label/artist folder
    "track_file": "{disc}{track} - {artist} - {title}",  # inside the album folder; extension added (see naming.py)
    "quality": "HI_RES_LOSSLESS",  # LOW, HIGH (AAC), LOSSLESS (16-bit FLAC), HI_RES_LOSSLESS
    "backend": "tidaler",  # used for matching and downloads unless a release says otherwise
    "port": 8737,  # the daemon's local API
    "follow_hours": 24,  # how often followed sources are re-checked
    "auto_match": 0.97,  # the daemon accepts matches at or above this score unasked
    "pause": 10,  # seconds between albums
    "retries": 3,
    "retry_wait": 10,
    "paused": False,  # downloads paused (the daemon still scans)
    "skip_atmos": True,  # Dolby Atmos mixes: hidden, never downloaded
    "source_tag": "tidaler",  # written to a SOURCE file in each album folder we download ("" = off)
}


def say(msg: str) -> None:
    """Progress messages. The daemon swaps this for its event log."""
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- settings & sources

SOURCES: dict[str, dict] = {}


def config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(**values) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({**config(), **values}, indent=2), encoding="utf-8")


def archive() -> Path:
    """The music folder. From $TIDAL_DAEMON_ARCHIVE, else the config file;
    the first time, the old library.db's archive setting is carried over."""
    a = os.environ.get("TIDAL_DAEMON_ARCHIVE") or config().get("archive")
    if not a:
        a = _old_setting("archive") or DEFAULTS["archive"]
        save_config(archive=a)
    return Path(a).expanduser()


def db_path() -> Path:
    """.tidal-daemon/music.db in the music folder ($TIDAL_DAEMON_DB
    overrides, for tests). A music.db still at the top of the music folder
    is moved in, once nothing has it open."""
    if os.environ.get("TIDAL_DAEMON_DB"):
        return Path(os.environ["TIDAL_DAEMON_DB"])
    path, old = archive() / DATA_DIR / DB_NAME, archive() / DB_NAME
    if not path.exists() and old.exists() and not _move_db(old, path):
        return old  # in use (an older daemon still running): keep it there for now
    return path


def data_dir() -> Path:
    """.tidal-daemon in the music folder (next to music.db): everything
    tidal-daemon keeps besides config.json and tui.json lives here."""
    return db_path().parent


def _move_db(old: Path, new: Path) -> bool:
    """Move a database file, but only while no other process has it open:
    leaving WAL mode needs every other connection gone (open_db turns it
    back on), and afterwards it's the one file to move."""
    import sqlite3

    try:
        c = sqlite3.connect(old, timeout=0)
        try:
            mode = c.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        finally:
            c.close()
        if str(mode).lower() != "delete":
            return False
        new.parent.mkdir(exist_ok=True)
        old.replace(new)
        return True
    except (sqlite3.Error, OSError):
        return False


def show(path: Path) -> str:
    """A path the way this system writes it: C:\\Users\\... on Windows, /home/... elsewhere."""
    return str(path)


def init(create: bool = False) -> dict[str, dict]:
    """Open music.db and load the sources. A music folder that has one keeps
    using it; otherwise NoDatabase, or with create=True a new, empty library
    (the music folder must already exist)."""
    path = db_path()
    if not path.exists():
        if not create:
            raise NoDatabase(path)
        if path.parent.name == DATA_DIR and path.parent.parent.is_dir():
            path.parent.mkdir(exist_ok=True)
        if not path.parent.is_dir():
            raise NoDatabase(path)
    db.open_db(path)
    db.adopt_backends(list(BACKENDS), DEFAULTS["backend"])
    return load_sources()


def _old_setting(key: str):
    if not OLD_DB.exists():
        return None
    import sqlite3

    try:
        c = sqlite3.connect(OLD_DB)
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        c.close()
        return json.loads(r[0]) if r else None
    except sqlite3.Error:
        return None


def load_sources() -> dict[str, dict]:
    SOURCES.clear()
    SOURCES.update(db.sources())
    return SOURCES


def settings() -> dict:
    stored = db.settings() if db.PATH is not None else {}
    return {**DEFAULTS, **stored, "archive": str(archive())}


# Placeholders for album_folder / track_file. Backends translate them to their downloader's own.
NAMING_FIELDS = {
    "album_artist": "the album's main artist",
    "album": "album title",
    "year": "release year",
    "date": "release date",
    "explicit": '" (Explicit)" for explicit albums, else nothing',
    "disc": '"2-" on multi-disc albums, else nothing',
    "track": "track number, zero-padded (01)",
    "artist": "the track's artists",
    "title": "track title",
    "isrc": "the track's ISRC",
}

# What the Settings screen shows and accepts. kind: path | pattern | text | choice | int | float | bool
SETTINGS = [
    ("archive", "path", "The music folder: all music, and the library (.tidal-daemon/music.db) inside it. Takes effect when the daemon restarts; nothing is moved."),
    ("label_folder", "pattern", "Folder for each label, under the archive. {name} = label name. Applies to labels without a folder of their own."),
    ("artist_folder", "pattern", "Folder artists' albums go in, under the archive (\"ARTISTS/{name}\": a folder per artist)."),
    ("album_folder", "pattern", "One folder per album, inside the label/artist folder. Placeholders: " + ", ".join("{%s}" % k for k in NAMING_FIELDS)),
    ("track_file", "pattern", "File name of each song inside its album folder (the extension is added)."),
    ("quality", "choice:HI_RES_LOSSLESS,LOSSLESS", "Download quality, FLAC only: HI_RES_LOSSLESS = up to 24-bit/192kHz (the best each album has); LOSSLESS = 16-bit CD quality."),
    ("follow_hours", "float", "How often followed labels/artists are checked for new releases, in hours."),
    ("auto_match", "float", "Score (0-1) at which the daemon accepts a label release's match without asking."),
    ("pause", "float", "Seconds to wait between albums."),
    ("retries", "int", "Retries per album before it's marked failed."),
    ("retry_wait", "float", "Seconds before each retry."),
    ("source_tag", "text", "Written to a SOURCE file in every album folder tidal-daemon downloads (one line, as in bandcamp.com / monochrome.tf). An existing SOURCE is never overwritten; albums you added yourself never get one. Empty: off."),
    ("skip_atmos", "bool", "Skip Dolby Atmos mixes. The catalogue lists them as separate releases with the same title as the stereo album; they come as Dolby audio, not FLAC. On: hidden in the Library and never downloaded. Off: shown under Dolby Atmos and downloadable."),
    ("paused", "bool", "Downloads paused (checking for new releases continues)."),
]


def set_setting(key: str, raw) -> object:
    """Validate and store a setting from the Settings screen; returns the stored value."""
    spec = {k: kind for k, kind, _ in SETTINGS}
    if key not in spec:
        raise ValueError(f"unknown setting {key!r}")
    kind = spec[key]
    if kind == "text":  # free text; empty allowed (turns the feature off)
        value = str(raw).strip()
        db.set_setting(key, value)
        return value
    if key == "archive":  # lives in the config file: the database is found through it
        value = str(raw).strip()
        if not value:
            raise ValueError("archive can't be empty")
        save_config(archive=value)
        return value
    if kind == "bool":
        value = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    elif kind == "int":
        value = int(raw)
    elif kind == "float":
        value = float(raw)
    elif kind.startswith("choice:"):
        value = str(raw).strip()
        if value not in kind[7:].split(","):
            raise ValueError(f"{key}: one of {kind[7:]}")
    else:
        value = str(raw).strip()
        if not value:
            raise ValueError(f"{key} can't be empty")
        if kind == "pattern":
            import string

            fields = {f for _, f, _, _ in string.Formatter().parse(value) if f}
            allowed = {"name"} if key in ("label_folder", "artist_folder") else set(NAMING_FIELDS)
            bad = fields - allowed
            if bad:
                raise ValueError(f"{key}: unknown placeholder {', '.join('{%s}' % b for b in sorted(bad))} — use {', '.join('{%s}' % a for a in sorted(allowed))}")
    db.set_setting(key, value)
    return value


def backend(name: str | None = None) -> Backend:
    s = settings()
    b = get_backend(name or s["backend"], s)
    b.settings = s  # backends live for the whole process; keep them on current settings
    return b


def find_artist_source(backend_name: str, artist_id) -> str | None:
    """The key of the source that already follows this backend artist, if any."""
    for k, src in SOURCES.items():
        if src.get("kind") == "artist" and src.get("backend") == backend_name and str(src.get("artist_id")) == str(artist_id):
            return k
    return None


def get_source(name: str) -> dict:
    if name not in SOURCES:
        sys.exit(f"no label or artist named {name!r}. Known: {', '.join(sorted(SOURCES))}")
    return SOURCES[name]


def display_name(name: str) -> str:
    return SOURCES.get(name, {}).get("name") or name


def label_dir(name: str) -> Path:
    """<archive>/(<name>)/ holds a source's albums.
    The brackets sort label folders above others; `folder` overrides the
    whole path (e.g. "OST", "ARTISTS/HKE")."""
    src = SOURCES.get(name, {})
    if src.get("folder"):
        return archive() / src["folder"]
    from .text import safe_folder

    return archive() / settings()["label_folder"].format(name=safe_folder(display_name(name)))


def artists_dir() -> Path | None:
    """The common folder artists go in ("ARTISTS" in "ARTISTS/{name}"), if any."""
    head = settings()["artist_folder"].replace("\\", "/").split("/")[0]
    return None if "{" in head or not head else archive() / head


def folder_artists(folder_name: str) -> list[str]:
    """The artists an album folder is named after: "A; B - Title" -> [A, B]."""
    if " - " not in folder_name:
        return []
    return [a.strip() for a in folder_name.split(" - ", 1)[0].split(";") if a.strip()]


def is_shared(name: str) -> bool:
    """True when the source's albums sit in the common ARTISTS folder, next to
    other artists' albums (rather than in a folder of their own)."""
    arts = artists_dir()
    return arts is not None and label_dir(name).resolve() == arts.resolve()


def folder_owners() -> dict[str, str]:
    """For the common ARTISTS folder: album folder name -> the source it
    belongs to. A folder a release records belongs to that source; otherwise
    to the artist it's named after ("Lone - Galaxy Garden" -> Lone). Artists
    followed on a service come before folders only listed as yours."""
    from .text import norm

    arts = artists_dir()
    if arts is None or not arts.is_dir():
        return {}
    shared = [k for k in SOURCES if is_shared(k)]
    service = [k for k in shared if SOURCES[k].get("kind") != "local"]
    local = [k for k in shared if SOURCES[k].get("kind") == "local"]
    recorded: dict[str, dict[str, str]] = {"service": {}, "local": {}}
    if shared:
        marks = ",".join("?" * len(shared))
        rows = db.conn().execute(
            f"SELECT source, json_extract(entry, '$.folder') FROM releases WHERE source IN ({marks}) "
            "AND json_extract(entry, '$.folder') IS NOT NULL", shared).fetchall()
        for src, folder in rows:
            recorded["local" if src in local else "service"].setdefault(folder, src)

    def by_name(keys):
        names = {}
        for k in keys:
            for n in (SOURCES[k].get("artist"), SOURCES[k].get("name")):
                for part in [n, *(n or "").split(";")]:
                    if part and part.strip():
                        names.setdefault(norm(part), k)
        return names

    names = {"service": by_name(service), "local": by_name(local)}
    owners = {}
    for d in arts.iterdir():
        if not d.is_dir():
            continue
        artists = [norm(a) for a in folder_artists(d.name)]
        for kind in ("service", "local"):
            k = recorded[kind].get(d.name) or next((names[kind][a] for a in artists if a in names[kind]), None)
            if k:
                owners[d.name] = k
                break
    return owners


def is_label(name: str) -> bool:
    """A label you collect (its own folder, e.g. "(VISION)"), not an artist
    or a folder of your own albums."""
    src = SOURCES.get(name, {})
    arts = artists_dir()
    d = label_dir(name).resolve()
    return src.get("kind") != "local" and not (arts is not None and (d == arts.resolve() or arts.resolve() in d.parents))


def elsewhere(name: str) -> dict[str, tuple]:
    """Releases of this source that another source has too (e.g. an artist's
    album that came out on a label you collect): rkey -> (that source's
    name, state, its key, its rkey). State: "downloaded", "yours", "queued",
    or "listed" (a label's release you don't have yet: downloads go there).
    Same album id on the same service, else the same artist and title."""
    from .text import norm

    rows = db.conn().execute(
        "SELECT source, rkey, backend, match_id, status, wanted, downloaded, json_extract(entry, '$.artist'), json_extract(entry, '$.title') "
        "FROM releases WHERE source != ? AND (downloaded IS NOT NULL OR wanted = 1 OR status IN ('owned', 'matched'))", (name,)).fetchall()
    labels = {k for k in SOURCES if is_label(k)}
    by_id, by_title = {}, {}
    for src, rkey, backend, mid, st, wanted, downloaded, artist, title in rows:
        if downloaded:
            state = "downloaded"
        elif st == "owned":
            state = "yours"
        elif wanted:
            state = "queued"
        elif src in labels and st == "matched":
            state = "listed"
        else:
            continue
        rank = ["downloaded", "yours", "queued", "listed"].index(state)
        hit = (display_name(src), state, src, rkey, rank)
        for d, k in ((by_id, (backend, mid) if mid else None), (by_title, (norm(artist or ""), norm(title or "")))):
            if k and (k not in d or rank < d[k][4]):
                d[k] = hit
    out = {}
    for r in db.release_rows(name):
        e = json.loads(r["entry"])
        hit = by_id.get((r["backend"], r["match_id"])) if r["match_id"] else None
        hit = hit or by_title.get((norm(e.get("artist") or ""), norm(e.get("title") or "")))
        if hit and not (hit[1] == "listed" and is_label(name)):  # only artists hand downloads to a label
            out[r["rkey"]] = hit[:4]
    return out


def album_dirs(name: str) -> list[Path]:
    """The album folders of one source (in a shared ARTISTS folder: only its own)."""
    d = label_dir(name)
    if not d.is_dir():
        return []
    dirs = sorted(p for p in d.iterdir() if p.is_dir())
    if is_shared(name):
        owners = folder_owners()
        dirs = [p for p in dirs if owners.get(p.name) == name]
    return dirs


def releases_url(src: dict) -> str | None:
    """The page the release list came from, for finding a release by hand."""
    kind = src.get("kind")
    if kind == "bandcamp":
        return src["site"].rstrip("/") + "/music"
    if kind == "shopify":
        return src["site"].rstrip("/") + "/releases"
    if kind == "artist" and src.get("artist_id"):
        return backend(src.get("backend")).artist_url(src["artist_id"])
    if kind == "file":
        return src.get("path")
    return None


# --------------------------------------------------------------------------- locks

_LOCKS: dict[str, object] = {}


def lock_label(name: str, wait: bool = False) -> bool:
    """One writer per source at a time; long jobs (check, dl) work from a
    copy loaded at start. The OS drops the lock when the process ends, even
    on a crash. Returns False (or exits, for the CLI) when it's taken."""
    if name in _LOCKS:
        return True
    locks = data_dir() / "locks"  # per library: two libraries never block each other
    locks.mkdir(parents=True, exist_ok=True)
    f = open(locks / f"{name}.lock", "a+")
    try:
        if WINDOWS:
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        if wait:
            return False
        sys.exit(f"{name} is busy: another tidal-daemon job (or the daemon) is working on it. Try again when it's done.")
    _LOCKS[name] = f
    return True


def unlock_label(name: str) -> None:
    f = _LOCKS.pop(name, None)
    if f is None:
        return
    try:
        if WINDOWS:
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


# --------------------------------------------------------------------------- entries

RELEASE_FIELDS = ("artist", "title", "type", "date", "source_url")
MATCH_FIELDS = (
    "backend", "match_id", "confirmed", "skipped", "note", "score", "guess", "guess_id",
    "upc", "tracks", "copyright", "released",
    "audio",  # atmos | hires | lossless | lossy, as the backend labels the matched album
    "folder",  # album folder the download went into, under the source's folder
    "failed",  # last download couldn't finish it: {when, error, missing}; cleared on success
    "unavailable",  # tracks the service lists but won't deliver; not counted as missing
    "short", "short_for",  # the source's tracks the matched album lacks; match_id they were worked out for
)  # fmt: skip
# Decisions that aren't about a backend match: owned (you have it), bandcamp (price check),
# origin ("local": files you brought in yourself, not downloaded by tidal-daemon),
# source_tag (what the album folder's SOURCE file says, e.g. bandcamp.com).
OWN_FIELDS = ("owned", "bandcamp", "origin", "source_tag")
# Facts read from the source's own release page: tracklist (its track titles).
SOURCE_FIELDS = ("tracklist",)
# Everything a re-scrape must carry across.
DECISION_FIELDS = MATCH_FIELDS + OWN_FIELDS + SOURCE_FIELDS


def entry(artist, title, kind="", date=None, source_url="", **match) -> dict:
    return {"artist": artist, "title": title, "type": kind, "date": date, "source_url": source_url, **match}


def key(e: dict) -> str:
    """Stable identity for a release across re-scrapes."""
    return e.get("source_url") or f"{e['artist']}|{e['title']}".casefold()


def status(e: dict) -> str:
    """owned: you have it, never downloaded. matched: found on a backend,
    downloadable. missing: not on the backend. review: still undecided."""
    if e.get("owned"):
        return "owned"
    if e.get("match_id"):
        return "matched"
    if e.get("skipped"):
        return "missing"
    return "review"


def status_label(st: str) -> str:
    """How a status reads in summaries."""
    b = backend().label
    return {"matched": f"{b} download", "owned": "owned", "missing": f"not on {b}", "review": "to review"}[st]


def picked(e: dict, only) -> bool:
    """True when no --only filter is set, or any filter appears in the
    release's artist or title (case-insensitive)."""
    if not only:
        return True
    hay = (e["artist"].casefold(), e["title"].casefold())
    return any(o.casefold() in h for o in only for h in hay)


def load_entries(name: str) -> list[dict]:
    return [json.loads(r["entry"]) for r in db.release_rows(name)]


def release_state(name: str) -> dict[str, dict]:
    """rkey -> {wanted, downloaded}: the queue columns, not part of entries."""
    return {r["rkey"]: {"wanted": bool(r["wanted"]), "downloaded": r["downloaded"]} for r in db.release_rows(name)}


def save_entries(name: str, entries: list[dict]) -> None:
    """Store the source's whole release list (for jobs that hold its lock)."""
    db.replace_releases(name, [(key(e), e, status(e)) for e in entries])


def save_entry(name: str, e: dict) -> None:
    """Store one release (safe alongside other writers)."""
    db.update_release(name, key(e), e, status(e))


# --------------------------------------------------------------------------- summaries


def tally(entries) -> str:
    n = {st: sum(1 for e in entries if status(e) == st) for st in ("matched", "owned", "missing", "review")}
    failed = sum(1 for e in entries if status(e) == "matched" and e.get("failed"))
    b = backend().label
    dls = f"{n['matched']} {b} downloads" + (f" ({failed} failed)" if failed else "")
    return f"{dls} | {n['review']} to review | {n['missing']} not on {b} | {n['owned']} owned"


def summarize(name: str, entries) -> None:
    """Save, then print the tally and anything still left to review."""
    save_entries(name, entries)
    pending = sorted((e for e in entries if status(e) == "review"), key=lambda e: -e.get("score", 0))
    for e in pending[:15]:
        say(f"  review {e.get('score', 0):.2f}  {e['artist']} - {e['title']}")
    if len(pending) > 15:
        say(f"  ... and {len(pending) - 15} more to review")
    say(f"\n{tally(entries)}")

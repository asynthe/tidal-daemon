"""Albums on disk: finding them, checking them, and noticing ones you added."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import db, library
from .backends import Track
from .library import status
from .text import ARTIST_SPLIT, norm, ratio

AUDIO = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".wav", ".aiff", ".aif", ".alac", ".wv", ".ape"}
LOSSY = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"}


def audio_files(folder: Path) -> list[Path]:
    return sorted(f for f in folder.rglob("*") if f.suffix.lower() in AUDIO)


def not_flac(files) -> str:
    """'' when every audio file is FLAC, else a summary like '12 .mp3 (lossy)'.
    The archive is FLAC only: lossy files need replacing, other lossless
    formats (wav, aiff, ...) need converting."""
    counts: dict[str, int] = {}
    for f in files:
        ext = f.suffix.lower()
        if ext in AUDIO and ext != ".flac":
            counts[ext] = counts.get(ext, 0) + 1
    return ", ".join(f"{n} {ext}" + (" (lossy)" if ext in LOSSY else "") for ext, n in sorted(counts.items()))


def expected_tracks(e: dict) -> int | None:
    """Tracks a complete download has: the album's count minus tracks the
    service lists but won't deliver."""
    return e["tracks"] - len(e.get("unavailable") or []) if e.get("tracks") else None


def album_folder(dest: Path, e: dict) -> Path | None:
    """Where a matched release was downloaded: the recorded folder or, for
    older downloads, a folder named after it (the label's or the service's
    naming, "<artist> - <title>[ (Explicit)]")."""
    if not dest.is_dir():
        return None
    d = dest / e["folder"] if e.get("folder") else None
    if d and d.is_dir():
        return d
    names = [(e["artist"], e["title"])]
    if " - " in (e.get("guess") or ""):
        names.append(tuple(e["guess"].split(" - ", 1)))
    from .text import clean_name

    # in the shared ARTISTS folder, only folders named after the release's artist
    shared = library.artists_dir() is not None and dest.resolve() == library.artists_dir().resolve()
    mine = {norm(a) for a in e["artist"].split(";")} | {norm(e["artist"])}
    best, best_r = None, 0.0
    for x in dest.iterdir():
        if not x.is_dir():
            continue
        if shared and not mine & {norm(a) for a in library.folder_artists(x.name)}:
            continue
        # compare titles with the folder's title part ("<artist> - <title>"),
        # never the whole name: a self-titled album's title is in every folder
        part = clean_name(x.name.split(" - ", 1)[1] if " - " in x.name else x.name)
        shortened = part.endswith("…")
        folder_title = norm(part.rstrip("…"))
        for artist, title in names:
            t = norm(title)
            if not t or not folder_title:
                continue
            if t == folder_title or (shortened and len(folder_title) >= 8 and t.startswith(folder_title)):
                r = ratio(f"{artist} - {title}", x.name)
                if r > best_r:
                    best, best_r = x, r
    return best


def read_source_tag(folder: Path | None) -> str | None:
    """What an album folder's SOURCE file says (first line), if it has one."""
    if folder is None:
        return None
    f = folder / "SOURCE"
    try:
        line = f.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return None
    return line[0].strip()[:100] if line else None


def album_year(files: list[Path]) -> str | None:
    import mutagen

    for f in files[:3]:
        try:
            t = mutagen.File(f, easy=True)
        except Exception:
            continue
        d = (t.get("date") or [None])[0] if t else None
        if d:
            return str(d)[:10]
    return None


def local_entry(folder: Path, fallback_artist: str) -> dict | None:
    """A release for an album folder you added yourself: owned, from its tags."""
    files = audio_files(folder)
    if not files:
        return None
    artist, album = album_tags(folder, files)
    return library.entry(
        artist=artist or fallback_artist,
        title=album or folder.name,
        kind="Album",
        date=album_year(files),
        source_url=f"local:{folder.name}",
        owned=True,
        origin="local",
        folder=folder.name,
        source_tag=read_source_tag(folder),
    )


def adopt_folders(name: str, entries: list[dict]) -> list[str]:
    """Album folders in a source's folder that no release accounts for (you
    put them there): matched to a release by their tags and marked owned,
    else added as an extra owned release. Also keeps each release's
    source_tag in step with its folder's SOURCE file. Returns what changed."""
    from . import db

    changed = []
    known = {e["folder"]: e for e in entries if e.get("folder")}
    ignored = db.ignored(name)
    for folder in library.album_dirs(name):
        e = known.get(folder.name)
        if e is not None:  # ours or already adopted: just keep the SOURCE tag current
            tag = read_source_tag(folder)
            if tag != e.get("source_tag"):
                e["source_tag"] = tag
                changed.append(f"source of {folder.name}: {tag}")
            continue
        if folder.name in ignored:
            continue
        files = audio_files(folder)
        if not files:
            continue
        artist, album = album_tags(folder, files)
        best = max(entries, key=lambda x: release_score(x, artist, album), default=None)
        if best is not None and release_score(best, artist, album) >= 0.8 and not best.get("folder"):
            best.update(owned=True, origin="local", folder=folder.name, source_tag=read_source_tag(folder))
            changed.append(f"{folder.name}: yours, as {best['title']}")
        else:
            new = local_entry(folder, library.display_name(name))
            if new is not None:
                entries.append(new)
                changed.append(f"{folder.name}: yours (not on the service)")
    return changed


def write_source_tag(folder: Path | None, tag: str | None) -> bool:
    """A SOURCE file in an album folder saying where it came from: one line,
    no newline (the tagging convention used across the archive). Never
    overwrites one that's there. Returns True if it wrote one."""
    if not tag or folder is None or not folder.is_dir():
        return False
    f = folder / "SOURCE"
    if f.exists():
        return False
    f.write_text(tag, encoding="utf-8", newline="")
    return True


def _int(v) -> int | None:
    """'3', '3/12' -> 3."""
    try:
        return int(str(v).split("/")[0])
    except (TypeError, ValueError):
        return None


def file_tags(f: Path) -> dict:
    """title, track number, disc number and ISRC from a file's tags."""
    import mutagen

    try:
        t = mutagen.File(f, easy=True)
    except Exception:
        t = None
    get = (lambda k: (t.get(k) or [None])[0]) if t else (lambda k: None)
    return {"title": get("title"), "num": _int(get("tracknumber")), "disc": _int(get("discnumber")) or 1, "isrc": get("isrc")}


def inventory(tracks: list[Track], folder: Path | None, unavailable: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Pair each of the album's songs with its file. A file matches by ISRC,
    else by disc + track number, else by title (tags first, file name last).
    Returns (one row per song with its state, files that matched no song)."""
    files = [(f, file_tags(f)) for f in (audio_files(folder) if folder else [])]
    gone = {norm(u.split(" - ", 1)[-1]) for u in (unavailable or [])}
    rows = []
    for t in tracks:
        hit = next((x for x in files if t.isrc and x[1]["isrc"] and x[1]["isrc"].upper() == t.isrc.upper()), None)
        hit = hit or next((x for x in files if x[1]["num"] == t.num and x[1]["disc"] == t.disc and (not x[1]["title"] or ratio(x[1]["title"], t.title) > 0.6)), None)
        hit = hit or next((x for x in files if x[1]["title"] and norm(x[1]["title"]) == norm(t.title)), None)
        hit = hit or next((x for x in files if x[0].name.startswith(f"{t.num:02d}. ") and norm(t.title) in norm(x[0].stem)), None)
        if hit:
            files.remove(hit)
        state = "present" if hit else ("unavailable" if not t.available or norm(t.title) in gone else "missing")
        rows.append(
            {
                "disc": t.disc,
                "num": t.num,
                "title": t.title,
                "artist": ", ".join(t.artists),
                "duration": t.duration,
                "isrc": t.isrc,
                "track_id": t.id,
                "file": str(hit[0].relative_to(folder)) if hit else None,
                "state": state,
            }
        )
    return rows, [str(f.relative_to(folder)) for f, _ in files]


def check_album(name: str, group: list[dict], *, ffmpeg: str | None = None, fix: bool = False, tracks: list[Track] | None = None) -> tuple[list[str], dict, Path | None]:
    """Check one matched album against its song list: every song the service
    delivers is on disk, readable and FLAC. Stores the song list in the
    database and updates the releases in `group` (track count, unavailable
    songs, folder). Returns (problems, details for a failure record, folder)."""
    from . import db

    e = group[0]
    b = library.backend(e.get("backend"))
    album_id = str(e["match_id"])
    folder = album_folder(library.label_dir(name), e)
    if tracks is None:
        tracks = b.album_tracks(album_id)
    rows, extras = inventory(tracks, folder, e.get("unavailable"))
    db.set_tracks(name, b.name, album_id, rows)
    gone = [f"{r['artist']} - {r['title']}" for r in rows if r["state"] == "unavailable"]
    for x in group:
        x["tracks"] = len(tracks)
        if gone:
            x["unavailable"] = gone
        else:
            x.pop("unavailable", None)
        if folder:
            x["folder"] = folder.name
    if folder is None:
        return ["not downloaded"], {}, None

    problems, record = [], {}
    missing = [r for r in rows if r["state"] == "missing"]
    if missing:
        need = sum(1 for r in rows if r["state"] != "unavailable")
        problems.append(f"incomplete: {need - len(missing)}/{need} tracks")
        record["missing"] = [f"{r['num']}. {r['title']}" for r in missing]
    files = audio_files(folder)
    broken = [f for f in files if not readable(f, ffmpeg)]
    if broken:
        problems.append(f"unreadable: {len(broken)} file(s)")
        record["broken"] = [f.name for f in broken]
        if fix:
            for f in broken:
                f.unlink()  # so the next download fetches it again instead of skipping it
    nf = not_flac(files)
    if nf:
        problems.append(f"not FLAC: {nf}")
    if extras:
        record["extra_files"] = extras
    return problems, record, folder


def readable(f: Path, ffmpeg: str | None) -> bool:
    """The file opens as audio with a real length; with ffmpeg, it also
    decodes start to finish without errors (slow: reads every byte)."""
    import mutagen

    try:
        a = mutagen.File(f)
        if a is None or not a.info.length > 0:
            return False
    except Exception:
        return False
    if ffmpeg:
        r = subprocess.run([ffmpeg, "-v", "error", "-i", str(f), "-f", "null", "-"], capture_output=True)
        return r.returncode == 0 and not r.stderr.strip()
    return True


def album_tags(folder: Path, files: list[Path]) -> tuple[str, str]:
    """(album artist, album) from the first tagged file, else the folder name
    ("Artist - Album", as Bandcamp names its zips)."""
    import mutagen

    for f in files[:5]:
        try:
            tags = mutagen.File(f, easy=True)
        except Exception:
            continue
        if tags and tags.get("album"):
            artist = (tags.get("albumartist") or tags.get("artist") or [""])[0]
            return artist, tags["album"][0]
    artist, _, album = folder.name.partition(" - ")
    return (artist, album) if album else ("", folder.name)


def release_score(e: dict, artist: str, album: str) -> float:
    """How well local tags fit a release, under the label's naming or, once
    matched, the service's (labels often credit and title releases differently)."""
    names = [(e["artist"], e["title"])]
    if e.get("match_id") and " - " in (e.get("guess") or ""):
        names.append(tuple(e["guess"].split(" - ", 1)))
    best = 0.0
    for want_artist, want_title in names:
        a = max([ratio(want_artist, artist)] + [ratio(w, artist) for w in ARTIST_SPLIT.split(want_artist)])
        best = max(best, 0.65 * ratio(want_title, album) + 0.35 * a)
    return best


def _own_download(e: dict, files: list[Path]) -> bool:
    """Files named the way the release's backend names its downloads."""
    try:
        pattern = library.backend(e.get("backend")).own_files
    except Exception:
        return False
    return bool(pattern) and all(pattern.match(f.name) for f in files)


def scan_local(name: str, entries: list[dict], interactive: bool | None = None) -> bool:
    """Look for albums dropped into the source's folder by hand (e.g. unzipped
    Bandcamp downloads) and offer to mark the matching release as owned.
    Returns True when something was marked."""
    d = library.label_dir(name)
    if not d.exists():
        return False
    ignored = db.ignored(name)
    downloaded = {e["folder"] for e in entries if e.get("folder") and e.get("match_id")}
    if interactive is None:
        interactive = sys.stdin.isatty()
    changed = False
    say = library.say

    for folder in library.album_dirs(name):
        files = audio_files(folder)
        if not files:
            continue
        bad = not_flac(files)
        if bad:  # every run, owned or ignored alike, until it's replaced
            say(f"\n[!] NOT FLAC: {folder.name}  ({bad})")
        if folder.name in ignored or folder.name in downloaded:
            continue
        artist, album = album_tags(folder, files)
        best = max(entries, key=lambda e: release_score(e, artist, album), default=None)
        sc = release_score(best, artist, album) if best else 0.0
        if best and sc >= 0.80:
            if status(best) == "owned":
                continue
            if status(best) == "matched" and _own_download(best, files):
                continue
        say(f"\n[!] Album found: {artist or '?'} - {album}")
        say(f"    {folder}  ({len(files)} files)")
        known = bool(best) and sc >= 0.80
        if known:
            say(f"    looks like: {best['artist']} - {best['title']}  [{status(best)}, {sc:.2f}]")
        else:
            say("    no matching release in the discography")
        if not interactive:
            if known:
                say(f"    run `tidal-daemon scan {name}` in a terminal to mark it")
            continue
        yes = known and not bad  # not FLAC: still markable, but not by just pressing enter
        q = f"mark as owned? y=yes / n=not now / i=ignore this folder [{'y' if yes else 'n'}]: " if known else "i=ignore this folder / n=not now [n]: "
        while True:
            try:
                ans = input("    " + q).strip().lower() or ("y" if yes else "n")
            except EOFError:
                return changed
            if ans in ("n", "i") or (known and ans == "y"):
                break
        if ans == "y":
            best["owned"] = True
            best["note"] = f"local: {folder.name}"
            changed = True
        elif ans == "i":
            db.ignore(name, folder.name)
    return changed


def tidy_names(name: str, entries: list[dict], dry_run: bool = False) -> list[tuple[str, str]]:
    """Rename a source's album folders and files that carry invisible
    characters or make paths too long (see text.tidy_path). Updates the
    releases' recorded folders; returns (old, new) for each album renamed."""
    from .text import PATH_LIMIT, _shorten, clean_name

    dest = library.label_dir(name)
    if not dest.is_dir():
        return []
    renamed = []
    # Only folders this tool downloaded; your own albums are never renamed.
    ours = {e["folder"] for e in entries if e.get("folder") and e.get("match_id") and e.get("origin") != "local"}
    for album in sorted(x for x in dest.iterdir() if x.is_dir() and x.name in ours):
        # the folder first, leaving room for the file names inside it
        new_name = _shorten(clean_name(album.name) or "_", min(180, max(PATH_LIMIT - len(str(dest)) - 60, 24)), False)
        target = dest / new_name
        if target != album and target.exists():
            library.say(f"  skip {album.name!r}: {new_name!r} already exists")
            continue
        files = [f for f in album.rglob("*") if f.is_file()]
        moves = []
        for f in files:
            rel = f.relative_to(album)
            room = PATH_LIMIT - len(str(target / rel.parent)) - 1
            fixed = _shorten(clean_name(f.name) or "_", max(room, 24), True)
            if fixed != f.name or target != album:
                moves.append((f, target / rel.parent / fixed))
        if target == album and not moves:
            continue
        renamed.append((album.name, new_name))
        if dry_run:
            continue
        for f, new in moves:  # files first, inside the old folder, then the folder itself
            inside = album / new.relative_to(target)
            if inside != f and not inside.exists():
                f.rename(inside)
        if target != album:
            album.rename(target)
        for e in entries:
            if e.get("folder") == album.name:
                e["folder"] = new_name
    return renamed

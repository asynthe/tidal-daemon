"""Sharper embedded cover art.

For each album folder, the best cover available — the album's original from
the service (found from the tracks' URL or ISRC tag) or a bigger cover.* already in
the folder — is embedded into every FLAC as a JPEG of at most MAX px, its
aspect kept. Audio and tags are untouched. The original also replaces a
smaller cover.jpg in the folder.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

MAX = 1400  # sharp on a phone's full-screen player, small enough to embed in every track
COVER_FILES = ("cover.png", "cover.jpg", "cover.jpeg", "folder.jpg", "folder.png")


def image_size(path: Path) -> tuple[int, int] | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    streams = json.loads(out.stdout or "{}").get("streams") or []
    return (streams[0]["width"], streams[0]["height"]) if streams else None


def embedded_width(flac: Path) -> int:
    """The shorter side of the biggest embedded picture (0: none). Measured
    from the image itself when the tag leaves the size out, as tidaler does."""
    from mutagen.flac import FLAC

    best = 0
    for p in FLAC(flac).pictures:
        side = min(p.width, p.height)
        if not side:
            tmp = Path(tempfile.gettempdir()) / "_embedded_cover"
            tmp.write_bytes(p.data)
            side = min(image_size(tmp) or (0, 0))
        best = max(best, side)
    return best


def _norm(title: str) -> str:
    return re.sub(r"\s*[\(\[].*?[\)\]]", "", title).casefold().strip()


def original_cover_url(session, flac: Path) -> str | None:
    """The album's original-size cover on the service, from the track's URL tag
    (tidaler writes .../track/<id>) or, failing that, its ISRC."""
    from mutagen.flac import FLAC

    tags = FLAC(flac)
    url = (tags.get("url") or [""])[0]
    m = re.search(r"/(?:browse/)?track/(\d+)(?!\w)", url)
    if m:
        try:
            return session.track(int(m.group(1))).album.image("origin")
        except Exception:
            pass  # gone from the service: try the ISRC
    isrc = (tags.get("isrc") or [""])[0]
    album = (tags.get("album") or [""])[0]
    if isrc and album:
        try:
            hits = session.get_tracks_by_isrc(isrc)
        except Exception:
            return None
        # an ISRC also turns up on compilations: only trust the same album title
        same = [t for t in hits if t.album and _norm(t.album.name) == _norm(album)]
        if same:
            return same[0].album.image("origin")
    return None


def to_embed(src: Path, dest: Path) -> tuple[int, int]:
    """src as a JPEG for embedding: at most MAX px a side, aspect kept."""
    vf = f"scale=w='min({MAX},iw)':h='min({MAX},ih)':force_original_aspect_ratio=decrease:flags=lanczos,format=yuvj420p"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", vf, "-q:v", "2", str(dest)], check=True)
    return image_size(dest)


def embed(flacs: list[Path], jpg: Path, size: tuple[int, int]) -> None:
    """Replace every track's pictures with jpg as the front cover; checks the
    audio and the tags came through unchanged."""
    from mutagen.flac import FLAC, Picture

    data = jpg.read_bytes()
    for fl in flacs:
        f = FLAC(fl)
        md5, tags = f.info.md5_signature, sorted(f.tags.items()) if f.tags else []
        pic = Picture()
        pic.type, pic.mime, pic.desc = 3, "image/jpeg", ""
        (pic.width, pic.height), pic.depth, pic.data = size, 24, data
        f.clear_pictures()
        f.add_picture(pic)
        f.save()
        g = FLAC(fl)
        if g.info.md5_signature != md5 or (sorted(g.tags.items()) if g.tags else []) != tags or len(g.pictures) != 1:
            raise RuntimeError(f"{fl.name}: changed more than the cover")


def upgrade(folder: Path, session, fetch, apply: bool) -> str | None:
    """Upgrade one album's embedded cover; returns what happened, or None if
    there's nothing better."""
    flacs = sorted(folder.glob("*.flac"))
    if not flacs:
        return None
    have = min(embedded_width(f) for f in flacs)
    if have >= MAX:
        return None
    candidates: list[tuple[int, Path, str]] = []
    for name in COVER_FILES:
        p = folder / name
        if p.exists() and (s := image_size(p)):
            candidates.append((min(s), p, name))
    tmp = Path(tempfile.gettempdir())
    origin = None
    if session is not None and (url := original_cover_url(session, flacs[0])):
        origin = tmp / "_original_cover.jpg"
        origin.write_bytes(fetch(url))
        if s := image_size(origin):
            candidates.append((min(s), origin, "original"))
    if not candidates:
        return None
    side, best, where = max(candidates, key=lambda c: c[0])
    if min(side, MAX) <= have * 1.2:  # not meaningfully sharper than what's embedded
        return None
    jpg = tmp / "_cover_embed.jpg"
    size = to_embed(best, jpg)
    if apply:
        embed(flacs, jpg, size)
        local = folder / "cover.jpg"
        if best is origin and (not local.exists() or min(image_size(local) or (0, 0)) < side):
            local.write_bytes(origin.read_bytes())
    return f"{have}px -> {size[0]}x{size[1]} from {where}"


def upgrade_all(folders: list[Path], session, log, apply: bool, delay: float = 0.3) -> tuple[int, int, list[str]]:
    import requests

    def fetch(url: str) -> bytes:
        time.sleep(delay)  # go easy on the service
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.content

    done = left = 0
    failed: list[str] = []
    for i, folder in enumerate(folders, 1):
        try:
            what = upgrade(folder, session, fetch, apply)
        except Exception as ex:
            failed.append(f"{folder.name}: {ex}")
            log(f"[{i}/{len(folders)}] [!] {folder.name}: {ex}")
            continue
        if what:
            done += 1
            log(f"[{i}/{len(folders)}] {folder.parent.name}/{folder.name}: {what}")
        else:
            left += 1
    return done, left, failed

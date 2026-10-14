"""One file-naming standard for the whole archive:

    01 - Artist - Title.flac
    01 - Artist 1; Artist 2 - Title.flac      several artists, "; " between them
    1-01 - Artist - Title.flac                multi-disc albums: disc-track

Names come from each file's tags, never from its current name. An artist the
title already credits ("(feat. crisp)", "(with …)") isn't repeated. Albums
are left alone (and reported) when renaming could break something or the
tags can't say what the name should be.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .local import AUDIO
from .text import PATH_LIMIT, _shorten, clean_name, norm

FEAT = re.compile(r"[\(\[](?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+)[\)\]]", re.I)
SPLIT_FEAT = re.compile(r"\s*(?:,|;|&|\band\b|\bx\b)\s*", re.I)
# Characters Windows doesn't allow in file names, swapped for readable stand-ins
# (M|O|O|N -> M.O.O.N, "Part 1: Intro" -> "Part 1 - Intro", "04:37" -> "04-37", "smile c:" -> "smile c").
FORBIDDEN = {"|": ".", "/": "-", "\\": "-", '"': "'", "<": "(", ">": ")", "?": "", "*": ""}


def _safe(name: str) -> str:
    name = re.sub(r":+(?=\s|$)", lambda m: " -" if m.end() < len(name) else "", name)
    name = name.replace(":", "-")
    for bad, good in FORBIDDEN.items():
        name = name.replace(bad, good)
    return re.sub(r"\s+", " ", name).strip()


@dataclass
class AlbumPlan:
    folder: Path
    renames: list[tuple[Path, Path]] = field(default_factory=list)
    skipped: str | None = None  # why the album is left alone
    note: str | None = None  # worth a look (e.g. artist folder vs tags)


def _tags(f: Path) -> dict | None:
    import mutagen

    try:
        t = mutagen.File(f, easy=True)
    except Exception:
        return None
    if t is None:
        return None

    def num(k):
        v = (t.get(k) or [None])[0]
        try:
            return int(str(v).split("/")[0]) if v is not None else None
        except ValueError:
            return None

    return {
        "artists": [a for a in (t.get("artist") or []) if a],
        "albumartist": (t.get("albumartist") or [""])[0],
        "title": (t.get("title") or [""])[0],
        "track": num("tracknumber"),
        "disc": num("discnumber") or 1,
        "discs": num("disctotal") or num("totaldiscs"),
    }


def artist_list(tags: dict) -> list[str]:
    """The track's artists as a list, without those the title already features."""
    raw = tags["artists"]
    if len(raw) == 1:
        one = raw[0]
        if ";" in one:
            raw = [a.strip() for a in one.split(";")]
        elif ", " in one and one != tags["albumartist"]:
            raw = [a.strip() for a in one.split(", ")]
    featured = {norm(x) for m in FEAT.finditer(tags["title"]) for x in SPLIT_FEAT.split(m.group(1)) if x.strip()}
    out = []
    for a in raw:
        if a and norm(a) not in featured and a not in out:
            out.append(a)
    return out or raw[:1]


def standard_name(tags: dict, ext: str, multi_disc: bool) -> str | None:
    """The file name the standard gives a track, or None if the tags can't say."""
    if not tags or not tags["track"] or not tags["title"].strip() or not tags["artists"]:
        return None
    number = f"{tags['disc']}-{tags['track']:02d}" if multi_disc else f"{tags['track']:02d}"
    artists = "; ".join(artist_list(tags))
    name = f"{number} - {artists} - {tags['title']}"
    # (a title may end in "." itself: fine before an extension, so no stripping)
    return clean_name(_safe(name)) + ext.lower()


def plan_album(folder: Path, folder_artist: str | None = None) -> AlbumPlan:
    """What renaming one album folder would do (nothing is changed)."""
    plan = AlbumPlan(folder)
    if any(p.is_dir() and any(f.suffix.lower() in AUDIO for f in p.rglob("*")) for p in folder.iterdir()):
        plan.skipped = "audio in subfolders (e.g. a box set's Disc 1, Disc 2)"
        return plan
    if any(p.suffix.lower() == ".cue" for p in folder.iterdir()):
        plan.skipped = "has a .cue sheet that refers to the file names"
        return plan
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO)
    if not files:
        return plan
    tags = {f: _tags(f) for f in files}
    untagged = [f.name for f, t in tags.items() if not t or not t["track"] or not t["title"].strip() or not t["artists"]]
    if untagged:
        plan.skipped = f"{len(untagged)} file(s) without track number/title/artist tags, e.g. {untagged[0]}"
        return plan
    discs = {t["disc"] for t in tags.values()}
    multi = len(discs) > 1 or any((t["discs"] or 1) > 1 for t in tags.values())
    names = {f: standard_name(t, f.suffix, multi) for f, t in tags.items()}
    # keep within the path limit: shorten titles (the part after the last " - ")
    for f, n in names.items():
        room = PATH_LIMIT - len(str(folder)) - 1
        if len(n) > room:
            names[f] = _shorten(n, max(room, 24), True)
    if len(set(names.values())) < len(names):
        plan.skipped = "two tracks would get the same name (duplicate track numbers?)"
        return plan
    for f, n in names.items():
        if n != f.name:
            target = folder / n
            if target.exists() and target.name.lower() not in {x.name.lower() for x in files}:
                plan.skipped = f"{n} already exists"
                return plan
            plan.renames.append((f, target))
    if folder_artist:
        album_artists = {norm(t["albumartist"] or (t["artists"] or [""])[0]) for t in tags.values()}
        if not any(norm(folder_artist) in a or a in norm(folder_artist) for a in album_artists if a):
            plan.note = f"tags say {sorted(album_artists)[0]!r}, not {folder_artist!r}"
    return plan


def apply_album(plan: AlbumPlan) -> dict[str, str]:
    """Do one album's renames (via temporary names, so swaps can't collide).
    Returns {old file name: new file name}."""
    temps = []
    for i, (src, dst) in enumerate(plan.renames):
        tmp = src.with_name(f".rename-{i}{src.suffix}")
        src.rename(tmp)
        temps.append((tmp, dst, src.name))
    done = {}
    for tmp, dst, old in temps:
        tmp.rename(dst)
        done[old] = dst.name
    return done

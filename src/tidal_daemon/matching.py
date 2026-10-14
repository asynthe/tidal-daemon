"""Matching scraped releases to a backend's albums: search, score, and the
interactive picker `check` uses."""

from __future__ import annotations

import re
import sys
import time

from .backends import Album, Backend
from .library import MATCH_FIELDS, picked, say, status
from .text import best_artist, norm, ratio


def score(rec: dict, album: Album) -> float:
    """Weighted similarity; the title carries more identity than the credit."""
    return 0.65 * ratio(rec["title"], album.title) + 0.35 * best_artist(rec["artist"], album.artists)


def describe(album: Album) -> str:
    """The facts that separate two near-identical releases."""
    meta = "  ".join(
        x for x in (album.date or "", f"{album.tracks} trk" if album.tracks else "", (album.copyright or "")[:38]) if x
    )
    return f"{album.artist} - {album.title}".ljust(52)[:52] + "  " + meta


def candidates(b: Backend, rec: dict, label: str = "") -> list[tuple[float, Album]]:
    scored = []
    for album in b.search_albums(f"{rec['artist']} {rec['title']}"):
        s = score(rec, album)
        # A copyright line naming the label is strong corroboration.
        if label and label.lower() in (album.copyright or "").lower():
            s = min(1.0, s + 0.05)
        scored.append((s, album))
    scored.sort(key=lambda t: -t[0])
    return scored


def apply_match(rec: dict, b: Backend, album: Album, confirmed: bool = False) -> None:
    rec.update(backend=b.name, match_id=album.id)
    rec.update({k: v for k, v in album.meta().items() if v is not None})
    if confirmed:
        rec["confirmed"] = True


def not_on_service(theirs: list[str], ours: list[str]) -> list[str]:
    """The source's tracks with no counterpart in the service's version of the
    album. Titles are compared loosely: compilations say "Artist - Title",
    and remix names may sit in the title on one side only."""
    def k(s: str) -> str:  # "Don't" = "Dont", "I'm" = "Im"
        return norm(re.sub(r"['’`]", "", s)).replace(" ", "")

    have = [h for h in map(k, ours) if h]
    # a typo on one side ("Entertainement") still counts as the same track
    out = [t for t in theirs if not any(h in k(t) or k(t) in h or ratio(h, k(t)) >= 0.9 for h in have)]
    if not out and len(theirs) > len(ours):  # names line up loosely, yet it's shorter
        out = [f"{len(theirs) - len(ours)} track(s)"]
    return out


def fetch_album(b: Backend, album_id: str) -> Album | None:
    try:
        return b.album(album_id)
    except Exception as e:
        say(f"  can't open {b.label} album {album_id}: {e}")
        return None


def ask(b: Backend, rec: dict, scored, threshold: float):
    """Returns ('pick', album), ('skip', reason), ('owned', note) or ('quit', None)."""
    top = scored[:6]
    print(f"\n  {rec['artist']} - {rec['title']}   [{rec.get('type') or '?'}]", file=sys.stderr)
    if rec.get("source_url"):
        print(f"  {rec['source_url']}", file=sys.stderr)
    for i, (s, album) in enumerate(top, 1):
        print(f"   {i}{'*' if s >= threshold else ' '} {s:.2f}  {describe(album)}", file=sys.stderr)

    default = "1" if top and top[0][0] >= threshold else "s"
    opts = f"paste {b.label} link / h=have it / s=not on {b.label} / q=quit"
    prompt = f"  1-{len(top)} / {opts} [{default}]: " if top else f"  no {b.label} results — {opts} [s]: "
    while True:
        try:
            raw = input(prompt).strip() or default
        except EOFError:
            return "quit", None
        head, _, rest = raw.partition(" ")
        head = head.lower()
        if head == "q":
            return "quit", None
        if head == "s":
            return "skip", rest.strip()
        if head == "h":
            return "owned", rest.strip()
        if head.isdigit() and 1 <= int(head) <= len(top):
            return "pick", top[int(head) - 1][1]
        link = b.parse_link(raw)
        if link and link[0] == "album":
            album = fetch_album(b, link[1])
            if album is not None:
                print(f"     {describe(album)}", file=sys.stderr)
                if (input("  use this one? [Y/n]: ").strip().lower() or "y") == "y":
                    return "pick", album
            continue
        print("  ?", file=sys.stderr)


def resolve(b: Backend, entries, label="", threshold=0.80, auto=0.97, delay=0.6, force=False, save=None, only=None, interactive=None):
    """Match entries that need it. Mutates entries. Without a terminal (or
    with interactive=False) only matches at or above `threshold` are taken."""
    # Owned releases are settled for good; even -f leaves them alone.
    todo = [e for e in entries if picked(e, only) and status(e) != "owned" and (force or status(e) == "review")]
    if not todo:
        say("nothing to check — everything is already decided")
        return
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        say(f"auto-accepting matches scoring {threshold:.2f} or more")

    for i, rec in enumerate(todo, 1):
        if force:
            for f in MATCH_FIELDS:
                rec.pop(f, None)
        try:
            scored = candidates(b, rec, label)
        except Exception as e:
            say(f"[{i}/{len(todo)}] ERROR {rec['artist']} - {rec['title']}: {e}")
            rec["note"] = f"error: {e}"
            time.sleep(delay * 3)
            continue

        best_score, best = scored[0] if scored else (0.0, None)
        rec["score"] = round(best_score, 3)
        if best is not None:
            rec["guess"] = f"{best.artist} - {best.title}"
            rec["guess_id"] = best.id

        if interactive and best_score < auto:
            print(f"[{i}/{len(todo)}]", end="", file=sys.stderr)
            action, value = ask(b, rec, scored, threshold)
            if action == "quit":
                say(f"\nstopped at {i}/{len(todo)} — re-run `check` to continue")
                return
            if action == "skip":
                rec["skipped"] = True
                rec["note"] = value or f"no {b.label} release"
            elif action == "owned":
                rec["owned"] = True
                if value:
                    rec["note"] = value
            else:
                apply_match(rec, b, value, confirmed=True)
        else:
            ok = best is not None and best_score >= (auto if interactive else threshold)
            if ok:
                apply_match(rec, b, best)
            say(
                f"[{i}/{len(todo)}] {'OK  ' if ok else 'MISS'} {best_score:.2f}  {rec['artist']} - {rec['title']}"
                + (f"  ->  {rec['guess']}" if best else "")
            )

        if save:
            save()
        time.sleep(delay)

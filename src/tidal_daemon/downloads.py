"""Downloading albums through a backend, and knowing whether it worked.

The backend only fetches files. Everything else is here, the same for any
backend: retries, checking the album is complete, recording failures on the
release (cleared on success), and the queue columns in the database.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import db, library
from .library import key, save_entry, status
from .local import album_folder, audio_files, check_album, expected_tracks, write_source_tag
from .naming import apply_album, plan_album

# printer(message, level): level is info | done | warn | error
Printer = Callable[[str, str], None]


@dataclass
class Report:
    done: list[dict] = field(default_factory=list)
    failures: list[tuple[dict, Exception]] = field(default_factory=list)
    lossy: list[tuple[dict, str]] = field(default_factory=list)
    interrupted: bool = False


def groups_of(entries: list[dict]) -> list[list[dict]]:
    """Catalogues list some releases twice: one download per album, with the
    outcome recorded on every listing of it."""
    same: dict[tuple, list[dict]] = {}
    for e in entries:
        if status(e) == "matched":
            same.setdefault((e.get("backend"), str(e["match_id"])), []).append(e)
    return list(same.values())


def _touched(dest: Path, since: float) -> list[Path]:
    """Album folders written into since `since` (adding a file bumps a folder's mtime)."""
    return [x for x in dest.iterdir() if x.is_dir() and x.stat().st_mtime >= since - 1]


def _count(folders: list[Path]) -> int:
    return sum(len(audio_files(d)) for d in folders)


def download(
    name: str,
    groups: list[list[dict]],
    *,
    abort: threading.Event,
    printer: Printer,
    retries: int = 3,
    retry_wait: float = 10,
    pause: float = 10,
    on_album: Callable[[int, int, str], None] | None = None,
) -> Report:
    dest = library.label_dir(name)
    dest.mkdir(parents=True, exist_ok=True)
    report = Report()

    for i, group in enumerate(groups, 1):
        if abort.is_set():
            break
        e = group[0]
        label = f"{e['artist']} - {e['title']}"
        if on_album:
            on_album(i, len(groups), label)
        b = library.backend(e.get("backend"))
        album_id = str(e["match_id"])

        # The album's song list is what "complete" means. If the service can't
        # give it right now, fall back to counting files.
        try:
            tracks = b.album_tracks(album_id)
            for x in group:
                x["tracks"] = len(tracks)
        except Exception:
            tracks = None

        def check(folders: list[Path]) -> tuple[Exception | None, dict]:
            """(error if songs are missing or unreadable, details for the record)."""
            if tracks is None:
                want = expected_tracks(e)
                if want and folders and _count(folders) < want:
                    return RuntimeError(f"incomplete: {_count(folders)}/{want} tracks"), {}
                return (None if folders else RuntimeError("nothing downloaded")), {}
            if folders:
                for x in group:
                    x["folder"] = folders[0].name  # check the folder this run wrote
            problems, details, _ = check_album(name, group, tracks=tracks)
            hard = [p for p in problems if p.startswith(("incomplete", "unreadable", "not downloaded"))]
            lossy = [p for p in problems if p.startswith("not FLAC")]
            if lossy and not hard:
                report.lossy.append((e, lossy[0]))
                printer(f"[!] NOT FLAC {label}: {lossy[0][10:]}", "warn")
            return (RuntimeError("; ".join(hard)) if hard else None), details

        err: Exception | None = None
        details: dict = {}
        folders: list[Path] = []
        for attempt in range(retries + 1):
            t0, err = time.time(), None
            try:
                result = b.download_album(album_id, dest, lambda m: printer(m, "info"), abort, label)
                if result.unavailable:
                    for x in group:
                        x["unavailable"] = result.unavailable
            except Exception as ex:
                err = ex
                db.log(name, "error", f"{label} (try {attempt + 1}): {type(ex).__name__}: {ex}", traceback.format_exc())
            folders = _touched(dest, t0) or [f for f in [album_folder(dest, e)] if f]
            if err is None:
                err, details = check(folders)
            if err is None or abort.is_set():
                break
            if attempt < retries:
                printer(f"retry in {retry_wait:g}s {label}: {err}", "warn")
                abort.wait(retry_wait)

        folders = [d for d in folders if d.is_dir()]
        interrupted = abort.is_set()
        if interrupted:
            # Stopped mid-album: only a provably complete album counts as done.
            ok, details = check(folders) if folders else (RuntimeError("interrupted"), {})
            err = None if ok is None else RuntimeError("interrupted")

        if err is None:
            record = None
            report.done.append(e)
            if folders:
                write_source_tag(folders[0], library.settings().get("source_tag"))
                # The archive's naming standard (1-01 on multi-disc albums,
                # guests named in the title not repeated); then record the new names.
                plan = plan_album(folders[0])
                if plan.renames and not plan.skipped:
                    apply_album(plan)
                    if tracks is not None:
                        check_album(name, group, tracks=tracks)
            note = f" (without {', '.join(e['unavailable'])}: no longer on {b.label})" if e.get("unavailable") else ""
            printer(f"done {label}{note}", "done")
        else:
            if not interrupted:
                report.failures.append((e, err))
                printer(f"FAILED {label}: {err}", "error")
            for d in folders:
                if not any(d.iterdir()):
                    d.rmdir()  # nothing landed; don't leave an empty album folder
            folders = [d for d in folders if d.is_dir()]
            record = {"when": f"{datetime.now():%Y-%m-%d %H:%M}", "error": str(err)[:300], **details}

        keys = []
        for x in group:
            if record:
                x["failed"] = record
            else:
                x.pop("failed", None)
            if folders:
                x["folder"] = folders[0].name
            save_entry(name, x)
            keys.append(key(x))
        db.set_downloaded(name, keys, None if record else db.now())
        if record is None:
            db.set_wanted(name, keys, True)
        db.log(name, "done" if record is None else "error", f"{'downloaded' if record is None else 'failed'}: {label}" + (f" ({record['error']})" if record else ""))

        if interrupted:
            report.interrupted = True
            if record:
                printer(f"interrupted {label}: marked failed, it resumes next time", "warn")
            break
        if i < len(groups):
            abort.wait(pause)
    return report

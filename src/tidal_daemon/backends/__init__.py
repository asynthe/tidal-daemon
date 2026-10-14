"""Backends: the only code that talks to a music service.

Everything else in tidal-daemon (sources, matching, the library, the daemon, the
TUI) works with the neutral types below. A backend wraps one service's
catalogue (search, albums, artists) and its downloader. To support another
service, add a module here and list it in BACKENDS.
"""

from __future__ import annotations

import importlib
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class BackendError(Exception):
    """The service can't be used right now: not logged in, unreachable, ..."""


@dataclass
class Album:
    id: str
    title: str
    artists: list[str]
    date: str | None = None
    tracks: int | None = None
    upc: str | None = None
    copyright: str | None = None
    url: str | None = None
    # "atmos" (Dolby Atmos: spatial, not FLAC), "hires", "lossless", "lossy", or None if unknown
    audio: str | None = None

    @property
    def artist(self) -> str:
        return ", ".join(self.artists) or "?"

    def meta(self) -> dict:
        """What a match keeps about the album."""
        return {"upc": self.upc, "tracks": self.tracks, "copyright": self.copyright, "released": self.date, "audio": self.audio}


@dataclass
class Track:
    num: int
    title: str
    disc: int = 1
    id: str | None = None
    artists: list[str] = field(default_factory=list)
    duration: int | None = None  # seconds
    isrc: str | None = None
    available: bool = True  # False: listed, but the service won't deliver it


@dataclass
class Artist:
    id: str
    name: str
    url: str | None = None
    hint: str | None = None  # e.g. a few album titles, to tell same-named artists apart


@dataclass
class DownloadResult:
    # Tracks the service lists but won't deliver; they don't count as missing.
    unavailable: list[str] = field(default_factory=list)


Log = Callable[[str], None]


class Backend(ABC):
    name = ""  # stored with every match, e.g. "tidaler"
    label = ""  # shown to people
    # How this backend names downloaded files; `scan` uses it to tell the
    # backend's own downloads from albums you added yourself.
    own_files: re.Pattern | None = None

    def __init__(self, settings: dict):
        self.settings = settings  # library settings: quality, ...

    @abstractmethod
    def login(self) -> int:
        """Interactive login; returns an exit code."""

    @abstractmethod
    def search_albums(self, query: str, limit: int = 10) -> list[Album]: ...

    @abstractmethod
    def album(self, album_id: str) -> Album: ...

    @abstractmethod
    def album_tracks(self, album_id: str) -> list[Track]: ...

    @abstractmethod
    def artist(self, artist_id: str) -> Artist: ...

    @abstractmethod
    def find_artists(self, name: str, limit: int = 10) -> list[Artist]: ...

    @abstractmethod
    def artist_albums(self, artist_id: str, groups: set[str]) -> list[tuple[str, Album]]:
        """(release type, album) for an artist. groups: "albums",
        "ep_singles", "other" (appears-on)."""

    @abstractmethod
    def parse_link(self, text: str) -> tuple[str, str] | None:
        """("album" | "artist", id) for one of this service's links, else None.
        Must work without logging in."""

    @abstractmethod
    def download_album(self, album_id: str, dest: Path, log: Log, abort: threading.Event, label: str = "") -> DownloadResult:
        """Download one album into its own folder under dest, skipping files
        already there. Raises on failure; setting `abort` stops it early."""

    def progress(self):
        """A rich renderable showing the album in flight, or None."""
        return None

    def album_url(self, album_id: str) -> str | None:
        return None

    def artist_url(self, artist_id: str) -> str | None:
        return None


# name -> "module:Class", imported on first use so an unused backend's
# libraries never load.
BACKENDS = {
    "tidaler": "tidal_daemon.backends.tidaler:TidalerBackend",
}
_LOADED: dict[str, Backend] = {}


def get_backend(name: str, settings: dict) -> Backend:
    if name not in BACKENDS:
        raise BackendError(f"unknown backend {name!r} (have: {', '.join(BACKENDS)})")
    if name not in _LOADED:
        module, _, cls = BACKENDS[name].partition(":")
        _LOADED[name] = getattr(importlib.import_module(module), cls)(settings)
    return _LOADED[name]


def parse_any_link(text: str, settings: dict) -> tuple[Backend, str, str] | None:
    """(backend, "album" | "artist", id) for a link from any backend."""
    for name in BACKENDS:
        b = get_backend(name, settings)
        hit = b.parse_link(text)
        if hit and "/" in text:  # bare ids only count for the default backend
            return b, *hit
    return None

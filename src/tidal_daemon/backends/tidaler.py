"""The tidaler backend: catalogue and downloads, both through tidaler.

This is the only module in tidal-daemon that imports tidaler.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import Album, Artist, Backend, BackendError, DownloadResult, Log, Track

# The library's naming placeholders (see library.NAMING_FIELDS) in tidaler's terms.
TIDALER_FIELDS = {
    "album_artist": "album_artist",
    "album": "album_title",
    "year": "album_year",
    "date": "album_date",
    "explicit": "album_explicit",
    "disc": "track_volume_num_optional",
    "track": "album_track_num",
    "artist": "artist_name",
    "title": "track_title",
    "isrc": "isrc",
}


def tidaler_template(album_folder: str, track_file: str) -> str:
    """"{album_artist} - {album}" + "{track}. {title}" -> tidaler's own template."""

    def tr(pattern: str) -> str:
        def sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in TIDALER_FIELDS:
                raise BackendError(f"unknown naming placeholder {{{name}}}")
            return "{" + TIDALER_FIELDS[name] + "}"

        return re.sub(r"\{(\w+)\}", sub, pattern)

    return tr(album_folder).strip("/\\") + "/" + tr(track_file)


# A service link: .../album/<id> or .../artist/<id>. Bandcamp has the same
# paths (with names, not numbers), so its pages never count.
LINK = re.compile(r"^(?!.*bandcamp\.com).*?/(?:browse/)?(album|artist)/(\d+)(?!\w)", re.I)
UNAVAILABLE = "This item is not available for listening anymore"


def _audio(a) -> str | None:
    """The catalogue lists Dolby Atmos mixes as separate releases with the same title."""
    modes = getattr(a, "audio_modes", None) or []
    tags = getattr(a, "media_metadata_tags", None) or []
    if "DOLBY_ATMOS" in modes or "DOLBY_ATMOS" in tags:
        return "atmos"
    if "HIRES_LOSSLESS" in tags:
        return "hires"
    if "LOSSLESS" in tags:
        return "lossless"
    return "lossy" if tags else None


def _album(a) -> Album:
    date = getattr(a, "release_date", None)
    names = [x.name for x in (getattr(a, "artists", None) or [])]
    if not names and getattr(a, "artist", None):
        names = [a.artist.name]
    return Album(
        id=str(a.id),
        title=a.name,
        artists=names,
        date=str(date)[:10] if date else None,
        tracks=getattr(a, "num_tracks", None),
        upc=getattr(a, "universal_product_number", None),
        copyright=(getattr(a, "copyright", "") or "").strip() or None,
        url=getattr(a, "share_url", None) or None,
        audio=_audio(a),
    )


def _clean_tidaler_paths() -> None:
    """tidaler names files with the catalogue's titles as they are, invisible
    zero-width characters and all, and without minding path length. Wrap the
    one function it builds (and checks for existing) paths with, so names are
    clean from the start and re-runs still find what's already there."""
    import tidaler.download as tdl

    from ..text import tidy_path

    if getattr(tdl, "_tidal_daemon_clean", False):
        return
    original = tdl.path_file_sanitize

    def sanitize(path_file, adapt: bool = False, uniquify: bool = False):
        return original(tidy_path(Path(path_file)), adapt=adapt, uniquify=uniquify)

    tdl.path_file_sanitize = sanitize
    tdl._tidal_daemon_clean = True


class _AlbumProgress:
    """Wraps rich's Progress so tidaler's per-album bar shows our label.

    tidaler names that bar "List '<title>'" (no artist, cut at 30 chars), so
    add_task's description is replaced. Each album gets a brand-new Progress
    rather than removing the old task: tidaler looks tasks up by position,
    `progress.tasks[task_id]`, which only holds while none was ever removed.
    """

    def __init__(self, make):
        self._make = make
        self._p = make()
        self.label = ""

    def new_album(self, label: str) -> None:
        self._p = self._make()
        self.label = label

    def add_task(self, description, **kw):
        return self._p.add_task(self.label or description, **kw)

    def __rich__(self):
        return self._p

    def __getattr__(self, name):
        return getattr(self._p, name)


def _tidaler_home() -> Path:
    """Point tidaler's login and settings (token.json, settings.json) at
    .tidal-daemon/tidaler instead of ~/.config/tidaler, so they live with
    the library. A login kept in the old place is copied over, once. Must
    run before tidaler.config is imported (it reads the path at import)."""
    from tidaler.helper import path

    from .. import library

    home = library.data_dir() / "tidaler"
    old = Path(path.path_config_base())
    if old != home:
        home.mkdir(parents=True, exist_ok=True)
        for f in ("token.json", "settings.json"):
            if (old / f).is_file() and not (home / f).exists():
                shutil.copy2(old / f, home / f)
        path.path_config_base = lambda: str(home)
    return home


class TidalerBackend(Backend):
    name = "tidaler"
    label = "tidaler"
    own_files = re.compile(r"^\d+\. ")  # tidaler names tracks "01. Artist - Title"

    def __init__(self, settings: dict):
        super().__init__(settings)
        self._tidaler = None
        self._shim = None
        self._downloads: dict[Path, object] = {}
        self.home = _tidaler_home()

    # ------------------------------------------------------------------ session

    def _client(self):
        if self._tidaler is None:
            from tidaler import config

            client = config.Tidal(config.Settings())
            if not client.login_token():
                raise BackendError("not logged in — run `tidal-daemon login`")
            self._tidaler = client
        return self._tidaler

    @property
    def session(self):
        return self._client().session

    def login(self) -> int:
        exe = Path(sys.executable).with_name("tidaler.exe" if os.name == "nt" else "tidaler")
        # tidaler puts its files in $XDG_CONFIG_HOME/tidaler: here, .tidal-daemon/tidaler
        return subprocess.call([str(exe), "login"], env={**os.environ, "XDG_CONFIG_HOME": str(self.home.parent)})

    # ------------------------------------------------------------------ catalogue

    def search_albums(self, query: str, limit: int = 10) -> list[Album]:
        hits = self.session.search(query, limit=limit).get("albums") or []
        return [_album(a) for a in hits]

    def album(self, album_id: str) -> Album:
        return _album(self.session.album(int(album_id)))

    def album_tracks(self, album_id: str) -> list[Track]:
        return [
            Track(
                num=t.track_num,
                title=t.name,
                disc=getattr(t, "volume_num", None) or 1,
                id=str(t.id),
                artists=[a.name for a in (t.artists or [])],
                duration=getattr(t, "duration", None),
                isrc=getattr(t, "isrc", None),
                available=bool(getattr(t, "allow_streaming", True) and getattr(t, "available", True)),
            )
            for t in self.session.album(int(album_id)).tracks()
        ]

    def artist(self, artist_id: str) -> Artist:
        a = self.session.artist(int(artist_id))
        return Artist(str(a.id), a.name, getattr(a, "share_url", None) or None)

    def find_artists(self, name: str, limit: int = 10) -> list[Artist]:
        hits = self.session.search(name, limit=limit).get("artists") or []
        out = []
        for i, a in enumerate(hits):
            hint = None
            if i < 8:  # same-named artists are common: show what each one released
                try:
                    hint = ", ".join(x.name for x in a.get_albums(limit=3)) or None
                except Exception:
                    pass
            out.append(Artist(str(a.id), a.name, getattr(a, "share_url", None) or None, hint))
        return out

    def artist_albums(self, artist_id: str, groups: set[str]) -> list[tuple[str, Album]]:
        a = self.session.artist(int(artist_id))
        calls = {
            "albums": ("Album", a.get_albums),
            "ep_singles": ("EP/Single", getattr(a, "get_ep_singles", None) or a.get_albums_ep_singles),
            "other": ("Other", a.get_albums_other),
        }
        out, seen = [], set()
        for group, (kind, fn) in calls.items():
            if group not in groups:
                continue
            for album in fn(limit=200):
                if album.id not in seen:
                    seen.add(album.id)
                    out.append((kind, _album(album)))
        return out

    def parse_link(self, text: str) -> tuple[str, str] | None:
        m = LINK.search(text)
        if m:
            return m.group(1).lower(), m.group(2)
        text = text.strip()
        return ("album", text) if text.isdigit() and len(text) >= 6 else None

    def _share_url(self, kind: str, id_) -> str | None:
        try:
            return f"{self.session.config.share_base_url}/{kind}/{id_}"
        except BackendError:
            return None  # not logged in

    def album_url(self, album_id: str) -> str | None:
        return self._share_url("album", album_id)

    def artist_url(self, artist_id) -> str | None:
        return self._share_url("artist", artist_id)

    # ------------------------------------------------------------------ downloads

    def progress(self):
        return self._shim

    def download_album(self, album_id: str, dest: Path, log: Log, abort: threading.Event, label: str = "") -> DownloadResult:
        from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
        from tidaler.config import HandlingApp
        from tidaler.constants import CoverDimensions, MediaType, Quality
        from tidaler.download import Download
        from tidaler.helper.path import get_format_template
        from tidaler.helper.wrapper import LoggerWrapped

        cols = (TextColumn("[progress.description]{task.description}"), BarColumn(), TaskProgressColumn())
        unavailable: list[str] = []

        class QuietLog(LoggerWrapped):
            """tidaler's log trimmed to "Downloaded item '...'" per track plus
            problems; notes the tracks the service won't stream."""

            def debug(self, value):
                pass  # "Next download will start in N seconds.", skips, file moves

            def info(self, value):
                value = str(value)
                if value.startswith(UNAVAILABLE):
                    unavailable.append(value.partition("Skipping: ")[2].strip())
                if not value.startswith("Finished list"):
                    self.fn_print(value)

        try:
            quality = Quality(self.settings.get("quality") or "HI_RES_LOSSLESS")
        except ValueError:
            raise BackendError(f"quality {self.settings.get('quality')!r}: want one of {[q.value for q in Quality]}")
        client = self._client()
        _clean_tidaler_paths()
        client.settings.data.extract_flac = True  # FLAC out of the MP4 container, via FFmpeg
        client.settings.data.album_track_num_pad_min = 2  # {track} is always 01, 02, ... (tidaler: 1 below 10 tracks)
        client.settings.data.filename_delimiter_artist = "; "  # "Artist 1; Artist 2" (the archive's naming standard)
        client.settings.data.filename_delimiter_album_artist = "; "
        # Covers: tidaler embeds 320px by default. "origin" embeds 1280px (its cap for tags)
        # and saves the full-resolution original as cover.jpg.
        client.settings.data.metadata_cover_embed = True
        client.settings.data.cover_album_file = True
        client.settings.data.metadata_cover_dimension = CoverDimensions.PxORIGIN
        # An Atmos release is only downloaded when Atmos isn't skipped; then get the real Atmos audio.
        client.settings.data.download_dolby_atmos = not self.settings.get("skip_atmos", True)

        if self._shim is None:
            self._shim = _AlbumProgress(lambda: Progress(*cols, expand=True))
        self._shim.new_album(label)
        dl = self._downloads.get(dest)
        if dl is None:
            dl = self._downloads[dest] = Download(
                tidal_obj=client,  # tidaler's own parameter name
                skip_existing=True,
                path_base=str(dest),
                fn_logger=QuietLog(log),
                progress=Progress(*cols),
                progress_overall=self._shim,
                event_abort=abort,
                event_run=HandlingApp().event_run,
            )
        dl.fn_logger = QuietLog(log)
        dl.event_abort = abort
        dl.progress = Progress(*cols)  # fresh per album; never remove tasks (see _AlbumProgress)

        # Folder and file names come from the library's naming settings; without
        # them, tidaler's own template minus its leading "Albums/".
        if self.settings.get("album_folder") and self.settings.get("track_file"):
            template = tidaler_template(self.settings["album_folder"], self.settings["track_file"])
        else:
            parts = get_format_template(MediaType.ALBUM, client.settings).split("/")
            while len(parts) > 1 and "{" not in parts[0]:
                parts.pop(0)
            template = "/".join(parts)
        dl.items(
            media_id=str(album_id),
            media_type=MediaType.ALBUM,
            file_template=template,
            video_download=False,
            download_delay=client.settings.data.download_delay,
            quality_audio=quality,
            quality_video=client.settings.data.quality_video,
        )
        return DownloadResult(unavailable=sorted(set(unavailable)))

"""tidal-daemon TUI: an ncmpcpp-style client for the daemon.

Modelled on the ncmpcpp setup in dots/config/ncmpcpp: no header or column
titles, the terminal's own colours, a ─╼ progress bar over a classic status
line, numbered screens and vim keys.

    1 library     labels and artists | albums | songs; space queues an album
    2 downloads   what's downloading (> ), the queue, what finished or failed
    3 search      find artists on the service and add them
    4 settings    folders, file naming, quality, timings
    5 log         new releases, downloads, errors
    6 help

It only talks to the daemon's local API and never opens the database, so it
can sit open while the daemon works.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, DataTable, Input, RichLog, Static

from .text import norm  # plain string helpers: no database, no backend

ROOT = Path(__file__).resolve().parents[2]  # the project folder (src/tidal_daemon/ -> .)
SCREENS = ["library", "downloads", "search", "settings", "log", "help"]
SCREEN_NAMES = {"library": "Library", "downloads": "Downloads", "search": "Search", "settings": "Settings", "log": "Log", "help": "Help"}
VIEWS = ["all", "to download", "downloaded", "failed", "to review"]

# Colours from the ncmpcpp song_columns_list_format: [5] magenta, [6] cyan, [4] blue.
ARTIST, ALBUM, YEAR, KIND, NUM = "magenta", "magenta", "magenta", "cyan", "blue"
DIM = "bright_black"


class Api:
    def __init__(self, host: str, port: int):
        self.base = f"http://{host}:{port}"

    def _call(self, req, timeout: float = 5):
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def get(self, path: str, timeout: float = 5, **q):
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in q.items())
        return self._call(f"{self.base}{path}" + (f"?{qs}" if qs else ""), timeout)

    def post(self, path: str, **body):
        req = urllib.request.Request(f"{self.base}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        return self._call(req)


PREFS = Path(os.environ["TIDAL_DAEMON_TUI_PREFS"]) if os.environ.get("TIDAL_DAEMON_TUI_PREFS") else Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "tidal-daemon" / "tui.json" if os.name == "nt" else Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "tidal-daemon" / "tui.json"


def load_prefs() -> dict:
    """The TUI's own look-and-feel choices (kept apart from the library)."""
    try:
        return json.loads(PREFS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_prefs(**values) -> None:
    try:
        PREFS.parent.mkdir(parents=True, exist_ok=True)
        PREFS.write_text(json.dumps({**load_prefs(), **values}, indent=2), encoding="utf-8")
    except OSError:
        pass


def start_daemon() -> None:
    """Start `tidal-daemon serve` in the background, logging to daemon.log in .tidal-daemon."""
    from . import library  # only for where .tidal-daemon is

    log_dir = library.data_dir()
    log_dir.mkdir(exist_ok=True)
    log = open(log_dir / "daemon.log", "a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [sys.executable, str(ROOT / "tidal-daemon"), "serve"],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        start_new_session=os.name != "nt",
        cwd=ROOT,
    )


def fit(s: str | None, width: int) -> str:
    """Fixed-width column, like ncmpcpp's (20)/(25) widths."""
    s = s or ""
    return (s[: width - 1] + "…") if len(s) > width else s.ljust(width)


def mark(r: dict, selected: bool = False) -> Text:
    """One-glyph state: ● selected, + queued, ✔ done, ✗ failed, · on the
    service, ✔ also for albums you have from elsewhere, - not on the service, ? to review."""
    if selected:
        return Text("●", style="bold white")
    if r.get("elsewhere") and r["elsewhere"][1] != "listed" and not r.get("downloaded") and r["status"] != "owned":  # another source has it
        return Text("+", style="bold yellow") if r["elsewhere"][1] == "queued" else Text("✔", style="green")
    if r["status"] == "matched":
        if r.get("failed"):
            return Text("✗", style="red")
        if r.get("downloaded"):
            return Text("✔", style="green")
        if r.get("wanted"):
            return Text("+", style="bold yellow")
        return Text("·", style="white")
    return {"owned": Text("✔", style="green"), "missing": Text("-", style=DIM), "review": Text("?", style="magenta")}[r["status"]]


def lacking(r: dict) -> bool:
    """Missing tracks: the service's version is shorter than the label's, the
    download failed, or songs aren't all on disk."""
    on_disk = r.get("downloaded") and r.get("needed") and (r.get("present") or 0) < r["needed"]
    return r["status"] == "matched" and bool(r.get("short") or r.get("failed") or on_disk)


CATEGORIES = ["All", "Albums", "EPs & Singles", "Dolby Atmos", "Other"]


def category(r: dict) -> str:
    """A release folded into the category column. Dolby Atmos mixes (the catalogue
    lists them as separate releases with the same title) get their own."""
    if r.get("audio") == "atmos":
        return "Dolby Atmos"
    k = (r.get("type") or "").casefold()
    if k in ("album", "lp", "compilation", "mixtape"):
        return "Albums"
    if k in ("ep", "single", "ep/single", "track") or "single" in k:
        return "EPs & Singles"
    return "Other"


def is_artist(src: dict) -> bool:
    """An artist (albums in ARTISTS/, or the older ARTISTS/<name>/), not a label."""
    return (src.get("folder") or "").replace("\\", "/").split("/")[0] == "ARTISTS"


def newest_first(rows: list[dict]) -> list[dict]:
    """Releases by date, newest first. One without a date (a Bandcamp release
    not matched yet) stays by its neighbours in the source's own order."""
    dated = [r["date"] for r in rows]
    for i, d in enumerate(dated):  # undated: the date of the nearest dated one above, else below
        if not d:
            dated[i] = next((x for x in reversed(dated[:i]) if x), None) or next((x for x in dated[i + 1:] if x), "")
    order = sorted(range(len(rows)), key=lambda i: dated[i], reverse=True)  # stable: ties keep the source's order
    return [rows[i] for i in order]


def selectable(r: dict) -> bool:
    """On the service, not queued, not downloaded (or downloaded but broken),
    and not already in another label/artist of yours."""
    other = r.get("elsewhere")
    return r["status"] == "matched" and not r["wanted"] and (not r["downloaded"] or bool(r.get("failed"))) and (not other or other[1] == "listed")


def in_view(r: dict, view: str) -> bool:
    if view == "to download":
        return r["status"] == "matched" and not r["downloaded"]
    if view == "downloaded":
        return bool(r["downloaded"])
    if view == "failed":
        return bool(r["failed"])
    if view == "to review":
        return r["status"] == "review"
    return True


HELP = f"""
  [b]Screens[/b]  (also clickable in the tab bar at the top)
    1  library     labels and artists | categories | releases | songs
    2  downloads   the album downloading now (>), the queue, recent results
    3  search      find an artist on the service and add it
    4  settings    where music goes, how folders and files are named, quality, timings
    5  log         new releases, downloads, errors
    6  help        this
    tab / shift-tab   next / previous screen      alt+1…6   switch even while typing

  [b]Moving[/b]
    j k  ↓ ↑      down / up            g G      top / bottom
    h l  ← →      left / right column   enter    open (an album opens its songs)

  [b]Library[/b]
    → l / ← h     into the next column / back (artists → categories → releases → songs)
    space         select / unselect a release (●); on a category, all of it
                  (selections carry across categories: some albums, some singles)
    enter         with albums selected: start downloading them (asks first);
                  otherwise opens the album's songs
    d             unqueue
    a             select every album that isn't downloaded or queued yet (again: unselect)
    c             check files: are all songs on disk? (on an album, or a whole label/artist)
    t             hide / show the songs column (opening an album shows it again)
    f             follow: new releases get queued automatically ([yellow]★[/])
    u             check this label/artist for new releases now
    v             view: all, to download, downloaded, failed, to review

  [b]Album marks[/b]
    [bold white]●[/] selected   [bold yellow]+[/] queued   [green]✔[/] downloaded   [red]✗[/] failed / incomplete   · on the service
    (Atmos)  a Dolby Atmos mix (not FLAC): hidden while Settings → skip_atmos is on
    [green]✔[/] also: an album you have from another source (shown after the title, e.g. (bandcamp.com))
    [{DIM}]-[/] not on the service   [magenta]?[/] needs a match (`tidal-daemon check <name>`)
    songs column: [green]9/9[/] all songs on disk, [red]7/9[/] some missing
    [bold red][!][/]           missing tracks: not all on disk, a failed download, or the
                  service's version is shorter than the label's (songs column: [bold red]![/])

  [b]Song marks[/b]
    [green]✔[/] on disk   [red]✗[/] missing   [{DIM}]–[/] the service no longer has it

  [b]Search[/b]
    / or i        start typing        enter   search (on a result: add it)
    esc           stop typing         F       add and follow

  [b]Settings[/b]
    enter         edit (on/off and choices switch straight away)
    enter / esc   save / cancel while editing

  [b]Daemon[/b]
    p             pause / resume downloads (the album in progress finishes)
    S             start the daemon if it isn't running
    q             quit (the daemon keeps going)
"""


class Confirm(ModalScreen[bool]):
    """A Y / N question in a box in the middle of the screen."""

    DEFAULT_CSS = """
    Confirm { align: center middle; background: ansi_default 0%; }
    Confirm > Static { width: auto; max-width: 90%; height: auto; padding: 1 3; border: round ansi_white; background: ansi_default; color: ansi_white; }
    """
    BINDINGS = [Binding("y", "answer(True)", show=False), Binding("n,escape", "answer(False)", show=False)]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble((self.question, "bold"), "   ", ("Y", "bold green"), " / ", ("N", "bold red")))

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class TidalDaemonApp(App):
    TITLE = "tidal-daemon"
    CSS = f"""
    Screen {{ background: ansi_default; color: ansi_white; }}
    ContentSwitcher {{ height: 1fr; }}
    DataTable, RichLog, #help {{
        background: ansi_default;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-color: ansi_bright_black;
        scrollbar-color-hover: ansi_white;
        scrollbar-color-active: ansi_white;
        scrollbar-corner-color: ansi_default;
    }}
    DataTable > .datatable--cursor {{ background: ansi_default; text-style: reverse; }}
    DataTable > .datatable--hover {{ background: ansi_default; }}
    DataTable:blur > .datatable--cursor {{ text-style: none; color: ansi_white; background: ansi_bright_black; }}
    #tabs {{
        height: 1; background: ansi_default;
        link-style: none; link-style-hover: none; link-background-hover: ansi_default; link-color-hover: ansi_white;
    }}
    #sources {{ width: 26; border-right: vkey ansi_bright_black; }}
    #groups {{ width: 21; border-right: vkey ansi_bright_black; }}
    #releases {{ width: 1fr; border-right: vkey ansi_bright_black; }}
    #tracks {{ width: 1fr; }}
    #hits {{ height: 1fr; }}
    #searchbar {{ height: 1; background: ansi_default; border-top: none; }}
    #slash {{ width: 2; padding: 0 0 0 1; color: ansi_white; text-style: bold; display: none; }}
    #searchbar.typing #slash {{ display: block; }}
    #query {{ border: none; height: 1; padding: 0 1; background: ansi_default; color: ansi_white; }}
    #query > .input--placeholder {{ color: ansi_bright_black; }}
    #query > .input--cursor {{ background: ansi_white; color: ansi_black; }}
    #settings-table {{ height: auto; max-height: 60%; }}
    #setting-help {{ padding: 1 1 0 1; background: ansi_default; color: ansi_white; }}
    #setting-input {{ border: none; height: 1; padding: 0 1; background: ansi_default; display: none; }}
    #hits > .datatable--header {{ background: ansi_default; color: ansi_bright_black; text-style: bold; }}
    #help, #log {{ background: ansi_default; padding: 0 1; }}
    #bar, #status {{ height: 1; background: ansi_default; }}
    #hint {{ height: 1; padding: 0 1; background: ansi_default; color: ansi_bright_black; }}
    """
    BINDINGS = [
        *[Binding(str(i + 1), f"screen('{name}')", show=False) for i, name in enumerate(SCREENS)],
        Binding("tab", "cycle(1)", show=False, priority=True),
        Binding("shift+tab", "cycle(-1)", show=False, priority=True),
        Binding("j,down", "move(1)", show=False),
        Binding("k,up", "move(-1)", show=False),
        Binding("g,home", "edge(0)", show=False),
        Binding("G,end", "edge(1)", show=False),
        Binding("h", "column(0)", show=False),
        Binding("l", "column(1)", show=False),
        # The tables use ←/→ for horizontal scrolling; take them first
        # (action_column hands them back to a text box being typed in).
        Binding("left", "column(0)", show=False, priority=True),
        Binding("right", "column(1)", show=False, priority=True),
        Binding("space", "toggle", show=False),
        Binding("d", "unqueue", show=False),
        Binding("a", "queue_all", show=False),
        Binding("f", "follow", show=False),
        Binding("u", "update", show=False),
        Binding("v", "view", show=False),
        Binding("p", "pause", show=False),
        Binding("F", "add_follow", show=False),
        Binding("slash", "find", show=False),
        Binding("S", "start", show=False),
        Binding("c", "check_files", show=False),
        Binding("t", "toggle_songs", show=False),
        Binding("i", "find", show=False),
        Binding("escape", "stop_typing", show=False),
        *[Binding(f"alt+{i + 1}", f"screen('{name}')", show=False, priority=True) for i, name in enumerate(SCREENS)],
        Binding("q", "quit", show=False),
    ]

    def __init__(self, api: Api):
        super().__init__(ansi_color=True)
        self.api = api
        self.online = False
        self.status: dict = {}
        self.sources: list[dict] = []
        self.source: str | None = None
        self.releases: list[dict] = []
        self.dl_rows: list[dict] = []
        self.hits: list[dict] = []
        self.selected: dict[str, set[str]] = {}  # source -> rkeys picked, not queued yet
        self.group = "All"  # category shown in the releases column
        self.all_releases: list[dict] = []  # the source's releases, before the category filter
        self.release_cache: dict[str, list[dict]] = {}  # source -> releases, as last fetched
        self._status_busy = self._data_busy = self._data_again = False
        self.track_cache: dict[tuple[str, str], dict] = {}  # (source, rkey) -> songs, as last fetched
        self.settings_rows: list[dict] = []
        self.naming_fields: dict[str, str] = {}
        self.editing: str | None = None  # setting key being edited
        self.songs_for: tuple[str, str] | None = None  # (source, rkey) shown in the songs column
        self.view = "all"
        self.last_event = 0
        self.sigs: dict[str, str] = {}
        self.adding: set[str] | None = None  # sources before an add: the new one gets selected

    def compose(self) -> ComposeResult:
        yield Static("", id="tabs")
        with ContentSwitcher(initial="library"):
            yield DataTable(id="downloads", show_header=False, cursor_type="row")
            with Horizontal(id="library"):
                yield DataTable(id="sources", show_header=False, cursor_type="row")
                yield DataTable(id="groups", show_header=False, cursor_type="row")
                yield DataTable(id="releases", show_header=False, cursor_type="row")
                yield DataTable(id="tracks", show_header=False, cursor_type="row")
            with Vertical(id="search"):
                yield DataTable(id="hits", show_header=True, cursor_type="row")
                with Horizontal(id="searchbar"):
                    yield Static("/", id="slash")
                    yield Input(placeholder="press / to type an artist name (or paste a link)", id="query")
            with Vertical(id="settings"):
                yield DataTable(id="settings-table", show_header=True, cursor_type="row")
                yield Input(id="setting-input")
                yield Static("", id="setting-help")
            yield Static(HELP, id="help")
            yield RichLog(id="log", markup=False, wrap=True)
        yield Static("", id="hint")  # what-next, above the bar, like the search bar's hint
        yield Static("", id="bar")
        yield Static("", id="status")

    def on_mount(self) -> None:
        self.query_one("#downloads", DataTable).add_columns("", "artist", "title", "where", "tracks")
        self.query_one("#sources", DataTable).add_columns("", "name", "count")
        self.query_one("#groups", DataTable).add_columns("", "category", "n")
        self.query_one("#releases", DataTable).add_columns("", "title", "year", "songs", "artist")
        self.query_one("#tracks", DataTable).add_columns("", "no", "title", "artist", "time")
        self.query_one("#settings-table", DataTable).add_columns(fit("Setting", 16), "Value")
        self.query_one("#hits", DataTable).add_columns(fit("Artist", 26), fit("ID", 10), "Albums (a few, to tell same-named artists apart)")
        self.action_screen("library")
        self.draw_tabs()
        if not load_prefs().get("songs_column", True):
            self._show_songs(False)
        self.tick()
        self.set_interval(1, self.tick)
        self.set_interval(3, self.refresh_data)

    # ------------------------------------------------------------------ status bar

    def tick(self) -> None:
        # One status request at a time: a slow daemon gets asked again only
        # after it answered (cancelling would mean it never gets to answer).
        if not self._status_busy:
            self._status_busy = True
            self._fetch_status()

    @work(thread=True, group="status")
    def _fetch_status(self) -> None:
        try:
            st = self.api.get("/status")
        except (urllib.error.URLError, OSError):
            st = None
        self.call_from_thread(self._apply_status, st)

    def _apply_status(self, st: dict | None) -> None:
        """ncmpcpp's classic status bar: progress bar, then what's going on."""
        self._status_busy = False
        if st is None:
            if self.online or not self.status:
                self.online = False
                self.status = {"offline": True}
                self.query_one("#bar", Static).update(Text("─" * self.size.width, style=DIM))
                self.query_one("#status", Static).update(
                    Text.assemble(("Daemon not running", "bold red"), " — press S to start it, or run `tidal-daemon serve`")
                )
            return
        self.status = st
        if not self.online:
            self.online = True
            self.refresh_data()
            self.check_setup()
        self.draw_status()

    def draw_status(self) -> None:
        bar, line = self.query_one("#bar", Static), self.query_one("#status", Static)
        st = self.status
        if st.get("offline"):
            return
        p = st.get("progress") or {}
        width = max(self.size.width, 10)
        frac = min(p["done"] / p["total"], 1.0) if p.get("total") else 0.0
        filled = int(frac * (width - 1))
        bar.update(Text.assemble(("─" * filled + ("╼" if p else ""), "white"), ("─" * (width - filled - (1 if p else 0)), DIM)))

        right = Text()
        if p.get("total"):
            right.append(f"[{p['done']}/{p['total']}] ", style="white")
        right.append(f"[queue {st['queue']}]", style="white")
        if st.get("paused"):
            right.append(" [paused]", style="bold yellow")
        if st.get("needs_setup"):
            left = Text("Waiting for setup: no music.db yet", style="bold yellow")
        elif p:
            left = Text.assemble(("Downloading: ", "bold"), (p["label"], "white"))
        elif st.get("current"):
            left = Text.assemble((st["current"][:1].upper() + st["current"][1:], "white"))
        else:
            left = Text("Idle" + (" — downloads paused" if st.get("paused") else ""), style=DIM)
        # A message (from notify) sits just left of the counters, cut short
        # rather than overlapping the activity text.
        msg = self._message()
        left = left[: max(width - len(right.plain) - 1, 0)]
        room = width - len(left.plain) - len(right.plain) - 4  # at least 2 spaces each side
        if msg is not None and room > 3:
            text, style = msg
            text = text if len(text) <= room else text[: room - 1] + "…"
            right = Text.assemble((text, style), "  ", right)
        pad = max(width - len(left.plain) - len(right.plain), 1)
        line.update(Text.assemble(left, " " * pad, right))

    def notify(self, message, *, title: str = "", severity: str = "information", timeout: float | None = None, markup: bool = True) -> None:
        """No pop-up toasts: the message shows as plain text at the bottom right
        of the status line for a few seconds."""
        import time

        self._flash = (str(message), "bold white" if severity == "error" else "bright_black", time.monotonic() + (timeout or 5))
        if self.status:
            self.draw_status()
        self.set_timer((timeout or 5) + 0.1, self.draw_status)

    def _message(self) -> tuple[str, str] | None:
        import time

        flash = getattr(self, "_flash", None)
        if flash is None or time.monotonic() > flash[2]:
            return None
        return flash[0], flash[1]

    # ------------------------------------------------------------------ data

    def refresh_data(self) -> None:
        """Fetch what the current screen shows, in the background. While a
        fetch is still running, just remember to fetch once more after it."""
        if not self.online:
            return
        if self._data_busy:
            self._data_again = True
            return
        self._data_busy = True
        self._fetch_data(self.query_one(ContentSwitcher).current, self.source, self.last_event, bool(self.editing))

    @work(thread=True, group="data")
    def _fetch_data(self, screen: str, source: str | None, after: int, editing: bool) -> None:
        # The requests go out together: a busy daemon costs one wait, not one per request.
        asks = {"sources": ("/sources", {}), "events": ("/events", {"after": after})}
        if screen == "downloads":
            asks["queue"] = ("/queue", {})
        elif screen == "library" and source:
            asks["releases"] = ("/releases", {"source": source})
        elif screen == "settings" and not editing:
            asks["settings"] = ("/settings", {})

        def one(item):
            name, (path, q) = item
            try:
                return name, self.api.get(path, **q)
            except (urllib.error.URLError, OSError):
                return name, None

        with ThreadPoolExecutor(max_workers=len(asks)) as pool:
            got = {name: data for name, data in pool.map(one, asks.items()) if data is not None}
        if "releases" in got:
            got["releases"] = (source, got["releases"])
        self.call_from_thread(self._apply_data, got)

    def _apply_data(self, got: dict) -> None:
        self._data_busy = False
        if self._data_again:  # the screen or source changed meanwhile
            self._data_again = False
            self.call_later(self.refresh_data)
        if "sources" in got:
            self.render_sources(got["sources"])
        if "queue" in got:
            self.render_downloads(got["queue"])
        if "releases" in got:
            source, rows = got["releases"]
            self.release_cache[source] = rows
            if source == self.source:
                self.render_releases()
        if "settings" in got and not self.editing:
            self.render_settings(got["settings"])
        if "events" in got:
            self.render_log(got["events"])

    def _changed(self, name: str, data) -> bool:
        sig = json.dumps(data, sort_keys=True, default=str)
        if self.sigs.get(name) == sig:
            return False
        self.sigs[name] = sig
        return True

    @staticmethod
    def _refill(t: DataTable, rows: list[tuple], keys: list[str]) -> None:
        """Replace a table's rows, keeping the cursor where it was."""
        cursor = t.cursor_row
        t.clear()
        for cells, k in zip(rows, keys):
            t.add_row(*cells, key=k)
        if rows:
            t.move_cursor(row=min(cursor, len(rows) - 1), animate=False)

    def render_sources(self, sources: list[dict]) -> None:
        self.sources = sources
        # Labels first, then artists (folder ARTISTS/...), each alphabetical.
        self.sources.sort(key=lambda s: (is_artist(s), s["name"].casefold()))
        if self._changed("sources", self.sources):
            rows = []
            for s in self.sources:
                c = s["counts"]
                artist = is_artist(s)
                # a label folder in parentheses, as on disk; one named otherwise (OST) shows plain
                folder = s.get("folder") or ""
                name = s["name"] if artist or (folder and not (folder.startswith("(") and folder.endswith(")"))) else f"({s['name']})"
                count = Text(
                    f"{c.get('owned', 0)} local" if s.get("kind") == "local" else f"{c.get('downloaded', 0)}/{c.get('matched', 0)}",
                    style=DIM,
                )
                if c.get("queued"):
                    count.append(f" +{c['queued']}", style="yellow")
                rows.append((Text("★" if s["follow"] else " ", style="yellow"), Text(fit(name, 22), style=ARTIST if artist else "white"), count))
            self._refill(self.query_one("#sources", DataTable), rows, [s["key"] for s in self.sources])
        if self.adding is not None:
            new = [s["key"] for s in self.sources if s["key"] not in self.adding]
            if new:
                self.adding = None
                self.action_screen("library")
                self.query_one("#sources", DataTable).move_cursor(row=[s["key"] for s in self.sources].index(new[0]))
                self.select(new[0])
                self.notify(f"added — space queues a release, a queues all of them")
        if self.source is None and self.sources:
            self.select(self.sources[0]["key"])

    def select(self, key: str) -> None:
        if key != self.source:
            self.source = key
            self.group = "All"
            self.sigs.pop("releases", None)
            self.load_releases()

    def load_releases(self) -> None:
        """Draw the source's releases now (from the cache), then refresh them."""
        if not self.source:
            return
        self.render_releases()
        self._fetch_releases(self.source)

    @work(thread=True, exclusive=True, group="releases")
    def _fetch_releases(self, source: str) -> None:
        try:
            rows = self.api.get("/releases", source=source)
        except (urllib.error.URLError, OSError):
            return
        self.call_from_thread(self._apply_releases, source, rows)

    def _apply_releases(self, source: str, rows: list[dict]) -> None:
        self.release_cache[source] = rows
        if source == self.source:
            self.render_releases()

    def _title_cell(self, r: dict) -> Text:
        """Title, plus (Atmos) for Dolby mixes and (bandcamp.com) etc. for albums
        whose SOURCE isn't this tool's own tag."""
        extra = []
        if r.get("audio") == "atmos":
            extra.append("Atmos")
        tag = r.get("source_tag")
        if tag and tag != self.status.get("source_tag"):
            extra.append(tag)
        if r.get("elsewhere") and not r.get("downloaded") and r["status"] != "owned":
            # "in VISION": you have it there; "VISION": a label release, downloads into its folder
            extra.append(r["elsewhere"][0] if r["elsewhere"][1] == "listed" else f"in {r['elsewhere'][0]}")
        suffix = "".join(f" ({x})" for x in extra)
        flag = "[!] " if lacking(r) else ""
        return Text.assemble((flag, "bold red"), (fit(r["title"], max(34 - len(suffix) - len(flag), 8)), ALBUM), (suffix, DIM))

    def _artist_cell(self, r: dict) -> Text:
        """The release's artist, only when it isn't just this artist's own
        (a collaboration, a guest, someone else's release). Labels: always."""
        src = self._source() or {}
        main = src.get("name") if is_artist(src) else None
        artist = r.get("artist") or ""
        if main and norm(artist) == norm(main):
            return Text("")
        if src.get("kind") == "artist":  # the service gives a list, joined ", ": show it the archive's way
            artist = artist.replace(", ", "; ")
        return Text(fit(artist, 30), style=DIM if main else ARTIST)

    def render_releases(self) -> None:
        if not self.source:
            return
        skip_atmos = self.status.get("skip_atmos", True)
        everything = newest_first([
            r for r in self.release_cache.get(self.source, []) if in_view(r, self.view) and not (skip_atmos and r.get("audio") == "atmos")
        ])
        sel = self.selected.get(self.source, set())
        if not self._changed("releases", [self.source, self.view, self.group, everything, sorted(sel)]):
            return
        self.all_releases = everything

        # category column: All, then the categories this source has
        present = [c for c in CATEGORIES if c == "All" or any(category(r) == c for r in everything)]
        if self.group not in present:
            self.group = "All"
        gcells = []
        for c in present:
            members = [r for r in everything if c == "All" or category(r) == c]
            picked = sum(1 for r in members if r["rkey"] in sel)
            gcells.append(
                (
                    Text("●" if picked else " ", style="bold white"),
                    Text(fit(c, 14), style="white"),
                    Text(f"{picked}/{len(members)}" if picked else str(len(members)), style=DIM),
                )
            )
        gt = self.query_one("#groups", DataTable)
        cursor = gt.cursor_row
        gt.clear()
        for cells, c in zip(gcells, present):
            gt.add_row(*cells, key=c)
        gt.move_cursor(row=present.index(self.group) if self.focused is not gt else min(cursor, len(present) - 1), animate=False)

        rows = [r for r in everything if self.group == "All" or category(r) == self.group]
        self.releases = rows
        def songs(r: dict) -> Text:
            if r.get("needed"):
                ok = r["present"] >= r["needed"]
                return Text(f"{r['present']}/{r['needed']}".rjust(5), style="green" if ok else "red")
            return Text(str(r.get("tracks") or "").rjust(5), style=NUM)

        cells = [
            (
                mark(r, r["rkey"] in sel),
                self._title_cell(r),
                Text((r["date"] or "")[:4].ljust(4), style=YEAR),
                songs(r),
                self._artist_cell(r),
            )
            for r in rows
        ]
        self._refill(self.query_one("#releases", DataTable), cells, [r["rkey"] for r in rows])
        if self.songs_for and self.songs_for[0] == self.source:
            self.load_tracks(self.songs_for[1], fetch=False)

    def load_tracks(self, rkey: str, fetch: bool) -> None:
        """The songs column: every song of the album and whether it's on disk.
        Drawn from the cache at once, then refreshed; fetch=True asks the
        daemon to build the list if it has none yet."""
        if not self.source:
            return
        self.songs_for = (self.source, rkey)
        cached = self.track_cache.get((self.source, rkey))
        if cached is not None:
            self.render_tracks(rkey, cached)
        self._fetch_tracks(self.source, rkey, fetch)

    @work(thread=True, exclusive=True, group="tracks")
    def _fetch_tracks(self, source: str, rkey: str, fetch: bool) -> None:
        try:
            data = self.api.get("/tracks", timeout=60 if fetch else 5, source=source, rkey=rkey, fetch=int(fetch))
        except (urllib.error.URLError, OSError):
            return
        self.call_from_thread(self._apply_tracks, source, rkey, data)

    def _apply_tracks(self, source: str, rkey: str, data: dict) -> None:
        self.track_cache[(source, rkey)] = data
        if self.songs_for == (source, rkey):  # still the album on screen
            self.render_tracks(rkey, data)

    def render_tracks(self, rkey: str, data: dict) -> None:
        rows = data.get("tracks") or []
        if not self._changed("tracks", [rkey, rows, data.get("note"), data.get("short")]):
            return
        glyph = {"present": ("✔", "green"), "missing": ("✗", "red"), "unavailable": ("–", DIM)}
        cells, keys = [], []
        for t in rows:
            d = t.get("duration") or 0
            g, colour = glyph.get(t["state"], ("?", "white"))
            cells.append(
                (
                    Text(g, style=colour),
                    Text((f"{t['disc']}-" if t["disc"] > 1 else "") + f"{t['num']:02d}", style=NUM),
                    Text(fit(t["title"], 30), style="white" if t["state"] == "present" else colour),
                    Text(fit(t["artist"], 18), style=ARTIST),
                    Text(f"{d // 60}:{d % 60:02d}" if d else "", style=DIM),
                )
            )
            keys.append(f"{t['disc']}-{t['num']}")
        for i, title in enumerate(data.get("short") or []):  # on the label's release, not the service's
            cells.append((Text("!", style="bold red"), Text(""), Text(fit(title, 30), style="red"), Text(fit("not on the service", 18), style=DIM), Text("")))
            keys.append(f"short-{i}")
        self._refill(self.query_one("#tracks", DataTable), cells, keys)
        self.hint("" if rows else "enter (or l) loads the song list")

    def draw_tabs(self) -> None:
        """ncmpcpp-like screen list: the current one highlighted, each clickable."""
        current = self.query_one(ContentSwitcher).current
        names = SCREEN_NAMES
        parts = []
        for i, name in enumerate(SCREENS, 1):
            style = "reverse bold" if name == current else "ansi_bright_black"
            parts.append(f"[@click=app.screen('{name}')][{style}] {i}:{names[name]} [/][/]")
        self.query_one("#tabs", Static).update(" ".join(parts))

    def render_downloads(self, q: dict) -> None:
        if not self._changed("downloads", q):
            return
        cur = q.get("current")
        rows, cells = [], []
        if cur:
            artist, _, title = cur["label"].partition(" - ")
            rows.append({"kind": "current"})
            done = f"{cur['done']}/{cur['total']}" if cur.get("total") else f"{cur['done']}"
            cells.append((Text("> ", style="bold white"), Text(fit(artist, 22), style="bold " + ARTIST), Text(fit(title, 40), style="bold white"), Text(fit(cur["source"], 18), style=KIND), Text(done, style="bold " + NUM)))
        for item in q["queued"]:
            if cur and f"{item['artist']} - {item['title']}" == cur["label"]:
                continue
            rows.append({"kind": "queued", **item})
            cells.append(
                (
                    Text("  "),
                    Text(fit(item["artist"], 22), style=ARTIST),
                    Text(fit(item["title"], 40), style=ALBUM),
                    Text(fit(item["source_name"], 18), style=KIND),
                    Text(("retry " if item.get("failed") else "") + str(item.get("tracks") or ""), style="yellow" if item.get("failed") else NUM),
                )
            )
        if not rows:
            rows.append({"kind": "note"})
            cells.append((Text("  "), Text("nothing queued", style=DIM), Text("— select releases in the Library (1)", style=DIM), Text(""), Text("")))
        rows.append({"kind": "sep"})
        cells.append((Text(""), Text("── recent ──", style=DIM), Text(""), Text(""), Text("")))
        for ev in reversed(q["recent"]):
            ok = ev["level"] == "done"
            msg = ev["message"].split(": ", 1)[-1]
            artist, _, title = msg.partition(" - ")
            rows.append({"kind": "recent"})
            cells.append((Text("✔ " if ok else "✗ ", style="green" if ok else "red"), Text(fit(artist, 22), style=DIM), Text(fit(title, 40), style="white" if ok else "red"), Text(fit(ev["source"] or "", 18), style=DIM), Text(ev["ts"][5:16].replace("T", " "), style=DIM)))
        self.dl_rows = rows
        self._refill(self.query_one("#downloads", DataTable), cells, [str(i) for i in range(len(cells))])

    def render_log(self, events: list[dict]) -> None:
        log = self.query_one("#log", RichLog)
        for ev in events:
            if ev["id"] <= self.last_event:
                continue
            self.last_event = ev["id"]
            style = {"error": "red", "warn": "yellow", "done": "green", "new": "bold cyan"}.get(ev["level"], "white")
            log.write(Text.assemble((ev["ts"][5:16].replace("T", " ") + " ", DIM), (fit(ev["source"] or "-", 15) + " ", KIND), (ev["message"], style)))

    # ------------------------------------------------------------------ helpers

    def _post(self, path: str, then=None, **body) -> None:
        """Send an action to the daemon in the background; `then` runs (on the
        UI thread) once it's accepted."""
        self._send(path, body, then)

    @work(thread=True, group="post")
    def _send(self, path: str, body: dict, then) -> None:
        try:
            self.api.post(path, **body)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except Exception:
                msg = str(e)
            self.call_from_thread(self.notify, msg, severity="error")
            return
        except (urllib.error.URLError, OSError) as e:
            self.call_from_thread(self.notify, f"daemon unreachable: {e}", severity="error")
            return
        self.call_from_thread(self._posted, then)

    def _posted(self, then) -> None:
        self.sigs.clear()
        self.refresh_data()
        if self.source:
            self._fetch_releases(self.source)
        if then is not None:
            then()

    def _table(self) -> DataTable | None:
        f = self.focused
        return f if isinstance(f, DataTable) else None

    def _release(self, any_focus: bool = False) -> dict | None:
        t = self.query_one("#releases", DataTable)
        if not (any_focus or self.focused in (t, self.query_one("#tracks"))):
            return None
        if not self.releases or t.cursor_row >= len(self.releases):
            return None
        return self.releases[t.cursor_row]

    def _source(self) -> dict | None:
        return next((s for s in self.sources if s["key"] == self.source), None)

    # ------------------------------------------------------------------ navigation

    def hint(self, text: str) -> None:
        """A grey line above the progress bar (where Search has its search bar); Library only."""
        on_library = self.query_one(ContentSwitcher).current == "library"
        # The line always keeps its place (an empty line when there's nothing to
        # say), so the screen never jumps as hints come and go.
        self.query_one("#hint", Static).update(text if on_library else "")

    def action_screen(self, name: str) -> None:
        self.query_one(ContentSwitcher).current = name
        self.draw_tabs()
        if name != "library":
            self.hint("")
        # Search opens on the results, not the text box, so number keys still
        # switch screens; / or i starts typing.
        focus = {
            "downloads": "#downloads",
            "library": "#releases" if self.source else "#sources",
            "search": "#hits",
            "settings": "#settings-table",
            "log": "#log",
        }.get(name)
        if focus:
            self.query_one(focus).focus()
        self.sigs.pop(name, None)
        self.refresh_data()

    def action_cycle(self, step: int) -> None:
        if self.focused is self.query_one("#query", Input) and step == 1 and self.hits:
            self.query_one("#hits").focus()
            return
        i = SCREENS.index(self.query_one(ContentSwitcher).current)
        self.action_screen(SCREENS[(i + step) % len(SCREENS)])

    def action_move(self, step: int) -> None:
        t = self._table()
        if t is not None:
            t.action_cursor_down() if step > 0 else t.action_cursor_up()
        elif self.focused is self.query_one("#log"):
            self.query_one("#log").scroll_relative(y=step)

    def action_edge(self, end: int) -> None:
        t = self._table()
        if t is not None and t.row_count:
            t.move_cursor(row=t.row_count - 1 if end else 0)

    def action_column(self, right: int) -> None:
        if isinstance(self.focused, Input):  # typing: arrows move the text cursor
            self.focused.action_cursor_right() if right else self.focused.action_cursor_left()
            return
        if self.query_one(ContentSwitcher).current != "library":
            return
        cols = ["sources", "groups", "releases", "tracks"]
        here = self.focused.id if self.focused is not None and self.focused.id in cols else "releases"
        i = max(0, min(len(cols) - 1, cols.index(here) + (1 if right else -1)))
        if cols[i] == "tracks":
            self._open_album()
        else:
            self.query_one(f"#{cols[i]}").focus()

    def _open_album(self) -> None:
        r = self._release(any_focus=True)
        if r is None or r["status"] != "matched":
            return
        self._show_songs(True)
        self.load_tracks(r["rkey"], fetch=True)
        self.query_one("#tracks").focus()

    def _show_songs(self, show: bool) -> None:
        t = self.query_one("#tracks", DataTable)
        if t.display == show:
            return
        t.display = show
        # without the songs column the releases column needs no right edge
        self.query_one("#releases").styles.border_right = ("vkey", "ansi_bright_black") if show else None
        if not show and self.focused is t:
            self.query_one("#releases").focus()
        save_prefs(songs_column=show)

    def action_toggle_songs(self) -> None:
        if self.query_one(ContentSwitcher).current == "library":
            show = not self.query_one("#tracks", DataTable).display
            self._show_songs(show)
            self.notify("songs column shown" if show else "songs column hidden — t brings it back")

    def action_find(self) -> None:
        self.action_screen("search")
        self.query_one("#query", Input).focus()

    def on_descendant_focus(self, event) -> None:
        self._searchbar_state()

    def on_descendant_blur(self, event) -> None:
        self._searchbar_state()

    def _searchbar_state(self) -> None:
        """The bar at the bottom of Search: a grey hint, or a white "/" prompt while typing."""
        try:
            bar, box = self.query_one("#searchbar"), self.query_one("#query", Input)
        except Exception:
            return
        typing = self.focused is box
        bar.set_class(typing, "typing")
        box.placeholder = "" if typing else "press / to type an artist name (or paste a link)"

    def action_stop_typing(self) -> None:
        """esc: leave the search bar; the vim keys work on the results again."""
        if self.focused is self.query_one("#query", Input):
            self.query_one("#hits").focus()
        elif self.editing:
            self._end_edit()

    @on(DataTable.RowHighlighted, "#sources")
    def _source_moved(self, event: DataTable.RowHighlighted) -> None:
        k = self._cursor_key(self.query_one("#sources", DataTable))
        if k:
            self.select(k)

    @staticmethod
    def _cursor_key(t: DataTable) -> str | None:
        """The row the cursor is on now. Rebuilding a table moves the cursor
        through row 0 first; acting on those stale events would make tables
        that rebuild each other ping-pong forever."""
        keys = [k.value for k in t.rows]
        return keys[t.cursor_row] if keys and 0 <= t.cursor_row < len(keys) else None

    @on(DataTable.RowHighlighted, "#groups")
    def _group_moved(self, event: DataTable.RowHighlighted) -> None:
        g = self._cursor_key(self.query_one("#groups", DataTable))
        if g and g != self.group:
            self.group = g
            self.sigs.pop("releases", None)
            self.load_releases()

    @on(DataTable.RowHighlighted, "#releases")
    def _release_moved(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or not event.row_key.value:
            return
        r = next((x for x in self.releases if x["rkey"] == event.row_key.value), None)
        if r is not None and r["status"] == "matched":
            self.load_tracks(r["rkey"], fetch=False)
        else:  # not on the service: there's no song list to show
            self.songs_for = None
            self.sigs.pop("tracks", None)
            self.query_one("#tracks", DataTable).clear()
            self.hint("")

    @on(DataTable.RowSelected)
    def _enter(self, event: DataTable.RowSelected) -> None:
        """Enter (or a click): open a source, open an album's songs, add a search hit."""
        tid = event.data_table.id
        if tid == "sources":
            self.query_one("#groups").focus()
        elif tid == "groups":
            if self.selected.get(self.source):
                self._start_selected()
            else:
                self.query_one("#releases").focus()
        elif tid == "releases":
            if self.selected.get(self.source):
                self._start_selected()
            else:
                self._open_album()
        elif tid == "settings-table":
            self._edit_setting()
        elif tid == "hits":
            if self.hits:
                self._add(follow=False)
            else:
                self.query_one("#query", Input).focus()

    # ------------------------------------------------------------------ library actions

    def action_toggle(self) -> None:
        if self.focused is self.query_one("#hits"):
            return self._add(follow=False)
        if self.focused is self.query_one("#groups"):
            # the whole category: select what can be downloaded; again to unselect
            can = {r["rkey"] for r in self.releases if selectable(r)}
            sel = self.selected.setdefault(self.source, set())
            if can and can <= sel:
                sel -= can
            else:
                sel |= can
            self.load_releases()
            self.notify(f"{self.group}: {len(can & sel)} selected — enter starts the downloads" if can & sel else f"{self.group}: nothing selected")
            return
        r = self._release()
        if r is None:
            return
        if r["status"] != "matched":
            self.notify("not found on the service — nothing to download (match it with `tidal-daemon check`)")
            return
        if r["downloaded"] and not r.get("failed"):
            self.notify("already downloaded")
            return
        if r["wanted"]:
            self.notify("already queued — d removes it from the queue")
            return
        sel = self.selected.setdefault(self.source, set())
        sel.symmetric_difference_update({r["rkey"]})
        self.load_releases()
        t = self.query_one("#releases", DataTable)
        t.move_cursor(row=min(t.cursor_row + 1, max(len(self.releases) - 1, 0)))

    def action_unqueue(self) -> None:
        cur = self.query_one(ContentSwitcher).current
        if cur == "downloads":
            t = self.query_one("#downloads", DataTable)
            row = self.dl_rows[t.cursor_row] if self.dl_rows and t.cursor_row < len(self.dl_rows) else {}
            if row.get("kind") == "queued":
                self._post("/want", source=row["source"], rkeys=[row["rkey"]], wanted=False)
            elif row.get("kind") == "current":
                self.notify("downloading now — it finishes, then the next one starts (p pauses after it)")
            return
        r = self._release()
        if r and r["wanted"] and not r["downloaded"]:
            self._post("/want", source=self.source, rkeys=[r["rkey"]], wanted=False)

    def action_queue_all(self) -> None:
        """Select every release that can still be downloaded; again to unselect."""
        if self.query_one(ContentSwitcher).current != "library" or not self.source:
            return
        can = {r["rkey"] for r in self.releases if r["status"] == "matched" and not r["wanted"] and (not r["downloaded"] or r.get("failed"))}
        sel = self.selected.setdefault(self.source, set())
        if can and can <= sel:
            sel.clear()
        else:
            sel |= can
        self.load_releases()
        self.notify(f"{len(sel)} selected — enter starts the downloads" if sel else "nothing selected")

    def _start_selected(self) -> None:
        """Enter with a selection: confirm, then queue it (the daemon starts downloading)."""
        sel = self.selected.get(self.source, set())
        src = self._source() or {}
        kind = "Artist" if is_artist(src) else "Label"
        n = len(sel)

        def answered(yes: bool) -> None:
            if not yes:
                return

            def queued() -> None:
                sel.clear()
                self.load_releases()
                self.notify(f"{n} release{'s' if n != 1 else ''} queued — progress in Downloads (2)")

            self._post("/want", then=queued, source=self.source, rkeys=sorted(sel), wanted=True)

        self.push_screen(Confirm(f'Start selected downloads for {kind} "{src.get("name", self.source)}"? ({n} release{"s" if n != 1 else ""})'), answered)

    def action_follow(self) -> None:
        if self.query_one(ContentSwitcher).current != "library":
            return
        src = self._source()
        if src:
            msg = f"{src['name']}: " + ("no longer followed" if src["follow"] else "followed ★ — new releases get queued automatically")
            self._post("/follow", then=lambda: self.notify(msg), source=src["key"], follow=not src["follow"])

    def action_update(self) -> None:
        src = self._source()
        if src:
            self._post("/scan", then=lambda: self.notify(f"checking {src['name']} for new releases…"), source=src["key"])

    def action_check_files(self) -> None:
        if self.query_one(ContentSwitcher).current != "library" or not self.source:
            return
        src = self._source()
        if self.focused is self.query_one("#sources"):
            self._post("/verify", then=lambda: self.notify(f"checking every downloaded album of {src['name']} song by song…"), source=self.source)
            return
        r = self._release()
        if r and r["status"] == "matched":
            self._post("/verify", then=lambda: self.notify(f"checking {r['title']} song by song…"), source=self.source, rkey=r["rkey"])

    def action_view(self) -> None:
        if self.query_one(ContentSwitcher).current == "library":
            self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
            self.notify(f"view: {self.view}")
            self.load_releases()

    def action_pause(self) -> None:
        if not self.online:
            return
        paused = bool(self.status.get("paused"))
        self._post("/pause", then=lambda: self.notify("downloads resumed" if paused else "downloads paused (the album in progress finishes)"), paused=not paused)

    def action_start(self) -> None:
        if self.online:
            self.notify("the daemon is already running")
            return
        start_daemon()
        self.notify("starting the daemon…")

    # ------------------------------------------------------------------ settings

    def check_setup(self) -> None:
        self._fetch_setup()

    @work(thread=True, exclusive=True, group="setup")
    def _fetch_setup(self) -> None:
        try:
            st = self.api.get("/setup")
        except (urllib.error.URLError, OSError):
            return
        self.call_from_thread(self._run_setup, st)

    def _run_setup(self, st: dict) -> None:
        """A fresh start: ask before creating the music folder, the artists
        folder and the database (.tidal-daemon/music.db, inside the music folder; a new,
        empty library). A music folder that already
        has a music.db just uses it. Paths are shown the way this system
        writes them."""
        asks: list[tuple[str, str, dict]] = []
        if not st["archive_exists"]:
            asks.append((f"Create directory '{st['archive']}'?", "archive", {}))
        if st.get("artists") and not st["artists_exists"]:
            asks.append((f"Create directory '{st['artists']}'?", "artists", {}))
        if not st["database_exists"]:
            asks.append((f"Create database '{st['database']}'?", "database", {}))
        if not asks:
            return

        def ask(i: int) -> None:
            if i == len(asks):
                self.sigs.clear()
                self.refresh_data()
                return
            question, what, _ = asks[i]

            def answered(yes: bool) -> None:
                extra = asks[i][2]
                if not yes:
                    if what == "archive":
                        self.notify("Nothing created. Set the music folder in Settings (archive); you'll be asked again.", timeout=10)
                        self.action_screen("settings")
                    elif what == "database":
                        self.notify("No database: tidal-daemon can't keep your library without it. Restart the TUI to be asked again.", severity="warning", timeout=10)
                    else:
                        ask(i + 1)
                    return
                self._post("/setup", then=lambda: ask(i + 1), create=what, **extra)

            self.push_screen(Confirm(question), answered)

        ask(0)

    def load_settings(self) -> None:
        self.refresh_data()

    def render_settings(self, data: dict) -> None:
        self.naming_fields = data.get("naming_fields", {})
        rows = data["settings"]
        if not self._changed("settings", rows):
            return
        self.settings_rows = rows
        cells = []
        for r in rows:
            v = r["value"]
            shown = ("on" if v else "off") if r["kind"] == "bool" else (str(v) if v not in ("", None) else "(off)")
            cells.append((Text(fit(r["key"], 16), style=KIND), Text(shown, style="white")))
        self._refill(self.query_one("#settings-table", DataTable), cells, [r["key"] for r in rows])
        self._setting_help()

    def _current_setting(self) -> dict | None:
        t = self.query_one("#settings-table", DataTable)
        if not self.settings_rows or t.cursor_row >= len(self.settings_rows):
            return None
        return self.settings_rows[t.cursor_row]

    def _example_path(self, values: dict) -> str:
        """Where a song would land with these settings, for the preview."""
        sample = {
            "album_artist": "Lone", "album": "Hyperphantasia", "year": "2022", "date": "2022-06-17", "explicit": "",
            "disc": "", "track": "01", "artist": "Lone", "title": "Hyperphantasia", "isrc": "GBXXX2200001",
        }  # fmt: skip
        try:
            artist = values["artist_folder"].format(name="Lone")
            album = values["album_folder"].format(**sample)
            song = values["track_file"].format(**sample) + ".flac"
            label = values["label_folder"].format(name="Dream Catalogue")
        except (KeyError, IndexError, ValueError) as e:
            return f"(pattern error: {e})"
        root = values["archive"].rstrip("/\\")
        sep = "\\" if "\\" in root else "/"  # the daemon's system: C:\... or /home/...

        def path(*parts: str) -> str:
            return sep.join([root, *(x.replace("/", sep).replace("\\", sep) for x in parts)])

        return f"{path(artist, album, song)}\n  a label:  {path(label, album, song)}"

    @on(DataTable.RowHighlighted, "#settings-table")
    def _setting_moved(self, event: DataTable.RowHighlighted) -> None:
        self._setting_help()

    def _setting_help(self, override: str | None = None) -> None:
        r = self._current_setting()
        if r is None:
            return
        values = {x["key"]: x["value"] for x in self.settings_rows}
        if override is not None and self.editing:
            values[self.editing] = override
        text = Text(r["help"] + "\n\n", style="white")
        if r["kind"] == "pattern" and r["key"] in ("album_folder", "track_file"):
            text.append("placeholders:\n", style=DIM)
            for k, v in self.naming_fields.items():
                text.append(f"  {{{k}}}".ljust(18), style=KIND)
                text.append(v + "\n", style=DIM)
            text.append("\n")
        text.append("example:  ", style=DIM)
        text.append(self._example_path(values), style="green")
        self.query_one("#setting-help", Static).update(text)

    def _edit_setting(self) -> None:
        r = self._current_setting()
        if r is None:
            return
        kind = r["kind"]
        if kind == "bool":
            return self._save_setting(r["key"], not r["value"])
        if kind.startswith("choice:"):
            choices = kind[7:].split(",")
            nxt = choices[(choices.index(r["value"]) + 1) % len(choices)] if r["value"] in choices else choices[0]
            return self._save_setting(r["key"], nxt)
        self.editing = r["key"]
        box = self.query_one("#setting-input", Input)
        box.value = str(r["value"])
        box.display = True
        box.focus()
        box.cursor_position = len(box.value)

    @on(Input.Changed, "#setting-input")
    def _setting_typed(self, event: Input.Changed) -> None:
        if self.editing:
            self._setting_help(override=event.value)

    @on(Input.Submitted, "#setting-input")
    def _setting_entered(self, event: Input.Submitted) -> None:
        if self.editing:
            self._save_setting(self.editing, event.value)

    def _save_setting(self, key: str, value) -> None:
        def saved() -> None:
            self.notify(f"{key} saved")
            self._end_edit()
            self.sigs.pop("settings", None)
            self.load_settings()
            if key == "archive":
                self.check_setup()

        self._post("/settings", then=saved, key=key, value=value)

    def _end_edit(self) -> None:
        self.editing = None
        box = self.query_one("#setting-input", Input)
        box.display = False
        self.query_one("#settings-table").focus()
        self._setting_help()

    # ------------------------------------------------------------------ search / add

    @on(Input.Submitted, "#query")
    def _search(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if "://" in text or ".com/" in text or "bandcamp.com" in text:
            self._add(follow=False, url=text)
            return
        self.query_one("#query", Input).placeholder = f"searching for {text}…"
        self._fetch_hits(text)

    @work(thread=True, exclusive=True, group="search")
    def _fetch_hits(self, text: str) -> None:
        try:
            hits = self.api.get("/search_artists", timeout=60, q=text)
        except (urllib.error.URLError, OSError) as e:
            self.call_from_thread(self.notify, f"search failed: {e}", severity="error")
            return
        self.call_from_thread(self._show_hits, text, hits)

    def _show_hits(self, text: str, hits: list[dict]) -> None:
        self.hits = hits
        self._searchbar_state()
        t = self.query_one("#hits", DataTable)
        t.clear()
        for h in self.hits:
            t.add_row(Text(fit(h["name"], 26), style="white"), Text(fit(str(h["id"]), 10), style=DIM), Text(h.get("hint") or "", style=DIM))
        if self.hits:
            t.focus()
        else:
            self.notify(f"no artists found for {text!r}")

    def action_add_follow(self) -> None:
        if self.focused is self.query_one("#hits"):
            self._add(follow=True)

    def _add(self, follow: bool, url: str | None = None) -> None:
        if url is None:
            t = self.query_one("#hits", DataTable)
            if not self.hits or t.cursor_row >= len(self.hits):
                return
            hit = self.hits[t.cursor_row]
            have = next((s for s in self.sources if s.get("artist_id") == str(hit["id"])), None)
            if have:  # already in the library: just go there
                self.action_screen("library")
                keys = [s["key"] for s in self.sources]
                self.query_one("#sources", DataTable).move_cursor(row=keys.index(have["key"]))
                self.select(have["key"])
                self.notify(f"{have['name']} is already in your library")
                return
            url = hit["url"]
        self.adding = {s["key"] for s in self.sources}
        self._post("/add", url=url, label=False, follow=follow)
        self.notify("adding… it opens in the Library (1) when it's ready" + (" — followed ★" if follow else ""))


def run(host: str = "127.0.0.1", port: int | None = None) -> None:
    TidalDaemonApp(Api(host, port or 8737)).run()

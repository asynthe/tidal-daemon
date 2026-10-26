# tidal-daemon

## Pending: move from Windows to Gentoo (started 2026-10-13)

The user is moving this project from Windows to Gentoo. When they open it on
Gentoo, help them finish the steps below, then delete this section.

Where things are now (since commit 600a63e):
- Everything tidal-daemon keeps is in `<music folder>/.tidal-daemon/`:
  `music.db` (the library), `tidaler/` (Tidal login: token.json,
  settings.json), `locks/`, `daemon.log`, rename-plan.txt, phone-delete.sh.
- Only `config.json` (where the music folder is) and `tui.json` (TUI look)
  live outside it, in `~/.config/tidal-daemon/` (`%APPDATA%\tidal-daemon\`
  on Windows).
- Before the move, copies of config.json, tui.json and the tidaler login were
  put into `.tidal-daemon/` so they travel with it. On Windows the music
  folder was `~/Desktop/archive/music` (~842G). The user plans to re-download
  music on Gentoo, using the library in music.db.

Steps on Gentoo:
1. `git clone --recurse-submodules` the repo (GitHub/GitLab `tidal-daemon`),
   so the `tidaler` submodule comes too.
2. Put the music folder somewhere, with the saved `.tidal-daemon/` inside it.
3. Copy `.tidal-daemon/config.json` and `tui.json` to
   `~/.config/tidal-daemon/`, then set `"archive"` in config.json to the new
   music folder path (it still says `~/Desktop/archive/music`).
4. `./tidal-daemon serve` (first run builds `.venv`). The login is read from
   `.tidal-daemon/tidaler/`, so no new login should be needed; if it is,
   `./tidal-daemon login`.
5. Needs Python 3.12/3.13, FFmpeg on PATH, and for `sync-phone`
   `dev-util/android-tools` (adb on PATH; cli.py otherwise only looks in a
   Windows WinGet path).
6. Check that release/source folder paths in music.db still resolve under the
   new music folder (not verified before the move).

Things to know:
- `tidal-daemon.cmd` is Windows-only; use the `tidal-daemon` script.
- Known Windows-only bug, left alone: a daemon whose stdout goes to a file
  (as when started from the TUI) can crash on non-ASCII log text (cp1252).
  It shouldn't happen on Linux with a UTF-8 locale.
- The repo's old `.cache/` and `library.db` aren't used any more and needn't
  be copied.

## Testing

The user often has their own `tidal-daemon serve`/`tui` running. Never test
against the live music.db or port 8737: copy music.db to a scratch
`.tidal-daemon/` folder, set `paused` true in the copy, and run
`TIDAL_DAEMON_DB=<copy> tidal-daemon serve --port 8799`.

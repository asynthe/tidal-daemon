# tidal-daemon

Label and artist discographies, matched to a music service and kept downloaded.

Point it at a record label (Bandcamp or Shopify store), an artist, or a plain
list of releases. It scrapes the release list, matches each release against
the backend's catalogue (asking when it isn't sure), and downloads what you
want as FLAC into a tidy music folder. Run as a daemon, it re-checks followed
sources for new releases and works through the download queue. A TUI connects
to the daemon to browse, search, add sources and tick releases for download.

## Setup

```sh
git clone --recurse-submodules <this repo>
cd tidal-daemon
./tidal-daemon login      # first run builds .venv from the tidaler submodule
./tidal-daemon tui        # creates the library on first run
./tidal-daemon serve      # the daemon
```

On Windows, use `tidal-daemon.cmd`. Requires Python 3.12 or 3.13 and FFmpeg on the PATH.

## Commands

```
tidal-daemon serve                daemon: re-checks followed sources, downloads the queue
tidal-daemon tui                  open the TUI (connects to the daemon)
tidal-daemon add <url>            add an artist/label from a service artist link or Bandcamp page
tidal-daemon follow <name>        watch it for new releases and download them
tidal-daemon check <name>         refresh + match against the backend, asking when unsure
tidal-daemon dl <name>            download matched releases not downloaded yet
tidal-daemon verify <name>        check downloads: every track there, readable, FLAC
```

`tidal-daemon -h` lists the rest.

Everything lives in one SQLite file, `.tidal-daemon/music.db`, inside the music folder.

## Credits

This code uses [tidaler](https://github.com/maya-doshi/tidaler), so props to
them: it does the heavy lifting here (login, catalogue search, downloads and
tagging). tidal-daemon is the layer on top that tracks labels and artists and
keeps their releases downloaded. If this is useful to you, go star tidaler.

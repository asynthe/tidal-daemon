"""Where release lists come from. Each source kind turns a label's or
artist's page into a list of entries:

    bandcamp  a Bandcamp /music grid (scraped)
    shopify   a label shop on the Shopify Storefront API (scraped)
    artist    an artist's discography straight from a backend: already matched
    file      a hand-written list, one "Artist - Title" per line
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import library
from .library import entry, say

UA = "Mozilla/5.0"


# --------------------------------------------------------------------------- http


def _open(req, tries: int = 4) -> bytes:
    """urlopen with backoff. Label CDNs drop connections under rapid repeats,
    which surfaces as a transient SSL EOF rather than an HTTP status."""
    for attempt in range(1, tries + 1):
        try:
            return urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:
            if attempt == tries:
                raise
            wait = 2**attempt
            say(f"  {type(e).__name__}, retrying in {wait}s ({attempt}/{tries - 1})")
            time.sleep(wait)


def get(url: str) -> str:
    return _open(urllib.request.Request(url, headers={"User-Agent": UA})).decode("utf-8", "replace")


def get_curl(url: str) -> str:
    """GET via the curl binary. Bandcamp serves Python's TLS client a bot
    challenge page instead of the content, but lets curl through."""
    try:
        out = subprocess.run(["curl", "-sSL", "--fail", "--retry", "3", "-A", UA, url], capture_output=True, check=True, timeout=60)
    except FileNotFoundError:
        sys.exit("curl not found — Bandcamp sources need it on PATH")
    except subprocess.CalledProcessError as e:
        sys.exit(f"curl {url}: {e.stderr.decode(errors='replace').strip()}")
    return out.stdout.decode("utf-8", "replace")


def post_json(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": UA, **headers}
    )
    return json.loads(_open(req))


# --------------------------------------------------------------------------- shopify

SHOPIFY_QUERY = """
query products($after: String) {
  products(first: 60, after: $after, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      title handle productType tags vendor
      releaseDate: metafield(namespace: "custom", key: "releaseDate") { value }
      artists: metafield(namespace: "custom", key: "artists") {
        references(first: 10) {
          nodes { ... on Metaobject { name: field(key: "name") { value } } }
        }
      }
    }
  }
}
"""


def discover_shopify(site: str) -> tuple[str, str]:
    """Pull the storefront endpoint and public token out of the site's own JS
    config — client-side values every visitor receives."""
    page = get(site)
    token = re.search(r'X-Shopify-Storefront-Access-Token"\s*:\s*"([0-9a-f]{32})"', page) or re.search(
        r'storefrontAccessToken"?\s*:\s*"([0-9a-f]{32})"', page
    )
    host = re.search(r'host:"(https?://[^"]*shopify[^"]*graphql[^"]*)"', page)
    if not token:
        sys.exit(f"{site}: no Shopify storefront token found — set endpoint/token on the source")
    return (host.group(1) if host else site.rstrip("/") + "/api/shopify/graphql"), token.group(1)


def fetch_shopify(cfg: dict) -> list[dict]:
    endpoint, token = cfg.get("endpoint"), cfg.get("token")
    if not (endpoint and token):
        endpoint, token = discover_shopify(cfg["site"])
    types = set(cfg.get("types") or ["Album", "EP", "Single", "Compilation", "LP", "Mixtape"])

    nodes, after = [], None
    while True:
        payload = post_json(
            endpoint, {"query": SHOPIFY_QUERY, "variables": {"after": after}}, {"X-Shopify-Storefront-Access-Token": token}
        )
        if "errors" in payload:
            sys.exit(f"GraphQL error: {payload['errors']}")
        page = payload["data"]["products"]
        nodes.extend(page["nodes"])
        say(f"  scraped {len(nodes)}")
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    out, skipped = [], 0
    for n in nodes:
        artists = [
            a["name"]["value"] for a in (n.get("artists") or {}).get("references", {}).get("nodes", []) if a.get("name")
        ]
        if n["productType"] not in types or not artists:
            skipped += 1
            continue
        out.append(
            entry(
                artist=" & ".join(artists),
                title=n["title"],
                kind=n["productType"],
                date=(n.get("releaseDate") or {}).get("value"),
                source_url=f"{cfg['site'].rstrip('/')}/releases/{n['handle']}",
            )
        )
    say(f"  {len(out)} releases ({skipped} non-music/creditless skipped)")
    return out


# --------------------------------------------------------------------------- bandcamp


def bandcamp_name(site: str) -> str:
    page = get_curl(site.rstrip("/") + "/music")
    og = re.search(r'<meta property="og:site_name" content="([^"]*)"', page)
    return html.unescape(og.group(1)) if og else ""


def fetch_bandcamp(cfg: dict) -> list[dict]:
    """A Bandcamp /music grid. The first few releases are server-rendered <li>s;
    the rest ride along as JSON in the grid's data-client-items attribute."""
    site = cfg["site"].rstrip("/")
    page = get_curl(site + "/music")
    band = re.search(r'<meta property="og:site_name" content="([^"]*)"', page)
    band = html.unescape(band.group(1)) if band else ""

    items = []
    for m in re.finditer(r'<li data-item-id="(album|track)-\d+".*?</li>', page, re.S):
        li = m.group(0)
        href = re.search(r'<a href="([^"]+)"', li)
        title = re.search(r'<p class="title">\s*(.*?)\s*(?:<br>|</p>)', li, re.S)
        override = re.search(r'<span class="artist-override">\s*(.*?)\s*</span>', li, re.S)
        if href and title:
            items.append(
                {
                    "type": m.group(1),
                    "page_url": href.group(1),
                    "title": html.unescape(title.group(1)),
                    "artist": html.unescape(override.group(1)) if override else band,
                }
            )
    blob = re.search(r'data-client-items="([^"]*)"', page)
    if blob:
        items += json.loads(html.unescape(blob.group(1)))

    types = set(cfg.get("types") or ["album", "track"])
    out, seen = [], set()
    for it in items:
        url = it["page_url"] if it["page_url"].startswith("http") else site + it["page_url"]
        if it.get("type") not in types or url in seen:
            continue
        seen.add(url)
        out.append(
            entry(
                artist=(it.get("artist") or band).strip(),
                title=it["title"].strip(),
                kind="Album" if it["type"] == "album" else "Single",
                source_url=url,
            )
        )
    say(f"  {len(out)} releases")
    return out


def bandcamp_price(url: str) -> dict:
    """Price facts from a Bandcamp album/track page. `free` means it can be
    had for nothing: a free download, or name-your-price with a zero minimum
    (Bandcamp may ask for an email address for those)."""
    page = get_curl(url)
    m = re.search(r'data-tralbum="([^"]*)"', page)
    if not m:
        return {"error": "no album data on page"}
    t = json.loads(html.unescape(m.group(1)))
    cur = t.get("current") or {}
    blob = re.search(r'data-blob="([^"]*)"', page)
    sellers = json.loads(html.unescape(blob.group(1))).get("item_sellers", {}) if blob else {}
    currency = next((v.get("currency") for v in sellers.values() if v.get("currency")), None)

    if t.get("freeDownloadPage") or cur.get("download_pref") == 1:
        return {"free": True, "kind": "free download", "currency": currency}
    if cur.get("is_set_price"):
        return {"free": False, "kind": "fixed", "price": cur.get("set_price"), "currency": currency}
    low = cur.get("minimum_price") or 0
    return {"free": not low, "kind": "name your price", "price": low, "currency": currency, "email": bool(cur.get("require_email")) or None}


def bandcamp_tracks(url: str) -> list[str]:
    """Track titles of a Bandcamp album/track page, in order."""
    page = get_curl(url)
    m = re.search(r'data-tralbum="([^"]*)"', page)
    if not m:
        raise ValueError("no album data on page")
    return [t.get("title") or "" for t in json.loads(html.unescape(m.group(1))).get("trackinfo") or []]


# --------------------------------------------------------------------------- artist / file


def fetch_artist(cfg: dict) -> list[dict]:
    """An artist's discography straight from a backend: exact ids, no matching."""
    from .text import ratio

    b = library.backend(cfg.get("backend"))
    if not cfg.get("artist_id"):
        want = cfg["artist"]
        hits = b.find_artists(want)
        best = max(hits, key=lambda a: ratio(want, a.name), default=None)
        if best is None or ratio(want, best.name) < 0.85:
            sys.exit(f"no confident {b.label} artist for {want!r} — add it from a link instead")
        cfg["artist_id"] = best.id
    say(f"  artist: {cfg.get('artist') or cfg.get('name')} ({b.label} {cfg['artist_id']})")

    out = []
    for kind, album in b.artist_albums(str(cfg["artist_id"]), set(cfg.get("include") or ["albums", "ep_singles"])):
        e = entry(
            artist=album.artist,
            title=album.title,
            kind=kind,
            date=album.date,
            source_url=album.url or "",
            backend=b.name,
            match_id=album.id,
        )
        e.update({k: v for k, v in album.meta().items() if v is not None})
        out.append(e)
    say(f"  {len(out)} releases")
    return out


def fetch_file(cfg: dict) -> list[dict]:
    """A hand-written list, one 'Artist - Title' per line."""
    out = []
    path = Path(cfg["path"]) if Path(cfg["path"]).is_absolute() else library.ROOT / cfg["path"]
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            artist, _, title = line.partition("\t")
        elif " - " in line:
            artist, _, title = line.partition(" - ")
        else:
            say(f"  unparseable: {line!r}")
            continue
        out.append(entry(artist.strip(), title.strip(), "Unknown"))
    say(f"  {len(out)} entries")
    return out


def fetch(cfg: dict) -> list[dict]:
    kind = cfg.get("kind")
    if kind == "bandcamp":
        return fetch_bandcamp(cfg)
    if kind == "shopify":
        return fetch_shopify(cfg)
    if kind == "artist":
        return fetch_artist(cfg)
    if kind == "file":
        return fetch_file(cfg)
    if kind == "local":
        return fetch_local(cfg)
    sys.exit(f"unknown source kind {kind!r} (want: bandcamp, shopify, artist, file, local)")


def fetch_local(cfg: dict, dirs: list[Path] | None = None) -> list[dict]:
    """A folder of albums you added yourself (no service): one owned release
    per album folder (`dirs`: just these, e.g. one artist's in ARTISTS)."""
    from .local import local_entry

    root = library.archive() / cfg["folder"]
    if dirs is None:
        dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    return [e for d in dirs if (e := local_entry(d, cfg.get("name", ""))) is not None]


AUDIO_RANK = {"hires": 3, "lossless": 2, "lossy": 1}


def collapse_versions(name: str, entries: list[dict]) -> list[dict]:
    """Services list one album several times (a Hi-Res and an older lossless
    copy, a version with one more bonus track, ...). Keep one per title and
    release date: the one you have or picked, else the best audio, then the
    most tracks, then the newest id. Atmos mixes stay separate."""
    from .text import norm

    state = library.release_state(name)

    def rank(e):
        st = state.get(library.key(e), {})
        mine = bool(e.get("origin") == "local" or e.get("owned") or st.get("downloaded") or st.get("wanted"))
        mid = str(e.get("match_id") or "")
        return (mine, AUDIO_RANK.get(e.get("audio"), 0), e.get("tracks") or 0, int(mid) if mid.isdigit() else 0)

    best: dict[tuple, dict] = {}
    for e in entries:
        if not e.get("match_id"):
            continue
        g = (norm(e["title"]), e.get("released") or e.get("date"), e.get("audio") == "atmos")
        if g not in best or rank(e) > rank(best[g]):
            best[g] = e
    keep = {id(e) for e in best.values()}
    return [e for e in entries if not e.get("match_id") or id(e) in keep]


def refresh(name: str, entries_before: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Re-read a source's release list, carrying every decision across by
    release key. Returns (all entries, the ones never seen before)."""
    src = library.get_source(name)
    fresh = fetch_local(src, library.album_dirs(name)) if src.get("kind") == "local" else fetch(src)
    library.db.put_source(name, src)  # e.g. an artist_id found by name
    prev = {library.key(e): e for e in (entries_before if entries_before is not None else library.load_entries(name))}
    new = []
    if src.get("kind") == "local":  # the folders on disk are the whole truth
        return fresh, [e for e in fresh if library.key(e) not in prev]
    for e in fresh:
        old = prev.get(library.key(e))
        if old is None:
            new.append(e)
            continue
        for f in library.DECISION_FIELDS:
            if f in old and not (f in ("match_id", "backend") and e.get(f)):
                e[f] = old[f]
    # Albums of yours the service doesn't list stay (see local.adopt_folders).
    keys = {library.key(e) for e in fresh}
    fresh += [e for k, e in prev.items() if k not in keys and e.get("origin") == "local" and str(e.get("source_url", "")).startswith("local:")]
    if src.get("kind") == "artist":
        fresh = collapse_versions(name, fresh)
        kept = {id(e) for e in fresh}
        new = [e for e in new if id(e) in kept]
    return fresh, new

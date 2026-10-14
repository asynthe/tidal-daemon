"""Name normalisation and fuzzy comparison, shared by every stage."""

import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

# Words that differ between a catalogue's naming and a service's, carrying no identity.
NOISE = re.compile(
    r"\b(original mix|extended mix|radio edit|feat\.?|ft\.?|vip|remixe?s?|"
    r"ep|lp|single|album|deluxe|remastered|explicit)\b",
    re.I,
)
ARTIST_SPLIT = re.compile(r"\s*(?:&|,|\bx\b|\bvs\.?\b|\band\b|\bwith\b|\+)\s*", re.I)


def norm(s: str) -> str:
    """Casefold, strip accents and punctuation, drop noise words, collapse space."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold().replace("&", " and ")
    s = NOISE.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def best_artist(want: str, names: list[str]) -> float:
    """Best per-artist similarity: catalogues merge and reorder co-credits freely."""
    names = names or [""]
    best = ratio(want, ", ".join(names))
    for w in ARTIST_SPLIT.split(want):
        for n in names:
            best = max(best, ratio(w, n))
    return best


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", name).casefold())


def safe_folder(name: str) -> str:
    """Windows forbids these in folder names."""
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")


# Paths stay below Windows' classic MAX_PATH (260), which many tools still
# trip over; elsewhere only the per-name limit (255 bytes) matters.
PATH_LIMIT = 250 if os.name == "nt" else 4000
NAME_LIMIT = 180


def clean_name(s: str) -> str:
    """A file/folder name without invisible characters: zero-width spaces,
    joiners, direction marks, BOMs (Unicode "format" chars) and control chars."""
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cf", "Cc"))
    return re.sub(r"\s+", " ", s).strip()


def _shorten(s: str, n: int, keep_suffix: bool) -> str:
    if len(s) <= n:
        return s
    stem, dot, ext = s.rpartition(".") if keep_suffix and "." in s else (s, "", "")
    suffix = dot + ext
    return stem[: max(n - len(suffix) - 1, 1)].rstrip(" .") + "…" + suffix


def tidy_path(path: Path) -> Path:
    """Clean every name in a path and keep it within PATH_LIMIT, shortening
    the file name first, then its folder (never the archive part above)."""
    anchor, parts = (path.parts[0], list(path.parts[1:])) if path.anchor else ("", list(path.parts))
    if not parts:
        return path
    parts = [clean_name(p) or "_" for p in parts]
    parts = [_shorten(p, NAME_LIMIT, i == len(parts) - 1) for i, p in enumerate(parts)]
    for i in (len(parts) - 1, len(parts) - 2):  # file name, then album folder
        over = len(str(Path(anchor, *parts))) - PATH_LIMIT
        if over <= 0 or i < 0:
            break
        parts[i] = _shorten(parts[i], max(len(parts[i]) - over, 24), i == len(parts) - 1)
    return Path(anchor, *parts)

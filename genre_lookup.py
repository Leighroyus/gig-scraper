#!/usr/bin/env python3
"""
Genre Lookup — Enrich band names with genre tags from Last.fm / MusicBrainz.
Caches results in SQLite so each band is only looked up once.
"""

import os
import re
import time
import json
import sqlite3
import logging
from typing import Optional, List, Dict

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger(__name__)

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "").strip()
CACHE_DB = os.environ.get("GENRE_CACHE_DB", os.path.join(os.path.dirname(__file__), "genre_cache.db"))

# Genre keywords that count as "heavy"
HEAVY_GENRES = {
    # Metal — always heavy
    "metal", "death metal", "black metal", "thrash metal", "groove metal",
    "stoner metal", "doom metal", "sludge metal", "deathcore", "metalcore",
    "progressive metal", "nu metal", "djent", "power metal", "speed metal",
    "symphonic metal", "folk metal", "viking metal", "gothic metal",
    "avant-garde metal", "alternative metal", "crossover thrash",
    "funeral doom", "drone metal", "blackgaze", "post-metal",
    "atmospheric black metal", "depressive black metal", "raw black metal",
    "blackened doom", "stoner doom metal", "death doom metal",
    "brutal death metal", "melodic death metal", "progressive metalcore",
    "mathcore", "grindcore", "deathgrind", "noisecore",
    # Punk/hardcore — always heavy
    "hardcore", "hardcore punk", "powerviolence", "crust punk",
    "screamo", "emo violence", "d-beat", "oi",
    "skate punk", "melodic hardcore", "street punk", "horror punk",
    "anarcho punk", "punk", "punk rock", "garage punk", "ska punk",
    # Other unambiguous
    "doom", "sludge", "thrash", "stoner rock", "noise rock",
    "math rock", "psychobilly", "crust",
}


def _init_cache(db_path: str = CACHE_DB) -> None:
    """Create cache table if it doesn't exist."""
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS genre_cache (
            band_key    TEXT PRIMARY KEY,
            genres      TEXT,
            source      TEXT,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.close()


def _cache_get(band: str, db_path: str = CACHE_DB) -> Optional[Dict]:
    """Check cache for a band."""
    key = _band_key(band)
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT genres, source, fetched_at FROM genre_cache WHERE band_key = ?", [key]
    ).fetchone()
    con.close()
    if row:
        return {"genres": json.loads(row[0]), "source": row[1], "fetched_at": row[2]}
    return None


def _cache_set(band: str, genres: List[str], source: str, db_path: str = CACHE_DB) -> None:
    """Store genre result in cache."""
    key = _band_key(band)
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT OR REPLACE INTO genre_cache (band_key, genres, source, fetched_at) VALUES (?, ?, ?, datetime('now'))",
        [key, json.dumps(genres), source],
    )
    con.close()


def _band_key(band: str) -> str:
    """Normalise band name for cache key."""
    return re.sub(r"[^a-z0-9]", "", band.lower().strip())


# ---------------------------------------------------------------------------
# Artist name splitting
# ---------------------------------------------------------------------------

# Prefixes/suffixes to strip before splitting
_STRIP_PATTERNS = [
    # Event type prefixes (with optional colon)
    r"^(?:EP|Album|Single|Demo|Tape|Release)\s+Launch\s*[:\-–—]?\s*",
    r"^(?:EP|Album|Single|Demo|Tape|Release)\s+Tour\s*[:\-–—]?\s*",
    r"^(?:\d+(?:st|nd|rd|th)\s+)?(?:Anniversary)\s+(?:Tour|Show)\s*[:\-–—]?\s*",
    r"^(?:Tour|Live|Shows?|Gig|Concert)\s+(?:Presents?|at|@)\s*",
    r"^Presents?\s+",
    r"^(?:Live|Instore|Acoustic)\s+at\s+",
    # Venue prefixes (e.g. "The Tote presents")
    r"^(?:The\s+)?(?:Corner\s+Hotel|Tote|Tote\s+Hotel|Max\s+Watts|Shotkickers|"
    r"Bendigo\s+Hotel|Night\s+Hawks|Cherry\s+Bar|Old\s+Bar|Evelyn\s+Hotel|"
    r"Kindred\s+Studios|Croxton\s+Bandroom|Barwon\s+Club|Torquay\s+Hotel|"
    r"Northcote\s+Socialist|Cherry\s+Bar)\s+(?:presents?|at|@)\s+",
    # "Event Name: Band A & Band B" style (colon separator)
    # Strip event-looking prefixes before a colon, e.g. "Punk in the Park: ..."
    r"^[A-Za-z0-9' ]+(?:Festival|Fest|Night|Presents?|Show|Launch|Tour|Weekend|in the Park|at the)\s*[:\-–—]\s*",
    # Generic "Word Word Word:" pattern (3+ words before colon) — likely an event name
    r"^(?:[A-Za-z' ]{3,}?)\s*:\s*",
    # Common prefixes
    r"^(?:featuring|feat\.?|ft\.?|w\/|with)\s+",
]

# Suffixes to strip after splitting
_STRIP_SUFFIXES = [
    r"(?:–|—|-)\s*(?:'?\w+(?:'s)?\s+)?(?:Anniversary|Tour|EP|Album|Single|Launch|Shows?|Live|Supporting).*$",
    r"\s*\d+\+.*$",
    r"\s*(?:SELLING\s+FAST|SOLD\s+OUT|WAITLIST|FREE\s+ENTRY|TICKETS).*$",
    r"\s*\(.*?\)\s*$",
    r"\s*\[.*?\]\s*$",
]

# Separators for splitting multi-band titles (ordered by specificity)
# Note: '&' is excluded — too ambiguous with band names like "Tom & Jerry"
_SEPARATORS = [
    (r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE),
    (r"\bw/\s*", re.IGNORECASE),  # matches 'Launch w/ Band' or 'Launch w/Band' (no leading space needed)
    (r"\s+with\s+", re.IGNORECASE),
    (r"\s+and\s+", re.IGNORECASE),  # e.g. 'Neon Goblin and Black Wattle Witches'
    (r"\s+\+\s+", 0),
]


def split_artists(raw_title: str) -> List[str]:
    """Split a multi-band event title into individual band names.

    Handles separators: w/, with, +, feat., featuring, &
    Also strips common prefixes/suffixes before splitting.

    Examples:
        "Frenzal Rhomb w/ Ceres" -> ["Frenzal Rhomb", "Ceres"]
        "Polaris feat. In Hearts Wake" -> ["Polaris", "In Hearts Wake"]
        "Punk in the Park: Bad Religion + Millencolin" -> ["Bad Religion", "Millencolin"]
    """
    title = raw_title.strip()
    if not title:
        return []

    # --- Strip common prefixes ---
    for pattern in _STRIP_PATTERNS:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE).strip()

    # --- Strip common suffixes ---
    for pattern in _STRIP_SUFFIXES:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE).strip()

    # --- Clean separator-context noise around + ---
    # "Jul 12 + 13" is a date range, not bands. Protect date patterns first.
    # Replace date-range pluses with a sentinel
    title = re.sub(
        r"(\d{1,2})\s*\+\s*(\d{1,2})",
        r"\1⟨PLUS⟩\2",
        title,
    )

    # --- Split on separators ---
    parts = [title]
    for sep_pattern, flags in _SEPARATORS:
        new_parts = []
        for part in parts:
            new_parts.extend(re.split(sep_pattern, part, flags=flags))
        parts = new_parts

    # --- Restore protected plus signs ---
    parts = [p.replace("⟨PLUS⟩", "+") for p in parts]

    # --- Clean each part ---
    cleaned = []
    for name in parts:
        # Strip leading/trailing punctuation, dashes, pipes, colons
        name = name.strip(" -–—|·•,:")
        # Remove leading/trailing quotes
        name = name.strip("'\"'")
        # Collapse whitespace
        name = re.sub(r"\s+", " ", name).strip()
        # Skip very short or pure noise
        if name and len(name) > 1 and not re.match(r"^\d+$", name):
            cleaned.append(name)

    return cleaned


def clean_artist_name(raw: str) -> str:
    """Extract likely artist name from event title.

    Strips venue names, tour suffixes, age restrictions, ticket info, etc.
    If the title contains band separators, returns the first/primary band.

    E.g. "28 Days – 30th Anniversary Tour (Pt. 2) – Torquay Hotel 18+" → "28 Days"
    "Frenzal Rhomb w/ Ceres" → "Frenzal Rhomb"
    """
    # Try splitting first — if we get multiple artists, return the primary one
    artists = split_artists(raw)
    if artists:
        return artists[0]

    # Fallback: manual cleaning
    name = raw.strip()
    # Remove common suffixes after em-dash or pipe
    for sep in [' – ', ' — ', ' | ', ' - ']:
        if sep in name:
            name = name.split(sep)[0].strip()
    # Remove venue names (common Melbourne venues)
    venue_re = r'\b(?:The\s+)?(?:Corner\s+Hotel|Tote\s+Hotel|Max\s+Watts|Shotkickers|Bendigo\s+Hotel|Night\s+Hawks|Cherry\s+Bar|Old\s+Bar|Evelyn\s+Hotel|Kindred\s+Studios|Croxton\s+Bandroom|Barwon\s+Club|Barwon\s+Heads\s+Hotel|Torquay\s+Hotel)\b'
    name = re.sub(venue_re, '', name, flags=re.IGNORECASE).strip()
    # Remove trailing age restriction / ticket info
    name = re.sub(r'\s*\d+\+.*$', '', name)
    name = re.sub(r'\s*(?:SELLING\s+FAST|SOLD\s+OUT|WAITLIST|FREE\s+ENTRY).*$', '', name, flags=re.IGNORECASE)
    # Remove tour/anniversary/ep launch suffixes
    name = re.sub(r"\s*[–\-]\s*(?:'?\w+(?:'s)?\s+)?(?:Anniversary|Tour|EP|Album|Single|Launch|Show|Live|Supporting).*$", '', name, flags=re.IGNORECASE)
    # Remove trailing parentheticals
    name = re.sub(r'\s*\([^)]*\)\s*$', '', name)
    name = re.sub(r'\s*\[[^\]]*\]\s*$', '', name)
    return name.strip()


# ---------------------------------------------------------------------------
# Last.fm
# ---------------------------------------------------------------------------

def _lastfm_lookup(band: str) -> Optional[List[str]]:
    """Query Last.fm artist.getTopTags. Returns list of tag names."""
    if not LASTFM_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "artist.getTopTags",
                "artist": band,
                "api_key": LASTFM_API_KEY,
                "format": "json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        toptags = data.get("toptags", {}).get("tag", [])
        return [t["name"] for t in toptags if t.get("name")]
    except Exception as e:
        log.warning("Last.fm lookup failed for %s: %s", band, e)
        return None


# ---------------------------------------------------------------------------
# MusicBrainz
# ---------------------------------------------------------------------------

def _musicbrainz_lookup(band: str) -> Optional[List[str]]:
    """Query MusicBrainz search API. Returns list of tags."""
    try:
        resp = requests.get(
            "https://musicbrainz.org/ws/2/artist/",
            params={"query": band, "fmt": "json", "limit": 1},
            headers={"User-Agent": "gig-scraper/1.0 (https://github.com/Leighroyus/gig-scraper)"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        artists = data.get("artists", [])
        if not artists:
            return None
        tags = []
        for artist in artists:
            for tag in artist.get("tags", []):
                tags.append(tag["name"])
        return tags if tags else None
    except Exception as e:
        log.warning("MusicBrainz lookup failed for %s: %s", band, e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lookup_genres(band: str, force: bool = False) -> Dict:
    """
    Look up genres for a band. Returns dict with 'genres', 'source', 'is_heavy'.
    Uses cache → Last.fm → MusicBrainz chain.
    """
    _init_cache()

    # Clean the artist name for lookup
    artist = clean_artist_name(band)
    if not artist or len(artist) < 2:
        return {"genres": [], "source": "skipped", "is_heavy": False}

    # Check cache (use cleaned artist name)
    if not force:
        cached = _cache_get(artist)
        if cached:
            cached["is_heavy"] = _is_heavy(cached["genres"])
            return cached

    # Try Last.fm
    genres = _lastfm_lookup(artist)
    if genres:
        # Last.fm returns ~100 tags, many irrelevant. Take top 15.
        genres = genres[:15]
        _cache_set(artist, genres, "lastfm")
        log.info("Last.fm: %s → %s", artist, genres[:5])
        return {"genres": genres, "source": "lastfm", "is_heavy": _is_heavy(genres)}

    # Rate limit for MusicBrainz
    time.sleep(1.1)

    # Try MusicBrainz
    genres = _musicbrainz_lookup(artist)
    if genres:
        _cache_set(artist, genres, "musicbrainz")
        log.info("MusicBrainz: %s → %s", artist, genres[:5])
        return {"genres": genres, "source": "musicbrainz", "is_heavy": _is_heavy(genres)}

    # Unknown
    _cache_set(artist, [], "unknown")
    return {"genres": [], "source": "unknown", "is_heavy": False}


def _is_heavy(genres: List[str]) -> bool:
    """Check if any genre matches our heavy keywords."""
    for g in genres:
        g_lower = g.lower().strip()
        for heavy in HEAVY_GENRES:
            if g_lower == heavy:
                return True
            if heavy in g_lower:
                return True
    return False


def batch_lookup(bands: List[str]) -> Dict[str, Dict]:
    """Look up genres for multiple bands."""
    results = {}
    for band in bands:
        artist = clean_artist_name(band)
        if artist:
            results[band] = lookup_genres(band)
            time.sleep(0.2)  # Be polite to APIs
    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    # Demo split_artists
    test_titles = [
        "Frenzal Rhomb w/ Ceres",
        "Polaris feat. In Hearts Wake",
        "Bad Religion + Millencolin",
        "Ep Launch: The Smith Street Band with Hard Ons",
        "28 Days – 30th Anniversary Tour (Pt. 2) – Torquay Hotel 18+",
        "Sun 12 Jul 07:00pm The Bennies with Hockey Dad",
        "Tom & Jerry Band Night",
        "Punk in the Park: Bad Religion & Pennywise",
    ]
    print("=== split_artists demo ===")
    for title in test_titles:
        artists = split_artists(title)
        primary = clean_artist_name(title)
        print(f"  {title!r}")
        print(f"    split → {artists}")
        print(f"    primary → {primary!r}")
        print()

    bands = sys.argv[1:] or ["Frenzal Rhomb", "Amorphis", "Between the Buried and Me", "GW3 Band"]
    for band in bands:
        result = lookup_genres(band)
        heavy = "🔥" if result["is_heavy"] else "  "
        print(f"{heavy} {band} → {result['genres'][:5]} ({result['source']})")

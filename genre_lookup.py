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
    "metal", "hardcore", "punk", "doom", "sludge", "thrash", "death metal",
    "black metal", "groove metal", "stoner rock", "stoner metal", "grindcore",
    "post-hardcore", "metalcore", "deathcore", "powerviolence", "crust punk",
    "noise rock", "post-punk", "industrial", "industrial metal", "gothic metal",
    "progressive metal", "nu metal", "math rock", "screamo", "emo", "emo punk",
    "skate punk", "melodic hardcore", "hard rock", "grunge", "alternative metal",
    "djent", "progressive rock", "psychedelic rock", "psychedelic metal",
    "folk metal", "viking metal", "doom metal", "funeral doom", "drone metal",
    "blackgaze", "post-metal", "atmospheric black metal", "symphonic metal",
    "power metal", "speed metal", "crossover thrash", "hardcore punk",
    "anarcho punk", "Oi", "street punk", "horror punk", "psychobilly",
    "no wave", "noise", "experimental rock", "avant-garde metal",
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


def clean_artist_name(raw: str) -> str:
    """Extract likely artist name from event title.
    
    Strips venue names, tour suffixes, age restrictions, ticket info, etc.
    E.g. "28 Days – 30th Anniversary Tour (Pt. 2) – Torquay Hotel 18+" → "28 Days"
    """
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
        # MusicBrainz doesn't have genres on artist search directly,
        # but we can check the tags/disambiguation
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
    """Check if any genre matches our heavy keywords.
    
    Uses substring matching to avoid false positives like
    'folk' matching 'folk metal' or 'alternative' matching 'alternative metal'.
    """
    for g in genres:
        g_lower = g.lower().strip()
        for heavy in HEAVY_GENRES:
            # Exact match
            if g_lower == heavy:
                return True
            # Heavy genre is a substring of the tag (e.g. 'death metal' in 'melodic death metal')
            if heavy in g_lower:
                return True
            # Tag is a substring of heavy — only match if tag is a meaningful prefix
            # (e.g. 'punk' matches 'punk rock' because punk is the primary genre)
            # Skip this direction to avoid false positives
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
    bands = sys.argv[1:] or ["Frenzal Rhomb", "Amorphis", "Between the Buried and Me", "GW3 Band"]
    for band in bands:
        result = lookup_genres(band)
        heavy = "🔥" if result["is_heavy"] else "  "
        print(f"{heavy} {band} → {result['genres'][:5]} ({result['source']})")

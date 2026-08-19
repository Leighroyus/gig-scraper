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
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger(__name__)

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "").strip()
CACHE_DB = os.environ.get("GENRE_CACHE_DB", os.path.join(os.path.dirname(__file__), "genre_cache.db"))

# Genre keywords that count as "heavy" with confidence weights.
# Weight 1.0 = unambiguous heavy, 0.6-0.8 = usually heavy, 0.3-0.5 = borderline.
HEAVY_GENRES = {
    # Metal — always heavy (1.0)
    "metal": 1.0, "death metal": 1.0, "black metal": 1.0, "thrash metal": 1.0,
    "groove metal": 1.0, "stoner metal": 1.0, "doom metal": 1.0,
    "sludge metal": 1.0, "deathcore": 1.0, "metalcore": 1.0,
    "progressive metal": 1.0, "nu metal": 1.0, "djent": 1.0,
    "power metal": 0.9, "speed metal": 1.0, "symphonic metal": 0.9,
    "folk metal": 0.8, "viking metal": 0.9, "gothic metal": 0.8,
    "avant-garde metal": 0.9, "alternative metal": 0.7,
    "crossover thrash": 1.0, "funeral doom": 1.0, "drone metal": 1.0,
    "blackgaze": 1.0, "post-metal": 0.8, "atmospheric black metal": 1.0,
    "depressive black metal": 1.0, "raw black metal": 1.0,
    "blackened doom": 1.0, "stoner doom metal": 1.0, "death doom metal": 1.0,
    "brutal death metal": 1.0, "melodic death metal": 1.0,
    "progressive metalcore": 1.0, "mathcore": 1.0, "grindcore": 1.0,
    "deathgrind": 1.0, "noisecore": 1.0,
    # Punk/hardcore — always heavy (1.0)
    "hardcore": 1.0, "hardcore punk": 1.0, "powerviolence": 1.0,
    "crust punk": 1.0, "screamo": 1.0, "emo violence": 1.0,
    "d-beat": 1.0, "oi": 0.9, "skate punk": 0.6, "melodic hardcore": 0.7,
    "street punk": 0.8, "horror punk": 0.7, "anarcho punk": 0.8,
    "punk": 0.6, "punk rock": 0.6, "garage punk": 0.6, "ska punk": 0.3,
    # Other heavy-adjacent
    "doom": 1.0, "sludge": 1.0, "thrash": 1.0, "stoner rock": 0.9,
    "noise rock": 0.7, "math rock": 0.5, "psychobilly": 0.7, "crust": 0.9,
    # Hard rock / industrial
    "hard rock": 0.5, "industrial": 0.7, "industrial metal": 1.0, "industrial rock": 0.6,

    # Non-heavy genres (for penalty calculation)
    "rock": 0.0, "pop": 0.0, "indie": 0.0, "indie rock": 0.0,
    "alternative rock": 0.0, "electronic": 0.0, "pop punk": 0.1,
    "emo": 0.1, "post-punk": 0.2,
}

# Confidence thresholds
HEAVY_THRESHOLD = 0.65       # Score >= this = definitely heavy
BORDERLINE_LOW = 0.35        # Score >= this but < HEAVY_THRESHOLD = ask user
BORDERLINE_HIGH = 0.65       # Score >= this = definitely heavy (alias)
REJECT_THRESHOLD = 0.35      # Score < this = definitely not heavy
BORDERLINE_ZONES = (BORDERLINE_LOW, HEAVY_THRESHOLD)  # (0.35, 0.65)

def is_heavy_confident(score: float) -> str:
    """Classify a confidence score into a decision.
    Returns: 'yes', 'no', or 'borderline'.
    """
    if score >= HEAVY_THRESHOLD:
        return 'yes'
    elif score < BORDERLINE_LOW:
        return 'no'
    else:
        return 'borderline'


def _init_cache(db_path: str = CACHE_DB) -> sqlite3.Connection:
    """Create cache table if it doesn't exist. Returns connection (caller must close)."""
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS genre_cache (
            band_key    TEXT PRIMARY KEY,
            genres      TEXT,
            source      TEXT,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return con


def _cache_get(band: str, conn: sqlite3.Connection = None, db_path: str = CACHE_DB) -> Optional[Dict]:
    """Check cache for a band. If conn is provided, use it; otherwise open/close."""
    key = _band_key(band)
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT genres, source, fetched_at FROM genre_cache WHERE band_key = ?", [key]
        ).fetchone()
        if row:
            return {"genres": json.loads(row[0]), "source": row[1], "fetched_at": row[2]}
        return None
    finally:
        if own_conn:
            conn.close()


def seed_cache_from_duckdb(duckdb_path: str = None) -> int:
    """Pre-populate the SQLite genre cache from DuckDB bands table.

    This avoids redundant API calls for bands we already know about.
    Returns the number of bands seeded.
    """
    if duckdb_path is None:
        duckdb_path = os.environ.get('GIG_DB_PATH', os.path.join(os.path.dirname(__file__), 'gigs.duckdb'))

    if not os.path.exists(duckdb_path):
        log.debug('DuckDB not found at %s, skipping cache seed', duckdb_path)
        return 0

    try:
        import duckdb
        _init_cache()

        con_db = duckdb.connect(duckdb_path, read_only=True)
        rows = con_db.execute(
            "SELECT name, genres, is_heavy, genre_source FROM bands WHERE genres IS NOT NULL AND genres != '[]'"
        ).fetchall()
        con_db.close()

        if not rows:
            return 0

        count = 0
        con_sql = sqlite3.connect(CACHE_DB)
        for band_name, genres_json, is_heavy, source in rows:
            key = _band_key(band_name)
            # Skip if already cached
            existing = con_sql.execute(
                "SELECT 1 FROM genre_cache WHERE band_key = ?", [key]
            ).fetchone()
            if existing:
                continue

            try:
                genres = json.loads(genres_json) if genres_json else []
            except (json.JSONDecodeError, TypeError):
                genres = []

            src = source or 'duckdb'
            con_sql.execute(
                "INSERT OR REPLACE INTO genre_cache (band_key, genres, source, fetched_at) VALUES (?, ?, ?, datetime('now'))",
                [key, json.dumps(genres), src],
            )
            count += 1

        con_sql.commit()
        con_sql.close()
        log.info('Seeded %d bands from DuckDB into genre cache', count)
        return count
    except Exception as e:
        log.warning('Failed to seed cache from DuckDB: %s', e)
        return 0


def _cache_set(band: str, genres: List[str], source: str, conn: sqlite3.Connection = None, db_path: str = CACHE_DB, heavy_score: float = 0.0, tags_json: str = None) -> None:
    """Store genre result in cache. If conn is provided, use it; otherwise open/close."""
    key = _band_key(band)
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(db_path)
    try:
        # Ensure heavy_score and tags columns exist (migration)
        try:
            conn.execute("SELECT heavy_score FROM genre_cache LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE genre_cache ADD COLUMN heavy_score REAL DEFAULT 0.0")
            conn.execute("ALTER TABLE genre_cache ADD COLUMN tags_json TEXT")
        conn.execute(
            "INSERT OR REPLACE INTO genre_cache (band_key, genres, source, fetched_at, heavy_score, tags_json) VALUES (?, ?, ?, datetime('now'), ?, ?)",
            [key, json.dumps(genres), source, heavy_score, tags_json],
        )
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()


def _band_key(band: str) -> str:
    """Normalise band name for cache key."""
    return re.sub(r"[^a-z0-9]", "", band.lower().strip())


# ---------------------------------------------------------------------------
# Artist name splitting
# ---------------------------------------------------------------------------

# Prefixes/suffixes to strip before splitting
_STRIP_PATTERNS = [
    # Ticket-status prefixes (must come first — these are common)
    r"^(?:SELLING\s+FAST|SOLD\s+OUT|WAITLIST|FREE\s+ENTRY|TICKETS?\s+(?:ON\s+SALE|AVAILABLE))\s*[–\-—]?\s*",
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
    r"Kindred\s+Studios|Croxton\s+Bandroom|Barwon\s+Club|Barwon\s+Heads\s+Hotel|"
    r"Torquay\s+Hotel|Northcote\s+Socialist|Cherry\s+Bar)\s+(?:presents?|at|@)\s+",
    # "Event Name: Band A & Band B" style (colon separator)
    r"^[A-Za-z0-9' ]+(?:Festival|Fest|Night|Presents?|Show|Launch|Tour|Weekend|in the Park|at the)\s*[:\-–—]\s*",
    # Generic "Word Word Word:" pattern (3+ words before colon) — likely an event name
    r"^(?:[A-Za-z' ]{3,}?)\s*:\s*",
    # Common prefixes
    r"^(?:featuring|feat\.?|ft\.?|w\/|with)\s+",
]

# Suffixes to strip after splitting
_STRIP_SUFFIXES = [
    r"\s*(?:–|—|-)\s*(?:'?\w+(?:'s)?\s+)?(?:Anniversary|Tour|EP|Album|Single|Launch|Shows?|Live|Supporting).*$",
    r"\s*\d+\+.*$",
    r"\s*(?:SELLING\s+FAST|SOLD\s+OUT|WAITLIST|FREE\s+ENTRY|TICKETS).*$",
    r"\s*\(.*?\)\s*$",
    r"\s*\[.*?\]\s*$",
    # Venue names at end (common ones)
    r"\s*[–\-—]?\s*(?:The\s+)?(?:Corner\s+Hotel|Tote\s+Hotel|Max\s+Watts|Shotkickers|"
    r"Bendigo\s+Hotel|Night\s+Hawks|Cherry\s+Bar|Old\s+Bar|Evelyn\s+Hotel|"
    r"Kindred\s+Studios|Croxton\s+Bandroom|Barwon\s+Club|Barwon\s+Heads\s+Hotel|"
    r"Torquay\s+Hotel|Northcote\s+Socialist).*$",
    # Country tags like (USA), (UK), (Australia)
    r"\s*\(?\s*(?:USA|UK|Australia|AUS|Canada|Germany|Japan)\s*\)?\s*$",
]

# Separators for splitting multi-band titles (ordered by specificity)
# Note: '&' is excluded — too ambiguous with band names like "Tom & Jerry"
_SEPARATORS = [
    (r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE),
    (r"\bw/\s*", re.IGNORECASE),  # matches 'Launch w/ Band' or 'Launch w/Band' (no leading space needed)
    (r"\s+with\s+", re.IGNORECASE),
    (r"\s+and\s+", re.IGNORECASE),  # e.g. 'Neon Goblin and Black Wattle Witches'
    (r"\s+\+\s+", 0),
    # Em-dash and en-dash as separators (e.g. "Frenzal Rhomb – Support Act")
    (r"\s*[–—]\s+", 0),
    # Comma separator for multi-band (e.g. "Band A, Band B")
    (r"\s*,\s+", 0),
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

def _lastfm_lookup(band: str) -> Optional[List[Dict]]:
    """Query Last.fm artist.getTopTags. Returns list of {name, count} dicts."""
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
        return [{"name": t["name"], "count": int(t.get("count", 0))} for t in toptags if t.get("name")]
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

_duckdb_seeded = False

# Patterns that indicate non-music entries (skip API calls for these)
_NON_MUSIC_PATTERNS = [
    r'(?i)\b(?:happy\s+hour|trivia|quiz|karaoke|open\s+mic|jam\s+night|bingo|raffle|auction|charity|fundrais|raffle|sunday\s+roast|parma|steak|meat\s+tray|pottery|yoga|meditation|tattoo|piercing|market|flea|car\s+boot|garage\s+sale)\b',
    r'(?i)\b(?:nfl|nba|mlb|premier\s+league|afl|nrl|super\s+rugby|cricket|tennis|boxing|mma|ufc|wrestling|f1|formula|grand\s+prix)\b',
    r'(?i)^\s*(?:the\s+)?(?:front\s+bar|back\s+bar|beer\s+garden|beer\s+hall)\b',
    r'(?i)\b(?:swap\s+meet|flea\s+market|car\s+show|auto\s+show|food\s+festival|wine\s+festival|beer\s+festival)\b',
    r'(?i)^\s*(?:DJ\s+)?(?:set|b2b|back\s+to\s+back)\b',
    r'(?i)\b(?:talk|seminar|workshop|class|lecture|panel|conference|meetup|meet\s+up)\b',
    r'(?i)\b(?:film\s+festival|movie|cinema|screening|theatre|theater|play|comedy|stand\s+up|improv)\b',
    r'(?i)\b(?:book\s+club|reading|poetry|open\s+book)\b',
    r'(?i)^\s*(?:free\s+entry|free\s+show)\b',
    r'(?i)\b(?:wednesdays?|thursdays?|fridays?|saturdays?|sundays?)\s+(?:@|at)\s+\w+\b',
    r'(?i)^\s*(?:the\s+)?(?:bendi|bendigo)\s+(?:x|ft|featuring)\b',
]


def _is_non_music(name: str) -> bool:
    """Check if a band name looks like a non-music event."""
    for pattern in _NON_MUSIC_PATTERNS:
        if re.search(pattern, name):
            return True
    return False


def lookup_genres(band: str, force: bool = False, duckdb_path: str = None) -> Dict:
    """
    Look up genres for a band. Returns dict with 'genres', 'source', 'is_heavy', 'heavy_score'.
    Uses cache → DuckDB → Last.fm → MusicBrainz chain.
    """
    global _duckdb_seeded
    conn = _init_cache()
    try:
        # Seed cache from DuckDB on first call (cheap, idempotent)
        if not force and not _duckdb_seeded:
            _duckdb_seeded = True
            seed_cache_from_duckdb(duckdb_path)

        # Clean the artist name for lookup
        artist = clean_artist_name(band)
        if not artist or len(artist) < 2:
            return {"genres": [], "source": "skipped", "is_heavy": False, "heavy_score": 0.0}

        # Skip non-music entries early (no API call needed)
        if _is_non_music(artist):
            _cache_set(artist, [], "skipped", conn=conn)
            conn.commit()
            return {"genres": [], "source": "skipped", "is_heavy": False, "heavy_score": 0.0}

        # Check cache (use cleaned artist name)
        if not force:
            cached = _cache_get(artist, conn=conn)
            if cached:
                # Skip if already tried MusicBrainz and got nothing
                if cached["source"] in ("musicbrainz_tried", "unknown"):
                    cached["is_heavy"] = _is_heavy(cached["genres"])
                    cached["heavy_score"] = cached.get("heavy_score", 0.0)
                    return cached
                cached["is_heavy"] = _is_heavy(cached["genres"])
                cached["heavy_score"] = cached.get("heavy_score", 0.0)
                return cached

        # Try Last.fm
        tags = _lastfm_lookup(artist)
        if tags:
            # Last.fm returns ~100 tags, many irrelevant. Take top 15.
            tags = tags[:15]
            genres = [t["name"] for t in tags]
            score = _calc_heavy_score(tags)
            tags_json = json.dumps(tags)
            _cache_set(artist, genres, "lastfm", conn=conn, heavy_score=score, tags_json=tags_json)
            conn.commit()
            log.info("Last.fm: %s → %s (score=%.2f)", artist, genres[:5], score)
            return {"genres": genres, "source": "lastfm", "is_heavy": score >= HEAVY_THRESHOLD, "heavy_score": score}

        # Rate limit for MusicBrainz
        time.sleep(1.1)

        # Try MusicBrainz
        genres = _musicbrainz_lookup(artist)
        if genres:
            score = _calc_heavy_score([{"name": g, "count": 50} for g in genres])
            _cache_set(artist, genres, "musicbrainz", conn=conn, heavy_score=score)
            conn.commit()
            log.info("MusicBrainz: %s → %s (score=%.2f)", artist, genres[:5], score)
            return {"genres": genres, "source": "musicbrainz", "is_heavy": score >= HEAVY_THRESHOLD, "heavy_score": score}

        # Unknown — mark as musicbrainz_tried so we don't retry next time
        _cache_set(artist, [], "musicbrainz_tried", conn=conn)
        conn.commit()
        return {"genres": [], "source": "unknown", "is_heavy": False, "heavy_score": 0.0}
    finally:
        conn.close()


def _calc_heavy_score(tags: List[Dict]) -> float:
    """Calculate a heavy confidence score from weighted genre tags.

    Args:
        tags: List of {name, count} dicts from Last.fm (count = 0-100).

    Returns:
        Score from 0.0 (definitely not heavy) to 1.0 (definitely heavy).
    """
    if not tags:
        return 0.0

    best_score = 0.0
    heavy_matches = []

    for tag in tags:
        tag_name = tag["name"].lower().strip()
        tag_count = tag.get("count", 0)  # 0-100 from Last.fm

        # Find matching heavy genre
        weight = None
        for heavy_genre, genre_weight in HEAVY_GENRES.items():
            if tag_name == heavy_genre or heavy_genre in tag_name:
                weight = genre_weight
                break

        if weight is not None and weight > 0:
            # Score = genre_weight * (tag_count / 100)
            # A death metal tag at count=100 → 1.0
            # A punk tag at count=50 → 0.3
            tag_score = weight * (tag_count / 100.0)
            heavy_matches.append((tag_name, weight, tag_count, tag_score))
            best_score = max(best_score, tag_score)

    # Bonus for multiple heavy genre matches (corroboration)
    if len(heavy_matches) >= 2:
        avg_top2 = sum(h[3] for h in heavy_matches[:2]) / 2.0
        best_score = max(best_score, avg_top2)

    # Cap at 1.0
    return min(best_score, 1.0)


def _is_heavy(tags) -> bool:
    """Check if any genre matches our heavy keywords (legacy bool interface).
    Accepts either List[str] or List[Dict] (with 'name' and 'count' keys).
    """
    # Normalise to list of dicts
    if tags and isinstance(tags[0], str):
        tags = [{"name": g, "count": 50} for g in tags]
    score = _calc_heavy_score(tags)
    return score >= HEAVY_THRESHOLD


def batch_lookup(bands: List[str]) -> Dict[str, Dict]:
    """Look up genres for multiple bands.

    Uses parallel Last.fm lookups (4 workers) followed by a rate-limited
    MusicBrainz queue (1 req/sec) for any misses.
    """
    global _duckdb_seeded
    conn = _init_cache()

    try:
        # Seed cache from DuckDB on first call
        if not _duckdb_seeded:
            _duckdb_seeded = True
            seed_cache_from_duckdb()

        results = {}
        # Step 1: Prepare all bands — clean names, check cache, classify
        lastfm_candidates = []   # (original_band, artist_name)
        mb_candidates = []       # (original_band, artist_name)
        mb_skip = set()          # band keys to skip (already tried MB)

        for band in bands:
            artist = clean_artist_name(band)
            if not artist or len(artist) < 2:
                results[band] = {"genres": [], "source": "skipped", "is_heavy": False, "heavy_score": 0.0}
                continue

            if _is_non_music(artist):
                _cache_set(artist, [], "skipped", conn=conn)
                results[band] = {"genres": [], "source": "skipped", "is_heavy": False, "heavy_score": 0.0}
                continue

            # Check cache
            cached = _cache_get(artist, conn=conn)
            if cached:
                # Skip if already tried MusicBrainz and got nothing
                if cached["source"] in ("musicbrainz_tried", "unknown"):
                    cached["is_heavy"] = _is_heavy(cached["genres"])
                    cached["heavy_score"] = cached.get("heavy_score", 0.0)
                    results[band] = cached
                    continue
                cached["is_heavy"] = _is_heavy(cached["genres"])
                cached["heavy_score"] = cached.get("heavy_score", 0.0)
                results[band] = cached
                continue

            # Not cached — needs Last.fm lookup
            lastfm_candidates.append((band, artist))

        # Step 2: Parallel Last.fm lookups
        if lastfm_candidates:
            log.info("Running parallel Last.fm lookups for %d bands...", len(lastfm_candidates))

            def _do_lastfm(item):
                band, artist = item
                tags = _lastfm_lookup(artist)
                return (band, artist, tags)

            # 4 workers, Last.fm allows ~5 req/sec
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(_do_lastfm, item): item for item in lastfm_candidates}

                for future in as_completed(futures):
                    band, artist, tags = future.result()
                    if tags:
                        tags = tags[:15]
                        genres = [t["name"] for t in tags]
                        score = _calc_heavy_score(tags)
                        tags_json = json.dumps(tags)
                        _cache_set(artist, genres, "lastfm", conn=conn, heavy_score=score, tags_json=tags_json)
                        conn.commit()
                        log.info("Last.fm: %s → %s (score=%.2f)", artist, genres[:5], score)
                        results[band] = {"genres": genres, "source": "lastfm", "is_heavy": score >= HEAVY_THRESHOLD, "heavy_score": score}
                    else:
                        # Last.fm missed — queue for MusicBrainz
                        mb_candidates.append((band, artist))

        # Step 3: MusicBrainz batch queue with rate limiting (1 req/sec)
        # Cap at 30 MB lookups to prevent timeout — uncached bands next run
        MB_CAP = 30
        skipped_mb = []
        if len(mb_candidates) > MB_CAP:
            log.warning("Capping MusicBrainz lookups to %d of %d candidates", MB_CAP, len(mb_candidates))
            # Prioritise shorter (more likely real) artist names
            all_mb = sorted(mb_candidates, key=lambda x: len(x[1]))
            skipped_mb = all_mb[MB_CAP:]
            mb_candidates = all_mb[:MB_CAP]

        if mb_candidates:
            log.info("Running rate-limited MusicBrainz lookups for %d bands...", len(mb_candidates))
            for i, (band, artist) in enumerate(mb_candidates):
                if i > 0:
                    time.sleep(1.1)  # MusicBrainz allows 1 req/sec

                genres = _musicbrainz_lookup(artist)
                if genres:
                    score = _calc_heavy_score([{"name": g, "count": 50} for g in genres])
                    _cache_set(artist, genres, "musicbrainz", conn=conn, heavy_score=score)
                    conn.commit()
                    log.info("MusicBrainz: %s → %s (score=%.2f)", artist, genres[:5], score)
                    results[band] = {"genres": genres, "source": "musicbrainz", "is_heavy": score >= HEAVY_THRESHOLD, "heavy_score": score}
                else:
                    # Mark as tried so we don't retry next time
                    _cache_set(artist, [], "musicbrainz_tried", conn=conn)
                    conn.commit()
                    results[band] = {"genres": [], "source": "unknown", "is_heavy": False, "heavy_score": 0.0}

        # Mark remaining (uncapped) candidates as musicbrainz_tried so they get picked up next run
        for band, artist in skipped_mb:
            _cache_set(artist, [], "musicbrainz_tried", conn=conn)
            results[band] = {"genres": [], "source": "unknown", "is_heavy": False, "heavy_score": 0.0}
        conn.commit()

        return results

    finally:
        conn.close()


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

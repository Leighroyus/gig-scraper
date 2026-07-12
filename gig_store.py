#!/usr/bin/env python3
"""
Gig Store - DuckDB persistence for the gig scraper.
Dimensional schema: events, bands, genres, event_bands, band_genres.
"""

import json
import duckdb
import os
import re
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

try:
    import dateparser
except ImportError:
    dateparser = None

DB_PATH = os.environ.get("GIG_DB_PATH", os.path.join(os.path.dirname(__file__), "gigs.duckdb"))

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_NEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY,
    raw_title   VARCHAR NOT NULL,
    venue       VARCHAR NOT NULL,
    date        VARCHAR NOT NULL,
    date_iso    DATE,
    first_seen  TIMESTAMP NOT NULL,
    last_seen   TIMESTAMP NOT NULL,
    notified    BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS bands (
    band_id     INTEGER PRIMARY KEY,
    name        VARCHAR NOT NULL UNIQUE,
    genres      VARCHAR DEFAULT '[]',
    is_heavy    BOOLEAN DEFAULT FALSE,
    genre_source VARCHAR DEFAULT '',
    updated_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS genres (
    genre_id    INTEGER PRIMARY KEY,
    name        VARCHAR NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS event_bands (
    event_id    INTEGER REFERENCES events(event_id),
    band_id     INTEGER REFERENCES bands(band_id),
    PRIMARY KEY (event_id, band_id)
);

CREATE TABLE IF NOT EXISTS band_genres (
    band_id     INTEGER REFERENCES bands(band_id),
    genre_id    INTEGER REFERENCES genres(genre_id),
    PRIMARY KEY (band_id, genre_id)
);
"""

_OLD_FLAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS gigs (
    key         VARCHAR PRIMARY KEY,
    band        VARCHAR NOT NULL,
    venue       VARCHAR NOT NULL,
    date        VARCHAR NOT NULL,
    date_iso    DATE,
    first_seen  TIMESTAMP NOT NULL,
    last_seen   TIMESTAMP NOT NULL,
    notified    BOOLEAN DEFAULT FALSE,
    genres      VARCHAR DEFAULT '[]',
    is_heavy    BOOLEAN DEFAULT FALSE,
    genre_source VARCHAR DEFAULT ''
);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _connect(db_path: str = DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection and ensure the schema exists."""
    con = duckdb.connect(db_path)
    _ensure_new_schema(con)
    return con


def _has_table(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    """Check if a table exists."""
    return con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()[0] > 0


def _ensure_new_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Ensure the new dimensional tables exist."""
    if _has_table(con, "events"):
        return
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id    INTEGER PRIMARY KEY,
            raw_title   VARCHAR NOT NULL,
            venue       VARCHAR NOT NULL,
            date        VARCHAR NOT NULL,
            date_iso    DATE,
            first_seen  TIMESTAMP NOT NULL,
            last_seen   TIMESTAMP NOT NULL,
            notified    BOOLEAN DEFAULT FALSE
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS bands (
            band_id     INTEGER PRIMARY KEY,
            name        VARCHAR NOT NULL UNIQUE,
            genres      VARCHAR DEFAULT '[]',
            is_heavy    BOOLEAN DEFAULT FALSE,
            genre_source VARCHAR DEFAULT '',
            updated_at  TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS genres (
            genre_id    INTEGER PRIMARY KEY,
            name        VARCHAR NOT NULL UNIQUE
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS event_bands (
            event_id    INTEGER REFERENCES events(event_id),
            band_id     INTEGER REFERENCES bands(band_id),
            PRIMARY KEY (event_id, band_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS band_genres (
            band_id     INTEGER REFERENCES bands(band_id),
            genre_id    INTEGER REFERENCES genres(genre_id),
            PRIMARY KEY (band_id, genre_id)
        )
    """)


def _next_id(con: duckdb.DuckDBPyConnection, table: str, col: str) -> int:
    """Get the next available ID for a table."""
    max_id = con.execute(f"SELECT COALESCE(MAX({col}), 0) FROM {table}").fetchone()[0]
    return max_id + 1


def _ensure_old_flat(con: duckdb.DuckDBPyConnection) -> None:
    """Ensure the old flat gigs table exists (for migration)."""
    con.execute(_OLD_FLAT_SCHEMA)
    for col, default in [('date_iso', 'DATE'), ('genres', "VARCHAR DEFAULT '[]'"),
                         ('is_heavy', 'BOOLEAN DEFAULT FALSE'), ('genre_source', "VARCHAR DEFAULT ''")]:
        try:
            con.execute(f"SELECT {col} FROM gigs LIMIT 1")
        except Exception:
            con.execute(f"ALTER TABLE gigs ADD COLUMN {col} {default}")


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_from_flat(con: Optional[duckdb.DuckDBPyConnection] = None, db_path: str = DB_PATH) -> Dict:
    """Migrate data from old flat 'gigs' table to new dimensional schema.

    Returns dict with counts of migrated records.
    """
    own_conn = con is None
    if own_conn:
        con = duckdb.connect(db_path)
        _ensure_old_flat(con)

    # Check if old table has data
    has_gigs = _has_table(con, "gigs")
    has_events = _has_table(con, "events")

    if has_events:
        # Already migrated
        log.info("New schema already exists, skipping migration")
        return {"migrated": 0, "skipped": True}

    if not has_gigs:
        log.info("No old gigs table found, creating fresh schema")
        con.execute(_NEW_SCHEMA)
        return {"migrated": 0, "fresh": True}

    row_count = con.execute("SELECT COUNT(*) FROM gigs").fetchone()[0]
    if row_count == 0:
        log.info("Old gigs table is empty, creating fresh schema")
        con.execute(_NEW_SCHEMA)
        return {"migrated": 0, "empty": True}

    log.info("Migrating %d rows from flat gigs table to dimensional schema", row_count)

    # Create new schema
    con.execute(_NEW_SCHEMA)

    # Read all old data
    rows = con.execute(
        "SELECT band, venue, date, date_iso, first_seen, last_seen, notified, "
        "genres, is_heavy, genre_source FROM gigs ORDER BY first_seen"
    ).fetchall()

    migrated_events = 0
    migrated_bands = 0

    for band_name, venue, date, date_iso, first_seen, last_seen, notified, genres_json, is_heavy, genre_source in rows:
        # Insert event
        event_id = _next_id(con, 'events', 'event_id')
        con.execute(
            "INSERT INTO events (event_id, raw_title, venue, date, date_iso, first_seen, last_seen, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [event_id, band_name, venue, date, date_iso, first_seen, last_seen, notified],
        )
        migrated_events += 1

        # Upsert band
        band_row = con.execute("SELECT band_id FROM bands WHERE name = ?", [band_name]).fetchone()
        if band_row:
            band_id = band_row[0]
        else:
            band_id = _next_id(con, 'bands', 'band_id')
            con.execute(
                "INSERT INTO bands (band_id, name, genres, is_heavy, genre_source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [band_id, band_name, genres_json or '[]', is_heavy or False, genre_source or '', last_seen],
            )
            migrated_bands += 1

        # Link event ↔ band
        con.execute(
            "INSERT OR IGNORE INTO event_bands (event_id, band_id) VALUES (?, ?)",
            [event_id, band_id],
        )

        # Link band ↔ genres
        if genres_json:
            try:
                genres_list = json.loads(genres_json)
                for g in genres_list:
                    g = g.strip()
                    if not g:
                        continue
                    genre_row = con.execute("SELECT genre_id FROM genres WHERE name = ?", [g]).fetchone()
                    if not genre_row:
                        genre_id = _next_id(con, 'genres', 'genre_id')
                        con.execute("INSERT INTO genres (genre_id, name) VALUES (?, ?)", [genre_id, g])
                    else:
                        genre_id = genre_row[0]
                    con.execute(
                        "INSERT OR IGNORE INTO band_genres (band_id, genre_id) VALUES (?, ?)",
                        [band_id, genre_id],
                    )
            except (json.JSONDecodeError, TypeError):
                pass

    log.info("Migration complete: %d events, %d bands", migrated_events, migrated_bands)

    # Drop old table
    con.execute("DROP TABLE IF EXISTS gigs")

    # Sequences are kept — they have the correct next values

    if own_conn:
        con.close()

    return {"migrated": migrated_events, "bands": migrated_bands}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def normalize_date(raw: str) -> Optional[str]:
    """Parse a free-text date into ISO format (YYYY-MM-DD), or None."""
    if not raw or raw.strip().upper() == 'TBA':
        return None
    if dateparser is None:
        return None
    parsed = dateparser.parse(
        raw,
        settings={
            'DATE_ORDER': 'DMY',
            'PREFER_DAY_OF_MONTH': 'first',
            'RETURN_AS_TIMEZONE_AWARE': False,
        },
    )
    if parsed:
        return parsed.strftime('%Y-%m-%d')
    return None


def _event_key(raw_title: str, venue: str, date: str) -> str:
    """Create a stable key for deduplication (event-level, not per-band)."""
    title_clean = re.sub(r'[^\w]', '', raw_title.lower())[:80]
    venue_clean = re.sub(r'[^\w]', '', venue.lower())[:40]
    date_clean = re.sub(r'[^\w]', '', date.lower())[:20]
    return f"{title_clean}|{venue_clean}|{date_clean}"


# ---------------------------------------------------------------------------
# Core persistence
# ---------------------------------------------------------------------------

def upsert_gigs(gigs: List[Dict], db_path: str = DB_PATH) -> Dict:
    """Insert or update gigs. Splits multi-band titles into individual bands.

    Returns dict with 'new' and 'seen' lists (backward-compatible format).
    """
    from genre_lookup import split_artists

    now = datetime.now()
    new_gigs = []
    seen_gigs = []

    with _connect(db_path) as con:
        for gig in gigs:
            raw_title = gig['band']
            venue = gig['venue']
            date = gig['date']
            date_iso = normalize_date(date)

            # Check if event already exists (by raw_title + venue + date)
            existing = con.execute(
                "SELECT event_id FROM events WHERE raw_title = ? AND venue = ? AND date = ?",
                [raw_title, venue, date],
            ).fetchone()

            if existing:
                event_id = existing[0]
                con.execute(
                    "UPDATE events SET last_seen = ?, venue = ?, date = ?, date_iso = ? "
                    "WHERE event_id = ?",
                    [now, venue, date, date_iso, event_id],
                )
                seen_gigs.append(gig)
            else:
                event_id = _next_id(con, 'events', 'event_id')
                con.execute(
                    "INSERT INTO events (event_id, raw_title, venue, date, date_iso, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [event_id, raw_title, venue, date, date_iso, now, now],
                )
                new_gigs.append(gig)

            # Split title into bands
            band_names = split_artists(raw_title)
            if not band_names:
                # Fallback: treat the whole title as one band
                band_names = [raw_title]

            for band_name in band_names:
                # Upsert band
                band_row = con.execute(
                    "SELECT band_id FROM bands WHERE name = ?", [band_name]
                ).fetchone()
                if band_row:
                    band_id = band_row[0]
                else:
                    band_id = _next_id(con, 'bands', 'band_id')
                    con.execute(
                        "INSERT INTO bands (band_id, name, updated_at) VALUES (?, ?, ?)",
                        [band_id, band_name, now],
                    )

                # Link event ↔ band
                con.execute(
                    "INSERT OR IGNORE INTO event_bands (event_id, band_id) VALUES (?, ?)",
                    [event_id, band_id],
                )

    return {'new': new_gigs, 'seen': seen_gigs}


def mark_notified(gigs: List[Dict], db_path: str = DB_PATH) -> None:
    """Mark gigs as having been notified about."""
    now = datetime.now()
    with _connect(db_path) as con:
        for gig in gigs:
            con.execute(
                "UPDATE events SET notified = TRUE, last_seen = ? "
                "WHERE raw_title = ? AND venue = ? AND date = ?",
                [now, gig['band'], gig['venue'], gig['date']],
            )


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def cleanup_old_gigs(days: int = 90, db_path: str = DB_PATH) -> int:
    """Delete events whose ISO date is more than *days* ago. Returns count deleted."""
    if days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    with _connect(db_path) as con:
        count = con.execute(
            "SELECT COUNT(*) FROM events WHERE date_iso IS NOT NULL AND date_iso < ?",
            [cutoff],
        ).fetchone()[0]
        if count > 0:
            # Get event_ids to delete
            event_ids = con.execute(
                "SELECT event_id FROM events WHERE date_iso IS NOT NULL AND date_iso < ?",
                [cutoff],
            ).fetchall()
            ids = [r[0] for r in event_ids]
            placeholders = ','.join(['?'] * len(ids))
            con.execute(f"DELETE FROM event_bands WHERE event_id IN ({placeholders})", ids)
            con.execute(f"DELETE FROM events WHERE event_id IN ({placeholders})", ids)

            # Clean up orphaned bands (no remaining events)
            con.execute("""
                DELETE FROM band_genres WHERE band_id NOT IN (
                    SELECT DISTINCT band_id FROM event_bands
                )
            """)
            con.execute("""
                DELETE FROM bands WHERE band_id NOT IN (
                    SELECT DISTINCT band_id FROM event_bands
                )
            """)

        return count


# ---------------------------------------------------------------------------
# Queries (backward-compatible)
# ---------------------------------------------------------------------------

def get_all_gigs(db_path: str = DB_PATH) -> List[Dict]:
    """Get all known gigs, joined across tables. Returns same format as old flat schema."""
    with _connect(db_path) as con:
        rows = con.execute("""
            SELECT e.raw_title, b.name AS band, e.venue, e.date, e.date_iso,
                   e.first_seen, e.last_seen, e.notified,
                   b.genres, b.is_heavy, b.genre_source
            FROM events e
            JOIN event_bands eb ON e.event_id = eb.event_id
            JOIN bands b ON eb.band_id = b.band_id
            ORDER BY COALESCE(e.date_iso, '9999-12-31'), e.last_seen DESC
        """).fetchall()

    # Flatten to match old format: each row = one band at one event
    result = []
    for r in rows:
        result.append({
            'band': r[1],          # band name (cleaned via split_artists)
            'venue': r[2],
            'date': r[3],
            'date_iso': r[4],
            'first_seen': r[5],
            'last_seen': r[6],
            'notified': r[7],
            'genres': json.loads(r[8]) if r[8] else [],
            'is_heavy': r[9] or False,
            'genre_source': r[10] or '',
        })
    return result


def get_new_gigs(db_path: str = DB_PATH) -> List[Dict]:
    """Get gigs that haven't been notified yet."""
    with _connect(db_path) as con:
        rows = con.execute("""
            SELECT b.name AS band, e.venue, e.date, e.date_iso
            FROM events e
            JOIN event_bands eb ON e.event_id = eb.event_id
            JOIN bands b ON eb.band_id = b.band_id
            WHERE e.notified = FALSE
            ORDER BY COALESCE(e.date_iso, '9999-12-31')
        """).fetchall()
    return [
        {'band': r[0], 'venue': r[1], 'date': r[2], 'date_iso': r[3]}
        for r in rows
    ]


def get_stats(db_path: str = DB_PATH) -> Dict:
    """Get summary stats across the dimensional schema."""
    with _connect(db_path) as con:
        total_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        total_bands = con.execute("SELECT COUNT(*) FROM bands").fetchone()[0]
        heavy_bands = con.execute("SELECT COUNT(*) FROM bands WHERE is_heavy = TRUE").fetchone()[0]
        new_unseen = con.execute("SELECT COUNT(*) FROM events WHERE notified = FALSE").fetchone()[0]
        total_genres = con.execute("SELECT COUNT(*) FROM genres").fetchone()[0]
        return {
            'total': total_events,
            'bands': total_bands,
            'heavy': heavy_bands,
            'new_unseen': new_unseen,
            'genres': total_genres,
        }


def update_gig_genres(band: str, genres: List[str], is_heavy: bool, source: str, db_path: str = DB_PATH) -> None:
    """Update genre info for a band in the bands table (and junction tables)."""
    now = datetime.now()
    with _connect(db_path) as con:
        band_row = con.execute("SELECT band_id FROM bands WHERE name = ?", [band]).fetchone()
        if not band_row:
            return
        band_id = band_row[0]

        # Update bands table (JSON genres for backward compat)
        con.execute(
            "UPDATE bands SET genres = ?, is_heavy = ?, genre_source = ?, updated_at = ? WHERE band_id = ?",
            [json.dumps(genres), is_heavy, source, now, band_id],
        )

        # Update band_genres junction
        con.execute("DELETE FROM band_genres WHERE band_id = ?", [band_id])
        for g in genres:
            g = g.strip()
            if not g:
                continue
            genre_row = con.execute("SELECT genre_id FROM genres WHERE name = ?", [g]).fetchone()
            if not genre_row:
                genre_id = _next_id(con, 'genres', 'genre_id')
                con.execute("INSERT INTO genres (genre_id, name) VALUES (?, ?)", [genre_id, g])
            else:
                genre_id = genre_row[0]
            con.execute(
                "INSERT OR IGNORE INTO band_genres (band_id, genre_id) VALUES (?, ?)",
                [band_id, genre_id],
            )


# ---------------------------------------------------------------------------
# Run migration if needed (import-time)
# ---------------------------------------------------------------------------

def _auto_migrate():
    """Check if migration is needed and run it."""
    try:
        con = duckdb.connect(DB_PATH)
        has_events = _has_table(con, "events")
        has_gigs = _has_table(con, "gigs")
        if has_gigs and not has_events:
            con.close()
            migrate_from_flat(db_path=DB_PATH)
        else:
            con.close()
    except Exception as e:
        log.warning("Auto-migration check failed: %s", e)


_auto_migrate()

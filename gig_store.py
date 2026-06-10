#!/usr/bin/env python3
"""
Gig Store - DuckDB persistence for the gig scraper.
Tracks seen gigs for deduplication across runs.
"""

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
# Context-managed connection
# ---------------------------------------------------------------------------

def _connect(db_path: str = DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection and ensure the schema exists."""
    con = duckdb.connect(db_path)
    _ensure_table(con)
    return con


def _ensure_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS gigs (
            key         VARCHAR PRIMARY KEY,
            band        VARCHAR NOT NULL,
            venue       VARCHAR NOT NULL,
            date        VARCHAR NOT NULL,
            date_iso    DATE,
            first_seen  TIMESTAMP NOT NULL,
            last_seen   TIMESTAMP NOT NULL,
            notified    BOOLEAN DEFAULT FALSE
        )
    """)
    # Migrate: add date_iso if missing (existing DBs)
    try:
        con.execute("SELECT date_iso FROM gigs LIMIT 1")
    except Exception:
        con.execute("ALTER TABLE gigs ADD COLUMN date_iso DATE")


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def normalize_key(band: str, venue: str, date: str) -> str:
    """Create a stable key from band + venue + date."""
    band_clean = re.sub(r'[^\w]', '', band.lower())
    venue_clean = re.sub(r'[^\w]', '', venue.lower())
    date_clean = re.sub(r'[^\w]', '', date.lower())
    return f"{band_clean}|{venue_clean}|{date_clean}"


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


# ---------------------------------------------------------------------------
# Core persistence
# ---------------------------------------------------------------------------

def upsert_gigs(gigs: List[Dict], db_path: str = DB_PATH) -> Dict:
    """
    Insert or update gigs. Returns dict with 'new' and 'seen' lists.
    New = never seen before. Seen = existed in a previous run.
    """
    now = datetime.now()
    new_gigs = []
    seen_gigs = []

    with _connect(db_path) as con:
        for gig in gigs:
            key = normalize_key(gig['band'], gig['venue'], gig['date'])
            date_iso = normalize_date(gig['date'])

            existing = con.execute("SELECT key FROM gigs WHERE key = ?", [key]).fetchone()

            if existing:
                con.execute(
                    "UPDATE gigs SET last_seen = ?, band = ?, venue = ?, date = ?, date_iso = ? WHERE key = ?",
                    [now, gig['band'], gig['venue'], gig['date'], date_iso, key],
                )
                seen_gigs.append(gig)
            else:
                con.execute(
                    "INSERT INTO gigs (key, band, venue, date, date_iso, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [key, gig['band'], gig['venue'], gig['date'], date_iso, now, now],
                )
                new_gigs.append(gig)

    return {'new': new_gigs, 'seen': seen_gigs}


def mark_notified(gigs: List[Dict], db_path: str = DB_PATH) -> None:
    """Mark gigs as having been notified about."""
    now = datetime.now()
    with _connect(db_path) as con:
        for gig in gigs:
            key = normalize_key(gig['band'], gig['venue'], gig['date'])
            con.execute("UPDATE gigs SET notified = TRUE, last_seen = ? WHERE key = ?", [now, key])


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def cleanup_old_gigs(days: int = 90, db_path: str = DB_PATH) -> int:
    """Delete gigs whose ISO date is more than *days* ago. Returns count deleted."""
    if days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    with _connect(db_path) as con:
        # Count first, then delete
        count = con.execute(
            "SELECT COUNT(*) FROM gigs WHERE date_iso IS NOT NULL AND date_iso < ?", [cutoff]
        ).fetchone()[0]
        if count > 0:
            con.execute("DELETE FROM gigs WHERE date_iso IS NOT NULL AND date_iso < ?", [cutoff])
        return count


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_all_gigs(db_path: str = DB_PATH) -> List[Dict]:
    """Get all known gigs."""
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT band, venue, date, date_iso, first_seen, last_seen, notified "
            "FROM gigs ORDER BY COALESCE(date_iso, '9999-12-31'), last_seen DESC"
        ).fetchall()
    return [
        {
            'band': r[0], 'venue': r[1], 'date': r[2], 'date_iso': r[3],
            'first_seen': r[4], 'last_seen': r[5], 'notified': r[6],
        }
        for r in rows
    ]


def get_stats(db_path: str = DB_PATH) -> Dict:
    """Get summary stats."""
    with _connect(db_path) as con:
        total = con.execute("SELECT COUNT(*) FROM gigs").fetchone()[0]
        new_unseen = con.execute("SELECT COUNT(*) FROM gigs WHERE notified = FALSE").fetchone()[0]
    return {'total': total, 'new_unseen': new_unseen}


def get_new_gigs(db_path: str = DB_PATH) -> List[Dict]:
    """Get gigs that haven't been notified yet."""
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT band, venue, date, date_iso FROM gigs "
            "WHERE notified = FALSE ORDER BY COALESCE(date_iso, '9999-12-31')"
        ).fetchall()
    return [
        {'band': r[0], 'venue': r[1], 'date': r[2], 'date_iso': r[3]}
        for r in rows
    ]

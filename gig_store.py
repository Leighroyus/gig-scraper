#!/usr/bin/env python3
"""
Gig Store - DuckDB persistence for the gig scraper.
Tracks seen gigs for deduplication across runs.
"""

import duckdb
import os
import re
from datetime import datetime
from typing import List, Dict, Optional


DB_PATH = os.environ.get("GIG_DB_PATH", os.path.join(os.path.dirname(__file__), "gigs.duckdb"))


def _normalize(band: str, venue: str, date: str) -> str:
    """Create a stable key from band + venue + date."""
    band_clean = re.sub(r'[^\w]', '', band.lower())
    venue_clean = re.sub(r'[^\w]', '', venue.lower())
    date_clean = re.sub(r'[^\w]', '', date.lower())
    return f"{band_clean}|{venue_clean}|{date_clean}"


def _ensure_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS gigs (
            key         VARCHAR PRIMARY KEY,
            band        VARCHAR NOT NULL,
            venue       VARCHAR NOT NULL,
            date        VARCHAR NOT NULL,
            first_seen  TIMESTAMP NOT NULL,
            last_seen   TIMESTAMP NOT NULL,
            notified    BOOLEAN DEFAULT FALSE
        )
    """)


def upsert_gigs(gigs: List[Dict], db_path: str = DB_PATH) -> Dict:
    """
    Insert or update gigs. Returns dict with 'new' and 'seen' lists.
    New = never seen before. Seen = existed in a previous run.
    """
    con = duckdb.connect(db_path)
    _ensure_table(con)

    now = datetime.now()
    new_gigs = []
    seen_gigs = []

    for gig in gigs:
        key = _normalize(gig['band'], gig['venue'], gig['date'])
        existing = con.execute("SELECT key FROM gigs WHERE key = ?", [key]).fetchone()

        if existing:
            con.execute(
                "UPDATE gigs SET last_seen = ?, band = ?, venue = ?, date = ? WHERE key = ?",
                [now, gig['band'], gig['venue'], gig['date'], key]
            )
            seen_gigs.append(gig)
        else:
            con.execute(
                "INSERT INTO gigs (key, band, venue, date, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                [key, gig['band'], gig['venue'], gig['date'], now, now]
            )
            new_gigs.append(gig)

    con.close()
    return {'new': new_gigs, 'seen': seen_gigs}


def mark_notified(gigs: List[Dict], db_path: str = DB_PATH) -> None:
    """Mark gigs as having been notified about."""
    con = duckdb.connect(db_path)
    _ensure_table(con)
    now = datetime.now()
    for gig in gigs:
        key = _normalize(gig['band'], gig['venue'], gig['date'])
        con.execute("UPDATE gigs SET notified = TRUE, last_seen = ? WHERE key = ?", [now, key])
    con.close()


def get_all_gigs(db_path: str = DB_PATH) -> List[Dict]:
    """Get all known gigs."""
    con = duckdb.connect(db_path)
    _ensure_table(con)
    rows = con.execute("SELECT band, venue, date, first_seen, last_seen, notified FROM gigs ORDER BY last_seen DESC").fetchall()
    con.close()
    return [
        {'band': r[0], 'venue': r[1], 'date': r[2], 'first_seen': r[3], 'last_seen': r[4], 'notified': r[5]}
        for r in rows
    ]


def get_stats(db_path: str = DB_PATH) -> Dict:
    """Get summary stats."""
    con = duckdb.connect(db_path)
    _ensure_table(con)
    total = con.execute("SELECT COUNT(*) FROM gigs").fetchone()[0]
    new_unseen = con.execute("SELECT COUNT(*) FROM gigs WHERE notified = FALSE").fetchone()[0]
    con.close()
    return {'total': total, 'new_unseen': new_unseen}

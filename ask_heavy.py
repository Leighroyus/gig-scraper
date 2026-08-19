#!/usr/bin/env python3
"""
Ask Heavy — Interactive tool to classify borderline bands via WhatsApp.

Usage:
    # Find all borderline bands (score between 0.35 and 0.65)
    python3 ask_heavy.py

    # Find borderline bands with upcoming events
    python3 ask_heavy.py --upcoming

    # Mark a band as heavy/not heavy
    python3 ask_heavy.py --band "Frenzal Rhomb" --yes
    python3 ask_heavy.py --band "Frenzal Rhomb" --no
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import duckdb

DB_PATH = os.environ.get("GIG_DB_PATH", os.path.join(os.path.dirname(__file__), "gigs.duckdb"))


def get_borderline_bands(upcoming_only=False, days=30):
    """Find bands in the borderline zone (score 0.35-0.65)."""
    con = duckdb.connect(DB_PATH, read_only=True)

    query = """
        SELECT b.band_id, b.name, b.genres, b.heavy_score, b.genre_source,
               COUNT(DISTINCT e.event_id) as event_count,
               MIN(e.date_iso) as next_event
        FROM bands b
        LEFT JOIN event_bands eb ON b.band_id = eb.band_id
        LEFT JOIN events e ON eb.event_id = e.event_id
        WHERE b.heavy_score >= 0.35
          AND b.heavy_score < 0.65
          AND b.heavy_source = 'auto'
    """

    if upcoming_only:
        cutoff = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')
        query += f" AND e.date_iso >= '{cutoff}'"

    query += """
        GROUP BY b.band_id, b.name, b.genres, b.heavy_score, b.genre_source
        ORDER BY b.heavy_score DESC
    """

    results = con.execute(query).fetchall()
    con.close()

    return [
        {
            "band_id": r[0],
            "name": r[1],
            "genres": json.loads(r[2]) if r[2] else [],
            "heavy_score": r[3],
            "source": r[4],
            "event_count": r[5],
            "next_event": str(r[6]) if r[6] else None,
        }
        for r in results
    ]


def mark_band_heavy(band_name, is_heavy, source="manual"):
    """Mark a band as heavy or not heavy."""
    con = duckdb.connect(DB_PATH)

    band_row = con.execute("SELECT band_id FROM bands WHERE name = ?", [band_name]).fetchone()
    if not band_row:
        print(f"Band not found: {band_name}")
        con.close()
        return False

    band_id = band_row[0]
    score = 1.0 if is_heavy else 0.0

    con.execute(
        "UPDATE bands SET is_heavy = ?, heavy_score = ?, heavy_source = ?, updated_at = CURRENT_TIMESTAMP WHERE band_id = ?",
        [is_heavy, score, source, band_id],
    )
    con.close()
    print(f"{'🔥 Marked' if is_heavy else '✓ Marked'} {band_name} as {'heavy' if is_heavy else 'not heavy'}")
    return True


def format_borderline_message(band):
    """Format a WhatsApp-style message asking about a borderline band."""
    genres_str = ", ".join(band["genres"][:5]) if band["genres"] else "unknown"
    score = band["heavy_score"]
    events = band["event_count"]

    msg = f"🤔 *Is {band['name']} heavy?*\n\n"
    msg += f"Tags: {genres_str}\n"
    msg += f"Confidence: {score:.0%} (borderline)\n"

    if band["next_event"]:
        msg += f"Next event: {band['next_event']}"
        if events > 1:
            msg += f" ({events} upcoming)"
        msg += "\n"

    msg += "\nReply: yes / no"
    return msg


def main():
    parser = argparse.ArgumentParser(description="Manage heavy band classifications")
    parser.add_argument("--band", help="Band name to classify")
    parser.add_argument("--yes", action="store_true", help="Mark as heavy")
    parser.add_argument("--no", action="store_true", help="Mark as not heavy")
    parser.add_argument("--upcoming", action="store_true", help="Only show bands with upcoming events")
    parser.add_argument("--days", type=int, default=30, help="Days ahead for upcoming filter")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.band:
        if args.yes:
            mark_band_heavy(args.band, True)
        elif args.no:
            mark_band_heavy(args.band, False)
        else:
            print("Specify --yes or --no with --band")
        return

    borderline = get_borderline_bands(upcoming_only=args.upcoming, days=args.days)

    if not borderline:
        print("No borderline bands found.")
        return

    if args.json:
        print(json.dumps(borderline, indent=2))
        return

    print(f"Found {len(borderline)} borderline bands:\n")
    for b in borderline:
        print(f"  {b['name']:30s}  score={b['heavy_score']:.2f}  genres={', '.join(b['genres'][:3])}")
        if b["next_event"]:
            print(f"  {'':30s}  next: {b['next_event']} ({b['event_count']} events)")
        print()


if __name__ == "__main__":
    main()

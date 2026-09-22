#!/usr/bin/env python3
"""Re-score all cached bands with the current heavy-scoring model.

Updates genre_cache.db (SQLite) and gigs.duckdb (bands table) in place.
Uses tags_json (Last.fm counts) where available; otherwise falls back to
the synthetic count=50 used for MusicBrainz/DuckDB-seeded entries.
"""

import json
import logging
import sqlite3

from genre_lookup import _calc_heavy_score, _band_key, _is_junk_tag, HEAVY_THRESHOLD, CACHE_DB

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("rescore")

import os

duckdb_path = os.environ.get('GIG_DB_PATH', os.path.join(os.path.dirname(__file__), 'gigs.duckdb'))


def main(dry_run: bool = False):
    con = sqlite3.connect(CACHE_DB)
    rows = con.execute(
        "SELECT band_key, genres, tags_json, source, heavy_score FROM genre_cache"
    ).fetchall()

    cache_changes = []  # (band_key, old_score, new_score, source)
    for band_key, genres_json, tags_json, source, old_score in rows:
        if tags_json:
            try:
                tags = json.loads(tags_json)
            except (json.JSONDecodeError, TypeError):
                tags = None
        else:
            tags = None
        if not tags:
            try:
                genres = json.loads(genres_json) if genres_json else []
            except (json.JSONDecodeError, TypeError):
                genres = []
            if not genres:
                continue  # skipped/unknown entries — nothing to score
            # Legacy MB/seed rows stored the full unfiltered tag list; keep
            # order (approx. vote order) but junk-filter and cap at 10 to
            # match the live _musicbrainz_lookup behaviour.
            genres = [g for g in genres if not _is_junk_tag(g)][:10]
            if not genres:
                continue
            tags = [{"name": g, "count": 50} for g in genres]

        score = _calc_heavy_score(tags)
        if abs((old_score or 0.0) - score) > 0.001:
            cache_changes.append((band_key, old_score or 0.0, score, source))
            if not dry_run:
                con.execute(
                    "UPDATE genre_cache SET heavy_score = ? WHERE band_key = ?",
                    [score, band_key],
                )
    if not dry_run:
        con.commit()
    con.close()

    up = sum(1 for _, o, n, _ in cache_changes if o < HEAVY_THRESHOLD <= n)
    down = sum(1 for _, o, n, _ in cache_changes if o >= HEAVY_THRESHOLD > n)
    log.info("Cache: %d bands re-scored (%d score changes, %d gained heavy, %d lost heavy)",
             len(rows), len(cache_changes), up, down)

    # --- Sync DuckDB bands table ---
    import duckdb
    db = duckdb.connect(duckdb_path)
    bands = db.execute(
        "SELECT band_id, name, heavy_score, heavy_source FROM bands"
    ).fetchall()

    duck_changes = []
    for band_id, name, old_score, heavy_source in bands:
        key = _band_key(name)
        new_score = next((n for k, _, n, _ in cache_changes if k == key), None)
        if new_score is None:
            continue
        if heavy_source == "manual":
            continue  # don't touch manually-classified bands
        old_heavy = (old_score or 0.0) >= HEAVY_THRESHOLD
        new_heavy = new_score >= HEAVY_THRESHOLD
        if abs((old_score or 0.0) - new_score) > 0.001:
            duck_changes.append((name, old_score or 0.0, new_score, old_heavy, new_heavy))
            if not dry_run:
                db.execute(
                    "UPDATE bands SET heavy_score = ?, is_heavy = ? WHERE band_id = ?",
                    [new_score, new_heavy, band_id],
                )
    if not dry_run:
        db.commit()
    db.close()

    d_up = sum(1 for c in duck_changes if not c[3] and c[4])
    d_down = sum(1 for c in duck_changes if c[3] and not c[4])
    log.info("DuckDB: %d bands updated (%d gained heavy, %d lost heavy)",
             len(duck_changes), d_up, d_down)

    print("\n=== Biggest score changes ===")
    all_changes = sorted(cache_changes, key=lambda x: abs(x[2] - x[1]), reverse=True)[:25]
    for k, o, n, src in all_changes:
        arrow = "UP  " if n > o else "down"
        print(f"  {arrow} {k:35s} {o:.2f} -> {n:.2f} ({src})")


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if dry:
        log.info("DRY RUN — no changes will be written")
    main(dry_run=dry)
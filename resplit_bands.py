#!/usr/bin/env python3
"""Re-split multi-band entries and enrich with genres."""

from gig_store import _connect, _next_id
from genre_lookup import split_artists, clean_artist_name, lookup_genres
import json, time, logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger(__name__)

def main():
    con = _connect()

    rows = con.execute('''
        SELECT DISTINCT b.band_id, b.name 
        FROM bands b
        WHERE b.name LIKE '%w/%' 
           OR b.name LIKE '% with %' 
           OR b.name LIKE '% + %'
           OR b.name LIKE '% feat.%'
           OR b.name LIKE '% featuring %'
    ''').fetchall()

    log.info('Found %d multi-band entries to check', len(rows))

    fixed = 0
    for band_id, name in rows:
        bands = split_artists(name)
        if len(bands) <= 1:
            continue
        
        clean_bands = [b.strip() for b in bands if len(b.strip()) > 2 and len(b.strip()) < 80]
        if len(clean_bands) < 2:
            continue
        
        log.info('Splitting: "%s"', name[:70])
        log.info('  → %s', clean_bands)
        
        for band_name in clean_bands:
            cleaned = clean_artist_name(band_name)
            if not cleaned or len(cleaned) < 2:
                continue
            existing = con.execute('SELECT band_id FROM bands WHERE name = ?', [cleaned]).fetchone()
            if not existing:
                result = lookup_genres(cleaned)
                new_id = _next_id(con, 'bands', 'band_id')
                con.execute(
                    'INSERT INTO bands (band_id, name, genres, is_heavy, genre_source, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
                    [new_id, cleaned, json.dumps(result['genres']), result['is_heavy'], result['source']]
                )
                log.info('  + %s → %s', cleaned, result['genres'][:3])
                time.sleep(0.3)
        fixed += 1

    total_bands = con.execute('SELECT COUNT(*) FROM bands').fetchone()[0]
    heavy = con.execute('SELECT COUNT(*) FROM bands WHERE is_heavy = TRUE').fetchone()[0]
    log.info('\nDone. Split %d entries. Total bands: %d, Heavy: %d', fixed, total_bands, heavy)
    con.close()

if __name__ == '__main__':
    main()

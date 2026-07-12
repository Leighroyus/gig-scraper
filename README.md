# Gig Scraper

A CLI tool that scrapes gig listings from Melbourne, Geelong, and Surf Coast venues. Persists results in DuckDB with a dimensional schema (events → bands → genres). Enriches bands with genre data from Last.fm and MusicBrainz.

## Installation

```bash
cd ~/projects/gig_scraper
pip install -r requirements.txt
python -m playwright install chromium
```

## Setup

1. Get a free Last.fm API key at https://www.last.fm/api/account/create
2. Set it in `.env`:
```
LASTFM_API_KEY=your_key_here
```

## Usage

```bash
# Scrape all venues
python3 gig_scraper.py --region all

# Only show gigs not seen in previous runs
python3 gig_scraper.py --region all --new-only

# Enrich all bands with genres
python3 gig_scraper.py --enrich-genres

# Filter for heavy (metal/punk/hardcore) only
python3 gig_scraper.py --genre heavy

# Combine: enrich + filter heavy
python3 gig_scraper.py --enrich-genres --genre heavy

# JSON output
python3 gig_scraper.py --region all --format json
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--region` | `all` | Region to scrape: `melbourne`, `geelong`, `surfcoast`, `all` |
| `--format` | `text` | Output format: `text`, `json` |
| `--limit` | `10` | Max gigs per venue (applied after non-gig filtering) |
| `--delay` | `2.0` | Delay between requests (seconds) |
| `--new-only` | off | Only output gigs not seen in previous runs |
| `--enrich-genres` | off | Look up genres for all bands via Last.fm/MusicBrainz |
| `--genre` | none | Filter output by genre (e.g. `heavy`) |
| `--db-path` | `gigs.duckdb` | Custom path to DuckDB database |
| `--output` | none | Write output to file |
| `--db-cleanup-days` | `90` | Delete gigs older than N days from DB (0=disable) |

## Venues

**Melbourne:**
- The Corner Hotel — Richmond
- The Tote — Collingwood
- Max Watts — Melbourne CBD
- Shotkickers — Thornbury
- Bendigo Hotel — Collingwood
- Night Hawks — Fitzroy
- Cherry Bar — Melbourne CBD
- The Old Bar — Fitzroy
- The Evelyn Hotel — Fitzroy
- Kindred Studios — Footscray
- The Croxton Bandroom — Thornbury

**Geelong:**
- Barwon Club — South Geelong
- Barwon Heads Hotel — Barwon Heads

**Surf Coast:**
- Torquay Hotel — Torquay

## Genre Enrichment

The scraper enriches bands with genre tags from Last.fm (primary) and MusicBrainz (fallback). Results are cached in SQLite (`genre_cache.db`) so each band is only looked up once.

```bash
# Enrich all bands
python3 gig_scraper.py --enrich-genres

# Filter for heavy genres (metal, punk, hardcore, doom, sludge, etc.)
python3 gig_scraper.py --enrich-genres --genre heavy
```

### How band splitting works

Multi-band event titles are automatically split into individual bands:
- `"Frenzal Rhomb w/ Ceres"` → `["Frenzal Rhomb", "Ceres"]`
- `"METALCORE MAYHEM! TARIOT (SG) + NEW MILLION + SUNDREAMER + DETESTOR"` → 4 bands
- `"Maajela Man Ke Andar EP Launch w/Neon Goblin and Black Wattle Witches"` → 3 bands

Separators: `w/`, `with`, `+`, `feat.`/`featuring`/`ft.`, `and`

## Database Schema

The DuckDB database uses a dimensional model:

```
events (event_id, raw_title, venue, date, date_iso, first_seen, last_seen, notified)
  ├── event_bands (event_id → band_id)
  │     └── bands (band_id, name, genres, is_heavy, genre_source, updated_at)
  │           └── band_genres (band_id → genre_id)
  │                 └── genres (genre_id, name)
```

- **events** — one row per scraped event
- **bands** — unique band names with genre data
- **genres** — unique genre tags
- **event_bands** — links events to bands (multi-band bills supported)
- **band_genres** — links bands to genres

```bash
# Check stats
python3 -c "from gig_store import get_stats; print(get_stats())"
# → {'total': 376, 'bands': 636, 'heavy': 108, 'genres': 448}

# Query all gigs
python3 -c "from gig_store import get_all_gigs; [print(f'{g[\"band\"]} | {g[\"venue\"]}') for g in get_all_gigs()]"
```

## Venue Configuration

Venues are configured in `venues.json`. Each venue can have:

- `name` — display name
- `url` — venue website
- `type` — `js` (Playwright), `scrapeops` (proxy), or omit for static HTML
- `selectors` — CSS selectors for `container`, `title`, `date`
- `exclude_patterns` — regex patterns to filter out non-gig entries
- `timeout`, `wait_time` — Playwright timeout overrides
- `wait_for_selector` — CSS selector to wait for before extracting content

### Adding a new venue

1. Inspect the venue website to find the event listing page
2. Identify CSS selectors for event containers, titles, and dates
3. Add to `venues.json` with appropriate `type` (static or `js`)
4. Run `python3 gig_scraper.py --region <region>` to test

## Example

```bash
# Full scrape, all regions
python3 gig_scraper.py --region all --limit 10

# New gigs only, save to file
python3 gig_scraper.py --region all --new-only --output new_gigs.json

# Melbourne only, JSON
python3 gig_scraper.py --region melbourne --format json

# Heavy gigs only
python3 gig_scraper.py --enrich-genres --genre heavy --region melbourne
```

## Cron Job

Runs every 3 days at 10:00 AM Melbourne time via OpenClaw cron. Enriches genres and sends only heavy (metal/punk/hardcore) gigs via WhatsApp.

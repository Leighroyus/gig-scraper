# Gig Scraper

A CLI tool that scrapes gig listings from Melbourne, Geelong, and Surf Coast venues. Persists results in DuckDB for cross-run deduplication.

## Installation

```bash
cd ~/projects/gig_scraper
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```bash
# Scrape all venues
python3 gig_scraper.py --region all

# Only show gigs not seen in previous runs
python3 gig_scraper.py --region all --new-only

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

**Geelong:**
- Barwon Club — South Geelong
- The Old Bar — Geelong
- Barwon Heads Hotel — Barwon Heads

**Surf Coast:**
- Torquay Hotel — Torquay

## Persistence & Deduplication

Gigs are stored in a DuckDB database (`gigs.duckdb` alongside the scraper). Each gig is tracked by a composite key of **band + venue + date** (normalized, case-insensitive).

- `first_seen` — when the gig first appeared in a scrape
- `last_seen` — most recent scrape that found it
- `notified` — whether it's been included in `--new-only` output
- `date_iso` — parsed ISO date for sorting and pruning

Use `--new-only` to only see gigs added since the last run. This is the default for the cron job, so you only get pinged when there's something new.

```bash
# Check what's in the database
python3 -c "from gig_store import get_all_gigs, get_stats; print(get_stats()); [print(f'{g[\"band\"]} | {g[\"venue\"]} | {g[\"date\"]}') for g in get_all_gigs()]"
```

## Venue Configuration

Venues are configured in `venues.json`. Each venue can have:

- `name` — display name
- `url` — venue website
- `type` — `js` (Playwright), `scrapeops` (proxy), or omit for static HTML
- `selectors` — CSS selectors for `container`, `title`, `date`
- `exclude_patterns` — regex patterns to filter out non-gig entries specific to this venue
- `requires_js` — boolean, alternative to `type: "js"`
- `timeout`, `wait_time` — Playwright timeout overrides
- `wait_for_selector` — CSS selector to wait for before extracting content

## Example

```bash
# Full scrape, all regions
python3 gig_scraper.py --region all --limit 10

# New gigs only, save to file
python3 gig_scraper.py --region all --new-only --output new_gigs.json

# Melbourne only, JSON
python3 gig_scraper.py --region melbourne --format json
```

## Cron Job

Runs every 3 days at 10:00 AM Melbourne time via OpenClaw cron. Uses `--new-only` to avoid duplicate notifications.

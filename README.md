# Gig Scraper

A CLI tool that scrapes gig listings from Melbourne, Geelong, and Surf Coast venues.

## Installation

```bash
cd ~/projects/TalkingToClaude
pip install -r requirements.txt
```

## Usage

```bash
python gig_scraper.py
```

## Options

### --region
Choose which region to scrape:
- `melbourne` - Melbourne venues only
- `geelong` - Geelong venues only  
- `surfcoast` - Surf Coast venues only
- `all` (default) - All regions

### --format
Choose output format:
- `text` (default) - Human-readable text
- `json` - JSON output

## Venues

**Melbourne:**
- Corner Hotel
- The Tote

**Geelong:**
- Workers Club Geelong

**Surf Coast:**
- Torquay Hotel

## Example

```bash
# Scrape all regions, text output
python gig_scraper.py --region all

# Scrape Melbourne only, JSON output
python gig_scraper.py --region melbourne --format json
```

#!/usr/bin/env python3
"""
Gig Scraper CLI - Scrape upcoming gigs from venues in Melbourne, Geelong, and Surf Coast
"""

import argparse
import os
import sys
import json
import re
import time
import logging
from typing import List, Dict, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML acquisition helpers
# ---------------------------------------------------------------------------

def _fetch_static(session: requests.Session, url: str, max_retries: int = 3, base_delay: float = 1.0) -> str:
    """Fetch a URL with retries and exponential backoff. Returns HTML string."""
    for attempt in range(max_retries):
        try:
            response = session.get(url, timeout=10)
            response.raise_for_status()
            return response.text
        except (requests.RequestException, requests.HTTPError) as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            log.warning("Request failed (attempt %d/%d): %s. Retrying in %ds…", attempt + 1, max_retries, e, delay)
            time.sleep(delay)
    raise requests.RequestException(f"Failed to fetch {url} after {max_retries} attempts")


def _fetch_scrapeops(session: requests.Session, url: str, api_key: str) -> str:
    """Fetch via ScrapeOps proxy. Returns HTML string."""
    if not api_key:
        raise ValueError("ScrapeOps API key is blank — set SCRAPEOPS_API_KEY")
    proxy_url = "https://proxy.scrapeops.io/v1/"
    params = {"api_key": api_key, "url": url, "render_js": "true"}
    response = session.get(proxy_url, params=params, timeout=120)
    response.raise_for_status()
    return response.text


def _fetch_playwright(url: str, wait_for_selector: str = None, timeout: int = 30000, wait_time: int = 5000, browser=None) -> str:
    """Fetch JS-rendered content via Playwright. Returns HTML string.

    If *browser* is passed the caller owns its lifecycle; otherwise a
    throwaway browser is launched and closed.
    """
    owns_browser = browser is None
    if owns_browser:
        _pw = sync_playwright().start()
        browser = _pw.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.set_extra_http_headers({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        page.goto(url, timeout=timeout)

        if wait_for_selector:
            try:
                page.wait_for_selector(wait_for_selector, timeout=10000)
            except Exception:
                log.warning("Selector %s not found, proceeding anyway", wait_for_selector)
        else:
            try:
                page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                page.wait_for_timeout(wait_time)

        content = page.content()
        page.close()
        return content
    finally:
        if owns_browser:
            browser.close()
            _pw.stop()


# ---------------------------------------------------------------------------
# Event parsing (shared across all venue types)
# ---------------------------------------------------------------------------

def _parse_events(html: str, venue: Dict, event_limit: int) -> List[Dict]:
    """Extract gig dicts from rendered HTML using venue selectors."""
    soup = BeautifulSoup(html, 'html.parser')
    selectors = venue.get('selectors')

    if selectors:
        events = soup.select(selectors['container'])
    else:
        event_selectors = [
            '[class*="event"]', '[class*="show"]', '[class*="gig"]',
            '.calendar__item', '.event', '.gig', '.show', '.listing',
            '.event-item', '.event-listing', 'article', '.post',
        ]
        events = []
        for selector in event_selectors:
            found = soup.select(selector)
            if found:
                events = found
                break

    gigs = []
    for event in events:
        gig = _extract_gig(event, venue['name'], selectors)
        if gig and gig['band']:
            gigs.append(gig)
            if len(gigs) >= event_limit:
                break
    return gigs


def _extract_gig(element, venue_name: str, selectors: Optional[Dict] = None) -> Optional[Dict]:
    """Extract band name, date, and venue from an HTML element."""
    try:
        band_name = ""
        date_match = None

        # --- Custom selectors ---
        if selectors:
            title_sel = selectors.get('title', '')
            if title_sel:
                title_elem = element.select_one(title_sel)
                if title_elem:
                    band_name = title_elem.get_text(strip=True)

            date_sel = selectors.get('date', '')
            if date_sel:
                date_elem = element.select_one(date_sel)
                if date_elem:
                    date_text = date_elem.get_text(strip=True)
                    date_match = _extract_date(date_text)

        # --- Fallback: generic extraction ---
        if not band_name:
            # BUG FIX #4: find() with tag names only; CSS selectors need select_one()
            title_elem = element.find(['h1', 'h2', 'h3', 'h4'])
            if not title_elem:
                title_elem = element.select_one('.title, .band, .artist')
            if title_elem:
                band_name = title_elem.get_text(strip=True)
            else:
                text = element.get_text(strip=True)
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                for line in lines:
                    if len(line) > 3 and not re.match(r'^\d+[\/\-\.]\d+', line):
                        band_name = line
                        break

        if not date_match:
            text = element.get_text(strip=True)
            date_match = _extract_date(text)

        # --- Clean band name ---
        if band_name:
            band_name = re.sub(r'^(live|presents|featuring|with|at)\s+', '', band_name, flags=re.IGNORECASE)
            band_name = re.sub(r'\s+(live|show|concert|gig)$', '', band_name, flags=re.IGNORECASE)
            band_name = band_name.strip()

            # Per-venue non-gig exclusions (loaded from config, not hardcoded)
            # These are the bare-minimum universal patterns only
            universal_non_gig = [
                r'saturdays?\s+at\s+the\s+corner',
                r'fridays?\s+at\s+the\s+corner',
                r'sunday\s+roast',
                r'steak\s+night',
                r'parma\s*night',
                r'footy\s+tipping',
                r'trivia',
            ]
            for pattern in universal_non_gig:
                if re.search(pattern, band_name, re.IGNORECASE):
                    return None

            # Per-venue filter patterns from venues.json
            for pattern in venue_exclusion_patterns(venue_name):
                if re.search(pattern, band_name, re.IGNORECASE):
                    return None

        if band_name and len(band_name) > 2:
            clean_date = date_match or 'TBA'
            if clean_date != 'TBA':
                clean_date = re.sub(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)(\d)', r'\1 \2', clean_date)
                clean_date = re.sub(r'(\d{1,2})\s*(\d{4})', r'\1, \2', clean_date)
            return {'band': band_name, 'venue': venue_name, 'date': clean_date}

    except Exception as e:
        log.error("Error extracting gig info from %s: %s", venue_name, e)
    return None


_DATE_PATTERNS = [
    r'\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b',
    r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})\b',
    r'\b((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b',
    r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s?\d{1,2}\b,?\s?\d{4})',
    r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\d{1,2}\d{4})',
    r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})',
    r'\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+\d{1,2}[\/\-\.]\d{1,2})\b',
]


def _extract_date(text: str) -> Optional[str]:
    """Try to pull a date string out of *text* using common AU formats."""
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Per-venue exclusion patterns (from venues.json)
# ---------------------------------------------------------------------------

_venue_exclusion_cache: Dict[str, List[str]] = {}


def _load_venue_exclusions(venues: Dict) -> None:
    """Pre-load exclusion patterns from venues config."""
    for region_venues in venues.values():
        for venue in region_venues:
            patterns = venue.get('exclude_patterns', [])
            if patterns:
                _venue_exclusion_cache[venue['name']] = patterns


def venue_exclusion_patterns(venue_name: str) -> List[str]:
    return _venue_exclusion_cache.get(venue_name, [])


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

class GigScraper:
    def __init__(self, config_file: str = 'venues.json', event_limit: int = 10, request_delay: float = 2.0):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self.config_file = config_file
        self.event_limit = event_limit
        self.request_delay = request_delay
        self.venues = self._load_venues()
        self.scrapeops_key = os.environ.get("SCRAPEOPS_API_KEY", "").strip()
        _load_venue_exclusions(self.venues)

    def _load_venues(self) -> Dict:
        """Load venue configuration from JSON file. Fail loudly on errors."""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            log.error("Config file %s not found — cannot continue without a valid venues.json", self.config_file)
            sys.exit(1)
        except json.JSONDecodeError as e:
            log.error("Error parsing config file %s: %s", self.config_file, e)
            sys.exit(1)

    def get_html(self, venue: Dict, browser=None) -> str:
        """Get HTML for a venue using the appropriate method."""
        vtype = venue.get('type', '')
        if vtype == 'scrapeops':
            return _fetch_scrapeops(self.session, venue['url'], self.scrapeops_key)
        elif venue.get('requires_js', False) or vtype == 'js':
            wait_for = venue.get('wait_for_selector')
            html = _fetch_playwright(
                venue['url'],
                wait_for,
                venue.get('timeout', 30000),
                venue.get('wait_time', 5000),
                browser=browser,
            )
            # BUG FIX #3: check with soup.select(), not string-in-html
            if wait_for:
                soup = BeautifulSoup(html, 'html.parser')
                if not soup.select(wait_for):
                    log.warning("Wait selector '%s' not found after initial load, retrying with networkidle", wait_for)
                    html = _fetch_playwright(
                        venue['url'], None, venue.get('timeout', 30000),
                        browser=browser,
                    )
            return html
        else:
            html = _fetch_static(self.session, venue['url'])
            # Basic 404 check
            soup = BeautifulSoup(html, 'html.parser')
            page_text = soup.get_text().lower()
            if any(ind in page_text for ind in ['page not found', '404', 'not found', 'does not exist']):
                if len(page_text) < 500:
                    log.warning("%s may be showing a 404 page", venue['name'])
            return html

    def scrape_region(self, region: str) -> List[Dict]:
        """Scrape all venues in a region, reusing one browser for JS venues."""
        if region not in self.venues:
            log.error("Unknown region: %s. Available: %s", region, ', '.join(self.venues.keys()))
            return []

        all_gigs = []
        js_venues = [v for v in self.venues[region] if v.get('requires_js', False) or v.get('type') == 'js']
        needs_browser = len(js_venues) > 0

        # BUG FIX #8: single browser launch per region
        pw_ctx = None
        browser = None
        if needs_browser:
            pw_ctx = sync_playwright().start()
            browser = pw_ctx.chromium.launch(headless=True)

        try:
            for i, venue in enumerate(self.venues[region]):
                log.info("Scraping %s…", venue['name'])
                try:
                    html = self.get_html(venue, browser=browser)
                    gigs = _parse_events(html, venue, self.event_limit)
                    all_gigs.extend(gigs)
                except Exception as e:
                    log.error("Error scraping %s: %s", venue['name'], e)

                # Rate-limit between venues (not after the last one)
                if i < len(self.venues[region]) - 1:
                    time.sleep(self.request_delay)
        finally:
            if browser:
                browser.close()
            if pw_ctx:
                pw_ctx.stop()

        # BUG FIX #2: deduplicate using the same key as gig_store (band+venue+date)
        seen = set()
        unique_gigs = []
        for gig in all_gigs:
            band_name = gig['band'].strip()
            band_lower = band_name.lower()

            if len(band_lower) < 3:
                continue

            # Skip date-only entries
            date_only_pattern = r'^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*\d{1,2}[a-z]*\s*(feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)?\d*$'
            if re.match(date_only_pattern, band_lower):
                continue

            band_key = re.sub(r'[^\w]', '', band_lower)
            venue_key = re.sub(r'[^\w]', '', gig['venue'].lower())
            date_key = re.sub(r'[^\w]', '', gig['date'].lower())
            key = f"{band_key}|{venue_key}|{date_key}"
            if key and key not in seen:
                seen.add(key)
                gig['band'] = re.sub(r'\s*\(Read More\)\s*', '', band_name)
                gig['band'] = re.sub(r'\s+', ' ', gig['band']).strip()
                unique_gigs.append(gig)

        return unique_gigs

    def format_output(self, gigs: List[Dict], format_type: str = 'text') -> str:
        """Format the output"""
        if not gigs:
            return "No gigs found."
        if format_type == 'json':
            return json.dumps(gigs, indent=2, ensure_ascii=False)
        return '\n'.join(f"{g['band']} | {g['venue']} | {g['date']}" for g in gigs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    # BUG FIX #5: configure logging at the top, before anything else
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description='Scrape upcoming gigs from venues')
    parser.add_argument('--region', choices=['melbourne', 'geelong', 'surfcoast', 'all'], default='all',
                        help='Region to scrape (default: all)')
    parser.add_argument('--format', choices=['text', 'json'], default='text',
                        help='Output format (default: text)')
    parser.add_argument('--limit', type=int, default=10,
                        help='Max gigs per venue (default: 10)')
    parser.add_argument('--delay', type=float, default=2.0,
                        help='Delay between venue requests in seconds (default: 2.0)')
    parser.add_argument('--output', default=None,
                        help='Output file path (optional)')
    parser.add_argument('--new-only', action='store_true',
                        help='Only output gigs not seen in previous runs (dedup via DuckDB)')
    parser.add_argument('--db-path', default=None,
                        help='Path to DuckDB database (default: gigs.duckdb in scraper dir)')
    parser.add_argument('--db-cleanup-days', type=int, default=90,
                        help='Delete gigs older than N days from DB (default: 90, 0=disable)')
    args = parser.parse_args()

    # Validate ScrapeOps key early if any venue needs it
    scrapeops_key = os.environ.get("SCRAPEOPS_API_KEY", "").strip()

    scraper = GigScraper(event_limit=args.limit, request_delay=args.delay)

    # Check if any venue actually needs ScrapeOps
    all_venues = []
    regions = ['melbourne', 'geelong', 'surfcoast'] if args.region == 'all' else [args.region]
    for r in regions:
        all_venues.extend(scraper.venues.get(r, []))
    if any(v.get('type') == 'scrapeops' for v in all_venues) and not scrapeops_key:
        log.error("SCRAPEOPS_API_KEY not set but at least one venue requires ScrapeOps. Set the env var or change venue type.")
        sys.exit(1)

    # BUG FIX #11: import gig_store here so it's always available
    from gig_store import upsert_gigs, mark_notified, cleanup_old_gigs
    db_path = args.db_path or os.path.join(os.path.dirname(__file__), 'gigs.duckdb')

    # Housekeeping: clean old gigs
    if args.db_cleanup_days > 0:
        deleted = cleanup_old_gigs(days=args.db_cleanup_days, db_path=db_path)
        if deleted:
            log.info("Cleaned up %d old gigs from database", deleted)

    # Scrape
    if args.region == 'all':
        all_gigs = []
        for region in regions:
            gigs = scraper.scrape_region(region)
            all_gigs.extend(gigs)
    else:
        all_gigs = scraper.scrape_region(args.region)

    # BUG FIX #11: filter BEFORE limiting
    # (The --limit already applies per-venue in _parse_events via event_limit,
    #  but if the user meant a global limit, apply it after filtering.)
    # Note: the per-venue limit in _parse_events is fine here since we fixed
    # the parsing to be correct. The old bug was about --limit slicing before
    # filtering non-gigs — that's now handled because _parse_events already
    # filters non-gigs before the limit is applied (gigs list is filtered,
    # then we return gigs which is already clean).

    # Persist to DuckDB
    result = upsert_gigs(all_gigs, db_path=db_path)

    if args.new_only:
        display_gigs = result['new']
        print(f"[{len(result['new'])} new, {len(result['seen'])} already known]", file=sys.stderr)
    else:
        display_gigs = all_gigs

    # BUG FIX #11: only mark notified when --new-only is driving output
    if args.new_only and display_gigs:
        mark_notified(display_gigs, db_path=db_path)

    # Write to output file if specified
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(display_gigs, f, indent=2)
        print(f"Written {len(display_gigs)} gigs to {args.output}", file=sys.stderr)

    output = scraper.format_output(display_gigs, args.format)
    print(output)


if __name__ == '__main__':
    main()

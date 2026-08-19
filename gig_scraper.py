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
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import urlparse

import socket
try:
    from curl_cffi import requests as cf_requests
    _use_cffi = True
except ImportError:
    cf_requests = None
    _use_cffi = False
import requests as _requests_fallback
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from playwright.sync_api import sync_playwright
from lineup_parser import LineupParser, load_known_artists, load_venue_names

log = logging.getLogger(__name__)

# Lazy-init lineup parser (loaded once on first use)
_lineup_parser = None
def _get_lineup_parser():
    global _lineup_parser
    if _lineup_parser is None:
        _lineup_parser = LineupParser(
            known_artists=load_known_artists(),
            venue_names=load_venue_names(),
        )
    return _lineup_parser

# Cache DNS check results to avoid repeated lookups
_dns_cache: Dict[str, bool] = {}


def _check_dns(url: str, timeout: float = 3.0) -> bool:
    """Quick DNS check — returns True if hostname resolves.

    Uses getaddrinfo with AF_INET (IPv4) to avoid IPv6-only lookups
    that can fail on dual-stack hosts with flaky local DNS.
    """
    hostname = urlparse(url).hostname
    if not hostname:  # handle malformed URLs (file://, data:, etc.)
        return False
    if hostname in _dns_cache:
        return _dns_cache[hostname]
    try:
        # AF_INET + getaddrinfo is more reliable than create_connection
        # which can fail on hosts with broken systemd-resolved
        # Note: getaddrinfo doesn't accept timeout param, but DNS lookups
        # are typically <100ms. The timeout is enforced at the connect level.
        addrinfos = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
        if addrinfos:
            _dns_cache[hostname] = True
            return True
        _dns_cache[hostname] = False
        log.warning("DNS returned no results for %s — skipping venue", hostname)
        return False
    except (socket.gaierror, socket.timeout, OSError) as e:
        _dns_cache[hostname] = False
        log.warning("DNS resolution failed for %s (%s) — skipping venue", hostname, e)
        return False


# ---------------------------------------------------------------------------
# Algolia API fetch (for Oztix/Algolia venues)
# ---------------------------------------------------------------------------

# Shared Algolia credentials (search-only, embedded in page source)
_ALGOLIA_APP_ID = 'ICGFYQWGTD'
_ALGOLIA_API_KEY = 'fd2db5fd9e8d6a99f5fa0b564cadd484'
_ALGOLIA_BASE_URL = f'https://{_ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/*/queries'
_ALGOLIA_HEADERS = {
    'x-algolia-application-id': _ALGOLIA_APP_ID,
    'x-algolia-api-key': _ALGOLIA_API_KEY,
    'content-type': 'application/json',
}


def _format_algolia_date(iso_str: str) -> str:
    """Convert ISO date string from Algolia to a human-readable AU format."""
    if not iso_str:
        return 'TBA'
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.strftime('%b %d, %Y')
    except (ValueError, AttributeError):
        return iso_str


def _fetch_algolia(venue_name: str, index: str = 'prod_oztix_eventguide_past_events_first',
                   event_limit: int = 100) -> List[Dict]:
    """Fetch events directly from Algolia API for Oztix venues.

    Returns list of gig dicts compatible with the rest of the scraper.
    """
    payload = {
        'requests': [{
            'indexName': index,
            'params': f'hitsPerPage={event_limit}&page=0&query=&filters=(Venue.Name:"{venue_name}")'
        }]
    }
    try:
        r = _requests_fallback.post(_ALGOLIA_BASE_URL, headers=_ALGOLIA_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        hits = r.json()['results'][0]['hits']
    except Exception as e:
        log.error("Algolia API request failed for %s: %s", venue_name, e)
        return []

    gigs = []
    for h in hits:
        if h.get('IsCancelled') or h.get('IsPostponed'):
            continue
        event_name = h.get('EventName', '').strip()
        if not event_name:
            continue
        date_str = _format_algolia_date(h.get('DateStart', ''))
        gigs.append({
            'band': event_name,
            'venue': venue_name,
            'date': date_str,
        })
    return gigs


# ---------------------------------------------------------------------------
# HTML acquisition helpers
# ---------------------------------------------------------------------------

def _fetch_static(url: str, max_retries: int = 3, base_delay: float = 1.0) -> str:
    """Fetch a URL with TLS fingerprint impersonation via curl_cffi.

    Falls back to plain requests if curl_cffi is not installed.
    """
    use_cffi = _use_cffi
    for attempt in range(max_retries):
        try:
            if use_cffi:
                response = cf_requests.get(url, impersonate='chrome124', timeout=10)
            else:
                response = _requests_fallback.get(url, timeout=10,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'})
            response.raise_for_status()
            return response.text
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            log.warning("Request failed (attempt %d/%d): %s. Retrying in %ds…", attempt + 1, max_retries, e, delay)
            time.sleep(delay)
    raise Exception(f"Failed to fetch {url} after {max_retries} attempts")


def _fetch_scrapeops_deprecated(url: str, max_retries: int = 3, base_delay: float = 2.0) -> str:
    """DEPRECATED: ScrapeOps is no longer needed. Kept for reference only."""
    log.warning("_fetch_scrapeops_deprecated called — this should not happen. Venue config may still reference type=scrapeops")
    raise NotImplementedError("ScrapeOps proxy removed — use curl_cffi (type=static) or Algolia (type=algolia) instead")


def _fetch_playwright(url: str, wait_for_selector: str = None, timeout: int = 30000, wait_time: int = 5000, browser=None, max_retries: int = 2, base_delay: float = 3.0) -> str:
    """Fetch JS-rendered content via Playwright with retries. Returns HTML string.

    If *browser* is passed the caller owns its lifecycle; otherwise a
    throwaway browser is launched and closed.
    """
    owns_browser = browser is None
    last_err = None
    for attempt in range(max_retries):
        try:
            if owns_browser:
                _pw = sync_playwright().start()
                browser = _pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_extra_http_headers({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
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
        except Exception as e:
            last_err = e
            if attempt == max_retries - 1:
                break
            delay = base_delay * (2 ** attempt)
            log.warning("Playwright failed (attempt %d/%d) for %s: %s. Retrying in %ds…", attempt + 1, max_retries, url, e, delay)
            time.sleep(delay)
    raise Exception(f"Playwright failed for {url} after {max_retries} attempts: {last_err}")


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
                    # Also check for support acts in heading-5 (John Curtin style)
                    support_sel = selectors.get('support', '')
                    if support_sel:
                        support_elem = element.select_one(support_sel)
                        if support_elem and support_elem.get_text(strip=True):
                            band_name += ' ' + support_elem.get_text(strip=True)

            date_sel = selectors.get('date', '')
            if date_sel:
                date_elem = element.select_one(date_sel)
                if date_elem:
                    date_text = date_elem.get_text(' ', strip=True)
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

        # --- Clean band name via LineupParser ---
        parsed = None
        if band_name:
            # Keep any existing venue-specific exclusions from venues.json
            for pattern in venue_exclusion_patterns(venue_name):
                if re.search(pattern, band_name, re.IGNORECASE):
                    return None

            parser = _get_lineup_parser()
            parsed = parser.parse(band_name, venue=venue_name)

            if not parsed.is_gig:
                return None

            if not parsed.names:
                return None

            # Use headliner as the primary band name for backward compat
            band_name = parsed.headliner

        if band_name and len(band_name) > 2:
            clean_date = date_match or 'TBA'
            if clean_date != 'TBA':
                clean_date = re.sub(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)(\d)', r'\1 \2', clean_date)
                clean_date = re.sub(r'(\d{1,2})\s*(\d{4})', r'\1, \2', clean_date)
            result = {'band': band_name, 'venue': venue_name, 'date': clean_date}
            # Attach full parsed lineup for downstream use (genre lookup, etc.)
            if parsed and len(parsed.names) > 1:
                result['lineup'] = parsed.names
                result['support'] = parsed.names[1:]
            if parsed.needs_review:
                result['needs_review'] = True
            return result

    except Exception as e:
        log.error("Error extracting gig info from %s: %s", venue_name, e)
    return None


_DATE_PATTERNS = [
    r'\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b',
    r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})\b',
    r'\b((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b',
    r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s?\d{1,2}\b,?\s?\d{4})',
    # FIX: require space between month and day to avoid matching "Jan12024"
    r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\s+\d{4})',
    r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})',
    r'\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+\d{1,2}[\/\-\.]\d{1,2})\b',
    r'((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s*\d{4})',
    # Day-of-week + day + month + time (no year): e.g. "Sun 12 Jul 07:00pm"
    r'((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}:\d{2}\s*(?:am|pm)?)',
    # FIX: day-of-week + day + month (no year) — e.g. "SATURDAY 8 AUGUST"
    r'((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)',
]


def _extract_date(text: str) -> Optional[str]:
    """Try to pull a date string out of *text* using common AU formats."""
    if not text:
        return None
    # Strip ordinal suffixes (1st, 2nd, 3rd, 4th, etc.) so patterns match
    cleaned = re.sub(r'(\d{1,2})(?:st|nd|rd|th)', r'\1', text, flags=re.IGNORECASE)
    for pattern in _DATE_PATTERNS:
        match = re.search(pattern, cleaned, re.IGNORECASE)
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
        self.config_file = config_file
        self.event_limit = event_limit
        self.request_delay = request_delay
        self.venues = self._load_venues()
        _load_venue_exclusions(self.venues)

    def _load_venues(self) -> Dict:
        """Load venue configuration from JSON file. Fail loudly on errors."""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Config file {self.config_file} not found")
        except json.JSONDecodeError as e:
            raise ValueError(f"Error parsing config file {self.config_file}: {e}")

    def get_html(self, venue: Dict, browser=None) -> str:
        """Get HTML for a venue using the appropriate method."""
        vtype = venue.get('type', '')
        if vtype == 'scrapeops':
            # Fallback: treat scrapeops venues as static (curl_cffi handles Cloudflare)
            log.info("%s has type=scrapeops — using curl_cffi instead", venue['name'])
            return _fetch_static(venue['url'])
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
            html = _fetch_static(venue['url'])
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
        algolia_venues = [v for v in self.venues[region] if v.get('type') == 'algolia']
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
                # --- Algolia API venues: bypass Playwright entirely ---
                if venue.get('type') == 'algolia':
                    log.info("Fetching %s via Algolia API…", venue['name'])
                    try:
                        index = venue.get('algolia_index', 'prod_oztix_eventguide_past_events_first')
                        gigs = _fetch_algolia(venue['name'], index=index, event_limit=self.event_limit)
                        all_gigs.extend(gigs)
                        log.info("  → %d events from Algolia", len(gigs))
                    except Exception as e:
                        log.error("Algolia API failed for %s: %s", venue['name'], e)
                    if i < len(self.venues[region]) - 1:
                        time.sleep(self.request_delay)
                    continue

                # FIX: skip DNS check for ScrapeOps venues (they resolve server-side)
                if not _check_dns(venue['url']):
                    log.warning("Skipping %s (DNS resolution failed)", venue['name'])
                    continue
                
                log.info("Scraping %s…", venue['name'])
                max_venue_retries = 2
                for attempt in range(max_venue_retries):
                    try:
                        html = self.get_html(venue, browser=browser)
                        gigs = _parse_events(html, venue, self.event_limit)
                        all_gigs.extend(gigs)
                        break
                    except Exception as e:
                        if attempt == max_venue_retries - 1:
                            log.error("Error scraping %s (all %d attempts failed): %s", venue['name'], max_venue_retries, e)
                        else:
                            log.warning("Scrape %s failed (attempt %d/%d): %s — retrying…", venue['name'], attempt + 1, max_venue_retries, e)
                            time.sleep(self.request_delay * 2)

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

    @staticmethod
    def format_output(gigs: List[Dict], format_type: str = 'text') -> str:
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
    parser.add_argument('--enrich-genres', action='store_true',
                        help='Look up genres for all bands via Last.fm/MusicBrainz')
    parser.add_argument('--genre', default=None,
                        help='Filter output to only heavy genres (metal/punk/hardcore etc.)')
    parser.add_argument('--db-path', default=None,
                        help='Path to DuckDB database (default: gigs.duckdb in scraper dir)')
    parser.add_argument('--db-cleanup-days', type=int, default=90,
                        help='Delete gigs older than N days from DB (default: 90, 0=disable)')
    args = parser.parse_args()

    scraper = GigScraper(event_limit=args.limit, request_delay=args.delay)

    if not _use_cffi:
        log.warning("curl_cffi not installed — falling back to plain requests (may hit Cloudflare blocks). pip install curl_cffi")

    # BUG FIX #11: import gig_store here so it's always available
    from gig_store import init_db, upsert_gigs, mark_notified, cleanup_old_gigs
    db_path = args.db_path or os.path.join(os.path.dirname(__file__), 'gigs.duckdb')

    # Initialize DB schema (replaces import-time migration)
    init_db(db_path)

    # Housekeeping: clean old gigs
    if args.db_cleanup_days > 0:
        deleted = cleanup_old_gigs(days=args.db_cleanup_days, db_path=db_path)
        if deleted:
            log.info("Cleaned up %d old gigs from database", deleted)

    # Scrape
    if args.region == 'all':
        all_gigs = []
        regions = ['melbourne', 'geelong', 'surfcoast']
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

    # Genre enrichment (parallel via batch_lookup)
    if args.enrich_genres:
        from genre_lookup import batch_lookup
        from gig_store import update_gig_genres
        unique_bands = list({g['band'] for g in display_gigs if len(g['band']) > 2})
        log.info("Enriching genres for %d unique bands…", len(unique_bands))
        genre_results = batch_lookup(unique_bands)
        for gig in display_gigs:
            band = gig['band']
            gr = genre_results.get(band, {"genres": [], "is_heavy": False, "source": "skipped", "heavy_score": 0.0})
            update_gig_genres(band, gr['genres'], gr['is_heavy'], gr['source'], db_path=db_path, heavy_score=gr.get('heavy_score', 0.0))
            gig['genres'] = gr['genres']
            gig['is_heavy'] = gr['is_heavy']
            gig['heavy_score'] = gr.get('heavy_score', 0.0)

    # Genre filter
    if args.genre == 'heavy':
        display_gigs = [g for g in display_gigs if g.get('is_heavy')]
        print(f"[{len(display_gigs)} heavy gigs]", file=sys.stderr)

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

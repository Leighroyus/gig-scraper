#!/usr/bin/env python3
"""
Gig Scraper CLI - Scrape upcoming gigs from venues in Melbourne, Geelong, and Surf Coast
"""

import argparse
import os
import requests
from bs4 import BeautifulSoup
import json
import sys
from datetime import datetime
from urllib.parse import urlparse
import re
import time
import logging
from typing import List, Dict, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; rely on env vars

from playwright.sync_api import sync_playwright


class GigScraper:
    def __init__(self, config_file='venues.json', event_limit: int = 10, request_delay: float = 2.0):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self.config_file = config_file
        self.event_limit = event_limit
        self.request_delay = request_delay
        self.venues = self._load_venues()

        # ScrapeOps API key (from environment, never hardcoded)
        self.scrapeops_key = os.environ.get("SCRAPEOPS_API_KEY", "").strip()
        if not self.scrapeops_key:
            logging.warning("SCRAPEOPS_API_KEY not set; ScrapeOps proxy will be unavailable")

        # Configure logging
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    def _load_venues(self) -> Dict:
        """Load venue configuration from JSON file"""
        default_venues = {
            'melbourne': [
                {
                    'name': 'The Corner Hotel',
                    'url': 'https://www.cornerhotel.com/events-and-specials/',
                    'type': 'corner',
                    'selectors': {
                        'container': '.calendar__item',
                        'title': '.calendar__item-title',
                        'date': '.calendar__item-date'
                    }
                },
                {
                    'name': 'The Tote',
                    'url': 'https://thetotehotel.com/',
                    'type': 'tote',
                    'selectors': {
                        'container': '[class*="event"]',
                        'title': '.event-name, .event-details',
                        'date': '.event-date, .highlight'
                    }
                }
            ],
            'geelong': [
            ],
            'surfcoast': [
                {
                    'name': 'Torquay Hotel',
                    'url': 'https://www.bandsintown.com/v/10028067-torquay-hotel',
                    'type': 'torquay'
                }
            ]
        }

        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            logging.warning(f"Config file {self.config_file} not found, using default venues")
            # Create default config file
            with open(self.config_file, 'w') as f:
                json.dump(default_venues, f, indent=2)
            return default_venues
        except json.JSONDecodeError as e:
            logging.error(f"Error parsing config file {self.config_file}: {e}")
            return default_venues

    def _make_request_with_retry(self, url: str, max_retries: int = 3, base_delay: float = 1.0) -> requests.Response:
        """Make HTTP request with exponential backoff retry logic"""
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=10)
                response.raise_for_status()
                return response
            except (requests.RequestException, requests.HTTPError) as e:
                if attempt == max_retries - 1:
                    raise e

                delay = base_delay * (2 ** attempt)
                logging.warning(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {delay}s...")
                time.sleep(delay)

        raise requests.RequestException(f"Failed to fetch {url} after {max_retries} attempts")

    def scrape_with_scrapeops(self, url: str) -> str:
        """Scrape using ScrapeOps Proxy"""
        proxy_url = "https://proxy.scrapeops.io/v1/"
        params = {
            "api_key": self.scrapeops_key,
            "url": url,
            "render_js": "true"
        }
        try:
            response = self.session.get(proxy_url, params=params, timeout=120)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logging.error(f"ScrapeOps error: {e}")
            raise

    def scrape_with_playwright(self, url: str, wait_for_selector: str = None, timeout: int = 30000, wait_time: int = 5000) -> str:
        """Scrape JavaScript-rendered content using Playwright"""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.set_extra_http_headers({
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    })

                    page.goto(url, timeout=timeout)

                    # Wait for specific selector if provided, otherwise wait for network idle
                    if wait_for_selector:
                        try:
                            page.wait_for_selector(wait_for_selector, timeout=10000)
                        except Exception:
                            logging.warning(f"Selector {wait_for_selector} not found, proceeding anyway")
                    else:
                        # Try network idle first, fallback to fixed wait
                        try:
                            page.wait_for_load_state('networkidle', timeout=15000)
                        except Exception:
                            page.wait_for_timeout(wait_time)

                    content = page.content()
                    return content
                finally:
                    browser.close()

        except Exception as e:
            logging.error(f"Error with Playwright scraping: {e}")
            raise e

    def scrape_js_venue(self, venue: Dict) -> List[Dict]:
        """Scrape venue website that requires JavaScript rendering"""
        gigs = []
        try:
            # Get the rendered HTML content
            wait_for = venue.get('wait_for_selector')
            timeout = venue.get('timeout', 30000)
            wait_time = venue.get('wait_time', 5000)
            html_content = self.scrape_with_playwright(
                venue['url'],
                wait_for,
                timeout,
                wait_time)

            # Debug: if wait_for was specified but not found, try harder
            if wait_for and wait_for not in html_content:
                logging.warning(f"Wait selector '{wait_for}' not found, retrying with networkidle")
                html_content = self.scrape_with_playwright(venue['url'], None, timeout)

            soup = BeautifulSoup(html_content, 'html.parser')

            # DEBUG: Log raw HTML sample for Torquay
            if 'torquay' in venue.get('name', '').lower():
                sample = html_content[html_content.find('gig'):html_content.find('gig')+500] if 'gig' in html_content else 'NO GIG FOUND'
                logging.info(f"TORQUAY DEBUG HTML sample: {sample[:200]}")

            # Use custom selectors if defined, otherwise fall back to generic
            if 'selectors' in venue:
                events = soup.select(venue['selectors']['container'])
            else:
                # Generic selectors for common event patterns
                event_selectors = [
                    '[class*="event"]',
                    '[class*="show"]',
                    '[class*="gig"]',
                    '.calendar__item',
                    '.event',
                    '.gig',
                    '.show',
                    '.listing',
                    '.event-item',
                    '.event-listing',
                    'article',
                    '.post'
                ]
                events = []
                for selector in event_selectors:
                    found_events = soup.select(selector)
                    if found_events:
                        events = found_events
                        break

            for event in events[:self.event_limit]:
                gig = self.extract_gig_info(event, venue['name'], venue.get('selectors'))
                if gig and gig['band']:
                    gigs.append(gig)

        except Exception as e:
            logging.error(f"Error scraping {venue['name']} with Playwright: {e}")

        return gigs

    def scrape_scrapeops_venue(self, venue: Dict) -> List[Dict]:
        """Scrape using ScrapeOps API"""
        gigs = []
        try:
            html_content = self.scrape_with_scrapeops(venue['url'])
            soup = BeautifulSoup(html_content, 'html.parser')

            # Use custom selectors if defined
            if 'selectors' in venue:
                events = soup.select(venue['selectors']['container'])
            else:
                events = soup.select('.gig, .event, [class*="gig"], [class*="event"]')

            for event in events[:self.event_limit]:
                gig = self.extract_gig_info(event, venue['name'], venue.get('selectors'))
                if gig and gig['band']:
                    gigs.append(gig)

        except Exception as e:
            logging.error(f"Error scraping {venue['name']} with ScrapeOps: {e}")

        return gigs

    def scrape_generic_venue(self, venue: Dict) -> List[Dict]:
        """Generic scraper for venue websites"""
        gigs = []
        try:
            response = self._make_request_with_retry(venue['url'])

            # Check if we got redirected to a 404 page or if content suggests 404
            if response.status_code == 404:
                raise requests.HTTPError(f"404 Not Found for {venue['url']}")

            soup = BeautifulSoup(response.content, 'html.parser')

            # Check for common 404 page indicators
            page_text = soup.get_text().lower()
            if any(indicator in page_text for indicator in ['page not found', '404', 'not found', 'does not exist']):
                if len(page_text) < 500:  # Short pages are often error pages
                    print(f"Warning: {venue['name']} may be showing a 404 page", file=sys.stderr)
                    return []

            # Use custom selectors if defined, otherwise fall back to generic
            if 'selectors' in venue:
                events = soup.select(venue['selectors']['container'])
            else:
                # Generic selectors for common event patterns
                event_selectors = [
                    '.calendar__item',
                    '.event',
                    '.gig',
                    '.show',
                    '.listing',
                    '.event-item',
                    '.event-listing',
                    'article',
                    '.post'
                ]
                events = []
                for selector in event_selectors:
                    found_events = soup.select(selector)
                    if found_events:
                        events = found_events
                        break

            for event in events[:self.event_limit]:
                gig = self.extract_gig_info(event, venue['name'], venue.get('selectors'))
                if gig and gig['band']:
                    gigs.append(gig)

        except Exception as e:
            logging.error(f"Error scraping {venue['name']}: {e}")

        return gigs

    def extract_gig_info(self, element, venue_name: str, selectors: Optional[Dict] = None) -> Optional[Dict]:
        """Extract band name, date, and venue from an element"""
        try:
            # Try custom selectors first if provided
            band_name = ""
            date_match = None

            if selectors:
                # Pass full selector string to select_one (BS handles comma-separated selectors)
                title_sel = selectors.get('title', '')
                if title_sel:
                    title_elem = element.select_one(title_sel)
                    if title_elem:
                        band_name = title_elem.get_text(strip=True)
                date_text = ""
                date_sel = selectors.get('date', '')
                if date_sel:
                    date_elem = element.select_one(date_sel)
                    if date_elem:
                        date_text = date_elem.get_text(strip=True)
                    # Try to extract date pattern
                    if date_text:  # Check for empty text before applying regex
                        date_patterns = [
                            r'\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b',
                            r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})\b',
                            r'\b((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b',
                            r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s?\d{1,2},?\s?\d{4})',
                        ]
                        for pattern in date_patterns:
                            match = re.search(pattern, date_text, re.IGNORECASE)
                            if match:
                                date_match = match.group(1)
                                break

            # Fall back to generic extraction if custom selectors didn't work
            if not band_name:
                text = element.get_text(strip=True)
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                title_elem = element.find(['h1', 'h2', 'h3', 'h4', '.title', '.band', '.artist'])
                if title_elem:
                    band_name = title_elem.get_text(strip=True)
                elif lines:
                    for line in lines:
                        if len(line) > 3 and line and not re.match(r'^\d+[\/\-\.]\d+', line):
                            band_name = line
                            break

            # Fall back date extraction if custom selectors didn't work
            if not date_match:
                text = element.get_text(strip=True)
                if text:  # Check for empty text before applying regex
                    date_patterns = [
                        r'\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b',
                        r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})\b',
                        r'\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+\d{1,2}[\/\-\.]\d{1,2})\b',
                        r'\b((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b',
                        r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s?\d{1,2},?\s?\d{4})',
                    ]
                    for pattern in date_patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            date_match = match.group(1)
                            break

            # Clean up band name
            if band_name:
                # Remove common prefixes/suffixes
                band_name = re.sub(r'^(live|presents|featuring|with|at)\s+', '', band_name, flags=re.IGNORECASE)
                band_name = re.sub(r'\s+(live|show|concert|gig)$', '', band_name, flags=re.IGNORECASE)
                band_name = band_name.strip()

                # Filter out obvious non-gig events (restaurant/bar events)
                non_gig_patterns = [
                    r'saturdays?\s+at\s+the\s+corner',
                    r'fridays?\s+at\s+the\s+corner',
                    r'sunday\s+roast',
                    r'steak\s+night',
                    r'parma\s*night',
                    r'footy\s+tipping',
                    r'trivia',
                    r'rooftop',
                    r'djs?\s',
                    r'rock\s*pop\s*culture',
                    r'\$?\d+\s*roast',
                    r'\$?\d+\s*special',
                    r'more\s*info',
                ]
                if band_name:  # Check for empty band_name before applying regex
                    for pattern in non_gig_patterns:
                        if re.search(pattern, band_name, re.IGNORECASE):
                            return None  # Skip non-gig events

            if band_name and len(band_name) > 2:
                # Normalize concatenated dates (e.g. "Jun112026" → "Jun 11, 2026")
                clean_date = date_match or 'TBA'
                if clean_date != 'TBA':
                    # Insert space between month letters and digits
                    clean_date = re.sub(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)(\d)', r'\1 \2', clean_date)
                    # Insert comma before year if missing
                    clean_date = re.sub(r'(\d{1,2})\s*(\d{4})', r'\1, \2', clean_date)

                return {
                    'band': band_name,
                    'venue': venue_name,
                    'date': clean_date
                }

        except Exception as e:
            logging.error(f"Error extracting gig info from {venue_name}: {e}")

        return None

    def scrape_region(self, region: str) -> List[Dict]:
        """Scrape all venues in a region"""
        if region not in self.venues:
            print(f"Unknown region: {region}. Available: {', '.join(self.venues.keys())}")
            return []

        all_gigs = []
        for venue in self.venues[region]:
            print(f"Scraping {venue['name']}...", file=sys.stderr)

            # Use ScrapeOps for blocked sites
            if venue.get('type') == 'scrapeops':
                gigs = self.scrape_scrapeops_venue(venue)
            # Use Playwright for venues that require JavaScript
            elif venue.get('requires_js', False) or venue.get('type') == 'js':
                gigs = self.scrape_js_venue(venue)
            else:
                gigs = self.scrape_generic_venue(venue)

            all_gigs.extend(gigs)
            # Rate limiting between venues
            if venue != self.venues[region][-1]:
                time.sleep(self.request_delay)

        # Deduplicate by band name
        seen = set()
        unique_gigs = []

        # Patterns to filter out non-gig entries
        date_only_pattern = r'^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*\d{1,2}[a-z]*\s*(feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)?\d*$'
        description_patterns = [
            r'\b(gin|vermouth|dirty|dirty gin)\b',
            r'\b(matinee|start|saturday)\b',
            r'\b(eyeball|little)\b',
            r'^\d+\s*(pm|am)$',
            r'\b(for The Kreep)\b',
        ]

        for gig in all_gigs:
            band_name = gig['band'].strip()
            band_lower = band_name.lower()

            # Skip if band name is too short
            if len(band_lower) < 3:
                continue

            # Skip date-only entries like "Sat28Feb" or "Saturday 28th February"
            if re.match(date_only_pattern, band_lower):
                continue

            # Skip description-like entries
            skip = False
            for pattern in description_patterns:
                if re.search(pattern, band_lower):
                    skip = True
                    break
            if skip:
                continue

            # Deduplicate by normalized band name
            key = re.sub(r'[^\w]', '', band_lower)  # Remove punctuation for comparison
            if key and key not in seen:
                seen.add(key)
                # Clean up the band name - remove (Read More) and extra whitespace
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

        # Text format
        output = []
        for gig in gigs:
            output.append(f"{gig['band']} | {gig['venue']} | {gig['date']}")

        return '\n'.join(output)


def main():
    parser = argparse.ArgumentParser(description='Scrape upcoming gigs from venues')
    parser.add_argument('--region',
                       choices=['melbourne', 'geelong', 'surfcoast', 'all'],
                       default='all',
                       help='Region to scrape (default: all)')
    parser.add_argument('--format',
                       choices=['text', 'json'],
                       default='text',
                       help='Output format (default: text)')
    parser.add_argument('--limit',
                       type=int, default=10,
                       help='Max gigs per venue (default: 10)')
    parser.add_argument('--delay',
                       type=float, default=2.0,
                       help='Delay between venue requests in seconds (default: 2.0)')
    parser.add_argument('--output',
                       default=None,
                       help='Output file path (optional)')
    parser.add_argument('--new-only',
                       action='store_true',
                       help='Only output gigs not seen in previous runs (dedup via DuckDB)')
    parser.add_argument('--db-path',
                       default=None,
                       help='Path to DuckDB database (default: gigs.duckdb in scraper dir)')

    args = parser.parse_args()

    scraper = GigScraper(event_limit=args.limit, request_delay=args.delay)

    if args.region == 'all':
        all_gigs = []
        for region in ['melbourne', 'geelong', 'surfcoast']:
            gigs = scraper.scrape_region(region)
            all_gigs.extend(gigs)
    else:
        all_gigs = scraper.scrape_region(args.region)

    # Persist to DuckDB and get dedup info
    from gig_store import upsert_gigs, mark_notified
    db_path = args.db_path or os.path.join(os.path.dirname(__file__), 'gigs.duckdb')
    result = upsert_gigs(all_gigs, db_path=db_path)

    if args.new_only:
        display_gigs = result['new']
        print(f"[{len(result['new'])} new, {len(result['seen'])} already known]", file=sys.stderr)
    else:
        display_gigs = all_gigs

    # Mark displayed gigs as notified
    if display_gigs:
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
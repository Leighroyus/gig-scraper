# Shotkickers Gig Scraper — Missing September Events

**Date:** 2026-07-25  
**Status:** Root cause identified

## Summary

The scraper only captures 9 events (through Aug 1st) because the Shotkickers website uses **Algolia's InfiniteHits widget** with a default page size of 9. The scraper loads the page once via Playwright and never clicks "Show more events", so it only gets the first batch.

## Root Cause

The Shotkickers site (`shotkickers.com/gigs`) is powered by **Algolia + Oztix**. The page uses the `ais-InfiniteHits` widget, which:

1. Loads only **9 events** on initial page render (`hitsPerPage=9`)
2. Has a **"Show more events" button** (`.ais-InfiniteHits-loadMore`) that triggers additional API calls
3. The scraper's `_fetch_playwright()` function loads the page and waits for `networkidle`, but **never scrolls or clicks the load-more button**

## Investigation Results

### 1. What HTML does Playwright get?

Playwright receives a fully rendered page with **exactly 9 `.event-tile` elements**. The CSS selectors (`.event-tile`, `.event-name`, `.date-time-venue`) **are correct** and work fine. The problem is not selector accuracy — it's that only 9 tiles exist in the DOM at load time.

### 2. Are the CSS selectors correct?

**Yes.** All three selectors match the current site structure:
- `.event-tile` → 9 elements ✅
- `.event-name` → 9 elements ✅
- `.date-time-venue` → 9 elements ✅

### 3. Lazy loading / infinite scroll / pagination?

**Yes — InfiniteHits with a "Show more" button.** The page contains:
- `ais-InfiniteHits` container
- `ais-InfiniteHits-list` with 9 `ais-InfiniteHits-item` children
- `ais-InfiniteHits-loadMore` button with text "Show more events"

The button is **not disabled**, meaning more events are available. Clicking/scrolling would trigger additional Algolia API calls to load the rest.

### 4. Is event_limit of 10 capping results?

**No.** The event_limit is 10, but only 9 events exist in the DOM. The limit isn't the bottleneck — the Playwright page load is.

### 5. Algolia API — the real solution

The site makes a POST request to:
```
https://ICGFYQWGTD-dsn.algolia.net/1/indexes/*/queries
```

With body:
```json
{
  "requests": [{
    "indexName": "prod_oztix_eventguide_past_events_first",
    "params": "distinct=true&facets=[\"Categories\"]&hitsPerPage=9&page=0&query=&filters=(Venue.Name:\"Shotkickers\")"
  }]
}
```

**Key finding:** The API supports `hitsPerPage=1000` and returns **all 36 events in a single call**. The index contains events through October 2026:

| # | Date | Event |
|---|------|-------|
| 1-9 | Jul 25 – Aug 1 | Current scraper output |
| 10 | Aug 2 | Lucy Wise 'V Line' Single Launch |
| 11 | Aug 6 | CAMILLA |
| 12 | Aug 7 | FLESH RITUALS & SIREN SONGS V |
| 13-28 | Aug 8 – Aug 30 | 16 more events |
| 29-30 | Sep 5 | Tracksuit Larry, O.R.B |
| 31 | Sep 6 | SHEPPARTON AIRPLANE |
| 32 | Sep 19 | RUBYHOO - WHO'S RUBY? TOUR |
| 33 | Sep 26 | Phil Coyne & The Wayward Aces |
| 34-36 | Oct 17-30 | 3 more events |

**Total: 36 events** (vs 9 captured by scraper)

## Algolia API Details

- **App ID:** `ICGFYQWGTD`
- **Search API Key:** `fd2db5fd9e8d6a99f5fa0b564cadd484` (search-only, exposed in page source)
- **Index:** `prod_oztix_eventguide_past_events_first`
- **Filter:** `(Venue.Name:"Shotkickers")`
- **Response fields:** `EventName`, `DateStart` (ISO), `Venue.Name`, `IsCancelled`, `IsPostponed`, `Bands[]`, `SpecialGuests`, `EventUrl`, `PriceFrom`

This same Algolia app is shared across all Oztix venues:
- **Kindred Studios** — same index (`prod_oztix_eventguide_past_events_first`)
- **Bendigo Hotel** — same app, slightly different index (`prod_oztix_eventguide`)
- **Croxton** — likely same setup

## Recommended Fix

**Option A (Best): Query Algolia API directly for Oztix/Algolia venues**

Replace Playwright scraping with a direct Algolia API call. Advantages:
- Returns all events in one request (no pagination needed)
- Structured JSON data (no HTML parsing)
- Faster and more reliable than Playwright
- No dependency on page rendering

Implementation sketch:
```python
def _fetch_algolia_venue(venue: Dict) -> List[Dict]:
    """Fetch events directly from Algolia API for Oztix venues."""
    app_id = 'ICGFYQWGTD'
    api_key = 'fd2db5fd9e8d6a99f5fa0b564cadd484'
    index = venue.get('algolia_index', 'prod_oztix_eventguide_past_events_first')
    
    headers = {
        'x-algolia-application-id': app_id,
        'x-algolia-api-key': api_key,
        'content-type': 'application/json'
    }
    payload = {
        'requests': [{
            'indexName': index,
            'params': f'hitsPerPage=1000&page=0&query=&filters=(Venue.Name:"{venue["name"]}")'
        }]
    }
    r = requests.post(
        f'https://{app_id}-dsn.algolia.net/1/indexes/*/queries',
        headers=headers, json=payload, timeout=15
    )
    hits = r.json()['results'][0]['hits']
    return [{
        'band': h.get('EventName', ''),
        'venue': venue['name'],
        'date': _format_algolia_date(h.get('DateStart', '')),
    } for h in hits if not h.get('IsCancelled')]
```

**Option B (Quick fix): Click "Show more" in Playwright**

Modify `_fetch_playwright()` to click the `.ais-InfiniteHits-loadMore` button repeatedly until it's disabled or no new content appears. This is a smaller change but still relies on Playwright.

**Option C (Hybrid): Keep Playwright but increase initial page size**

Override the Algolia query parameters via Playwright's `page.route()` to change `hitsPerPage` from 9 to 1000. This avoids the need to click load-more while keeping the Playwright pipeline.

## Additional Notes

- The index name `past_events_first` is misleading — it contains both past and future events
- The API key is search-only (safe to embed), but could be rotated by Oztix at any time
- All Oztix/Algolia venues (Shotkickers, Kindred, Croxton, Bendigo Hotel) likely have the same pagination issue

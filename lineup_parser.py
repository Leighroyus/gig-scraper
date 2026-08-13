"""
lineup_parser
=============
Extract artist names from noisy gig-listing title strings.

Design: subtractive, not extractive. We can't reliably describe what a band
name looks like, but we CAN enumerate the noise around it. Strip everything
identifiable as not-an-artist, then split what's left on separator tokens.

Pipeline order is load-bearing:
 1. normalise unicode dashes, whitespace, quotes
 2. drop venue known from scraper context - exact match removal
 3. drop age/price MUST precede splitting ("18+" contains a separator)
 4. drop promo SELLING FAST / SOLD OUT / TICKETS ON SALE
 5. drop tour "Australian Tour", "Wild God Tour 2026", "Album Launch"
 6. mask known cached artists containing "+" "&" "/" -> placeholder
 7. segment split on dashes, drop locality-only segments
 8. split on separator vocabulary (determiner-aware for and/&)
 9. parse tokens pull out (USA)/(Melb) origin tags
 10. score confidence per token; low scores route to review/LLM

Usage:
    parser = LineupParser(known_artists=load_cache(), venue_names=VENUES)
    result = parser.parse(title, venue="Torquay Hotel")
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------
# Vocabularies - tune these against your own scraped corpus
# --------------------------------------------------------------------------

# Ticketing / promo chatter. Applied globally, not just at string edges,
# because venues love to bookend: "SELLING FAST - <lineup> - SELLING FAST"
PROMO_NOISE = [
    r"selling\s+fast", r"sell(?:ing)?\s+out\s+soon", r"sold\s+out", r"almost\s+gone",
    r"final\s+(?:release|tickets?|allocation)", r"last\s+(?:few\s+)?tickets?",
    r"limited\s+tickets?", r"(?:2nd|second|3rd|third)\s+show\s+added",
    r"just\s+announced", r"new\s+show", r"on\s+sale\s+(?:now|\w+day)",
    r"tickets?\s+on\s+sale", r"tickets?\s+available", r"free\s+entry", r"free\s+show",
    r"door\s+sales?", r"pre-?sale", r"early\s+bird", r"low\s+ticket\s+warning",
    r"\bcancelled\b", r"\bpostponed\b", r"\brescheduled\b",
    r"presented\s+by\b[^-|]*", r"\bproudly\s+presents?\b", r"\bpresents\b",
    r"\bin\s+association\s+with\b[^-|]*",
]

# Age restriction, pricing, ticketing metadata.
# Runs BEFORE the split step - "18+" would otherwise fabricate an artist.
AGE_PRICE = [
    r"\b1[68]\s*\+", r"\b21\s*\+", r"\ba\s*/\s*a\b", r"\ball\s+ages\b",
    r"\bunder\s*1[68]s?\b", r"\blicensed\s+venue\b", r"\bover\s+18s?\s+only\b",
    r"\$\s*\d+(?:[.,]\d{2})?(?:\s*-\s*\$?\d+(?:[.,]\d{2})?)?",
    r"\b\d+\s*(?:dollars|bucks)\b",
    r"\+?\s*\bbf\b", r"\bbooking\s+fee\b", r"\bplus\s+fees\b",
    r"\bdoors?\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
]

# Tour / release / event descriptors that trail the lineup.
# The generic "<up to 4 words> Tour" rule catches album-titled tours
# ("Wild God Tour 2026") that no fixed list would enumerate.
TOUR_DESCRIPTORS = [
    r"(?:[A-Za-z0-9']+\s+){0,4}\btour\b(?:\s+\d{4})?",
    r"\b(?:album|ep|single|record|lp|book|film|video|mixtape)\s+"
    r"(?:launch|release|preview|party)\b",
    r"\b\d{1,2}(?:st|nd|rd|th)\s+anniversary(?:\s+(?:show|celebration))?\b",
    r"\blive\s+(?:at|in)\s+[^-|]*",
    r"\b(?:one\s+night\s+only|encore\s+show|special\s+event)\b",
    r"\bside\s?show\b", r"\bafter\s?party\b", r"\bmatinee\b",
]

# Non-music events. If matched, the row isn't a gig at all.
NOT_A_GIG = re.compile(
    r"\b(?:trivia|karaoke|bingo|quiz\s+night|open\s+mic|comedy\s+night|"
    r"meat\s+raffle|poker|speed\s+dating|book\s+club|market\s+day|"
    r"afl|nrl|nba|mlb|premier\s+league|super\s+rugby|cricket|boxing|mma|ufc|"
    r"f1|formula|grand\s+prix|nfl|wrestling)\b",
    re.IGNORECASE,
)

# Tokens that occupy an artist slot but name no specific artist.
NON_ARTIST_SLOTS = {
    "support", "supports", "special guests", "special guest", "guests", "guest",
    "more", "more tba", "tba", "tbc", "dj", "djs", "dj set", "dj sets",
    "local support", "local supports", "and more", "friends", "very special guests",
    "surprise guest", "surprise guests", "plus more", "special guests tba",
    "live music", "live", "band", "bands", "the band", "plus support",
}

# Bare localities that survive as phantom artists ("Blink-182 - Melbourne").
LOCALITIES = {
    "melbourne", "sydney", "brisbane", "adelaide", "perth", "hobart", "darwin",
    "canberra", "geelong", "torquay", "ballarat", "bendigo", "newcastle",
    "gold coast", "sunshine coast", "byron bay", "wollongong", "castlemaine",
    "victoria", "nsw", "queensland", "tasmania", "australia", "australian",
}

# Determiners: "X and the Y" is one band, "X and Y" is usually two.
DETERMINER = r"(?!(?:the|a|an|his|her|my|their|our|los|las|le|la|les)\b)"

# Split vocabulary. Longest-first so "w/" isn't eaten by "/".
# and/& carry a determiner lookahead so "Amyl and the Sniffers" stays intact.
SEPARATORS = [
    r"\s+w/\s*", r"\s+with\s+", r"\s+feat(?:uring|\.)?\s+", r"\s+ft\.?\s+",
    r"\s*//\s*", r"\s*\|\s*", r"\s*\+\s*",
    r"\s+and\s+" + DETERMINER, r"\s*&\s*" + DETERMINER,
    r"\s*,\s*", r"\s*/\s*", r"\s+x\s+", r"\s*[\u00b7\u2022]\s*",
]

# Origin / locality tags: "(USA)", "[UK]", "(Melb)". Captured, not discarded.
ORIGIN_TAG = re.compile(
    r"[\(\[]\s*("
    r"usa|us|uk|nz|can|canada|japan|jpn|germany|ger|swe|sweden|nor|norway|"
    r"ireland|irl|scotland|france|fra|netherlands|nld|brazil|bra|"
    r"melb|melbourne|syd|sydney|bris|brisbane|adel|adelaide|perth|"
    r"hobart|canberra|darwin|geelong|vic|nsw|qld|wa|sa|tas|act|nt|aus|australia"
    r")\s*[\)\]]",
    re.IGNORECASE,
)

BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
EMPTY_BRACKETS = re.compile(r"[\(\[\{]\s*[\)\]\}]")
DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
TRIM = " -|,:;. '\""


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class Artist:
    name: str
    origin: Optional[str] = None
    billing: str = "support"  # "headline" | "support"
    position: int = 0
    confidence: float = 0.5
    flags: list = field(default_factory=list)


@dataclass
class ParsedLineup:
    artists: list
    raw: str
    cleaned: str
    dropped: list = field(default_factory=list)
    needs_review: bool = False
    is_gig: bool = True

    @property
    def headliner(self) -> Optional[str]:
        return self.artists[0].name if self.artists else None

    @property
    def names(self) -> list:
        return [a.name for a in self.artists]


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

class LineupParser:
    def __init__(
        self,
        known_artists: Optional[set] = None,
        venue_names: Optional[set] = None,
        min_confidence: float = 0.45,
    ):
        # Cache of confirmed artist names, lowercase key -> display form.
        self.known_artists = {a.lower().strip(): a.strip() for a in (known_artists or set())}
        self.venue_names = {v.lower().strip() for v in (venue_names or set())}
        self.min_confidence = min_confidence

        f = re.IGNORECASE
        self._promo = [re.compile(p, f) for p in PROMO_NOISE]
        self._age = [re.compile(p, f) for p in AGE_PRICE]
        self._tour = [re.compile(p, f) for p in TOUR_DESCRIPTORS]
        self._split = re.compile("|".join(SEPARATORS), f)

        # Cached names containing separators need masking before the split.
        self._ambiguous = sorted(
            (k for k in self.known_artists if re.search(r"[+&/,]| x | and ", k)),
            key=len,
            reverse=True,
        )

    # -- step 1 ------------------------------------------------------------
    def _normalise(self, s: str) -> str:
        s = unicodedata.normalize("NFKC", s)
        for d in DASH_CHARS:
            s = s.replace(d, "-")
        s = s.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
        return re.sub(r"\s+", " ", s).strip()

    # -- steps 2-5 ---------------------------------------------------------
    def _strip_noise(self, s: str, venue: Optional[str]) -> tuple:
        dropped = []

        def scrub(text, pattern):
            dropped.extend(m.group(0).strip() for m in pattern.finditer(text) if m.group(0).strip())
            return pattern.sub(" ", text)

        targets = set(self.venue_names)
        if venue:
            targets.add(venue.lower().strip())

        for v in sorted(targets, key=len, reverse=True):
            if v and v in s.lower():
                s = re.sub(re.escape(v), " ", s, flags=re.IGNORECASE)
                dropped.append(v)

        for group in (self._age, self._promo, self._tour):
            for p in group:
                s = scrub(s, p)

        return re.sub(r"\s+", " ", s).strip(TRIM + "/&+"), dropped

    # -- step 6 ------------------------------------------------------------
    def _mask_known(self, s: str) -> tuple:
        """Protect cached names containing separator characters."""
        masks = {}
        for i, key in enumerate(self._ambiguous):
            pattern = re.compile(re.escape(key), re.IGNORECASE)
            if pattern.search(s):
                token = "\x00ART%d\x00" % i
                masks[token] = self.known_artists[key]
                s = pattern.sub(token, s)
        return s, masks

    # -- steps 7-8 ---------------------------------------------------------
    def _split_tokens(self, s: str) -> list:
        segments = re.split(r"\s-\s|\s-(?=\S)|(?<=\S)-\s", s)
        tokens = []
        for seg in segments:
            seg = seg.strip(TRIM)
            if not seg or seg.lower() in LOCALITIES:
                continue
            tokens.extend(t for t in self._split.split(seg) if t and t.strip())
        return [t.strip(TRIM) for t in tokens if t.strip(TRIM)]

    def _parse_token(self, token: str, masks: dict) -> Optional[Artist]:
        for mask, original in masks.items():
            token = token.replace(mask, original)

        origin = None
        m = ORIGIN_TAG.search(token)
        if m:
            origin = m.group(1).upper()
            token = ORIGIN_TAG.sub(" ", token)

        flags = []
        token = EMPTY_BRACKETS.sub(" ", token)
        if BRACKETED.search(token):
            flags.append("bracketed_removed")
            token = BRACKETED.sub(" ", token)

        name = re.sub(r"\s+", " ", token).strip(TRIM)
        low = name.lower()

        if not name or low in NON_ARTIST_SLOTS or low in LOCALITIES:
            return None
        if len(name) < 2 or not re.search(r"[A-Za-z0-9]", name):
            return None

        return Artist(name=name, origin=origin, flags=flags)

    # -- step 10 -----------------------------------------------------------
    def _score(self, artist: Artist) -> float:
        name, score = artist.name, 0.55
        low = name.lower()

        if low in self.known_artists:
            return 0.99

        words = name.split()
        if len(words) > 6:
            score -= 0.30
            artist.flags.append("too_long")
        elif len(words) <= 4:
            score += 0.10

        if re.search(
            r"\b(?:night|party|show|session|festival|celebration|weekender|"
            r"takeover|edition|series|special|extravaganza)$",
            low,
        ):
            score -= 0.25
            artist.flags.append("event_word")

        if re.search(r"\b(?:every|weekly|monthly|fortnightly|each)\b", low):
            score -= 0.35
            artist.flags.append("recurring_event")

        if re.search(
            r"\b(?:mon|tue|tues|wed|thu|thur|fri|sat|sun)(?:day)?\b|"
            r"\b(?:jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b|"
            r"^\d{1,2}(?:st|nd|rd|th)\b",
            low,
        ):
            score -= 0.25
            artist.flags.append("date_fragment")

        if name.isupper() or all(w[0].isupper() for w in words if w and w[0].isalpha()):
            score += 0.12

        if artist.origin:
            score += 0.10

        if re.search(r"\b\d{4}\b", name):
            score -= 0.15
            artist.flags.append("contains_year")

        return max(0.0, min(1.0, score))

    # -- public ------------------------------------------------------------
    def parse(self, title: str, venue: Optional[str] = None) -> ParsedLineup:
        raw = title
        s = self._normalise(title)

        if NOT_A_GIG.search(s):
            return ParsedLineup([], raw, s, [], needs_review=False, is_gig=False)

        s, dropped = self._strip_noise(s, venue)
        s, masks = self._mask_known(s)

        artists = []
        for token in self._split_tokens(s):
            artist = self._parse_token(token, masks)
            if artist is None:
                continue
            artist.position = len(artists)
            artist.billing = "headline" if not artists else "support"
            artist.confidence = self._score(artist)
            artists.append(artist)

        cleaned = s
        for mask, original in masks.items():
            cleaned = cleaned.replace(mask, original)

        needs_review = (
            not artists or any(a.confidence < self.min_confidence for a in artists)
        )

        return ParsedLineup(artists, raw, cleaned, dropped, needs_review)


# --------------------------------------------------------------------------
# Convenience: load from DuckDB / venues.json
# --------------------------------------------------------------------------

def load_known_artists(db_path: str = None) -> set:
    """Load confirmed artist names from DuckDB bands table."""
    import os
    if db_path is None:
        db_path = os.environ.get('GIG_DB_PATH', os.path.join(os.path.dirname(__file__), 'gigs.duckdb'))
    if not os.path.exists(db_path):
        return set()
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        rows = con.execute("SELECT name FROM bands WHERE name IS NOT NULL AND name != ''").fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def load_venue_names() -> set:
    """Load all venue names from venues.json."""
    import os, json
    venues_path = os.path.join(os.path.dirname(__file__), 'venues.json')
    if not os.path.exists(venues_path):
        return set()
    with open(venues_path) as f:
        data = json.load(f)
    names = set()
    for region_venues in data.values():
        for v in region_venues:
            names.add(v.get('name', ''))
    return names

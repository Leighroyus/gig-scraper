#!/bin/bash
# Gig Scraper — biweekly heavy gig report via WhatsApp
set -euo pipefail
cd /home/leigh/clawd/projects/gig_scraper

# Enrich genres (uses cache, fast after first run)
python3 gig_scraper.py --enrich-genres --format text --new-only >/dev/null 2>&1 || true

# Get heavy gigs
output=$(python3 gig_scraper.py --genre heavy --format text 2>/dev/null)

if [ -n "$output" ]; then
    openclaw message send --channel whatsapp --target +61432237661 --message "🤘 Heavy gigs coming up:
$output"
else
    echo "No heavy gigs found" >&2
fi

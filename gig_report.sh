#!/bin/bash
# Gig Scraper — biweekly heavy gig report via WhatsApp
set -euo pipefail
cd /home/leigh/clawd/projects/gig_scraper

# Enrich genres (uses cache, fast after first run)
# NOTE: --new-only NOT used here — we want all gigs available for the heavy report
python3 gig_scraper.py --enrich-genres --format text >/dev/null 2>&1 || true

# Get heavy gigs
output=$(python3 gig_scraper.py --genre heavy --format text 2>/dev/null)

if [ -n "$output" ]; then
    openclaw message send --channel whatsapp --target +61432237661 --message "🤘 Heavy gigs coming up:
$output"
else
    echo "No heavy gigs found" >&2
fi

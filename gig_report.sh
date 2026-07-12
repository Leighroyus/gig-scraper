#!/bin/bash
# Gig Scraper — biweekly report via WhatsApp
set -euo pipefail
cd /home/leigh/clawd/projects/gig_scraper

output=$(python3 gig_scraper.py --new-only --format text 2>/dev/null)

if [ -n "$output" ]; then
    openclaw message send --channel whatsapp --target +61432237661 --message "🎸 New gigs this fortnight:
$output"
else
    echo "No new gigs found" >&2
fi

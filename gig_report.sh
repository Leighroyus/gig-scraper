#!/bin/bash
# Gig Scraper — biweekly heavy gig report via WhatsApp
set -euo pipefail
cd /home/leigh/.openclaw/workspace/tools/gig_scraper

# Scrape + enrich genres + filter heavy in one pass
output=$(python3 gig_scraper.py --enrich-genres --genre heavy --format text 2>/dev/null)

if [ -n "$output" ]; then
    /home/leigh/.nvm/versions/node/v22.23.2/bin/openclaw message send --channel whatsapp --target +61432237661 --message "🤘 Heavy gigs coming up:
$output"
else
    echo "No heavy gigs found" >&2
fi

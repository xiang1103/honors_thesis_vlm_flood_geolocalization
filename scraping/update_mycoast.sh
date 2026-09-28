#!/usr/bin/env bash
#
# Bring the MyCoast data up to date: scrape, dedupe, metadata, review site.
#
#   ./scraping/update_mycoast.sh                   # the routine update
#   ./scraping/update_mycoast.sh --refresh-pages   # extra args go to mycoast_scrape.py
#   SKIP_GIS=1 ./scraping/update_mycoast.sh        # leave gis_flood_images.json alone
#
# Steps, in this order, stopping at the first failure:
#
#   1. mycoast_scrape.py          full API list, only NEW report pages fetched
#                                 (scrape_data/mycoast_pages.jsonl), temporary
#                                 image URLs swapped for their CDN originals
#   2. dedupe_mycoast.py --apply  the scrape merges fresh reports OVER existing
#                                 ones, which restores every duplicate image and
#                                 photo-less report the last dedupe removed --
#                                 so this must follow every scrape
#   3. make_mycoast_metadata.py   data/mycoast_meta_data.json from the deduped
#                                 file -- REQUIRED after every scrape (CLAUDE.md)
#   4. gis_scrape.py --sources mycoast
#                                 the :8768 review site reads gis_flood_images.json,
#                                 not mycoast.json. Runs after step 1 so its
#                                 temporary-URL fix reads the fresh page cache.
#
# A routine run takes a few minutes: the API list is seconds, a new report
# page ~7 s (6 in parallel), and dedupe fetches only images it has not seen.
# Output also goes to scrape_data/logs/mycoast-update_latest.log.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="${PY:-/home/liu47/conda_envs/newEnv_local/bin/python3}"
LOG_DIR="$ROOT/scrape_data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/mycoast-update_$(date +%Y%m%d_%H%M%S).log"
ln -sfn "$LOG" "$LOG_DIR/mycoast-update_latest.log"

if [ ! -x "$PY" ]; then
    echo "error: python not found at $PY (set PY=... to override)" >&2
    exit 1
fi

cd "$ROOT"
exec > >(tee "$LOG") 2>&1

step() { echo; echo "=== $* ($(date '+%F %T')) ==="; }

step "1/4 scrape MyCoast"
"$PY" scraping/api_based_scraping/mycoast_scrape.py "$@"

step "2/4 dedupe"
"$PY" verification/dedupe_mycoast.py --apply

step "3/4 metadata"
"$PY" make_mycoast_metadata.py

if [ "${SKIP_GIS:-0}" = "1" ]; then
    step "4/4 review-site data SKIPPED (SKIP_GIS=1)"
else
    step "4/4 refresh MyCoast rows in gis_flood_images.json"
    "$PY" scraping/api_based_scraping/gis_scrape.py --sources mycoast
fi

step "done"
echo "log: $LOG"

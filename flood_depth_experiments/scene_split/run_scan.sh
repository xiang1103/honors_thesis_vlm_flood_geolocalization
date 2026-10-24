#!/usr/bin/env bash
# The shot scan, slowly: ONE worker, waiting out YouTube's bot check.
#
# Owner's decision, 2026-10-15: four workers (~750 videos/h) tripped YouTube's
# "confirm you're not a bot" check after an hour, so the scan now runs one
# worker with a pause between downloads (scan_shots.py --gap). When YouTube
# blocks, scan_shots.py exits 3 and frees its GPU; this loop then probes once
# every BLOCK_WAIT_S (metadata only, nothing downloaded, no model loaded) and
# restarts the scan when downloads work again. The scan resumes from its
# ledger, so nothing is repeated. Exits when the scan finishes (exit 0) or
# fails for another reason. BLOCK_WAIT_S=0 stops at the first block instead
# (exit 3) -- the weekly cron run (scraping/video_scraping/run_weekly.sh) does
# that, so it retries next week rather than holding on.
#
#   setsid nohup flood_depth_experiments/scene_split/run_scan.sh > /dev/null 2>&1 &
#   tail -f scrape_data/logs/shot-scan.log
set -u
cd "$(dirname "$0")/../.."
PY=/home/liu47/conda_envs/newEnv_local/bin/python3
SCAN=flood_depth_experiments/scene_split/scan_shots.py
LOG=scrape_data/logs/shot-scan.log
BLOCK_WAIT_S=${BLOCK_WAIT_S:-7200}
mkdir -p scrape_data/logs
exec >>"$LOG" 2>&1
exec 9>scrape_data/youtube_shot_scan/.run_scan.lock
flock -n 9 || { echo "$(date -Is) another run_scan.sh is running; exiting"; exit 1; }

while true; do
    "$PY" "$SCAN" --probe
    rc=$?
    if [ $rc -eq 0 ]; then
        "$PY" "$SCAN" "$@"
        rc=$?
        [ $rc -eq 0 ] && { echo "$(date -Is) scan finished"; exit 0; }
    fi
    if [ $rc -ne 3 ]; then
        echo "$(date -Is) scan_shots.py exited $rc (not a bot check); stopping"
        exit $rc
    fi
    if [ "$BLOCK_WAIT_S" -eq 0 ]; then
        echo "$(date -Is) YouTube block (bot check, or stream 403s in a row); BLOCK_WAIT_S=0, stopping until the next run"
        exit 3
    fi
    echo "$(date -Is) YouTube block (bot check, or stream 403s in a row); probing again in ${BLOCK_WAIT_S} s"
    sleep "$BLOCK_WAIT_S"
done

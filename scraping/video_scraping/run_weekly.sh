#!/usr/bin/env bash
# The WEEKLY YouTube routine (owner, 2026-10-24; replaces the daily 03:30 scrape
# and 05:00 visual check), from cron on Sunday 03:30, after the API quota reset
# at midnight Pacific. The three steps run one after another, each needing the
# one before:
#
#   1. youtube_scrape.py        search -> labels -> flood file + metadata
#   2. verify_video_frames.py   VLM on the 3 auto-frames of new videos -> flood file
#   3. run_scan.sh              shot scan of flood videos not yet in the ledger
#
# A step that fails does not stop the later ones: each also works on what is
# already there. Each step logs to its own file, as before. The shot scan runs
# with BLOCK_WAIT_S=0: if YouTube blocks downloads it stops, and the next try
# is next week. Every step has its own lock, so a manual run cannot collide.
#
#   scraping/video_scraping/run_weekly.sh        # what cron runs
#   tail scrape_data/logs/youtube-weekly.log     # one line per step
set -u
cd "$(dirname "$0")/../.."
PY=/home/liu47/conda_envs/newEnv_local/bin/python3
mkdir -p scrape_data/logs
STEPS=scrape_data/logs/youtube-weekly.log

step() {
    local name=$1; shift
    echo "$(date -Is) $name: start" >> "$STEPS"
    "$@"
    echo "$(date -Is) $name: exit $?" >> "$STEPS"
}

step scrape "$PY" scraping/video_scraping/youtube_scrape.py >> scrape_data/logs/youtube-cron.log 2>&1
step frames "$PY" verification/verify_video_frames.py >> scrape_data/logs/youtube-frames-cron.log 2>&1
step shots env BLOCK_WAIT_S=0 flood_depth_experiments/scene_split/run_scan.sh

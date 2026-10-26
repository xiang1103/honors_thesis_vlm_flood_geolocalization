#!/usr/bin/env bash
# Publish the data_view site: pages + data on Vercel, the scan's frames on R2.
#
#   1. export_site.py   site_build/ from the CURRENT code (data_view_web/) and data;
#                       frames listed in scrape_data/site_frames.txt, not copied
#   2. rclone sync      those frames -> r2:<bucket>/frames. Only new or changed
#                       files are uploaded; frames no longer listed are deleted
#   3. vercel deploy    site_build/ -> production. A static folder: no Next.js,
#                       no build step. The CLI uploads only files Vercel lacks
#
# The frames go up BEFORE the site, so a new page never points at a frame that
# is not there yet. Any step failing stops the run (the live site stays as it was).
#
#   ./deploy_site.sh                 # after a code change, or from cron after new data
#   DRY_RUN=1 ./deploy_site.sh       # export + show what rclone would upload; no upload, no deploy
#
# Needs, in .env (see .env.example): R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
# R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_URL, VERCEL_TOKEN, VERCEL_ORG_ID,
# VERCEL_PROJECT_ID. Log: scrape_data/logs/deploy-site.log.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PY=/home/liu47/conda_envs/newEnv_local/bin/python3
# Last Vercel CLI that runs on this machine's Node 18 (61.0.0 needs Node 20).
VERCEL_CLI=vercel@60.1.3
LOG=scrape_data/logs/deploy-site.log
mkdir -p scrape_data/logs
exec > >(tee -a "$LOG") 2>&1
echo "== $(date -Is) deploy_site.sh"

# .env holds KEY=value lines; values already exported win.
if [[ -f .env ]]; then
    # `|| [[ -n $key ]]`: also the last line when the file does not end in a newline.
    while IFS='=' read -r key value || [[ -n $key ]]; do
        [[ $key =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
        [[ -n ${!key:-} ]] || export "$key=$value"
    done < .env
fi
for var in R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET R2_PUBLIC_URL \
           VERCEL_TOKEN VERCEL_ORG_ID VERCEL_PROJECT_ID; do
    [[ -n ${!var:-} ]] || { echo "missing $var (set it in .env)"; exit 2; }
done

# One run at a time (cron and a manual run must not interleave uploads).
exec 9>scrape_data/.deploy_site.lock
flock -n 9 || { echo "another deploy_site.sh holds the lock"; exit 0; }

echo "-- 1/3 export"
"$PY" export_site.py --frames-url "${R2_PUBLIC_URL%/}/frames"

echo "-- 2/3 frames -> R2"
# An rclone remote defined by environment, so no rclone.conf is needed.
export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
       RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
       RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
       RCLONE_CONFIG_R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" \
       RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
# --size-only: frames are never rewritten in place, and comparing sizes avoids
# a metadata read per object (each is a billed R2 operation).
rclone sync scrape_data/youtube_shot_scan/frames "r2:${R2_BUCKET}/frames" \
    --files-from scrape_data/site_frames.txt --size-only --transfers 16 --stats-one-line --stats 60s \
    ${DRY_RUN:+--dry-run}

if [[ -n ${DRY_RUN:-} ]]; then
    echo "-- 3/3 skipped (DRY_RUN)"
    exit 0
fi
echo "-- 3/3 site -> Vercel"
# VERCEL_ORG_ID / VERCEL_PROJECT_ID pick the project, so no interactive link.
npx --yes "$VERCEL_CLI" deploy site_build --prod --yes --token "$VERCEL_TOKEN"
echo "== $(date -Is) done"

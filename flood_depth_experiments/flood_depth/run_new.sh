#!/usr/bin/env bash
# Depth answers (C1-C5, full_5cm) for shots not yet answered, in the
# FOREGROUND -- the weekly cron step (scraping/video_scraping/run_weekly.sh,
# added 2026-10-26). run_all.sh is the detached, hand-run launcher; this one
# picks its GPUs at run time and waits for every shard, so the deploy after it
# publishes the new answers.
#
# run_all.py resumes (a shot with a line in any answers_*.jsonl is skipped), so
# this only asks about shots the shot scan added since the last run. Exits at
# once, loading nothing, when there are none. GPUs: up to MAX_GPUS (default 3)
# with >= MIN_FREE_GIB (default 60) free, most free first; none -> skip the week.
# Each shard rebuilds video_labels.json when it finishes (what the site reads).
#
#   flood_depth_experiments/flood_depth/run_new.sh
#   tail scrape_data/logs/flood-depth-weekly.log
set -u
cd "$(dirname "$0")/../.."
PY=/home/liu47/conda_envs/newEnv_local/bin/python3
RUN=flood_depth_experiments/flood_depth/run_all.py
LOG=scrape_data/logs/flood-depth-weekly.log
MAX_GPUS=${MAX_GPUS:-3}
MIN_FREE_GIB=${MIN_FREE_GIB:-60}
mkdir -p scrape_data/logs
exec >>"$LOG" 2>&1
exec 9>scrape_data/experiments/flood_depth/.run_new.lock
flock -n 9 || { echo "$(date -Is) another run_new.sh is running; exiting"; exit 1; }
if pgrep -u "$USER" -f "$RUN --shard" > /dev/null; then
    echo "$(date -Is) run_all.py shards already running (run_all.sh by hand?); exiting"
    exit 1
fi

todo=$("$PY" "$RUN" --plan | sed -n 's/.*: \([0-9]*\) shots to do.*/\1/p')
echo "$(date -Is) $todo shots to do"
[ "${todo:-0}" -gt 0 ] || exit 0

gpus=$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits \
       | awk -F', *' -v min=$((MIN_FREE_GIB * 1024)) '$2 >= min {print $2, $1}' \
       | sort -rn | head -n "$MAX_GPUS" | awk '{print $2}')
n=$(echo $gpus | wc -w)
[ "$n" -gt 0 ] || { echo "$(date -Is) no GPU has ${MIN_FREE_GIB} GiB free; skipping"; exit 1; }
echo "$(date -Is) $n shards on GPUs" $gpus

pids=()
i=0
for gpu in $gpus; do
    "$PY" "$RUN" --shard "$i/$n" --device-map "cuda:$gpu" &
    pids+=($!)
    i=$((i + 1))
done
rc=0
for pid in "${pids[@]}"; do
    wait "$pid" || rc=$?
done
echo "$(date -Is) done, exit $rc"
exit $rc

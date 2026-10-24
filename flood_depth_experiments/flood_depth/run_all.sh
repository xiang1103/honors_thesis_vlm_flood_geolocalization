#!/usr/bin/env bash
# Start run_all.py detached, one shard per GPU given on the command line.
#
#   experiments/flood_depth/run_all.sh 0 2 3        # 3 shards on GPUs 0, 2, 3
#   tail -f scrape_data/logs/flood-depth-5cm-shard*.log
#
# Each shard loads its own VLM (~55 GB) on its GPU. The shard count is the
# number of GPUs given; keep it the same when restarting, or shots move
# between shards (harmless: resume reads every shard's answers file).
set -u
cd "$(dirname "$0")/../.."
PY=/home/liu47/conda_envs/newEnv_local/bin/python3
[ $# -ge 1 ] || { echo "usage: $0 GPU [GPU ...]"; exit 1; }
mkdir -p scrape_data/logs
n=$#
i=0
for gpu in "$@"; do
    log=scrape_data/logs/flood-depth-5cm-shard${i}of${n}.log
    setsid nohup "$PY" experiments/flood_depth/run_all.py --shard "$i/$n" --device-map "cuda:$gpu" \
        >> "$log" 2>&1 < /dev/null &
    echo "shard $i/$n on cuda:$gpu -> $log (pid $!)"
    i=$((i + 1))
done

#!/usr/bin/env bash
#
# Run the full flood crawl inside a detached tmux session, so it survives
# an SSH disconnect (closing a laptop lid, dropping VPN, ending the session).
#
#   ./run_crawl.sh                      # every registered outlet, resumable
#   OUTLETS=guardian,cna ./run_crawl.sh # just these
#   OUTLETS=images ./run_crawl.sh       # only outlets that yield captioned images
#
# Then:
#   tmux attach -t flood-scrape     # watch it
#   Ctrl-b then d                   # detach, leaving it running
#   tail -f scrape_data/logs/flood-scrape_latest.log   # watch without attaching
#
# NOTE: this survives a dropped *connection*, not a suspended *machine*. If
# this box is the laptop itself, closing the lid suspends the CPU and the
# crawl pauses until you reopen it. If you SSH into this box from the laptop,
# closing the lid is exactly the case tmux is for.
set -euo pipefail

SESSION="${SESSION:-flood-scrape}"
OUTLETS="${OUTLETS:-all}"
SINCE_DAYS="${SINCE_DAYS:-365}"
LIMIT="${LIMIT:-1200}"
MAX_PAGES="${MAX_PAGES:-60}"
DELAY="${DELAY:-0.6}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PY:-/home/liu47/conda_envs/newEnv_local/bin/python3}"
LOG_DIR="$HERE/../scrape_data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/${SESSION}_$(date +%Y%m%d_%H%M%S).log"

if [ ! -x "$PY" ]; then
    echo "error: python not found at $PY (set PY=... to override)" >&2
    exit 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "session '$SESSION' is already running -- attach with:"
    echo "    tmux attach -t $SESSION"
    echo "or stop it with:  tmux kill-session -t $SESSION"
    exit 1
fi

LATEST="$LOG_DIR/${SESSION}_latest.log"
ln -sfn "$LOG" "$LATEST"

# --resume makes the run restartable: URLs already in the outlet's JSON/JSONL
# are skipped, so re-running after a kill picks up where it left off.
CMD="cd '$HERE' && '$PY' scrape.py \
    --outlets '$OUTLETS' \
    --max-pages $MAX_PAGES \
    --limit $LIMIT \
    --since-days $SINCE_DAYS \
    --delay $DELAY \
    --resume 2>&1 | tee '$LOG'"

# `exec bash` keeps the pane alive after the crawl exits so the final summary
# table is still there when you attach later.
tmux new-session -d -s "$SESSION" "$CMD; echo; echo '=== crawl finished, pane kept open ==='; exec bash"

echo "started tmux session '$SESSION'"
echo "  outlets : $OUTLETS"
echo "  log     : $LOG"
echo "            $LATEST -> same file"
echo
echo "  attach  : tmux attach -t $SESSION       (detach: Ctrl-b then d)"
echo "  tail    : tail -f $LATEST"
echo "  status  : tmux ls"
echo "  stop    : tmux kill-session -t $SESSION"

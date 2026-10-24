#!/usr/bin/env python3
"""Ask the local VLM the depth question under each input condition (C1-C5).

Reads the items (sample_items.py) and, for C5, the cached detections
(detect_refs.py); appends one prediction per (item, condition, frames, prompt,
model, thinking) to scrape_data/experiments/flood_depth/predictions.jsonl,
flushed as it goes. Resumable and prompt-AWARE: a key already in the ledger is
skipped, an edited prompt (new prompt_id) is new work. A reply with no numeric
is recorded with `answer: null` and asked again on the next run (decoding is
greedy, so this only helps after a change such as MAX_NEW_TOKENS); the latest
row per key counts, and evaluate.py reports what stays unparsed.

C1 is asked of EVERY frame of the shot by default (--c1-frames all): the key
frame's answer is the C1 score, the other two give the label-free
within-shot consistency metric. --c1-frames key asks only the key frame.

    python3 flood_depth_experiments/flood_depth/run_conditions.py                       # all conditions, freest GPU
    python3 flood_depth_experiments/flood_depth/run_conditions.py --conditions C1,C2 --limit 20
    python3 flood_depth_experiments/flood_depth/run_conditions.py --thinking            # reasoning on (slow; separate rows)
"""
from __future__ import annotations

import os

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "verification"))
sys.path.insert(0, str(PROJECT / "scraping" / "video_scraping"))
sys.path.insert(0, str(PROJECT / "scraping" / "api_based_scraping"))
import verify_video_frames  # noqa: E402,F401  -- sets CUDA_DEVICE_ORDER before torch starts
from verify_video_frames import pick_free_gpu  # noqa: E402
from conditions import CONDITIONS, prompt_id, prompt_text  # noqa: E402
from detect_refs import detector_id, read_refs  # noqa: E402
from levels import parse_depth_range  # noqa: E402
from sample_items import OUT_DIR, read_items  # noqa: E402

PREDICTIONS_FILE = OUT_DIR / "predictions.jsonl"
#: Room for a short numeric interval, with some headroom for models that add
#: an unwanted explanation.
MAX_NEW_TOKENS = 96
MAX_NEW_TOKENS_THINKING = 4096


def pred_key(row: dict) -> tuple:
    return (row["item_id"], row["condition"], tuple(row["frames"]), row["prompt_id"],
            row["model"], bool(row.get("thinking")))


def read_predictions(path: Path = PREDICTIONS_FILE) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue                                 # a killed run's last line
            out[pred_key(row)] = row             # latest row per key wins
    return out


def jobs_for(items: list[dict], conditions: list[str], c1_frames: str, refs: dict, det_id: str):
    """(item, condition, frame-or-None, refs-row-or-None) for every question to ask."""
    missing_refs = 0
    for it in items:
        for cond in conditions:
            if cond == "C1" and c1_frames == "all":
                for f in it["frames"]:
                    yield it, cond, f["j"], None
            elif cond == "C5":
                row = refs.get((it["item_id"], it["key_frame"], det_id))
                if row is None:
                    missing_refs += 1
                    continue
                yield it, cond, None, row
            else:
                yield it, cond, None, None
    if missing_refs:
        print(f"WARNING: C5 skipped for {missing_refs} item(s) with no cached detections "
              f"({det_id}); run detect_refs.py", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items", type=Path, default=OUT_DIR / "items.jsonl")
    ap.add_argument("--conditions", default="C1,C2,C3,C4,C5")
    ap.add_argument("--c1-frames", choices=("all", "key"), default="all")
    ap.add_argument("--limit", type=int, default=None, help="first N items only")
    ap.add_argument("--device-map", default="free", help="'free' = GPU with the most free memory, or cuda:N")
    ap.add_argument("--thinking", action="store_true", help="reasoning on (recorded per row; ~10x slower)")
    args = ap.parse_args()

    conditions = [c.strip().upper() for c in args.conditions.split(",") if c.strip()]
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown:
        ap.error(f"unknown condition(s): {unknown}; known: {list(CONDITIONS)}")
    items = read_items(args.items)[:args.limit] if args.limit else read_items(args.items)

    from local_vlm import DEFAULT_MODEL_PATH
    from local_vlm.backend import LocalVLM
    model_name = f"local:{Path(DEFAULT_MODEL_PATH).name}"
    det_id = detector_id()
    done = {k: r for k, r in read_predictions().items() if r.get("answer") is not None}
    pids = {c: prompt_id(c) for c in conditions}

    def key_of(it, cond, j, refs_row):
        frames = [j] if j is not None else (
            [f["j"] for f in it["frames"]] if cond == "C2" else [it["key_frame"]])
        return (it["item_id"], cond, tuple(frames), pids[cond], model_name, args.thinking)

    jobs = [job for job in jobs_for(items, conditions, args.c1_frames, read_refs(), det_id)
            if key_of(*job) not in done]
    print(f"=== run_conditions: {len(items)} items, conditions {conditions}, {len(jobs)} questions to ask "
          f"(prompt ids {pids})", flush=True)
    if not jobs:
        return 0

    device = args.device_map
    if device == "free":
        gpu = pick_free_gpu(60)
        if gpu is None:
            print("no GPU has 60 GiB free; not starting", flush=True)
            return 1
        device = f"cuda:{gpu}"
    import torch
    torch.set_num_threads(4)
    vlm = LocalVLM(DEFAULT_MODEL_PATH, device_map=device, enable_thinking=args.thinking,
                   max_new_tokens=MAX_NEW_TOKENS_THINKING if args.thinking else MAX_NEW_TOKENS)
    t0 = time.monotonic()
    vlm.load()
    print(f"VLM loaded on {device} in {time.monotonic() - t0:.0f} s", flush=True)

    PREDICTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    answers: Counter = Counter()
    t_start = time.monotonic()
    with PREDICTIONS_FILE.open("a", encoding="utf-8") as fh:
        for n, (it, cond, j, refs_row) in enumerate(jobs, start=1):
            parts, info = CONDITIONS[cond](it, j, refs_row)
            t1 = time.monotonic()
            text = vlm.generate_parts(parts)
            answer = parse_depth_range(text)
            answers[(cond, answer)] += 1
            row = {"item_id": it["item_id"], "video_id": it["video_id"], "condition": cond,
                   "frames": info["frames"], "key_frame": it["key_frame"],
                   "prompt_id": pids[cond], "model": model_name, "thinking": args.thinking,
                   "answer": answer, "model_output": text,
                   "seconds": round(time.monotonic() - t1, 2),
                   "predicted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            for extra in ("n_boxes", "detector"):
                if extra in info:
                    row[extra] = info[extra]
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            if n % 25 == 0 or n == len(jobs):
                rate = n / (time.monotonic() - t_start)
                print(f"  [{n}/{len(jobs)}] {rate * 3600:.0f} questions/h; last {it['item_id']} {cond} -> {answer}",
                      flush=True)

    # One copy of each condition's fixed text next to the ledger, by prompt id,
    # so a row can always be traced to the exact wording that produced it.
    prompts_file = OUT_DIR / "prompts.json"
    known = json.loads(prompts_file.read_text(encoding="utf-8")) if prompts_file.exists() else {}
    for c in conditions:
        known.setdefault(pids[c], {"condition": c, "text": prompt_text(c)})
    tmp = prompts_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(known, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, prompts_file)

    print("=== done; answers this run:", flush=True)
    for cond in conditions:
        dist = {"unparsed" if a is None else a: count
                for (c, a), count in answers.items() if c == cond}
        print(f"  {cond}: {dist}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

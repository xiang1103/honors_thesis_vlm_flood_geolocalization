#!/usr/bin/env python3
"""C1-C5 over EVERY eligible flood shot, with compact output.

The pilot runner (run_conditions.py) keeps the model's full reply and a
timestamp per question; at ~70k questions that is waste. This writes ONE
short line per shot, once all its questions are answered:

    {"id":"-2wHuNoueGo:shot009","C1":["35-40 cm","40-45 cm","40-45 cm"],"C2":"35-40 cm",...}

  C1   the answer for each of the shot's 3 frames (j = 0, 1, 2); the key
       frame (j = 1) is the C1 score, all three give within-shot consistency
  C2-C5 one answer each; null = no usable interval

A reply counts as answered only if it IS a valid numeric interval ("35-40 cm") or X. Anything longer --
the model sometimes starts "Based on the visual evidence..." despite "Do not
explain", mostly in C2 -- is asked again with room to finish
(LONG_REPLY_TOKENS), and the interval is taken only if it is at the END of that
reply (a number mentioned mid-reasoning is not an answer). Those cases go to
long_<shard>.jsonl with the reply and the interval taken, so answers that came
from reasoning can be told apart; it is the only place raw replies are kept.

Everything common to all lines -- model, thinking, prompt ids, detector,
image size -- is written ONCE to run.json in the output folder. A run whose
prompts or model differ from run.json refuses to start: use a new --out, so
one folder always holds answers to one set of prompts.

Items are built in memory from the shot-scan ledger (sample_items.eligible:
flood shot, not ai_video, 3 frames on disk) -- no item file, the ids say
where the frames are. C5's detections are made inline on the same GPU and
cached compactly in refs_<shard>.jsonl:
    {"id": ..., "b": [["car", 0.70, x0, y0, x1, y1], ...]}   (coords 0-1)

Answers already in the pilot ledger (predictions.jsonl) and boxes already in
refs.jsonl are reused when their prompt / detector ids match.

One file per shard (NFS: several writers on one file can interleave). Resume:
a shot with a line in any answers_*.jsonl is skipped.

    python3 flood_depth_experiments/flood_depth/run_all.py --plan                     # counts, no GPU
    python3 flood_depth_experiments/flood_depth/run_all.py --shard 0/3 --device-map cuda:0
    flood_depth_experiments/flood_depth/run_all.sh 0 2 3                              # one shard per GPU, detached
"""
from __future__ import annotations

import os

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import argparse
import json
import re
import sys
import time
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
import conditions  # noqa: E402
from conditions import CONDITIONS, prompt_id  # noqa: E402
from detect_refs import detector_id, read_refs  # noqa: E402
from levels import parse_depth_range  # noqa: E402
from run_conditions import MAX_NEW_TOKENS, read_predictions  # noqa: E402
from sample_items import FRAMES_DIR, OUT_DIR, attach_video_meta, eligible  # noqa: E402
from scan_shots import LEDGER_DIR, read_ledger  # noqa: E402

# Keep the earlier L0-L5 run intact; the centimeter-range prompt gets a new
# output directory because run.json binds each folder to one prompt set.
FULL_DIR = OUT_DIR / "full_5cm"
LONG_REPLY_TOKENS = 512


def direct_depth_range(text: str) -> str | None:
    return parse_depth_range(text)


def final_depth_range(text: str) -> str | None:
    """The answer a finished long reply ENDS on: its last non-empty line,
    read with the same lenient parser (any width kept), else None. A number
    earlier in the reasoning is never taken."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1]
    for lead in ("final answer:", "answer:", "therefore,", "so,"):
        if last.lower().lstrip("*_ ").startswith(lead):
            last = last.lstrip("*_ ")[len(lead):]
    return parse_depth_range(last)


CONDS = ["C1", "C2", "C3", "C4", "C5"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def header(model: str, thinking: bool) -> dict:
    return {"model": model, "thinking": thinking, "max_new_tokens": MAX_NEW_TOKENS,
            "long_reply_tokens": LONG_REPLY_TOKENS,
            "prompt_ids": {c: prompt_id(c) for c in CONDS}, "detector": detector_id(),
            "image_long_side": conditions.IMAGE_LONG_SIDE, "key_frame": 1,
            "c1": "answers for frames j = 0, 1, 2",
            "answer_format": "integer-centimeter interval <=5 cm wide, or X; null = unparsed"}


def same_run(a: dict, b: dict) -> bool:
    keys = ("model", "thinking", "prompt_ids", "detector", "image_long_side")
    return all(a.get(k) == b.get(k) for k in keys)


def read_jsonl(paths) -> list[dict]:
    rows = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue                                     # a killed run's last line
    return rows


def read_answers(folder: Path = FULL_DIR) -> dict[str, dict]:
    """shot id -> its compact line, across every shard's file."""
    return {r["id"]: r for r in read_jsonl(sorted(folder.glob("answers_*.jsonl")))}


def read_compact_refs(folder: Path = FULL_DIR) -> dict[str, list]:
    return {r["id"]: r["b"] for r in read_jsonl(sorted(folder.glob("refs_*.jsonl")))}


def boxes_from_compact(b: list) -> list[dict]:
    return [{"label": x[0], "score": x[1], "box": x[2:6]} for x in b]


def compact_boxes(boxes: list[dict]) -> list:
    return [[b["label"], b["score"], *b["box"]] for b in boxes]


def pilot_cache(model: str, thinking: bool) -> dict[tuple, str]:
    """(item_id, condition, frames) -> answer, from the pilot ledger, for the
    current prompts only."""
    pids = {c: prompt_id(c) for c in CONDS}
    out = {}
    for row in read_predictions().values():
        if (row.get("answer") is not None and row["model"] == model
                and bool(row.get("thinking")) == thinking and pids.get(row["condition"]) == row["prompt_id"]):
            out[(row["item_id"], row["condition"], tuple(row["frames"]))] = row["answer"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shard", default="0/1", help="i/n: this worker takes every n-th shot starting at i")
    ap.add_argument("--device-map", default="free", help="'free' = GPU with the most free memory, or cuda:N")
    ap.add_argument("--out", type=Path, default=FULL_DIR)
    ap.add_argument("--limit", type=int, default=None, help="stop after this many shots (test)")
    ap.add_argument("--thinking", action="store_true")
    ap.add_argument("--plan", action="store_true", help="print counts and exit; no GPU")
    args = ap.parse_args()

    from local_vlm import DEFAULT_MODEL_PATH
    model_name = f"local:{Path(DEFAULT_MODEL_PATH).name}"
    head = header(model_name, args.thinking)
    run_file = args.out / "run.json"
    if run_file.exists():
        existing = json.loads(run_file.read_text(encoding="utf-8"))
        if not same_run(existing, head):
            print(f"{run_file} was written for other prompts/model/detector:\n  {existing}\n  now: {head}\n"
                  f"use a new --out so one folder holds one set of prompts", flush=True)
            return 1

    shard_i, shard_n = (int(x) for x in args.shard.split("/"))
    items, _ = eligible(read_ledger(LEDGER_DIR), FRAMES_DIR)
    items.sort(key=lambda it: it["item_id"])
    done = read_answers(args.out)
    todo = [it for k, it in enumerate(items) if k % shard_n == shard_i and it["item_id"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"=== {now_iso()} run_all shard {args.shard}: {len(todo)} shots to do "
          f"({len(done)} of {len(items)} eligible already answered, all shards)", flush=True)
    if args.plan or not todo:
        return 0
    attach_video_meta(todo)                     # C3's title/tags/description, in memory only

    det_id = head["detector"]
    old_refs = {k[0]: v["boxes"] for k, v in read_refs().items() if k[2] == det_id}
    new_refs = {i: boxes_from_compact(b) for i, b in read_compact_refs(args.out).items()}
    cache = pilot_cache(model_name, args.thinking)

    device = args.device_map
    if device == "free":
        gpu = pick_free_gpu(58)
        if gpu is None:
            print("no GPU has 58 GiB free; not starting", flush=True)
            return 1
        device = f"cuda:{gpu}"
    import torch
    torch.set_num_threads(4)
    from local_vlm.backend import LocalVLM
    from detect_refs import Detector
    vlm = LocalVLM(DEFAULT_MODEL_PATH, device_map=device, enable_thinking=args.thinking,
                   max_new_tokens=MAX_NEW_TOKENS)
    t0 = time.monotonic()
    vlm.load()
    detector = Detector(device)
    print(f"VLM + detector loaded on {device} in {time.monotonic() - t0:.0f} s", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)
    if not run_file.exists():
        tmp = run_file.with_suffix(f".json.{os.getpid()}.tmp")   # shards start together
        tmp.write_text(json.dumps({**head, "created": now_iso()}, indent=1), encoding="utf-8")
        os.replace(tmp, run_file)
    tag = f"{shard_i}of{shard_n}"
    asked = reused = 0
    t_start = time.monotonic()
    with (args.out / f"answers_{tag}.jsonl").open("a", encoding="utf-8") as out_fh, \
         (args.out / f"refs_{tag}.jsonl").open("a", encoding="utf-8") as refs_fh, \
         (args.out / f"long_{tag}.jsonl").open("a", encoding="utf-8") as long_fh:

        def ask(it, cond, j=None, refs_row=None):
            nonlocal asked, reused
            parts, info = CONDITIONS[cond](it, j, refs_row)
            hit = cache.get((it["item_id"], cond, tuple(info["frames"])))
            if hit is not None:
                reused += 1
                return hit
            text = vlm.generate_parts(parts)
            asked += 1
            answer = direct_depth_range(text)
            if answer is None:
                vlm.max_new_tokens = LONG_REPLY_TOKENS
                try:
                    text = vlm.generate_parts(parts)
                finally:
                    vlm.max_new_tokens = MAX_NEW_TOKENS
                answer = direct_depth_range(text) or final_depth_range(text)
                long_fh.write(json.dumps({"id": it["item_id"], "c": cond, "f": info["frames"], "a": answer,
                                          "out": text}, ensure_ascii=False) + "\n")
                long_fh.flush()
            return answer

        for n, it in enumerate(todo, start=1):
            iid = it["item_id"]
            if iid in old_refs:
                boxes = old_refs[iid]
            elif iid in new_refs:
                boxes = new_refs[iid]
            else:
                boxes = detector(conditions.frame_image(it, it["key_frame"]))
                refs_fh.write(json.dumps({"id": iid, "b": compact_boxes(boxes)}, ensure_ascii=False) + "\n")
                refs_fh.flush()
            line = {"id": iid,
                    "C1": [ask(it, "C1", f["j"]) for f in it["frames"]],
                    "C2": ask(it, "C2"), "C3": ask(it, "C3"), "C4": ask(it, "C4"),
                    "C5": ask(it, "C5", refs_row={"boxes": boxes, "detector": det_id})}
            out_fh.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")
            out_fh.flush()
            if n % 50 == 0 or n == len(todo):
                el = time.monotonic() - t_start
                print(f"  [{n}/{len(todo)}] {n / el * 3600:.0f} shots/h, {asked} asked, {reused} reused; "
                      f"eta {(len(todo) - n) / (n / el) / 3600:.1f} h", flush=True)
    print(f"=== {now_iso()} shard {args.shard} done: {len(todo)} shots, {asked} questions asked", flush=True)
    import make_video_labels                    # per-video labels for display, from ALL shards so far
    make_video_labels.write(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

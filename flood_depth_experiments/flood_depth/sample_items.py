#!/usr/bin/env python3
"""Pick the benchmark ITEMS: flood shots from the shot scan, with their frames.

An item is one shot -- a continuous camera take -- because water depth does
not change inside one take; the human label is per shot, and every condition
is graded against that one label (PLAN.md, "Unit"). Item id:
`<video_id>:shot<NNN>` (1-based, the same number as the saved frame files).

Eligible shots (from scrape_data/youtube_shot_scan/*.jsonl):
  - the scan called it a flood shot (`flood: true`);
  - the video is NOT `ai_video: true` (owner: AI videos are dropped whole);
  - it has 3 sampled frames (shots >= 1 s; 1-frame shots would make C2 = C1);
  - all 3 frames are on disk (the first 750 scanned videos have none).

Selection: at most --per-video shots per video (a 20-shot news package would
otherwise dominate), chosen at random with --seed, then --n items at random.
Deterministic for a given (ledger, seed, per-video, n), and the chosen list is
WRITTEN, so labels and predictions always refer to the same file.

Video metadata (title, description, tags -- condition C3) is copied from
data/youtube_videos.json into each item, so a run is reproducible even if the
master changes.

    python3 flood_depth_experiments/flood_depth/sample_items.py --stats            # pool size, writes nothing
    python3 flood_depth_experiments/flood_depth/sample_items.py --n 300            # pilot -> items.jsonl
    python3 flood_depth_experiments/flood_depth/sample_items.py --n 0 --out pool.jsonl   # every eligible shot
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(PROJECT / "flood_depth_experiments" / "scene_split"))
from scan_shots import LEDGER_DIR, frame_path, read_ledger  # noqa: E402

VIDEOS_FILE = PROJECT / "data" / "youtube_videos.json"
FRAMES_DIR = LEDGER_DIR / "frames"
OUT_DIR = PROJECT / "scrape_data" / "experiments" / "flood_depth"
FRAMES_PER_SHOT = 3
KEY_FRAME = 1          # the middle frame (50% of the shot): C1 / C3 / C4 / C5


def item_id(video_id: str, shot: int) -> str:
    return f"{video_id}:shot{shot:03d}"


def eligible(ledger: dict[str, dict], frames_dir: Path) -> tuple[list[dict], Counter]:
    """Every eligible shot as a bare item (no video metadata yet), plus why
    the others were left out."""
    why: Counter = Counter()
    items = []
    for vid, row in ledger.items():
        if "shots" not in row:
            why["video: scan error"] += 1
            continue
        if row.get("ai_video"):
            why["video: ai_video"] += 1
            continue
        for k, shot in enumerate(row["shots"]):
            if not shot.get("flood"):
                why["shot: not flood"] += 1
                continue
            if len(shot["frames"]) < FRAMES_PER_SHOT:
                why["shot: 1 frame (< 1 s)"] += 1
                continue
            paths = [frame_path(frames_dir, vid, k, j) for j in range(FRAMES_PER_SHOT)]
            if not all(p.exists() for p in paths):
                why["shot: frames not saved"] += 1
                continue
            items.append({
                "item_id": item_id(vid, k + 1),
                "video_id": vid,
                "shot": k + 1,
                "start": shot["start"],
                "end": shot["end"],
                "frames": [{"j": j, "t": f["t"], "path": str(p.relative_to(PROJECT))}
                           for j, (f, p) in enumerate(zip(shot["frames"], paths))],
                "key_frame": KEY_FRAME,
                "video_width": row.get("width"),
                "video_height": row.get("height"),
                "scan_prompt": row.get("prompt"),
            })
    return items, why


def select(items: list[dict], n: int, per_video: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    by_video: dict[str, list[dict]] = {}
    for it in items:
        by_video.setdefault(it["video_id"], []).append(it)
    capped = []
    for vid in sorted(by_video):
        shots = by_video[vid]
        capped += rng.sample(shots, min(per_video, len(shots))) if per_video else shots
    capped.sort(key=lambda it: it["item_id"])
    chosen = rng.sample(capped, min(n, len(capped))) if n else capped
    return sorted(chosen, key=lambda it: it["item_id"])


def attach_video_meta(items: list[dict]) -> None:
    videos = {v["video_id"]: v for v in json.loads(VIDEOS_FILE.read_text(encoding="utf-8"))}
    for it in items:
        v = videos.get(it["video_id"], {})
        it.update(title=v.get("title") or "", description=v.get("description") or "",
                  tags=v.get("tags") or [], published_utc=v.get("published_utc"),
                  in_ny=v.get("in_ny"), in_nyc=v.get("in_nyc"),
                  embed_url=v.get("embed_url"))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    """Atomic: .tmp + fsync + os.replace, the repo's write discipline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_items(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=300, help="items to pick; 0 = every eligible shot (default %(default)s)")
    ap.add_argument("--per-video", type=int, default=3, help="max shots per video; 0 = no cap (default %(default)s)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=OUT_DIR / "items.jsonl")
    ap.add_argument("--force", action="store_true", help="overwrite an existing --out (labels may refer to it!)")
    ap.add_argument("--stats", action="store_true", help="print the pool and exit, write nothing")
    args = ap.parse_args()

    items, why = eligible(read_ledger(LEDGER_DIR), FRAMES_DIR)
    print(f"eligible shots: {len(items)} in {len({i['video_id'] for i in items})} videos")
    for reason, count in why.most_common():
        print(f"  left out, {reason}: {count}")
    if args.stats:
        return 0
    if args.out.exists() and not args.force:
        print(f"{args.out} exists; labels and predictions refer to it by item id. "
              f"Pass --force to replace it, or choose another --out.")
        return 1
    chosen = select(items, args.n, args.per_video, args.seed)
    attach_video_meta(chosen)
    for it in chosen:
        it["selected_by"] = {"seed": args.seed, "per_video": args.per_video, "n": args.n}
    write_jsonl(args.out, chosen)
    print(f"wrote {len(chosen)} items from {len({i['video_id'] for i in chosen})} videos -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Visual flood check for the YouTube videos: the local VLM on YouTube's three
automatic frames of every video (`hq1.jpg`-`hq3.jpg`, at ~25/50/75%).

Owner's rule (2026-10-07): one plain question per frame -- is there flooding
in sight? -- nothing about New York or street level. A video whose frames
ALL answer no is not a flood video. Stored on each video as `flood_visual`:

    {"prompt": ..., "model": ..., "classified_at": ...,
     "frames": [{"frame": 1, "image_url": ..., "answer": "yes" | "no" | null}, ...],
     "flood": true | false | null}

  flood = true   at least one frame answered yes
          false  at least one frame answered, none yes
          null   no frame could be judged (every fetch failed)

`export_flood_videos.is_flood()` then requires `flood is True` as well as the
text score, so an unclassified video (`flood_visual` null) is NOT in the
flood-only file until this has run on it.

The cover is not judged: the uploader picks it, the automatic frames are what
the footage actually shows.

Answers are appended to scrape_data/youtube_frame_answers.jsonl as they land
-- the resume ledger, a CACHE that is never deleted (it is what makes a re-run
cost only new videos). Resume is prompt-agnostic, the repo's convention (see
CLAUDE.md "Resume is prompt-agnostic"): each answer records its own prompt.
Failed fetches are retried on the next run; answers are not.

At the end the answers are written into data/youtube_videos.json under the
scraper's lock, then the flood-only file and both metadata files are rebuilt.

    python3 verification/verify_video_frames.py --device-map cuda:6
    python3 verification/verify_video_frames.py --limit 20        # trial
    python3 verification/verify_video_frames.py --merge-only      # write answers back, no GPU
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "scraping" / "video_scraping"))
sys.path.insert(0, str(PROJECT / "scraping" / "api_based_scraping"))

DEFAULT_VIDEOS = PROJECT / "data" / "youtube_videos.json"
LEDGER = PROJECT / "scrape_data" / "youtube_frame_answers.jsonl"

PROMPT = ("Is there any flooding visible in this image? "
          "Answer with a single word: yes or no.")
FRAMES = (1, 2, 3)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def frame_url(video_id: str, n: int) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hq{n}.jpg"


def read_ledger(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """(video_id, frame) -> latest row. A completed answer wins over a later
    error for the same frame, so a transient failure never erases a verdict."""
    best: dict[tuple[str, int], dict[str, Any]] = {}
    if not path.exists():
        return best
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue                                   # a killed run's last line
        key = (row["video_id"], row["frame"])
        if row.get("answer") or not best.get(key, {}).get("answer"):
            best[key] = row
    return best


def classify(args: argparse.Namespace, videos: list[dict[str, Any]],
             ledger: dict[tuple[str, int], dict[str, Any]]) -> None:
    from local_vlm import DEFAULT_MODEL_PATH
    from local_vlm.backend import LocalVLM

    from video_signals import FLOOD_TEXT_THRESHOLD
    # Text-flood videos first: they are the ones the rule can change, so a run
    # stopped early has spent its time where it matters.
    ordered = sorted(videos, key=lambda v: (v.get("flood_text_score") or 0) < FLOOD_TEXT_THRESHOLD)
    todo = [(v["video_id"], n) for v in ordered for n in FRAMES
            if not ledger.get((v["video_id"], n), {}).get("answer")]
    if args.limit:
        todo = todo[:args.limit]
    print(f"frames: {len(videos) * len(FRAMES)} total, {len(todo)} to classify "
          f"({len(videos) * len(FRAMES) - len(todo)} already answered)", flush=True)
    if not todo:
        return

    vlm = LocalVLM(args.model_path or DEFAULT_MODEL_PATH, device_map=args.device_map, max_new_tokens=16)
    t0 = time.monotonic()
    vlm.load()
    print(f"model loaded on {args.device_map} in {time.monotonic() - t0:.0f}s", flush=True)

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()
    done = yes = failed = 0
    t0 = time.monotonic()
    with LEDGER.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(vlm.classify, frame_url(vid, n), PROMPT): (vid, n) for vid, n in todo}
        for future in as_completed(futures):
            vid, n = futures[future]
            result = future.result()
            row = {"video_id": vid, "frame": n, "image_url": frame_url(vid, n),
                   "answer": result.get("answer"), "status": result.get("status"),
                   "model_output": result.get("model_output"), "error": result.get("error"),
                   "prompt": PROMPT, "model": vlm.name, "at": now_iso()}
            with write_lock:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
            ledger[(vid, n)] = row if row["answer"] or not ledger.get((vid, n), {}).get("answer") \
                else ledger[(vid, n)]
            done += 1
            yes += row["answer"] == "yes"
            failed += not row["answer"]
            if done % 200 == 0 or done == len(todo):
                rate = done / max(time.monotonic() - t0, 1e-9)
                print(f"  {done}/{len(todo)} frames  yes {yes}  failed {failed}  "
                      f"{rate:.1f}/s  ~{(len(todo) - done) / max(rate, 1e-9) / 60:.0f} min left", flush=True)


def flood_visual(video_id: str, ledger: dict[tuple[str, int], dict[str, Any]]) -> dict[str, Any] | None:
    rows = [ledger.get((video_id, n)) for n in FRAMES]
    if not any(rows):
        return None                                    # never attempted: stays unclassified
    frames = [{"frame": n, "image_url": frame_url(video_id, n), "answer": (r or {}).get("answer")}
              for n, r in zip(FRAMES, rows)]
    answers = [f["answer"] for f in frames if f["answer"]]
    first = next(r for r in rows if r)
    return {
        "prompt": first.get("prompt"),
        "model": first.get("model"),
        "classified_at": max(r["at"] for r in rows if r),
        "frames": frames,
        "flood": True if "yes" in answers else (False if answers else None),
    }


def merge(videos_path: Path, ledger: dict[tuple[str, int], dict[str, Any]]) -> None:
    """Write `flood_visual` into the master file, under the scraper's lock,
    then rebuild the flood-only file and both metadata files."""
    import export_flood_videos
    import make_youtube_metadata
    from gis_scrape import exclusive_run, write_json

    with exclusive_run(videos_path):
        rows = json.loads(videos_path.read_text(encoding="utf-8"))
        for row in rows:
            visual = flood_visual(row["video_id"], ledger)
            if visual is not None:
                row["flood_visual"] = visual
        write_json(videos_path, rows)
        verdicts = [r["flood_visual"]["flood"] for r in rows if r.get("flood_visual")]
        print(f"merged into {videos_path.name}: {len(verdicts)} of {len(rows)} videos classified -- "
              f"flood visible {verdicts.count(True)}, not {verdicts.count(False)}, "
              f"undecidable {verdicts.count(None)}", flush=True)
        export_flood_videos.export(videos_path)
    make_youtube_metadata.refresh(videos_path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--videos", type=Path, default=DEFAULT_VIDEOS)
    ap.add_argument("--device-map", default="auto",
                    help="e.g. cuda:6 -- pin one free GPU; others may be in use by other people")
    ap.add_argument("--model-path", default=None)
    ap.add_argument("--workers", type=int, default=4, help="concurrent image fetches (GPU work is serialised)")
    ap.add_argument("--limit", type=int, default=None, help="classify at most this many frames (trial)")
    ap.add_argument("--merge-only", action="store_true", help="skip the GPU, write existing answers back")
    args = ap.parse_args()

    videos = json.loads(args.videos.read_text(encoding="utf-8"))
    ledger = read_ledger(LEDGER)
    if not args.merge_only:
        classify(args, videos, ledger)
    if args.limit:
        # A trial must not rebuild the flood-only file: with a handful of
        # videos classified, the visual rule would empty it until a full run.
        print("trial (--limit): answers are in the ledger; not merged. "
              "Run without --limit, or with --merge-only, to apply them.")
        return 0
    merge(args.videos, ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

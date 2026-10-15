#!/usr/bin/env python3
"""Scan flood videos shot by shot: which shots actually show flooding?

For every video (flood-only file, <= --max-duration, owner's "no long videos
yet"), in a pipeline:

  producer thread (CPU + a little GPU), one video ahead of the model:
    1. download to a temporary file      (youtube_download; deleted in step 4)
    2. shots: transnetv2 + autoshot, combined with methods.combine (the union
       rule, owner 2026-10-15) -- both models' raw shots are kept in the
       ledger, so the union rule can change later WITHOUT re-downloading
    3. sample frames per union shot (owner's rule + refinements, 2026-10-15):
         shot >= 1 s : 3 frames at 10%, 50%, 90% of the shot (not the very
                       first/last frame, which can be half-blended at a cut)
         shot <  1 s : 1 frame, the middle (three would be the same image)
    4. delete the video -- only the sampled frames (in memory) go on
  main thread (the VLM, on one GPU):
    5. ask SHOT_PROMPT of every sampled frame
    6. a shot is a flood shot if >= 2 of its 3 frames say yes (1-frame shots:
       that frame decides). A frame the model could not answer counts as no.
    7. append one line per video to the ledger, flushed at once

Ledger: one JSONL file PER WORKER in scrape_data/youtube_shot_scan/
(shard_<i>of<n>.jsonl) -- /home is NFS, where several processes appending to
ONE file can interleave or lose lines. Resume reads every file in the folder:
a video with a line (and no transient error) is never scanned again, whatever
the shard count was. It is the
record of the scan, not unmerged work: nothing reads it into
data/youtube_videos.json yet (that waits for scene grouping and the `scenes`
field -- scraping/video_scraping/CLAUDE.md, "PLAN: flood scenes").

Videos are downloaded and deleted one at a time; no video file is kept
(owner's rule, 2026-10-14). Run several shards on different GPUs to go
faster; each loads its own copy of the VLM (~55 GB):

    python3 experiments/scene_split/scan_shots.py --only xtBb2eU9d2k i-U6LeACMnk --save-frames DIR   # test
    python3 experiments/scene_split/scan_shots.py --shard 0/4 --device-map cuda:0   # one of four workers
    (start workers on DIFFERENT GPUs: started together, "free" would pick the same one)
"""
from __future__ import annotations

import os

# A cap on CPU threads per worker, set before torch / numpy load: several
# workers each taking every core is what overloaded the shared machine.
for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PROJECT / "scraping" / "video_scraping"))
sys.path.insert(0, str(PROJECT / "verification"))
sys.path.insert(0, str(PROJECT / "scraping" / "api_based_scraping"))
sys.path.insert(0, str(PROJECT))
import verify_video_frames  # noqa: E402,F401  -- sets CUDA_DEVICE_ORDER before torch starts
from verify_video_frames import pick_free_gpu  # noqa: E402
import methods  # noqa: E402
from youtube_download import DownloadError, NeedsCookies, Unavailable, TooLong, downloaded  # noqa: E402

FLOOD_FILE = PROJECT / "data" / "youtube_flood_videos.json"
LEDGER_DIR = PROJECT / "scrape_data" / "youtube_shot_scan"

#: The frame-check question, plus the owner-approved clause (2026-10-15) so a
#: weather map or a "FLASH FLOOD WARNING" banner is not counted as flooding.
SHOT_PROMPT = ("Is there any flooding visible in this image? Count only flooding in the scene "
               "itself, not in text, maps or graphics. Answer with a single word: yes or no.")
SHORT_SHOT_S = 1.0            # below this, one middle frame
SAMPLE_AT = (0.1, 0.5, 0.9)   # fractions of the shot
PASS_OF_3 = 2                 # flood shot: >= 2 of 3 frames say yes
MIN_DOWNLOAD_GAP_S = 3.0      # be gentle with YouTube


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sample_times(start: float, end: float) -> list[float]:
    dur = end - start
    if dur < SHORT_SHOT_S:
        return [round((start + end) / 2, 3)]
    return [round(start + f * dur, 3) for f in SAMPLE_AT]


def is_flood_shot(answers: list[str | None]) -> bool:
    yes = sum(a == "yes" for a in answers)
    return yes >= 1 if len(answers) == 1 else yes >= PASS_OF_3


def read_ledger(folder: Path) -> dict[str, dict]:
    """Latest line per video across every worker's file; a finished line
    beats a later transient error for the same video."""
    done: dict[str, dict] = {}
    for path in sorted(folder.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue                                  # a killed run's last line
            prev = done.get(row["video_id"])
            # A finished line always wins; among errors, the latest counts.
            if prev is None or "error" not in row or "error" in prev:
                if not (prev is not None and "error" not in prev):
                    done[row["video_id"]] = row
    return done


def needs_scan(row: dict | None) -> bool:
    """Scan unless a finished line exists. Permanent failures (gone, too long)
    are not retried; transient ones (network, 403 after retries) are."""
    if row is None:
        return True
    return row.get("error_kind") == "transient"


# --------------------------------------------------------------------------
# Producer: download -> shots -> frames -> delete
# --------------------------------------------------------------------------

def prepare(video_id: str, max_duration: float) -> dict:
    """Everything the VLM step needs, with the video already deleted."""
    import cv2
    from PIL import Image
    with downloaded(video_id, max_duration_s=max_duration) as video:
        fps = video.fps or 25.0
        raw = {}
        timing = {}
        for name in methods.METHODS:
            t0 = time.monotonic()
            raw[name] = methods.run(name, video.path, fps, video.duration_s)
            timing[name] = round(time.monotonic() - t0, 1)
        union = methods.combine(raw["transnetv2"], raw["autoshot"], fps=fps)
        cap = cv2.VideoCapture(str(video.path))
        shots = []
        for start, end in union:
            frames = []
            for t in sample_times(start, end):
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, bgr = cap.read()
                frames.append((t, Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)) if ok else None))
            shots.append({"start": start, "end": end, "frames": frames})
        cap.release()
        meta = {"title": video.title, "duration_s": video.duration_s, "fps": fps,
                "width": video.width, "height": video.height}
    # the video file is gone here
    return {"video_id": video_id, "meta": meta, "raw_shots": raw, "detect_s": timing, "shots": shots}


def producer(todo: list[str], out: queue.Queue, stop: threading.Event, max_duration: float) -> None:
    last = 0.0
    for vid in todo:
        if stop.is_set():
            break
        wait = MIN_DOWNLOAD_GAP_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        try:
            item = prepare(vid, max_duration)
        except NeedsCookies as exc:
            out.put({"video_id": vid, "fatal": f"YouTube bot check, stopping: {exc}"})
            break
        except (Unavailable, TooLong) as exc:
            item = {"video_id": vid, "error": f"{type(exc).__name__}: {exc}", "error_kind": "permanent"}
        except Exception as exc:  # noqa: BLE001 -- one bad video must not end the scan
            item = {"video_id": vid, "error": f"{type(exc).__name__}: {exc}"[:500], "error_kind": "transient"}
        out.put(item)
    out.put(None)


# --------------------------------------------------------------------------
# Main: VLM over the sampled frames
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="these video ids only (test)")
    ap.add_argument("--shard", default="0/1", help="i/n: this worker takes every n-th video starting at i")
    ap.add_argument("--max-duration", type=float, default=300, help="skip longer videos (s)")
    ap.add_argument("--limit", type=int, default=None, help="stop after this many videos")
    ap.add_argument("--ledger-dir", type=Path, default=LEDGER_DIR)
    ap.add_argument("--device-map", default="free", help="'free' = GPU with the most free memory, or cuda:N")
    ap.add_argument("--save-frames", type=Path, default=None,
                    help="also write each sampled frame as JPEG here, named by verdict (for inspecting a test)")
    args = ap.parse_args()

    shard_i, shard_n = (int(x) for x in args.shard.split("/"))
    done = read_ledger(args.ledger_dir)
    if args.only:
        candidates = list(args.only)
    else:
        videos = json.loads(FLOOD_FILE.read_text(encoding="utf-8"))
        candidates = [v["video_id"] for v in videos if (v.get("duration_s") or 0) <= args.max_duration]
    todo = [v for k, v in enumerate(candidates) if k % shard_n == shard_i and needs_scan(done.get(v))]
    if args.limit:
        todo = todo[:args.limit]
    print(f"=== {now_iso()} scan_shots shard {args.shard}: {len(todo)} videos to scan "
          f"({sum(1 for v in candidates if not needs_scan(done.get(v)))} of {len(candidates)} already done)",
          flush=True)
    if not todo:
        return 0

    from local_vlm import DEFAULT_MODEL_PATH
    from local_vlm.backend import LocalVLM, parse_answer
    device = args.device_map
    if device == "free":
        gpu = pick_free_gpu(60)
        if gpu is None:
            print("no GPU has 60 GiB free; not starting", flush=True)
            return 1
        device = f"cuda:{gpu}"
    import torch
    torch.set_num_threads(4)
    methods.DEVICE = device                     # shot models on this worker's GPU too
    vlm = LocalVLM(DEFAULT_MODEL_PATH, device_map=device, max_new_tokens=16)
    t0 = time.monotonic()
    vlm.load()
    print(f"VLM loaded on {device} in {time.monotonic() - t0:.0f} s", flush=True)

    q: queue.Queue = queue.Queue(maxsize=2)
    stop = threading.Event()
    threading.Thread(target=producer, args=(todo, q, stop, args.max_duration), daemon=True).start()

    args.ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger = args.ledger_dir / f"shard_{shard_i}of{shard_n}{'_only' if args.only else ''}.jsonl"
    n = shots_total = flood_total = 0
    t_start = time.monotonic()
    with ledger.open("a", encoding="utf-8") as fh:
        while (item := q.get()) is not None:
            if "fatal" in item:
                print(f"FATAL {item['video_id']}: {item['fatal']}", flush=True)
                stop.set()
                break
            row = {"video_id": item["video_id"], "scanned_at": now_iso()}
            if "error" in item:
                row.update(error=item["error"], error_kind=item["error_kind"])
                print(f"  {item['video_id']}: {item['error_kind']} error: {item['error'][:120]}", flush=True)
            else:
                shots_out = []
                for k, shot in enumerate(item["shots"]):
                    answers = []
                    for t, image in shot["frames"]:
                        answer = parse_answer(vlm._generate(image, SHOT_PROMPT)) if image is not None else None
                        answers.append(answer)
                        if args.save_frames and image is not None:
                            d = args.save_frames / item["video_id"]
                            d.mkdir(parents=True, exist_ok=True)
                            image.resize((320, round(image.height * 320 / image.width))).save(
                                d / f"shot{k + 1:03d}_{t:07.2f}s_{answer}.jpg", quality=80)
                    flood = is_flood_shot(answers)
                    shots_out.append({"start": shot["start"], "end": shot["end"],
                                      "frames": [{"t": t, "answer": a} for (t, _), a in zip(shot["frames"], answers)],
                                      "flood": flood})
                row.update(item["meta"], prompt=SHOT_PROMPT, model=vlm.name, detect_s=item["detect_s"],
                           raw_shots=item["raw_shots"], shots=shots_out)
                n += 1
                shots_total += len(shots_out)
                fl = sum(s["flood"] for s in shots_out)
                flood_total += fl
                rate = n / (time.monotonic() - t_start)
                print(f"  [{n}/{len(todo)}] {item['video_id']}: {len(shots_out)} shots, {fl} flood "
                      f"({sum(s['end'] - s['start'] for s in shots_out if s['flood']):.0f} s of "
                      f"{item['meta']['duration_s']:.0f} s) -- {3600 * rate:.0f} videos/h", flush=True)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
    print(f"=== done: {n} videos, {shots_total} shots, {flood_total} flood shots", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

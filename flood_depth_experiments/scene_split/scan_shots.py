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
    6b. AI check (owner, 2026-10-15): ONE question per shot ("is this real
       camera footage?"; no = AI) with all its frames together (AI video gives itself away over time -- things that
       morph between frames), after the owner's example AI images if any are
       in AI_EXAMPLES_DIR. The VIDEO is AI-generated if >= AI_VIDEO_SHARE of
       its answered shots are; the owner's rule is to drop such a video whole.
       Both the per-shot answers and the video verdict go in the ledger.
    7. append one line per video to the ledger, flushed at once
    8. save every sampled frame as a small JPEG (FRAME_WIDTH px wide) in
       <ledger-dir>/frames/<video_id>/shot<k>_<j>.jpg, so the review page
       (data_view_server.py, /shots.html) shows exactly what the model saw.
       Stills, not video; gitignored, never published. --no-frames turns it off.

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

    python3 flood_depth_experiments/scene_split/scan_shots.py --only xtBb2eU9d2k i-U6LeACMnk   # test
    python3 flood_depth_experiments/scene_split/scan_shots.py --probe      # is YouTube letting us download? (exit 0 / 3)
    flood_depth_experiments/scene_split/run_scan.sh                        # ROUTINE: one slow worker, waits out bot checks

Speed (owner, 2026-10-15): four workers at ~750 videos/h tripped YouTube's
"confirm you're not a bot" check after ~1 h. Now ONE worker, a randomised
--gap between downloads (~180 videos/h); on a bot check the scan exits with
code 3 (freeing the GPU) and run_scan.sh probes again every few hours.
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

#: Owner's prompt, 2026-10-15 (second version): real flooding seen at STREET
#: LEVEL -- streets, buildings and surroundings visible -- in a real, not
#: AI-generated, picture. The first version ("Is there any flooding visible in
#: this image? Count only flooding in the scene itself, not in text, maps or
#: graphics. ...") scanned the first 750 videos; each ledger line records its
#: own prompt, and finished videos are not re-scanned when this changes.
SHOT_PROMPT = (
    "Does this image show real flooding seen from street level? Answer yes if water is "
    "standing or flowing across a street, sidewalk, parking lot, yard or other ground where "
    "it does not normally stand -- deep or shallow -- and the camera is roughly at the height "
    "of a person or a vehicle, with some surroundings visible such as the street, buildings, "
    "vehicles, poles or signs. The picture must be a real photograph or video frame, not "
    "AI-generated, animated, rendered or from a video game. Phone videos often show blurred "
    "copies of the picture at the sides; judge the sharp picture in the middle. Answer no "
    "for maps, charts, graphics, or text and captions about flooding; news anchors, "
    "interviews or people filling the frame; aerial, drone or high elevated views; indoor "
    "scenes; close-ups of water with no surroundings; and roads that are only wet from rain. "
    "Answer with a single word: yes or no.")
SHORT_SHOT_S = 1.0            # below this, one middle frame
SAMPLE_AT = (0.1, 0.5, 0.9)   # fractions of the shot
PASS_OF_3 = 2                 # flood shot: >= 2 of 3 frames say yes
DOWNLOAD_GAP_S = 10.0         # mean pause between downloads, randomised 0.5x-1.5x
FRAME_WIDTH = 320             # saved review frames
EXIT_BOT_CHECK = 3            # exit code when YouTube asks to confirm we are not a bot
PROBE_TRIES = 10              # --probe: skip up to this many gone/too-long videos
#: Stream 403s in a row that count as a block, like the bot check (exit 3).
#: Seen 2026-10-24: metadata (and so --probe) still worked, but every stream --
#: https and HLS, yt-dlp 2026.10.21 with no PO-token provider -- was refused, and
#: the scan would otherwise spend ~25 s per video failing through the whole list.
STREAM_403_BLOCK = 5

#: Per-shot AI question (owner, 2026-10-15). Asked the OTHER way round --
#: "is this real camera footage?", answer "no" = AI -- because asked "is this
#: AI-generated?" the model said no to obvious AI: on YouTube's auto-frames of
#: 20 videos their uploaders call AI and 20 real pre-2023 flood videos it
#: caught 6/20 AI, 0/20 false. This question: 16/20 AI (one of the 4 misses
#: looks real), 1/20 false. The note on blurred side copies / tilted slideshow
#: photos removed 2 of 3 false alarms (real phone video read as "edited").
AI_PROMPT = (
    "Is this real camera footage of a real place, filmed by a phone, dashcam, news or "
    "security camera? Answer no if it is AI-generated (Sora, Veo, Kling and similar), "
    "computer-rendered, animated or a video game. AI video often looks too clean, glossy and "
    "cinematic, with dramatic lighting, warped or melting details, garbled text, or impossible "
    "scenes. Shaky, blurry, low-quality, compressed or filtered video and news graphics on real "
    "footage are still real. Phone videos often show blurred copies of the picture at the "
    "sides, and slideshows show photos tilted on a blurred background; those edits are not "
    "AI -- judge the picture itself. Answer with a single word: yes or no.")
#: AI_PROMPT's answer -> the shot's `ai` label.
AI_FROM_ANSWER = {"yes": "no", "no": "yes"}
#: The owner's example AI images (any .jpg/.jpeg/.png/.webp), shown before
#: every shot's frames. Gitignored; empty or missing = no examples.
AI_EXAMPLES_DIR = PROJECT / "scrape_data" / "ai_examples"
AI_EXAMPLES_MAX = 8           # more costs time on every shot
AI_EXAMPLE_SIDE = 448         # examples are shrunk to this longest side
#: A video is AI-generated if at least this share of its answered shots are.
AI_VIDEO_SHARE = 0.5


def load_ai_examples(folder: Path = AI_EXAMPLES_DIR) -> list[tuple[str, "Image.Image"]]:
    from PIL import Image
    paths = sorted(p for p in folder.glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}) \
        if folder.is_dir() else []
    examples = []
    for path in paths[:AI_EXAMPLES_MAX]:
        image = Image.open(path).convert("RGB")
        image.thumbnail((AI_EXAMPLE_SIDE, AI_EXAMPLE_SIDE))
        examples.append((path.name, image))
    return examples


def ai_parts(examples: list, frames: list) -> list:
    """The AI question's message: examples (if any), the shot's frames, the prompt."""
    parts: list = []
    if examples:
        parts += ["For reference, these are frames from AI-generated videos:", *(im for _, im in examples)]
    parts += [f"Now judge this video: {len(frames)} frame(s) from one shot, in time order:", *frames, AI_PROMPT]
    return parts


def ai_video_verdict(shots: list[dict]) -> tuple[float | None, bool | None]:
    """(share of answered shots judged AI, is the video AI). None when no
    shot has an answer."""
    answered = [s["ai"] for s in shots if s.get("ai") in ("yes", "no")]
    if not answered:
        return None, None
    share = sum(a == "yes" for a in answered) / len(answered)
    return round(share, 3), share >= AI_VIDEO_SHARE


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


def frame_path(frames_dir: Path, video_id: str, shot: int, frame: int) -> Path:
    """Where frame `frame` (0-based) of shot `shot` (0-based) is saved; the
    review server builds the same names."""
    return frames_dir / video_id / f"shot{shot + 1:03d}_{frame}.jpg"


def probe(video_ids: list[str], tries: int = PROBE_TRIES) -> int:
    """0 if YouTube hands out a video's formats, EXIT_BOT_CHECK if it asks for
    a sign-in, 1 on anything else. Metadata only: nothing is downloaded.

    A video that is gone (private, deleted) or too long says nothing about
    YouTube letting us in, so the probe moves on to the next id -- one video
    made private stopped the whole scan on 2026-10-19. The scan itself records
    such videos as permanent errors and skips them."""
    import yt_dlp
    from youtube_download import _classify, _options
    import tempfile
    for video_id in video_ids[:tries]:
        with tempfile.TemporaryDirectory() as tmp, yt_dlp.YoutubeDL(_options(Path(tmp))) as ydl:
            try:
                ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            except yt_dlp.utils.DownloadError as exc:
                err = _classify(exc)
                print(f"probe {video_id}: {type(err).__name__}: {str(err)[:200]}", flush=True)
                if isinstance(err, (Unavailable, TooLong)):
                    continue
                return EXIT_BOT_CHECK if isinstance(err, NeedsCookies) else 1
        print(f"probe {video_id}: ok", flush=True)
        return 0
    print(f"probe: none of {min(tries, len(video_ids))} videos was available; giving up", flush=True)
    return 1


def producer(todo: list[str], out: queue.Queue, stop: threading.Event, max_duration: float,
             gap_s: float) -> None:
    import random
    last = 0.0
    refused = 0                 # stream 403s in a row
    for vid in todo:
        if stop.is_set():
            break
        wait = gap_s * random.uniform(0.5, 1.5) - (time.monotonic() - last)
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
        refused = refused + 1 if "HTTP Error 403" in item.get("error", "") else 0
        out.put(item)
        if refused >= STREAM_403_BLOCK:
            out.put({"video_id": vid, "fatal": f"{refused} stream 403s in a row, treating as a block"})
            break
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
    ap.add_argument("--gap", type=float, default=DOWNLOAD_GAP_S,
                    help="mean seconds between downloads (randomised 0.5x-1.5x; default %(default)s)")
    ap.add_argument("--no-frames", action="store_true", help="do not save the sampled frames for review")
    ap.add_argument("--probe", action="store_true",
                    help=f"only check whether YouTube allows downloads (exit 0, or {EXIT_BOT_CHECK} on a bot check)")
    args = ap.parse_args()
    frames_dir = None if args.no_frames else args.ledger_dir / "frames"

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
    if args.probe:
        return probe(todo) if todo else 0
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
    ai_examples = load_ai_examples()
    print(f"AI check: {len(ai_examples)} example image(s) from {AI_EXAMPLES_DIR}", flush=True)

    q: queue.Queue = queue.Queue(maxsize=2)
    stop = threading.Event()
    threading.Thread(target=producer, args=(todo, q, stop, args.max_duration, args.gap), daemon=True).start()

    args.ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger = args.ledger_dir / f"shard_{shard_i}of{shard_n}{'_only' if args.only else ''}.jsonl"
    n = shots_total = flood_total = 0
    blocked = False
    t_start = time.monotonic()
    with ledger.open("a", encoding="utf-8") as fh:
        while (item := q.get()) is not None:
            if "fatal" in item:
                print(f"FATAL {item['video_id']}: {item['fatal']}", flush=True)
                stop.set()
                blocked = True
                break
            row = {"video_id": item["video_id"], "scanned_at": now_iso()}
            if "error" in item:
                row.update(error=item["error"], error_kind=item["error_kind"])
                print(f"  {item['video_id']}: {item['error_kind']} error: {item['error'][:120]}", flush=True)
            else:
                shots_out = []
                for k, shot in enumerate(item["shots"]):
                    answers = []
                    for j, (t, image) in enumerate(shot["frames"]):
                        answer = parse_answer(vlm._generate(image, SHOT_PROMPT)) if image is not None else None
                        answers.append(answer)
                        if frames_dir and image is not None:
                            path = frame_path(frames_dir, item["video_id"], k, j)
                            path.parent.mkdir(parents=True, exist_ok=True)
                            image.resize((FRAME_WIDTH, round(image.height * FRAME_WIDTH / image.width))).save(
                                path, quality=80)
                    flood = is_flood_shot(answers)
                    images = [image for _, image in shot["frames"] if image is not None]
                    ai = (AI_FROM_ANSWER.get(parse_answer(vlm.generate_parts(ai_parts(ai_examples, images))))
                          if images else None)
                    shots_out.append({"start": shot["start"], "end": shot["end"],
                                      "frames": [{"t": t, "answer": a} for (t, _), a in zip(shot["frames"], answers)],
                                      "flood": flood, "ai": ai})
                ai_share, ai_video = ai_video_verdict(shots_out)
                row.update(item["meta"], prompt=SHOT_PROMPT, model=vlm.name, detect_s=item["detect_s"],
                           raw_shots=item["raw_shots"], shots=shots_out,
                           ai_prompt=AI_PROMPT, ai_examples=[name for name, _ in ai_examples],
                           ai_share=ai_share, ai_video=ai_video, ai_video_share=AI_VIDEO_SHARE)
                n += 1
                shots_total += len(shots_out)
                fl = sum(s["flood"] for s in shots_out)
                flood_total += fl
                rate = n / (time.monotonic() - t_start)
                print(f"  [{n}/{len(todo)}] {item['video_id']}: {len(shots_out)} shots, {fl} flood "
                      f"({sum(s['end'] - s['start'] for s in shots_out if s['flood']):.0f} s of "
                      f"{item['meta']['duration_s']:.0f} s), AI {ai_share} -> {ai_video} -- {3600 * rate:.0f} videos/h", flush=True)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
    print(f"=== {'stopped by a YouTube block' if blocked else 'done'}: {n} videos, {shots_total} shots, "
          f"{flood_total} flood shots", flush=True)
    return EXIT_BOT_CHECK if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())

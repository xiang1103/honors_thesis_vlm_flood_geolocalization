#!/usr/bin/env python3
"""Scene filter: is a shot a direct camera view of one real scene?

Why (docs/water_depth_segmentation.md, "Dry-scene calibration"): MoGe-2
assumes one real scene seen through one lens. A TV studio with flood footage
on a screen behind the presenter, picture-in-picture, a phone or monitor
filmed by another camera, or a view through a windscreen with the dashboard
in frame breaks that assumption -- MoGe takes its scale from the screen or
the glass, and objects behind it measured 8-25% of their true size. A VLM
asked for water depth is fooled by the same shots.

LABELS, NOT A FILTER: every shot keeps its row everywhere; this only writes
one label per shot. Depth tests read `skip_ids()` and leave those shots out
(geometry_depth.py, calibrate_dry.py, evaluate.py --scene real); nothing is
deleted, and changing SKIP needs no re-run.

Per shot, the local VLM (thinking off) sees the shot's key frame (the middle
of 3, the frame every depth method uses) and picks one letter from PROMPT.

Shots labelled, in this order:
  1. the dry-scene calibration frames (calibrate_dry.py) -- labelled first so
     the filter can be checked against the scale MoGe actually measured there
  2. the geometry pilot's shots
  3. every eligible flood shot (sample_items.eligible)
  --all-shots adds every other shot in the scan ledger with a key frame.

Output (experiment only, nothing in data/):
  scrape_data/experiments/flood_depth/scene_filter/
    labels_<i>of<n>.jsonl   one line per shot, appended + flushed; resume key
                            is (id, prompt_id): editing PROMPT re-labels
    summary.json            counts per scene, and the calibration check

    python3 flood_depth_experiments/scene_split/scene_filter.py --limit 20        # smoke test
    python3 flood_depth_experiments/scene_split/scene_filter.py --shard 0/3       # one of 3 workers
    python3 flood_depth_experiments/scene_split/scene_filter.py --summary         # counts + check, no GPU
"""
from __future__ import annotations

import os

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "flood_depth_experiments" / "flood_depth"))
sys.path.insert(0, str(PROJECT / "flood_depth_experiments" / "depth_detection"))
sys.path.insert(0, str(PROJECT / "verification"))
sys.path.insert(0, str(PROJECT / "scraping" / "video_scraping"))
sys.path.insert(0, str(PROJECT / "scraping" / "api_based_scraping"))

from sample_items import FRAMES_DIR, KEY_FRAME, OUT_DIR as DEPTH_DIR, eligible  # noqa: E402
from scan_shots import LEDGER_DIR, frame_path, read_ledger  # noqa: E402

OUT_DIR = DEPTH_DIR / "scene_filter"
CALIBRATION_FRAMES = DEPTH_DIR / "geometry" / "calibration" / "frames.jsonl"
GEOMETRY_SHOTS = DEPTH_DIR / "geometry" / "shots.jsonl"

PROMPT = """Look at this frame from a video. Which ONE of these describes it best?

A. A camera filming a real scene directly: a street, flood, building, landscape or people. Dashcam footage counts as A when little or none of the car's inside is visible. Small text banners or logos on top of the picture are fine.
B. A TV studio or newsroom: a presenter, desk or set, with any footage shown on a screen or video wall.
C. Two or more separate pictures in one frame: picture-in-picture, split screen, side by side, or a collage.
D. A screen filmed by another camera: a phone, TV, monitor or tablet showing a picture.
E. A view through a vehicle windscreen or a window where the glass, its frame, the dashboard or the vehicle's inside take up a clear part of the frame.
F. Not camera footage of a scene: a map, chart, title card, text, logo, or computer graphic.

Answer with the letter only."""

SCENES = {"A": "direct", "B": "studio", "C": "multi_picture", "D": "filmed_screen",
          "E": "through_glass", "F": "graphic"}
#: Scenes the depth tests leave out. through_glass is in on the calibration's
#: evidence; `--summary` reports each scene's measured scale, so check there
#: before changing this.
SKIP = {"studio", "multi_picture", "filmed_screen", "through_glass", "graphic"}
MAX_NEW_TOKENS = 8
LETTER_RE = re.compile(r"\b([A-F])\b")
THINK_RE = re.compile(r"<think>.*?(</think>|\Z)", re.DOTALL | re.IGNORECASE)


def prompt_id() -> str:
    return "scene:" + hashlib.sha256(PROMPT.encode()).hexdigest()[:8]


def parse_letter(text: str) -> str | None:
    m = LETTER_RE.search(THINK_RE.sub(" ", text))
    return m.group(1) if m else None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- reading the labels (what the depth tests call) ---------------------------

def read_labels(folder: Path = OUT_DIR) -> dict[str, dict]:
    """id -> row, for the CURRENT prompt only (a row from an edited prompt is
    stale). Later lines win."""
    pid, out = prompt_id(), {}
    for path in sorted(folder.glob("labels_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("prompt_id") == pid:
                    out[row["id"]] = row
    return out


def read_scenes(folder: Path = OUT_DIR) -> dict[str, str | None]:
    """id -> scene name (None = the model gave no letter)."""
    return {i: r["scene"] for i, r in read_labels(folder).items()}


def skip_ids(folder: Path = OUT_DIR) -> set[str]:
    """Shots the depth tests leave out. An unlabelled shot is NOT in here --
    unknown is kept, and readers report how many were unlabelled."""
    return {i for i, s in read_scenes(folder).items() if s in SKIP}


# -- which shots ---------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def targets(all_shots: bool) -> list[tuple[str, str]]:
    """(id, key-frame path relative to the project), in labelling order."""
    out: dict[str, str] = {}
    for row in _read_jsonl(CALIBRATION_FRAMES):
        if row.get("path"):
            out.setdefault(row["id"], row["path"])
    for row in _read_jsonl(GEOMETRY_SHOTS):
        out.setdefault(row["id"], row["frames"][KEY_FRAME])
    ledger = read_ledger(LEDGER_DIR)
    items, _ = eligible(ledger, FRAMES_DIR)
    for it in sorted(items, key=lambda it: it["item_id"]):
        out.setdefault(it["item_id"], it["frames"][it["key_frame"]]["path"])
    if all_shots:
        for vid in sorted(ledger):
            for k, shot in enumerate(ledger[vid].get("shots", [])):
                p = frame_path(FRAMES_DIR, vid, k, KEY_FRAME)
                if len(shot["frames"]) > KEY_FRAME and p.exists():
                    out.setdefault(f"{vid}:shot{k + 1:03d}", str(p.relative_to(PROJECT)))
    return list(out.items())


# -- labelling -----------------------------------------------------------------

def run(args) -> None:
    from verify_video_frames import pick_free_gpu  # sets CUDA_DEVICE_ORDER before torch starts

    shard_i, shard_n = (int(x) for x in args.shard.split("/"))
    done = read_labels()
    todo = [t for k, t in enumerate(targets(args.all_shots)) if k % shard_n == shard_i and t[0] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"=== {now_iso()} scene_filter shard {args.shard}: {len(todo)} shots to do "
          f"({len(done)} labelled, all shards)", flush=True)
    if not todo:
        return
    device = args.device
    if device == "free":
        gpu = pick_free_gpu(58)
        if gpu is None:
            raise SystemExit("no GPU has 58 GiB free; not starting")
        device = f"cuda:{gpu}"
    import torch
    from PIL import Image
    from local_vlm import DEFAULT_MODEL_PATH
    from local_vlm.backend import LocalVLM
    torch.set_num_threads(4)
    vlm = LocalVLM(DEFAULT_MODEL_PATH, device_map=device, max_new_tokens=MAX_NEW_TOKENS)
    t0 = time.monotonic()
    vlm.load()
    print(f"VLM loaded on {device} in {time.monotonic() - t0:.0f} s", flush=True)

    pid, model = prompt_id(), vlm.name
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.monotonic()
    with (OUT_DIR / f"labels_{shard_i}of{shard_n}.jsonl").open("a", encoding="utf-8") as fh:
        for n, (iid, path) in enumerate(todo, start=1):
            row = {"id": iid, "frame": path, "prompt_id": pid, "model": model}
            try:
                with Image.open(PROJECT / path) as im:
                    image = im.convert("RGB")
                text = vlm.generate_parts([image, PROMPT])
                letter = parse_letter(text)
                row.update(scene=SCENES.get(letter), letter=letter, raw=text)
            except Exception as exc:  # noqa: BLE001 -- one unreadable frame must not stop the run
                row.update(scene=None, letter=None, error=f"{type(exc).__name__}: {exc}")
            row["at"] = now_iso()
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            if n % 200 == 0 or n == len(todo):
                el = time.monotonic() - t_start
                print(f"  [{n}/{len(todo)}] {n / el * 3600:.0f} shots/h, "
                      f"eta {(len(todo) - n) / (n / el) / 3600:.1f} h", flush=True)
    print(f"=== {now_iso()} shard {args.shard} done", flush=True)


# -- summary + check against the calibration's measured scale -----------------

def summary() -> dict:
    labels = read_labels()
    scenes = {i: r["scene"] for i, r in labels.items()}
    calib = _read_jsonl(CALIBRATION_FRAMES)
    geo = {r["id"] for r in _read_jsonl(GEOMETRY_SHOTS)}
    items, _ = eligible(read_ledger(LEDGER_DIR), FRAMES_DIR)
    flood = {it["item_id"] for it in items}

    def counts(ids) -> dict:
        c = Counter(scenes[i] if i in scenes else "unlabelled" for i in ids)
        c = {str(k): v for k, v in c.most_common()}
        labelled = sum(v for k, v in c.items() if k != "unlabelled")
        skipped = sum(v for k, v in c.items() if k in SKIP)
        return {"shots": len(ids), "by_scene": c,
                "skipped_share_of_labelled": round(skipped / labelled, 3) if labelled else None}

    # MoGe's measured / nominal size of license plates and wheels per scene: the
    # calibration's one independent check of whether a scene breaks the scale
    from calibrate_dry import nominal
    ratios: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    near: dict[str, Counter] = defaultdict(Counter)
    for row in calib:
        scene = scenes.get(row["id"], "unlabelled")
        for inst in row.get("instances", []):
            if inst["reject"]:
                continue
            near[scene]["instances"] += 1
            near[scene]["under_5m"] += inst["dist_m"] < 5
            for key in ("width_2d", "diameter_ellipse"):
                if key in inst["m"]:
                    ratios[f"{inst['cls']}:{key}"][scene].append(inst["m"][key] / nominal(inst["cls"], key))

    def stats(v: list[float]) -> dict:
        if not v:
            return {"n": 0}
        return {"n": len(v), "median_ratio": round(statistics.median(v), 3),
                "within_20pct": round(sum(abs(x - 1) <= 0.2 for x in v) / len(v), 3)}

    res = {
        "generated_at": now_iso(), "prompt_id": prompt_id(), "skip": sorted(SKIP),
        "labelled": len(labels), "no_letter": sum(1 for r in labels.values() if r["scene"] is None),
        "calibration_frames": counts([r["id"] for r in calib]),
        "geometry_shots": counts(geo),
        "flood_shots": counts(flood),
        "calibration_check": {
            "under_5m_share": {s: round(c["under_5m"] / c["instances"], 3)
                               for s, c in sorted(near.items()) if c["instances"]},
            "measured_over_nominal": {k: {s: stats(v) for s, v in sorted(d.items())}
                                      for k, d in sorted(ratios.items())},
        },
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DIR / "summary.json.tmp"
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, OUT_DIR / "summary.json")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shard", default="0/1", help="i/n: this worker takes every n-th shot starting at i")
    ap.add_argument("--device", default="free", help="'free' = GPU with the most free memory, or cuda:N")
    ap.add_argument("--limit", type=int, default=None, help="only this many shots (smoke test)")
    ap.add_argument("--all-shots", action="store_true", help="also every non-flood shot in the ledger")
    ap.add_argument("--summary", action="store_true", help="only counts + calibration check, no GPU")
    args = ap.parse_args()
    if not args.summary:
        run(args)
    print(json.dumps(summary(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

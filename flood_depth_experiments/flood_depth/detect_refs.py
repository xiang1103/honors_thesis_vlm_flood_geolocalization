#!/usr/bin/env python3
"""Detect reference objects for condition C5 (Grounding DINO, open-vocabulary).

For each item's key frame, finds objects whose height is roughly known
(levels.REFERENCE_HEIGHTS) and caches the boxes, so the VLM run never waits on
the detector and every prompt/model variant of C5 sees the SAME boxes.

Cache: scrape_data/experiments/flood_depth/refs.jsonl, one line per
(item, frame, detector config), appended and flushed as it goes. Resumable;
changing QUERY / thresholds / MAX_BOXES gives a new `detector` id, i.e. new work.
Boxes are normalised [x0, y0, x1, y1] in 0-1, so they do not depend on the
resolution the detector or the VLM saw.

Weights: /home/liu47/models/grounding-dino-base (IDEA-Research/grounding-dino-base,
Apache-2.0), loaded with local_files_only.

    python3 flood_depth_experiments/flood_depth/detect_refs.py                 # items.jsonl, freest GPU
    python3 flood_depth_experiments/flood_depth/detect_refs.py --device cuda:3 --items other.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PROJECT / "verification"))
sys.path.insert(0, str(PROJECT / "scraping" / "video_scraping"))
sys.path.insert(0, str(PROJECT / "scraping" / "api_based_scraping"))
import verify_video_frames  # noqa: E402,F401  -- sets CUDA_DEVICE_ORDER before torch starts
from verify_video_frames import pick_free_gpu  # noqa: E402
from conditions import frame_image  # noqa: E402
from sample_items import OUT_DIR, read_items  # noqa: E402

MODEL_PATH = "/home/liu47/models/grounding-dino-base"
#: Grounding DINO's text format: lower-case phrases, each ending in a period.
#: Objects with a roughly known height (see levels.REFERENCE_HEIGHTS).
QUERY = ("person. car. truck. bus. bicycle. motorcycle. fire hydrant. stop sign. "
         "parking meter. mailbox. traffic cone. trash can. door. stairs. curb.")
BOX_THRESHOLD = 0.35
TEXT_THRESHOLD = 0.25
MAX_BOXES = 8          # more numbers than this clutter a 640 px frame
SAME_BOX_IOU = 0.7     # same label, this much overlap -> one object
REFS_FILE = OUT_DIR / "refs.jsonl"


def detector_id() -> str:
    cfg = json.dumps([MODEL_PATH, QUERY, BOX_THRESHOLD, TEXT_THRESHOLD, MAX_BOXES, SAME_BOX_IOU])
    return "gdino-base:" + hashlib.sha256(cfg.encode()).hexdigest()[:8]


def read_refs(path: Path = REFS_FILE) -> dict[tuple[str, int, str], dict]:
    out: dict[tuple[str, int, str], dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue                                 # a killed run's last line
            out[(row["item_id"], row["frame"], row["detector"])] = row
    return out


def iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def keep_boxes(raw: list[dict]) -> list[dict]:
    """Highest score first; drop same-label near-duplicates; cap at MAX_BOXES.
    Returned in reading order (top-left first) so the numbers are easy to follow."""
    kept: list[dict] = []
    for b in sorted(raw, key=lambda b: -b["score"]):
        if any(k["label"] == b["label"] and iou(k["box"], b["box"]) >= SAME_BOX_IOU for k in kept):
            continue
        kept.append(b)
        if len(kept) == MAX_BOXES:
            break
    return sorted(kept, key=lambda b: (round(b["box"][1], 1), b["box"][0]))


class Detector:
    def __init__(self, device: str) -> None:
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
        self.torch = torch
        self.device = device
        self.processor = AutoProcessor.from_pretrained(MODEL_PATH, local_files_only=True)
        self.model = AutoModelForZeroShotObjectDetection.from_pretrained(
            MODEL_PATH, local_files_only=True).to(device).eval()

    def __call__(self, image) -> list[dict]:
        inputs = self.processor(images=image, text=QUERY, return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            outputs = self.model(**inputs)
        result = self.processor.post_process_grounded_object_detection(
            outputs, inputs["input_ids"], threshold=BOX_THRESHOLD, text_threshold=TEXT_THRESHOLD,
            target_sizes=[(image.height, image.width)])[0]
        labels = result.get("text_labels", result.get("labels"))
        raw = []
        for box, score, label in zip(result["boxes"].tolist(), result["scores"].tolist(), labels):
            label = str(label).strip()
            if not label:                     # a box whose phrase fell under text_threshold
                continue
            x0, y0, x1, y1 = box
            raw.append({"label": label, "score": round(score, 3),
                        "box": [round(max(0.0, x0 / image.width), 4), round(max(0.0, y0 / image.height), 4),
                                round(min(1.0, x1 / image.width), 4), round(min(1.0, y1 / image.height), 4)]})
        return keep_boxes(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items", type=Path, default=OUT_DIR / "items.jsonl")
    ap.add_argument("--device", default="free", help="'free' = GPU with the most free memory, or cuda:N")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    items = read_items(args.items)
    det_id = detector_id()
    cache = read_refs()
    todo = [it for it in items if (it["item_id"], it["key_frame"], det_id) not in cache]
    if args.limit:
        todo = todo[:args.limit]
    print(f"detect_refs {det_id}: {len(todo)} of {len(items)} items to do", flush=True)
    if not todo:
        return 0
    device = args.device
    if device == "free":
        gpu = pick_free_gpu(8)
        if gpu is None:
            print("no GPU has 8 GiB free; not starting", flush=True)
            return 1
        device = f"cuda:{gpu}"
    detector = Detector(device)
    t0 = time.monotonic()
    REFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with REFS_FILE.open("a", encoding="utf-8") as fh:
        for n, it in enumerate(todo, start=1):
            boxes = detector(frame_image(it, it["key_frame"]))
            fh.write(json.dumps({"item_id": it["item_id"], "frame": it["key_frame"], "detector": det_id,
                                 "query": QUERY, "boxes": boxes,
                                 "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                                ensure_ascii=False) + "\n")
            fh.flush()
            if n % 50 == 0 or n == len(todo):
                print(f"  [{n}/{len(todo)}] {(time.monotonic() - t0) / n:.2f} s/item", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

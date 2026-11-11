#!/usr/bin/env python3
"""Routes B and V, VLM part: what each object standing in the water is, and how tall.

geometry_depth.py stores, per frame, every SAM 3 object that can be a
reference and has water just below it (`objects_b`: box, visible height).
This script asks the local VLM (Qwen, thinking off) two SEPARATE questions
about each one, on the frame upscaled 3x with the object in a red box:

  free  (route V)  what is it, is its top visible, how high is its top above
                   the ground in cm -- the VLM is NOT shown the size table;
  table (route B)  which entry of reference_sizes.json is it, from the WHOLE
                   table plus "none / cannot tell" -- labels only, never
                   heights, and without the SAM 3 phrase (it anchored v1).

Neither question sees the other's answer or any height, so route V's sizes
can be compared with the table's for the entries the VLM picked
(geometry_depth.vlm_size_check). Depths are computed later from the stored
answers (geometry_depth.ref_objects), so editing the table needs no rerun.

Output (experiment only):
  scrape_data/experiments/flood_depth/geometry/
    object_labels.jsonl          one line per object, appended + flushed; resume key
                                 (id, frame, k, geometry config, prompt id)
    crops/<video>_shotNNN_<j>_<k>.jpg   the object, cropped and enlarged, for the report

    python3 flood_depth_experiments/depth_detection/object_sizes.py            # freest GPU
    python3 flood_depth_experiments/depth_detection/object_sizes.py --limit 5  # smoke test
"""
from __future__ import annotations

import os

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import argparse
import hashlib
import json
import re
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from geometry_depth import (OUT_DIR, PROJECT, REF_TABLE, choose_shots, config_id, read_frames,  # noqa: E402
                            scene_kept)

PROMPT_FREE = (
    "Look at the object inside the red box in this photo.\n"
    "Answer in exactly three lines:\n"
    "object: <what it is, in a few words>\n"
    "top_visible: <yes or no -- yes only if the top of the object is inside the photo and not hidden "
    "behind something>\n"
    "top_height_cm: <how high the top of this object is above the ground it stands on, in centimetres, "
    "one number -- its normal size, as if there were no water>")
PROMPT_TABLE = (
    "Look at the object inside the red box in this photo. Which of these is it? "
    "Answer with the letter(s) only.\n{choices}\n"
    "ZZ) none of these, or cannot tell")
# v1 (objsize:5e36938d, 2026-11-05) named the SAM 3 phrase and offered only that phrase's
# entries; the pick followed the detector (9 of 15 'bollard' picks were named something
# else by the free question). v2 offers the whole table and does not name the phrase.
MAX_NEW_TOKENS = 60
UPSCALE = 3


def prompt_id() -> str:
    labels = [(e["id"], e["label"], e["phrases"]) for e in REF_TABLE["entries"]]
    return "objsize:" + hashlib.sha256(json.dumps([PROMPT_FREE, PROMPT_TABLE, UPSCALE, labels])
                                       .encode()).hexdigest()[:8]


def letters(n: int) -> list[str]:
    """A..Y, then AA, AB, ... (Z / ZZ is 'none')."""
    one = list(string.ascii_uppercase[:25])
    return (one + [a + b for a in "AB" for b in string.ascii_uppercase[:25]])[:n]


def parse_free(text: str) -> dict:
    out = {"vlm_object": None, "top_visible": None, "vlm_top_cm": None}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "object":
            out["vlm_object"] = val or None
        elif key == "top_visible":
            out["top_visible"] = True if val.lower().startswith("yes") else False if val.lower().startswith("no") \
                else None
        elif key == "top_height_cm":
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", val)][:2]   # "60-70" -> 65
            out["vlm_top_cm"] = round(sum(nums) / len(nums), 1) if nums else None   # sanity: geometry_depth.vlm_size
    return out


def parse_letter(text: str, n: int) -> str | None:
    ok = set(letters(n)) | {"Z", "ZZ"}
    found = [x for x in re.findall(r"\b([A-Z]{1,2})\b", text.upper()) if x in ok]
    return found[-1] if found else None


def read_done() -> set[tuple]:
    path = OUT_DIR / "object_labels.jsonl"
    done = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            done.add((r["id"], r["frame"], r["k"], r["config"], r["prompt_id"]))
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="free")
    ap.add_argument("--limit", type=int, default=None, help="only this many objects (smoke test)")
    ap.add_argument("--scene", default="real", choices=["real", "all"])
    args = ap.parse_args()

    cfg, pid = config_id(), prompt_id()
    shots, _ = scene_kept(choose_shots(0, 0), args.scene)
    frames = read_frames(cfg)
    done = read_done()
    todo = []
    for shot in shots:
        for j, rel in enumerate(shot["frames"]):
            row = frames.get((shot["id"], j))
            for o in (row or {}).get("objects_b", []):
                if (shot["id"], j, o["k"], cfg, pid) not in done:
                    todo.append((shot, j, rel, o))
    if args.limit:
        todo = todo[:args.limit]
    print(f"object_sizes {pid} on {cfg}: {len(todo)} objects to do", flush=True)
    if not todo:
        return 0

    from verify_video_frames import pick_free_gpu  # sets CUDA_DEVICE_ORDER before torch starts
    device = args.device
    if device == "free":
        gpu = pick_free_gpu(58)
        if gpu is None:
            raise SystemExit("no GPU has 58 GiB free; not starting")
        device = f"cuda:{gpu}"
    import torch
    from PIL import Image, ImageDraw
    sys.path.insert(0, str(PROJECT))
    from local_vlm import DEFAULT_MODEL_PATH
    from local_vlm.backend import LocalVLM
    torch.set_num_threads(4)
    vlm = LocalVLM(DEFAULT_MODEL_PATH, device_map=device, max_new_tokens=MAX_NEW_TOKENS)
    t0 = time.monotonic()
    vlm.load()
    print(f"VLM loaded on {device} in {time.monotonic() - t0:.0f} s", flush=True)

    (OUT_DIR / "crops").mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "object_labels.jsonl").open("a", encoding="utf-8") as fh:
        for n, (shot, j, rel, o) in enumerate(todo, 1):
            vid, num = shot["id"].split(":")
            row = {"id": shot["id"], "frame": j, "k": o["k"], "config": cfg, "prompt_id": pid,
                   "model": vlm.name, "phrase": o["phrase"]}
            try:
                with Image.open(PROJECT / rel) as im:
                    frame = im.convert("RGB")
                x0, y0, x1, y1 = o["box"]
                big = frame.resize((frame.width * UPSCALE, frame.height * UPSCALE), Image.BICUBIC)
                ImageDraw.Draw(big).rectangle([x0 * UPSCALE - 3, y0 * UPSCALE - 3, x1 * UPSCALE + 3,
                                               y1 * UPSCALE + 3], outline=(255, 0, 0), width=3)
                pad = max(4, (x1 - x0) // 3, (y1 - y0) // 3)
                crop = frame.crop((max(0, x0 - pad), max(0, y0 - pad), min(frame.width, x1 + pad + 1),
                                   min(frame.height, y1 + pad + 1)))
                crop.resize((crop.width * 4, crop.height * 4), Image.BICUBIC).save(
                    OUT_DIR / "crops" / f"{vid}_{num}_{j}_{o['k']}.jpg", quality=90)

                raw_free = vlm.generate_parts([big, PROMPT_FREE])
                row.update(parse_free(raw_free), raw_free=raw_free)
                cands = REF_TABLE["entries"]
                ls = letters(len(cands))
                choices = "\n".join(f"{l}) {e['label']}" for l, e in zip(ls, cands))
                raw_table = vlm.generate_parts([big, PROMPT_TABLE.format(choices=choices)])
                letter = parse_letter(raw_table, len(cands))
                row.update(letter=letter, raw_table=raw_table,
                           entry=cands[ls.index(letter)]["id"] if letter in ls else None)
            except Exception as exc:  # noqa: BLE001 -- one bad object must not stop the run
                row["error"] = f"{type(exc).__name__}: {exc}"
            row["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            if n % 25 == 0 or n == len(todo) or n <= 3:
                print(f"  [{n}/{len(todo)}] {shot['id']} f{j} #{o['k']} {o['phrase']}: "
                      f"{row.get('vlm_object')!r} top {row.get('vlm_top_cm')} cm visible={row.get('top_visible')}"
                      f" | table {row.get('entry')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

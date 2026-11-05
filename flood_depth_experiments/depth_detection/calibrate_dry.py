#!/usr/bin/env python3
"""Dry-scene calibration: does segmentation + MoGe-2 measure known sizes right?

Route B of docs/water_depth_segmentation.md reads depth off objects of known
size, which assumes (1) the segmenter finds the object correctly and (2) the
measured size is right. Both can be TESTED without depth labels on dry street
scenes, where every reference object is fully visible: measure it and compare
with its standard size. The spread of measured / nominal is the error the
method would carry into a flood frame; the share of masks failing the shape
checks is the extraction failure rate.

Frames: key frames (j = 1) of NON-flood shots from the shot scan (flood:
false, not ai_video), one random shot per video, fixed seed. Frames where
SAM 3 finds flood water are skipped.

References and their nominal sizes (US, where most of the data is from):

  wheel         diameter 0.66 m (passenger cars 0.58-0.80); hub 0.33 m above ground
  fire hydrant  height 0.75 m (0.60-0.95)
  stop sign     face width 0.76 m (30 in standard; 24 / 36 in exist)
  license plate width 0.305 m, height 0.152 m (US / Canada)
  curb          height 0.15 m (0.10-0.20)

Each size is measured two ways:
  2d  mask extent in pixels x (distance / focal length in pixels) -- the
      distance is the median over the mask's interior, so depth bleeding at
      the object's edges does not matter;
  3d  extent of the object's own 3D points (2nd-98th percentile) along
      gravity (heights) or the dominant horizontal axis (widths).
The wheel also gets an ellipse fit (major axis ~ diameter whatever the yaw)
and its hub height above the fitted road plane.

Output: scrape_data/experiments/flood_depth/geometry/calibration/
  frames.jsonl   one line per frame: instances with all measurements, resumable
  crops/<class>/ each instance, mask outlined, numbers written on it
  summary.json   per class and method: median measured / nominal, IQR, share
                 within 10 / 20 %, by distance and by pixel size; shape-check pass rates
  sheet_<class>.jpg  crops of that class sorted by measured / nominal, for eyeballing

    python3 flood_depth_experiments/depth_detection/calibrate_dry.py --limit 20     # smoke test
    python3 flood_depth_experiments/depth_detection/calibrate_dry.py --n 1000       # the run
    python3 flood_depth_experiments/depth_detection/calibrate_dry.py --summary      # re-score, no GPU
"""
from __future__ import annotations

import os

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import argparse
import json
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from geometry_depth import (MOGE_ID, OUT_DIR as GEO_DIR, PROJECT, _erode, estimate_gravity,  # noqa: E402
                            fit_ground_plane)
from sample_items import FRAMES_DIR  # noqa: E402
from scan_shots import LEDGER_DIR, frame_path, read_ledger  # noqa: E402
from scene_filter import read_scenes, SKIP as SCENE_SKIP  # noqa: E402

OUT_DIR = GEO_DIR / "calibration"
KEY_FRAME = 1

#: class -> (SAM 3 phrase, {measure: nominal metres})
REFERENCES = {
    "wheel": ("wheel", {"diameter": 0.66, "hub_height": 0.33}),
    "fire hydrant": ("fire hydrant", {"height": 0.75}),
    "stop sign": ("stop sign", {"width": 0.76}),
    "license plate": ("license plate", {"width": 0.305, "height": 0.152}),
    "curb": ("curb", {"height": 0.15}),
}
CONTEXT = ["car", "road", "sidewalk", "flood water"]
MIN_POINTS = 6
DIST_BINS = [(0, 5), (5, 10), (10, 20), (20, 1e9)]
PX_BINS = [(0, 10), (10, 20), (20, 40), (40, 1e9)]


# --------------------------------------------------------------------------
# measuring one instance
# --------------------------------------------------------------------------

def _extent(v: np.ndarray) -> float:
    lo, hi = np.percentile(v, [2, 98])
    return float(hi - lo)


def _ellipse(mask: np.ndarray):
    import cv2
    cs, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea)
    if len(c) < 5:
        return None
    (cx, cy), (a1, a2), ang = cv2.fitEllipse(c)
    major, minor = max(a1, a2), min(a1, a2)
    fill = float(mask.sum()) / (np.pi * major * minor / 4 + 1e-6)
    return {"cx": cx, "cy": cy, "major_px": major, "minor_px": minor, "fill": fill}


def measure_instance(cls: str, mask: np.ndarray, points: np.ndarray, valid: np.ndarray,
                     fx: float, fy: float, g: np.ndarray, plane, car_mask: np.ndarray) -> dict | None:
    inner = _erode(mask) & valid
    if inner.sum() < MIN_POINTS:
        inner = mask & valid
    if inner.sum() < MIN_POINTS:
        return None
    ys, xs = np.nonzero(mask)
    w_px, h_px = int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)
    z = float(np.median(points[inner][:, 2]))
    pts = points[mask & valid]
    heights = pts @ g
    horiz = pts - heights[:, None] * g
    horiz = horiz - horiz.mean(0)
    axis = np.linalg.svd(horiz, full_matrices=False)[2][0]
    r = {"px_w": w_px, "px_h": h_px, "px": round(float(np.sqrt(mask.sum())), 1),
         "dist_m": round(float(np.median(np.linalg.norm(points[inner], axis=1))), 2),
         "m": {}}
    width_2d, height_2d = w_px * z / fx, h_px * z / fy
    width_3d, height_3d = _extent(horiz @ axis), _extent(heights)
    m = r["m"]
    if cls == "wheel":
        e = _ellipse(mask)
        if e:
            r.update(fill=round(e["fill"], 2), aspect=round(e["minor_px"] / e["major_px"], 2))
            m["diameter_ellipse"] = e["major_px"] * z / fy
            if plane is not None:
                cy, cx = int(round(e["cy"])), int(round(e["cx"]))
                if 0 <= cy < valid.shape[0] and 0 <= cx < valid.shape[1] and valid[cy, cx]:
                    n, d = plane[0], plane[1]
                    m["hub_height"] = float((points[cy, cx] @ n - d) / (n @ g))
        m["diameter_2d"], m["diameter_3d"] = height_2d, height_3d
        r["on_car"] = round(float((mask & car_mask).sum() / mask.sum()), 2)
    elif cls == "fire hydrant":
        m["height_2d"], m["height_3d"] = height_2d, height_3d
    elif cls == "stop sign":
        m["width_2d"], m["width_3d"] = width_2d, width_3d
    elif cls == "license plate":
        m["width_2d"], m["width_3d"] = width_2d, width_3d
        m["height_2d"], m["height_3d"] = height_2d, height_3d
    elif cls == "curb":
        m["height_2d"], m["height_3d"] = height_2d, height_3d
    r["m"] = {k: round(v, 3) for k, v in m.items()}
    return r


def shape_ok(cls: str, inst: dict) -> str | None:
    """Why this mask is not trusted as the object it claims to be; None = ok."""
    if inst["px"] < 4:
        return "too_small"
    if cls == "wheel":
        if inst.get("fill") is None:
            return "no_ellipse"
        if inst["fill"] < 0.7:
            return "not_elliptical"
        if inst["aspect"] < 0.3:
            return "too_thin"
        if inst["on_car"] < 0.5:
            return "not_on_car"
    if cls == "stop sign" and not 0.6 <= inst["px_w"] / max(inst["px_h"], 1) <= 1.6:
        return "not_square"
    if cls == "license plate" and not 1.2 <= inst["px_w"] / max(inst["px_h"], 1) <= 3.5:
        return "wrong_aspect"
    if cls == "fire hydrant" and inst["px_h"] < inst["px_w"]:
        return "wider_than_tall"
    return None


def nominal(cls: str, key: str) -> float:
    return REFERENCES[cls][1][key.rsplit("_", 1)[0] if key != "hub_height" else key]


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def choose_frames(n: int, seed: int) -> list[dict]:
    """One random dry shot per video. Not scene-filtered here: the frame list
    must stay the same whatever the labels say; summary() leaves the flagged
    ones out."""
    rng = random.Random(seed)
    ledger = read_ledger(LEDGER_DIR)
    pool = []
    for vid in sorted(ledger):
        row = ledger[vid]
        if "shots" not in row or row.get("ai_video"):
            continue
        dry = [k for k, s in enumerate(row["shots"])
               if not s.get("flood") and len(s["frames"]) > KEY_FRAME
               and frame_path(FRAMES_DIR, vid, k, KEY_FRAME).exists()]
        if dry:
            k = rng.choice(dry)
            pool.append({"id": f"{vid}:shot{k + 1:03d}",
                         "path": str(frame_path(FRAMES_DIR, vid, k, KEY_FRAME).relative_to(PROJECT))})
    rng.shuffle(pool)
    return pool[:n] if n else pool


def read_done() -> dict[str, dict]:
    path = OUT_DIR / "frames.jsonl"
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[row["id"]] = row
    return out


def save_crop(rgb: np.ndarray, mask: np.ndarray, cls: str, name: str, label: str) -> None:
    import cv2
    ys, xs = np.nonzero(mask)
    pad = max(6, int(0.6 * max(np.ptp(xs), np.ptp(ys))))
    y0, y1 = max(0, ys.min() - pad), min(rgb.shape[0], ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(rgb.shape[1], xs.max() + pad + 1)
    crop = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2BGR).copy()
    cs, _ = cv2.findContours(mask[y0:y1, x0:x1].astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    scale = max(1, int(np.ceil(96 / max(crop.shape[:2]))))
    crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    cv2.drawContours(crop, [c * scale for c in cs], -1, (0, 0, 255), 1)
    crop = cv2.copyMakeBorder(crop, 14, 0, 0, max(0, 110 - crop.shape[1]), cv2.BORDER_CONSTANT)
    cv2.putText(crop, label, (2, 11), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1, cv2.LINE_AA)
    d = OUT_DIR / "crops" / cls.replace(" ", "_")
    d.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(d / f"{name}.jpg"), crop)


def run(args) -> None:
    from verify_video_frames import pick_free_gpu  # sets CUDA_DEVICE_ORDER before torch starts
    import torch
    from PIL import Image
    from moge.model.v2 import MoGeModel
    from segment import Segmenter

    frames = choose_frames(args.n, args.seed)
    done = read_done()
    todo = [f for f in frames if f["id"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"calibrate_dry: {len(todo)} of {len(frames)} frames to do", flush=True)
    if not todo:
        return
    device = args.device
    if device == "free":
        gpu = pick_free_gpu(20)
        if gpu is None:
            raise SystemExit("no GPU with 20 GiB free")
        device = f"cuda:{gpu}"
    moge = MoGeModel.from_pretrained(MOGE_ID).to(device).eval()
    seg = Segmenter("sam3", device)
    phrases = [p for p, _ in REFERENCES.values()] + CONTEXT
    by_phrase = {p: c for c, (p, _) in REFERENCES.items()}
    rng = np.random.default_rng(0)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with (OUT_DIR / "frames.jsonl").open("a", encoding="utf-8") as fh:
        for i, fr in enumerate(todo, 1):
            row = {"id": fr["id"], "path": fr["path"]}
            try:
                rgb = np.asarray(Image.open(PROJECT / fr["path"]).convert("RGB"))
                found = seg.instances(Image.fromarray(rgb), phrases)
                h, w = rgb.shape[:2]
                ctx = {p: np.logical_or.reduce([m for q, _, m in found if q == p])
                       if any(q == p for q, _, _ in found) else np.zeros((h, w), bool) for p in CONTEXT}
                refs = [(q, s, m) for q, s, m in found if q in by_phrase and m.any()]
                row.update(size=[w, h], water_frac=round(float(ctx["flood water"].mean()), 3),
                           counts=dict(Counter(q for q, _, _ in found)))
                if row["water_frac"] >= 0.01:
                    row["skipped"] = "water"
                elif refs:
                    with torch.inference_mode():
                        out = moge.infer(torch.tensor(rgb / 255.0, dtype=torch.float32, device=device)
                                         .permute(2, 0, 1))
                    points = np.nan_to_num(out["points"].float().cpu().numpy())
                    normals = np.nan_to_num(out["normal"].float().cpu().numpy())
                    valid = out["mask"].cpu().numpy().astype(bool) & (np.abs(points).sum(-1) > 0)
                    K = out["intrinsics"].float().cpu().numpy()
                    fx, fy = float(K[0, 0]) * w, float(K[1, 1]) * h
                    ground = _erode(ctx["road"] | ctx["sidewalk"]) & ~ctx["car"] & valid
                    plane = fit_ground_plane(points[ground], normals[ground], rng) if ground.sum() else None
                    g, g_basis = estimate_gravity(normals[valid], plane[0] if plane else None)
                    row.update(focal_px=round(fx, 1), gravity=g_basis,
                               plane_resid_cm=round(plane[3], 2) if plane else None, instances=[])
                    for k, (q, score, mask) in enumerate(refs):
                        cls = by_phrase[q]
                        inst = measure_instance(cls, mask, points, valid, fx, fy, g, plane, ctx["car"])
                        if inst is None:
                            continue
                        inst.update(cls=cls, score=score, reject=shape_ok(cls, inst),
                                    crop=f"{fr['id'].replace(':', '_')}_{k}")
                        row["instances"].append(inst)
                        main = next(iter(inst["m"].items()), None)
                        label = (f"{main[0].split('_')[0][:4]} {main[1]:.2f}m d{inst['dist_m']:.0f}"
                                 if main else "") + (f" X:{inst['reject']}" if inst["reject"] else "")
                        save_crop(rgb, mask, cls, inst["crop"], label)
            except Exception as e:  # noqa: BLE001 -- one bad frame must not stop the run
                row["error"] = f"{type(e).__name__}: {e}"
            row["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            if i % 50 == 0 or i == len(todo):
                rate = (time.time() - t0) / i
                print(f"[{i}/{len(todo)}] {rate:.2f} s/frame, ~{rate * (len(todo) - i) / 60:.0f} min left",
                      flush=True)


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

def _stats(ratios: list[float]) -> dict:
    if not ratios:
        return {"n": 0}
    q1, q3 = np.percentile(ratios, [25, 75])
    a = np.abs(np.array(ratios) - 1)
    return {"n": len(ratios), "median_ratio": round(statistics.median(ratios), 3),
            "iqr_ratio": round(float(q3 - q1), 3),
            "within_10pct": round(float((a <= 0.10).mean()), 3),
            "within_20pct": round(float((a <= 0.20).mean()), 3)}


def _bin(v: float, bins) -> str:
    for lo, hi in bins:
        if lo <= v < hi:
            return f"{lo}-{hi if hi < 1e9 else 'inf'}"
    return "?"


def summary(scene: str = "real") -> dict:
    rows = list(read_done().values())
    scenes = read_scenes()
    scene_counts = Counter(scenes.get(r["id"], "unlabelled") or "no_letter" for r in rows)
    if scene == "real":
        rows = [r for r in rows if scenes.get(r["id"]) not in SCENE_SKIP]
    detected, rejected = Counter(), defaultdict(Counter)
    ratios: dict[str, list[float]] = defaultdict(list)
    by_dist: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_px: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    sheet: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for row in rows:
        for k, inst in enumerate(row.get("instances", [])):
            cls = inst["cls"]
            detected[cls] += 1
            rejected[cls][inst["reject"] or "kept"] += 1
            if inst["reject"]:
                continue
            for key, val in inst["m"].items():
                r = val / nominal(cls, key)
                name = f"{cls}:{key}"
                ratios[name].append(r)
                by_dist[name][_bin(inst["dist_m"], DIST_BINS)].append(r)
                by_px[name][_bin(inst["px"], PX_BINS)].append(r)
            first = next(iter(inst["m"].items()), None)
            if first:
                sheet[cls].append((first[1] / nominal(cls, first[0]),
                                   inst.get("crop", f"{row['id'].replace(':', '_')}_{k}")))
    res = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "moge": MOGE_ID, "segmenter": "sam3",
        "scene_filter": scene, "frames_by_scene": dict(scene_counts.most_common()),
        "frames": len(rows),
        "frames_skipped_water": sum(1 for r in rows if r.get("skipped") == "water"),
        "frames_with_reference": sum(1 for r in rows if r.get("instances")),
        "frames_with_error": sum(1 for r in rows if r.get("error")),
        "nominal_m": {c: v for c, (_, v) in REFERENCES.items()},
        "detected": dict(detected),
        "shape_check": {c: dict(v.most_common()) for c, v in rejected.items()},
        "measured_over_nominal": {k: _stats(v) for k, v in sorted(ratios.items())},
        "by_distance_m": {k: {b: _stats(v) for b, v in sorted(d.items())} for k, d in sorted(by_dist.items())},
        "by_pixel_size": {k: {b: _stats(v) for b, v in sorted(d.items())} for k, d in sorted(by_px.items())},
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DIR / "summary.json.tmp"
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, OUT_DIR / "summary.json")
    make_sheets(sheet)
    return res


def make_sheets(sheet: dict[str, list[tuple[float, str]]], per_sheet: int = 80) -> None:
    """Kept crops of each class, sorted by measured / nominal (evenly sampled)."""
    import cv2
    for cls, items in sheet.items():
        items.sort()
        if len(items) > per_sheet:
            items = [items[round(i * (len(items) - 1) / (per_sheet - 1))] for i in range(per_sheet)]
        tiles = []
        for _, name in items:
            im = cv2.imread(str(OUT_DIR / "crops" / cls.replace(" ", "_") / f"{name}.jpg"))
            if im is not None:
                tiles.append(cv2.resize(im, (110, int(im.shape[0] * 110 / im.shape[1]))))
        if not tiles:
            continue
        cols = 10
        hmax = max(t.shape[0] for t in tiles)
        tiles = [cv2.copyMakeBorder(t, 0, hmax - t.shape[0] + 2, 0, 2, cv2.BORDER_CONSTANT) for t in tiles]
        blank = np.zeros_like(tiles[0])
        tiles += [blank] * (-len(tiles) % cols)
        grid = np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)])
        cv2.imwrite(str(OUT_DIR / f"sheet_{cls.replace(' ', '_')}.jpg"), grid)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=1000, help="frames (one dry shot per video, default %(default)s)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="free", help="'free' = GPU with the most free memory, or cuda:N")
    ap.add_argument("--limit", type=int, default=None, help="only this many frames (smoke test)")
    ap.add_argument("--summary", action="store_true", help="only re-score stored frames, no GPU")
    ap.add_argument("--scene", default="real", choices=["real", "all"],
                    help="real = score without frames the scene filter flags (default); all = every frame")
    args = ap.parse_args()
    if not args.summary:
        run(args)
    res = summary(args.scene)
    print(json.dumps({k: v for k, v in res.items() if k not in ("by_distance_m", "by_pixel_size")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

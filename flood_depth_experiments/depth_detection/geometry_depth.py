#!/usr/bin/env python3
"""Water depth from geometry + segmentation -- the 50-shot pilot.

Design and reasoning: docs/water_depth_segmentation.md (read it first). Per
frame:

  1. MoGe-2 (Ruicheng/moge-2-vitl-normal): a metric 3D point and a surface
     normal per pixel, camera at the origin (OpenCV axes: x right, y down).
  2. segment.py: water / dry ground / objects / wheels masks.
  3. Ground plane: RANSAC on dry-ground points whose normals face up.
  4. Gravity: the direction perpendicular to vertical surfaces (walls, car
     sides); falls back to the plane's normal, then to the camera's -y.
  5. Waterline, found in 2D and looked up in 3D, never from water pixels:
       contact = object pixels (masks eroded 1 px, i.e. sampled just off the
                 edge) with water within 3 px BELOW them, on a vertical surface;
       shore   = dry-ground pixels (eroded) within 3 px of water.
     Water is level, so every waterline point should sit at one height along
     gravity: the water level is their robust median, the IQR is its spread.
  6. Route A (plane): depth = water level minus the extended ground plane,
     at the contacts (evidence) and over the water region (rays through the
     water pixels meet the level water surface; the plane is below).
  7. Route B (wheels): a wheel touching the water, top of the tyre minus a
     typical tyre diameter = the ground under it; depth = level minus that.

Every number is stored, abstaining or not; `abstain_reason()` decides from
the stored numbers, so thresholds can be changed and `--summary` re-run
without a GPU.

Shots: random eligible flood shots (sample_items.eligible: flood, not
ai_video, 3 frames on disk), one per video, fixed seed -- an honest coverage
number, not hand-picked scenes. The list is written once (shots.jsonl) and
reused, so reruns and resumes measure the same shots.

Output (experiment only, nothing in data/):
  scrape_data/experiments/flood_depth/geometry/
    shots.jsonl     the chosen shots
    frames.jsonl    one line per (shot, frame, config), appended + flushed; resume key
    overlays/<video>_shotNNN.jpg   the 3 frames side by side, masks + waterline + numbers
    summary.json    coverage, within-shot spread vs the VLM's C1, agreement with C4/C5

    python3 flood_depth_experiments/depth_detection/geometry_depth.py                # 50 shots, freest GPU
    python3 flood_depth_experiments/depth_detection/geometry_depth.py --limit 2      # smoke test
    python3 flood_depth_experiments/depth_detection/geometry_depth.py --summary      # re-score, no GPU
    python3 flood_depth_experiments/depth_detection/geometry_depth.py --segmenter sam3   # once access is granted

Scene filter: shots that scene_split/scene_filter.py labels as studio, screen,
picture-in-picture, through-glass or graphic are left out of the run and the
summary (MoGe's scale breaks on them). Their stored rows are kept; --scene all
puts them back.
"""
from __future__ import annotations

import os

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import argparse
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PROJECT / "flood_depth_experiments" / "flood_depth"))
sys.path.insert(0, str(PROJECT / "flood_depth_experiments" / "scene_split"))
sys.path.insert(0, str(PROJECT / "verification"))
sys.path.insert(0, str(PROJECT / "scraping" / "video_scraping"))
sys.path.insert(0, str(PROJECT / "scraping" / "api_based_scraping"))

from levels import midpoint_cm  # noqa: E402
from sample_items import FRAMES_DIR, FRAMES_PER_SHOT, OUT_DIR as DEPTH_DIR, eligible, select  # noqa: E402
from scan_shots import LEDGER_DIR, read_ledger  # noqa: E402
from scene_filter import skip_ids  # noqa: E402
from segment import segmenter_id  # noqa: E402

OUT_DIR = DEPTH_DIR / "geometry"
VLM_DIR = DEPTH_DIR / "full_5cm"
MOGE_ID = "Ruicheng/moge-2-vitl-normal"

UP0 = np.array([0.0, -1.0, 0.0])      # camera "up" (OpenCV y points down)
GROUND_NORMAL_MAX_DEG = 40            # ground pixels: normal within this of UP0
PLANE_MAX_TILT_DEG = 40               # a fitted plane tilted more than this is not ground
SLOPE_MAX_DEG = 15                    # walls-gravity vs plane normal: more = distrust walls
RANSAC_ITERS = 300
WATERLINE_PX = 3                      # how close to the water a waterline pixel must be
VERTICAL_MAX_COS = 0.5                # contact surface: |normal . gravity| below this
TYRE_DIAMETER_M = 0.66                # typical passenger-car tyre
MAX_RANGE_M = 60.0                    # ignore points farther than this

# Abstain rules (applied to stored numbers; see abstain_reason)
MIN_GROUND_PX = 300
MAX_PLANE_RESID_CM = 5.0
MIN_CONTACT_PX = 8
MIN_SHORE_PX = 15
MAX_LEVEL_IQR_CM = 20.0
MIN_DEPTH_CM = -5.0
MAX_DEPTH_CM = 300.0


def config_id(segmenter: str) -> str:
    cfg = json.dumps([MOGE_ID, segmenter_id(segmenter), GROUND_NORMAL_MAX_DEG, PLANE_MAX_TILT_DEG,
                      SLOPE_MAX_DEG, RANSAC_ITERS, WATERLINE_PX, VERTICAL_MAX_COS, TYRE_DIAMETER_M,
                      MAX_RANGE_M])
    return "geo:" + hashlib.sha256(cfg.encode()).hexdigest()[:8]


# --------------------------------------------------------------------------
# geometry, pure numpy
# --------------------------------------------------------------------------

def _shift_down_any(mask: np.ndarray, k: int) -> np.ndarray:
    """True where `mask` holds at any of the k pixels directly BELOW."""
    out = np.zeros_like(mask)
    for i in range(1, k + 1):
        out[:-i] |= mask[i:]
    return out


def _erode(mask: np.ndarray, px: int = 1) -> np.ndarray:
    import cv2
    return cv2.erode(mask.astype(np.uint8), np.ones((2 * px + 1, 2 * px + 1), np.uint8)).astype(bool)


def _dilate(mask: np.ndarray, px: int) -> np.ndarray:
    import cv2
    return cv2.dilate(mask.astype(np.uint8), np.ones((2 * px + 1, 2 * px + 1), np.uint8)).astype(bool)


def fit_ground_plane(pts: np.ndarray, nrm: np.ndarray, rng: np.random.Generator):
    """RANSAC plane through dry-ground points -> (normal (up), d, inlier frac, residual cm)
    with normal . x = d on the plane, or None."""
    up_ok = np.abs(nrm @ UP0) > np.cos(np.radians(GROUND_NORMAL_MAX_DEG))
    pts = pts[up_ok]
    if len(pts) < MIN_GROUND_PX:
        return None
    if len(pts) > 6000:
        pts = pts[rng.choice(len(pts), 6000, replace=False)]
    thr = np.maximum(0.02, 0.015 * pts[:, 2])          # noise grows with distance
    best, best_n = None, 0
    cos_tilt = np.cos(np.radians(PLANE_MAX_TILT_DEG))
    for _ in range(RANSAC_ITERS):
        a, b, c = pts[rng.choice(len(pts), 3, replace=False)]
        n = np.cross(b - a, c - a)
        norm = np.linalg.norm(n)
        if norm < 1e-9:
            continue
        n /= norm
        if n @ UP0 < 0:
            n = -n
        if n @ UP0 < cos_tilt:
            continue
        inl = np.abs(pts @ n - n @ a) < thr
        if inl.sum() > best_n:
            best, best_n = inl, int(inl.sum())
    if best is None or best_n < MIN_GROUND_PX // 2:
        return None
    q = pts[best]
    centre = q.mean(0)
    n = np.linalg.svd(q - centre, full_matrices=False)[2][-1]
    if n @ UP0 < 0:
        n = -n
    d = float(n @ centre)
    resid = float(np.median(np.abs(q @ n - d))) * 100
    return n, d, best_n / len(pts), resid


def estimate_gravity(normals: np.ndarray, plane_n: np.ndarray | None) -> tuple[np.ndarray, str]:
    """Gravity 'up' from vertical surfaces (their normals are horizontal, so up
    is the direction they all lack); else the plane's normal; else the camera."""
    ref = plane_n if plane_n is not None else UP0
    vert = normals[np.abs(normals @ ref) < 0.34]          # within ~20 deg of vertical
    if len(vert) >= 500:
        g = np.linalg.eigh(vert.T @ vert)[1][:, 0]       # smallest eigenvalue
        if g @ ref < 0:
            g = -g
        if np.degrees(np.arccos(np.clip(g @ ref, -1, 1))) <= SLOPE_MAX_DEG:
            return g, "walls"
    return (plane_n, "plane") if plane_n is not None else (UP0.copy(), "camera")


def _robust(values: np.ndarray) -> tuple[float, float, np.ndarray]:
    """Median and IQR after dropping points beyond 3 MAD; also the kept mask."""
    med = np.median(values)
    mad = np.median(np.abs(values - med)) * 1.4826 + 1e-3
    keep = np.abs(values - med) <= 3 * mad
    v = values[keep]
    q1, q3 = np.percentile(v, [25, 75])
    return float(np.median(v)), float(q3 - q1), keep


def _pct(v: np.ndarray, q: float) -> float | None:
    return round(float(np.percentile(v, q)) * 100, 1) if len(v) else None


def measure(points: np.ndarray, normals: np.ndarray, valid: np.ndarray, masks, rng) -> tuple[dict, dict]:
    """One frame -> (stored numbers, drawing info). Heights are along gravity, metres."""
    h, w = valid.shape
    valid = valid & (np.linalg.norm(points, axis=-1) < MAX_RANGE_M)
    water = masks.water & valid
    ground = _erode(masks.ground) & valid
    objects = _erode(masks.objects) & valid
    s: dict = {"water_frac": round(float(masks.water.mean()), 3),
               "ground_frac": round(float(masks.ground.mean()), 3),
               "object_frac": round(float(masks.objects.mean()), 3),
               "valid_frac": round(float(valid.mean()), 3),
               "ground_px": int(ground.sum())}
    draw = {"contact": np.zeros((h, w), bool), "shore": np.zeros((h, w), bool)}

    plane = fit_ground_plane(points[ground], normals[ground], rng) if ground.sum() else None
    plane_n = plane_d = None
    if plane:
        plane_n, plane_d, frac, resid = plane
        s.update(plane_inliers=round(frac, 3), plane_resid_cm=round(resid, 2),
                 plane_tilt_deg=round(float(np.degrees(np.arccos(np.clip(plane_n @ UP0, -1, 1)))), 1))
    g, g_basis = estimate_gravity(normals[valid & ~masks.water], plane_n)
    s["gravity"] = g_basis
    if plane_n is not None:
        s["slope_deg"] = round(float(np.degrees(np.arccos(np.clip(g @ plane_n, -1, 1)))), 1)

    vertical = np.abs(normals @ g) < VERTICAL_MAX_COS
    contact = objects & vertical & _shift_down_any(water, WATERLINE_PX)
    shore = ground & _dilate(water, WATERLINE_PX)
    draw.update(contact=contact, shore=shore)
    s.update(n_contact=int(contact.sum()), n_shore=int(shore.sum()))
    height = points @ g                                   # metres along gravity
    hc, hs = height[contact], height[shore]
    if len(hc):
        s["contact_level_m"] = round(float(np.median(hc)), 3)
    if len(hs):
        s["shore_level_m"] = round(float(np.median(hs)), 3)
    if len(hc) and len(hs):
        # water is level: contacts and shore should sit at the same height
        s["level_gap_cm"] = round((float(np.median(hc)) - float(np.median(hs))) * 100, 1)
    line = np.concatenate([hc, hs])
    if len(line) == 0:
        return s, draw
    level, iqr, _ = _robust(line)
    s.update(level_m=round(level, 3), level_iqr_cm=round(iqr * 100, 1))
    if (contact.sum() >= MIN_CONTACT_PX):
        _, iqr_c, _ = _robust(hc)
        s["contact_iqr_cm"] = round(iqr_c * 100, 1)

    if plane_n is not None:
        ng = float(plane_n @ g)

        def depth_at(surface_pts: np.ndarray) -> np.ndarray:
            # ground under a point on the water surface, along gravity
            return (surface_pts @ plane_n - plane_d) / ng

        if len(hc):
            surf = points[contact] + (level - hc)[:, None] * g   # each contact moved onto the level
            dc = depth_at(surf)
            s.update(depth_contact_cm=_pct(dc, 50), depth_contact_p90_cm=_pct(dc, 90))
        # water region: the ray through each water pixel meets the level surface
        rays = points[water]
        gr = rays @ g
        ok = gr < -1e-6
        if level < 0 and ok.any():
            lam = level / gr[ok]
            surf = rays[ok] * lam[:, None]
            near = np.linalg.norm(surf, axis=1) < MAX_RANGE_M
            dw = depth_at(surf[near])
            if len(dw):
                s.update(depth_water_cm=_pct(dw, 50), depth_water_p90_cm=_pct(dw, 90))
        s["camera_height_m"] = round(float(-plane_d / ng), 2)

    # route B: wheels touching the water
    wheel_depths = []
    for wm in masks.wheels:
        wv = wm & valid
        if wv.sum() < 30 or (_dilate(wm, WATERLINE_PX) & water).sum() < 5:
            continue
        top = float(np.percentile(height[wv], 95))
        wheel_depths.append((level - (top - TYRE_DIAMETER_M)) * 100)
    if wheel_depths:
        s.update(n_wheels=len(wheel_depths), depth_wheel_cm=round(float(np.median(wheel_depths)), 1))
    return s, draw


def abstain_reason(s: dict) -> str | None:
    """Why this frame gives no route-A depth, from its stored numbers; None = answer."""
    if s.get("error"):
        return "error"
    if s.get("water_frac", 0) < 0.01:
        return "no_water"
    if "plane_resid_cm" not in s:
        return "no_dry_ground"
    if s["plane_resid_cm"] > MAX_PLANE_RESID_CM:
        return "plane_fit_poor"
    if s.get("n_contact", 0) < MIN_CONTACT_PX:
        return "shore_only" if s.get("n_shore", 0) >= MIN_SHORE_PX else "no_waterline"
    if s.get("level_iqr_cm", 1e9) > MAX_LEVEL_IQR_CM:
        return "level_not_flat"
    d = s.get("depth_contact_cm")
    if d is None:
        return "no_waterline"
    if d < MIN_DEPTH_CM:
        return "contact_below_ground"
    if d > MAX_DEPTH_CM:
        return "implausible"
    return None


def depth_answer(s: dict) -> float | None:
    return None if abstain_reason(s) else max(0.0, s["depth_contact_cm"])


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------

def overlay(rgb: np.ndarray, masks, draw: dict, s: dict, scale: int = 2) -> np.ndarray:
    import cv2
    img = rgb.astype(np.float32)
    for mask, colour in ((masks.water, (40, 110, 255)), (masks.ground, (40, 200, 70)),
                         (masks.objects, (255, 150, 30))):
        img[mask] = img[mask] * 0.6 + np.array(colour) * 0.4
    img[draw["shore"]] = (255, 235, 0)
    img[draw["contact"]] = (255, 0, 0)
    img = cv2.resize(img.clip(0, 255).astype(np.uint8), None, fx=scale, fy=scale,
                     interpolation=cv2.INTER_NEAREST)
    why = abstain_reason(s)
    lines = [f"A {s['depth_contact_cm']} cm" if not why else f"A -- ({why})",
             f"water {s.get('depth_water_cm')} / p90 {s.get('depth_water_p90_cm')} cm",
             f"wheel {s.get('depth_wheel_cm')} cm  n_c {s.get('n_contact', 0)}  n_s {s.get('n_shore', 0)}",
             f"iqr {s.get('level_iqr_cm')}  gap {s.get('level_gap_cm')}  g {s.get('gravity')}"]
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    for i, text in enumerate(lines):
        y = 16 + 16 * i
        cv2.putText(bgr, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(bgr, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    return bgr


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def scene_kept(shots: list[dict], scene: str) -> tuple[list[dict], int]:
    """Shots the scene filter keeps (all of them with scene='all'), and how many it left out."""
    if scene == "all":
        return shots, 0
    skip = skip_ids()
    kept = [s for s in shots if s["id"] not in skip]
    return kept, len(shots) - len(kept)


def choose_shots(n: int, seed: int) -> list[dict]:
    path = OUT_DIR / "shots.jsonl"
    if path.exists():
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    items, _ = eligible(read_ledger(LEDGER_DIR), FRAMES_DIR)
    chosen = select(items, n, per_video=1, seed=seed)
    rows = [{"id": it["item_id"], "video_id": it["video_id"], "shot": it["shot"],
             "frames": [f["path"] for f in it["frames"]]} for it in chosen]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return rows


def read_frames(cfg: str) -> dict[tuple[str, int], dict]:
    path = OUT_DIR / "frames.jsonl"
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue                     # a killed run's last line
            if row.get("config") == cfg:
                out[(row["id"], row["frame"])] = row
    return out


def run(args) -> None:
    from verify_video_frames import pick_free_gpu  # sets CUDA_DEVICE_ORDER before torch starts
    import cv2
    import torch
    from PIL import Image
    from moge.model.v2 import MoGeModel
    from segment import Segmenter

    cfg = config_id(args.segmenter)
    shots, _ = scene_kept(choose_shots(args.n, args.seed), args.scene)
    done = read_frames(cfg)
    todo = [s for s in shots if any((s["id"], j) not in done for j in range(FRAMES_PER_SHOT))]
    if args.limit:
        todo = todo[:args.limit]
    print(f"geometry_depth {cfg}: {len(todo)} of {len(shots)} shots to do", flush=True)
    if not todo:
        return
    device = args.device
    if device == "free":
        gpu = pick_free_gpu(30)
        if gpu is None:
            raise SystemExit("no GPU with 30 GiB free")
        device = f"cuda:{gpu}"
    print(f"device {device}", flush=True)
    t0 = time.time()
    moge = MoGeModel.from_pretrained(MOGE_ID).to(device).eval()
    seg = Segmenter(args.segmenter, device)
    print(f"models loaded in {time.time() - t0:.0f} s", flush=True)

    (OUT_DIR / "overlays").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    with (OUT_DIR / "frames.jsonl").open("a", encoding="utf-8") as fh:
        for i, shot in enumerate(todo, 1):
            tiles = []
            for j, rel in enumerate(shot["frames"]):
                t = time.time()
                rgb = np.asarray(Image.open(PROJECT / rel).convert("RGB"))
                row = {"id": shot["id"], "frame": j, "config": cfg, "size": [rgb.shape[1], rgb.shape[0]]}
                try:
                    with torch.inference_mode():
                        out = moge.infer(torch.tensor(rgb / 255.0, dtype=torch.float32, device=device)
                                         .permute(2, 0, 1))
                    points = out["points"].float().cpu().numpy()
                    normals = out["normal"].float().cpu().numpy()
                    valid = out["mask"].cpu().numpy().astype(bool) & np.isfinite(points).all(-1)
                    points = np.nan_to_num(points)
                    normals = np.nan_to_num(normals)
                    masks = seg(Image.fromarray(rgb))
                    stats, draw = measure(points, normals, valid, masks, rng)
                    stats["labels"] = masks.labels
                    tiles.append(overlay(rgb, masks, draw, stats))
                except Exception as e:  # noqa: BLE001 -- one bad frame must not stop the pilot
                    stats = {"error": f"{type(e).__name__}: {e}"}
                    tiles.append(cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), None, fx=2, fy=2))
                stats["abstain"] = abstain_reason(stats)
                stats["seconds"] = round(time.time() - t, 2)
                row.update(stats)
                row["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                fh.write(json.dumps(row) + "\n")
                fh.flush()
            hmax = max(t.shape[0] for t in tiles)
            tiles = [cv2.copyMakeBorder(t, 0, hmax - t.shape[0], 0, 6, cv2.BORDER_CONSTANT) for t in tiles]
            vid, num = shot["id"].split(":")
            cv2.imwrite(str(OUT_DIR / "overlays" / f"{vid}_{num}.jpg"), np.hstack(tiles))
            print(f"[{i}/{len(todo)}] {shot['id']}: "
                  + ", ".join(str(r) for r in _shot_answers(read_frames_for(fh.name, shot["id"], cfg))),
                  flush=True)


def read_frames_for(path: str, item: str, cfg: str) -> list[dict]:
    rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    latest = {r["frame"]: r for r in rows if r["id"] == item and r.get("config") == cfg}
    return [latest[j] for j in sorted(latest)]


def _shot_answers(rows: list[dict]) -> list:
    return [depth_answer(r) if depth_answer(r) is not None else r.get("abstain") or abstain_reason(r)
            for r in rows]


def read_vlm() -> dict[str, dict]:
    out = {}
    for path in sorted(VLM_DIR.glob("answers_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[row["id"]] = row
    return out


def summary(segmenter: str, scene: str = "real") -> dict:
    from collections import Counter
    cfg = config_id(segmenter)
    shots = choose_shots(0, 0) if (OUT_DIR / "shots.jsonl").exists() else []
    shots, scene_skipped = scene_kept(shots, scene)
    frames = read_frames(cfg)
    vlm = read_vlm()
    reasons, basis = Counter(), Counter()
    per_shot, geo_spread, c1_spread_same, c1_spread_all = [], [], [], []
    vs_c4, vs_c5, vs_wheel, gaps, secs = [], [], [], [], []
    for shot in shots:
        rows = [frames.get((shot["id"], j)) for j in range(FRAMES_PER_SHOT)]
        rows = [r for r in rows if r]
        if len(rows) < FRAMES_PER_SHOT:
            continue
        answers = []
        for r in rows:
            why = abstain_reason(r)
            reasons[why or "answered"] += 1
            basis[r.get("gravity", "-")] += 1
            secs.append(r.get("seconds", 0))
            if r.get("level_gap_cm") is not None:
                gaps.append(r["level_gap_cm"])
            a = depth_answer(r)
            if a is not None:
                answers.append(a)
                if r.get("depth_wheel_cm") is not None:
                    vs_wheel.append(abs(a - r["depth_wheel_cm"]))
        v = vlm.get(shot["id"], {})
        c1 = [midpoint_cm(x) for x in (v.get("C1") or [])]
        c1 = [x for x in c1 if x is not None]
        if len(c1) == FRAMES_PER_SHOT:
            c1_spread_all.append(max(c1) - min(c1))
        geo = statistics.median(answers) if answers else None
        if len(answers) == FRAMES_PER_SHOT:
            geo_spread.append(max(answers) - min(answers))
            if len(c1) == FRAMES_PER_SHOT:
                c1_spread_same.append(max(c1) - min(c1))
        for key, acc in (("C4", vs_c4), ("C5", vs_c5)):
            m = midpoint_cm(v.get(key))
            if geo is not None and m is not None:
                acc.append(geo - m)
        per_shot.append({"id": shot["id"], "frames": [depth_answer(r) if depth_answer(r) is not None
                                                     else abstain_reason(r) for r in rows],
                         "geo_cm": geo, "C1": v.get("C1"), "C4": v.get("C4"), "C5": v.get("C5")})

    def med(x):
        return round(statistics.median(x), 1) if x else None

    n_frames = sum(reasons.values())
    res = {
        "config": cfg, "segmenter": segmenter_id(segmenter), "moge": MOGE_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scene_filter": scene, "shots_skipped_by_scene": scene_skipped,
        "shots": len(per_shot), "frames": n_frames,
        "frames_answered": reasons.get("answered", 0),
        "shots_with_answer": sum(1 for p in per_shot if p["geo_cm"] is not None),
        "shots_all_3_answered": len(geo_spread),
        "abstain_reasons": dict(reasons.most_common()),
        "gravity_basis": dict(basis.most_common()),
        "within_shot_spread_cm": {
            "geometry_median": med(geo_spread),
            "vlm_C1_same_shots_median": med(c1_spread_same),
            "vlm_C1_all_shots_median": med(c1_spread_all)},
        "geometry_minus_vlm_cm": {"C4_median": med(vs_c4), "C4_abs_median": med([abs(x) for x in vs_c4]),
                                  "C5_median": med(vs_c5), "C5_abs_median": med([abs(x) for x in vs_c5]),
                                  "n": len(vs_c4)},
        "route_A_vs_wheel_abs_median_cm": med(vs_wheel),
        "contact_minus_shore_level_cm_median": med(gaps),
        "seconds_per_frame_median": med(secs),
        "per_shot": per_shot,
    }
    tmp = OUT_DIR / "summary.json.tmp"
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, OUT_DIR / "summary.json")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=50, help="shots in the pilot (only used when shots.jsonl is new)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--segmenter", default="gdino-sam2", choices=["gdino-sam2", "sam3"])
    ap.add_argument("--device", default="free", help="'free' = GPU with the most free memory, or cuda:N")
    ap.add_argument("--limit", type=int, default=None, help="only this many shots (smoke test)")
    ap.add_argument("--summary", action="store_true", help="only re-score stored frames, no GPU")
    ap.add_argument("--scene", default="real", choices=["real", "all"],
                    help="real = leave out shots the scene filter flags (default); all = every shot")
    args = ap.parse_args()
    if not args.summary:
        run(args)
    res = summary(args.segmenter, args.scene)
    print(json.dumps({k: v for k, v in res.items() if k != "per_shot"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

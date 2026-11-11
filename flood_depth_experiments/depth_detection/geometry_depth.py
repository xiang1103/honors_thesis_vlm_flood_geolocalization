#!/usr/bin/env python3
"""Water depth from geometry + segmentation -- the 50-shot pilot.

Design and reasoning: docs/water_depth_segmentation.md (read it first). Per
frame:

  1. MoGe-2 (Ruicheng/moge-2-vitl-normal): a metric 3D point and a surface
     normal per pixel, camera at the origin (OpenCV axes: x right, y down).
  2. segment.py (SAM 3): water / dry ground / objects / wheels masks.
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
  7. Route B (known size), per object standing in the water, independent of
     route A (no ground plane, no frame-wide level): every SAM 3 instance
     whose phrase can be a reference (reference_sizes.json) and whose mask
     has water just below it; its own waterline is where the mask meets that
     water; visible height = top of the mask to that waterline, in pixels x
     distance / focal length (MoGe's). Stored per object, no size assumed.
     object_sizes.py then asks the local VLM about each object, twice and
     separately: which table entry it is (route B: depth = the table's
     height - visible) and, without the table, how high its top is (route V:
     depth = the VLM's height - visible). A frame's answer per route is the
     median over its usable objects.

All routes are computed for every frame and stored side by side; neither
overrides the other. Every number is stored, abstaining or not;
`abstain_reason(s, route)` decides from the stored numbers, so thresholds
can be changed and `--summary` re-run without a GPU.

Shots: random eligible flood shots (sample_items.eligible: flood, not
ai_video, 3 frames on disk), one per video, fixed seed -- an honest coverage
number, not hand-picked scenes. The list is written once (shots.jsonl) and
reused, so reruns and resumes measure the same shots.

Output (experiment only, nothing in data/):
  scrape_data/experiments/flood_depth/geometry/
    shots.jsonl     the chosen shots
    frames.jsonl    one line per (shot, frame, config), appended + flushed; resume key
    overlays/<video>_shotNNN_<j>.jpg   one per frame: masks, route A waterline, route B objects (numbered)
    frames/<video>_shotNNN_<j>.jpg     the original frame, unmasked (copied, for the report)
    object_labels.jsonl                object_sizes.py's VLM answers per object (routes B and V)
    summary.json    per route: coverage, within-shot spread vs the VLM's C1, agreement with C4/C5

    python3 flood_depth_experiments/depth_detection/geometry_depth.py                # 50 shots, freest GPU
    python3 flood_depth_experiments/depth_detection/geometry_depth.py --limit 2      # smoke test
    python3 flood_depth_experiments/depth_detection/geometry_depth.py --summary      # re-score, no GPU

Segmentation is SAM 3 only (owner, 2026-11-04). Rows from the earlier
Grounding DINO + SAM 2 and SAM 3 runs stay in frames.jsonl under their own
config ids; summary and report read the current config only.

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

REF_TABLE_PATH = HERE / "reference_sizes.json"
REF_TABLE = json.loads(REF_TABLE_PATH.read_text(encoding="utf-8"))
REF_ENTRIES = {e["id"]: e for e in REF_TABLE["entries"]}
REF_PHRASES = sorted({p for e in REF_TABLE["entries"] for p in e["phrases"]})

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
MAX_RANGE_M = 60.0                    # ignore points farther than this

# Abstain rules (applied to stored numbers; see abstain_reason)
MIN_GROUND_PX = 300
MAX_PLANE_RESID_CM = 5.0
MIN_CONTACT_PX = 8
MIN_SHORE_PX = 15
MAX_LEVEL_IQR_CM = 20.0
MIN_DEPTH_CM = -5.0
MAX_DEPTH_CM = 300.0
MIN_OBJ_PX = 20                       # routes B/V: object mask pixels
MIN_OBJ_WATER_PX = 3                  # routes B/V: object pixels with water just below
MAX_OBJECTS = 10                      # routes B/V: per frame, highest SAM 3 score first
DEDUP_IOU = 0.6                       # two phrases' masks overlapping more = one object
MAX_VLM_SIZE_CM = 1000.0              # route V: a VLM size above this (or <= 0) is not an answer
MAX_REF_SPREAD_CM = 25.0              # routes B/V: objects in one frame disagree by more = abstain
ROUTES = ("A", "B", "V")              # A plane, B table sizes, V VLM sizes


def config_id() -> str:
    cfg = json.dumps([MOGE_ID, segmenter_id(), GROUND_NORMAL_MAX_DEG, PLANE_MAX_TILT_DEG,
                      SLOPE_MAX_DEG, RANSAC_ITERS, WATERLINE_PX, VERTICAL_MAX_COS,
                      MAX_RANGE_M, "route-b-objects", REF_PHRASES, MIN_OBJ_PX, MIN_OBJ_WATER_PX,
                      MAX_OBJECTS, DEDUP_IOU])
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


def measure(points: np.ndarray, normals: np.ndarray, valid: np.ndarray, masks, rng,
            fy: float) -> tuple[dict, dict]:
    """One frame -> (stored numbers, drawing info). Heights are along gravity, metres;
    fy = MoGe's vertical focal length in pixels (route B)."""
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
    draw = {"contact": np.zeros((h, w), bool), "shore": np.zeros((h, w), bool),
            "obj_line": np.zeros((h, w), bool), "objects": []}
    route_b(s, draw, points, valid, masks, fy)

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

    return s, draw


def route_b(s: dict, draw: dict, points: np.ndarray, valid: np.ndarray, masks, fy: float) -> None:
    """Routes B/V, geometry part: every object that can be a reference and stands in
    water -> its visible height (mask top to its own waterline). No size is assumed
    here; the size comes later from the table (B) or the VLM (V)."""
    below = _shift_down_any(masks.water, WATERLINE_PX)
    kept, objs = [], []
    for phrase, score, m in sorted(masks.instances, key=lambda x: -x[1]):
        if phrase not in REF_PHRASES:
            continue
        wv, wet = m & valid, m & below
        if wv.sum() < MIN_OBJ_PX or wet.sum() < MIN_OBJ_WATER_PX:
            continue
        if any((m & k).sum() / (m | k).sum() > DEDUP_IOU for k in kept):
            continue
        kept.append(m)
        ys, xs = np.nonzero(m)
        top = int(ys.min())
        line = float(np.median(np.nonzero(wet)[0]))       # the object's own waterline row
        z = float(np.median(points[wv][:, 2]))
        k = len(objs)
        objs.append({"k": k, "phrase": phrase, "score": score,
                     "box": [int(xs.min()), top, int(xs.max()), int(ys.max())],
                     "top_px": top, "line_px": round(line, 1), "visible_px": round(line - top + 1, 1),
                     "dist_m": round(z, 2), "visible_cm": round((line - top + 1) * z / fy * 100, 1),
                     "top_at_edge": top == 0})
        draw["obj_line"] |= wet
        draw["objects"].append((top, int(round(line)), int(xs.min()), int(xs.max()), k))
        if len(objs) == MAX_OBJECTS:
            break
    s["objects_b"] = objs


# --------------------------------------------------------------------------
# routes B / V from stored objects + the VLM's labels (no GPU)
# --------------------------------------------------------------------------

def ref_usable(entry: dict) -> bool:
    return (entry["basis"] != "varies" and entry["on_ground"]
            and (entry["max_cm"] - entry["min_cm"]) / 2 <= REF_TABLE["max_half_range_cm"])


_LABELS: dict | None = None


def object_labels() -> dict[tuple[str, int, int], dict]:
    """(shot id, frame, object k) -> object_sizes.py's answers, for the current config."""
    global _LABELS
    if _LABELS is None:
        _LABELS = {}
        path = OUT_DIR / "object_labels.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("config") == config_id() and r.get("prompt_id") == _object_prompt_id():
                    _LABELS[(r["id"], r["frame"], r["k"])] = r
    return _LABELS


def _object_prompt_id() -> str:
    from object_sizes import prompt_id
    return prompt_id()


def vlm_size(lab: dict) -> float | None:
    v = lab.get("vlm_top_cm")
    return v if v is not None and 0 < v <= MAX_VLM_SIZE_CM else None


def ref_objects(s: dict, route: str) -> list[dict]:
    """Per object: {k, phrase, size_cm, entry, depth_cm or None, why}."""
    out = []
    for o in s.get("objects_b", []):
        lab = object_labels().get((s.get("id"), s.get("frame"), o["k"]))
        r = {"k": o["k"], "phrase": o["phrase"], "visible_cm": o["visible_cm"], "size_cm": None,
             "entry": None, "depth_cm": None, "why": None}
        out.append(r)
        if o["top_at_edge"]:
            r["why"] = "top_cut_off"
            continue
        if lab is None:
            r["why"] = "not_labelled"
            continue
        if lab.get("top_visible") is False:
            r["why"] = "top_hidden"
            continue
        if route == "B":
            e = REF_ENTRIES.get(lab.get("entry") or "")
            if e is None:
                r["why"] = "not_in_table"
                continue
            r["entry"] = e["id"]
            if not ref_usable(e):
                r["why"] = "size_varies"
                continue
            r["size_cm"] = e["top_cm"]
        else:
            if vlm_size(lab) is None:
                r["why"] = "no_vlm_size"
                continue
            r["size_cm"] = vlm_size(lab)
        d = round(r["size_cm"] - o["visible_cm"], 1)
        if d < MIN_DEPTH_CM:
            r["why"] = "visible_taller_than_size"
            continue
        r["depth_cm"] = max(0.0, d)
    return out


def _route_bv(s: dict, route: str) -> tuple[float | None, str | None, float | None]:
    """(frame depth, abstain reason, spread) for route B or V."""
    if "objects_b" not in s:
        return None, "not_measured", None            # a row from before the general route B
    objs = ref_objects(s, route)
    if not objs:
        return None, "no_object_in_water", None
    d = [o["depth_cm"] for o in objs if o["depth_cm"] is not None]
    if not d:
        from collections import Counter
        return None, Counter(o["why"] for o in objs).most_common(1)[0][0], None
    spread = round(max(d) - min(d), 1)
    if spread > MAX_REF_SPREAD_CM:
        return None, "objects_disagree", spread
    return round(float(np.median(d)), 1), None, spread


def abstain_reason(s: dict, route: str = "A") -> str | None:
    """Why this frame gives no depth by `route` ("A" plane, "B" table sizes,
    "V" VLM sizes), from its stored numbers (+ object labels); None = answer."""
    if s.get("error"):
        return "error"
    if s.get("water_frac", 0) < 0.01:
        return "no_water"
    if route in ("B", "V"):
        return _route_bv(s, route)[1]
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


def depth_answer(s: dict, route: str = "A") -> float | None:
    if abstain_reason(s, route):
        return None
    return max(0.0, s["depth_contact_cm"]) if route == "A" else _route_bv(s, route)[0]


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
    img[draw["obj_line"]] = (255, 0, 255)
    img = cv2.resize(img.clip(0, 255).astype(np.uint8), None, fx=scale, fy=scale,
                     interpolation=cv2.INTER_NEAREST)
    why_a = abstain_reason(s, "A")
    lines = [f"A {s['depth_contact_cm']} cm" if not why_a else f"A -- ({why_a})",
             f"B/V objects in water: {len(s.get('objects_b', []))}"]
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    for top, line, x0, x1, k in draw.get("objects", []):   # routes B/V: object top to its waterline
        x = (x0 + x1) * scale // 2
        cv2.rectangle(bgr, (x0 * scale, top * scale), (x1 * scale, line * scale), (255, 0, 255), 1)
        cv2.line(bgr, (x, top * scale), (x, line * scale), (255, 0, 255), 2)
        for colour, thick in (((0, 0, 0), 3), ((255, 255, 255), 1)):
            cv2.putText(bgr, str(k), (x0 * scale + 2, top * scale + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        colour, thick, cv2.LINE_AA)
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

    cfg = config_id()
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
    seg = Segmenter(device)
    print(f"models loaded in {time.time() - t0:.0f} s", flush=True)

    import shutil
    for sub in ("overlays", "frames"):
        (OUT_DIR / sub).mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    with (OUT_DIR / "frames.jsonl").open("a", encoding="utf-8") as fh:
        for i, shot in enumerate(todo, 1):
            vid, num = shot["id"].split(":")
            for j, rel in enumerate(shot["frames"]):
                stem = f"{vid}_{num}_{j}.jpg"
                shutil.copyfile(PROJECT / rel, OUT_DIR / "frames" / stem)   # unmasked, for the report
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
                    fy = float(out["intrinsics"][1, 1]) * rgb.shape[0]   # normalised -> pixels
                    stats, draw = measure(points, normals, valid, masks, rng, fy)
                    stats["fy_px"] = round(fy, 1)
                    stats["labels"] = masks.labels
                    cv2.imwrite(str(OUT_DIR / "overlays" / stem), overlay(rgb, masks, draw, stats))
                except Exception as e:  # noqa: BLE001 -- one bad frame must not stop the pilot
                    stats = {"error": f"{type(e).__name__}: {e}"}
                stats["abstain"] = abstain_reason(stats, "A")   # B/V need object_sizes.py's labels first
                stats["seconds"] = round(time.time() - t, 2)
                row.update(stats)
                row["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                fh.write(json.dumps(row) + "\n")
                fh.flush()
            rows = read_frames_for(fh.name, shot["id"], cfg)
            print(f"[{i}/{len(todo)}] {shot['id']}: A "
                  + ", ".join(str(depth_answer(r, "A") if depth_answer(r, "A") is not None
                                  else abstain_reason(r, "A")) for r in rows)
                  + " | objects in water " + ", ".join(str(len(r.get("objects_b", []))) for r in rows),
                  flush=True)


def read_frames_for(path: str, item: str, cfg: str) -> list[dict]:
    rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    latest = {r["frame"]: r for r in rows if r["id"] == item and r.get("config") == cfg}
    return [latest[j] for j in sorted(latest)]


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


def summary(scene: str = "real") -> dict:
    from collections import Counter
    cfg = config_id()
    shots = choose_shots(0, 0) if (OUT_DIR / "shots.jsonl").exists() else []
    shots, scene_skipped = scene_kept(shots, scene)
    frames = read_frames(cfg)
    vlm = read_vlm()

    def med(x):
        return round(statistics.median(x), 1) if x else None

    per_shot, secs, gaps = [], [], []
    pairs = {("A", "B"): [], ("A", "V"): [], ("B", "V"): []}
    routes = {r: {"reasons": Counter(), "spread": [], "c1_same": [], "vs_c4": [], "vs_c5": [], "shots": 0}
              for r in ROUTES}
    c1_spread_all = []
    for shot in shots:
        rows = [frames.get((shot["id"], j)) for j in range(FRAMES_PER_SHOT)]
        rows = [r for r in rows if r]
        if len(rows) < FRAMES_PER_SHOT:
            continue
        v = vlm.get(shot["id"], {})
        c1 = [midpoint_cm(x) for x in (v.get("C1") or [])]
        c1 = [x for x in c1 if x is not None]
        if len(c1) == FRAMES_PER_SHOT:
            c1_spread_all.append(max(c1) - min(c1))
        line = {"id": shot["id"], "C1": v.get("C1"), "C4": v.get("C4"), "C5": v.get("C5")}
        for r in rows:
            secs.append(r.get("seconds", 0))
            if r.get("level_gap_cm") is not None:
                gaps.append(r["level_gap_cm"])
            ans = {route: depth_answer(r, route) for route in ROUTES}
            for (x, y), lst in pairs.items():
                if ans[x] is not None and ans[y] is not None:
                    lst.append(ans[x] - ans[y])
        for route, acc in routes.items():
            answers = []
            for r in rows:
                acc["reasons"][abstain_reason(r, route) or "answered"] += 1
                d = depth_answer(r, route)
                if d is not None:
                    answers.append(d)
            geo = round(statistics.median(answers), 1) if answers else None
            acc["shots"] += geo is not None
            if len(answers) == FRAMES_PER_SHOT:
                acc["spread"].append(max(answers) - min(answers))
                if len(c1) == FRAMES_PER_SHOT:
                    acc["c1_same"].append(max(c1) - min(c1))
            for key, lst in (("C4", acc["vs_c4"]), ("C5", acc["vs_c5"])):
                m = midpoint_cm(v.get(key))
                if geo is not None and m is not None:
                    lst.append(geo - m)
            line[f"route_{route}_frames"] = [depth_answer(r, route) if depth_answer(r, route) is not None
                                             else abstain_reason(r, route) for r in rows]
            line[f"route_{route}_cm"] = geo
        per_shot.append(line)

    res = {
        "config": cfg, "segmenter": segmenter_id(), "moge": MOGE_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scene_filter": scene, "shots_skipped_by_scene": scene_skipped,
        "shots": len(per_shot), "frames": len(per_shot) * FRAMES_PER_SHOT,
        "routes": {route: {
            "frames_answered": acc["reasons"].get("answered", 0),
            "shots_with_answer": acc["shots"],
            "shots_all_3_answered": len(acc["spread"]),
            "abstain_reasons": dict(acc["reasons"].most_common()),
            "within_shot_spread_cm": {"geometry_median": med(acc["spread"]),
                                      "vlm_C1_same_shots_median": med(acc["c1_same"])},
            "geometry_minus_vlm_cm": {"C4_median": med(acc["vs_c4"]),
                                      "C4_abs_median": med([abs(x) for x in acc["vs_c4"]]),
                                      "C5_median": med(acc["vs_c5"]),
                                      "C5_abs_median": med([abs(x) for x in acc["vs_c5"]]),
                                      "n": len(acc["vs_c4"])},
        } for route, acc in routes.items()},
        "vlm_C1_within_shot_spread_all_shots_median_cm": med(c1_spread_all),
        "route_differences_cm": {f"{x}_minus_{y}": {"n_frames": len(l), "median": med(l),
                                                     "abs_median": med([abs(v) for v in l])}
                                 for (x, y), l in pairs.items()},
        "vlm_size_check": vlm_size_check(),
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


def vlm_size_check() -> dict:
    """How the VLM's free size answer (route V, asked without the table) compares
    with the table's size for the entry the VLM picked (route B), per entry."""
    by = {}
    for lab in object_labels().values():
        e = REF_ENTRIES.get(lab.get("entry") or "")
        if e is None or vlm_size(lab) is None:
            continue
        by.setdefault(e["id"], []).append((vlm_size(lab), e))
    out, allerr, inside = {}, [], 0
    for eid, xs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        e = xs[0][1]
        err = [v - e["top_cm"] for v, _ in xs]
        ins = sum(e["min_cm"] <= v <= e["max_cm"] for v, _ in xs)
        allerr += err
        inside += ins
        out[eid] = {"n": len(xs), "table_cm": e["top_cm"], "range_cm": [e["min_cm"], e["max_cm"]],
                    "vlm_median_cm": round(statistics.median(v for v, _ in xs), 1),
                    "vlm_minus_table_median_cm": round(statistics.median(err), 1),
                    "abs_error_median_cm": round(statistics.median(abs(x) for x in err), 1),
                    "vlm_inside_table_range": ins}
    return {"objects": len(allerr), "abs_error_median_cm": round(statistics.median(abs(x) for x in allerr), 1)
            if allerr else None, "share_inside_table_range": round(inside / len(allerr), 3) if allerr else None,
            "by_entry": out}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=50, help="shots in the pilot (only used when shots.jsonl is new)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="free", help="'free' = GPU with the most free memory, or cuda:N")
    ap.add_argument("--limit", type=int, default=None, help="only this many shots (smoke test)")
    ap.add_argument("--summary", action="store_true", help="only re-score stored frames, no GPU")
    ap.add_argument("--scene", default="real", choices=["real", "all"],
                    help="real = leave out shots the scene filter flags (default); all = every shot")
    args = ap.parse_args()
    if not args.summary:
        run(args)
    res = summary(args.scene)
    print(json.dumps({k: v for k, v in res.items() if k != "per_shot"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

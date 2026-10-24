"""The data behind the review site's /depth.html (data_view_server.py).

Joins a full run's per-video answers (video_labels.json), its detector boxes
(refs_*.jsonl) and the uploader text, and describes each condition's prompt
as the ordered parts the model was handed, so the page can show exactly
what each condition saw:

    {"text": "..."}                    fixed text, verbatim from conditions.py
    {"image": 1} / {"image": [0,1,2]}  the shot's frame(s), by index j
    {"image": 1, "boxes": true}        the key frame with the C5 boxes drawn
    {"slot": "uploader_text"}          this video's title/tags/description block
    {"slot": "boxes"}                  this shot's box listing (or NO_BOXES)

The page asks for one page of videos at a time (query(): filter, sort and
paginate here, so the browser never downloads the whole run) and the run's
fixed parts once (meta()).

The layouts below mirror conditions.build_c1..build_c5 -- change them
together. The page warns when the run's prompt ids differ from the current
conditions.py, i.e. when the text shown is not what that run asked.
"""
from __future__ import annotations

import json
from pathlib import Path

import conditions
from conditions import (BOX_COLOURS, BOXES_INTRO, MULTI_INTRO, NO_BOXES, QUESTION, SINGLE_INTRO,
                        heights_block, prompt_id, text_block)
from levels import REFERENCE_HEIGHTS, midpoint_cm

PROJECT = Path(__file__).resolve().parents[2]
VIDEOS_FILE = PROJECT / "data" / "youtube_videos.json"
LEDGER_DIR = PROJECT / "scrape_data" / "youtube_shot_scan"
DEFAULT_RUN = PROJECT / "scrape_data" / "experiments" / "flood_depth" / "full_5cm"
CONDS = ["C1", "C2", "C3", "C4", "C5"]
KEY = 1
FRAMES = [0, 1, 2]


def prompt_layouts() -> dict[str, list[dict]]:
    heights = heights_block()
    return {
        "C1": [{"text": SINGLE_INTRO}, {"image": KEY}, {"text": QUESTION}],
        "C2": [{"text": MULTI_INTRO.format(n=len(FRAMES))}, {"image": FRAMES}, {"text": QUESTION}],
        "C3": [{"text": SINGLE_INTRO}, {"image": KEY}, {"slot": "uploader_text"}, {"text": QUESTION}],
        "C4": [{"text": SINGLE_INTRO}, {"image": KEY}, {"text": heights}, {"text": QUESTION}],
        "C5": [{"text": SINGLE_INTRO}, {"image": KEY, "boxes": True}, {"slot": "boxes"},
               {"text": heights}, {"text": QUESTION}],
    }


def _jsonl(paths) -> list[dict]:
    rows = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue                                     # a killed run's last line
    return rows


def watched_files(run_dir: Path) -> list[Path]:
    """What the catalog is built from; the server rebuilds when these change."""
    return [run_dir / "video_labels.json", *sorted(run_dir.glob("refs_*.jsonl"))]


def read_boxes(run_dir: Path, detector: str | None) -> dict[str, list]:
    """shot id -> compact boxes. run_all.py reused the pilot's detections
    (refs.jsonl, key frame, same detector id) and wrote only new ones to
    refs_*.jsonl, so both are read."""
    out = {}
    pilot = run_dir.parent / "refs.jsonl"
    if pilot.is_file():
        for r in _jsonl([pilot]):
            if r.get("detector") == detector and r.get("frame") == KEY:
                out[r["item_id"]] = [[b["label"], b["score"], *b["box"]] for b in r["boxes"]]
    out.update({r["id"]: r["b"] for r in _jsonl(sorted(run_dir.glob("refs_*.jsonl")))})
    return out


def key_frame_times(video_ids: set[str], ledger_dir: Path = LEDGER_DIR) -> dict[tuple[str, int], float]:
    """(video_id, shot number) -> time of the key frame in seconds, from the
    shot-scan ledger (a finished line wins over an error, as in
    scan_shots.read_ledger, which is not imported: it pulls in torch)."""
    rows: dict[str, dict] = {}
    for row in _jsonl(sorted(ledger_dir.glob("*.jsonl"))):
        vid = row.get("video_id")
        if vid in video_ids and "shots" in row:
            rows[vid] = row
    out = {}
    for vid, row in rows.items():
        for k, shot in enumerate(row["shots"]):
            frames = shot.get("frames") or []
            if len(frames) > KEY:
                out[(vid, k + 1)] = frames[KEY].get("t")
    return out


def load_depth_catalog(run_dir: Path, videos_file: Path = VIDEOS_FILE) -> dict:
    labels_file = run_dir / "video_labels.json"
    empty = {"available": False, "run_dir": str(run_dir), "items": []}
    if not labels_file.is_file():
        return empty
    labels = json.loads(labels_file.read_text(encoding="utf-8"))
    head = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    boxes = read_boxes(run_dir, head.get("detector"))
    meta = {}
    if videos_file.is_file():
        meta = {v["video_id"]: v for v in json.loads(videos_file.read_text(encoding="utf-8"))}
    run = {**labels["run"], "detector": head.get("detector")}
    times = key_frame_times(set(labels["videos"]))
    items = []
    for vid, video in labels["videos"].items():
        v = meta.get(vid, {})
        shots = []
        for s in video["shots"]:
            iid = f"{vid}:shot{s['s']:03d}"
            shots.append({**s, "b": boxes.get(iid),        # [label, score, x0, y0, x1, y1] in 0-1
                          "kt": times.get((vid, s["s"]))})  # key frame's time, s
        items.append({
            "video_id": vid,
            "title": v.get("title") or vid,
            "tags": v.get("tags") or [],
            "published": v.get("published_utc") or "",
            "in_ny": v.get("in_ny"), "in_nyc": v.get("in_nyc"),
            "source_url": v.get("source_url") or f"https://www.youtube.com/watch?v={vid}",
            "embed_url": v.get("embed_url") or f"https://www.youtube-nocookie.com/embed/{vid}",
            # exactly the block C3 was given (conditions.text_block)
            "uploader_text": text_block({"title": v.get("title"), "tags": v.get("tags"),
                                         "description": v.get("description")}),
            "shots": shots,
            "label": video["label"],
        })
    current = {c: prompt_id(c) for c in CONDS}
    return {
        "available": True,
        "run_dir": str(run_dir),
        "run": run,
        "prompt_ids_current": current,
        "prompt_changed": [c for c in CONDS if run["prompt_ids"].get(c) != current[c]],
        "layouts": prompt_layouts(),
        "boxes_intro": BOXES_INTRO,
        "no_boxes": NO_BOXES,
        "box_colours": BOX_COLOURS,
        "reference_heights": REFERENCE_HEIGHTS,
        "image_long_side": conditions.IMAGE_LONG_SIDE,
        "items": items,
    }


# -- one page of videos -------------------------------------------------------

PAGE_SIZE = 10
SPREAD_CM = 30          # the "conditions disagree" filter


def meta(catalog: dict) -> dict:
    """Everything but the videos: run header, prompt layouts, box colours."""
    out = {k: v for k, v in catalog.items() if k != "items"}
    out["videos"] = len(catalog["items"])
    out["shots"] = sum(len(i["shots"]) for i in catalog["items"])
    out["page_size"] = PAGE_SIZE
    return out


def location(item: dict) -> str:
    if item["in_nyc"] is True:
        return "nyc"
    if item["in_ny"] is True:
        return "ny"
    if item["in_ny"] is False:
        return "outside"
    return "unknown"


def spread(item: dict) -> float:
    """Range of the five conditions' median depths for one video, in cm."""
    values = [item["label"][c].get("median_cm") for c in CONDS]
    values = [v for v in values if v is not None]
    return max(values) - min(values) if len(values) > 1 else 0.0


def query(catalog: dict, params: dict[str, str]) -> dict:
    """One page of videos. params: cond, filter, location, sort, q, page."""
    cond = params.get("cond") if params.get("cond") in CONDS else "C5"
    flt = params.get("filter", "all")
    where = params.get("location", "all")
    words = (params.get("q") or "").lower().split()

    def keep(item: dict) -> bool:
        has_boxes = any(s.get("b") for s in item["shots"])
        if flt == "boxes" and not has_boxes or flt == "noboxes" and has_boxes:
            return False
        if flt == "x" and not item["label"][cond]["x"]:
            return False
        if flt == "spread" and spread(item) <= SPREAD_CM:
            return False
        if where != "all" and location(item) != where:
            return False
        if words:
            hay = f"{item['title']}\n{' '.join(item['tags'])}\n{item['video_id']}".lower()
            if not all(w in hay for w in words):
                return False
        return True

    items = [i for i in catalog["items"] if keep(i)]
    order = params.get("sort", "deep")
    if order in ("deep", "shallow"):
        def depth(i):
            d = midpoint_cm(i["label"][cond]["deepest"])
            return (d is None, -(d or 0) if order == "deep" else (d or 0))
        items.sort(key=depth)                                   # unanswered last
    elif order == "spread":
        items.sort(key=spread, reverse=True)
    elif order == "shots":
        items.sort(key=lambda i: len(i["shots"]), reverse=True)
    else:
        items.sort(key=lambda i: i["published"], reverse=True)
    pages = max(1, -(-len(items) // PAGE_SIZE))
    try:
        page = min(max(1, int(params.get("page", 1))), pages)
    except ValueError:
        page = 1
    chosen = items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    return {"total": len(items), "page": page, "pages": pages, "page_size": PAGE_SIZE,
            "items": [{**i, "spread_cm": spread(i), "location": location(i)} for i in chosen]}

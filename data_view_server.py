#!/usr/bin/env python3
"""Serve data_view, the local hub for viewing the project's datasets. Two tabs:

/            MyCoast photos: the MyCoast rows of ``data/gis_data/gis_flood_images.json``
             (written by ``scraping/api_based_scraping/gis_scrape.py``; PHOTO_SOURCES).
             Images are never downloaded by Python; the browser loads the remote
             URLs directly, a thumbnail first, falling back to the original.

/videos.html Flood videos: every scanned video with at least one flood shot that
             was not judged AI-generated (is_valid_flood). Joins the shot scan's
             ledger (``scrape_data/youtube_shot_scan/``, written by
             ``flood_depth_experiments/scene_split/scan_shots.py``), the depth run
             (``--depth``, default ``scrape_data/experiments/flood_depth/full_5cm``;
             each flood shot's answer under conditions C1-C5) and
             ``data/youtube_videos.json`` (title, date, location). Rebuilt when the
             ledger or the run changes, so a reload shows newly scanned videos.
             Frames are the scan's own small JPEGs, served from the ledger folder;
             the player is YouTube's embed.

    python3 data_view_server.py              # http://127.0.0.1:8768

The site is read-only: the review decisions it once stored in the browser were
removed 2026-10-24. The old Shots and Depth pages were merged into /videos.html
the same day (the depth page's prompt viewer and detector boxes went with it;
``flood_depth_experiments/flood_depth/depth_review.py`` still builds that data).
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS = PROJECT_DIR / "data" / "gis_data" / "gis_flood_images.json"
DEFAULT_VIDEOS = PROJECT_DIR / "data" / "youtube_videos.json"
DEFAULT_SHOTS = PROJECT_DIR / "scrape_data" / "youtube_shot_scan"
DEFAULT_DEPTH = PROJECT_DIR / "scrape_data" / "experiments" / "flood_depth" / "full_5cm"
DEPTH_CODE = PROJECT_DIR / "flood_depth_experiments" / "flood_depth"
DEPTH_CONDS = ["C1", "C2", "C3", "C4", "C5"]
DEPTH_KEY_FRAME = 1     # depth_review.KEY: the frame C1, C3, C4 and C5 were shown
STATIC_DIR = PROJECT_DIR / "data_view_web"
#: The news site's stylesheet, served as-is so the two sites cannot drift apart
#: visually. GIS-only rules live in data_view_web/gis.css.
SHARED_STYLES = PROJECT_DIR / "image_review_web" / "styles.css"

#: Decisions live only in the browser's localStorage, keyed by `review_id()`.
#: Bump this if that function changes, so the page can detect it loudly.
#: The "gis_review" name predates the rename to data_view and is kept on
#: purpose: changing it orphans every decision already saved in browsers.
REVIEW_ID_SCHEME = "gis_review_v1"
#: Same idea for videos; a different scheme and storage key, so photo and
#: video decisions can never be mistaken for each other.
VIDEO_REVIEW_ID_SCHEME = "youtube_review_v1"
#: The only third-party frame the pages may load: YouTube's privacy-enhanced
#: player, used by the video detail view.
VIDEO_EMBED_ORIGIN = "https://www.youtube-nocookie.com"

SOURCE_LABELS = {
    "mycoast": "MyCoast",
    "usgs_stn": "USGS STN",
    "wikimedia_commons": "Commons",
    "napsg_photomappers": "NAPSG",
}

#: The photo tab shows these sources only; the other rows of
#: gis_flood_images.json (USGS STN, Commons, NAPSG) stay in the file.
PHOTO_SOURCES = {"mycoast"}

COMMONS_THUMB_WIDTH = 500   # Wikimedia serves only standard widths; 640 returns HTTP 400


def review_id(source_url: str, image_url: str) -> str:
    """Identity of one image for HUMAN review decisions.

    Namespaced for the same reason the news site's id is: gis_scrape.py's
    record_id hashes the same two fields, and without the prefix the two ids
    would be identical strings, easy to join or overwrite by mistake.
    """
    value = f"{REVIEW_ID_SCHEME}\n{source_url}\n{image_url}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def _safe_remote_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def commons_thumbnail(url: str) -> str:
    """500px rendition of a Commons original, or "" if there is none.

    Originals run to tens of megabytes, which makes a page of 24 unusable.
    upload.wikimedia.org/wikipedia/commons/a/ab/Name.jpg
      -> upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Name.jpg/500px-Name.jpg
    A thumbnail wider than the original is an error there; the page then falls
    back to the original.
    """
    bare = url.split("?", 1)[0]
    m = re.match(r"(https://upload\.wikimedia\.org/wikipedia/commons)/([0-9a-f]/[0-9a-f]{2})/([^/]+)$", bare)
    if not m or m.group(3).rsplit(".", 1)[-1].lower() not in {"jpg", "jpeg", "png", "gif", "webp"}:
        return ""
    name = quote(unquote(m.group(3)))
    return f"{m.group(1)}/thumb/{m.group(2)}/{name}/{COMMONS_THUMB_WIDTH}px-{name}"


def event_label(source: str, event: str) -> str:
    """NAPSG incidents are ids ("CrowdsourcedPhoto_20210828_Ida_336")."""
    m = re.match(r"CrowdsourcedPhoto_(\d{4})(\d{2})(\d{2})_(.+?)(?:_\d+)?$", event)
    if source == "napsg_photomappers" and m:
        return f"{m.group(4).replace('_', ' ')} ({m.group(1)}-{m.group(2)}-{m.group(3)})"
    return event


def location_class(row: dict) -> str:
    if row.get("in_nyc") is True:
        return "nyc"
    if row.get("in_nyc") is False:
        return "ny"
    return "unknown"


def load_catalog(results_file: Path) -> dict:
    try:
        rows = json.loads(results_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {results_file}: {exc}") from exc
    if not isinstance(rows, list):
        raise RuntimeError(f"Expected a JSON array of records in {results_file}")

    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        image_url = _safe_remote_url(row.get("image_url"))
        source_url = _safe_remote_url(row.get("source_url"))
        if not image_url:
            continue
        source = str(row.get("source") or "unknown")
        if source not in PHOTO_SOURCES:
            continue
        thumbnail = _safe_remote_url(row.get("thumbnail_url"))
        if source == "wikimedia_commons":
            thumbnail = commons_thumbnail(image_url)
        event = str(row.get("event") or "")
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        items.append(
            {
                "id": review_id(source_url, image_url),
                "record_id": str(row.get("record_id") or ""),
                "source": source,
                "source_label": SOURCE_LABELS.get(source, source),
                "title": str(row.get("title") or "Untitled"),
                "text": str(row.get("text") or "").strip(),
                "date": str(row.get("date") or ""),
                "event": event,
                "event_label": event_label(source, event),
                "lat": row.get("lat"),
                "lon": row.get("lon"),
                "location": location_class(row),
                "nyc_basis": str(row.get("nyc_basis") or ""),
                "image_url": image_url,
                "thumbnail_url": thumbnail,
                "source_url": source_url,
                "license": str(row.get("license") or ""),
                "credit": str(row.get("credit") or ""),
                "extra": {str(k): v for k, v in extra.items()},
            }
        )

    items.sort(key=lambda item: (item["date"], item["source"], item["id"]), reverse=True)
    by_source = Counter(item["source"] for item in items)
    locations = Counter(item["location"] for item in items)
    events: dict[str, Counter] = {}
    for item in items:
        events.setdefault(item["source"], Counter())[item["event_label"] or "(none)"] += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review_id_scheme": REVIEW_ID_SCHEME,
        "summary": {
            "images": len(items),
            "sources": [
                {"value": s, "label": SOURCE_LABELS.get(s, s), "count": n}
                for s, n in sorted(by_source.items(), key=lambda kv: -kv[1])
            ],
            "events": {s: c.most_common() for s, c in events.items()},
            "nyc": locations["nyc"],
            "ny_not_nyc": locations["ny"],
            "location_unknown": locations["unknown"],
            "with_coordinates": sum(1 for i in items if i["lat"] is not None),
        },
        "items": items,
    }


def video_review_id(source_url: str) -> str:
    """Identity of one video for HUMAN review decisions (namespaced, like
    `review_id`, so it never equals the scraper's record_id)."""
    return hashlib.sha256(f"{VIDEO_REVIEW_ID_SCHEME}\n{source_url}".encode("utf-8")).hexdigest()[:24]


def flood_rule():
    """(text threshold, is_flood) -- the exporter's own rule, so "flood" on
    this page means exactly what data/youtube_flood_videos.json means."""
    import sys
    sys.path.insert(0, str(PROJECT_DIR / "scraping" / "video_scraping"))
    sys.path.insert(0, str(PROJECT_DIR / "scraping" / "api_based_scraping"))
    from export_flood_videos import is_flood
    from video_signals import FLOOD_TEXT_THRESHOLD, ai_by_text
    return FLOOD_TEXT_THRESHOLD, is_flood, ai_by_text


def video_location(row: dict) -> str:
    if row.get("in_nyc") is True:
        return "nyc"
    if row.get("in_ny") is True:
        return "ny"
    if row.get("in_ny") is False:
        return "outside"
    return "unknown"


def load_video_catalog(videos_file: Path) -> dict:
    """The video page's data. A missing file is not an error -- the photo page
    must still work before the first YouTube scrape -- the page says so."""
    empty = {"review_id_scheme": VIDEO_REVIEW_ID_SCHEME, "available": False,
             "source_file": str(videos_file), "summary": None, "items": []}
    if not videos_file.is_file():
        return empty
    try:
        rows = json.loads(videos_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {videos_file}: {exc}") from exc
    threshold, is_flood, ai_by_text = flood_rule()
    items: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        source_url = _safe_remote_url(row.get("source_url"))
        embed_url = _safe_remote_url(row.get("embed_url"))
        if not source_url or not embed_url.startswith(VIDEO_EMBED_ORIGIN + "/"):
            continue
        thumbs = [t for t in row.get("thumbnails") or [] if isinstance(t, dict)
                  and _safe_remote_url(t.get("image_url"))]
        score = float(row.get("flood_text_score") or 0)
        visual = row.get("flood_visual") if isinstance(row.get("flood_visual"), dict) else None
        items.append({
            "id": video_review_id(source_url),
            "record_id": str(row.get("record_id") or ""),
            "video_id": str(row.get("video_id") or ""),
            "title": str(row.get("title") or "Untitled"),
            "description": str(row.get("description") or "").strip(),
            "tags": [str(t) for t in row.get("tags") or []],
            "published": str(row.get("published_utc") or ""),
            "recording_date": str(row.get("recording_date") or ""),
            "duration_s": row.get("duration_s"),
            # The cover is first; the rest are YouTube's automatic frames.
            "thumbnails": [{"image_url": _safe_remote_url(t.get("image_url")),
                            "thumbnail_url": _safe_remote_url(t.get("thumbnail_url"))} for t in thumbs],
            "lat": row.get("lat"),
            "lon": row.get("lon"),
            "location": video_location(row),
            "flood_text_score": round(score, 3),
            "flood": is_flood(row),
            "ai_text": ai_by_text(row),
            "text_flood": score >= threshold,
            # None = not classified yet; otherwise True/False/None per the VLM.
            "visual_classified": visual is not None,
            "visual_flood": (visual or {}).get("flood"),
            "visual_frames": [f.get("answer") for f in (visual or {}).get("frames") or []],
            "available": bool(row.get("available")),
            "source_url": source_url,
            "embed_url": embed_url,
        })
    items.sort(key=lambda i: (i["published"], i["id"]), reverse=True)
    locations = Counter(i["location"] for i in items)
    return {
        **empty,
        "available": True,
        "summary": {
            "videos": len(items),
            "flood": sum(i["flood"] for i in items),
            "text_flood": sum(i["text_flood"] for i in items),
            "visual_classified": sum(i["visual_classified"] for i in items),
            "flood_threshold": threshold,
            "nyc": locations["nyc"],
            "ny_not_nyc": locations["ny"],
            "outside_ny": locations["outside"],
            "location_unknown": locations["unknown"],
            "with_coordinates": sum(1 for i in items if i["lat"] is not None),
            "unavailable": sum(1 for i in items if not i["available"]),
        },
        "items": items,
    }


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
#: scan_shots.frame_path(): <video_id>/shot<k, 3 digits, from 1>_<j, from 0>.jpg
FRAME_NAME_RE = re.compile(r"^shot\d{3}_\d\.jpg$")


def read_shot_ledger(folder: Path) -> list[dict]:
    """Finished scan lines, the latest per video (scan_shots.read_ledger's
    rule, minus the error rows, which have no shots to show)."""
    done: dict[str, dict] = {}
    for path in sorted(folder.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue                                  # a killed run's last line
            if "error" in row or not isinstance(row.get("shots"), list):
                continue
            prev = done.get(row["video_id"])
            if prev is None or row.get("scanned_at", "") >= prev.get("scanned_at", ""):
                done[row["video_id"]] = row
    return list(done.values())


def load_shot_catalog(shots_dir: Path, videos_by_id: dict[str, dict]) -> dict:
    """The shot page's data: one item per scanned video, its shots with their
    frames' answers, joined to the video's catalog row for title, labels and
    the player. Frame URLs are not listed -- the page builds them from
    `has_frames` and the shot / frame index (see FRAME_NAME_RE)."""
    frames_dir = shots_dir / "frames"
    items = []
    for row in read_shot_ledger(shots_dir):
        vid = str(row["video_id"])
        video = videos_by_id.get(vid, {})
        shots = [{"start": s["start"], "end": s["end"], "flood": bool(s.get("flood")), "ai": s.get("ai"),
                  "answers": [f.get("answer") for f in s.get("frames") or []],
                  "times": [f.get("t") for f in s.get("frames") or []]}
                 for s in row["shots"]]
        # YouTube's duration is whole seconds; the decoded video can run a
        # fraction longer, so the last shot's end is the better length.
        duration = max(float(row.get("duration_s") or 0), max((s["end"] for s in shots), default=0))
        flood_s = sum(s["end"] - s["start"] for s in shots if s["flood"])
        items.append({
            "video_id": vid,
            "title": str(row.get("title") or video.get("title") or "Untitled"),
            "duration_s": duration,
            "width": row.get("width"),
            "height": row.get("height"),
            "fps": row.get("fps"),
            "scanned_at": str(row.get("scanned_at") or ""),
            "published": video.get("published", ""),
            "tags": video.get("tags", []),
            "flood_text_score": video.get("flood_text_score"),
            "location": video.get("location", "unknown"),
            "source_url": f"https://www.youtube.com/watch?v={vid}",
            "embed_url": f"{VIDEO_EMBED_ORIGIN}/embed/{vid}",
            "cover_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "has_frames": (frames_dir / vid).is_dir(),
            "raw_shot_counts": {k: len(v) for k, v in (row.get("raw_shots") or {}).items()},
            "shots": shots,
            "n_shots": len(shots),
            # AI check (scans from 2026-10-15 on; None = not checked).
            "ai_share": row.get("ai_share"),
            "ai_video": row.get("ai_video"),
            "n_flood": sum(s["flood"] for s in shots),
            "flood_s": round(flood_s, 2),
            "flood_share": round(flood_s / duration, 4) if duration else 0.0,
        })
    items.sort(key=lambda i: i["scanned_at"], reverse=True)
    return {
        "available": bool(items),
        "source_dir": str(shots_dir),
        "summary": {
            "videos": len(items),
            "shots": sum(i["n_shots"] for i in items),
            "flood_shots": sum(i["n_flood"] for i in items),
            "videos_with_flood": sum(1 for i in items if i["n_flood"]),
            "hours": round(sum(i["duration_s"] for i in items) / 3600, 2),
            "flood_hours": round(sum(i["flood_s"] for i in items) / 3600, 2),
            "with_frames": sum(i["has_frames"] for i in items),
            "ai_checked": sum(i["ai_video"] is not None for i in items),
            "ai_videos": sum(i["ai_video"] is True for i in items),
        },
        "items": items,
    }


def is_valid_flood(item: dict) -> bool:
    """A video the site shows: at least one flood shot, and not judged
    AI-generated (the depth benchmark's rule; ai_video None = scanned before
    the AI check existed, kept)."""
    return item["n_flood"] > 0 and item["ai_video"] is not True


def depth_by_shot(depth_catalog: dict) -> tuple[dict, dict]:
    """(video_id -> {shot number -> {cond: answer}}, video_id -> label) from a
    depth run. C1 asked each frame on its own; its key-frame answer is the one
    shown, as on the old depth page."""
    shots: dict[str, dict[int, dict]] = {}
    labels: dict[str, dict] = {}
    for item in depth_catalog.get("items") or []:
        per = shots.setdefault(item["video_id"], {})
        for s in item["shots"]:
            c1 = s.get("C1")
            per[s["s"]] = {"C1": c1[DEPTH_KEY_FRAME] if isinstance(c1, list) and len(c1) > DEPTH_KEY_FRAME else c1,
                           **{c: s.get(c) for c in DEPTH_CONDS[1:]}}
        labels[item["video_id"]] = {c: item["label"][c].get("deepest") for c in DEPTH_CONDS}
    return shots, labels


def load_flood_video_catalog(shot_catalog: dict, depth_catalog: dict, videos_by_id: dict) -> dict:
    """The Videos tab: one item per valid flood video (is_valid_flood), its
    shots from the scan and, where the depth run covered a shot, that shot's
    answer under each condition."""
    depth_shots, depth_labels = depth_by_shot(depth_catalog)
    items = []
    for item in shot_catalog["items"]:
        if not is_valid_flood(item):
            continue
        vid = item["video_id"]
        per = depth_shots.get(vid, {})
        shots = [{**shot, "depth": per.get(k + 1)} for k, shot in enumerate(item["shots"])]
        items.append({**item, "shots": shots,
                      "description": videos_by_id.get(vid, {}).get("description", ""),
                      "depth_label": depth_labels.get(vid)})
    items.sort(key=lambda i: i["published"] or "", reverse=True)
    locations = Counter(i["location"] for i in items)
    return {
        "available": bool(items),
        "source_dir": shot_catalog["source_dir"],
        "depth": {"run_dir": depth_catalog.get("run_dir"), "model": (depth_catalog.get("run") or {}).get("model"),
                  "conditions": DEPTH_CONDS},
        "summary": {
            "videos": len(items),
            "scanned": shot_catalog["summary"]["videos"],
            "flood_shots": sum(i["n_flood"] for i in items),
            "flood_hours": round(sum(i["flood_s"] for i in items) / 3600, 2),
            "with_depth": sum(1 for i in items if i["depth_label"]),
            "nyc": locations["nyc"],
            "ny_not_nyc": locations["ny"],
            "outside_ny": locations["outside"],
            "location_unknown": locations["unknown"],
        },
        "items": items,
    }


class FloodVideoCatalog:
    """The Videos tab's payload, rebuilt only when the scan ledger or the
    depth run changes, so a reload during a running scan shows new videos
    without re-reading on every request."""

    def __init__(self, shots_dir: Path, depth_dir: Path, video_catalog: dict):
        import sys
        sys.path.insert(0, str(DEPTH_CODE))
        import depth_review
        self.shots_dir = shots_dir
        self.depth_dir = depth_dir
        self._depth = depth_review
        self.videos_by_id = {i["video_id"]: i for i in video_catalog["items"]}
        self._key = None
        self._payload = b""
        self._lock = threading.Lock()

    def payload(self) -> bytes:
        watched = [*sorted(self.shots_dir.glob("*.jsonl")), *self._depth.watched_files(self.depth_dir)]
        key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in watched if p.exists())
        with self._lock:
            if key != self._key:
                shots = load_shot_catalog(self.shots_dir, self.videos_by_id)
                depth = self._depth.load_depth_catalog(self.depth_dir)
                catalog = load_flood_video_catalog(shots, depth, self.videos_by_id)
                self._payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._key = key
            return self._payload


def make_handler(catalog: dict, videos: FloodVideoCatalog):
    payload = json.dumps(catalog, ensure_ascii=False).encode("utf-8")
    frames_dir = videos.shots_dir / "frames"

    class ReviewHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def do_GET(self):  # noqa: N802 - stdlib handler API
            path = self.path.split("?", 1)[0]
            if path == "/api/images":
                self._send(payload, "application/json; charset=utf-8")
                return
            if path == "/api/videos":
                self._send(videos.payload(), "application/json; charset=utf-8")
                return
            if path.startswith("/frames/"):
                parts = path.split("/")
                # Only <11-char id>/<frame name>: nothing else in the folder is served.
                if (len(parts) == 4 and VIDEO_ID_RE.match(parts[2]) and FRAME_NAME_RE.match(parts[3])
                        and (frames_dir / parts[2] / parts[3]).is_file()):
                    self._send((frames_dir / parts[2] / parts[3]).read_bytes(), "image/jpeg")
                else:
                    self.send_error(404)
                return
            if path == "/shared/styles.css":
                self._send(SHARED_STYLES.read_bytes(), "text/css; charset=utf-8")
                return
            super().do_GET()

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def end_headers(self):
            # Same headers as the news site: never cache (a stale app.js
            # against fresh markup fails silently), and images only over https.
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' https: data:; "
                "style-src 'self'; script-src 'self'; connect-src 'self'; "
                f"frame-src {VIDEO_EMBED_ORIGIN}",
            )
            super().end_headers()

    return ReviewHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--videos", type=Path, default=DEFAULT_VIDEOS,
                        help="YouTube videos: titles, dates, locations (optional; default %(default)s)")
    parser.add_argument("--shots", type=Path, default=DEFAULT_SHOTS,
                        help="shot-scan ledger folder (optional; default %(default)s)")
    parser.add_argument("--depth", type=Path, default=DEFAULT_DEPTH,
                        help="flood-depth run folder (optional; default %(default)s)")
    args = parser.parse_args()

    if not args.results.is_file():
        parser.error(
            f"No dataset at {args.results}. It is written by: "
            f"python3 scraping/api_based_scraping/gis_scrape.py"
        )
    for required in (STATIC_DIR, SHARED_STYLES):
        if not required.exists():
            parser.error(f"Missing web asset: {required}")

    catalog = load_catalog(args.results)
    videos = FloodVideoCatalog(args.shots, args.depth, load_video_catalog(args.videos))
    summary = catalog["summary"]
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(catalog, videos))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            parser.error(
                f"Port {args.port} is already in use, possibly by another user "
                f"on this machine. Re-run with --port <free port>."
            )
        raise
    print(f"MyCoast photos: {summary['images']} images ({summary['nyc']} NYC) at "
          f"http://{args.host}:{args.port}", flush=True)
    print(f"Flood videos: http://{args.host}:{args.port}/videos.html "
          f"(reads {args.shots} and {args.depth})", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

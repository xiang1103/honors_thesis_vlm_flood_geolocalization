#!/usr/bin/env python3
"""Serve the local browser for the GIS flood-photo dataset (New York State).

The news reviewer's sibling. Same look and the same three-way decision
(useful / reject / unsure), but it reads ``data/gis_flood_images.json``, written
by ``scraping/api_based_scraping/gis_scrape.py``, and shows what that data has
instead of news: source, event, coordinates and the NYC label.

Nothing here has been classified by a model yet, so every row is shown.

Images are never downloaded by Python; the browser loads the remote URLs
directly -- a small thumbnail where the source offers one (MyCoast, Commons),
falling back to the original.

    python3 gis_review_server.py              # http://127.0.0.1:8768
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS = PROJECT_DIR / "data" / "gis_flood_images.json"
STATIC_DIR = PROJECT_DIR / "gis_review_web"
#: The news site's stylesheet, served as-is so the two sites cannot drift apart
#: visually. GIS-only rules live in gis_review_web/gis.css.
SHARED_STYLES = PROJECT_DIR / "image_review_web" / "styles.css"

#: Decisions live only in the browser's localStorage, keyed by `review_id()`.
#: Bump this if that function changes, so the page can detect it loudly.
REVIEW_ID_SCHEME = "gis_review_v1"

SOURCE_LABELS = {
    "mycoast": "MyCoast",
    "usgs_stn": "USGS STN",
    "wikimedia_commons": "Commons",
    "napsg_photomappers": "NAPSG",
}

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


def make_handler(catalog: dict):
    payload = json.dumps(catalog, ensure_ascii=False).encode("utf-8")

    class ReviewHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def do_GET(self):  # noqa: N802 - stdlib handler API
            path = self.path.split("?", 1)[0]
            if path == "/api/images":
                self._send(payload, "application/json; charset=utf-8")
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
                "default-src 'self'; img-src https: data:; "
                "style-src 'self'; script-src 'self'; connect-src 'self'",
            )
            super().end_headers()

    return ReviewHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
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
    summary = catalog["summary"]
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(catalog))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            parser.error(
                f"Port {args.port} is already in use, possibly by another user "
                f"on this machine. Re-run with --port <free port>."
            )
        raise
    sources = ", ".join(f"{s['label']} {s['count']}" for s in summary["sources"])
    print(
        f"GIS image review: {summary['images']} images ({sources}; "
        f"{summary['nyc']} NYC) at http://{args.host}:{args.port}",
        flush=True,
    )
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

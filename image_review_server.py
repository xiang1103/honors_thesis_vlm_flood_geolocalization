#!/usr/bin/env python3
"""Serve a local browser for reviewing flood-news image URLs.

The server reads the existing ``data/outlets/*_flood.json`` files and exposes
only their metadata. Images are never downloaded by Python; the browser loads
the original remote URLs directly when a review page is visible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PROJECT_DIR / "data" / "outlets"
STATIC_DIR = PROJECT_DIR / "image_review_web"

# These are useful review hints, not ground-truth classifications.
NON_STREET_HINT = re.compile(
    r"\b(map|satellite|radar|forecast|file photo|archival|illustration|"
    r"graphic|chart|headshot|portrait|weather model|storm track)\b",
    re.IGNORECASE,
)


#: Bump when the shape of `review_id()` changes. The browser stores this
#: alongside the decisions it saved, so a future change is detected and
#: migrated loudly instead of silently presenting an empty review set.
REVIEW_ID_SCHEME = "human_review_v2"


def review_id(article_url: str, image_url: str) -> str:
    """Identity of one image-in-one-article, for HUMAN review decisions.

    Domain-separated on purpose. `verify_images_vlm.make_occurrence_id()`
    hashes the same two fields for the model's answers; without the literal
    prefix the two ids would be indistinguishable strings over identical
    inputs, and it would be far too easy to join or overwrite one with the
    other. The prefix makes them provably disjoint namespaces.

    Non-positional, for the same reason the VLM id is: Adapter.images()
    dedupes by URL within an article (adapters.py `seen`), so an image's
    index can never disambiguate anything -- it can only change when a
    publisher inserts a photo, which would strand a human's decision.
    """
    value = f"{REVIEW_ID_SCHEME}\n{article_url}\n{image_url}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def legacy_review_id(article_url: str, image_url: str, image_index: int) -> str:
    """The pre-v2 id: sha1 over a pipe-joined, POSITION-DEPENDENT identity.

    Kept solely so the browser can find decisions saved under the old scheme
    and carry them forward. Decisions live only in localStorage -- there is no
    server-side copy to migrate -- so removing this would silently orphan
    every review made before the change. Do not delete it.
    """
    identity = f"{article_url}|{image_url}|{image_index}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def _safe_remote_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def load_catalog(data_dir: Path) -> dict:
    """Load per-outlet arrays and flatten them into one record per image."""
    items: list[dict] = []
    article_count = 0

    for path in sorted(data_dir.glob("*_flood.json")):
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read {path}: {exc}") from exc
        if not isinstance(records, list):
            raise RuntimeError(f"Expected a JSON array in {path}")

        fallback_outlet = path.stem.removesuffix("_flood")
        article_count += len(records)
        for article_index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            article_url = _safe_remote_url(record.get("url"))
            images = record.get("images") or []
            if not isinstance(images, list):
                continue
            for image_index, image in enumerate(images):
                if not isinstance(image, dict):
                    image = {"url": image, "caption": "", "source": "unknown"}
                image_url = _safe_remote_url(image.get("url"))
                if not image_url:
                    continue
                caption = str(image.get("caption") or "").strip()
                items.append(
                    {
                        "id": review_id(article_url, image_url),
                        "legacy_id": legacy_review_id(
                            article_url, image_url, image_index
                        ),
                        "outlet": str(record.get("outlet") or fallback_outlet),
                        "article_title": str(record.get("title") or "Untitled article"),
                        "article_date": str(record.get("date") or ""),
                        "article_url": article_url,
                        "flood_score": record.get("flood_score"),
                        "flood_verified": bool(record.get("flood_verified")),
                        "image_url": image_url,
                        "caption": caption,
                        "image_source": str(image.get("source") or "unknown"),
                        "image_index": image_index + 1,
                        "article_image_count": len(images),
                        "suspect_nonstreet": bool(NON_STREET_HINT.search(caption)),
                        "source_file": path.name,
                        "article_index": article_index,
                    }
                )

    duplicate_counts = Counter(item["image_url"] for item in items)
    for item in items:
        item["duplicate_count"] = duplicate_counts[item["image_url"]]

    items.sort(
        key=lambda item: (item["article_date"], item["outlet"], item["article_title"]),
        reverse=True,
    )
    outlets = sorted({item["outlet"] for item in items})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review_id_scheme": REVIEW_ID_SCHEME,
        "summary": {
            "articles": article_count,
            "images": len(items),
            "unique_images": len(duplicate_counts),
            "verified_images": sum(item["flood_verified"] for item in items),
            "suspect_nonstreet": sum(item["suspect_nonstreet"] for item in items),
            "outlets": outlets,
        },
        "items": items,
    }


def make_handler(catalog: dict):
    class ReviewHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def do_GET(self):  # noqa: N802 - stdlib handler API
            if self.path.split("?", 1)[0] == "/api/images":
                payload = json.dumps(catalog, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            super().do_GET()

        def end_headers(self):
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    if not args.data_dir.is_dir():
        parser.error(f"Data directory does not exist: {args.data_dir}")
    if not STATIC_DIR.is_dir():
        parser.error(f"Web assets directory does not exist: {STATIC_DIR}")

    catalog = load_catalog(args.data_dir)
    summary = catalog["summary"]
    server = ThreadingHTTPServer((args.host, args.port), make_handler(catalog))
    print(
        f"Reviewing {summary['images']} image references from "
        f"{summary['articles']} articles at http://{args.host}:{args.port}",
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

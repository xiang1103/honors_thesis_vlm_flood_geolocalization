#!/usr/bin/env python3
"""Serve a local browser for reviewing flood-news image URLs.

The server reads ``data/verified_images_news.json`` -- the classified dataset,
not the raw corpus -- so every image shown has already been fetched, judged and
deduplicated. Each item carries the model's verdict, and the page defaults to
showing only images the model marked `yes`; the "Model verdict" control
toggles the rejected ones back in for auditing.

Images are never downloaded by Python; the browser loads the original remote
URLs directly when a review page is visible.
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
DEFAULT_RESULTS = PROJECT_DIR / "data" / "verified_images_news.json"
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

    Domain-separated on purpose. `verification/verify_images_vlm.py`'s make_occurrence_id()
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


def load_catalog(results_file: Path, include_rejected: bool = False) -> dict:
    """One catalog item per classified image.

    Reads the verification output rather than the corpus, so images that could
    not be fetched or were deduplicated away never reach the page at all --
    there is nothing to review about an image that is not in the dataset.

    Only images the model answered `yes` are served: this page reviews the
    dataset, and a rejected image is not part of it. Pass include_rejected
    (CLI: --include-rejected) to load the `no` rows too when auditing what the
    model threw out -- deliberately a launch flag rather than a UI control, so
    the page cannot drift into showing rejects by a stale select.
    """
    try:
        payload = json.loads(results_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {results_file}: {exc}") from exc
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"Expected a 'results' array in {results_file}")

    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        article_url = _safe_remote_url(row.get("article_url"))
        image_url = _safe_remote_url(row.get("image_url"))
        if not image_url:
            continue
        answer = str(row.get("answer") or "").strip().lower()
        if not include_rejected and answer != "yes":
            continue
        caption = str(row.get("caption") or "").strip()
        image_index = int(row.get("image_index") or 0)
        items.append(
            {
                "id": review_id(article_url, image_url),
                "legacy_id": legacy_review_id(article_url, image_url, image_index),
                "outlet": str(row.get("outlet") or "unknown"),
                "article_title": str(row.get("article_title") or "Untitled article"),
                "article_date": str(row.get("article_date") or ""),
                "article_url": article_url,
                "flood_score": row.get("article_flood_score"),
                "flood_verified": bool(row.get("article_flood_verified")),
                "image_url": image_url,
                "caption": caption,
                "image_source": str(row.get("image_source") or "unknown"),
                "image_index": image_index + 1,
                "model_answer": answer,
                "model_output": str(row.get("model_output") or "").strip(),
                "suspect_nonstreet": bool(NON_STREET_HINT.search(caption)),
                "source_file": results_file.name,
            }
        )

    # Derived from the rows themselves, since there is no corpus here to ask.
    # After dedupe.py every image is unique, so duplicate_count is normally 1 --
    # it stays so the "repeated image" filter keeps working if duplicates return.
    per_article = Counter(item["article_url"] for item in items)
    duplicate_counts = Counter(item["image_url"] for item in items)
    for item in items:
        item["article_image_count"] = per_article[item["article_url"]]
        item["duplicate_count"] = duplicate_counts[item["image_url"]]

    items.sort(
        key=lambda item: (item["article_date"], item["outlet"], item["article_title"]),
        reverse=True,
    )
    outlets = sorted({item["outlet"] for item in items})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review_id_scheme": REVIEW_ID_SCHEME,
        "prompt": payload.get("current_prompt") or payload.get("prompt"),
        "summary": {
            "articles": len(per_article),
            "images": len(items),
            "unique_images": len(duplicate_counts),
            "model_yes": sum(1 for i in items if i["model_answer"] == "yes"),
            "model_no": sum(1 for i in items if i["model_answer"] == "no"),
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
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument(
        "--include-rejected",
        action="store_true",
        help="also load images the model answered 'no' (default: yes only).",
    )
    args = parser.parse_args()

    if not args.results.is_file():
        parser.error(
            f"No dataset at {args.results}. It is written by: "
            f"python3 verification/verify_images_vlm.py"
        )
    if not STATIC_DIR.is_dir():
        parser.error(f"Web assets directory does not exist: {STATIC_DIR}")

    catalog = load_catalog(args.results, include_rejected=args.include_rejected)
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

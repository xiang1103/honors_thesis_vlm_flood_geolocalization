#!/usr/bin/env python3
"""Serve the local browser for the flood-news image dataset.

One site, one command. It was two -- a human-labelling page on :8765 and a
model-results browser on :8766 -- which read the SAME file, rendered the same
images, and drifted apart in what they showed about them. They are merged here:
the model's verdict, the New York judgement and the human's decision are three
facts about one image, so they belong on one card.

Reads ``data/verified_images_news.json`` (the classified dataset, not the raw
corpus), so every image shown has already been fetched, judged and
deduplicated. New York labels from ``data/nyc_scraped_images.json`` are joined
on when that file exists.

Images are never downloaded by Python; the browser loads the original remote
URLs directly.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS = PROJECT_DIR / "data" / "verified_images_news.json"
DEFAULT_NYC_FILE = PROJECT_DIR / "data" / "nyc_scraped_images.json"
STATIC_DIR = PROJECT_DIR / "image_review_web"

#: Bump when the shape of `review_id()` changes. The browser stores this
#: alongside the decisions it saved, so a future change is detected and
#: migrated loudly instead of silently presenting an empty review set.
REVIEW_ID_SCHEME = "human_review_v2"

#: New York verdicts worth showing. They are joined on `occurrence_id`, which
#: both files inherit from the same verifier rows -- NOT on `id` below, which
#: is a different namespace on purpose (see review_id).
NYC_LABELS = ("nyc", "nyc_metro_not_nyc")


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

    Unchanged by the merge, deliberately. Decisions live only in the browser's
    localStorage; a new scheme would orphan every one of them.
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


def load_nyc_labels(nyc_file: Path) -> dict[str, dict]:
    """occurrence_id -> New York verdict, or {} when the file is absent.

    Deliberately NOT fatal: the site is useful without the New York pass, and
    verification/filter_nyc.py is an optional, separately run stage. A missing
    file disables that one filter rather than taking the whole site down.
    """
    if not nyc_file.is_file():
        return {}
    try:
        payload = json.loads(nyc_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: ignoring {nyc_file}: {exc}", flush=True)
        return {}
    labels: dict[str, dict] = {}
    for row in payload.get("results") or []:
        occurrence_id = str(row.get("occurrence_id") or "")
        label = str(row.get("nyc_label") or "").strip().lower()
        if occurrence_id and label in NYC_LABELS:
            labels[occurrence_id] = {
                "nyc_label": label,
                "nyc_confidence": str(row.get("nyc_confidence") or ""),
                "nyc_place": str(row.get("nyc_place") or ""),
                "nyc_evidence": str(row.get("nyc_evidence") or ""),
            }
    return labels


def load_catalog(results_file: Path, nyc_labels: dict[str, dict] | None = None) -> dict:
    """One catalog item per classified image, carrying every fact about it.

    ALL rows are loaded, `no` included. The old labelling page loaded only
    `yes` rows and hid the rest behind a launch flag, so that the page could
    not drift into showing rejects by way of a stale select. The merged page
    keeps that guarantee differently, and more visibly: the answer filter opens
    on `yes` (see --answer) and every card carries its YES/NO badge, so a
    rejected image is never mistakable for part of the dataset.
    """
    try:
        payload = json.loads(results_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {results_file}: {exc}") from exc
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"Expected a 'results' array in {results_file}")

    nyc_labels = nyc_labels or {}
    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        article_url = _safe_remote_url(row.get("article_url"))
        image_url = _safe_remote_url(row.get("image_url"))
        answer = str(row.get("answer") or "").strip().lower()
        if not image_url or answer not in {"yes", "no"}:
            continue
        image_index = int(row.get("image_index") or 0)
        occurrence_id = str(row.get("occurrence_id") or "")
        nyc = nyc_labels.get(occurrence_id, {})
        items.append(
            {
                # `id` is the HUMAN review id and nothing else: it is what the
                # browser's saved decisions are keyed by.
                "id": review_id(article_url, image_url),
                "legacy_id": legacy_review_id(article_url, image_url, image_index),
                "occurrence_id": occurrence_id,
                "outlet": str(row.get("outlet") or "unknown"),
                "article_title": str(row.get("article_title") or "Untitled article"),
                "article_date": str(row.get("article_date") or ""),
                "article_url": article_url,
                "article_flood_score": row.get("article_flood_score"),
                "article_flood_verified": bool(row.get("article_flood_verified")),
                "image_url": image_url,
                "caption": str(row.get("caption") or "").strip(),
                "image_source": str(row.get("image_source") or "unknown"),
                "image_index": image_index + 1,
                "model_answer": answer,
                "model_output": str(row.get("model_output") or "").strip(),
                "model": str(row.get("model") or ""),
                "attempts": int(row.get("attempts") or 0),
                "verified_at": str(row.get("verified_at") or ""),
                "nyc_label": nyc.get("nyc_label", ""),
                "nyc_confidence": nyc.get("nyc_confidence", ""),
                "nyc_place": nyc.get("nyc_place", ""),
                "nyc_evidence": nyc.get("nyc_evidence", ""),
                "source_file": results_file.name,
            }
        )

    # Derived from the rows themselves, since there is no corpus here to ask.
    # After dedupe.py every image is unique, so duplicate_count is normally 1 --
    # it stays so the "repeated image" cue keeps working if duplicates return.
    per_article = Counter(item["article_url"] for item in items)
    duplicate_counts = Counter(item["image_url"] for item in items)
    for item in items:
        item["article_image_count"] = per_article[item["article_url"]]
        item["duplicate_count"] = duplicate_counts[item["image_url"]]

    items.sort(
        key=lambda item: (item["article_date"], item["outlet"], item["article_title"]),
        reverse=True,
    )
    outlets = sorted({item["outlet"] for item in items}, key=str.casefold)
    nyc_counts = Counter(item["nyc_label"] for item in items if item["nyc_label"])
    model_yes = sum(1 for i in items if i["model_answer"] == "yes")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review_id_scheme": REVIEW_ID_SCHEME,
        "prompt": payload.get("current_prompt") or payload.get("prompt"),
        "summary": {
            "articles": len(per_article),
            "images": len(items),
            "unique_images": len(duplicate_counts),
            "model_yes": model_yes,
            "model_no": len(items) - model_yes,
            "outlets": outlets,
            "nyc_available": bool(nyc_labels),
            "nyc": nyc_counts.get("nyc", 0),
            "nyc_metro": nyc_counts.get("nyc_metro_not_nyc", 0),
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
            # Never cache: this is a local dev server whose HTML/JS change
            # constantly. A browser holding a stale app.js against fresh markup
            # fails silently -- the script dies on a missing element and the
            # page sits on its initial "Loading..." text forever.
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument(
        "--nyc-file",
        type=Path,
        default=DEFAULT_NYC_FILE,
        help="New York labels from verification/filter_nyc.py. Optional: if it "
             "is missing the site loads with the New York filter disabled.",
    )
    parser.add_argument(
        "--answer",
        choices=("yes", "no", "all"),
        default="yes",
        help="Which model verdict the page opens on (default: yes, the dataset "
             "itself). All rows are always loaded; this only sets the starting "
             "filter, which the page then shows and lets you change.",
    )
    args = parser.parse_args()

    if not args.results.is_file():
        parser.error(
            f"No dataset at {args.results}. It is written by: "
            f"python3 verification/verify_images_vlm.py"
        )
    if not STATIC_DIR.is_dir():
        parser.error(f"Web assets directory does not exist: {STATIC_DIR}")

    nyc_labels = load_nyc_labels(args.nyc_file)
    catalog = load_catalog(args.results, nyc_labels)
    catalog["default_answer"] = args.answer
    summary = catalog["summary"]

    if nyc_labels:
        nyc_note = f"{summary['nyc']} NYC + {summary['nyc_metro']} NYC metro"
    else:
        nyc_note = f"no New York labels ({args.nyc_file.name} not found)"
    # This is a shared machine: another user's process may already hold the
    # port, and the bare OSError traceback does not say so. Name the cause and
    # the fix instead.
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(catalog))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            parser.error(
                f"Port {args.port} is already in use, possibly by another user "
                f"on this machine. Re-run with --port <free port>."
            )
        raise
    print(
        f"Image review: {summary['images']} images from "
        f"{summary['articles']} articles "
        f"({summary['model_yes']} yes, {summary['model_no']} no; {nyc_note}) "
        f"at http://{args.host}:{args.port}",
        flush=True,
    )
    print(f"Opening on model answer = {args.answer}. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

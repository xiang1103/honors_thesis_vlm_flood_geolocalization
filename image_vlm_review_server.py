#!/usr/bin/env python3
"""Serve a separate local website for exploring Qwen flood-image results.

The server reads the completed VLM result file and serves only JSON metadata
and static web assets. Remote news images are loaded directly by the browser;
this program never downloads or proxies them.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_FILE = PROJECT_DIR / "data" / "image_verification.json"
STATIC_DIR = PROJECT_DIR / "image_vlm_review_web"


def _safe_remote_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def load_results(results_file: Path) -> dict:
    """Load and compact the canonical verification results for the browser."""
    try:
        payload = json.loads(results_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {results_file}: {exc}") from exc

    source_rows = payload.get("results")
    if not isinstance(source_rows, list):
        raise RuntimeError(f"Expected a 'results' array in {results_file}")

    url_counts = Counter(
        url for row in source_rows if (url := _safe_remote_url(row.get("image_url")))
    )
    items: list[dict] = []
    for row in source_rows:
        image_url = _safe_remote_url(row.get("image_url"))
        answer = str(row.get("answer") or "").strip().lower()
        if not image_url or answer not in {"yes", "no"}:
            continue
        items.append(
            {
                "id": str(row.get("occurrence_id") or ""),
                "answer": answer,
                "model_output": str(row.get("model_output") or "").strip(),
                "model": str(row.get("model") or ""),
                "image_url": image_url,
                "caption": str(row.get("caption") or "").strip(),
                "image_source": str(row.get("image_source") or "unknown"),
                "image_index": int(row.get("image_index") or 0) + 1,
                "article_title": str(row.get("article_title") or "Untitled article"),
                "article_url": _safe_remote_url(row.get("article_url")),
                "article_date": str(row.get("article_date") or ""),
                "article_flood_score": row.get("article_flood_score"),
                "article_flood_verified": bool(row.get("article_flood_verified")),
                "outlet": str(row.get("outlet") or "unknown"),
                "duplicate_count": url_counts[image_url],
                "reused_for_duplicate_url": bool(row.get("reused_for_duplicate_url")),
                "attempts": int(row.get("attempts") or 0),
                "verified_at": str(row.get("verified_at") or ""),
            }
        )

    items.sort(
        key=lambda item: (item["article_date"], item["outlet"], item["article_title"]),
        reverse=True,
    )
    yes_count = sum(item["answer"] == "yes" for item in items)
    no_count = sum(item["answer"] == "no" for item in items)
    outlets = sorted({item["outlet"] for item in items}, key=str.casefold)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prompt": str(payload.get("prompt") or ""),
        "summary": {
            "total": len(items),
            "yes": yes_count,
            "no": no_count,
            "yes_rate": round(yes_count / len(items) * 100, 1) if items else 0,
            "unique_images": len(url_counts),
            "outlets": outlets,
        },
        "items": items,
    }


def make_handler(catalog: dict):
    class ResultsHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def do_GET(self):  # noqa: N802 - stdlib handler API
            if self.path.split("?", 1)[0] == "/api/results":
                body = json.dumps(catalog, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
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

    return ResultsHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_FILE)
    args = parser.parse_args()

    if not args.results_file.is_file():
        parser.error(f"Results file does not exist: {args.results_file}")
    if not STATIC_DIR.is_dir():
        parser.error(f"Web assets directory does not exist: {STATIC_DIR}")

    catalog = load_results(args.results_file)
    summary = catalog["summary"]
    server = ThreadingHTTPServer((args.host, args.port), make_handler(catalog))
    print(
        f"VLM review: {summary['total']} results "
        f"({summary['yes']} yes, {summary['no']} no) at "
        f"http://{args.host}:{args.port}",
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

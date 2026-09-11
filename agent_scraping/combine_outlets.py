#!/usr/bin/env python3
"""Merge the per-outlet crawl files into one canonical corpus.

    data/outlets/<outlet>_flood.json  x29   ->   data/news_scrape_results.json

The per-outlet files stay the crawl's WORKING files: scrape.py resumes from
them and finalizes each one atomically, so a crash while crawling one outlet
cannot damage the other 28. This merge runs at the end of a crawl and produces
the single file everything downstream reads.

Which means the combined file is DERIVED. Never hand-edit it -- edit or
re-crawl the outlet file and merge again, or the next crawl silently reverts
your change.

    python3 agent_scraping/combine_outlets.py
    python3 agent_scraping/combine_outlets.py --outlets-dir data/outlets --dest data/x.json
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_OUTLETS_DIR = PROJECT / "data" / "outlets"
DEFAULT_DEST = PROJECT / "data" / "news_scrape_results.json"


def combine(outlets_dir: Path, dest: Path, quiet: bool = False) -> dict[str, Any]:
    """Merge every <outlet>_flood.json into one array. Returns stats."""
    by_url: dict[str, dict] = {}
    per_outlet: dict[str, int] = {}
    collisions = 0

    for path in sorted(outlets_dir.glob("*_flood.json")):
        name = path.stem.removesuffix("_flood")
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read {path}: {exc}") from exc
        if not isinstance(records, list):
            raise RuntimeError(f"Expected a JSON array in {path}")

        kept = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            url = str(record.get("url") or "")
            if not url:
                continue
            # An article URL is the identity of an article. The same URL in two
            # outlet files means one of them mis-attributed it; keep the first
            # and count it rather than emitting the article twice.
            if url in by_url:
                collisions += 1
                continue
            record.setdefault("outlet", name)
            by_url[url] = record
            kept += 1
        per_outlet[name] = kept

    # Newest first, matching the per-outlet files' own ordering.
    articles = sorted(by_url.values(), key=lambda r: (r.get("date") or ""),
                      reverse=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(articles, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, dest)          # atomic: never a half-written corpus

    stats = {
        "outlets": len(per_outlet),
        "articles": len(articles),
        "images": sum(len(a.get("images") or []) for a in articles),
        "videos": sum(len(a.get("videos") or []) for a in articles),
        "flood_verified": sum(1 for a in articles if a.get("flood_verified")),
        "duplicate_urls_skipped": collisions,
        "path": str(dest),
    }
    if not quiet:
        print(f"combined {stats['outlets']} outlets -> {dest}")
        print(f"  articles={stats['articles']} images={stats['images']} "
              f"videos={stats['videos']} flood_verified={stats['flood_verified']}"
              + (f" duplicate_urls_skipped={collisions}" if collisions else ""))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outlets-dir", type=Path, default=DEFAULT_OUTLETS_DIR)
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = ap.parse_args()
    if not args.outlets_dir.is_dir():
        raise SystemExit(f"No such directory: {args.outlets_dir}")
    combine(args.outlets_dir, args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

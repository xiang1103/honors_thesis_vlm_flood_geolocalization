#!/usr/bin/env python3
"""Write data/meta_data.json: a snapshot of the published dataset.

    python3 make_metadata.py

Every count comes from `data/image_verification.json` alone, so the numbers
describe the DATASET, not the crawl. That file already excludes dead URLs (a
fetch failure is never written as a completed row) and exact duplicates
(removed by dedupe.py), so nothing here subtracts them and no field reports a
gap -- which is what made the earlier version confusing to read.

The consequence to keep in mind: `articles` counts articles that contributed at
least one image to the dataset. Articles the crawl collected that carry no
usable image -- text-only outlets, the feed-only majors -- are in
`news_scrape_results.json` and deliberately not here. For the size of the
collection rather than the dataset, read the corpus.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
DEFAULT_VERIFICATION = PROJECT / "data" / "image_verification.json"
DEFAULT_DEST = PROJECT / "data" / "meta_data.json"


def build(verification_path: Path) -> dict:
    payload = json.loads(verification_path.read_text(encoding="utf-8"))
    rows = payload.get("results") or []

    per: dict[str, dict] = defaultdict(
        lambda: {"articles": 0, "images": 0, "yes": 0, "no": 0}
    )
    # Rows are one per IMAGE, so an article appears once per image it carries.
    # Articles therefore have to be counted as distinct URLs, not as rows.
    articles_by_outlet: dict[str, set] = defaultdict(set)
    all_articles: set[str] = set()

    for row in rows:
        outlet = str(row.get("outlet") or "unknown")
        per[outlet]["images"] += 1
        answer = row.get("answer")
        if answer in ("yes", "no"):
            per[outlet][answer] += 1
        article_url = row.get("article_url")
        if article_url:
            articles_by_outlet[outlet].add(str(article_url))
            all_articles.add(str(article_url))

    for outlet, urls in articles_by_outlet.items():
        per[outlet]["articles"] = len(urls)

    totals = {
        "articles": len(all_articles),
        "images": len(rows),
        "yes": sum(1 for r in rows if r.get("answer") == "yes"),
        "no": sum(1 for r in rows if r.get("answer") == "no"),
        "outlets": len(per),
    }
    if totals["images"]:
        totals["yes_rate"] = round(totals["yes"] / totals["images"], 4)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Carried through so the snapshot says which criteria produced the
        # yes/no counts -- they are meaningless without it.
        "prompt": payload.get("current_prompt") or payload.get("prompt"),
        "totals": totals,
        "by_outlet": {k: dict(per[k]) for k in sorted(per)},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verification", type=Path, default=DEFAULT_VERIFICATION)
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = ap.parse_args()

    if not args.verification.is_file():
        raise SystemExit(f"missing: {args.verification}")

    meta = build(args.verification)
    tmp = args.dest.with_name(args.dest.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, args.dest)

    t = meta["totals"]
    print(f"wrote {args.dest}")
    print(f"  articles {t['articles']} | images {t['images']} "
          f"| yes {t['yes']} no {t['no']} | outlets {t['outlets']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

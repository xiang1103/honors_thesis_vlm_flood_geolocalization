#!/usr/bin/env python3
"""Write data/meta_data.json: a snapshot of the published dataset.

    python3 make_metadata.py

Every count comes from `data/verified_images_news.json` alone, so the numbers
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
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
DEFAULT_VERIFICATION = PROJECT / "data" / "verified_images_news.json"
DEFAULT_CORPUS = PROJECT / "data" / "news_scrape_results.json"
DEFAULT_DEST = PROJECT / "data" / "meta_data.json"


DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_date(value):
    """Tolerant ISO-ish date -> naive datetime, or None.

    Outlets emit offsets without a colon ('2026-08-30T07:04:00-0400'), which
    fromisoformat rejects before 3.11, so fall back to a plain YYYY-MM-DD
    match rather than dropping the record.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    match = DATE_RE.search(str(value))
    if match:
        try:
            return datetime(*(int(g) for g in match.groups()))
        except ValueError:
            return None
    return None


def scraped_stats(corpus_path: Path) -> dict:
    """What the CRAWL collected: size and publication span of the corpus.

    Read from `news_scrape_results.json`, not the verification file, because
    that is the only record of what was scraped -- the dataset is a filtered
    subset of it. The two blocks in this file therefore answer different
    questions and are deliberately not comparable line by line.

    The span is MEASURED, never taken from the crawl's --since-days flag: that
    flag filters what a single run keeps and does not remove older articles
    already collected, so the two disagree (the corpus reaches back 14 years
    while the last crawl ran with a 10-year window).
    """
    if not Path(corpus_path).is_file():
        return {}
    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    images = sum(len(a.get("images") or []) for a in corpus)
    dates = [d for d in (parse_date(a.get("date")) for a in corpus) if d]
    stats = {
        "articles": len(corpus),
        "images": images,
        "articles_with_images": sum(1 for a in corpus if a.get("images")),
        "outlets": len({a.get("outlet") for a in corpus if a.get("outlet")}),
    }
    if dates:
        earliest, latest = min(dates), max(dates)
        span = (latest - earliest).days
        stats.update({
            "earliest": earliest.date().isoformat(),
            "latest": latest.date().isoformat(),
            "span_days": span,
            "span_years": round(span / 365.25, 1),
            "undated": len(corpus) - len(dates),
        })
    return stats


def build(verification_path: Path, corpus_path: Path = None) -> dict:
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

    verified = {
        "articles": len(all_articles),
        "images": len(rows),
        "yes": sum(1 for r in rows if r.get("answer") == "yes"),
        "no": sum(1 for r in rows if r.get("answer") == "no"),
        "outlets": len(per),
    }
    if verified["images"]:
        verified["yes_rate"] = round(verified["yes"] / verified["images"], 4)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Carried through so the snapshot says which criteria produced the
        # yes/no counts -- they are meaningless without it.
        "prompt": payload.get("current_prompt") or payload.get("prompt"),
        # Two blocks, two questions: what the crawl collected, and what
        # survived into the dataset. Never merge them into one set of totals.
        "scraped": scraped_stats(corpus_path or DEFAULT_CORPUS),
        "verified": verified,
        "by_outlet": {k: dict(per[k]) for k in sorted(per)},
    }


def refresh(verification_path: Path = DEFAULT_VERIFICATION,
            dest: Path = DEFAULT_DEST, quiet: bool = False,
            corpus_path: Path = None) -> dict | None:
    """Regenerate the snapshot. Returns the metadata, or None if there is
    nothing to describe yet.

    Called at the end of a crawl, a verification run and a dedupe, so the file
    can never quietly describe an older state of the dataset. A missing
    verification file is NOT an error -- on a fresh checkout the first crawl
    finishes before anything has been classified.
    """
    verification_path, dest = Path(verification_path), Path(dest)
    if not verification_path.is_file():
        return None

    meta = build(verification_path, corpus_path or DEFAULT_CORPUS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, dest)

    if not quiet:
        v, sc = meta["verified"], meta["scraped"]
        print(f"metadata: {dest}")
        if sc:
            print(f"  scraped  {sc['articles']} articles | {sc['images']} images "
                  f"| {sc.get('earliest')} -> {sc.get('latest')} "
                  f"({sc.get('span_years')} years)")
        print(f"  verified {v['articles']} articles | {v['images']} images "
              f"| yes {v['yes']} no {v['no']} | outlets {v['outlets']}")
    return meta


def refresh_quietly(verification_path: Path = DEFAULT_VERIFICATION,
                    dest: Path = DEFAULT_DEST) -> None:
    """refresh() that can never take down its caller.

    The metadata is a derived convenience; a crawl that spent an hour
    collecting articles must not fail at the last step because a summary file
    could not be written.
    """
    try:
        refresh(verification_path, dest)
    except Exception as exc:                      # noqa: BLE001
        print(f"warning: could not update metadata ({type(exc).__name__}: {exc})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verification", type=Path, default=DEFAULT_VERIFICATION)
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = ap.parse_args()

    if not args.verification.is_file():
        raise SystemExit(f"missing: {args.verification}")
    refresh(args.verification, args.dest, corpus_path=args.corpus)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

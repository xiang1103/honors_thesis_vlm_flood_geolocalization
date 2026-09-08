#!/usr/bin/env python3
"""Flood-news dataset scraper (direct from US outlets).

Discovery + extraction are outlet-specific (see adapters.py); this module holds
the generic crawl loop: polite fetching with backoff, article parsing, flood
scoring, and resumable JSONL output.

Design rationale + the outlet bake-off that selected CBS: see design.md.

Usage
-----
  PY=/home/liu47/miniconda3/bin/python3

  # smoke test (2 listing pages)
  $PY scrape.py --outlet cbs --max-pages 2 --out data/cbs_flood.jsonl

  # full crawl, resumable -- safe to Ctrl-C and re-run
  $PY scrape.py --outlet cbs --max-pages 120 --resume --out data/cbs_flood.jsonl

  # add other outlets into the same file
  $PY scrape.py --outlet ap  --resume --out data/cbs_flood.jsonl

Record schema (one JSON object per line):
  {url, outlet, title, date, text, images:[{url,caption,source}],
   flood_score, flood_verified, scraped_at}
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests
import trafilatura
from lxml import html as lhtml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters import get_adapter          # noqa: E402
from verify import score_record           # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(session, url, retries=3, timeout=25):
    """GET with backoff. Returns Response or None.

    Per the task brief, bot blocks are NOT fatal: 403/429/5xx are retried a
    couple of times, then logged and skipped so the crawl continues.
    """
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429, 500, 502, 503, 504):
                wait = (2 ** attempt) + random.uniform(0, 1.0)
                log(f"  HTTP {r.status_code} on {url[:70]} -- retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            log(f"  HTTP {r.status_code} on {url[:70]} -- skip")
            return None
        except requests.RequestException as e:
            wait = (2 ** attempt) + random.uniform(0, 1.0)
            log(f"  {type(e).__name__} on {url[:70]} -- retry in {wait:.1f}s")
            time.sleep(wait)
    log(f"  giving up on {url[:70]}")
    return None


def jsonld_objects(doc):
    for s in doc.xpath("//script[@type='application/ld+json']/text()"):
        try:
            d = json.loads(s)
        except Exception:
            continue
        for o in (d if isinstance(d, list) else [d]):
            if isinstance(o, dict):
                yield o
                for g in o.get("@graph", []) or []:
                    if isinstance(g, dict):
                        yield g


#: JSON-LD @type values that actually carry an article headline. Without this
#: filter the Organization/WebSite node wins and every title becomes "CBS News".
ARTICLE_TYPES = {"newsarticle", "article", "reportagenewsarticle",
                 "reportage", "blogposting", "liveblogposting", "webpage"}


def _is_article_node(o):
    t = o.get("@type") or ""
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower() in ARTICLE_TYPES for x in types)


def extract_title_date(doc):
    title, date = None, None
    for o in jsonld_objects(doc):
        if not _is_article_node(o):
            continue
        t = o.get("headline") or o.get("name")
        if t and not title and isinstance(t, str):
            title = t.strip()
        d = o.get("datePublished") or o.get("dateCreated")
        if d and not date and isinstance(d, str):
            date = d.strip()
    # og:title is per-article on every outlet tested; <title> is often the
    # bare site name, so it is the last resort.
    if not title:
        og = doc.xpath("//meta[@property='og:title']/@content")
        if og and og[0].strip():
            title = og[0].strip()
    if not title:
        h1 = doc.xpath("//h1//text()")
        if h1:
            title = " ".join(t.strip() for t in h1 if t.strip()) or None
    if not title:
        title = (doc.xpath("//title/text()") or [""])[0].strip() or None
    if not date:
        m = doc.xpath("//meta[@property='article:published_time']/@content")
        if m:
            date = m[0].strip()
        else:
            t = doc.xpath("//time/@datetime")
            date = t[0].strip() if t else None
    if title:
        title = re.sub(r"\s+", " ", title)
    return title, date


def clean_text(txt):
    if not txt:
        return ""
    txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", txt)   # trafilatura image markers
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def scrape_article(session, url, adapter):
    r = fetch(session, url)
    if r is None:
        return None
    try:
        doc = lhtml.fromstring(r.content)
    except Exception as e:
        log(f"  parse error {type(e).__name__} on {url[:70]}")
        return None
    title, date = extract_title_date(doc)
    text = clean_text(trafilatura.extract(r.text, include_comments=False) or "")
    images = adapter.images(doc, url)
    rec = {
        "url": url,
        "outlet": adapter.name,
        "title": title,
        "date": date,
        "text": text,
        "images": images,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return score_record(rec)


def load_seen(path):
    seen = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(json.loads(line)["url"])
                except Exception:
                    pass
    return seen


def download_images(rec, image_dir, session):
    import hashlib
    os.makedirs(image_dir, exist_ok=True)
    for im in rec.get("images", []):
        u = im["url"]
        name = hashlib.sha1(u.encode()).hexdigest()[:16]
        ext = os.path.splitext(u.split("?")[0])[1][:5] or ".jpg"
        dest = os.path.join(image_dir, name + ext)
        if os.path.exists(dest):
            im["local_path"] = dest
            continue
        r = fetch(session, u, retries=2, timeout=30)
        if r is None:
            continue
        with open(dest, "wb") as fh:
            fh.write(r.content)
        im["local_path"] = dest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outlet", default="cbs", help="cbs (primary) | ap | nbc")
    ap.add_argument("--max-pages", type=int, default=5, help="listing pages to walk")
    ap.add_argument("--limit", type=int, default=0, help="stop after N articles (0=no limit)")
    ap.add_argument("--out", default="data/flood_articles.jsonl")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between article fetches")
    ap.add_argument("--resume", action="store_true", help="skip URLs already in --out")
    ap.add_argument("--download-images", action="store_true")
    ap.add_argument("--image-dir", default="data/images")
    ap.add_argument("--min-images", type=int, default=0,
                    help="only keep articles with >= this many captioned images")
    args = ap.parse_args()

    adapter = get_adapter(args.outlet)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    seen = load_seen(args.out) if args.resume else set()
    if seen:
        log(f"resume: {len(seen)} URLs already in {args.out}")

    session = requests.Session()

    # ---- phase 1: discovery
    urls, empty_streak = [], 0
    for page in range(1, args.max_pages + 1):
        listing = adapter.discover_url(page)
        if listing is None:
            break
        r = fetch(session, listing)
        if r is None:
            log(f"listing page {page}: unreachable -- stopping discovery")
            break
        try:
            found = adapter.article_links(lhtml.fromstring(r.content))
        except Exception:
            found = []
        new = [u for u in dict.fromkeys(found) if u not in seen and u not in urls]
        urls.extend(new)
        log(f"listing page {page}: {len(found)} links, {len(new)} new (total {len(urls)})")
        # CBS occasionally serves an empty river mid-range; tolerate a few.
        empty_streak = empty_streak + 1 if not found else 0
        if empty_streak >= 4:
            log("4 consecutive empty listing pages -- assuming end of feed")
            break
        time.sleep(args.delay * 0.5)

    if args.limit:
        urls = urls[:args.limit]
    log(f"discovery complete: {len(urls)} articles to fetch")

    # ---- phase 2: extraction (append as we go, so Ctrl-C keeps progress)
    kept = skipped = 0
    with open(args.out, "a") as fh:
        for i, u in enumerate(urls, 1):
            rec = scrape_article(session, u, adapter)
            if rec is None:
                skipped += 1
            elif len(rec["images"]) < args.min_images:
                skipped += 1
                log(f"[{i}/{len(urls)}] too few images -- skip {u[:60]}")
            else:
                if args.download_images:
                    download_images(rec, args.image_dir, session)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                kept += 1
                log(f"[{i}/{len(urls)}] OK imgs={len(rec['images'])} "
                    f"score={rec['flood_score']:.2f} {(rec['title'] or '')[:52]}")
            time.sleep(args.delay)

    log(f"done: kept={kept} skipped={skipped} -> {args.out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Flood-news dataset scraper (direct from US outlets).

Discovery + extraction are outlet-specific (see adapters.py); this module holds
the generic crawl loop: polite fetching with backoff, article parsing, flood
scoring, and resumable JSONL output.

Design rationale + the outlet bake-off that selected CBS: see design.md.

Usage
-----
  PY=/home/liu47/conda_envs/newEnv_local/bin/python3

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
from datetime import datetime, timedelta, timezone

import requests
import trafilatura
from lxml import html as lhtml
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# The text scorer lives in verification/ with the image verifier and the
# deduplicator -- scoring what was scraped is a verification concern, not a
# crawling one. The crawl still calls it inline, per-article, as it always did.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "verification"))
from adapters import ADAPTERS, IMAGE_OUTLETS, get_adapter   # noqa: E402
from verify_text import score_record       # noqa: E402

sys.path.insert(0, os.path.dirname(_HERE))
from make_metadata import refresh_quietly    # noqa: E402

#: The one file every outlet's crawl merges into. Per-outlet JSONs are gone:
#: the JSONL is still per-outlet (crash safety during a run) but it is merged
#: straight into this corpus, which is also what --resume reads.
DEFAULT_CORPUS = Path(__file__).resolve().parents[1] / "data" / "news_scrape_results.json"

#: Canonical key order for every emitted record. Human-facing fields first
#: (title/outlet/date), then the payload, then machine metadata. Python dicts
#: preserve insertion order and json.dump respects it, so writing through
#: `ordered()` is what actually fixes the on-disk field order.
FIELD_ORDER = ["title", "outlet", "date", "url", "text", "images", "videos",
               "flood_score", "flood_verified", "scraped_at"]


def ordered(rec):
    """Re-emit a record with FIELD_ORDER first, any extra keys after."""
    out = {k: rec[k] for k in FIELD_ORDER if k in rec}
    out.update({k: v for k, v in rec.items() if k not in out})
    return out


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


def extract_title_date(doc, json_objects=None):
    title, date = None, None
    for o in (json_objects if json_objects is not None else jsonld_objects(doc)):
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
    # feed_only outlets are ones whose article page is KNOWN to be unfetchable
    # (NYT 403s, the Washington Post times out). Attempting it anyway costs
    # three retries with backoff per article -- ~75s each against WaPo -- to
    # learn what the adapter already told us. Go straight to the feed record.
    if getattr(adapter, "feed_only", False):
        rec = adapter.fallback_record(url)
        if rec is None:
            return None
        rec["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return score_record(rec)

    r = fetch(session, url)
    if r is None:
        # NYT/WaPo block the article page outright. Their public feed already
        # gave us headline, date, summary and (for NYT) a captioned image, so
        # a failed fetch is not the end of the record for those outlets.
        fallback = getattr(adapter, "fallback_record", None)
        rec = fallback(url) if fallback else None
        if rec is None:
            return None
        rec["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return score_record(rec)
    try:
        doc = lhtml.fromstring(r.content)
    except Exception as e:
        log(f"  parse error {type(e).__name__} on {url[:70]}")
        return None
    jl = list(jsonld_objects(doc))
    title, date = extract_title_date(doc, jl)
    text = clean_text(trafilatura.extract(r.text, include_comments=False) or "")
    images = adapter.images(doc, url)
    # Additive: a feed-supplied image the page itself didn't expose.
    feed_images = getattr(adapter, "feed_images", None)
    if feed_images:
        have = {i["url"] for i in images}
        images += [i for i in feed_images(url) if i["url"] not in have]
    videos = adapter.videos(doc, url, jl)
    rec = {
        "title": title,
        "outlet": adapter.name,
        "date": date,
        "url": url,
        "text": text,
        "images": images,
        "videos": videos,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return score_record(rec)


DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_date(s):
    """Tolerant ISO-ish date -> datetime(UTC-naive). Outlets emit
    '2026-08-30T07:04:00-0400' (no colon in the offset). fromisoformat
    rejects that on <=3.10 (support landed in 3.11), so fall back to a plain
    YYYY-MM-DD match. Verified still required on this project's 3.10 env."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
    m = DATE_RE.search(s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def finalize(jsonl_path, corpus_path, keep_jsonl=False):
    """Merge this run's JSONL into the corpus, then drop the JSONL.

    MUST merge, not overwrite. With --resume the JSONL holds only the articles
    fetched *this* run, so rebuilding the corpus from it alone silently
    destroys everything collected previously. Existing records are loaded
    first and keyed by url; new ones update or extend them.

    A failed read of an EXISTING corpus raises instead of being swallowed.
    That swallow used to be `except: pass`, which left the record dict empty
    and then wrote this run's handful of articles over the whole corpus --
    no exception, no warning, exit code 0. `os.path.exists` has already
    established the file is there, so a read failure at this point is a real
    anomaly, never the ordinary "no corpus yet" case.
    """
    by_url = {}
    if os.path.exists(corpus_path):
        try:
            with open(corpus_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(
                f"{corpus_path} exists but could not be read ({exc}). Refusing "
                f"to continue: writing now would replace the corpus with this "
                f"run's records alone. {jsonl_path} is kept -- fix or move the "
                f"corpus and re-run."
            ) from exc
        for r in existing:
            if r.get("url"):
                by_url[r["url"]] = ordered(r)
    before = len(by_url)

    if os.path.exists(jsonl_path):
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("url"):
                    by_url[r["url"]] = ordered(r)

    recs = sorted(by_url.values(), key=lambda r: (r.get("date") or ""), reverse=True)
    tmp = corpus_path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(recs, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())   # publish durable bytes, not page-cache bytes
        os.replace(tmp, corpus_path)    # atomic: old complete file, or new one
    except OSError as exc:
        # The corpus is untouched -- os.replace never ran -- but the partial
        # tmp would otherwise sit there consuming space.
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise RuntimeError(
            f"could not write {corpus_path} ({exc}). The existing corpus and "
            f"{jsonl_path} are untouched; free space and re-run."
        ) from exc
    if not keep_jsonl and os.path.exists(jsonl_path):
        os.remove(jsonl_path)           # only after the rename succeeded
    log(f"  finalize: {before} existing + new -> {len(recs)} total")
    return recs


def load_corpus_urls(corpus_path):
    """Every article URL already in the corpus, for --resume.

    Loaded ONCE per crawl and shared by every outlet. Loading it inside
    crawl_outlet() instead would re-parse the whole corpus 29 times a run
    (~8.7s vs ~0.3s measured); the set itself is O(1) to probe at any size.

    Staleness across outlets is not a correctness problem: article URLs are
    per-publisher, so outlet B never needs URLs outlet A added this run, and
    each outlet additionally unions in its own JSONL.
    """
    if not os.path.exists(corpus_path):
        return set()
    try:
        with open(corpus_path) as f:
            return {r["url"] for r in json.load(f) if r.get("url")}
    except (json.JSONDecodeError, OSError) as exc:
        # Same reasoning as finalize(): the file exists, so a read failure is
        # an anomaly. Continuing would silently re-crawl everything.
        raise RuntimeError(
            f"{corpus_path} exists but could not be read ({exc}). Refusing to "
            f"continue: --resume would re-fetch every article already collected."
        ) from exc


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


def crawl_outlet(name, args, corpus_urls=frozenset()):
    """Crawl one outlet into the corpus. Returns summary dict.

    The per-outlet JSONL is still this run's first stop -- appended and flushed
    per article, so a kill mid-outlet loses nothing -- but it now merges
    straight into the single corpus rather than a per-outlet JSON.
    """
    adapter = get_adapter(name)
    corpus_path = str(args.corpus)
    jsonl_path = os.path.join(args.data_dir, f"{name}_flood.jsonl")

    # resume: the corpus (loaded once, passed in) plus this outlet's own JSONL
    # if a previous run was interrupted before it could be merged.
    seen = set()
    if args.resume:
        seen |= load_seen(jsonl_path)
        seen |= corpus_urls
        if seen:
            log(f"resume: {len(seen)} URLs already collected for {name}")

    session = requests.Session()
    fetch_one = lambda u: fetch(session, u)          # noqa: E731

    ceiling = adapter.expected_ceiling
    log(f"=== {name.upper()} === (expected ceiling ~{ceiling if ceiling else '?'} articles)")

    urls = [u for u in adapter.discover(fetch_one, args.max_pages, log) if u not in seen]
    if args.limit:
        urls = urls[:args.limit]
    log(f"{name}: {len(urls)} new articles to fetch")

    cutoff = None
    if args.since_days:
        cutoff = datetime.now() - timedelta(days=args.since_days)

    kept = skipped = too_old = failed = 0
    consecutive_old = 0
    with open(jsonl_path, "a") as fh:
        for i, u in enumerate(urls, 1):
            rec = scrape_article(session, u, adapter)
            if rec is None:
                failed += 1
                time.sleep(args.delay)
                continue

            # date window. Listings are broadly reverse-chronological, so a long
            # run of out-of-window articles means we've paged past the window.
            if cutoff:
                d = parse_date(rec.get("date"))
                if d and d < cutoff:
                    too_old += 1
                    consecutive_old += 1
                    if adapter.chronological and consecutive_old >= args.old_streak:
                        log(f"{name}: {consecutive_old} consecutive articles older than "
                            f"{args.since_days}d -- stopping early")
                        break
                    time.sleep(args.delay)
                    continue
                consecutive_old = 0

            if len(rec["images"]) < args.min_images:
                skipped += 1
                time.sleep(args.delay)
                continue

            if args.download_images:
                download_images(rec, args.image_dir, session)
            fh.write(json.dumps(ordered(rec), ensure_ascii=False) + "\n")
            fh.flush()
            kept += 1
            if kept % 10 == 0 or i <= 3:
                log(f"  [{i}/{len(urls)}] kept={kept} img={len(rec['images'])} "
                    f"vid={len(rec['videos'])} {(rec['title'] or '')[:44]}")
            time.sleep(args.delay)

    corpus = finalize(jsonl_path, corpus_path, keep_jsonl=args.keep_jsonl)
    # finalize returns the WHOLE corpus now, so the per-outlet summary has to
    # select this outlet's records rather than counting everything.
    recs = [r for r in corpus if r.get("outlet") == name]
    nimg = sum(len(r.get("images") or []) for r in recs)
    nvid = sum(len(r.get("videos") or []) for r in recs)
    nver = sum(1 for r in recs if r.get("flood_verified"))
    log(f"{name}: {len(recs)} records ({len(corpus)} in corpus) -> {corpus_path}  "
        f"(images={nimg} videos={nvid} verified={nver} old_skipped={too_old} failed={failed})")
    return {"outlet": name, "records": len(recs), "images": nimg, "videos": nvid,
            "verified": nver, "too_old": too_old, "failed": failed,
            "path": corpus_path}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outlets", default="all",
                    help="comma list of outlet names, 'all' (every registered "
                         "outlet), or 'images' (only those measured to yield "
                         "captioned images). See --list-outlets.")
    ap.add_argument("--list-outlets", action="store_true",
                    help="print every registered outlet with its expected ceiling, then exit")
    ap.add_argument("--max-pages", type=int, default=200, help="listing pages per outlet")
    ap.add_argument("--limit", type=int, default=5000, help="max articles per outlet")
    ap.add_argument("--since-days", type=int, default=365,
                    help="only keep articles newer than this many days (0=no limit)")
    ap.add_argument("--old-streak", type=int, default=40,
                    help="stop an outlet after this many consecutive out-of-window articles")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), "scrape_data"),
                    help="where per-outlet .jsonl intermediates are written "
                         "(gitignored; merged into --corpus and deleted)")
    ap.add_argument("--delay", type=float, default=0.6, help="seconds between article fetches")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--keep-jsonl", action="store_true",
                    help="keep the intermediate JSONL instead of deleting it")
    ap.add_argument("--download-images", action="store_true")
    ap.add_argument("--image-dir", default=None)
    ap.add_argument("--min-images", type=int, default=0)
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS,
                    help="the single corpus every outlet merges into "
                         "(default: data/news_scrape_results.json)")
    args = ap.parse_args()

    if args.list_outlets:
        print(f"{'outlet':16s} {'ceiling':>8s}  note")
        for n in sorted(ADAPTERS):
            a = get_adapter(n)
            print(f"{n:16s} {a.expected_ceiling or 0:>8d}  "
                  f"{getattr(a, 'note', '') or (a.__doc__ or '').strip().split(chr(10))[0][:70]}")
        return

    if args.outlets == "all":
        names = sorted(ADAPTERS)
    elif args.outlets == "images":
        names = sorted(IMAGE_OUTLETS)
    else:
        names = [x.strip() for x in args.outlets.split(",") if x.strip()]
    os.makedirs(args.data_dir, exist_ok=True)
    if args.image_dir is None:
        args.image_dir = os.path.join(args.data_dir, "images")

    # One parse of the corpus for the whole crawl; see load_corpus_urls().
    corpus_urls = load_corpus_urls(str(args.corpus))
    if corpus_urls:
        log(f"resume: {len(corpus_urls)} URLs already in {args.corpus}")

    summaries = []
    for n in names:
        try:
            summaries.append(crawl_outlet(n, args, corpus_urls))
        except KeyboardInterrupt:
            log("interrupted by user -- finalizing what we have")
            break
        except Exception as e:
            log(f"{n}: FAILED {type(e).__name__}: {e}")

    print("\n" + "=" * 68)
    print(f"{'outlet':15s} {'records':>8s} {'images':>8s} {'videos':>7s} {'verified':>9s}")
    print("-" * 68)
    for s in summaries:
        print(f"{s['outlet']:15s} {s['records']:>8d} {s['images']:>8d} "
              f"{s['videos']:>7d} {s['verified']:>9d}")
    print("-" * 68)
    print(f"{'TOTAL':15s} {sum(s['records'] for s in summaries):>8d} "
          f"{sum(s['images'] for s in summaries):>8d} "
          f"{sum(s['videos'] for s in summaries):>7d} "
          f"{sum(s['verified'] for s in summaries):>9d}")

    print(f"\ncorpus: {args.corpus}")
    # A crawl changes the corpus, not the verification file, so the counts here
    # rarely move -- but refreshing keeps the snapshot's timestamp honest and
    # means the file exists after a first-ever crawl.
    print()
    refresh_quietly()


if __name__ == "__main__":
    main()

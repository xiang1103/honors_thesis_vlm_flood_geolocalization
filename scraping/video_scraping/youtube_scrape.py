#!/usr/bin/env python3
"""Collect flood videos from YouTube (Data API v3) into data/youtube_videos.json.

The unit is a VIDEO, collected for flood relevance, not coordinates. Every
video found is written with its labels -- flood text score, known-event match,
New York -- and none is dropped for lacking coordinates or a NY match; the
reader picks the cut. Format: "REQUIRED data format for VIDEOS" in CLAUDE.md.
Design and reasoning: scraping/video_scraping/design.md.

Steps:
  1. search.list   the query plan (event windows, geo circles, flood term x
                   NY place), 100 quota units per call. Every response is
                   appended to scrape_data/youtube_searches.jsonl as it
                   arrives, so spent quota is never lost. Relevance-ranked,
                   breadth-first and resumable: each run fetches the next
                   pages not yet in the cache; once every page is walked it
                   re-walks them oldest first, forever (run_searches()).
  2. videos.list   full metadata for every id found, plus a re-check of every
                   video already in the file (1 unit per 50 ids).
  3. labels        video_signals.py: flood_text_score,
                   in_ny / in_nyc; coordinates tested against the Census NY
                   boundary when the uploader set them.
  4. thumbnails    i.ytimg.com cover + three auto-frames per video, fetched
                   and fingerprinted (shared data/image_hashes.json). Only
                   thumbnails that decode are kept -- that is the proof they
                   display; exact repeats within one video are dropped.
  5. write         merged into the existing file (never overwritten),
                   atomically; then data/youtube_videos_meta_data.json.
  6. export        data/youtube_flood_videos.json, the flood-only dataset,
                   regenerated from the master (export_flood_videos.py), with
                   its own metadata file.

Video bytes are never downloaded (YouTube Terms of Service).

The API key comes from $YOUTUBE_API_KEY or YOUTUBE_API_KEY in .env. It is
sent as a header, never in the URL, so it cannot leak into logs or errors.

    python3 scraping/video_scraping/youtube_scrape.py --plan        # list searches + cost, no API calls
    python3 scraping/video_scraping/youtube_scrape.py               # the routine run
    python3 scraping/video_scraping/youtube_scrape.py --no-search   # re-check + relabel existing, ~free
    python3 scraping/video_scraping/youtube_scrape.py --query "flooded street queens"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

HERE = Path(__file__).resolve().parent
PROJECT_DIR = HERE.parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scraping" / "api_based_scraping"))
sys.path.insert(0, str(PROJECT_DIR / "verification"))
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(HERE))
from gis_scrape import Client, Regions, exclusive_run, log, write_json  # noqa: E402
from dedupe import DEFAULT_CACHE, fetch_digests  # noqa: E402
import video_signals as signals  # noqa: E402
import export_flood_videos  # noqa: E402

DEFAULT_OUTPUT = PROJECT_DIR / "data" / "youtube_videos.json"
SEARCH_CACHE = PROJECT_DIR / "scrape_data" / "youtube_searches.jsonl"
MYCOAST = PROJECT_DIR / "data" / "mycoast.json"
ENV_FILE = PROJECT_DIR / ".env"

API = "https://www.googleapis.com/youtube/v3"
SEARCH_COST = 100            # quota units per search.list call
LIST_COST = 1                # per videos.list call of up to 50 ids
DAILY_QUOTA = 10_000

# --------------------------------------------------------------------------
# Query plan
# --------------------------------------------------------------------------

#: Pushed to the server with the NOT operator: the worst false positives,
#: removed before they cost a result slot. The rest are labelled locally.
SERVER_NEGATIVES = "-minecraft -roblox -fortnite"

FLOOD_TERMS = ["flooding", "flooded street", "flash flood", "driving through flood",
               "storm surge flooding"]
PLACE_TERMS = ["New York City", "NYC", "Queens NY", "Brooklyn", "Bronx", "Staten Island",
               "Manhattan", "Long Island", "Westchester NY", "Hudson Valley",
               "Rochester NY", "Buffalo NY"]

#: (label, lat, lon, radius). Only finds videos whose uploader set a location.
GEO_CIRCLES = [
    ("NYC", 40.7128, -74.0060, "40km"),
    ("Long Island", 40.80, -73.10, "70km"),
    ("Hudson Valley", 41.70, -73.95, "80km"),
    ("Capital Region", 42.65, -73.75, "80km"),
    ("Central NY", 43.05, -76.15, "100km"),
    ("Western NY", 42.95, -78.30, "120km"),
]

#: Days after a known event during which uploads are searched for.
EVENT_SEARCH_DAYS = 7


def search_plan(queries: list[str] | None) -> list[tuple[str, dict[str, Any]]]:
    """Ordered (label, search.list params). Most specific first, so a run
    that hits the quota has spent it on the searches most likely to pay."""
    base = {"part": "snippet", "type": "video", "maxResults": 50,
            "videoEmbeddable": "true", "relevanceLanguage": "en"}
    if queries:
        return [(q, {**base, "q": f"{q} {SERVER_NEGATIVES}"}) for q in queries]

    plan: list[tuple[str, dict[str, Any]]] = []
    for day, name in sorted(signals.NY_FLOOD_EVENTS.items()):
        start = date.fromisoformat(day)
        end = start + timedelta(days=EVENT_SEARCH_DAYS)
        plan.append((f"event {day} {name}", {
            **base, "q": f"flood|flooding New York|NYC|Long Island {SERVER_NEGATIVES}",
            "publishedAfter": f"{start}T00:00:00Z", "publishedBefore": f"{end}T00:00:00Z"}))
    for name, lat, lon, radius in GEO_CIRCLES:
        plan.append((f"geo {name} {radius}", {
            **base, "q": f"flood|flooding|flooded {SERVER_NEGATIVES}",
            "location": f"{lat},{lon}", "locationRadius": radius}))
    for place in PLACE_TERMS:
        for term in FLOOD_TERMS:
            plan.append((f"{term} {place}", {**base, "q": f"{term} {place} {SERVER_NEGATIVES}"}))
    return plan


def search_key(params: dict[str, Any]) -> str:
    """Identity of a search, independent of page and key."""
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

class QuotaExceeded(Exception):
    pass


class ApiError(RuntimeError):
    """Any other API refusal; `reasons` are YouTube's error reason codes."""

    def __init__(self, message: str, reasons: set[str]):
        super().__init__(message)
        self.reasons = reasons


class YouTube:
    """search.list / videos.list with quota accounting."""

    def __init__(self, client: Client, key: str, budget: int):
        self.client, self.budget, self.spent = client, budget, 0
        self.headers = {"X-goog-api-key": key}      # header, not URL: keeps it out of logs

    def can_afford(self, units: int) -> bool:
        return self.spent + units <= self.budget

    def call(self, endpoint: str, units: int, **params: Any) -> dict[str, Any]:
        if not self.can_afford(units):
            raise QuotaExceeded(f"run budget of {self.budget} units reached")
        try:
            resp = self.client.request("GET", f"{API}/{endpoint}", params=params,
                                       headers=self.headers, timeout=60)
        except requests.HTTPError as exc:
            body = exc.response.json() if exc.response is not None else {}
            err = body.get("error", {})
            reasons = {e.get("reason") for e in err.get("errors", [])}
            if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
                raise QuotaExceeded("YouTube daily quota exhausted") from None
            raise ApiError(f"YouTube {endpoint}: HTTP {exc.response.status_code} "
                           f"{sorted(r for r in reasons if r)} {err.get('message', '')}",
                           {r for r in reasons if r}) from None
        self.spent += units
        return resp.json()


def load_api_key(env_file: Path) -> str | None:
    """$YOUTUBE_API_KEY wins over .env (same rule as verify_images_vlm.py)."""
    if os.environ.get("YOUTUBE_API_KEY"):
        return os.environ["YOUTUBE_API_KEY"]
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() == "YOUTUBE_API_KEY":
                return value.strip().strip("\"'") or None
    return None


# --------------------------------------------------------------------------
# Step 1: search, cached
# --------------------------------------------------------------------------

def read_search_cache(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                log(f"  {path.name}:{n} unreadable, ignored (a killed run's last line)")
    return rows


def run_searches(yt: YouTube | None, plan: list[tuple[str, dict[str, Any]]], pages: int,
                 cache_path: Path) -> list[dict[str, Any]]:
    """Spend the run's search budget: finish the walk, then re-walk, forever.

    Owner's design (2026-10-07): keep RELEVANCE ranking; walk every page of
    every search; once all are walked, start over. Each run does, in order:

      1. WALK -- every page never fetched, BREADTH-FIRST (page 1 of every
         search, then page 2 of every search, ...). Page N+1 is requested with
         the `next_page_token` saved on page N. A search ends at `pages` or
         when YouTube returns no token.
      2. RE-WALK -- only once step 1 has nothing left: re-fetch already-saved
         pages OLDEST FIRST (by `fetched_at`). A full pass of 74 searches x 10
         pages is ~740 pages at ~93 a day, so every page is refreshed about
         every 8 days. New uploads that rank into a query's top ~500 are picked
         up on the next visit to the page they landed on.

    Both steps run in one call, so a run that finishes the walk with budget
    left starts the re-walk at once. A re-fetched page that now has a token
    where it had none (the search grew) makes new step-1 work for the next run.

    Tokens are positions, not snapshots ('CDIQAA' = "from result 50", the same
    for every search), so a re-walked page is that slot of TODAY's ranking.

    The cache is the bookmark: each answer is appended and flushed the moment
    it arrives, so quota already spent is never lost, and the latest line per
    (search, page) is what counts. Returns the whole cache (old + new).
    """
    cache = read_search_cache(cache_path)
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for row in cache:
        latest[(row["key"], row["page"])] = row
    if yt is None:
        return cache

    searches = [(label, params, search_key(params)) for label, params in plan]
    run_started = now_iso()

    def token_for(key: str, page: int) -> tuple[bool, str | None]:
        """(fetchable now, token). Page 1 needs none; page N needs page N-1's."""
        if page == 1:
            return True, None
        prev = latest.get((key, page - 1))
        token = (prev or {}).get("next_page_token")
        return bool(token), token

    issued = {"walk": 0, "re-walk": 0}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("a", encoding="utf-8") as fh:

        def fetch(phase: str, label: str, params: dict[str, Any], key: str,
                  page: int, token: str | None) -> bool:
            """One search page into the cache. False once the budget is spent."""
            try:
                data = yt.call("search", SEARCH_COST, **params,
                               **({"pageToken": token} if token else {}))
            except QuotaExceeded as exc:
                log(f"  search stopped ({phase}, page {page}): {exc}; "
                    f"the next run continues from here")
                return False
            except ApiError as exc:
                if "invalidPageToken" not in exc.reasons:
                    raise
                # Recorded as an empty, final page so the walk does not pay to
                # fail again; the re-walk retries it when it is the oldest.
                log(f"  {label} p{page}: page token no longer valid, search ends here")
                data = {"error": "invalidPageToken"}
            hit = {"key": key, "label": label, "params": params, "page": page,
                   "video_ids": [i["id"]["videoId"] for i in data.get("items", [])
                                 if i.get("id", {}).get("videoId")],
                   "next_page_token": data.get("nextPageToken"),
                   "total_results": data.get("pageInfo", {}).get("totalResults"),
                   "error": data.get("error"),
                   "fetched_at": now_iso()}
            fh.write(json.dumps(hit, ensure_ascii=False) + "\n")
            fh.flush()
            cache.append(hit)
            latest[(key, page)] = hit
            issued[phase] += 1
            log(f"  [{phase} {issued[phase]}] {label} p{page}: {len(hit['video_ids'])} videos")
            return True

        # 1. Walk: pages never fetched, breadth-first.
        for page in range(1, pages + 1):
            for label, params, key in searches:
                if (key, page) in latest:
                    continue
                ok, token = token_for(key, page)
                if ok and not fetch("walk", label, params, key, page, token):
                    return cache

        # 2. Re-walk: every saved page, oldest first -- except pages this run
        # already fetched, which would be paying twice for the same answer.
        saved = sorted(((latest[(key, page)]["fetched_at"], page, label, params, key)
                        for label, params, key in searches
                        for page in range(1, pages + 1)
                        if (key, page) in latest and latest[(key, page)]["fetched_at"] < run_started),
                       key=lambda t: (t[0], t[1]))
        if saved:
            log(f"walk complete: {len(saved)} pages saved; re-walking oldest first "
                f"(oldest fetched {saved[0][0][:10]})")
        for _, page, label, params, key in saved:
            ok, token = token_for(key, page)
            if ok and not fetch("re-walk", label, params, key, page, token):
                return cache

    log(f"searches: {issued['walk']} walk + {issued['re-walk']} re-walk pages issued")
    return cache


def queries_by_video(cache: list[dict[str, Any]]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for row in cache:
        for vid in row["video_ids"]:
            found[vid].add(row["label"])
    return found


# --------------------------------------------------------------------------
# Step 2: video metadata
# --------------------------------------------------------------------------

VIDEO_PARTS = "snippet,contentDetails,status,recordingDetails,topicDetails,statistics"


def fetch_videos(yt: YouTube, ids: list[str]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """id -> API item, and the ids actually asked about. An asked id with no
    item is deleted or private. Stops (keeping what it has) at the quota."""
    items: dict[str, dict[str, Any]] = {}
    asked: set[str] = set()
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            data = yt.call("videos", LIST_COST, part=VIDEO_PARTS, id=",".join(chunk), maxResults=50)
        except QuotaExceeded as exc:
            log(f"  videos.list stopped: {exc}; {len(asked)} of {len(ids)} fetched")
            break
        asked.update(chunk)
        for item in data.get("items", []):
            items[item["id"]] = item
    return items, asked


def playable(item: dict[str, Any]) -> bool:
    """Public, embeddable, not age-restricted, not a premiere/stream yet to air."""
    status, snippet = item.get("status", {}), item.get("snippet", {})
    rating = item.get("contentDetails", {}).get("contentRating", {})
    return (status.get("privacyStatus") == "public"
            and status.get("embeddable") is True
            and rating.get("ytRating") != "ytAgeRestricted"
            and snippet.get("liveBroadcastContent") != "upcoming")


# --------------------------------------------------------------------------
# Step 3: records
# --------------------------------------------------------------------------

#: The stored fields, in order. Trimmed by the owner 2026-10-07: channel,
#: category, language, license, location_basis, ny_basis, ny_places,
#: flood_text_hits, queries, text and api_fields are no longer stored; nor,
#: from a second trim the same day, flood_text_relevant (it is just
#: flood_text_score >= FLOOD_TEXT_THRESHOLD) and flood_event_date. The
#: labels still USE channel title, category and the joined text -- read from
#: the API response in memory while labelling, then discarded.
VIDEO_FIELDS = ["video_id", "record_id", "source", "source_url", "embed_url", "title",
                "description", "tags", "published_utc", "recording_date", "duration_s",
                "thumbnails", "lat", "lon", "in_ny", "in_nyc",
                "flood_text_score", "flood_visual",
                "available", "checked_at", "scraped_at"]

DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_iso(value: str | None) -> str | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def duration_seconds(value: str | None) -> int | None:
    m = DURATION.match(value or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def image_record_id(source_url: str, image_url: str) -> str:
    """Same scheme as every image in the repo, so thumbnails join to labels."""
    return hashlib.sha256(f"{source_url}\n{image_url}".encode()).hexdigest()[:24]


def thumbnail_objects(video_id: str, source_url: str, thumbs: dict[str, Any]) -> list[dict[str, Any]]:
    """The cover (largest rendition the API lists) plus YouTube's three
    automatic frames, taken near 25/50/75% of the video. Each in the
    static-image shape so displayability rules and dedupe apply unchanged."""
    def url(name: str) -> str | None:
        return (thumbs.get(name) or {}).get("url")

    base = f"https://i.ytimg.com/vi/{video_id}"
    cover = url("maxres") or url("standard") or url("high") or f"{base}/hqdefault.jpg"
    high = url("high")
    images = [(cover, url("medium"), high if high != cover else None)]
    images += [(f"{base}/hq{n}.jpg", f"{base}/{n}.jpg", None) for n in (1, 2, 3)]
    return [{"image_url": image, "thumbnail_url": thumb, "scaled_url": scaled,
             "record_id": image_record_id(source_url, image),
             "image_sha256": None, "image_dhash": None}
            for image, thumb, scaled in images]


class Labeller:
    """Everything a record needs that is built once per run."""

    def __init__(self, client: Client):
        mycoast = json.loads(MYCOAST.read_text(encoding="utf-8")) if MYCOAST.exists() else []
        self.gazetteer = signals.Gazetteer((r.get("place"), r.get("county")) for r in mycoast)
        self._client, self._regions = client, None
        log(f"labels: {len({r.get('place') for r in mycoast})} MyCoast places in the gazetteer")

    def regions(self) -> Regions:
        """NY / NYC boundaries, fetched only if some video has coordinates."""
        if self._regions is None:
            self._regions = Regions(self._client)
        return self._regions


def build_record(item: dict[str, Any], labeller: Labeller,
                 existing: dict[str, Any] | None) -> dict[str, Any]:
    vid = item["id"]
    sn, cd = item.get("snippet", {}), item.get("contentDetails", {})
    rd = item.get("recordingDetails", {})
    source_url = f"https://www.youtube.com/watch?v={vid}"
    tags = sn.get("tags") or []
    title, description = sn.get("title") or "", sn.get("description") or ""
    text = "\n".join(p for p in (title, " ".join(tags), description) if p)   # labelling only

    loc = rd.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    coord_ny = coord_nyc = None
    if lat is not None and lon is not None:
        regions = labeller.regions()
        coord_ny, coord_nyc = regions.in_ny(lat, lon), regions.in_nyc(lat, lon)

    published = utc_iso(sn.get("publishedAt"))
    recorded = utc_iso(rd.get("recordingDate"))
    score, _hits = signals.score_flood_text(title, tags, description, sn.get("categoryId"))
    ny = signals.label_new_york(text, sn.get("channelTitle"), labeller.gazetteer,
                                coord_ny, coord_nyc, None)

    rec = {
        "video_id": vid,
        "record_id": hashlib.sha256(source_url.encode()).hexdigest()[:24],
        "source": "youtube",
        "source_url": source_url,
        "embed_url": f"https://www.youtube-nocookie.com/embed/{vid}",
        "title": title,
        "description": description,
        "tags": tags,
        "published_utc": published,
        "recording_date": recorded,
        "duration_s": duration_seconds(cd.get("duration")),
        "thumbnails": thumbnail_objects(vid, source_url, sn.get("thumbnails") or {}),
        "lat": lat, "lon": lon,
        "in_ny": ny["in_ny"],
        "in_nyc": ny["in_nyc"],
        "flood_text_score": score,
        "flood_visual": (existing or {}).get("flood_visual"),     # set by the verifier, kept
        "available": True,
        "checked_at": now_iso(),
        "scraped_at": now_iso(),
    }
    return {k: rec[k] for k in VIDEO_FIELDS}


# --------------------------------------------------------------------------
# Step 4: thumbnails
# --------------------------------------------------------------------------

def fingerprint_thumbnails(rows: list[dict[str, Any]], workers: int, cache_path: Path) -> None:
    """Fetch + decode every thumbnail (shared digest cache), keep those that
    decode, drop exact repeats inside one video. `hq2.jpg` is often the same
    frame as the default cover, and a missing `maxresdefault` answers 404 with
    a grey placeholder -- both are caught here, not guessed in advance."""
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    urls = sorted({t["image_url"] for r in rows for t in r["thumbnails"]})
    for u in urls:                      # a cached failure may have been transient: retry it
        if u in cache and cache[u] is None:
            del cache[u]
    cache = fetch_digests(urls, workers, 30, cache)
    tmp = cache_path.with_name(cache_path.name + ".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, cache_path)

    failed = repeats = 0
    for row in rows:
        kept, seen = [], set()
        for t in row["thumbnails"]:
            digest = cache.get(t["image_url"])
            if not digest:
                failed += 1
                continue
            if digest["image_sha256"] in seen:
                repeats += 1
                continue
            seen.add(digest["image_sha256"])
            kept.append({**t, **digest})
        row["thumbnails"] = kept
    across = Counter(t["image_sha256"] for r in rows for t in r["thumbnails"])
    log(f"thumbnails: {sum(len(r['thumbnails']) for r in rows)} kept, {failed} did not decode "
        f"(dropped), {repeats} repeats within a video (dropped), "
        f"{sum(n - 1 for n in across.values() if n > 1)} shared ACROSS videos "
        f"(kept: re-uploads, reported only)")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def read_existing(path: Path) -> dict[str, dict[str, Any]]:
    """Existing videos by id. An unreadable file raises -- continuing would
    write this run's videos over everything else."""
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"{path} exists but could not be read ({exc}); fix or move it") from exc
    return {r["video_id"]: r for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--query", action="append", metavar="Q",
                    help="search this instead of the built-in plan (repeatable)")
    ap.add_argument("--pages", type=int, default=10,
                    help="how deep to page each search, 50 videos per page (default 10, YouTube's "
                         "practical limit is ~500 results). Breadth-first, so a daily run of the same "
                         "command goes one level deeper per ~7,400 units")
    ap.add_argument("--max-units", type=int, default=9_500,
                    help=f"quota units this run may spend (default 9500 of the {DAILY_QUOTA} daily)")
    ap.add_argument("--no-search", action="store_true", help="skip step 1: re-check + relabel existing videos")
    ap.add_argument("--no-recheck", action="store_true", help="fetch only videos not yet in the file")
    ap.add_argument("--plan", action="store_true", help="print the planned searches and their cost, then exit")
    ap.add_argument("--workers", type=int, default=16, help="thumbnail fetch threads")
    ap.add_argument("--hash-cache", type=Path, default=DEFAULT_CACHE)
    args = ap.parse_args()

    plan = search_plan(args.query)
    if args.plan:
        for label, params in plan:
            log(f"  {label:45s} q={params['q']!r}")
        log(f"{len(plan)} searches x {args.pages} page(s) = "
            f"{len(plan) * args.pages * SEARCH_COST} units (daily quota {DAILY_QUOTA})")
        return 0

    key = load_api_key(ENV_FILE)
    if not key:
        log("error: no YouTube API key. Set YOUTUBE_API_KEY in the environment or in .env "
            "(see .env.example).")
        return 2

    client = Client()
    yt = YouTube(client, key, args.max_units)
    with exclusive_run(args.output):
        existing = read_existing(args.output)       # read first: fail before spending quota
        log(f"existing: {len(existing)} videos in {args.output.name}")

        # Searches may not spend the units videos.list will need afterwards:
        # re-checking every stored video, plus the new ones found (each 100-unit
        # search page yields at most 50 ids = 1 unit). Without this reserve a
        # full-budget run finds videos it cannot afford to fetch that day.
        recheck_units = 0 if args.no_recheck else -(-len(existing) // 50)
        max_pages = max(0, (args.max_units - recheck_units) // (SEARCH_COST + LIST_COST))
        yt.budget = args.max_units - recheck_units - max_pages * LIST_COST
        cache = run_searches(None if args.no_search else yt, plan, args.pages, SEARCH_CACHE)
        yt.budget = args.max_units
        found = queries_by_video(cache)
        new_ids = [v for v in found if v not in existing]
        recheck = [] if args.no_recheck else list(existing)
        log(f"videos.list: {len(new_ids)} new + {len(recheck)} re-checks")
        items, asked = fetch_videos(yt, new_ids + recheck)

        labeller = Labeller(client)
        fresh: dict[str, dict[str, Any]] = {}
        unplayable = 0
        for vid, item in items.items():
            if not playable(item) and vid not in existing:
                unplayable += 1          # never shown, so never written
                continue
            rec = build_record(item, labeller, existing.get(vid))
            rec["available"] = playable(item)
            fresh[vid] = rec
        gone = 0
        for vid in asked - set(items):
            if vid in existing:          # deleted or private: keep the row and its labels
                existing[vid] = {**existing[vid], "available": False, "checked_at": now_iso()}
                gone += 1
        log(f"built {len(fresh)} records; {unplayable} new videos not playable (skipped), "
            f"{gone} existing videos no longer available (kept, available=false)")

        fingerprint_thumbnails(list(fresh.values()), args.workers, args.hash_cache)

        merged = {**existing, **fresh}
        # Every row in VIDEO_FIELDS shape, so a row this run did not rebuild
        # (quota ran out, or --no-recheck) cannot carry dropped fields forward.
        rows = sorted(({k: r.get(k) for k in VIDEO_FIELDS} for r in merged.values()),
                      key=lambda r: (r["published_utc"] or "", r["video_id"]), reverse=True)
        write_json(args.output, rows)
        log(f"wrote {args.output}: {len(existing)} existing + {len(fresh)} fetched -> {len(rows)} videos; "
            f"quota spent this run: {yt.spent} units")
        summarize(rows)
        # The flood-only dataset, regenerated from the file just written --
        # inside the lock, so a concurrent run can never export a stale master.
        export_flood_videos.export(args.output)

    # REQUIRED after any change to the data file (CLAUDE.md).
    import make_youtube_metadata
    make_youtube_metadata.refresh(args.output)
    return 0


def summarize(rows: list[dict[str, Any]]) -> None:
    def n(pred) -> int:
        return sum(1 for r in rows if pred(r))
    log(f"  flood (score >= {signals.FLOOD_TEXT_THRESHOLD})   : "
        f"{n(lambda r: r['flood_text_score'] >= signals.FLOOD_TEXT_THRESHOLD)}")
    log(f"  in_ny true/false/unknown: {n(lambda r: r['in_ny'] is True)}/"
        f"{n(lambda r: r['in_ny'] is False)}/{n(lambda r: r['in_ny'] is None)}")
    log(f"  in_nyc true             : {n(lambda r: r['in_nyc'] is True)}")
    log(f"  with coordinates        : {n(lambda r: r['lat'] is not None)}")
    log(f"  available               : {n(lambda r: r['available'])}")


if __name__ == "__main__":
    sys.exit(main())

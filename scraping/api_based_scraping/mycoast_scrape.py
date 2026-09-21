"""Everything MyCoast holds about its New York flood reports.

`gis_scrape.py` collects MyCoast as one source among four and keeps the fields
they have in common. This goes deeper on MyCoast alone: every field the ArcGIS
layer exposes, PLUS the report's own page, which carries three things the API
does not have at all --

  * "What is Flooded" (Roads/streets, Sidewalks, Parking lots, Lawns,
    Structures). The reporter's own statement that a STREET flooded, which is a
    better street-view pre-filter than any caption.
  * the reporter's free-text description;
  * the exact local time (the API's Date is UTC milliseconds), plus the weather
    and nearby tide-station readings shown alongside.

Scope: State = NY, report types Flood Watch and Storm Reporter -- the flood
programs. MyCoast NY also runs CoastSnap (fixed beach cameras), Litter Watch
and marine debris; add them to `REPORT_TYPES` if they are ever wanted.

ONE ROW PER REPORT, with its images nested, because that is the shape the page
data is in: "What is Flooded" describes the report, not one photograph. Each
image still carries the `record_id` that `gis_scrape.py` gives it, so rows join
back to `data/gis_flood_images.json` and to review decisions.

Pages are cached in `scrape_data/mycoast_pages.jsonl` (append+flush per page).
Unlike the crawl's JSONL this is NOT deleted after the merge: it is what makes
a re-run free instead of another ~30 minutes of requests against mycoast.org.
`--refresh-pages` ignores it, `--no-pages` skips page fetching entirely.

    python3 scraping/api_based_scraping/mycoast_scrape.py
    python3 scraping/api_based_scraping/mycoast_scrape.py --no-pages   # API only, seconds
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lxml import html as lhtml

# Same directory: the shared HTTP client, the NY/NYC boundaries, ArcGIS paging
# and the MyCoast field conventions all live in gis_scrape.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gis_scrape import (  # noqa: E402
    MYCOAST_LAYER,
    MYCOAST_SIZE_SUFFIX,
    Client,
    Regions,
    arcgis_features,
    epoch_ms_to_iso,
    join_text,
    log,
    mycoast_list,
    record_id,
    write_json,
)

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_DIR / "data" / "mycoast.json"
DEFAULT_PAGE_CACHE = PROJECT_DIR / "scrape_data" / "mycoast_pages.jsonl"

STATE = "NY"
REPORT_TYPES = ("Flood Watch", "Storm Reporter")


# --------------------------------------------------------------------------
# The report page
# --------------------------------------------------------------------------

def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text_content()).strip()


def parse_report_page(page_html: str) -> dict[str, Any]:
    """The fields that exist only on mycoast.org/reports/<id>.

    Deliberately NOT collected: the byline naming the person who filed the
    report. They are private individuals and the photograph is the point.
    """
    doc = lhtml.fromstring(page_html)
    out: dict[str, Any] = {}

    for xpath, key in (("//h5[contains(@class,'report-location-label')]", "county"),
                       ("//h2[contains(@class,'title-location')]", "place"),
                       ("//div[@id='report-header-titleblock']//p[contains(@class,'byline')]", "local_time")):
        found = doc.xpath(xpath)
        if found:
            out[key] = _text(found[0])

    # "<p class='report-data-block'><strong>What is Flooded</strong>: <ul
    #  class='report-field-value-list'><li>Roads/streets<li>Sidewalks</ul></p>"
    #
    # Read off the RAW html, not the parsed tree: a <ul> inside a <p> is
    # invalid, so every HTML parser closes the <p> first and the list ends up a
    # SIBLING of the block it belongs to. Walking the tree silently paired
    # labels with the next report's values.
    submitted: dict[str, Any] = {}
    for match in re.finditer(
        r"<strong>([^<]{1,80}?)</strong>\s*:\s*(?:<ul[^>]*class='report-field-value-list'[^>]*>(.*?)</ul>|([^<]*))",
        page_html, re.S,
    ):
        label = html.unescape(match.group(1)).strip().rstrip(":")
        if match.group(2) is not None:
            values = [html.unescape(re.sub(r"<[^>]+>", " ", v)).strip()
                      for v in re.split(r"<li>", match.group(2)) if v.strip()]
            submitted[label] = [v for v in values if v]
        else:
            value = html.unescape(match.group(3) or "").strip()
            if value:
                submitted[label] = value
    if submitted:
        out["submitted"] = submitted

    comment = doc.xpath("//div[contains(@class,'post-author-comment')]//p")
    if comment:
        out["description"] = _text(comment[0])

    weather = {}
    for p in doc.xpath("//div[@id='weather-info']//p[contains(@class,'data')]"):
        label = p.xpath("./strong")
        if label:
            weather[_text(label[0]).rstrip(":")] = _text(p).split(":", 1)[-1].strip()
    if weather:
        out["weather"] = weather

    # The tide widget carries the report's epoch time and its timezone offset,
    # which is the only place the LOCAL time is machine-readable.
    tide = doc.xpath("//div[contains(@class,'tide-sources')]")
    if tide:
        for attr, key in (("data-report-time", "report_epoch"), ("data-tz-offset", "tz_offset")):
            value = tide[0].get(attr)
            if value not in (None, ""):
                try:
                    out[key] = int(float(value))
                except ValueError:
                    pass

    # Tide readings are rendered by JavaScript, but the station list is inlined.
    stations = []
    for match in re.finditer(r"mapTideStations\.push\((\{.*?\})\);", page_html, re.S):
        blob = match.group(1)
        title = re.search(r"<strong>(.*?)</strong>", blob)
        level = re.search(r"Water Level \(at time of report\):\s*</strong>(.*?)<", blob)
        distance = re.search(r"<em>(.*?)</em>", blob)
        stations.append({
            "station": html.unescape(title.group(1)) if title else None,
            "water_level_at_report": html.unescape(level.group(1)).strip() if level else None,
            "distance": html.unescape(distance.group(1)) if distance else None,
        })
    if stations:
        out["tide_stations"] = stations

    # <a href="...full.jpg" class="report-photo-grid__item">: the originals, in
    # page order. The API's ImageUrls holds resized copies of the same files.
    full = doc.xpath("//a[contains(@class,'report-photo-grid__item')]/@href")
    if full:
        out["page_image_urls"] = [str(u) for u in full]
    return out


def read_existing(path: Path) -> dict[str, dict[str, Any]]:
    """Existing reports keyed by report_id. A file that exists but will not
    parse raises rather than being ignored: continuing would write this run's
    reports over whatever is already there."""
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"{path} exists but could not be read ({exc}); fix or move it and re-run") from exc
    return {str(row["report_id"]): row for row in rows}


def load_page_cache(path: Path) -> dict[int, dict[str, Any]]:
    """report_id -> parsed page. A corrupt line is skipped, not fatal: the
    cache is an optimisation and the page can simply be fetched again."""
    cache: dict[int, dict[str, Any]] = {}
    if not path.is_file():
        return cache
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                cache[int(row["report_id"])] = row
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    return cache


def fetch_pages(client: Client, reports: list[dict[str, Any]], cache_path: Path,
                workers: int, refresh: bool) -> dict[int, dict[str, Any]]:
    """Fetch and parse every report page not already cached.

    mycoast.org answers a report page in ~7 seconds, so serial fetching would
    take about four hours for New York. A handful of workers brings that to
    minutes; each holds one connection and the pool is deliberately small,
    since this is someone else's server.
    """
    cache = {} if refresh else load_page_cache(cache_path)
    todo = [r for r in reports if int(r["attributes"]["ID"]) not in cache]
    log(f"  pages: {len(reports) - len(todo)} cached, {len(todo)} to fetch with {workers} workers")
    if not todo:
        return cache

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()
    done = failures = 0

    def fetch(report: dict[str, Any]) -> dict[str, Any] | None:
        attrs = report["attributes"]
        report_id = int(attrs["ID"])
        url = attrs.get("Report_URL") or f"https://mycoast.org/reports/{report_id}"
        try:
            parsed = parse_report_page(client.text("GET", url, timeout=120))
        except Exception as exc:              # noqa: BLE001 - one bad page must not end the run
            log(f"    {url}: {exc.__class__.__name__}: {exc}")
            return None
        parsed.update(report_id=report_id, report_url=url,
                      fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return parsed

    # Append+flush per page: a kill mid-run keeps everything already fetched,
    # and the next run resumes from the cache.
    with cache_path.open("a", encoding="utf-8") as handle, \
            ThreadPoolExecutor(max_workers=workers) as pool:
        for parsed in pool.map(fetch, todo):
            done += 1
            if parsed is None:
                failures += 1
            else:
                with write_lock:
                    cache[parsed["report_id"]] = parsed
                    handle.write(json.dumps(parsed, ensure_ascii=False) + "\n")
                    handle.flush()
            if done % 200 == 0 or done == len(todo):
                log(f"    {done}/{len(todo)} pages")
    if failures:
        log(f"  pages: {failures} could not be fetched or parsed; re-run to retry them")
    return cache


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

def photo_identity(url: str) -> str:
    """Filename with WordPress's renditions stripped: "foo-scaled.jpg",
    "foo-300x225.jpg" and "foo.jpg" are all the same photograph."""
    name = url.split("/")[-1]
    name = MYCOAST_SIZE_SUFFIX.sub("", name)
    return re.sub(r"-scaled(?=\.\w+$)", "", name)


def local_time_iso(page: dict[str, Any]) -> str | None:
    """The report's wall-clock time.

    Preferred source is the tide widget's epoch plus its UTC offset. Older
    reports have no tide widget, so fall back to parsing the byline
    ("07/07/1935 | 12:00 pm"), which has no offset and is returned naive.
    """
    epoch, tz_offset = page.get("report_epoch"), page.get("tz_offset")
    if epoch is not None:
        tz = timezone(timedelta(hours=tz_offset)) if tz_offset is not None else timezone.utc
        return datetime.fromtimestamp(epoch, tz=tz).isoformat(timespec="seconds")
    text = (page.get("local_time") or "").replace("|", "").strip()
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M%p", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def build_record(report: dict[str, Any], page: dict[str, Any], regions: Regions) -> dict[str, Any]:
    attrs = dict(report["attributes"])
    geometry = report.get("geometry") or {}
    lat = geometry.get("y", attrs.get("Latitude"))
    lon = geometry.get("x", attrs.get("Longitude"))
    report_id = int(attrs["ID"])
    source_url = attrs.get("Report_URL") or f"https://mycoast.org/reports/{report_id}"

    thumbnails = [u.strip() for u in (attrs.get("ImageUrls") or "").split("|") if u.strip()]
    from_page = list(page.get("page_image_urls") or [])
    # The page links WordPress's "-scaled" rendition of a large upload, which
    # is the SAME photograph at about half the bytes (verified: different
    # sha256, ~2.2 MB vs ~0.9 MB). Match on the identity below so the two
    # never count as two photos; keep the original as image_url.
    by_identity = {photo_identity(u): u for u in from_page}
    images = []
    for thumbnail in thumbnails:
        full = MYCOAST_SIZE_SUFFIX.sub("", thumbnail)
        scaled = by_identity.pop(photo_identity(full), None)
        images.append({
            "image_url": full,
            "thumbnail_url": thumbnail if thumbnail != full else None,
            "scaled_url": scaled if scaled and scaled != full else None,
            # Same id gis_scrape.py writes, so rows join across the two files.
            "record_id": record_id(source_url, full),
        })
    for leftover in by_identity.values():   # a photo on the page the API did not list
        images.append({"image_url": leftover, "thumbnail_url": None, "scaled_url": None,
                       "record_id": record_id(source_url, leftover), "page_only": True})

    submitted = page.get("submitted") or {}
    flooded = submitted.get("What is Flooded")
    text = join_text(
        attrs.get("Title"), attrs.get("Report_Type"),
        page.get("place"),
        mycoast_list(attrs.get("Guess_Flooding_Source")) and f"Flooding source: {mycoast_list(attrs['Guess_Flooding_Source'])}",
        mycoast_list(attrs.get("Guess_Flooding_Cause")) and f"Flooding cause: {mycoast_list(attrs['Guess_Flooding_Cause'])}",
        flooded and f"What is flooded: {', '.join(flooded) if isinstance(flooded, list) else flooded}",
        attrs.get("Estimated_Water_Depth") is not None and f"Estimated water depth: {attrs['Estimated_Water_Depth']}",
        page.get("description"),
    )
    return {
        "report_id": report_id,
        "source_url": source_url,
        "report_type": attrs.get("Report_Type"),
        "title": attrs.get("Title"),
        "county": page.get("county") or attrs.get("geo_administrative_area_level_2"),
        "place": page.get("place") or attrs.get("geo_neighborhood") or attrs.get("geo_locality"),
        "state": attrs.get("State"),
        "date_utc": epoch_ms_to_iso(attrs.get("Date")),
        "local_time": local_time_iso(page),
        "local_time_text": page.get("local_time"),
        "lat": lat,
        "lon": lon,
        "in_nyc": regions.in_nyc(lat, lon),
        "nyc_basis": "coordinates",
        "image_count": len(images),
        "images": images,
        "text": text,
        "description": page.get("description"),
        "submitted": submitted,
        "weather": page.get("weather") or {},
        "tide_stations": page.get("tide_stations") or [],
        "has_page_detail": bool(page),
        "api_fields": attrs,            # every field the layer exposes, verbatim
        "page_fetched_at": page.get("fetched_at"),
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def summarize(rows: list[dict[str, Any]]) -> None:
    images = sum(r["image_count"] for r in rows)
    with_page = sum(1 for r in rows if r["has_page_detail"])
    flooded = Counter()
    for row in rows:
        value = (row["submitted"] or {}).get("What is Flooded") or []
        for item in (value if isinstance(value, list) else [value]):
            flooded[item] += 1
    log(f"  reports {len(rows)}, images {images}, in NYC {sum(1 for r in rows if r['in_nyc'])}, "
        f"with page detail {with_page}, with description {sum(1 for r in rows if r['description'])}, "
        f"with local time {sum(1 for r in rows if r['local_time'])}")
    log(f"  types: {dict(Counter(r['report_type'] for r in rows))}")
    if flooded:
        log(f"  what is flooded: {dict(flooded.most_common(8))}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--page-cache", type=Path, default=DEFAULT_PAGE_CACHE)
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel report-page fetches (mycoast.org takes ~7s per page)")
    ap.add_argument("--no-pages", action="store_true", help="API fields only; skip mycoast.org")
    ap.add_argument("--refresh-pages", action="store_true", help="ignore the page cache and refetch")
    ap.add_argument("--limit", type=int, help="stop after N reports (for a test run)")
    args = ap.parse_args()

    client = Client()
    existing = read_existing(args.output)          # loud on a corrupt file, before any network work
    regions = Regions(client)

    types = ", ".join(f"'{t}'" for t in REPORT_TYPES)
    reports = arcgis_features(
        client, MYCOAST_LAYER, "ObjectId",
        where=f"State = '{STATE}' AND Report_Type IN ({types}) AND Removed = 0",
        outFields="*", returnGeometry="true",
    )
    log(f"  api: {len(reports)} {STATE} reports")
    if args.limit:
        reports = reports[: args.limit]

    pages: dict[int, dict[str, Any]] = {}
    if not args.no_pages:
        pages = fetch_pages(client, reports, args.page_cache, args.workers, args.refresh_pages)

    fresh = {}
    for report in reports:
        record = build_record(report, pages.get(int(report["attributes"]["ID"]), {}), regions)
        fresh[str(record["report_id"])] = record

    merged = {**existing, **fresh}
    rows = sorted(merged.values(), key=lambda r: (r.get("date_utc") or "", r["report_id"]), reverse=True)
    write_json(args.output, rows)
    log(f"wrote {args.output}: {len(existing)} existing + {len(fresh)} collected -> {len(rows)} reports")
    summarize(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

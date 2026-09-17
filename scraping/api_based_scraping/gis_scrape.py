"""Collect flood photographs of New York State from public GIS and open-data APIs.

Unlike the news crawl, every source here is queried through a documented API
and most records carry coordinates. Four sources, all checked by hand on
2026-09-16:

  mycoast   MyCoast citizen flood reports (ArcGIS). Report types "Flood Watch"
            and "Storm Reporter" only -- the layer also holds CoastSnap beach
            stations, litter and wildlife reports.
  stn       USGS Short-Term Network photos (high-water marks, sensor sites)
            tied to a flood event at a NY site. Public domain.
  commons   Wikimedia Commons, walked down from the New York flood / Sandy /
            Ida categories. The only source whose pixels can be redistributed.
  napsg     NAPSG PhotoMappers crowdsourced disaster photos (ArcGIS), tropical
            cyclone and flood incidents. Mostly reposted tweets and news.

LOCATION. Every record is in New York State; `in_nyc` labels the five
boroughs. Rectangles are only a server-side prefilter: an NYC bounding box
reaches across the Hudson into Jersey City and Newark, and a NY one covers half
of New Jersey and Connecticut. Containment is decided locally against real
boundaries fetched at start-up:

  * NY State   -- Census TIGERweb legal boundary (it includes state waters, so
                  shoreline reports are kept; the generalized Esri outline
                  dropped 376 of 1,802 MyCoast NY reports).
  * NYC        -- NYC Planning borough boundaries, WATER INCLUDED, for the same
                  reason.

Records without coordinates (most Commons files) keep `lat`/`lon` = None;
`nyc_basis` says whether `in_nyc` came from coordinates or a category name, and
is None when it is unknown.

ONE ROW PER (source page, image). `record_id = sha256(source_url \\n
image_url)[:24]`, the same scheme as the news verifier's occurrence_id. The
output is merged, not overwritten, so collecting one source leaves the others'
rows in place. URLs and text are stored, never pixels.

    python3 scraping/api_based_scraping/gis_scrape.py                    # all sources
    python3 scraping/api_based_scraping/gis_scrape.py --sources mycoast,stn
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import html
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from functools import reduce
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from shapely.geometry import Point, Polygon
from shapely.prepared import prep
from shapely.validation import make_valid

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_DIR / "data" / "gis_flood_images.json"

USER_AGENT = "vlm_flood-research/0.1 (honors thesis flood image dataset; python-requests)"

# Boundaries
NY_STATE_LAYER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Current/MapServer/80"
NYC_BOROUGH_LAYER = "https://services5.arcgis.com/GfwWNkhOj9bNBqoJ/arcgis/rest/services/NYC_Borough_Boundary_Water_Included/FeatureServer/0"
#: Padding on the server-side prefilter envelope, in degrees (~5 km). The
#: polygon test is exact; the pad only keeps points on the boundary line from
#: being cut before they reach it.
ENVELOPE_PAD_DEG = 0.05

# Sources
MYCOAST_LAYER = "https://services1.arcgis.com/tikbh7xC3WJpzTz6/arcgis/rest/services/current/FeatureServer/0"
MYCOAST_TYPES = ("Flood Watch", "Storm Reporter")
STN_API = "https://stn.wim.usgs.gov/STNServices"
NAPSG_LAYER = "https://services.arcgis.com/0ZRg6WRC7mxSLyKX/arcgis/rest/services/survey123_85163dd9335b4c518e3006e60389cfdd_stakeholder/FeatureServer/0"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

COMMONS_ROOTS = [
    "Floods in New York (state)",
    "Effects of Hurricane Sandy in New York (state)",
    "Effects of Hurricane Ida (2021) in New York (state)",
]
COMMONS_MAX_DEPTH = 4
#: Subcategories that sit under the flood categories but are not floods:
#: resilience parks, water-main breaks, post-storm reopenings, rebuilds, visits,
#: wind damage. Matched case-insensitively against the category name; the whole
#: subtree is skipped. Built from a full listing of the walk on 2026-09-16.
COMMONS_SKIP = (
    "flood protection", "stormwater", "water main break", "shutdown",
    "reopens", "restoration project", "east river park", "basketball courts",
    "(new york city subway service)", "coastal resiliency", "pier 42 park",
    "crane damage", "space shuttle", "joint field office", "visits",
    "frontline workforce", "fix&fortify", "return of the", "shuttle buses",
)
#: A Commons file without coordinates is labelled NYC only by its category.
#: Not "Metropolitan Transportation Authority": it runs Metro-North and the
#: LIRR too, and files there had coordinates outside the city.
COMMONS_NYC_CATEGORY = re.compile(
    r"new york city|manhattan|brooklyn|\bqueens\b|\bbronx\b|staten island|"
    r"rockaway|subway|south ferry|whitehall street|fdr drive|"
    r"montague street|greenpoint tubes|"
    r"cranberry tube|clark street tunnel|53rd street tunnel|14th street tunnel|"
    r"queens-midtown|brooklyn-battery",
    re.IGNORECASE,
)

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "tif", "tiff", "webp"}
MYCOAST_SIZE_SUFFIX = re.compile(r"-\d+x\d+(?=\.\w+$)")   # WordPress thumbnail: foo-300x225.jpg


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Client:
    """requests.Session with retries and a per-host minimum interval.

    Commons answers bursts with HTTP 429 (seen on an unthrottled category
    walk), so hosts can be given a delay; Retry-After is honoured.
    """

    def __init__(self, host_delay: dict[str, float] | None = None, retries: int = 5):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.host_delay = host_delay or {}
        self.retries = retries
        self._last: dict[str, float] = {}

    def json(self, method: str, url: str, timeout: float = 120, **kwargs) -> Any:
        host = urlparse(url).netloc
        for attempt in range(self.retries + 1):
            wait = self.host_delay.get(host, 0) - (time.monotonic() - self._last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == self.retries:
                    raise
                log(f"  {host}: {exc.__class__.__name__}, retrying")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.retries:
                    resp.raise_for_status()
                delay = float(resp.headers.get("Retry-After") or 2 ** attempt) + 1
                log(f"  {host}: HTTP {resp.status_code}, retrying in {delay:.0f}s")
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")


def arcgis(client: Client, layer: str, op: str = "query", **params: Any) -> dict[str, Any]:
    """One ArcGIS REST call. POST, because polygon and id lists get long.

    ArcGIS reports errors as HTTP 200 with an `error` body -- checking the
    status code alone turned a nonexistent service into "0 results".
    """
    data = client.json("POST", f"{layer}/{op}", data={"f": "json", **params})
    if "error" in data:
        raise RuntimeError(f"ArcGIS {op} on {layer} failed: {data['error']}")
    return data


def arcgis_features(client: Client, layer: str, oid_field: str, **params: Any) -> list[dict[str, Any]]:
    """Every matching feature, paging past the layer's maxRecordCount."""
    features: list[dict[str, Any]] = []
    while True:
        page = arcgis(client, layer, orderByFields=oid_field,   # stable order: no skips or repeats
                      resultOffset=len(features), outSR=4326, **params)
        features += page.get("features", [])
        if not page.get("exceededTransferLimit"):
            return features


# --------------------------------------------------------------------------
# Geography
# --------------------------------------------------------------------------

class Regions:
    """NY State and NYC boundaries for point-in-polygon tests."""

    def __init__(self, client: Client):
        self.ny = self._fetch(client, NY_STATE_LAYER, "STUSAB='NY'", "OBJECTID")
        self.nyc = self._fetch(client, NYC_BOROUGH_LAYER, "1=1", "OBJECTID")
        self._ny, self._nyc = prep(self.ny), prep(self.nyc)
        log(f"boundaries: NY {self.ny.bounds}, NYC {self.nyc.bounds}")

    @staticmethod
    def _fetch(client: Client, layer: str, where: str, oid: str):
        feats = arcgis_features(client, layer, oid, where=where, outFields=oid, returnGeometry="true")
        rings = [Polygon(r) for f in feats for r in f["geometry"]["rings"]]
        if not rings:
            raise RuntimeError(f"no boundary geometry from {layer} where {where}")
        # Esri rings are shells and holes mixed together; even-odd
        # (symmetric difference) turns a ring inside a ring into a hole.
        return make_valid(reduce(lambda a, b: a.symmetric_difference(b), rings))

    @staticmethod
    def envelope(geom) -> str:
        x0, y0, x1, y1 = geom.bounds
        p = ENVELOPE_PAD_DEG
        return f"{x0 - p},{y0 - p},{x1 + p},{y1 + p}"

    def in_ny(self, lat: float | None, lon: float | None) -> bool:
        return lat is not None and lon is not None and self._ny.contains(Point(lon, lat))

    def in_nyc(self, lat: float | None, lon: float | None) -> bool:
        return lat is not None and lon is not None and self._nyc.contains(Point(lon, lat))


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

FIELD_ORDER = ["record_id", "source", "title", "text", "date", "event", "lat", "lon",
               "in_nyc", "nyc_basis", "image_url", "thumbnail_url", "source_url",
               "license", "credit", "extra", "scraped_at"]


def record_id(source_url: str, image_url: str) -> str:
    return hashlib.sha256(f"{source_url}\n{image_url}".encode()).hexdigest()[:24]


def make_record(source: str, source_url: str, image_url: str, **fields: Any) -> dict[str, Any]:
    rec = {k: None for k in FIELD_ORDER}
    rec.update(fields)
    rec.update(record_id=record_id(source_url, image_url), source=source,
               source_url=source_url, image_url=image_url,
               scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rec["extra"] = {k: v for k, v in (rec["extra"] or {}).items() if v not in (None, "", "null")}
    return {k: rec[k] for k in FIELD_ORDER}


def nyc_label(regions: Regions, lat: float | None, lon: float | None) -> tuple[bool | None, str | None]:
    if lat is None or lon is None:
        return None, None
    return regions.in_nyc(lat, lon), "coordinates"


def epoch_ms_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def join_text(*parts: Any) -> str | None:
    kept = [str(p).strip() for p in parts if p and p != "null" and str(p).strip()]
    return "\n".join(kept) or None


def strip_html(s: str | None) -> str | None:
    if not s:
        return None
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip() or None


def extension(name: str | None) -> str:
    return (name or "").rsplit(".", 1)[-1].lower() if name and "." in name else ""


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def mycoast_list(value: str | None) -> str | None:
    """MyCoast stores multi-choice answers as a JSON list in a string, or 'null'."""
    if not value or value == "null":
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, list):
        return ", ".join(str(x) for x in parsed if x) or None
    return str(parsed) if parsed else None


def collect_mycoast(client: Client, regions: Regions) -> list[dict[str, Any]]:
    types = ", ".join(f"'{t}'" for t in MYCOAST_TYPES)
    feats = arcgis_features(
        client, MYCOAST_LAYER, "ObjectId",
        where=f"Report_Type IN ({types}) AND ImageUrls IS NOT NULL AND Removed = 0",
        geometry=Regions.envelope(regions.ny), geometryType="esriGeometryEnvelope",
        inSR=4326, spatialRel="esriSpatialRelIntersects", returnGeometry="true",
        outFields="ID,Title,Report_Type,Date,State,ImageUrls,Report_URL,Guess_Flooding_Source,"
                  "Guess_Flooding_Cause,Estimated_Water_Depth,Storm_Damage,geo_locality,"
                  "geo_neighborhood,Wind_Speed,Precipitation24h",
    )
    records, skipped = [], Counter()
    for f in feats:
        a, g = f["attributes"], f.get("geometry") or {}
        lat, lon = g.get("y"), g.get("x")
        # The State field is kept as a second opinion: one NY-tagged report sits
        # 1.2 km outside the legal boundary.
        if a.get("State") != "NY" and not regions.in_ny(lat, lon):
            skipped["outside NY"] += 1
            continue
        in_nyc, basis = nyc_label(regions, lat, lon)
        source, cause = mycoast_list(a.get("Guess_Flooding_Source")), mycoast_list(a.get("Guess_Flooding_Cause"))
        text = join_text(
            a.get("Title"), a.get("Report_Type"),
            source and f"Flooding source: {source}",
            cause and f"Flooding cause: {cause}",
            a.get("Estimated_Water_Depth") and f"Estimated water depth: {a['Estimated_Water_Depth']}",
        )
        for thumb in (u.strip() for u in (a.get("ImageUrls") or "").split("|")):
            if not thumb:
                continue
            full = MYCOAST_SIZE_SUFFIX.sub("", thumb)   # original upload behind the resized copy
            records.append(make_record(
                "mycoast", a.get("Report_URL") or f"https://mycoast.org/reports/{a.get('ID')}", full,
                title=a.get("Title"), text=text or None, date=epoch_ms_to_iso(a.get("Date")),
                event=a.get("Report_Type"), lat=lat, lon=lon, in_nyc=in_nyc, nyc_basis=basis,
                thumbnail_url=thumb if thumb != full else None,
                extra={"report_id": a.get("ID"), "state": a.get("State"),
                       "locality": a.get("geo_locality"), "neighborhood": a.get("geo_neighborhood"),
                       "storm_damage": a.get("Storm_Damage"), "wind_speed": a.get("Wind_Speed"),
                       "precipitation_24h": a.get("Precipitation24h")},
            ))
    log(f"  mycoast: {len(feats)} reports in the prefilter box -> {len(records)} images; skipped {dict(skipped)}")
    return records


def collect_stn(client: Client, regions: Regions) -> list[dict[str, Any]]:
    sites = {s["site_id"]: s for s in client.json("GET", f"{STN_API}/Sites/FilteredSites.json", params={"State": "NY"})}
    events = {e["event_id"]: e for e in client.json("GET", f"{STN_API}/Events.json")}
    # Every file in STN in one ~30 MB response. Per-event listings would need
    # the event ids up front and missed half the NY events when hand-picked.
    log(f"  stn: {len(sites)} NY sites, {len(events)} events; downloading the file index...")
    files = client.json("GET", f"{STN_API}/Files.json", timeout=600)

    records, skipped = [], Counter()
    for f in files:
        if f.get("filetype_id") != 1 or f.get("site_id") not in sites:
            continue
        if extension(f.get("name")) not in IMAGE_EXTENSIONS:
            skipped["not an image file"] += 1
            continue
        m = re.search(r"EVENT_(\d+)", f.get("path") or "")
        event = events.get(int(m.group(1))) if m else None
        if event is None:
            skipped["site photo, no flood event"] += 1
            continue
        if "exercise" in (event.get("event_name") or "").lower():
            skipped["training exercise"] += 1
            continue
        site = sites[f["site_id"]]
        lat = f.get("latitude_dd") or site.get("latitude_dd")
        lon = f.get("longitude_dd") or site.get("longitude_dd")
        in_nyc, basis = nyc_label(regions, lat, lon)
        records.append(make_record(
            "usgs_stn", f"{STN_API}/Files/{f['file_id']}.json", f"{STN_API}/Files/{f['file_id']}/Item",
            title=f.get("name"),
            text=join_text(f.get("description"), site.get("site_description"),
                           site.get("waterbody") and f"Waterbody: {site['waterbody']}",
                           ", ".join(x for x in (site.get("address"), site.get("city"), site.get("county")) if x and x != "0")),
            # file_date is the UPLOAD date (Sandy photos read 2016); only
            # photo_date describes the photograph.
            date=f.get("photo_date"), event=event.get("event_name"),
            lat=lat, lon=lon, in_nyc=in_nyc, nyc_basis=basis,
            license="Public domain (USGS)",
            extra={"file_id": f["file_id"], "site_id": f["site_id"], "site_no": site.get("site_no"),
                   "event_id": event["event_id"], "event_start": event.get("event_start_date"),
                   "event_end": event.get("event_end_date"), "uploaded": f.get("file_date"),
                   "hwm_id": f.get("hwm_id"),
                   "instrument_id": f.get("instrument_id"), "photo_direction": f.get("photo_direction"),
                   "coordinates_from": "photo" if f.get("latitude_dd") else "site"},
        ))
    log(f"  stn: {len(files)} files -> {len(records)} NY flood-event photos; skipped {dict(skipped)}")
    return records


def commons_api(client: Client, **params: Any) -> dict[str, Any]:
    data = client.json("GET", COMMONS_API, params={"format": "json", "maxlag": 5, **params})
    if "error" in data:
        raise RuntimeError(f"Commons API error: {data['error']}")
    return data


def collect_commons(client: Client, regions: Regions) -> list[dict[str, Any]]:
    visited: set[str] = set()
    file_category: dict[str, str] = {}
    skipped = Counter()

    def walk(category: str, depth: int) -> None:
        if category in visited or depth > COMMONS_MAX_DEPTH:
            return
        visited.add(category)
        if any(s in category.lower() for s in COMMONS_SKIP):
            skipped["category skipped: " + category] += 1
            return
        cont: dict[str, Any] = {}
        while True:
            data = commons_api(client, action="query", list="categorymembers",
                               cmtitle=f"Category:{category}", cmtype="file|subcat", cmlimit=500, **cont)
            for m in data["query"]["categorymembers"]:
                if m["ns"] == 6:
                    file_category.setdefault(m["title"], category)
                elif m["ns"] == 14:
                    walk(m["title"].removeprefix("Category:"), depth + 1)
            if "continue" not in data:
                return
            cont = data["continue"]

    for root in COMMONS_ROOTS:
        walk(root, 0)
    log(f"  commons: {len(visited)} categories walked, {len(file_category)} files")

    records = []
    titles = list(file_category)
    for i in range(0, len(titles), 50):
        data = commons_api(client, action="query", titles="|".join(titles[i:i + 50]),
                           prop="imageinfo|coordinates", iiprop="url|mime|extmetadata",
                           iiextmetadatafilter="ImageDescription|DateTimeOriginal|LicenseShortName|Artist")
        for page in data["query"]["pages"].values():
            info = (page.get("imageinfo") or [None])[0]
            if not info or not info.get("mime", "").startswith("image/") or info["mime"] == "image/svg+xml":
                skipped["not a raster image"] += 1
                continue
            meta = {k: v.get("value") for k, v in (info.get("extmetadata") or {}).items()}
            coord = (page.get("coordinates") or [None])[0]
            lat, lon = (coord["lat"], coord["lon"]) if coord else (None, None)
            category = file_category[page["title"]]
            if coord and not regions.in_ny(lat, lon):
                # Parent categories are multi-state (e.g. the July 2023
                # northeastern flash floods); coordinates settle it.
                skipped["coordinates outside NY"] += 1
                continue
            if coord:
                in_nyc, basis = regions.in_nyc(lat, lon), "coordinates"
            elif COMMONS_NYC_CATEGORY.search(category):
                in_nyc, basis = True, "category"
            else:
                in_nyc, basis = None, None
            taken = strip_html(meta.get("DateTimeOriginal"))
            day = re.search(r"\d{4}-\d{2}-\d{2}", taken or "")
            name = page["title"].removeprefix("File:")
            records.append(make_record(
                "wikimedia_commons", info["descriptionurl"], info["url"],
                title=name.rsplit(".", 1)[0], text=strip_html(meta.get("ImageDescription")),
                date=day.group(0) if day else None, event=category,
                lat=lat, lon=lon, in_nyc=in_nyc, nyc_basis=basis,
                license=meta.get("LicenseShortName"), credit=strip_html(meta.get("Artist")),
                extra={"date_original": taken, "mime": info.get("mime")},
            ))
    log(f"  commons: {len(records)} images; skipped {dict(skipped)}")
    return records


def collect_napsg(client: Client, regions: Regions) -> list[dict[str, Any]]:
    feats = arcgis_features(
        client, NAPSG_LAYER, "objectid",
        where="(Incident_Type = 'Tropical Cyclone' OR Incident LIKE '%Flood%' OR Incident LIKE '%Hurricane%')"
              " AND Removal_Reason IS NULL",
        geometry=Regions.envelope(regions.ny), geometryType="esriGeometryEnvelope",
        inSR=4326, spatialRel="esriSpatialRelIntersects", returnGeometry="true", outFields="*",
    )
    kept, skipped = [], Counter()
    for f in feats:
        g = f.get("geometry") or {}
        if regions.in_ny(g.get("y"), g.get("x")):
            kept.append(f)
        else:
            skipped["outside NY"] += 1

    attachments: dict[int, list[dict[str, Any]]] = {}
    for i in range(0, len(kept), 100):
        ids = ",".join(str(f["attributes"]["objectid"]) for f in kept[i:i + 100])
        data = arcgis(client, NAPSG_LAYER, "queryAttachments", objectIds=ids, returnUrl="true")
        for group in data.get("attachmentGroups", []):
            attachments[group["parentObjectId"]] = group["attachmentInfos"]

    records = []
    for f in kept:
        a, g = f["attributes"], f["geometry"]
        lat, lon = g["y"], g["x"]
        oid = a["objectid"]
        source = a.get("Photo_Source") or ""
        source_url = source if source.startswith("http") else f"{NAPSG_LAYER}/{oid}"
        photos = [x for x in attachments.get(oid, []) if (x.get("contentType") or "").startswith("image/")]
        if not photos:
            skipped["no image attachment"] += 1
        for att in photos:
            records.append(make_record(
                "napsg_photomappers", source_url, att["url"],
                title=a.get("Title"), text=join_text(a.get("Title"), a.get("Description")),
                date=epoch_ms_to_iso(a.get("Date_Time") or a.get("CreationDate")), event=a.get("Incident"),
                lat=lat, lon=lon, in_nyc=regions.in_nyc(lat, lon), nyc_basis="coordinates",
                credit=a.get("photographer") or a.get("Photo_Who"),
                extra={"objectid": oid, "incident_type": a.get("Incident_Type"),
                       "photo_categories": a.get("photo_categories"), "photo_when": a.get("Photo_When"),
                       "community_impact": a.get("Community_Impact"), "damage_score": a.get("damage_score"),
                       "geolocated_by": a.get("how_did_you_geolocate_this_phot"),
                       "attachment_name": att.get("name")},
            ))
    log(f"  napsg: {len(feats)} in the prefilter box -> {len(records)} images; skipped {dict(skipped)}")
    return records


SOURCES = {
    "mycoast": collect_mycoast,
    "stn": collect_stn,
    "commons": collect_commons,
    "napsg": collect_napsg,
}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def read_existing(path: Path) -> dict[str, dict[str, Any]]:
    """Existing rows keyed by record_id. A file that exists but will not parse
    raises: continuing would write this run's rows over everything else."""
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"{path} exists but could not be read ({exc}); fix or move it and re-run") from exc
    return {r["record_id"]: r for r in rows}


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    """Atomic replace, fsynced first (NFS), partial .tmp removed on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def exclusive_run(target: Path):
    """Two concurrent runs would each merge into a stale read of the output."""
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"another run holds {lock}")
        yield


def summarize(rows: list[dict[str, Any]]) -> None:
    by_source = Counter(r["source"] for r in rows)
    for source, n in sorted(by_source.items()):
        mine = [r for r in rows if r["source"] == source]
        log(f"  {source:20s} {n:6d} images | coords {sum(r['lat'] is not None for r in mine):6d}"
            f" | in NYC {sum(r['in_nyc'] is True for r in mine):6d}"
            f" | NYC unknown {sum(r['in_nyc'] is None for r in mine):6d}")
    log(f"  {'total':20s} {len(rows):6d}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default="all", help=f"comma-separated: {','.join(SOURCES)} (default all)")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--commons-delay", type=float, default=1.0,
                    help="seconds between Commons API calls (it returns 429 to bursts)")
    args = ap.parse_args()

    names = list(SOURCES) if args.sources == "all" else [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        ap.error(f"unknown source(s) {unknown}; choose from {list(SOURCES)}")

    client = Client(host_delay={urlparse(COMMONS_API).netloc: args.commons_delay})
    with exclusive_run(args.output):
        existing = read_existing(args.output)     # read first: fail before any network work
        regions = Regions(client)
        fresh: dict[str, dict[str, Any]] = {}
        for name in names:
            log(f"collecting {name}...")
            for rec in SOURCES[name](client, regions):
                fresh[rec["record_id"]] = rec

        merged = {**existing, **fresh}
        rows = sorted(merged.values(), key=lambda r: (r["source"], r["date"] or "", r["record_id"]))
        write_json(args.output, rows)
        log(f"wrote {args.output}: {len(existing)} existing + {len(fresh)} collected -> {len(rows)} rows")
        summarize(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

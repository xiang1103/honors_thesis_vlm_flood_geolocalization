'''
Scrape news outlets for recent floods and build a JSON dataset.

Pipeline
--------
1. Query GDELT DOC 2.0 API for articles (default: the GKG flood-disaster
   theme, which is far more precise than the bare word "flood").  GDELT
   indexes global news every 15 min and returns, per article: url, title,
   seendate, and a lead "socialimage".  This solves the "how do I find
   flood articles" problem without per-site crawling.  Note: GDELT rate-
   limits aggressively (HTTP 429), so keep to one search per run.
2. For each article URL, use trafilatura to extract the full body text
   and any inline image links.  (If trafilatura is unavailable or an
   extraction fails, we fall back to GDELT metadata only.)
2b. Additively recover gallery/carousel/slideshow images (with captions)
   that trafilatura's main-content filter drops -- see
   extract_gallery_images().  This only adds images trafilatura missed.
3. Merge, dedupe, and store as JSON:

    [
        {
            "title": str,
            "date": str,          # ISO 8601, e.g. "2026-08-31T14:00:00Z"
            "all_text": str,
            "image_links": [str, ...],          # flat list, all sources
            "gallery_images": [                 # captioned gallery subset
                {"url": str, "caption": str}, ...
            ]
        },
        ...
    ]

Usage
-----
    # Default: flood-disaster theme, last 7 days, up to 100 articles.
    python news_scrape.py --out flood_news.json

    # Custom keyword query and window:
    python news_scrape.py --query '"flash flood"' --timespan 24h --max 50
'''

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
import trafilatura
from lxml import html as lxml_html


GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# GDELT GKG theme for flood/flash-flood disaster coverage. Using the theme
# instead of the bare word "flood" avoids false positives like "flooded the
# market" or "flood of demand".
FLOOD_THEME_QUERY = "theme:NATURAL_DISASTER_FLOOD"


def search_gdelt(query=FLOOD_THEME_QUERY, max_records=100, timespan="7d",
                 language="english")->list:
    '''Return a list of article records from the GDELT DOC 2.0 API.

    Each record is a dict with at least: url, title, seendate, socialimage,
    domain.  max_records is capped at 250 by GDELT per request.
    '''
    params = {
        "query": f'{query} sourcelang:{language}' if language else query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": min(max_records, 250),
        "timespan": timespan,
        "sort": "DateDesc",
    }
    # GDELT is slow (20s+), rate-limits aggressively (HTTP 429, ~1 req/5s),
    # and occasionally times out; retry with exponential backoff.
    last_err = None
    for attempt in range(4):
        try:
            resp = requests.get(GDELT_DOC_API, params=params,
                                headers={"User-Agent": USER_AGENT},
                                timeout=90)
            
            # rate limited, wait more 
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            last_err = e
            print(f"  ! GDELT request failed (attempt {attempt + 1}/4): {e}",
                  file=sys.stderr)
            time.sleep(5 * (attempt + 1))
            continue
        # GDELT occasionally returns an empty body or non-JSON on rate limit.
        try:
            data = resp.json()
        except ValueError:
            print(f"  ! GDELT returned non-JSON (rate limited?): "
                  f"{resp.text[:120]!r}", file=sys.stderr)
            return []
        return data.get("articles", [])
    raise last_err


def normalize_gdelt_date(seendate):
    '''Convert GDELT's "YYYYMMDDTHHMMSSZ" to ISO 8601, or return as-is.'''
    if not seendate:
        return ""
    try:
        dt = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ")
        return dt.replace(tzinfo=timezone.utc).isoformat().replace(
            "+00:00", "Z")
    except ValueError:
        return seendate


# ---------------------------------------------------------------------------
# Gallery / carousel / slideshow image extraction
#
# trafilatura only keeps images inside the main-content block it detects, so it
# routinely drops gallery/carousel/slideshow photos even when they are present
# in the *static* HTML.  This section recovers those images together with their
# captions, straight from the same HTML trafilatura already downloaded.
#
# It is deliberately scoped to STATIC images only (no lazy-loaded / JS-rendered
# images).  It is applied ON TOP of trafilatura's output in extract_article():
# it only ever *adds* images trafilatura missed, and never removes anything.
# ---------------------------------------------------------------------------

# CSS class/id substrings (lower-cased) that mark a gallery-like container.
GALLERY_HINTS = (
    "gallery", "carousel", "slideshow", "slider", "swiper",
    "splide", "flickity", "slick", "lightbox", "wp-block-gallery",
)

# URL/filename substrings that mark non-editorial images we never want.
_JUNK_URL_RE = re.compile(
    r"(sprite|logo|icon|avatar|placeholder|blank|pixel|tracking|spacer|"
    r"1x1|/ads?/|doubleclick|gravatar)", re.I)

# XPath: descendants <img> of any element whose class/id contains a hint.
# ($hint is bound per call so the hint can't break the expression.)
_GALLERY_XPATH = (
    "//*[contains(translate(@class, "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), $hint) "
    "or contains(translate(@id, "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), $hint)]"
    "//img")


def _largest_srcset_url(srcset):
    '''Return the highest-resolution candidate URL from a srcset attribute.'''
    best_url, best_width = "", -1
    for candidate in srcset.split(","):
        parts = candidate.split()
        if not parts:
            continue
        width = 0
        if len(parts) > 1 and parts[1].endswith("w"):
            try:
                width = int(parts[1][:-1])
            except ValueError:
                width = 0
        if width >= best_width:
            best_width, best_url = width, parts[0]
    return best_url


def _image_url(img, base_url):
    '''Best static, absolute URL for an <img>: largest srcset, else src.'''
    srcset = img.get("srcset")
    url = _largest_srcset_url(srcset) if srcset else ""
    if not url:
        url = img.get("src", "")
    if not url or url.startswith("data:"):
        return ""
    return urljoin(base_url, url)


def _is_junk_image(url, img):
    '''True for icons/logos/tracking pixels, vector, or tiny declared images.'''
    if _JUNK_URL_RE.search(url):
        return True
    if url.lower().split("?", 1)[0].endswith(".svg"):
        return True
    for dim in ("width", "height"):
        value = img.get(dim, "")
        if value.isdigit() and int(value) < 200:
            return True
    return False


def _is_boilerplate(img):
    '''True if the <img> sits outside the article body and should be dropped.

    Two signals, both observed on real pages:
      * it is inside a <nav>/<aside>/<footer>/<header> region, or
      * it is wrapped in a link to another *page* -- a "related article" /
        recirculation thumbnail.  A link to an image *file* is treated as a
        lightbox / full-res link and kept.
    '''
    if img.xpath("./ancestor::*[self::nav or self::aside or self::footer "
                 "or self::header]"):
        return True
    for anchor in img.xpath("./ancestor::a[@href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        path = href.lower().split("?", 1)[0]
        if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            continue  # lightbox / full-resolution image link -> keep
        return True   # links to another page -> recirculation thumbnail
    return False


def _image_caption(img):
    '''Caption for an <img>: enclosing <figcaption>, else alt/aria-label.'''
    figure = img.xpath("./ancestor::figure[1]")
    if figure:
        figcaption = figure[0].xpath(".//figcaption")
        if figcaption:
            text = figcaption[0].text_content().strip()
            if text:
                return " ".join(text.split())
    for attr in ("data-caption", "aria-label", "title", "alt"):
        value = (img.get(attr) or "").strip()
        if value:
            return " ".join(value.split())
    return ""


def extract_gallery_images(html, base_url):
    '''Return [{"url", "caption"}] for gallery/carousel/slideshow images.

    Also includes <figure> images, since a <figure> natively pairs an image
    with its <figcaption>.  Static HTML only; icons/logos/tiny images filtered;
    deduped by URL within the page.  Never raises: returns [] on any problem.
    '''
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return []

    # Candidate <img> nodes: those inside a gallery-like container, plus every
    # <figure> image (the reliable image+caption pairing).
    candidates = []
    for hint in GALLERY_HINTS:
        candidates += tree.xpath(_GALLERY_XPATH, hint=hint)
    candidates += tree.xpath("//figure//img")

    images = []
    seen = set()
    for img in candidates:
        url = _image_url(img, base_url)
        if not url or url in seen or _is_junk_image(url, img):
            continue
        if _is_boilerplate(img):
            continue
        seen.add(url)
        images.append({"url": url, "caption": _image_caption(img)})
    return images


def extract_article(url):
    '''Fetch a URL and extract {text, images, title, date} via trafilatura.

    Returns a dict; any field may be empty on failure.
    '''
    result = {"text": "", "images": [], "title": "", "date": "",
              "gallery_images": []}

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return result

    # bare_extraction gives us structured metadata + text in one pass.
    try:
        data = trafilatura.bare_extraction(
            downloaded,
            include_images=True,
            with_metadata=True,
            favor_precision=True,
            as_dict=True,
        )
    except Exception:
        data = None

    if data:
        result["text"] = data.get("text", "") or ""
        result["title"] = data.get("title", "") or ""
        result["date"] = data.get("date", "") or ""
        # Lead image from OpenGraph/metadata.
        lead = data.get("image")
        if lead:
            result["images"].append(lead)

    # Pull inline <graphic src=...> images from the XML rendering.
    try:
        xml = trafilatura.extract(
            downloaded, include_images=True, output_format="xml",
        )
        if xml:
            for line in xml.splitlines():
                if "<graphic" in line and 'src="' in line:
                    src = line.split('src="', 1)[1].split('"', 1)[0]
                    if src:
                        result["images"].append(src)
    except Exception:
        pass

    # Additive step: recover gallery/carousel/slideshow images (with captions)
    # that trafilatura's main-content filter dropped, from the same HTML.
    result["gallery_images"] = extract_gallery_images(downloaded, url)

    return result


def build_dataset(query=FLOOD_THEME_QUERY, max_records=100, timespan="7d",
                  language="english", polite_delay=0.5):
    '''Run the full pipeline and return a list of dataset records.'''
    print(f"Querying GDELT for '{query}' (last {timespan}, "
          f"up to {max_records} articles)...")
    articles = search_gdelt(query, max_records, timespan, language)
    print(f"  -> {len(articles)} articles returned")

    dataset = []
    seen_urls = set()

    # iterate through all collected articles 
    for i, art in enumerate(articles, 1):
        url = art.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        title = art.get("title", "")
        date = normalize_gdelt_date(art.get("seendate", ""))
        images = []
        social = art.get("socialimage")
        if social:
            images.append(social)

        # gte the text from the article for more accurate information 
        extracted = extract_article(url)

        title = extracted["title"] or title
        date = date or normalize_gdelt_date(extracted["date"])
        images.extend(extracted["images"])

        # Fold gallery URLs into the flat image_links list (dedupe drops any
        # that trafilatura/GDELT already found), and keep the captioned
        # gallery entries in their own field.
        gallery = extracted["gallery_images"]
        images.extend(g["url"] for g in gallery)

        record = {
            "title": title.strip(),
            "date": date,
            "all_text": clean_text(extracted["text"]),
            "image_links": dedupe_preserve_order(images),
            "gallery_images": gallery,
            "url": url,
        }
        dataset.append(record)

        # don't query as often
        if polite_delay:
            time.sleep(polite_delay)

    return dataset


_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def clean_text(text):
    '''Strip trafilatura's inline ![](url) image markers and blank lines.

    The image URLs are captured separately in image_links, so removing the
    markers leaves clean prose for the all_text field.
    '''
    if not text:
        return ""
    text = _MD_IMAGE_RE.sub("", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    return "\n".join(lines)


def dedupe_preserve_order(items):
    seen = set()
    out = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Scrape recent flood news into a JSON dataset via GDELT.")
    parser.add_argument("--query", default=FLOOD_THEME_QUERY,
                        help="GDELT search query. Default uses the flood "
                             "disaster theme for precision; pass e.g. "
                             "'\"flash flood\"' for a keyword search.")
    parser.add_argument("--max", type=int, default=250, dest="max_records",
                        help="Max articles to fetch (GDELT caps at 250)")
    parser.add_argument("--timespan", default="7d",
                        help="Lookback window, e.g. 24h, 7d, 1m (default: 7d)")
    parser.add_argument("--language", default="english",
                        help="Source language filter (default: english)")
    parser.add_argument("--delay", type=float, default=0.2,
                        help="Politeness delay between article fetches (s)")
    args = parser.parse_args()

    start_time=time.time() 
    dataset = build_dataset(
        query=args.query,
        max_records=args.max_records,
        timespan=args.timespan,
        language=args.language,
        polite_delay=args.delay,
    )

    output_path = f"/home/liu47/vlm_flood/data/gdelt_{args.timespan} | {len(dataset)} News.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    n_text = sum(1 for r in dataset if r["all_text"])
    n_img = sum(len(r["image_links"]) for r in dataset)
    print(f"  {n_text} with full text, {n_img} total image links")
    end_time= time.time() 
    print(f"Scraping took {end_time-start_time:3f} seconds")

if __name__ == "__main__":
    main()

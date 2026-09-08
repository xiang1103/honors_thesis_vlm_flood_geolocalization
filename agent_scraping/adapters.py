"""Per-outlet scraping adapters.

Everything outlet-specific lives here: how to DISCOVER flood article URLs and
how to pull (image, caption) pairs out of that outlet's markup. The generic
fetch/extract machinery is in `scrape.py`.

Outlet choice is justified empirically in design.md. Short version: CBS is
primary because its /tag/flooding/ page paginates (~900-1400 articles) and uses
clean <figure>/<figcaption>; AP and NBC are secondary, lower-volume sources.
"""
import re
import time
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------- shared bits

# Images that are never article content: chrome, tracking, branding.
JUNK_URL = re.compile(
    r"(logo|icon|sprite|avatar|placeholder|pixel|tracker|blank|spacer|"
    r"1x1|transparent|badge|button|social)", re.I)

# An image whose ancestor carries one of these classes is site furniture or a
# link to a DIFFERENT article ("recirculation"), not part of this story.
# Without this, AP flood articles return photos of unrelated news. See design.md.
RECIRC_ANCESTOR = re.compile(
    r"PagePromo|PageList|Promo|Recirc|Related|Trending|MoreFrom|Outbrain|Taboola|"
    r"Navigation|nav-|masthead|sidebar|footer|header|Author|newsletter|advert",
    re.I)

MIN_DIM = 200  # declared width/height below this => thumbnail/icon


def absolutize(base, url):
    if not url:
        return None
    url = url.strip()
    if url.startswith("data:"):
        return None
    return urljoin(base, url)


def best_from_srcset(srcset):
    """srcset is static markup, so taking the highest-res variant is free quality."""
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        u = bits[0]
        w = -1
        if len(bits) > 1:
            m = re.match(r"(\d+)[wx]", bits[1])
            if m:
                w = int(m.group(1))
        if w > best_w:
            best, best_w = u, w
    return best


def img_url(img, base):
    """Pick the best URL for an <img>, preferring high-res srcset variants."""
    for attr in ("srcset", "data-srcset"):
        ss = img.get(attr)
        if ss:
            u = absolutize(base, best_from_srcset(ss))
            if u:
                return u
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        u = absolutize(base, img.get(attr))
        if u and not u.endswith(".svg"):
            return u
    return None


def is_junk(img, url):
    if not url or url.lower().endswith(".svg"):
        return True
    if JUNK_URL.search(urlparse(url).path):
        return True
    for attr in ("width", "height"):
        v = img.get(attr)
        if v and v.strip().isdigit() and int(v) < MIN_DIM:
            return True
    return False


def is_recirculation(img):
    """True if any ancestor marks this as furniture or another article's promo."""
    p = img.getparent()
    depth = 0
    while p is not None and depth < 12:
        blob = (p.get("class") or "") + " " + (p.get("id") or "") + " " + str(p.tag)
        if RECIRC_ANCESTOR.search(blob):
            return True
        # An <img> wrapped in a link to a different ARTICLE is a promo thumbnail.
        # A link to an image FILE is a lightbox/full-res link, which we keep.
        if p.tag == "a":
            href = p.get("href") or ""
            if href and not re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", href, re.I):
                return True
        p = p.getparent()
        depth += 1
    return False


def caption_for(img):
    """<figcaption> wins; fall back to alt/aria-label/title."""
    fig = img.xpath("ancestor::figure[1]")
    if fig:
        cap = " ".join(t.strip() for t in fig[0].xpath(".//figcaption//text()") if t.strip())
        if cap:
            return re.sub(r"\s+", " ", cap).strip()
    for attr in ("alt", "aria-label", "title"):
        v = (img.get(attr) or "").strip()
        if v:
            return re.sub(r"\s+", " ", v)
    return ""


# ------------------------------------------------------------------ video

#: iframe hosts that actually carry article video (not ads/analytics)
VIDEO_IFRAME = re.compile(r"(youtube\.com/embed|youtu\.be|player\.vimeo|"
                          r"dailymotion\.com/embed|jwplayer|brightcove)", re.I)


def _walk_json(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk_json(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_json(v)


def iso8601_duration_to_seconds(d):
    """'PT45S' / 'PT1M30S' -> int seconds. None if unparseable."""
    if not d or not isinstance(d, str):
        return None
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$", d.strip())
    if not m:
        return None
    dd, hh, mm, ss = (float(x) if x else 0 for x in m.groups())
    total = dd * 86400 + hh * 3600 + mm * 60 + ss
    return int(total) or None


def extract_videos(doc, base_url, json_objects):
    """Article video via four strategies, because outlets differ sharply:

    - JSON-LD ``VideoObject``  -- richest (Fox: name/description/duration/thumb)
    - ``<video>`` + ``<source>`` -- CBS ships a real media URL here
    - ``og:video``             -- CBS fallback when <source> is JS-injected
    - ``<iframe>``             -- YouTube/Vimeo/Brightcove embeds

    Returns [{url, caption, thumbnail, duration_s, source}].
    """
    out, seen = [], set()

    page = (base_url or "").rstrip("/")

    def add(url, caption, thumb, dur, src):
        u = absolutize(base_url, url)
        if not u or u in seen:
            return
        # A "video" whose URL is just the article page is not a video. AP's
        # VideoObject often omits contentUrl/embedUrl entirely, and falling
        # back to its `url` field yields the page itself.
        if u.rstrip("/") == page:
            return
        seen.add(u)
        out.append({"url": u,
                    "caption": re.sub(r"\s+", " ", (caption or "")).strip(),
                    "thumbnail": absolutize(base_url, thumb) if thumb else None,
                    "duration_s": dur,
                    "source": src})

    # 1. JSON-LD VideoObject
    for o in json_objects:
        for node in _walk_json(o):
            if not isinstance(node, dict):
                continue
            if str(node.get("@type", "")).lower() != "videoobject":
                continue
            thumb = node.get("thumbnailUrl")
            if isinstance(thumb, list):
                thumb = thumb[0] if thumb else None
            cap = node.get("description") or node.get("name") or ""
            # contentUrl/embedUrl only -- never `url`, which is the page.
            add(node.get("contentUrl") or node.get("embedUrl"),
                cap, thumb, iso8601_duration_to_seconds(node.get("duration")),
                "jsonld")

    # 2. <video> / <source>
    for v in doc.xpath("//video"):
        if is_recirculation(v):
            continue
        cap = caption_for(v) or " ".join(
            t.strip() for t in v.xpath("ancestor::figure[1]//figcaption//text()") if t.strip())
        poster = v.get("poster")
        url = v.get("src") or (v.xpath(".//source/@src") or [None])[0]
        if url:
            add(url, cap, poster, None, "video")

    # 3. og:video (CBS injects <source> via JS, so this is the reliable one)
    for m in doc.xpath("//meta[@property='og:video' or @property='og:video:url' "
                       "or @property='og:video:secure_url']/@content"):
        desc = doc.xpath("//meta[@property='og:description']/@content")
        add(m, desc[0] if desc else "", None, None, "og")

    # 4. embeds
    for f in doc.xpath("//iframe/@src"):
        if f and VIDEO_IFRAME.search(f):
            add(f, "", None, None, "iframe")

    return out


# ------------------------------------------------------------------- adapters

class Adapter:
    name = "base"
    host = ""
    #: xpath for images that are genuinely part of the story body
    body_xpath = "//article | //main"
    #: rough ceiling on reachable articles, for honest logging (see design.md)
    expected_ceiling = None
    #: True only if discover() returns ONE reverse-chronological run. Adapters
    #: that concatenate several indexes (CBS tags, AP hubs) are False, because
    #: the date resets at each index boundary and an early stop keyed on
    #: "N consecutive old articles" would discard every later index.
    chronological = True

    def discover_url(self, page):
        """URL of the Nth listing page (1-indexed). None => no more pages."""
        raise NotImplementedError

    def article_links(self, doc):
        """Article URLs from a listing page document."""
        raise NotImplementedError

    def videos(self, doc, base_url, json_objects):
        """[{url, caption, thumbnail, duration_s, source}] for one article."""
        return extract_videos(doc, base_url, json_objects)

    def discover(self, fetch, max_pages, log=print):
        """Walk listing pages and return article URLs.

        Overridden by outlets whose index is a JSON API rather than HTML.
        """
        from lxml import html as lhtml
        urls, empty = [], 0
        for page in range(1, max_pages + 1):
            listing = self.discover_url(page)
            if listing is None:
                break
            r = fetch(listing)
            if r is None:
                log(f"  listing page {page}: unreachable -- stop")
                break
            try:
                found = self.article_links(lhtml.fromstring(r.content))
            except Exception:
                found = []
            new = [u for u in dict.fromkeys(found) if u not in urls]
            urls.extend(new)
            if page % 10 == 0 or page <= 3:
                log(f"  page {page}: +{len(new)} (total {len(urls)})")
            time.sleep(0.35)          # politeness: 200 listing pages otherwise burst
            empty = empty + 1 if not found else 0
            if empty >= 4:
                log(f"  4 empty pages -- end of feed at page {page}")
                break
        return urls

    def images(self, doc, base_url):
        """[{url, caption, source}] for one article document."""
        out, seen = [], set()
        bodies = doc.xpath(self.body_xpath)
        for body in bodies:
            for img in body.xpath(".//img"):
                if is_recirculation(img):
                    continue
                u = img_url(img, base_url)
                if not u or is_junk(img, u) or u in seen:
                    continue
                cap = caption_for(img)
                if len(cap) < 25:          # captionless => low value for a VLM
                    continue
                seen.add(u)
                src = "figure" if img.xpath("ancestor::figure[1]") else "body"
                out.append({"url": u, "caption": cap, "source": src})
        return out


class CBSAdapter(Adapter):
    """PRIMARY. Editorially tagged, genuinely paginated, clean <figure> markup.

    Several flood-adjacent tags are merged for volume. `flooding` and
    `flash-flooding` are direct; `tropical-storm`, `hurricane`, `landslide` and
    `severe-weather` are adjacent and frequently carry flood imagery. They are
    included deliberately and left for `verify.py` to score rather than being
    dropped at crawl time -- filter on `flood_verified` downstream.

    Tags confirmed to exist (others 404): floods, flood, storms,
    extreme-weather, natural-disasters, rain, monsoon all return 404.
    """
    name = "cbs"
    host = "https://www.cbsnews.com"
    expected_ceiling = 2500
    # section.list-river is the true paginated feed. The larger
    # 'view-bulk-component' block is boilerplate repeated on every page.
    body_xpath = "//article | //div[contains(@class,'content__body')] | //main"
    TAGS = ["flooding", "flash-flooding", "tropical-storm", "hurricane",
            "landslide", "severe-weather"]
    chronological = False        # concatenated per-tag runs; see base class

    def _tag_url(self, tag, page):
        return f"{self.host}/tag/{tag}/" if page == 1 else f"{self.host}/tag/{tag}/{page}/"

    def discover_url(self, page):
        return self._tag_url("flooding", page)

    def article_links(self, doc):
        hrefs = doc.xpath("//section[contains(@class,'list-river')]//a[contains(@href,'/news/')]/@href")
        return [absolutize(self.host, h.split("?")[0]) for h in hrefs]

    def discover(self, fetch, max_pages, log=print):
        from lxml import html as lhtml
        urls = []
        for tag in self.TAGS:
            before, empty = len(urls), 0
            for page in range(1, max_pages + 1):
                r = fetch(self._tag_url(tag, page))
                if r is None:
                    break
                try:
                    found = self.article_links(lhtml.fromstring(r.content))
                except Exception:
                    found = []
                urls.extend(u for u in dict.fromkeys(found) if u not in urls)
                time.sleep(0.35)
                empty = empty + 1 if not found else 0
                if empty >= 4:
                    break
            log(f"  tag {tag}: +{len(urls) - before} (total {len(urls)})")
        return urls


class APAdapter(Adapter):
    """SECONDARY. No <figure>; captions live in img@alt. Body must be scoped
    to RichTextStoryBody or recirculation photos leak in (see design.md)."""
    name = "ap"
    host = "https://apnews.com"
    expected_ceiling = 30
    body_xpath = "//div[contains(@class,'RichTextStoryBody')] | //bsp-story-page"

    #: AP hubs have no pagination (JS 'load more'), so breadth comes from
    #: querying several flood-adjacent hubs instead of paging one.
    HUBS = ["floods", "hurricanes", "climate-and-environment", "severe-weather"]
    chronological = False        # concatenated per-hub runs

    def discover_url(self, page):
        return f"{self.host}/hub/{self.HUBS[page - 1]}" if page <= len(self.HUBS) else None

    def article_links(self, doc):
        out = []
        for a in doc.xpath("//a[@href]"):
            h = a.get("href") or ""
            if h.startswith("/"):
                h = self.host + h
            if "/article/" in h and re.search(r"flood|deluge|inundat|levee", h, re.I):
                out.append(h.split("?")[0])
        return out


class NBCAdapter(Adapter):
    """SECONDARY. Clean figures, but the weather section yields few floods."""
    name = "nbc"
    host = "https://www.nbcnews.com"
    expected_ceiling = 10
    body_xpath = "//article | //div[contains(@class,'article-body')] | //main"

    def discover_url(self, page):
        return f"{self.host}/news/weather" if page == 1 else None

    def article_links(self, doc):
        out = []
        for a in doc.xpath("//a[@href]"):
            h = a.get("href") or ""
            if h.startswith("/"):
                h = self.host + h
            txt = " ".join(a.itertext())
            if "nbcnews.com/news/" in h and re.search(r"flood|deluge|inundat|levee", h + " " + txt, re.I):
                out.append(h.split("?")[0])
        return out


class FoxAdapter(Adapter):
    """SECONDARY. Fox's flood list is entirely JS-rendered -- the static
    category page has ZERO article links. Its internal article-search API works
    but only with searchBy=tags (categories returns []), and it ignores
    `offset`/`size`: 30 items is a hard ceiling per tag. Two flood-ish tags are
    queried and merged to widen coverage slightly."""
    name = "fox"
    host = "https://www.foxnews.com"
    expected_ceiling = 60
    body_xpath = ("//div[contains(@class,'article-body')] | //article | //main")
    TAGS = ["fox-news/us/disasters/floods", "fox-news/us/disasters"]

    def discover_url(self, page):
        return None

    def article_links(self, doc):
        return []

    def discover(self, fetch, max_pages, log=print):
        import json as _json
        urls = []
        for tag in self.TAGS:
            u = (f"{self.host}/api/article-search?searchBy=tags"
                 f"&values={tag}&size=30&offset=0")
            r = fetch(u)
            if r is None:
                continue
            try:
                items = _json.loads(r.text)
            except Exception:
                items = []
            got = [i["url"] for i in items
                   if isinstance(i, dict) and i.get("url", "").startswith("http")]
            new = [g for g in got if g not in urls]
            urls.extend(new)
            log(f"  tag {tag}: +{len(new)} (total {len(urls)})")
        return urls


class NPRAdapter(Adapter):
    """SECONDARY. No working flood tag page; the weather section is the closest
    static index, so links are keyword-filtered for flood relevance."""
    name = "npr"
    host = "https://www.npr.org"
    expected_ceiling = 30
    body_xpath = "//div[@id='storytext'] | //article | //main"

    def discover_url(self, page):
        return f"{self.host}/sections/weather/" if page == 1 else None

    def article_links(self, doc):
        out = []
        for a in doc.xpath("//a[@href]"):
            h = (a.get("href") or "").split("?")[0]
            txt = " ".join(a.itertext())
            if re.search(r"npr\.org/20\d\d/", h) and re.search(
                    r"flood|deluge|inundat|levee|storm|hurricane|rain", h + " " + txt, re.I):
                out.append(h)
        return out


class CNNAdapter(Adapter):
    """SECONDARY, RECENT-ONLY. CNN's search API rejects every request with
    'missing request id' (an internal header we can't forge), so there is no
    archive access. The Google-News sitemap is the only static index, and it
    covers roughly the last 48 hours -- so CNN cannot satisfy a 1-year window."""
    name = "cnn"
    host = "https://www.cnn.com"
    expected_ceiling = 15
    body_xpath = "//div[contains(@class,'article__content')] | //article | //main"

    def discover_url(self, page):
        return f"{self.host}/sitemaps/cnn/news.xml" if page == 1 else None

    def article_links(self, doc):
        out = []
        for loc in doc.xpath("//*[local-name()='url']"):
            u = loc.xpath("./*[local-name()='loc']/text()")
            t = loc.xpath(".//*[local-name()='title']/text()")
            if not u:
                continue
            blob = (u[0] + " " + (t[0] if t else ""))
            if re.search(r"flood|deluge|inundat|levee|storm surge", blob, re.I):
                out.append(u[0].split("?")[0])
        return out


ADAPTERS = {a.name: a for a in (CBSAdapter, FoxAdapter, APAdapter,
                                NBCAdapter, NPRAdapter, CNNAdapter)}


def get_adapter(name):
    if name not in ADAPTERS:
        raise SystemExit(f"unknown outlet {name!r}; choose from {sorted(ADAPTERS)}")
    return ADAPTERS[name]()

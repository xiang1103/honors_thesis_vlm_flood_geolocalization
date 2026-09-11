"""Per-outlet scraping adapters.

Everything outlet-specific lives here: how to DISCOVER flood article URLs and
how to pull (image, caption) pairs out of that outlet's markup. The generic
fetch/extract machinery is in `scrape.py`.

Two kinds of adapter live here:

* **Hand-written classes** (CBS, Fox, AP, NBC, NPR, CNN) -- outlets whose
  discovery is genuinely custom: a JSON search API, a news sitemap, or several
  tag indexes merged together.
* **TagIndexAdapter + the TAG_OUTLETS config** (18 more, added in the
  50-outlet expansion) -- outlets that all share one shape, "paginated listing
  URLs plus an article-URL pattern". Adding one of these is a dict, not a
  class.

Outlet choice is justified empirically in design.md: CBS (~2,500 articles) and
the Guardian (~2,000, deepest non-US archive) carry the volume; the rest are
kept for geographic diversity, since the largest floods of any year happen
outside the US and US outlets cover them thinly.
"""
import functools
import re
import time
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------- shared bits

# Images that are never article content: chrome, tracking, branding.
JUNK_URL = re.compile(
    r"(logo|icon|sprite|avatar|placeholder|pixel|tracker|blank|spacer|"
    r"1x1|transparent|badge|button|social|"
    # CMS theme/chrome directories never hold editorial photography. CNA
    # serves `mc_core_theme/images/inbox-large.png` next to a real caption
    # block, and it was being emitted WITH that caption attached.
    r"/themes?/|/chrome/|/ui/|/sprites?/)", re.I)

# An image whose ancestor carries one of these classes is site furniture or a
# link to a DIFFERENT article ("recirculation"), not part of this story.
# Without this, AP flood articles return photos of unrelated news. See design.md.
RECIRC_ANCESTOR = re.compile(
    r"PagePromo|PageList|Promo|Recirc|Related|Trending|MoreFrom|Outbrain|Taboola|"
    r"Navigation|nav-|masthead|sidebar|footer|header|Author|newsletter|advert",
    re.I)

MIN_DIM = 200      # declared width/height below this => thumbnail/icon
MIN_CAPTION = 25   # a shorter 'caption' is a label, not supervision signal


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


#: attributes a lazy-loader parks the REAL image URL in while `src` holds a
#: placeholder. Checked before `src` -- see the note in img_url().
LAZY_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-hi-res-src")


def img_url(img, base):
    """Pick the best URL for an <img>, preferring high-res srcset variants.

    Lazy-load attributes beat `src`, not the other way round. When a lazy
    loader is in play, `src` holds a *placeholder* and the real photo is in
    `data-src` -- Hindustan Times ships
    `src="images.hindustantimes.com/default/550x309.jpg"` on every lazy image,
    so reading `src` first stored the same grey placeholder for every article
    instead of the photo. Placeholders don't reliably look like placeholders
    (that URL matches no junk pattern), so preferring the lazy attribute
    whenever one is present is the only rule that holds.
    """
    for attr in ("srcset", "data-srcset"):
        ss = img.get(attr)
        if ss:
            u = absolutize(base, best_from_srcset(ss))
            if u:
                return u
    for attr in LAZY_ATTRS + ("src",):
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


def in_promo_link(img):
    """True if the <img> is wrapped in a link to another PAGE.

    That is the one recirculation signal that never produces false positives: a
    photo belonging to *this* story is not hyperlinked to a different article.
    A link to an image FILE is a lightbox / full-res link, which we keep.
    """
    for a in img.xpath("./ancestor::a[@href]"):
        href = (a.get("href") or "").strip()
        if href and not re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", href, re.I):
            return True
    return False


def is_recirculation(img, stop_at=None, trusted=False):
    """True if the <img> is site furniture or another article's promo.

    Two refinements over the naive ancestor scan, both forced by measurement
    (see design.md, "Extraction fixes"):

    * ``stop_at`` -- the walk stops at the article body element. Everything
      above it is page *layout*, and layout classes collide with the promo
      vocabulary: PBS wraps the whole page in
      ``div.page__body--with-sidebar``, which made every PBS photo look like a
      sidebar promo and cost the outlet 100% of its images.
    * ``trusted`` -- set for an <img> in a <figure> that has a real
      <figcaption>. That pairing IS the editorial content, so the class
      heuristic is skipped and only the promo-link test applies. Without this,
      Hindustan Times lost its lead photo to an enclosing ``taboola-readmore``
      wrapper and Inside Climate News lost its featured image to
      ``header.entry-header`` -- both false positives on real article photos.
    """
    if in_promo_link(img):
        return True
    if trusted:
        return False
    p = img.getparent()
    depth = 0
    while p is not None and depth < 12:
        if stop_at is not None and p is stop_at:
            return False
        blob = (p.get("class") or "") + " " + (p.get("id") or "") + " " + str(p.tag)
        if RECIRC_ANCESTOR.search(blob):
            return True
        p = p.getparent()
        depth += 1
    return False


#: lower-cased class/id substrings that mark a caption element. Many outlets
#: ship no <figcaption> at all and put the caption in a sibling <p>/<div>.
_LOWER = "translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
CAPTION_XPATH = (f".//*[contains({_LOWER},'caption') or contains({_LOWER},'cutline') "
                 f"or contains({_LOWER},'media-desc')]"
                 f"[not(contains({_LOWER},'byline'))]")

#: leading label some outlets prepend to the caption text ("Caption: ..." on RNZ).
CAP_PREFIX = re.compile(r"^\s*(caption|photo|image|pictured)\s*[:\-]\s*", re.I)

#: text that occupies a caption slot but is not a caption for THIS image.
BAD_CAPTION = re.compile(
    r"^(related|watch|read more|advertisement|click to play|sign up|subscribe|"
    r"grist thanks its sponsors)\b", re.I)


def _tidy(text):
    return CAP_PREFIX.sub("", re.sub(r"\s+", " ", text or "")).strip()


def caption_for(img):
    """<figcaption> wins, then a nearby caption element, then alt/title.

    The middle step matters: PBS, RNZ and others ship zero <figcaption> and
    keep the caption in a neighbouring element (``p.post__hero-caption``).
    Byline elements are excluded by CAPTION_XPATH -- PBS's
    ``div.post__byline-caption`` otherwise wins and every caption becomes the
    reporter's name.
    """
    fig = img.xpath("ancestor::figure[1]")
    if fig:
        cap = " ".join(t.strip() for t in fig[0].xpath(".//figcaption//text()") if t.strip())
        if cap:
            return _tidy(cap)
    # Climb a few levels looking for a caption element in that block, nearest
    # ancestor first.
    #
    # The block must contain exactly ONE <img>. Without that guard the caption
    # gets attached to whichever image happens to sit nearby, which produces a
    # RIGHT caption on the WRONG image -- silently corrupt data, and worse for
    # a VLM than no image at all. Measured on CNA: a `mc_core_theme` icon was
    # emitted 16 times carrying real flood captions.
    node = img
    for _ in range(4):
        node = node.getparent()
        if node is None:
            break
        if len(node.xpath(".//img")) != 1:
            continue
        for c in node.xpath(CAPTION_XPATH):
            txt = " ".join(t.strip() for t in c.itertext() if t.strip())
            if len(txt) > 15:
                return _tidy(txt)
    for attr in ("alt", "aria-label", "title", "data-caption"):
        v = (img.get(attr) or "").strip()
        if v:
            return _tidy(v)
    return ""


def has_figcaption(img):
    """True if this <img> lives in a <figure> carrying a real <figcaption>."""
    fig = img.xpath("ancestor::figure[1]")
    if not fig:
        return False
    cap = " ".join(t.strip() for t in fig[0].xpath(".//figcaption//text()") if t.strip())
    return len(_tidy(cap)) >= MIN_CAPTION


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
        """[{url, caption, source}] for one article document.

        Two passes, in priority order:

        1. **Captioned <figure> elements, document-wide.** A <figure> with a
           real <figcaption> is an editorial image+caption pair by
           construction, and it is frequently the lead photo -- which sits
           ABOVE the body container on many sites (Grist's hero figure is
           outside `.entry-content`, so a body-scoped scan misses it entirely).
        2. **Images inside the body container**, with the full furniture and
           recirculation filters applied.

        Pass 1 runs first so the lead photo is never lost to body scoping;
        `seen` keeps pass 2 from re-adding it.
        """
        out, seen = [], set()

        def take(img, trusted, body=None):
            u = img_url(img, base_url)
            if not u or u in seen or is_junk(img, u):
                return
            if is_recirculation(img, stop_at=body, trusted=trusted):
                return
            cap = caption_for(img)
            if len(cap) < MIN_CAPTION or BAD_CAPTION.search(cap):
                return              # captionless => low value for a VLM
            seen.add(u)
            out.append({"url": u, "caption": cap,
                        "source": "figure" if trusted else "body"})

        for fig in doc.xpath("//figure[.//figcaption]"):
            for img in fig.xpath(".//img"):
                if has_figcaption(img):
                    take(img, trusted=True)

        # A body_xpath that matches nothing would silently yield zero images on
        # outlets with unusual markup (Times of India has no <article>/<main>
        # at all), so fall back to the whole document -- the filters above are
        # what keep furniture out, not the container.
        for body in (doc.xpath(self.body_xpath) or doc.xpath("//body")):
            for img in body.xpath(".//img"):
                take(img, trusted=has_figcaption(img), body=body)
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
    expected_ceiling = 50
    body_xpath = "//article | //div[contains(@class,'article-body')] | //main"

    #: /news/weather yields ~5 articles; the section fronts below yield ~50,
    #: and NBC's article URLs all end in an `rcna` id, which is a far more
    #: reliable article test than requiring the literal path "/news/".
    SECTIONS = ["/weather", "/hurricanes", "/news/weather"]
    chronological = False        # concatenated section fronts

    def discover_url(self, page):
        return (self.host + self.SECTIONS[page - 1]
                if page <= len(self.SECTIONS) else None)

    def article_links(self, doc):
        out = []
        for a in doc.xpath("//a[@href]"):
            h = (a.get("href") or "").split("?")[0]
            if h.startswith("/"):
                h = self.host + h
            txt = " ".join(a.itertext())
            if not re.search(r"nbcnews\.com/[a-z-]+/[a-z-]+/[a-z0-9-]+rcna\d+", h, re.I):
                continue
            if re.search(r"flood|deluge|inundat|levee|storm|hurricane|rain|"
                         r"cyclone|typhoon|monsoon", h + " " + txt, re.I):
                out.append(h)
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


# ---------------------------------------------------------------------------
# Generic tag/topic-index adapter
#
# The six hand-written classes above exist because each of those outlets needs
# genuinely custom discovery (a JSON search API, a sitemap, per-tag merging).
# Most outlets do NOT: their flood index is just "one or more listing URLs that
# paginate, holding links that match an article-URL pattern". Writing 19 more
# near-identical classes for those would be noise, so they are declared as
# CONFIG below and share this one implementation.
#
# Every route here was verified against the live site before being added -- see
# design.md, "The 50-outlet bake-off", for the probe results, the outlets that
# were rejected, and why.
# ---------------------------------------------------------------------------

#: Body containers, in the order sites actually use them. Broad on purpose: the
#: junk/recirculation filters are what keep furniture out, not this xpath.
DEFAULT_BODY = (
    "//article | //main "
    "| //div[contains(@class,'article-body')] "
    "| //div[contains(@class,'entry-content')] "
    "| //div[contains(@class,'story-body')] "
    "| //div[contains(@class,'content-body')] "
    "| //div[@id='storytext']")

#: Flood vocabulary for outlets whose only usable index is a BROAD section
#: (a weather or climate page) rather than a flood tag.
FLOOD_KEYWORD = re.compile(
    r"flood|deluge|inundat|levee|monsoon|storm|hurricane|cyclone|typhoon|"
    r"landslide|mudslide|rain|downpour|torrential|washed[- ]away|evacuat", re.I)


class TagIndexAdapter(Adapter):
    """Config-driven adapter: paginated listing pages + an article-URL pattern.

    Parameters
    ----------
    indexes : list of (page1_url, page_n_template_or_None)
        Page 1 is fetched from ``page1_url`` verbatim, because most sites 404
        on the explicit ``/page/1/`` form. ``None`` as the template means the
        index does not paginate (its "load more" is JavaScript) -- one page is
        all that is statically reachable.
    article_re : str
        Matched against the absolute URL to separate articles from navigation.
    keyword : bool
        Require flood vocabulary in the URL or link text. Set only for outlets
        whose index is a broad weather/climate section, where the index alone
        does not imply the topic.
    """

    def __init__(self, name, host, indexes, article_re, body_xpath=None,
                 expected_ceiling=None, keyword=False, page_cap=None, note=""):
        self.name = name
        self.host = host
        self.indexes = indexes
        self.article_re = re.compile(article_re, re.I)
        self.body_xpath = body_xpath or DEFAULT_BODY
        self.expected_ceiling = expected_ceiling
        self.keyword = keyword
        self.page_cap = page_cap
        self.note = note
        # Concatenating several indexes restarts the date at each boundary, so
        # the "N consecutive old articles" early stop must not apply. See the
        # ordering hazard in design.md.
        self.chronological = len(indexes) == 1

    def discover_url(self, page):
        base, template = self.indexes[0]
        if page == 1:
            return base
        return None if template is None else template.format(n=page)

    def article_links(self, doc, base_host=None):
        out = []
        for a in doc.xpath("//a[@href]"):
            href = (a.get("href") or "").split("#")[0].strip()
            if not href or href.startswith(("javascript:", "mailto:")):
                continue
            url = absolutize(base_host or self.host, href).split("?")[0]
            if not self.article_re.search(url):
                continue
            if self.keyword:
                text = " ".join(a.itertext())
                if not FLOOD_KEYWORD.search(url + " " + text):
                    continue
            out.append(url)
        return out

    def discover(self, fetch, max_pages, log=print):
        from lxml import html as lhtml
        urls = []
        limit = min(max_pages, self.page_cap) if self.page_cap else max_pages
        for base, template in self.indexes:
            before, empty = len(urls), 0
            for page in range(1, (1 if template is None else limit) + 1):
                url = base if page == 1 else template.format(n=page)
                r = fetch(url)
                if r is None:
                    break
                try:
                    found = self.article_links(lhtml.fromstring(r.content), url)
                except Exception:
                    found = []
                new = [u for u in dict.fromkeys(found) if u not in urls]
                urls.extend(new)
                time.sleep(0.35)
                # Stop on NEW links, not on found links: a site whose "next
                # page" is JavaScript serves page 1 again for every ?page=N, so
                # keying the stop on `found` would walk 200 identical pages.
                empty = empty + 1 if not new else 0
                if empty >= 3:
                    break
            log(f"  index {base.split('//')[-1][:58]}: +{len(urls) - before} "
                f"(total {len(urls)})")
        return urls


#: name -> TagIndexAdapter kwargs. Ordered roughly by measured yield.
#: `expected_ceiling` is the honest reachable count from the probe, not a hope.
TAG_OUTLETS = [
    # ---- deep archives: genuine HTML pagination -----------------------------
    dict(name="guardian", host="https://www.theguardian.com",
         indexes=[("https://www.theguardian.com/environment/flooding",
                   "https://www.theguardian.com/environment/flooding?page={n}"),
                  ("https://www.theguardian.com/world/natural-disasters",
                   "https://www.theguardian.com/world/natural-disasters?page={n}")],
         article_re=r"theguardian\.com/[a-z-]+/(?:article/)?20\d\d/[a-z]{3}/\d\d/",
         body_xpath="//div[contains(@class,'article-body')] | //article | //main",
         expected_ceiling=1500,
         note="best non-US source: 1,453 articles discovered, 253 inside a 1-year window "
              "(795 images, 3.1/article). Widen --since-days to reach the rest"),

    dict(name="hindustantimes", host="https://www.hindustantimes.com",
         indexes=[("https://www.hindustantimes.com/topic/flood",
                   "https://www.hindustantimes.com/topic/flood/page/{n}")],
         article_re=r"hindustantimes\.com/[a-z-]+/(?:[a-z-]+/)?[a-z0-9-]+-\d{15,}\.html",
         expected_ceiling=900, keyword=True,
         note="~30 new/page; keyword filter required -- the topic page mixes in horoscopes"),

    dict(name="pbs", host="https://www.pbs.org",
         indexes=[("https://www.pbs.org/newshour/tag/flooding",
                   "https://www.pbs.org/newshour/tag/flooding/page/{n}"),
                  ("https://www.pbs.org/newshour/tag/floods",
                   "https://www.pbs.org/newshour/tag/floods/page/{n}")],
         article_re=(r"pbs\.org/newshour/(?:nation|world|science|politics|health|"
                     r"economy|arts)/[a-z0-9-]{12,}$"),
         expected_ceiling=400,
         note="captions live in p.post__hero-caption, not <figcaption>"),

    dict(name="rnz", host="https://www.rnz.co.nz",
         indexes=[("https://www.rnz.co.nz/news/weather",
                   "https://www.rnz.co.nz/news/weather?page={n}")],
         article_re=r"rnz\.co\.nz/news/[a-z-]+/\d{6,}/",
         expected_ceiling=500, keyword=True,
         note="no flood tag; the weather section paginates deeply, so keyword-filter it"),

    dict(name="cna", host="https://www.channelnewsasia.com",
         indexes=[("https://www.channelnewsasia.com/topic/flood",
                   "https://www.channelnewsasia.com/topic/flood?page={n}")],
         article_re=r"channelnewsasia\.com/[a-z-]+/[a-z0-9-]+-\d{6,}$",
         expected_ceiling=150,
         note="richest captions per article of any outlet measured (7 on one story)"),

    dict(name="floodlist", host="https://floodlist.com",
         indexes=[(f"https://floodlist.com/{r}", f"https://floodlist.com/{r}/page/{{n}}")
                  for r in ("asia", "africa", "america", "europe", "australia")],
         article_re=r"floodlist\.com/[a-z]+/[a-z0-9-]{10,}$",
         expected_ceiling=3000,
         note="ARCHIVE (stopped publishing May 2024): 3,001 articles, 89% flood_verified -- "
              "the single largest source here, but needs --since-days 0 or it yields 1"),

    dict(name="grist", host="https://grist.org",
         indexes=[("https://grist.org/extreme-weather/",
                   "https://grist.org/extreme-weather/page/{n}/")],
         article_re=r"grist\.org/[a-z-]+/[a-z0-9-]{12,}/?$",
         expected_ceiling=400, keyword=True,
         note="pagination advertised to page 80; hero figure sits OUTSIDE .entry-content"),

    dict(name="jakartapost", host="https://www.thejakartapost.com",
         indexes=[("https://www.thejakartapost.com/tag/flood",
                   "https://www.thejakartapost.com/tag/flood/page/{n}")],
         article_re=r"thejakartapost\.com/[a-z-]+/20\d\d/\d\d/\d\d/[a-z0-9-]+",
         expected_ceiling=120,
         note="Indonesia flood coverage; ~10 new/page before 404"),

    dict(name="irishtimes", host="https://www.irishtimes.com",
         indexes=[("https://www.irishtimes.com/tags/flooding/",
                   "https://www.irishtimes.com/tags/flooding/{n}/")],
         article_re=r"irishtimes\.com/[a-z0-9/-]+-1\.\d{6,}",
         expected_ceiling=0,
         note="YIELDS NOTHING: its <figure>s carry no static <img>, AND the flooding tag "
              "surfaces 2016-era articles, so a 1-year window keeps 0. Kept only as a "
              "documented dead end -- do not re-probe"),

    dict(name="premiumtimes", host="https://www.premiumtimesng.com",
         indexes=[("https://www.premiumtimesng.com/tag/flood",
                   "https://www.premiumtimesng.com/tag/flood/page/{n}/")],
         article_re=r"premiumtimesng\.com/[a-z-]+/(?:[a-z-]+/)?\d{6,}-[a-z0-9-]+\.html",
         expected_ceiling=400,
         note="MOSTLY TEXT: measured 389 records but only 32 carry an image (154 total); "
              "strong Nigerian flood reporting, so kept for text + geography"),

    # ---- single static page: 'load more' is JavaScript ----------------------
    dict(name="independent", host="https://www.independent.co.uk",
         indexes=[("https://www.independent.co.uk/topic/flooding", None)],
         article_re=r"independent\.co\.uk/[a-z0-9/-]+-b\d{6,}\.html",
         expected_ceiling=75,
         note="densest single page measured: 71 flood articles, but ?page=N 404s"),

    dict(name="toi", host="https://timesofindia.indiatimes.com",
         indexes=[("https://timesofindia.indiatimes.com/topic/flood/news", None)],
         article_re=r"indiatimes\.com/[a-z0-9/-]+/articleshow/\d+\.cms",
         expected_ceiling=70, keyword=True,
         note="68 links on one page; has no <article>/<main>, so images() falls back to //body"),

    dict(name="abcau", host="https://www.abc.net.au",
         indexes=[("https://www.abc.net.au/news/topic/floods", None)],
         article_re=r"abc\.net\.au/news/20\d\d-\d\d-\d\d/[a-z0-9-]+/\d+",
         expected_ceiling=30,
         note="Australian floods; clean captions, no static pagination"),

    dict(name="globalnews", host="https://globalnews.ca",
         indexes=[("https://globalnews.ca/tag/flooding/", None)],
         article_re=r"globalnews\.ca/news/\d{6,}/",
         expected_ceiling=20, note="Canada"),

    dict(name="ctv", host="https://www.ctvnews.ca",
         indexes=[("https://www.ctvnews.ca/climate-and-environment/", None)],
         article_re=r"ctvnews\.ca/[a-z-]+/article/[a-z0-9-]{10,}/?$",
         expected_ceiling=20, keyword=True, note="Canada, broad climate section"),

    dict(name="aljazeera", host="https://www.aljazeera.com",
         indexes=[("https://www.aljazeera.com/tag/floods/", None)],
         article_re=r"aljazeera\.com/(?:news|features|gallery|economy|climate-crisis)/20\d\d/",
         expected_ceiling=15,
         note="excellent photo essays incl. /gallery/; 'show more' is JS-only"),

    dict(name="straitstimes", host="https://www.straitstimes.com",
         indexes=[("https://www.straitstimes.com/tags/floods", None)],
         article_re=r"straitstimes\.com/(?:singapore|asia|world|business|sport|life)/[a-z0-9-]{12,}",
         expected_ceiling=15, note="Singapore / SE Asia"),

    # ---- US majors that DO allow a direct article fetch --------------------
    dict(name="latimes", host="https://www.latimes.com",
         indexes=[("https://www.latimes.com/environment", None),
                  ("https://www.latimes.com/california", None),
                  ("https://www.latimes.com/weather", None)],
         article_re=r"latimes\.com/[a-z-]+/story/20\d\d-\d\d-\d\d/[a-z0-9-]+",
         expected_ceiling=40, keyword=True,
         note="major US daily; articles fetch cleanly with captions, but no flood tag page"),

    dict(name="usatoday", host="https://www.usatoday.com",
         indexes=[("https://www.usatoday.com/news/weather/", None),
                  ("https://www.usatoday.com/news/nation/", None)],
         article_re=r"usatoday\.com/story/[a-z]+/[a-z0-9/-]+/\d{6,}/",
         expected_ceiling=15, keyword=True,
         note="weather section is flood-dense but captions are near-absent: 11 records "
              "yielded 1 image. Useful for text, not for image-caption pairs"),

    dict(name="newsweek", host="https://www.newsweek.com",
         indexes=[("https://www.newsweek.com/weather", None)],
         article_re=r"newsweek\.com/[a-z0-9-]{12,}-\d{6,}",
         expected_ceiling=25, keyword=True,
         note="/topic/flooding 406s but /weather serves fine; ?page=N also 406s"),

    dict(name="insideclimate", host="https://insideclimatenews.org",
         indexes=[("https://insideclimatenews.org/tag/flooding/", None)],
         article_re=r"insideclimatenews\.org/news/\d{8}/[a-z0-9-]+/?$",
         expected_ceiling=15, note="featured image sits in header.entry-header"),
]


def _make(cfg):
    """Bind one TAG_OUTLETS entry into a zero-arg factory.

    ADAPTERS maps name -> callable, and get_adapter() calls it with no
    arguments, so a configured adapter has to arrive as a partial rather than
    as a class.
    """
    return functools.partial(TagIndexAdapter, **cfg)


ADAPTERS.update({cfg["name"]: _make(cfg) for cfg in TAG_OUTLETS})

#: Outlets that reliably yield captioned images. `--outlets all` uses every
#: registered outlet; this is the subset to use when the (image, caption) pair
#: is what matters. irishtimes/premiumtimes are excluded: both were measured
#: text-only (see their `note`).
IMAGE_OUTLETS = [n for n in ADAPTERS if n not in ("irishtimes", "premiumtimes")]


# ---------------------------------------------------------------------------
# Feed-based adapters (the paywalled US majors)
#
# NYT, WSJ and the Washington Post all refuse a direct article fetch -- see
# design.md, "The paywalled majors". Their PUBLIC RSS feeds are a different
# matter: they are a channel the publisher operates for redistribution, they
# answer 200, and NYT's carry `media:content` + `media:description`, i.e. a
# real image paired with a real caption -- exactly the product.
#
# The catch is structural: for these outlets the FEED ITEM is the record,
# because the article page cannot be fetched at all. `fallback_record()` is
# how that reaches scrape.py.
# ---------------------------------------------------------------------------

RSS_NS = {"media": "http://search.yahoo.com/mrss/",
          "dc": "http://purl.org/dc/elements/1.1/",
          "content": "http://purl.org/rss/1.0/modules/content/"}


def _rss_text(item, tag, ns=None):
    el = item.find(tag, ns) if ns else item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def _rss_date(raw):
    """RFC-822 pubDate -> ISO 8601. Returns the raw string if unparseable."""
    if not raw:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw).isoformat()
    except Exception:
        return raw


class RSSAdapter(Adapter):
    """Discovery (and, when the article is unfetchable, extraction) via RSS.

    `feed_only=True` means the article page is known to block us, so the feed
    item is the whole record: headline, publication date, the feed summary as
    text, and the `media:content` image captioned by `media:description`.
    That is genuinely less than a scraped article -- a ~150-character summary
    instead of the body -- and is labelled as such via `source: "feed"` on the
    image and a shorter `text`. It is, however, the only lawful and
    technically available route to these outlets.
    """

    def __init__(self, name, host, feeds, body_xpath=None, expected_ceiling=None,
                 keyword=True, feed_only=False, note=""):
        self.name = name
        self.host = host
        self.feeds = feeds
        self.body_xpath = body_xpath or DEFAULT_BODY
        self.expected_ceiling = expected_ceiling
        self.keyword = keyword
        self.feed_only = feed_only
        self.note = note
        self.chronological = False      # several feeds concatenated
        self.meta = {}                  # url -> record fields parsed from the feed

    def discover_url(self, page):
        return self.feeds[0] if page == 1 else None

    def article_links(self, doc, base_host=None):
        return []

    def _parse_feed(self, content):
        from lxml import etree
        root = etree.fromstring(content)
        return root.findall(".//item")

    def discover(self, fetch, max_pages, log=print):
        urls = []
        for feed in self.feeds:
            r = fetch(feed)
            if r is None:
                log(f"  feed {feed.split('/')[-1]}: unreachable")
                continue
            try:
                items = self._parse_feed(r.content)
            except Exception as e:
                log(f"  feed {feed.split('/')[-1]}: parse error {type(e).__name__}")
                continue
            before = len(urls)
            for item in items:
                link = _rss_text(item, "link")
                title = _rss_text(item, "title")
                summary = re.sub(r"<[^>]+>", "", _rss_text(item, "description"))
                if not link or link in self.meta:
                    continue
                # Section feeds are not flood feeds, so the topic filter is
                # what makes them usable at all.
                if self.keyword and not FLOOD_KEYWORD.search(
                        f"{link} {title} {summary}"):
                    continue

                images = []
                media = (item.findall("media:content", RSS_NS)
                         or item.findall("media:thumbnail", RSS_NS))
                caption = ""
                for tag in ("media:description", "media:credit"):
                    found = item.findall(tag, RSS_NS)
                    if found and found[0].text:
                        caption = re.sub(r"\s+", " ", found[0].text).strip()
                        break
                if media:
                    murl = media[0].get("url")
                    if murl and len(caption) >= MIN_CAPTION:
                        images.append({"url": murl, "caption": caption,
                                       "source": "feed"})

                self.meta[link] = {
                    "title": title,
                    "date": _rss_date(_rss_text(item, "pubDate")),
                    "text": summary,
                    "images": images,
                }
                urls.append(link)
            log(f"  feed {feed.split('/')[-1]}: +{len(urls) - before} "
                f"(total {len(urls)})")
            time.sleep(0.35)
        return urls

    def fallback_record(self, url):
        """Record built purely from the feed, for an unfetchable article."""
        m = self.meta.get(url)
        if not m:
            return None
        return {"title": m["title"], "outlet": self.name, "date": m["date"],
                "url": url, "text": m["text"], "images": list(m["images"]),
                "videos": []}

    def feed_images(self, url):
        """Feed image for an article we DID manage to fetch (additive)."""
        m = self.meta.get(url)
        return list(m["images"]) if m else []


RSS_OUTLETS = [
    dict(name="nytimes", host="https://www.nytimes.com",
         feeds=["https://rss.nytimes.com/services/xml/rss/nyt/Climate.xml",
                "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
                "https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
                "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
                "https://rss.nytimes.com/services/xml/rss/nyt/AsiaPacific.xml"],
         expected_ceiling=40, feed_only=True,
         note="FEED-ONLY (articles 403): feed carries a real image+caption; text is a ~150-char summary"),

    dict(name="washingtonpost", host="https://www.washingtonpost.com",
         feeds=["https://feeds.washingtonpost.com/rss/national",
                "https://feeds.washingtonpost.com/rss/world",
                "https://feeds.washingtonpost.com/rss/local"],
         expected_ceiling=20, feed_only=True,
         note="FEED-ONLY and TEXT-ONLY (site times out, feed carries no media)"),
]

ADAPTERS.update({cfg["name"]: functools.partial(RSSAdapter, **cfg)
                 for cfg in RSS_OUTLETS})

#: Feed-only outlets carry no scraped body text, so they are excluded from the
#: image preset only when they also carry no image (Washington Post).
IMAGE_OUTLETS = [n for n in ADAPTERS
                 if n not in ("irishtimes", "premiumtimes", "washingtonpost")]

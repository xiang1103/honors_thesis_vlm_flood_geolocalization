"""Per-outlet scraping adapters.

Everything outlet-specific lives here: how to DISCOVER flood article URLs and
how to pull (image, caption) pairs out of that outlet's markup. The generic
fetch/extract machinery is in `scrape.py`.

Outlet choice is justified empirically in design.md. Short version: CBS is
primary because its /tag/flooding/ page paginates (~900-1400 articles) and uses
clean <figure>/<figcaption>; AP and NBC are secondary, lower-volume sources.
"""
import re
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


# ------------------------------------------------------------------- adapters

class Adapter:
    name = "base"
    host = ""
    #: xpath for images that are genuinely part of the story body
    body_xpath = "//article | //main"

    def discover_url(self, page):
        """URL of the Nth listing page (1-indexed)."""
        raise NotImplementedError

    def article_links(self, doc):
        """Article URLs from a listing page document."""
        raise NotImplementedError

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
    """PRIMARY. Editorially flood-tagged, paginated, clean <figure> markup."""
    name = "cbs"
    host = "https://www.cbsnews.com"
    # section.list-river is the true paginated feed. The larger
    # 'view-bulk-component' block is boilerplate repeated on every page.
    body_xpath = "//article | //div[contains(@class,'content__body')] | //main"

    def discover_url(self, page):
        return f"{self.host}/tag/flooding/" if page == 1 else f"{self.host}/tag/flooding/{page}/"

    def article_links(self, doc):
        hrefs = doc.xpath("//section[contains(@class,'list-river')]//a[contains(@href,'/news/')]/@href")
        return [absolutize(self.host, h.split("?")[0]) for h in hrefs]


class APAdapter(Adapter):
    """SECONDARY. No <figure>; captions live in img@alt. Body must be scoped
    to RichTextStoryBody or recirculation photos leak in (see design.md)."""
    name = "ap"
    host = "https://apnews.com"
    body_xpath = "//div[contains(@class,'RichTextStoryBody')] | //bsp-story-page"

    def discover_url(self, page):
        # The hub has no real pagination (JS 'load more'), so page>1 is empty.
        return f"{self.host}/hub/floods" if page == 1 else None

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


ADAPTERS = {a.name: a for a in (CBSAdapter, APAdapter, NBCAdapter)}


def get_adapter(name):
    if name not in ADAPTERS:
        raise SystemExit(f"unknown outlet {name!r}; choose from {sorted(ADAPTERS)}")
    return ADAPTERS[name]()

# Agent Scraping — Direct Outlet Scraper (24 outlets)

Design + rationale for the direct-from-outlet flood dataset scraper.
Complements `../scraping_api/` (GDELT-API-based discovery). Read this before
changing the pipeline so the measurements below aren't re-derived.

## Goal

A reproducible dataset of US-outlet flood journalism for a VLM use case:

```json
{
  "title": "str", "outlet": "cbs", "date": "ISO-8601", "url": "str",
  "text": "str",
  "images": [ { "url": "str", "caption": "str", "source": "figure|body" } ],
  "videos": [ { "url": "str", "caption": "str", "thumbnail": "str",
                "duration_s": 45, "source": "jsonld|video|og|iframe" } ]
}
```

Key order is fixed by `FIELD_ORDER` in `scrape.py` and applied on write, so
scoring (which appends fields) can't reorder the human-facing ones.

**Storage format:** JSONL while crawling, then combined into a single
pretty-printed `data/{outlet}_flood.json` and the JSONL is **deleted**
(`--keep-jsonl` overrides). JSONL is
append-per-article and therefore crash-safe and resumable; a JSON array needs
its closing bracket, so an interrupted crawl would leave a corrupt file.
`export.py` converts to a plain JSON array once the crawl is done.

The **(image, caption) pair is the product**. A bare image list is worth much
less — the caption is the supervision signal.

## Outlet selection — decided by measurement, not assumption

Step 1 of the task was "choose the outlet that's easiest to scrape." That was
settled with four probes (kept in the session scratchpad, reproducible via the
numbers below). Every candidate was tested with a plain `requests` GET and a
normal desktop User-Agent — no proxies, no headless browser.

### Bot blocking: not the differentiator it was assumed to be

All 8 candidates returned **HTTP 200 with real content**:

| Outlet | HTTP | bytes | trafilatura text | `<figure>` | `<figcaption>` |
|---|---|---|---|---|---|
| FOX | 200 | 437K | 2,113 | 0 | 0 |
| NPR | 200 | 137K | 2,240 | 0 | 0 |
| CBS | 200 | 620K | 2,295 | 3 | 3 |
| ABC | 200 | 181K | 5,705 | 2 | 0 |
| NBC | 200 | 298K | 4,087 | 1 | 1 |
| PBS | 200 | 258K | 15,099 | 4 | 0 |
| AP  | 200 | — | — | 0 | 0 |
| CNN | 200 | 4.3M | 1,048 | 0 | 0 |

> Note: `../scraping_api/design.md` records "a `requests` fetch of a CNN flood
> URL returns nothing." That did **not** reproduce here — CNN returned 4.3MB.
> The old finding may have been a UA-less request or a transient block. Bot
> walls were therefore *not* the deciding factor; **discovery volume** was.

### The real differentiator: flood-specific discovery volume

Finding *flood* articles is harder than fetching them. Measured:

| Outlet | Discovery mechanism | Flood URLs reachable |
|---|---|---|
| **CBS** | `/tag/flooding/{N}/` — **paginated** | **~900–1,400** (see below) |
| AP | `/hub/floods` | ~27, then JS "load more" — hard cap |
| NBC | `/news/weather` section | ~4–5 |
| FOX | `/category/us/disasters/floods` | 0 matched (different URL scheme) |
| CNN | search API `search.prod.di.api.cnn.io` | HTTP 400 — params rejected |
| PBS/ABC | search / topic pages | 404 |

AP's sitemap was investigated as a volume route and **rejected**: the sitemap
index has 230 sub-sitemaps that each contain exactly **1 archived URL** from
2006-era paths. Useless for recent floods.

### Caption yield head-to-head (6 flood articles each)

| Outlet | fetched OK | captioned images | avg/article |
|---|---|---|---|
| CBS | 6/6 | 10 | **1.7** |
| NBC | 4/4 | 6 | 1.5 |
| AP  | 6/6 | 8 | 1.3 |

Yields are close; **CBS wins on volume, precision, and markup cleanliness.**

## Decision: CBS News is the primary outlet

1. **Volume** — `/tag/flooding/{N}/` paginates to at least page 90 (404 by
   ~120). The `section.list-river` container yields 6–13 items/page with
   **zero cross-page overlap** (verified pages 1,2,3,4,10,90). ≈900–1,400 articles.
2. **Precision** — the tag page is **editorially** flood-tagged. No keyword
   matching, so no "flood of demand" false positives (the exact failure that
   forced `../scraping_api/` onto GDELT's `NATURAL_DISASTER_FLOOD` theme).
3. **Markup** — standard `<figure>`/`<figcaption>`. Captions are real prose:
   *"An aerial view of the extensive destruction and debris deposits along the
   Trishuli River."*
4. **Static HTML** — no JS execution, no Playwright, no API key.

AP and NBC are implemented as **secondary adapters** for source diversity;
they share the extraction core and differ only in discovery + image selectors.

### Critical AP-specific finding (why adapters are per-outlet)

AP has **no `<figure>` elements**; captions live in the `<img alt>` attribute,
and `<p class="embed-caption">` is empty in static HTML (JS-filled). Worse,
naive `//main//img` on AP returns **recirculation** images — an AP Grand Canyon
flood article yielded photos of a Trump indictment, TSA lines, and Michigan
whitefish. Genuine photos sit in `div.RichTextStoryBody`; promos sit under
`PagePromo` / `PageList-items` / `HamburgerNavigation`. The extractor filters
by ancestor class. This mirrors the recirculation-thumbnail problem already
documented in `../scraping_api/design.md`.

## Do we need Claude agents to verify "is this actually flooding"?

**No — and that is a deliberate, measured decision.** The task allowed
multi-agent verification "only if needed." It isn't:

- CBS's flooding tag is human-curated, so precision is high at the source.
- Verification therefore has little to catch, and an LLM call per article
  would dominate cost for a ~1,000-article crawl (the task explicitly asks to
  limit token usage and keep the work in reproducible code).

Instead `verification/verify_text.py` applies a **zero-token heuristic**: flood-term density
across title/text/captions, producing `flood_score` + `flood_verified` on every
record. Records are kept, not dropped, so the threshold stays tunable after the
fact without re-crawling. If a manual audit later shows the heuristic is
insufficient, an LLM pass can run **once, over the low-scoring tail only**.

## Video extraction — four strategies, because outlets disagree

Measured on live articles; no single strategy covers both outlets:

| Outlet | `<video>` | `<source>` | `og:video` | JSON-LD `VideoObject` |
|---|---|---|---|---|
| CBS | 2 | 1 | 2 | **0** |
| Fox | 0 | 0 | 0 | **1** (rich) |

So `extract_videos()` tries all four, in descending order of metadata quality:

1. **JSON-LD `VideoObject`** — the richest. Fox supplies `name`,
   `description`, `contentUrl`, `embedUrl`, `thumbnailUrl`, and an ISO-8601
   `duration` (`PT45S` -> `duration_s: 45`).
2. **`<video>` / `<source>`** — CBS ships a real media URL
   (`qu.cbsnews.com/...`) plus a `poster` used as the thumbnail.
3. **`og:video`** — CBS fallback, since its `<source>` is often JS-injected.
4. **`<iframe>`** — YouTube/Vimeo/Brightcove embeds only; ad and analytics
   iframes are excluded by host allowlist.

Videos run through the same recirculation filter as images, so a promo video
for a different story is not attributed to this article.

## Outlet ceilings — why only CBS has archive depth

Re-probed when the task widened to "every website". The result is that
**five of six outlets are structurally capped**, because their archives
paginate via JavaScript:

| Outlet | Working discovery route | Ceiling | Why capped |
|---|---|---|---|
| **CBS** | 6 tags x `/tag/{tag}/{N}/` | **~2,000+** | genuine HTML pagination |
| Fox | `api/article-search?searchBy=tags` | ~46 | `offset`/`size` **ignored**, 30/tag |
| AP | 4 x `/hub/{topic}` | ~80 | deeper paging is JS "load more" |
| NPR | `/sections/weather/` | ~22 | no flood tag page; keyword-filtered |
| NBC | `/news/weather` | ~5 | no static flood tag page |
| CNN | news sitemap (last ~48h) | ~15 | search API rejects all requests |

Specific dead ends, recorded so they aren't retried:

- **Fox** `searchBy=categories` returns `[]`; **`searchBy=tags` is required.**
  `offset` and `size` are both ignored — 30 items is a hard per-tag ceiling, so
  two tags are merged. Fox's static category page has **zero** article links.
- **CNN** `search.prod.di.api.cnn.io/content` answers every request with
  `{"error":"missing request id"}`. Tried `request-id`, `X-Request-ID`,
  `x-amzn-trace-id`, `x-correlation-id` — all rejected. No archive access.
- **AP** sitemap index has 230 sub-sitemaps holding **one 2006-era URL each**.
- **ABC / PBS** — no static flood topic endpoint found (all 404).

Consequence: the 5,000-per-outlet cap **never binds**. It is retained as a
safety valve, not because any outlet approaches it.

## Widening coverage: multiple indexes per outlet

Neither CBS nor AP paginates a *single* flood index deep enough on its own, so
both query several indexes and merge. CBS tags that exist (all others 404 —
`floods`, `flood`, `storms`, `extreme-weather`, `natural-disasters`, `rain`,
`monsoon`):

`flooding`, `flash-flooding`, `tropical-storm`, `hurricane`, `landslide`,
`severe-weather`

The last four are flood-*adjacent*, not flood-specific. They are included
deliberately and left for `verification/verify_text.py` to score rather than filtered at crawl
time, because hurricane/tropical-storm coverage is dense with flood imagery.
Filter on `flood_verified` downstream to get the strict subset.

**Ordering hazard (important).** Merging indexes breaks the assumption behind
`--old-streak`: each tag restarts at the present day, so a "N consecutive
out-of-window articles" counter would trip on the first tag's old tail and
discard every later tag. Adapters therefore declare `chronological`; the early
stop applies only when it is `True`. CBS and AP set it `False`.


## The 50-outlet bake-off (expansion beyond the original six)

The original six outlets were all US national news. That is a narrow base for a
flood dataset: the largest flood events of any year happen in South Asia, West
Africa and Southeast Asia, and US outlets cover them thinly and without local
photography. So **50 candidate outlets were probed** and 18 were added, taking
the registry from 6 to 24.

Every route below was verified against the live site. The probes are
reproducible: fetch the index, count links matching the article pattern across
pages 1/2/3/10, and check whether page 2 yields anything page 1 did not.

### Two methodology bugs worth recording

Both produced confident, wrong conclusions before being caught:

1. **Advertising Brotli without being able to decode it.** Adding
   `Accept-Encoding: gzip, deflate, br` to look more browser-like made servers
   return Brotli, which `requests` cannot decode unless the `brotli` package is
   installed (it is not, in this env). `lxml` then parsed binary noise and
   reported **zero links on pages that were fine** — Global News "dropped" from
   308KB/9 links to 45KB/0. **Never advertise `br` here.**
2. **Regexes anchored with `/$` after the URL had its trailing slash stripped.**
   Silently zeroed Grist and CTV. A discovery pattern that returns 0 is
   indistinguishable from a dead site, so a zero result must be confirmed by
   looking at the actual hrefs before the outlet is written off.

### Outlets added (18)

Grouped by the only thing that really varies: how deep the archive goes.

| outlet | route | new articles/page | note |
|---|---|---|---|
| **guardian** | `/environment/flooding?page=N` + `/world/natural-disasters?page=N` | ~15 | deepest non-US archive; verified to page 40 (reaches 2024) |
| **hindustantimes** | `/topic/flood/page/N` | ~30 | keyword filter required — topic page mixes in horoscopes |
| **floodlist** | 5 regions x `/page/N` | ~16 | a dedicated flood site; every article on-topic by construction |
| **rnz** | `/news/weather?page=N` | ~24 | no flood tag; broad section, keyword-filtered |
| **premiumtimes** | `/tag/flood/page/N` | ~25 | Nigeria; **text-only** |
| **irishtimes** | `/tags/flooding/N/` | ~50 | **text-only** |
| **grist** | `/extreme-weather/page/N/` | ~14 | pagination advertised to page 80 |
| **pbs** | `/tag/flooding/page/N` + `/tag/floods/page/N` | ~9 | |
| **cna** | `/topic/flood?page=N` | ~12 | richest captions measured — 7 on one story |
| **jakartapost** | `/tag/flood/page/N` | ~10 | Indonesia |
| independent | `/topic/flooding` | 71 (one page) | densest single page found; `?page=N` 404s |
| toi | `/topic/flood/news` | 68 (one page) | |
| abcau | `/news/topic/floods` | 25 (one page) | |
| globalnews / ctv | tag / climate section | 16 / 15 | Canada |
| aljazeera | `/tag/floods/` | 8 (one page) | strong photo essays, incl. `/gallery/` |
| straitstimes | `/tags/floods` | 11 (one page) | |
| insideclimate | `/tag/flooding/` | 13 (one page) | |

The bottom group's "load more" is JavaScript, so one static page is genuinely
all there is. They are kept for **source diversity**, not volume — the same
rationale as the existing NBC/CNN adapters.

### Outlets rejected, and why (so they are not retried)

| outlet | verdict |
|---|---|
| Reuters | HTTP 401 on every request |
| Bloomberg, The Hill, Axios, AccuWeather, Mongabay, NDTV, Firstpost, phys.org, France24, news.com.au, Sky News | HTTP 403 — bot wall, full browser headers did not help |
| Newsweek, Euronews | HTTP 406 |
| **NYT** | topic page *is* statically paginated and looked ideal — but every article fetch 403s. Discovery without extraction is useless |
| Dawn, SCMP, DW, Yale Climate, ABC News (US), BBC topic pages | no static flood index found (404 on every candidate path) |
| Mirror / Metro / WalesOnline / Express (Reach plc) | `/all-about/{tag}` 404s |
| The Hindu, Deccan Herald, Indian Express, Jakarta *Post* tag rivals, Arab News, Gulf News, SMH, Stuff, AllAfrica, UPI, ScienceDaily, Bangkok Post, The Star (MY) | HTTP 200 but zero flood links in static HTML (JS-rendered index) |

**BBC** deserves a note: its news sitemap index resolves and its topic pages
return 200, but the flood topic IDs are opaque hashes with no discoverable
mapping, and the sitemap covers only ~48 hours. Same structural problem as CNN,
already documented above.


## The paywalled US majors — NYT, WSJ, Washington Post

These were requested explicitly, so the negative results matter as much as the
positive ones. Each was probed **sequentially, with full browser headers and a
`Referer`** — not in a concurrent burst, so rate limiting is not the
explanation.

| outlet | index page | article fetch | verdict |
|---|---|---|---|
| **NYT** | 200 — `/topic/subject/floods` even paginates statically | **403** | discovery works, extraction doesn't |
| **WSJ** | **401** on every section tried | n/a | hard auth wall |
| **Washington Post** | **connection timeout**, repeatedly | n/a | edge drops datacenter clients |

Discovery without extraction is worthless, so direct scraping is out for all
three. What *does* work is the channel each publisher operates for
redistribution — their **public RSS feeds**:

| feed | status | carries |
|---|---|---|
| NYT (Climate/World/US/Science/AsiaPacific) | 200, ~50 items each | title, date, ~150-char summary, **`media:content` image + `media:description` caption** |
| Washington Post (national/world/local) | 200, 15–23 items | title, date, ~150-char summary. **No media at all** |
| WSJ (`RSSWorldNews`) | 200 but **stale** — items dated Jan 2025; `RSSUSnews` 403 | unusable |

So:

- **`nytimes` is added as a feed-only adapter** and is genuinely useful: the
  feed hands over a real image paired with a real caption
  (*"Flooding in Penn Station, Aug. 20."*), which is exactly the product. What
  it does **not** give is body text — `text` is the feed summary, ~150 chars
  rather than ~4,000.
- **`washingtonpost` is added as feed-only and text-only.** No images. It is
  excluded from the `--outlets images` preset.
- **WSJ is not added.** There is no working route: the site 401s and the feed
  has not updated since January 2025. Adding a stub that collects nothing
  would be worse than leaving it out.

Both feed outlets are **recent-only** (a feed is a window on the last few
days), so they cannot backfill a year. Run them repeatedly to accumulate.

### The structural change this forced

Every other adapter assumes `discover()` yields URLs and `scrape_article()`
then fetches them. For NYT the fetch can never succeed, so the **feed item is
the record**. `RSSAdapter` stashes the parsed item in `self.meta[url]` and
exposes `fallback_record(url)`; `scrape_article()` calls it when the fetch
returns `None`. `feed_images()` is the additive counterpart for outlets that
*do* fetch — a feed image the page itself didn't expose is merged in.

### A scoring bug this exposed

`verify.score_record()` required `strong_b >= 2` — two flood terms in the body
— to set `flood_verified`. That floor assumes a full article. On a 150-char
feed summary it systematically under-verified: *"Nepal's Flood Relief Workers
Feel the Pain of Trump's Cuts to U.S.A.I.D."* scored **0.63 and still failed**.

The requirement now scales with the body actually available: in a body under
60 words, a flood term in the headline plus one in the body is as much
evidence as the text can carry. Re-scoring all 1,202 existing records with the
new rule changes **zero** verdicts — it only affects bodies too short to have
ever met the old floor.

## US majors that *do* allow direct scraping

Found while probing the paywalled three, and added:

| outlet | index | note |
|---|---|---|
| **latimes** | `/environment`, `/california`, `/weather` | articles fetch cleanly with captioned images; no flood tag, so keyword-filtered |
| **usatoday** | `/news/weather/`, `/news/nation/` | weather section is flood-dense (11 of 22 links) |
| **newsweek** | `/weather` | `/topic/flooding` 406s but `/weather` serves fine |

**NBC was also fixed rather than added.** Its adapter discovered ~5 articles
from `/news/weather`. The section fronts `/weather` and `/hurricanes` yield
~50 between them, and NBC article URLs all carry an `rcna` id — a far more
reliable test than requiring the literal path `/news/`. Ceiling raised 10 → 50.

Rejected here: ABC News and TIME (0 article links in static HTML), Christian
Science Monitor (article links redirect to `/auth/sso_login`).


## Measured results of the full 29-outlet crawl

Run 2026-09-10, `--since-days 365` (FloodList separately with the window off).
These are **measurements, not ceilings** — the `expected_ceiling` values in
`TAG_OUTLETS` were corrected against them.

| | before (6 outlets) | after (29 outlets) |
|---|---|---|
| records | 949 | **5,889** |
| captioned images | 1,624 | **8,677** |
| records with >=1 image | — | 4,017 (68%) |
| `flood_verified` | — | 4,445 (75%) |

Top sources by captioned images: floodlist 4,227 · cbs 1,458 · rnz 1,020 ·
guardian 795 · premiumtimes 154 · hindustantimes 150 · independent 137 ·
abcau 114 · fox 111.

**Caption density** (images per article) is a better quality signal than raw
count, and it does not track outlet size: **abcau 4.6**, cna 3.6, grist 3.5,
guardian 3.1, rnz 3.0, aljazeera 2.9 — all well above CBS's 1.7. The small
international outlets punch far above their volume.

### A `--limit` cap that silently truncated the best source

FloodList discovered **3,477** articles but the run passed `--limit 3000`, so
477 were never fetched -- and nothing in the output said so. The crawl
reported success. Finding it needed a separate check: compare the
"(total N)" discovery line against the "N new articles to fetch" line in the
log, per outlet. Topping up recovered 476 records and 603 images.

Guardian shows the same shape in the log (1,882 discovered, 1,200 fetched) but
is genuinely exhausted: of those 1,200, **zero** fell inside the 1-year window,
so the remaining 429 are older still. `--limit` truncation and window
exhaustion look identical in the summary line; only the kept-vs-skipped split
tells them apart.

### Three outlets behaved differently than the probe predicted

- **FloodList stopped publishing in May 2024.** Its most recent article is
  2024-05-13, so `--since-days 365` discarded 1,199 of 1,200 articles and kept
  **one**. Re-run with the window disabled it yields **3,477 records / 4,227
  images / 89% verified** — the single largest source in the dataset. It is an
  *archive*, not a live feed, and must be crawled as one.
- **Irish Times yields literally nothing** (0 records). Its `<figure>`s carry
  no static `<img>`, *and* the flooding tag surfaces 2016-era articles, so a
  1-year window keeps none of them. Recorded as a dead end.
- **Premium Times is mostly-text, not text-only** as first labelled: 389
  records but only 32 carry an image. Kept for text and geographic coverage.

### A silent data-corruption bug the audit caught

Auditing the collected data (not the code) surfaced something the per-article
spot checks had missed: CNA was emitting
`mc_core_theme/images/inbox-large.png` — a site **icon** — carrying a real
flood caption, 16 times. A right caption on a wrong image is worse than no
image at all: it is silently corrupt supervision that no downstream filter
would catch.

Cause: the proximity-caption fallback (added for PBS/RNZ, which ship no
`<figcaption>`) attached the caption to whichever `<img>` was nearby. Two
fixes:

1. The fallback now only accepts a caption from a block containing **exactly
   one** `<img>`. If a block holds several, which one the caption belongs to
   is unknowable, so no caption is claimed.
2. CMS theme/chrome directories (`/theme/`, `/themes/`, `/chrome/`, `/ui/`)
   are treated as junk URLs.

Impact was confined to CNA (32 of 90 images; every other outlet was clean),
which was re-crawled. PBS, RNZ, TOI and Guardian keep their captions
unchanged, so the guard cost nothing where the fallback was doing real work.

**Audit the data, not just the extractor.** Every one of these three findings
came from scanning the output — dates, duplicate URLs, URL shapes — and none
of them would have shown up in a spot check of two articles per outlet.

## Extraction fixes forced by the new outlets

Adding outlets exposed three real bugs in the shared extractor. All three were
**false negatives** — images silently dropped — so they cost yield on the
original six outlets too, invisibly.

### 1. The recirculation walk climbed out of the article

`is_recirculation()` walked up to 12 ancestors looking for promo/furniture
class names. On PBS the walk reached
`div.page__body--with-sidebar` — a page *layout* wrapper whose class contains
the substring `sidebar` — and rejected **every photo on the site**. PBS scored
0 captioned images until this was found.

Fix: the walk now takes `stop_at=<body element>` and stops there. Everything
above the article body is layout, not recirculation.

### 2. A captioned `<figure>` is content, and must not be second-guessed

Hindustan Times lost its lead photo to an enclosing `taboola-readmore`
wrapper; Inside Climate News lost its featured image to `header.entry-header`.
Both are real article photos inside a real `<figure>` with a real
`<figcaption>`.

Fix: an `<img>` in a `<figure>` carrying a `<figcaption>` of at least
`MIN_CAPTION` chars is `trusted` — the class heuristic is skipped for it. Only
the promo-*link* test still applies, because a photo hyperlinked to a
*different article* is a thumbnail no matter how it is marked up. That test has
no false positives; the class heuristic has many.

### 3. The lead photo often sits outside the body container

Grist's hero `<figure>` is above `.entry-content`, so any body-scoped scan
misses the single most valuable image on the page.

Fix: `Adapter.images()` now runs **two passes** — captioned `<figure>`s
document-wide first, then body-scoped images — deduped by URL. And when
`body_xpath` matches nothing at all (Times of India has neither `<article>` nor
`<main>`), it falls back to `//body` rather than silently returning nothing.

### 4. Captions are not always in `<figcaption>`

PBS keeps its caption in `p.post__hero-caption`; RNZ prefixes captions with a
literal `"Caption: "`. `caption_for()` now falls back to a nearby element whose
class contains `caption`/`cutline`/`media-desc`, **excluding** anything
matching `byline` — without that exclusion PBS's `div.post__byline-caption`
wins and every caption becomes the reporter's name. Leading `Caption:` /
`Photo:` labels are stripped, and boilerplate that occupies a caption slot
(`RELATED:`, `Click to play video`, `Grist thanks its sponsors`) is rejected.

**Regression check:** re-extracting stored articles from the original outlets
with the patched code returns identical image counts (18 images across 13
CBS/CNN/Fox/NBC/NPR articles, before and after). AP is unchanged by
construction — it has no `<figure>` elements, so the trusted path never fires,
and its promos already sat outside the `RichTextStoryBody` scope.

## Why most new outlets are config, not code

Nineteen more hand-written adapter classes would have been nineteen copies of
the same loop. The six original classes stay as classes because each needs
genuinely custom discovery (Fox's JSON search API, CNN's sitemap, CBS's
per-tag merging). Everything added here fits one shape — *paginated listing
URLs + an article-URL pattern* — so it is declared as data in `TAG_OUTLETS`
and executed by a single `TagIndexAdapter`. Adding an outlet is now a dict.

Two details in that adapter are load-bearing:

- **Page 1 is fetched from its own URL**, never `/page/1/` — most sites 404 on
  the explicit form.
- **The empty-page counter keys on *new* links, not *found* links.** A site
  whose pagination is JavaScript serves page 1 again for every `?page=N`;
  keying on `found` would happily walk 200 identical pages. This is what caps
  the single-page outlets at one fetch instead of sixty.

## Date window

`--since-days 365` (default) keeps only articles inside the window. The date
comes from JSON-LD `datePublished`, falling back to `article:published_time`
then `<time datetime>`. Parsing is deliberately tolerant: outlets emit
`2026-08-30T07:04:00-0400` (no colon in the offset), which Python 3.9's
`datetime.fromisoformat` rejects, so a plain `YYYY-MM-DD` regex is the fallback.

Because listings are broadly reverse-chronological, `--old-streak` (default 40)
stops an outlet after that many consecutive out-of-window articles rather than
walking the entire archive.

## Architecture

```
tag page /tag/flooding/{N}/   ──►  article URLs   (discovery, per-outlet)
        │  section.list-river
        ▼
fetch article (requests + UA, retry/backoff)
        │
        ├─► title/date  ── JSON-LD, else <title>/<time>
        ├─► text        ── trafilatura
        └─► images      ── <figure>+<figcaption>, junk + recirc filters
        ▼
heuristic flood scoring  ──►  JSONL (append, resumable)
```

## Deliberate scope limits

- **Static HTML only.** No lazy-loaded (`data-src`-only), JS-rendered, or CSS
  `background-image` assets. Same call as `../scraping_api/`.
- **Bot blocks / 403 / 404 are logged and skipped**, never fatal (per task).
- **Images are referenced by URL by default**; `--download-images` optionally
  fetches bytes. Keeps the repo small and the crawl fast.

## Operational notes

- **Python: `/home/liu47/conda_envs/newEnv_local/bin/python3`**
  (`conda activate /home/liu47/conda_envs/newEnv_local`) — Python 3.10.20 with
  `requests` 2.33.1, `trafilatura` 2.2.0, `lxml` 6.1.3, `lxml_html_clean`.
  The bare shell `python3` may resolve to an env without `trafilatura`.
  `bs4`/`feedparser` are deliberately avoided so the dep set stays minimal.
- **Resumable**: output is JSONL, appended; `--resume` skips URLs already in
  the file. A long crawl can be interrupted safely.
- **Politeness**: `--delay` (default 1.0s) between article fetches.
- CBS pages occasionally return an empty `list-river` (page 50 did). This is
  tolerated — the crawler continues rather than treating it as the end.

## Validated results (20-page run, 157 articles)

| metric | value |
|---|---|
| articles scraped | 157 — **0 failures, 0 bot blocks** |
| with >=1 captioned image | 111 (71%) |
| total captioned images | 309 (avg 1.97/article, max 14) |
| `flood_verified` | 149 (95%) |
| title / date coverage | 157/157 (100%) |
| median text / caption | 3,767 / 151 chars |

The 8 unverified records are genuinely marginal — climate-loss totals, "hottest
year on record", and one true CBS mis-tag (*"Spanish friar killed in monastery
attack"*, 0.20). That the heuristic isolated the real false positive for zero
tokens is the evidence behind the no-LLM-agents decision above.

**Title-extraction gotcha (fixed):** JSON-LD must be filtered to Article-typed
nodes. Reading `name` off any node picks the `Organization` node and every
title becomes literally `"CBS News"`. `og:title` -> `<h1>` -> `<title>` is the
fallback chain; bare `<title>` is last because it's often just the site name.

## Status

- [x] Outlet bake-off (8 outlets, 4 probe rounds) — CBS selected
- [x] CBS discovery via paginated `list-river`, verified non-overlapping
- [x] Article extractor: title/date/text/images+captions
- [x] AP + NBC secondary adapters with recirculation filtering
- [x] Zero-token heuristic flood verification — validated at 95%
- [x] 20-page validation crawl (157 articles, 309 captioned images)
- [ ] Full crawl to exhaustion (`scrape.py --max-pages 120 --resume`)
- [ ] Optional: LLM audit of the low-`flood_score` tail only (~5% of records)
- [ ] Optional: image download + dedup by perceptual hash

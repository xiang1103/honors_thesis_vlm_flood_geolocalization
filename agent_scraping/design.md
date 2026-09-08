# Agent Scraping — Direct Outlet Scraper (CBS News)

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

Instead `verify.py` applies a **zero-token heuristic**: flood-term density
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
deliberately and left for `verify.py` to score rather than filtered at crawl
time, because hurricane/tropical-storm coverage is dense with flood imagery.
Filter on `flood_verified` downstream to get the strict subset.

**Ordering hazard (important).** Merging indexes breaks the assumption behind
`--old-streak`: each tag restarts at the present day, so a "N consecutive
out-of-window articles" counter would trip on the first tag's old tail and
discard every later tag. Adapters therefore declare `chronological`; the early
stop applies only when it is `True`. CBS and AP set it `False`.

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

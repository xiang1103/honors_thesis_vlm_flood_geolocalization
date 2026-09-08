# Agent Scraping — Direct Outlet Scraper (CBS News)

Design + rationale for the direct-from-outlet flood dataset scraper.
Complements `../scraping_api/` (GDELT-API-based discovery). Read this before
changing the pipeline so the measurements below aren't re-derived.

## Goal

A reproducible dataset of US-outlet flood journalism for a VLM use case:

```json
{
  "url": "str", "outlet": "cbs", "title": "str", "date": "ISO-8601",
  "text": "str",
  "images": [ { "url": "str", "caption": "str", "source": "figure|lead" } ]
}
```

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

- **Python: `/home/liu47/miniconda3/bin/python3`** — the shell default may
  resolve to an env without `trafilatura`. Deps: `requests`, `trafilatura`,
  `lxml` (all already installed; `bs4`/`feedparser` deliberately avoided).
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

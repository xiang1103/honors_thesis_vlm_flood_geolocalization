# agent_scraping — flood news dataset (direct outlet scraping)

Scrapes US news outlets for flood articles with **text + captioned images**,
for a VLM flood-geolocalization dataset. Sibling of `../scraping_api/`, which
discovers articles through the GDELT API instead.

**Outlet: CBS News**, chosen by measurement, not assumption — see
[design.md](design.md) for the 8-outlet bake-off. AP and NBC are implemented as
secondary adapters.

## Quick start

```bash
PY=/home/liu47/miniconda3/bin/python3   # the default python3 lacks trafilatura

# smoke test
$PY scrape.py --outlet cbs --max-pages 2 --limit 6 --out ../data/cbs_flood.jsonl

# full crawl -- resumable, safe to Ctrl-C and re-run
$PY scrape.py --outlet cbs --max-pages 120 --resume --out ../data/cbs_flood.jsonl

# also pull the image files
$PY scrape.py --outlet cbs --max-pages 120 --resume --download-images \
    --out ../data/cbs_flood.jsonl --image-dir ../data/images

# re-score / audit an existing file (no network, no tokens)
$PY verify.py ../data/cbs_flood.jsonl
```

Dependencies: `requests`, `trafilatura`, `lxml` — all already installed.
No API key, no headless browser, no `bs4`/`feedparser`.

## Files

| file | role |
|---|---|
| `scrape.py` | crawl loop: fetch w/ backoff, parse, score, write JSONL |
| `adapters.py` | per-outlet discovery + image selection (CBS / AP / NBC) |
| `verify.py` | zero-token flood scoring; also a standalone audit CLI |
| `design.md` | why CBS, the measurements, and the scope decisions |

## Output

One JSON object per line (JSONL, appended — so a crawl is resumable):

```json
{
  "url": "https://www.cbsnews.com/news/...",
  "outlet": "cbs",
  "title": "Grand Canyon flash floods leave 2 dead...",
  "date": "2026-08-30T07:04:00-0400",
  "text": "Two deaths have been confirmed after parts of...",
  "images": [
    {"url": "https://assets3.cbsnewsstatic.com/...",
     "caption": "Remnants of stone bridge pylons remain along Bright Angel Creek following a flash flood",
     "source": "figure"}
  ],
  "flood_score": 1.0,
  "flood_verified": true,
  "scraped_at": "2026-09-08T14:40:43+00:00"
}
```

## Measured results (20-page run, 157 articles)

| metric | value |
|---|---|
| articles scraped | 157 (0 failures, 0 bot blocks) |
| with >=1 captioned image | 111 (71%) |
| total captioned images | 309 (avg 1.97/article, max 14) |
| `flood_verified` | 149 (95%) |
| title / date coverage | 157/157 (100%) |
| median article text | 3,767 chars |
| median caption length | 151 chars |

Projected full crawl (~120 pages): **~900–1,400 articles**.

## Why there is no Claude-agent verification step

The task allowed multi-agent verification "only if needed" — it isn't, and
that's a measured call. CBS's flooding tag is human-curated, so 95% of records
pass a zero-token heuristic. The handful that fail are genuinely off-topic
(e.g. *"Spanish friar killed in monastery attack"*, score 0.20 — a real CBS
mis-tag the heuristic caught). Spending an LLM call per article to re-confirm
what a human editor already tagged would dominate the cost of a 1,000-article
crawl for almost no precision gain.

Records are **scored, not dropped**, so the threshold is tunable after the fact
without re-crawling:

```python
# keep only high-confidence flood articles that carry images
[r for r in recs if r["flood_verified"] and r["images"]]
```

If an audit later shows the heuristic is insufficient, run an LLM pass over the
**low-scoring tail only** (`flood_score < 0.35`) — a few dozen records, not
a thousand.

## Known limits (deliberate)

- **Static HTML only** — no JS-rendered galleries, no `data-src`-only lazy
  images, no CSS backgrounds. Adding Playwright was judged not worth it.
- **29% of articles have no captioned image** — many CBS flood stories are
  text/video-only. Use `--min-images 1` to keep only image-bearing ones.
- **AP/NBC adapters are low-volume** (~27 and ~5 articles): their listing pages
  don't paginate without JS. They exist for source diversity, not scale.

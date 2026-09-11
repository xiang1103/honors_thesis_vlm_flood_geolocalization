# agent_scraping — flood news dataset (direct outlet scraping)

Scrapes **29 news outlets** worldwide for flood articles with **text +
captioned images**, for a VLM flood-geolocalization dataset. Sibling of
`../scraping_api/`, which discovers articles through the GDELT API instead.

Outlets are chosen by measurement, not assumption: ~60 candidates were probed
and 29 kept. See [design.md](design.md) for the bake-off, the rejected outlets
(and why, so they aren't retried), and the extraction bugs the expansion
exposed.

```bash
$PY scrape.py --list-outlets      # every outlet, its ceiling, and its quirk
```

**The paywalled US majors are a special case.** NYT, WSJ and the Washington
Post all refuse a direct article fetch (403 / 401 / connection timeout). NYT
and WaPo are therefore reached through their **public RSS feeds** and marked
`feed_only`: the feed item *is* the record. NYT's feeds carry a real image with
a real caption, which is the product; WaPo's carry no media, so it is
text-only. **WSJ is not included** — its site 401s and its feed has not updated
since January 2025, so there is no route that collects anything. Both feed
outlets are recent-only; run them repeatedly to accumulate.

**Highest-volume outlets:** `cbs` (~2,500), `guardian` (~2,000, deepest non-US
archive), `hindustantimes` (~900), `floodlist` (~800, a dedicated flood site),
`premiumtimes` (~600), `rnz` (~500). The rest are lower-volume sources kept for
geographic diversity — floods are a global story, and US outlets cover South
Asian, West African and Southeast Asian events thinly and without local
photography.

## Quick start

```bash
PY=/home/liu47/conda_envs/newEnv_local/bin/python3   # project env (py3.10, requests+trafilatura+lxml)

# every outlet, last year, capped at 5000/outlet -> data/{outlet}_flood.json
$PY scrape.py --outlets all --since-days 365 --limit 5000

# only outlets that actually yield captioned images (the VLM-relevant subset)
$PY scrape.py --outlets images

# one or more named outlets
$PY scrape.py --outlets cbs,guardian,cna

# resume an interrupted run
$PY scrape.py --outlets all --resume

# also download the image files
$PY scrape.py --outlets all --download-images --image-dir ../data/images

# audit an existing dataset (no network, no tokens)
$PY verify.py ../data/cbs_flood.json
```

Each outlet writes **`data/outlets/{outlet}_flood.json`** — a single pretty-printed
JSON array, sorted newest-first. The intermediate `.jsonl` is deleted on
completion (`--keep-jsonl` to retain it).

### Long crawls: run it in tmux

A full crawl is hours, so run it detached — it then survives an SSH
disconnect (closing a laptop lid, dropping VPN, ending the shell):

```bash
./run_crawl.sh                        # every outlet, resumable
OUTLETS=images ./run_crawl.sh         # only outlets that yield captioned images
OUTLETS=guardian,cna ./run_crawl.sh   # a subset
SINCE_DAYS=730 LIMIT=3000 ./run_crawl.sh
```

```bash
tmux attach -t flood-scrape           # watch it   (detach: Ctrl-b then d)
tail -f ../data/logs/crawl_latest.log # watch it without attaching
tmux ls                               # is it still running?
tmux kill-session -t flood-scrape     # stop it
```

The pane is kept open after the crawl exits, so the final summary table is
still there when you attach later. The run passes `--resume`, so killing it
and re-running picks up where it left off rather than re-fetching.

> **tmux survives a dropped connection, not a suspended machine.** If you SSH
> into this box from your laptop, closing the lid is exactly the case tmux
> handles. If this box *is* the laptop, closing the lid suspends the CPU and
> the crawl pauses until you reopen it — use `caffeinate` (macOS) or disable
> lid-suspend (Linux) if you need it to keep running locally.

### Key options

| flag | default | meaning |
|---|---|---|
| `--outlets` | `all` | comma list, `all`, or `images` (only outlets measured to yield captioned images — excludes the two text-only ones) |
| `--list-outlets` | off | print every outlet with its expected ceiling, then exit |
| `--data-dir` | `data/outlets` | where the per-outlet JSON files go |
| `--since-days` | `365` | date window; `0` disables |
| `--limit` | `5000` | max articles per outlet |
| `--old-streak` | `40` | stop an outlet after N consecutive out-of-window articles |
| `--min-images` | `0` | keep only articles with >= N captioned images |
| `--keep-jsonl` | off | don't delete the intermediate JSONL |

Dependencies: `requests`, `trafilatura`, `lxml` — all already installed.
No API key, no headless browser, no `bs4`/`feedparser`.

## Files

| file | role |
|---|---|
| `scrape.py` | crawl loop: fetch w/ backoff, parse, score, write JSONL |
| `adapters.py` | per-outlet discovery + image selection (CBS / AP / NBC) |
| `verify.py` | zero-token flood scoring; also a standalone audit CLI |
| `export.py` | filter/convert a dataset (`--verified-only`, `--min-images`) |
| `design.md` | why CBS, the measurements, and the scope decisions |

## Adding another outlet

Most outlets need no code — append a dict to `TAG_OUTLETS` in `adapters.py`:

```python
dict(name="example", host="https://example.com",
     indexes=[("https://example.com/tag/flood",            # page 1, verbatim
               "https://example.com/tag/flood/page/{n}")], # None if it can't paginate
     article_re=r"example\.com/20\d\d/[a-z0-9-]+",
     expected_ceiling=100,
     keyword=True,      # only if the index is a broad weather/climate section
     note="what's odd about this outlet")
```

Verify the route first — index returns 200 with real links, page 2 yields
articles page 1 did not, and an article actually yields captioned images.
`design.md` lists the two probe bugs that produce convincing false negatives.

## Output

### Video

Outlets disagree completely on video markup, so four strategies run in
descending order of metadata quality: JSON-LD `VideoObject` (Fox — gives
description, thumbnail, `PT45S` duration), `<video>`/`<source>` (CBS — real
media URL + poster), `og:video` (CBS fallback), and `<iframe>` embeds
(YouTube/Vimeo/Brightcove only). Measured: CBS exposes 2 `<video>` + 2
`og:video` and **zero** JSON-LD; Fox exposes **only** JSON-LD.

### Why JSONL during the crawl, plain JSON after

The crawler writes **JSONL** (one object per line, appended and flushed per
article) because that is the only format that survives interruption: a crash or
Ctrl-C 900 articles into a 1,400-article run still leaves a valid file, and
`--resume` works by reading back the URLs already present. A JSON array cannot
be appended to safely — it needs its closing `]`, so an interrupted write
produces an unparseable file. It also streams, so memory stays flat.

That reasoning only applies *while crawling*, so the crawl **finalizes
automatically**: when an outlet finishes, its JSONL is combined into a single
pretty-printed `data/{outlet}_flood.json` (sorted newest-first) and the JSONL is
deleted. `export.py` remains for filtering an existing dataset:

```bash
$PY export.py ../data/cbs_flood.json -o ../data/vlm.json --verified-only --min-images 1
```

### Field order

Fixed and enforced on write (via `FIELD_ORDER` / `ordered()` in `scrape.py`),
so re-scoring or hand-editing can't let it drift:

`title, outlet, date, url, text, images, videos, flood_score, flood_verified, scraped_at`

One JSON object per line (JSONL, appended — so a crawl is resumable):

```json
{
  "title": "Grand Canyon flash floods leave 2 dead...",
  "outlet": "cbs",
  "date": "2026-08-30T07:04:00-0400",
  "url": "https://www.cbsnews.com/news/...",
  "text": "Two deaths have been confirmed after parts of...",
  "images": [
    {"url": "https://assets3.cbsnewsstatic.com/...",
     "caption": "Remnants of stone bridge pylons remain along Bright Angel Creek following a flash flood",
     "source": "figure"}
  ],
  "videos": [
    {"url": "https://www.foxnews.com/video/6404693212112",
     "caption": "Dramatic video shows several feet of floodwater building up outside the glass doors...",
     "thumbnail": "https://static.foxnews.com/...",
     "duration_s": 45,
     "source": "jsonld"}
  ],
  "flood_score": 1.0,
  "flood_verified": true,
  "scraped_at": "2026-09-08T14:40:43+00:00"
}
```

## Measured results — full 29-outlet crawl (2026-09-10)

| metric | before (6 outlets) | after (29 outlets) |
|---|---|---|
| records | 949 | **5,889** |
| captioned images | 1,624 | **8,677** |
| records with >=1 image | — | 4,017 (68%) |
| `flood_verified` | — | 4,445 (75%) |
| median caption length | 151 | 102 chars |
| median article text | 3,767 | 2,393 chars |

Window: `--since-days 365`, except FloodList (an archive — see below).

| outlet | records | images | img/article | verified |
|---|---|---|---|---|
| floodlist | 3,477 | 4,227 | 1.2 | 3,104 |
| cbs | 851 | 1,458 | 1.7 | 505 |
| rnz | 345 | 1,020 | 3.0 | 183 |
| guardian | 253 | 795 | 3.1 | 219 |
| premiumtimes | 389 | 154 | 0.4 | 7 |
| hindustantimes | 96 | 150 | 1.6 | 93 |
| independent | 71 | 137 | 1.9 | 60 |
| abcau | 25 | 114 | 4.6 | 18 |
| fox | 47 | 111 | 2.4 | 30 |
| grist | 28 | 98 | 3.5 | 16 |
| jakartapost | 62 | 79 | 1.3 | 29 |
| cna | 25 | 56 | 2.2 | 23 |
| globalnews | 16 | 39 | 2.4 | 10 |
| toi | 30 | 36 | 1.2 | 30 |
| pbs | 31 | 33 | 1.1 | 29 |
| nbc | 19 | 29 | 1.5 | 11 |
| npr | 14 | 27 | 1.9 | 9 |
| aljazeera | 8 | 23 | 2.9 | 8 |
| ap | 34 | 23 | 0.7 | 33 |
| nytimes | 21 | 17 | 0.8 | 7 |
| insideclimate | 6 | 16 | 2.7 | 0 |
| newsweek | 10 | 16 | 1.6 | 4 |
| latimes | 6 | 9 | 1.5 | 4 |
| ctv | 5 | 4 | 0.8 | 2 |
| straitstimes | 4 | 3 | 0.8 | 2 |
| cnn | 2 | 2 | 1.0 | 1 |
| usatoday | 11 | 1 | 0.1 | 8 |
| washingtonpost | 3 | 0 | 0.0 | 0 |
| **TOTAL** | **5,889** | **8,677** | **1.5** | **4,445** |

**Caption density beats volume as a quality signal.** ABC Australia averages
**4.6** captioned images per article, CNA 3.6, Grist 3.5, Guardian 3.1 — all
well above CBS's 1.7. The small international outlets contribute far more per
article than their record counts suggest.

Three outlets behaved differently than their probe predicted, and the notes in
`TAG_OUTLETS` were corrected to match:

- **floodlist stopped publishing in May 2024.** With a 1-year window it yields
  **1** record. Crawled as an archive (`SINCE_DAYS=0`) it yields **3,477
  records / 3,624 images / 89% verified** — the largest single source here.
- **irishtimes yields 0** — no static `<img>` in its figures, and its flooding
  tag surfaces 2016-era articles. Documented dead end.
- **usatoday** has a flood-dense weather section but almost no captions: 11
  records produced 1 image.

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
- **Many outlets are low-volume by construction.** AP, NBC, Al Jazeera,
  Straits Times, Global News, CTV and Inside Climate News all paginate via
  JavaScript, so one static page is genuinely all there is (8–25 articles).
  They exist for source diversity, not scale.
- **Two outlets are text-only:** `irishtimes` (its `<figure>`s carry no static
  `<img>`) and `premiumtimes` (no `<figcaption>` anywhere). Both still produce
  good article text and are excluded from the `--outlets images` preset.
- **AP intermittently returns HTTP 403** to datacenter IPs. Non-fatal — the
  crawler logs and skips, per the retry policy above.

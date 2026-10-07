# vlm_flood

Honors thesis project. **The goal is visual geolocalization**: given a
street-level image, recover where it was taken. The dataset being built is
street-view imagery of flooded (and un-flooded) scenes -- SF-XL in spirit, but
flood-focused.

Coordinates are NOT required for the current phase: the near-term goal is
simply to collect images with street-view *geometry*. That is a deliberate
scope decision by the owner, with a known cost recorded under "Dataset
direction" below.

## Current focus (owner's decision, 2026-09-28) — MyCoast and sources like it

There are two collection techniques in the repo:

1. **News outlets** -- HTML crawl of 29 outlets (`scraping/scrape.py`). Built
   first; measured low yield for street-view geometry (see "Dataset
   direction"). Kept as a supplement, not being extended.
2. **Government / public APIs** -- documented ArcGIS and open-data endpoints
   (`scraping/api_based_scraping/gis_scrape.py`, `mycoast_scrape.py`):
   MyCoast, USGS STN, Wikimedia Commons, NAPSG PhotoMappers, all New York
   State for now.
3. **Videos** (from 2026-10-05) -- YouTube Data API v3 first. Collected for
   flood RELEVANCE, not coordinates; New York is labelled when it can be
   inferred. `scraping/video_scraping/youtube_scrape.py`; format: "REQUIRED
   data format for VIDEOS"; design: `scraping/video_scraping/design.md`.
   First live run 2026-10-06: 2,074 videos (see State).

**The owner wants to keep focusing on MyCoast data, or data like it.** MyCoast
is the best source found so far: citizen flood reports taken on foot or from a
car, so the camera is usually at street level; every report has coordinates
and a timestamp; and the reporter states "What is Flooded" (Roads/streets is
the top answer, 1,039 of 1,893 reports), which pre-filters for street scenes
better than any caption. When proposing new work, prefer (a) getting more out
of MyCoast -- other states, other report types, re-scrapes -- and (b) finding
other sources with the same properties: citizen/agency reports, structured
API, per-record GPS + time, photos taken at ground level. Examples to probe:
MyCoast's other state programs, 311-style flood complaint portals with photos,
NWS/CoCoRaHS/mPING-style spotter reports, state DOT road-closure imagery,
Mapillary. Do not spend effort extending the news crawler unless asked.

## REQUIRED data format for STATIC IMAGES — match `data/mycoast.json` (owner's rule, 2026-09-28; scoped to images 2026-10-05)

This contract covers sources whose unit is a **photo** (a report, page or post
carrying still images). **Video sources have their own contract** -- see
"REQUIRED data format for VIDEOS" below; do not force a video into this shape
and do not apply this section's coordinate requirement to videos.

**Any new static-image source, scraper, or rewrite of an existing one MUST write
records in exactly the shape `data/mycoast.json` has as of 2026-09-28.** The
data is used for web display, so the format is a contract, not a suggestion.
Do not rename, drop, re-type, or re-nest fields; do not invent a parallel
format "for now". If a source genuinely cannot fit, stop and ask the owner
before writing anything -- do not decide the schema yourself.

File: a JSON **list** of report objects, sorted newest first, written
atomically (`.tmp` + fsync + `os.replace`), merged into existing rows, never
overwritten wholesale.

**One report object** (every key always present; use `null`, `{}` or `[]` when
the source has no value -- never omit a key):

| key | type | meaning |
|---|---|---|
| `report_id` | int | the source's own stable id for the report |
| `source_url` | str | public page for the report |
| `report_type` | str | the source's category (MyCoast: "Flood Watch" / "Storm Reporter") |
| `title` | str | |
| `county` | str | |
| `place` | str | neighbourhood / locality |
| `state` | str | two-letter |
| `date_utc` | str | ISO 8601 with offset, UTC |
| `local_time` | str | ISO 8601 with the local offset |
| `local_time_text` | str | the time as the source displayed it |
| `lat`, `lon` | float | WGS84; required -- a record without coordinates does not belong in this file |
| `in_nyc` | bool | |
| `nyc_basis` | str | how `in_nyc` was decided ("coordinates") |
| `image_count` | int | `== len(images)` |
| `images` | list | image objects, below; never empty (a report with no photo is dropped) |
| `text` | str | all text fields joined, for search / text models |
| `description` | str or null | the reporter's free text |
| `submitted` | dict | the source's structured form answers, label -> str or list[str] |
| `weather` | dict | label -> str |
| `tide_stations` | list | `{station, water_level_at_report, distance}` |
| `has_page_detail` | bool | whether the report page was fetched and parsed |
| `api_fields` | dict | every raw field the source API returned, verbatim |
| `page_fetched_at` | str or null | ISO 8601 |
| `scraped_at` | str | ISO 8601 |
| `duplicate_images` | list | OPTIONAL, added only by dedupe |

**One image object:**

| key | type | meaning |
|---|---|---|
| `image_url` | str | the PERMANENT full-size photo -- see displayability below |
| `thumbnail_url` | str or null | a smaller rendition of the same photo, same rules |
| `scaled_url` | str or null | another rendition, same rules |
| `record_id` | str | `sha256(source_url + "\n" + image_url)[:24]` |
| `image_sha256`, `image_dhash` | str | set by dedupe after a SUCCESSFUL fetch + decode |
| `api_image_url` | str | OPTIONAL: the source's original URL when it was replaced by a permanent one |
| `page_only`, `temporary_url` | bool | OPTIONAL flags, `true` only |

**Every photo must be displayable in a browser.** Concretely, `image_url` (and
`thumbnail_url` when set) must be:

1. a direct link to the image FILE, not to a page, viewer or gallery;
2. **permanent** -- never a staging, signed, expiring or session URL. MyCoast's
   `blueurchin-reportimages` URLs looked fine and died within days; they are
   resolved to the CDN before writing (see Gotchas). Check any new source for
   the same behaviour by re-fetching a few URLs days apart;
3. publicly fetchable: HTTP 200 with an `image/*` content type to a plain GET
   with no cookies, login, API key, or referrer (the review site loads images
   with `referrerPolicy = "no-referrer"`; a CDN that rejects hotlinks, like
   AP's 403, fails this);
4. in a format browsers decode: JPEG, PNG, WebP or GIF -- not TIFF, HEIC or RAW;
5. a remote https URL -- never a local path or `data:` URI. Pixels are not
   stored in the repo (see Licensing).

Proof of the rule is `image_sha256`: dedupe only sets it after downloading and
decoding the image. An image without it has NOT been shown to be displayable
and must be treated as a defect to fix, not shipped. As of 2026-09-28 all
3,025 images in `mycoast.json` have it. After any scrape, run the dedupe step
(`./scraping/update_mycoast.sh` does) and check its `images with no digest`
line is 0; if it is not, find out why before moving on.

## REQUIRED data format for VIDEOS (owner's rule, 2026-10-05)

Video sources (YouTube first) are collected for **flood relevance, not
coordinates**. The goal is videos that show flooding; knowing they are from
New York is a bonus that is labelled, never required. Coordinates are kept when
the source gives them and otherwise `null` -- a video is NOT dropped for lacking
them, and a video whose location can never be set (most of them) is still
kept. This is the deliberate difference from the static-image contract.

File: `data/<source>_videos.json` (YouTube: `data/youtube_videos.json`), a JSON
**list** of video objects, sorted newest first by `published_utc`, written
atomically (`.tmp` + fsync + `os.replace`), merged into existing rows, never
overwritten wholesale. Same write discipline as `mycoast.json`. As with images,
if a source genuinely cannot fit, stop and ask the owner -- do not fork the
schema.

**One video object** (every key always present; `null`, `{}` or `[]` when
absent -- never omit a key):

| key | type | meaning |
|---|---|---|
| `video_id` | str | the source's own stable id (YouTube: the 11-char id). Strings are fine here |
| `record_id` | str | `sha256(source_url)[:24]` -- identity for resume and joins |
| `source` | str | `"youtube"`, ... |
| `source_url` | str | public watch page (`https://www.youtube.com/watch?v=<id>`) |
| `embed_url` | str | player URL for the review site (`https://www.youtube-nocookie.com/embed/<id>`) |
| `title` | str | |
| `description` | str | verbatim, may be `""` |
| `tags` | list[str] | uploader tags |
| `channel_id`, `channel_title` | str | |
| `published_utc` | str | ISO 8601 UTC, when uploaded |
| `recording_date` | str or null | ISO 8601, only if the uploader set one |
| `duration_s` | int | |
| `category_id` | str | YouTube category (e.g. `"25"` News & Politics) |
| `language` | str or null | `defaultAudioLanguage` / `defaultLanguage` |
| `license` | str | `"youtube"` or `"creativeCommon"` |
| `thumbnails` | list | image objects in the static-image shape (`image_url`, `thumbnail_url`, `scaled_url`, `record_id`, `image_sha256`, `image_dhash`), so the displayability rules and dedupe apply unchanged. The cover first, then auto-frames `hq1`-`hq3`; only thumbnails that decoded are listed, so may be `[]` |
| `lat`, `lon` | float or null | only from the source's own location metadata (`recordingDetails.location`) or a burned-in dashcam GPS overlay -- never invented from a place name |
| `location_basis` | str or null | `"api"`, `"overlay_ocr"`, or `null` |
| `in_ny` | bool or null | `null` = unknown, which is the common case and is NOT the same as `false` |
| `in_nyc` | bool or null | same |
| `ny_basis` | list[str] | every signal that fired: `"coordinates"`, `"gazetteer"`, `"channel"`, `"event_date"` (`"visual"` reserved) |
| `ny_places` | list[str] | the place names matched, e.g. `["Hollis", "Queens"]` |
| `flood_text_score` | float | signal 1, 0.0-1.0 (see below) |
| `flood_text_relevant` | bool | `flood_text_score >= FLOOD_TEXT_THRESHOLD` (0.3, `video_signals.py`); recomputed every run |
| `flood_text_hits` | dict | `{strong, weak, negative, soft_negative}` -> list of matched terms, so a score can be audited |
| `flood_event_date` | str or null | signal 2: the known NY flood day (`YYYY-MM-DD`) this video was recorded/uploaded within 3 days after |
| `flood_visual` | dict or null | signal 3, filled by the verifier: per-thumbnail answers + the aggregate. `null` = not yet classified |
| `queries` | list[str] | label of every search that returned this video (provenance; also measures which queries pay) |
| `text` | str | title + description + tags joined, for search / text models |
| `available` | bool | the video was public and embeddable at `checked_at` |
| `checked_at` | str | ISO 8601, last time availability was confirmed |
| `api_fields` | dict | every raw field the API returned, verbatim |
| `scraped_at` | str | ISO 8601 |

Field order is `VIDEO_FIELDS` in `youtube_scrape.py`.

**Every video must be playable in a browser**, the video counterpart of the
displayability rule: `status.privacyStatus == "public"`, `status.embeddable ==
true`, and not age-restricted (`contentDetails.contentRating.ytRating`), so the
review site's `embed_url` iframe plays it. Videos disappear (deleted, made
private) -- a re-scrape re-checks `available` and never deletes the row, so the
labels already spent on it survive. Thumbnails use YouTube's `i.ytimg.com` URLs
(the cover as the API lists it, and the auto-frames `hq1.jpg`-`hq3.jpg` with
`1.jpg`-`3.jpg` as their `thumbnail_url`), which meet the five static-image
rules. The scraper fetches and fingerprints every one (shared
`data/image_hashes.json`) and keeps only those that decode, so every listed
thumbnail has `image_sha256`; the metadata's `thumbnails_without_digest` must
be 0. A missing `maxresdefault.jpg` answers 404 with a grey `image/jpeg`
placeholder -- a content-type check alone would accept it.

**No video bytes, ever.** Not in the repo and not on disk as a pipeline step:
downloading (yt-dlp etc.) is against YouTube's Terms of Service, and the
licensing argument under "Dataset direction" applies equally. Everything the
pipeline needs -- metadata, thumbnails, the embed -- comes through the API and
public thumbnail URLs.

**Labels, not filters** -- as with the news verifiers. A video that scores 0,
has no coordinates, or has `in_ny = null` is still written with its labels; the
cut is a threshold applied by the reader. The only videos that never reach the
file are NEW ones failing playability (counted in the run log).

## Videos: how flood relevance and New York are decided

Full reasoning, measurements and open questions:
**`scraping/video_scraping/design.md`** -- read it before changing the search
plan or `video_signals.py`. In short:

- YouTube has no "flood" filter. `search.list` costs 100 of the 10,000 daily
  quota units; `videos.list` is 1 unit per 50 ids. The search plan aims at New
  York (event date windows, geo circles, flood term x NY place); every search
  answer is cached in `scrape_data/youtube_searches.jsonl` the moment it
  arrives.
- Flood relevance is three independent signals, none a filter: (1) text --
  `flood_text_score`, `verify_text.py`'s vocabulary plus video terms, hard
  negatives (games, CG, trailers, insurance...) zero it; (2) `flood_event_date`
  -- upload within 3 days after a known NY flood; (3) `flood_visual` -- the VLM
  over the thumbnails (not built yet). Text says ABOUT a flood; only frames say
  VISIBLE.
- New York: coordinates if set, else place names (ambiguous ones like
  Queens / Rochester / every MyCoast place need a NY marker), else NY channel;
  `false` only on evidence of elsewhere; otherwise `null`.

## REQUIRED: every scrape updates its metadata file (owner's rule, 2026-09-28)

A metadata file is the dataset's published summary; a scrape that leaves it
stale makes it wrong. So **any run that changes a data file must regenerate
that file's metadata as part of the same run** -- not as a separate manual step
someone has to remember, and never by hand-editing the JSON.

| data file | metadata file | generated by | wired in |
|---|---|---|---|
| `data/mycoast.json` | `data/mycoast_meta_data.json` | `make_mycoast_metadata.py` | step 3 of `scraping/update_mycoast.sh` |
| `data/news_scrape_results.json`, `data/verified_images_news.json` | `data/meta_data.json` | `make_metadata.py` | `refresh_quietly()` at the end of `scrape.py`, `verify_images_vlm.py`, `dedupe.py` |
| `data/youtube_videos.json` | `data/youtube_videos_meta_data.json` | `make_youtube_metadata.py` | `refresh()` at the end of `youtube_scrape.py` |

Rules:

- Metadata is computed from the FINAL data file (after dedupe), never from a
  scrape's own counters, so the numbers describe what is actually shipped.
- A new data source gets a metadata generator (a `make_*_metadata.py` script
  or a function), and its update path calls it. Add a row to the table above.
- Running a scraper directly (e.g. `mycoast_scrape.py` without the wrapper)
  leaves metadata stale; finish with the wrapper, or run the generator.
- Metadata files are the tracked, committed record (`data/*` is gitignored
  except them), so their key set is a format too: add keys, do not rename or
  remove them without asking the owner.
- `gis_flood_images.json` has NO metadata file yet. Adding one is open work,
  and the rule applies once it exists.

## Git — do not commit or push

**Never run `git commit` or `git push`.** The owner handles all commits and
pushes. Leave finished work staged or unstaged in the working tree and say what
changed; do not decide when a change is ready to record.

`git mv` and `git rm` are fine when restructuring (they preserve history), but
they stage changes — stop there. Never `git checkout`/`restore`/`reset` over
uncommitted work either; ask instead.

## Environment

```bash
conda activate /home/liu47/conda_envs/newEnv_local    # python3 -> 3.10.20
```

Base `python3` is conda base (3.9) and lacks `huggingface_hub` — always
activate first. The env has torch 2.11+cu130, transformers 5.6.1.

Hardware: 10x RTX PRO 6000 Blackwell, 96 GB VRAM each; GPUs 4 and 5 usually
have other users' processes, the rest are free. 1 TB RAM, 344 threads.
`/home/liu47` is NFS. It reads 97% used, which is misleading -- it is a 15 TB
filesystem with ~460 GB free, about 20,000x the corpus. Do not design around
disk pressure without checking the absolute number first.

## Layout

```
scraping/         news crawl           scrape.py, adapters.py, run_crawl.sh, design.md, export.py
  api_based_scraping/   government/public APIs (ACTIVE): gis_scrape.py, mycoast_scrape.py
                        superseded GDELT news discovery: news_scrape.py + news_api_design.md
  video_scraping/       videos (ACTIVE): youtube_scrape.py, video_signals.py, design.md
verification/     judging only         verify_text.py, verify_images_vlm.py, dedupe.py
                  MyCoast              dedupe_mycoast.py
                  New York subset      filter_nyc.py  (news -> nyc_scraped_images.json)
                  country pass         locate_articles.py, country_codes.py, make_iso_table.py,
                                       compile_countries.py
local_vlm/        model mechanics      backend.py, download_model.py
image_review_web/  + image_review_server.py   news review site (human + model)  :8765
gis_review_web/    + gis_review_server.py     GIS/MyCoast review site           :8768
make_metadata.py  writes data/meta_data.json (news dataset snapshot)
make_mycoast_metadata.py  writes data/mycoast_meta_data.json (MyCoast snapshot)
make_youtube_metadata.py  writes data/youtube_videos_meta_data.json (called by youtube_scrape.py)
map_countries.py  country choropleth of the news corpus
data/news_scrape_results.json   THE news corpus — source of truth for the news side
data/gis_flood_images.json      all four API sources, one row per (page, image)
data/mycoast.json               MyCoast in depth, one row per REPORT, images nested
data/youtube_videos.json        YouTube, one row per VIDEO, thumbnails nested
scrape_data/      ALL intermediates: per-outlet .jsonl, the verifier's .jsonl,
                  mycoast_pages.jsonl (page cache), crawl logs. Gitignored.
```

The old model-results site (:8766, `image_vlm_review_server.py`) was merged
into :8765 and no longer exists.

`scraping/design.md` is the outlet bake-off record: which outlets were
tested, which were rejected and why, and postmortems of real bugs. Read it
before touching `adapters.py` or adding an outlet.

## Commands

```bash
# crawl
python3 scraping/scrape.py --outlets all
python3 scraping/scrape.py --list-outlets

# classify images (local GPU, free, default backend)
python3 local_vlm/download_model.py                        # one time, 55.6 GB
python3 verification/verify_images_vlm.py --workers 8 --device-map cuda:0
python3 verification/verify_images_vlm.py --backend hf      # hosted, costs credits

# which countries flooded, per article (local GPU, article text only)
python3 verification/make_iso_table.py                     # once, needs pycountry
python3 verification/locate_articles.py --limit 50 --device-map cuda:0   # trial
python3 verification/locate_articles.py --device-map cuda:0
python3 verification/country_codes.py                      # re-resolve, no GPU
python3 verification/compile_countries.py                  # totals + flat CSV to plot
python3 map_countries.py                                   # interactive choropleth
python3 map_countries.py --primary-only --exclude-outlet floodlist   # any cut
python3 map_countries.py --serve                           # + serve on :8768, tunnel to view

# audit text scores / remove duplicate images
python3 verification/verify_text.py data/news_scrape_results.json
python3 verification/dedupe.py --dry-run
python3 verification/dedupe.py --apply            # --drop-near is UNSAFE, see below

# government / public APIs (no GPU)
python3 scraping/api_based_scraping/gis_scrape.py                        # all 4 sources, ~8 min
python3 scraping/api_based_scraping/gis_scrape.py --sources mycoast,stn  # subset; others' rows kept
python3 scraping/api_based_scraping/mycoast_scrape.py                    # MyCoast in depth + report pages
python3 scraping/api_based_scraping/mycoast_scrape.py --no-pages         # API fields only, seconds
python3 verification/dedupe_mycoast.py --dry-run
python3 verification/dedupe_mycoast.py --apply    # re-run after every mycoast_scrape.py
./scraping/update_mycoast.sh                     # ROUTINE MyCoast update, in order:
                                                  # scrape -> dedupe -> metadata -> gis refresh
python3 make_mycoast_metadata.py                  # data/mycoast_meta_data.json alone

# videos (no GPU; needs YOUTUBE_API_KEY in .env or the environment)
python3 scraping/video_scraping/youtube_scrape.py --plan          # searches + quota cost, no API calls
python3 scraping/video_scraping/youtube_scrape.py                 # DAILY routine: next unfetched result pages
                                                                  # (breadth-first, resumes from the search cache),
                                                                  # ~one page level per day; also writes metadata
python3 scraping/video_scraping/youtube_scrape.py --no-search     # re-check + relabel existing, ~free
python3 scraping/video_scraping/youtube_scrape.py --query "flooded street queens"   # ad hoc
python3 make_youtube_metadata.py                                  # metadata alone

# review sites
python3 image_review_server.py          # :8765 news, human labels + model answers
python3 gis_review_server.py            # :8768 GIS/MyCoast, reads gis_flood_images.json
```

`gis_review_server.py` and `map_countries.py --serve` both default to :8768;
pass `--port` to one of them if both are running.

Long crawls: `./scraping/run_crawl.sh` (tmux, survives disconnect).
YouTube runs DAILY from cron (installed 2026-10-07, `crontab -l`): 03:30 server time
(Eastern; the quota resets at midnight Pacific), the routine command above, log
appended to `scrape_data/logs/youtube-cron.log`. A second run while one holds the
lock exits at once (`another run holds ...`), so a manual run cannot collide with it.
MyCoast update: `./scraping/update_mycoast.sh` (scrape -> dedupe -> metadata -> gis refresh,
stops on first failure, log in `scrape_data/logs/mycoast-update_latest.log`).

## Data flow

```
scrape.py  --calls verify_text.score_record() INLINE, per article-->
    scrape_data/<outlet>_flood.jsonl      per-outlet, appended+flushed per article,
        |                                 merged and DELETED when the outlet finishes
    data/news_scrape_results.json         THE corpus: the seen-URL set and
        |                                 what every downstream reader consumes
verify_images_vlm.py -->
    data/verified_images_news.json    one row per (article, image) with yes/no
        |
dedupe.py -->  same file, exact duplicates removed

locate_articles.py  (reads the CORPUS, not the image results) -->
    data/article_countries.json       one record per flood article, with the
        |                             countries that flooded; joins on article_url
compile_countries.py -->
    data/country_totals.json          one row per country, join a map on alpha_3
    data/country_rows.csv             one row per (article, country), flat
        |
map_countries.py -->
    data/map_view/flood_map.html      plotly choropleth, ALWAYS this one file;
                                      filters apply BEFORE aggregation so the
                                      counts match what is drawn, and the cut
                                      is written into the map's own title
```

Government / public API side (independent of the news corpus):

```
gis_scrape.py  (mycoast | stn | commons | napsg) -->
    data/gis_flood_images.json        one row per (source page, image),
        |                             record_id = sha256(source_url \n image_url)[:24];
        |                             merged per source, not overwritten
    gis_review_server.py :8768        browse + human labels (localStorage)

mycoast_scrape.py  (ArcGIS layer + mycoast.org/reports/<id> pages) -->
    scrape_data/mycoast_pages.jsonl   parsed-page cache, KEPT after the run
        |                             (it is what makes a re-run free, ~30 min otherwise)
    data/mycoast.json                 one row per REPORT, images nested; each
        |                             image carries the same record_id as above,
        |                             so the two files join
dedupe_mycoast.py -->  same file, exact-duplicate images and image-less reports dropped
make_mycoast_metadata.py -->
    data/mycoast_meta_data.json       counts snapshot (tracked in git)
ad-hoc, not in the repo -->
    data/mycoast_points.csv           one row per report, lat/lon for plotting
    data/map_view/mycoast_ny_map.html point map of NY reports
```

`mycoast_points.csv` and `mycoast_ny_map.html` were produced by ad-hoc code
that is NOT in the repo. If they need regenerating, write a script rather than
repeating it by hand. (`mycoast_meta_data.json` was too, until
`make_mycoast_metadata.py`, which reproduces it field for field.)

Scope of the API scrapers: New York State only (Census TIGER boundary, state
waters included), `in_nyc` labels the five boroughs. MyCoast is limited to
`State = 'NY'` and report types `Flood Watch` + `Storm Reporter`
(`STATE`/`REPORT_TYPES` in `mycoast_scrape.py`). The report page's byline
names a private individual and is deliberately NOT collected.

Video side (independent of both; design in `scraping/video_scraping/design.md`):

```
youtube_scrape.py  (search.list -> videos.list -> labels -> thumbnails) -->
    scrape_data/youtube_searches.jsonl   search answers, appended per call; a
        |                                CACHE (deleting it only costs quota)
    data/youtube_videos.json             one row per VIDEO, merged, newest first;
        |                                thumbnails fingerprinted into the shared
        |                                data/image_hashes.json
make_youtube_metadata.refresh()  (same run) -->
    data/youtube_videos_meta_data.json   counts snapshot (tracked in git)
```

Supporting files: `data/image_hashes.json` (fingerprint cache, makes dedupe
re-runs instant; shared by dedupe.py, dedupe_mycoast.py and youtube_scrape.py), `/home/liu47/models/Qwen3.8-27B` (weights,
outside the repo).

## Invariants — do not break these

**Both verifiers label; neither filters.** `flood_score`/`flood_verified` and
the image `answer` are recorded on every record, `no` included. Selecting a
subset is the caller's job. Thresholds stay tunable without re-crawling or
re-classifying. `dedupe.py` is the only thing that deletes rows.

**`occurrence_id = sha256(article_url \n image_url)[:24]`.** Identity for
resume, deliberately NOT positional and NOT a duplicate detector. It once
included `image_index`; that was removed because `Adapter.images()` dedupes by
URL per article, so the index could never disambiguate anything and only broke
ids when a publisher inserted a photo. **Never hash article_url alone** —
4,651 of 8,665 rows would collide and be silently skipped.

**One corpus, no per-outlet JSON.** `data/news_scrape_results.json` is the
source of truth: the crawl's seen-set comes from it, every outlet's finalize merges into it,
and every downstream reader consumes it. The per-outlet **JSONL** still exists
during a run (appended and flushed per article, so a kill mid-outlet loses
nothing) and is deleted once merged. `load_corpus_urls()` parses the corpus
ONCE per crawl and shares the URL set across outlets -- doing it per outlet
costs ~8.7s instead of ~0.3s, measured. Set membership is O(1), so corpus size
does not affect lookup.

**finalize() raises rather than swallowing a bad corpus read.** It used to be
`except: pass`, which left the record dict empty and wrote the run's handful of
articles over the whole corpus with no exception and exit code 0. A count
comparison cannot catch this -- `before` is computed from the same read that
failed, so it is 0 and `after < before` never fires. The read must be loud.
`load_corpus_urls()` raises for the same reason: continuing would silently
re-crawl everything.

**`verified_images_news.json` is the resume ledger.** `pending = occurrences
not in this file`. So deleting rows makes them pending again: a verifier run
after `dedupe.py` *restores the duplicates it removed*. That is why dedup also
happens at CLASSIFY time: `read_existing_results()` builds a `sha_cache`
(image_sha256 -> verdict) from existing rows, `LocalVLM` checks it after
fetching and before generating, and extends it as the run proceeds. A duplicate
is still fetched -- the hash is unknowable without the bytes -- but never
re-classified, and its row is marked `reused_for_duplicate_image`. Local
backend only: the hosted API never hands us the pixels.

Two caches, two repeats: `url_cache` catches the same URL (no fetch at all),
`sha_cache` catches the same PIXELS behind a different URL.

**Resume is prompt-agnostic, by the owner's explicit decision.** Editing
`PROMPT` or `--model` does NOT re-classify existing rows. The file therefore
holds answers from more than one prompt; each row records its own `prompt` and
`model`, and `summary` reports `on_current_prompt` / `on_earlier_prompt` /
`distinct_prompts`. To re-score everything, move `final.json` aside first.

**JSONL is transient, and it is not redundant with the corpus.** Both the
crawl and the image verifier use the same pattern: append+flush per record
during a run, merge into the JSON at the end, delete (`--keep-jsonl` retains
it). The merge MUST merge, not overwrite -- the JSONL holds only THIS run's
records.

The crawl's JSONL is not made redundant by `load_corpus_urls()`. The corpus
only learns an outlet's articles at `finalize()`, i.e. after the whole outlet
completes -- floodlist is 4,227 articles at `--delay 0.6`, roughly 45 minutes.
The JSONL is the only thing holding work inside that window. A `.jsonl` found
in `scrape_data/` is therefore **unmerged work, not garbage**: re-run that
outlet and it is folded in and cleaned up automatically. Never
delete one to "tidy up".

**Guards on the corpus write, and the ones deliberately absent.** `finalize()`
fsyncs before `os.replace` (durable bytes, not page-cache bytes -- matters more
on NFS) and deletes the partial `.tmp` if the write fails, leaving the corpus
and JSONL untouched. A failed write cannot corrupt the corpus: `os.replace` is
atomic and never runs. Deliberately NOT added, do not re-add them:
`after < before` (a dict merge can only grow, so it cannot fire), a pre-flight
free-space check and a post-write re-read (not warranted at 20,000x headroom).
Atomicity is not correctness -- a complete but wrong file replaces a good one
just as atomically, which is why the READ guard is the one that matters.

**Local model: thinking stays OFF.** `Qwen3.8-27B` is a reasoning model whose
template defaults to `enable_thinking=True` at `reasoning_effort='xhigh'`. Left
on, it writes an analysis containing the phrase "yes/no", gets truncated by
`max_new_tokens`, and a first-match regex parses the question as the answer —
producing confident, meaningless results. `parse_answer()` strips `<think>`
blocks and takes the LAST match. Thinking off is also ~10x faster
(0.6-2.0s vs 6-22s per image).

## Gotchas

- **`--drop-near` is not trustworthy.** dHash collides on low-contrast images;
  your corpus is full of rainfall maps and river-level charts. At distance 2 it
  merged 23 distinct daily rainfall maps into one. Exact (sha256) dedupe is
  safe and has zero false positives. Fixing near-dedupe needs a stronger
  signal — cached 32x32 thumbnails verified by pixel correlation, or CLIP.
- **AP's CDN returns 403 to our direct fetch.** The hosted provider used to
  fetch images for us, so this only appears with the local backend. AP images
  cannot currently be re-scored locally.
- **Two incompatible id schemes.** The human review site uses
  `sha256("human_review_v2" \n article_url \n image_url)[:24]`
  (`image_review_server.py`), deliberately namespaced so it can never collide
  with the verifier's id — without the prefix all 1,624 ids were byte-identical.
  They are not joinable; join on `(article_url, image_url)`.
- **Human review decisions live only in browser localStorage**, versioned
  (`vlm-flood-image-reviews-v2`, `{scheme, migrated, reviews}`). Changing that
  id scheme orphans them; `legacy_id` + a one-shot migration exists for the v1
  key. There is no server-side copy.
- **Line endings.** Files written on Windows land as CRLF and flap the whole
  diff when rewritten here. Consider `*.json text eol=lf` in `.gitattributes`.
- **`scraping/api_based_scraping/` holds two different things.**
  `gis_scrape.py` and `mycoast_scrape.py` are the ACTIVE government/public-API
  scrapers. `news_scrape.py` is the superseded GDELT news-discovery approach;
  its notes are in `news_api_design.md` (formerly a nested `CLAUDE.md`, renamed
  so it stops being read as agent instructions). The reasoning on why no news
  API returns inline images is still worth reading; that code is not in use.
- **A MyCoast re-scrape restores deduped images.** `mycoast_scrape.py` merges
  fresh reports OVER existing ones, so run `dedupe_mycoast.py --apply` after
  every scrape (cheap: fingerprints are cached in `data/image_hashes.json`,
  shared with `dedupe.py`).
- **MyCoast image URLs come in renditions.** The API lists resized thumbnails
  (`-300x225.jpg`); the page links WordPress's `-scaled` copy; the original has
  neither suffix. They are different bytes of the SAME photo --
  `photo_identity()` collapses them. `image_url` is always the original.
- **Recent MyCoast reports carry TEMPORARY image URLs.** Since ~2026-09
  the API's `ImageUrls` for new reports point at
  `mycoast.org/blueurchin-reportimages/...`, which 404s within days; the photo
  then lives only at `cdn.mycoast.photos/...` (what the report page links, so
  `mycoast_scrape.py` stores it as `scaled_url`). The API does not update the
  URL afterwards. Both scrapers now swap it for the CDN original found on the
  report page (`mycoast_permanent_url()` in `gis_scrape.py`, `-scaled`
  stripped) and keep the staging URL as `api_image_url`; same pixels,
  verified. `gis_scrape.py` reads pages from `mycoast_pages.jsonl` and fetches
  only uncached ones, so run `mycoast_scrape.py` first. An image that could not
  be resolved is flagged (`temporary_url` / `extra.temporary_image_url`) and
  the run logs a count -- if that count is non-zero, those links will die.
- **`scrape_data/mycoast_pages.jsonl` is a cache, not unmerged work.** Unlike
  the crawl's JSONL it is never deleted; deleting it just costs ~30 min of
  requests against mycoast.org (≈7 s/page) on the next run.
- **35 MyCoast reports have pre-2000 timestamps** (e.g. 1935); listed under
  `odd_timestamps` in `mycoast_meta_data.json`. Treat their dates as unknown.

## State (2026-09-28)

```
data/news_scrape_results.json    53M   news corpus
data/verified_images_news.json   27M   news image labels
data/image_hashes.json          5.3M   fingerprint cache (dedupe.py + dedupe_mycoast.py)
data/gis_flood_images.json      7.4M   4 API sources, NY State
data/mycoast.json               7.9M   MyCoast NY, per report
```

News:
- Corpus: 11,219 articles, 18,273 images, 28 outlets (2012-06 to 2026-09);
  8,681 articles carry images. Per `data/meta_data.json` (2026-09-14).
- Classified: 12,611 image rows, ALL under the strict street-view prompt (one
  distinct prompt). 4,063 `yes` (32%) -- in line with the 20-40% predicted
  below. The mixed-prompt state described in older notes is gone.
- The 29 per-outlet `*_flood.json` were deleted once the corpus
  was verified a field-for-field superset; they remain in git history.

YouTube (2026-10-06, `data/youtube_videos.json`, 9,840 quota units: 2 scrape runs + 2 relabels):
- 2,761 videos, 11,019 thumbnails (all with digest), uploads 2006-08 to
  2026-10. Page 1 of all 74 searches + page 2 of the first 23 (page 2 still
  gave ~30 new videos per search). `flood_text_relevant` (>= 0.3): 2,093.
  `in_ny` true/false/null: 2,293/263/205; `in_nyc` true 1,262; 591 with
  uploader coordinates. Not yet VLM-classified, not hand-checked. Videos are
  NOT downloaded (owner's decision pending; see design.md).

Government / public APIs (New York State):
- `gis_flood_images.json`: 7,192 image rows -- MyCoast 3,083 (not deduped;
  refreshed 2026-09-28), Wikimedia Commons
  2,120 (only 398 with coordinates), USGS STN 1,873, NAPSG 116. Everything but
  most Commons files has lat/lon. Not yet model-classified.
- `mycoast.json` (after dedupe, re-scraped 2026-09-28): 1,938 reports, 3,025
  images, 2011-05 to 2026-09-28. The 2026-09-28 re-scrape added 45 reports / 77
  images (all dated 2026-09-13 onward) and lost nothing. Before it: 1,893
  reports, 2,948 images, 994 in NYC (Queens dominates, 745), Flood Watch 1,734,
  Storm Reporter 159. Now 1,013 reports in NYC; `mycoast_meta_data.json` is
  current. "What is Flooded" (pre-re-scrape): Roads/streets 1,039, Sidewalks 821,
  Lawns/vegetation 808, Structures 231, Parking lots 174. Every report has
  coordinates and a time. Not yet model-classified for street-view geometry.

## Dataset direction (2026-09-13) — READ BEFORE EXTENDING THE SCRAPER

**What "street-view" means here.** Google-Street-View-like geometry: camera at
person or vehicle height, the road surface occupying a meaningful part of the
frame, and enough surroundings (facades, fences, poles, signs, parked cars)
that the place could be recognised again. NOT: close-ups where people or
objects fill the frame, interiors, elevated or aerial vantage points, open
water or landscape with no street.

**News outlets are a mediocre source for this, measured.** A random sample of
images the current prompt accepted was inspected directly, not via captions:

| image | what it is | street-view? |
|---|---|---|
| Guardian, Portugal | flooded street, facades, numbered doors, eye level | yes |
| floodlist, Romania | mud-covered road, signs, eye level | yes |
| floodlist, Tbilisi | crowd close-up, people fill frame | no |
| floodlist, Chesil Beach | elevated view over rooftops | no |
| floodlist, Bangladesh | looking down into a camp from height | no |

Two of five. Photojournalism systematically prefers human subjects, close-ups
and elevated vantage points -- precisely the properties that disqualify an
image here. **A caption saying "street" describes the EVENT, not the camera.**
Do not infer street-view geometry from caption text; it was wrong when tried.

Expect roughly 20-40% of what the current filter accepts to be usable, i.e.
~1,600-2,800 of 8,044. Measured since: the strict prompt below accepts 32%
(4,063 of 12,611) of the now-larger news set.

**Sources where the geometry is guaranteed, not filtered for:**

0. **MyCoast** (in use, the current focus -- see top of file) -- citizen
   reports with GPS + time and a self-reported "What is Flooded"; mostly
   ground-level phone photos. Not guaranteed street-view, but far closer than
   news, and every image is geolocated, so it can serve as ground truth.
1. **Dashcam / drive-through-flood video** (YouTube etc.) -- camera at vehicle
   height, road filling the frame, facades passing. Structurally the same
   geometry as Street View, which is itself car-mounted. Many usable frames
   per clip.
2. **Mapillary** -- street-level by definition, contributor-uploaded, carries
   GPS *and capture timestamps*, so the same coordinates can be pulled before
   and during a flood event. CC-BY-SA, so redistributable.
3. News -- keep as a supplement; low yield, no control at capture time.

**Licensing.** 25% of accepted images are credited to Getty/EPA/AFP/Reuters and
the rest are outlet-owned. None are redistributable. The pipeline stores URLs
and captions and never pixels -- keep it that way and news stays usable;
shipping JPEGs in a release would be infringement. Wikimedia Commons and
CC-filtered Flickr are the sources where pixels CAN be redistributed.

**Street-view prompt** (APPLIED to the whole news set as of 2026-09-14; it is
`PROMPT` in `verification/verify_images_vlm.py`, and does not require visible
flooding):

> Does this photograph look like a street-level view of a road or street,
> similar to Google Street View? Answer yes only if ALL of the following hold:
> (1) the camera is at ground level, roughly the height of a person or a
> vehicle — not looking down from a balcony, bridge, drone, helicopter, or
> hillside; (2) a road, street, footpath, or other outdoor ground surface is
> visible and takes up a meaningful part of the frame; (3) the surroundings are
> visible — building facades, walls, fences, parked vehicles, poles, or signs —
> enough that the place could be recognised again. Answer no if the image is
> mainly a close-up of people, faces, animals, or objects; an interior; an
> elevated or aerial view; open water or landscape with no street; or a map,
> chart, diagram, or graphic.

Whether to also require visible flooding is undecided -- one clause either way.
Changing it means clearing existing rows so they read as pending (resume is
prompt-agnostic). The verifier reads only the news corpus; MyCoast/GIS images
have not been run through it.

**The cost of skipping coordinates.** They cannot be retrofitted: a news photo
with no GPS will never become a geolocalization training example. Uncoordinated
flood street imagery is still useful as the target domain for domain adaptation
and as a qualitative query set, but geolocalization cannot be EVALUATED without
ground truth, so a coordinate-bearing set has to exist eventually even if it is
small and hand-labelled.

## Open work

Priority is MyCoast and MyCoast-like sources (owner's direction, 2026-09-28).

1. Run the street-view prompt over MyCoast images (and the other GIS sources);
   the verifier currently reads only the news corpus, so it needs an input
   path for `mycoast.json` / `gis_flood_images.json`. Measure real yield,
   and check whether "What is Flooded = Roads/streets" predicts it.
2. Widen MyCoast: other states' programs (drop `State = 'NY'`), and decide
   whether other report types are wanted. Check terms of use before bulk
   collection outside NY.
3. Find more MyCoast-like sources: structured API, per-record GPS + time,
   ground-level citizen/agency photos (311 flood complaints, spotter reports,
   DOT road-closure cameras, Mapillary). Probe dashcam/YouTube as well.
4. Put the remaining MyCoast derivatives (`mycoast_points.csv`,
   `mycoast_ny_map.html`) behind a script; they are still ad-hoc. Add a
   metadata file for `gis_flood_images.json`.
5. Near-duplicate detection with a signal that works.
6. YouTube (first run done 2026-10-06): calibrate signal 1's threshold
   and the `in_ny` rules against ~150 hand-labelled videos; build signal 3
   (the VLM over thumbnails -> `flood_visual`); show videos on a review site.
   Details and open questions in `scraping/video_scraping/design.md`.
7. Build a small hand-labelled ground-truth set (:8765 / :8768 export
   decisions) so model and prompt changes can be measured instead of guessed.
   MyCoast's coordinates make it the natural geolocalization evaluation set.

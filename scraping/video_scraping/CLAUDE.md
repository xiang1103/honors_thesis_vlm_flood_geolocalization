# Video scraping: decisions and how the scraper works

This folder collects flood videos from YouTube. This file records the
**decisions the owner made** (2026-10-05 to 2026-10-07) and the reasoning
behind them, so they are not re-litigated. `design.md` here has the full
technical detail and measurements; the record FORMAT is the contract in the
root `CLAUDE.md` ("REQUIRED data format for VIDEOS").

## Owner's decisions

| # | Decision | Why / instead of |
|---|---|---|
| 1 | Videos have **their own format**; the MyCoast image format applies to static images only | a video is not a photo report; forcing it into `mycoast.json`'s shape did not fit |
| 2 | **Coordinates are optional.** A video without them is kept (`lat`/`lon` = `null`), never dropped | the goal is flood-relevant video; most uploads carry no location |
| 3 | **New York is a label, not a filter** (`in_ny` / `in_nyc` true / false / null) | searches aim at NY, but nothing outside it is discarded |
| 4 | **Keep every video, label it** (`flood_text_score`) | thresholds stay changeable without searching again (repo-wide "labels, not filters") |
| 5 | **Flood-relevance threshold is 0.3** (`FLOOD_TEXT_THRESHOLD` in `video_signals.py`) | owner's choice 2026-10-06; was an unstored 0.4. Relabel with `--no-search` (~1 unit / 50 videos) |
| 6 | **Keep relevance ranking.** Walk every page of every search; when all are walked, **start over** (rolling re-walk, oldest page first). Multi-day cycles are fine | owner rejected a date-sorted "what's new" pass because it gives up relevance order (see "Rejected") |
| 7 | **The ~500-results-per-query cap is accepted** for now | not a concern yet; date-sliced searches are the known fix if it becomes one |
| 8 | **Run daily from cron on this server** (03:30 Eastern, after the midnight-Pacific quota reset) | GitHub Actions rejected: all scraper state is gitignored and lives here (see "Rejected") |
| 9 | **Videos are NOT downloaded, for now.** Only IDs, metadata, thumbnails and the embed link are stored | owner said "don't make download changes yet" (2026-10-06). See "Downloading" |
| 10 | **Fields trimmed (2026-10-07)**: `channel_id`, `channel_title`, `category_id`, `language`, `license`, `location_basis`, `ny_basis`, `ny_places`, `flood_text_hits`, `queries`, `text` and `api_fields` are no longer stored, in existing data or future scrapes. 23 fields remain (`VIDEO_FIELDS`) | owner's cleanup. Master file 21 MB -> 8 MB. Labels unchanged: channel title, category and the joined text are still read from the API response while labelling, then discarded. Lost: auditing a score from the file, and re-deriving fields without the API (a re-check costs ~1 unit / 50 videos anyway). `queries` is recoverable from the JSONL. Earlier decision to keep `api_fields` (~46% of the file) reversed. Second trim the same day: `flood_text_relevant` (derivable: score >= 0.3) and `flood_event_date` dropped too -> 21 fields; the event-date signal is no longer computed |
| 11 | **Two files: the master keeps every video; a derived flood-only dataset** (`data/youtube_flood_videos.json`) holds ALL flood videos, NY or not (2026-10-07) | dropping non-flood videos from the master would make relabelling cost quota, and the JSONL still lists their IDs, so every night would re-fetch and re-judge them. The derived file is the one to hand out or train on |
| 12 | **Visual rule (2026-10-07)**: a flood video needs text >= 0.3 AND flooding visible in at least one of the 3 automatic frames; if none shows flooding, it is not a flood video. One plain prompt, nothing about NY or street level. The cover is not judged | the text signal alone let in obvious non-floods (a Swiss mountain drive past "a Flooded Village", an Alexander the Great history video); the frames are what the footage shows. Chosen over downloading videos or transcripts for now |

## How YouTube charges, and what that means

Quota is **10,000 units per day**, reset at midnight Pacific. It is charged
per **request**, not per field or per video:

| request | cost | returns |
|---|---|---|
| `search.list` | **100 units per page**, flat, whether the page holds 50 results or 0 | up to 50 video IDs (and a next-page token) |
| `videos.list` | **1 unit per request of up to 50 IDs**, however many detail parts are asked for | everything stored about those videos |

So ~99% of the quota is searching. One page of 50 fully described videos
costs 101 units. **No stored field costs extra:** the labels, the NY
decision, ids and URLs are computed locally, and thumbnails come from
`i.ytimg.com`, which is not the API and needs no key.

## The two files, and what each one tracks

| file | unit | tracks | prevents |
|---|---|---|---|
| `scrape_data/youtube_searches.jsonl` | one line per **search page** | which pages of which search are fetched, the 50 IDs each returned, and the token for the next page | paying again for a page already fetched; it is the **bookmark** that lets each night continue where the last stopped |
| `data/youtube_videos.json` | one entry per **video**, keyed by `video_id` | every video found that can play, with its labels | duplicates: a video found again is merged into its entry and the new search added to its `queries` |

Losing the JSONL costs quota (the walk restarts) but no videos. Losing the
JSON costs ~1 unit per 50 videos to rebuild from the IDs in the JSONL, but
anything not recomputable (future `flood_visual` answers) is gone. Neither is
in git.

**A page is all or nothing**: one request, saved as one line. What can be
unfinished is (a) a **search** not yet walked to its end (more pages exist),
and (b) **IDs without details** (found by a search, `videos.list` not run yet;
the next run fetches every ID in the JSONL missing from the JSON).

**Tokens are positions, not snapshots.** `CDIQAA` decodes to "start at result
50" and `CGQQAA` to "start at result 100", identical for every search.
Asking for page 3 later returns results 101-150 **of the ranking at that
moment**.

## What one run does (the cron job runs exactly this)

1. **Search** (budget minus a reserve for step 2):
   - **Walk**: every page never fetched, **breadth-first** (page 1 of all 74
     searches, then page 2 of all, ... to page 10), each page N+1 requested
     with the token saved on page N.
   - **Re-walk**, only when the walk has nothing left: re-fetch saved pages
     **oldest first**, skipping pages already fetched in this run.
2. **Details**: `videos.list` for every ID in the JSONL not yet in the JSON,
   plus a re-check of every stored video (title edits, deletions ->
   `available: false`, never removed).
3. **Labels and thumbnails**: text score, event date, NY labels; thumbnails
   fetched and checked, only decodable ones kept.
4. **Write**: merge into the JSON by `video_id`; regenerate
   `data/youtube_videos_meta_data.json`.
5. **Export**: regenerate `data/youtube_flood_videos.json` from the master
   (`export_flood_videos.is_flood()`: `flood_text_score >= 0.3`, `available`,
   and `flood_visual.flood is True`) and its metadata. Changing the rule
   needs no API: re-run `export_flood_videos.py`.
6. **Visual check** (separate, GPU; NOT in cron yet):
   `verification/verify_video_frames.py --device-map cuda:<free GPU>` asks
   the local model "is there any flooding visible?" for each of YouTube's
   three automatic frames, records them in
   `scrape_data/youtube_frame_answers.jsonl` (resume ledger, a cache: never
   delete), writes `flood_visual` back under the scraper's lock and
   rebuilds the flood-only file. Only new videos cost anything on a re-run.

Numbers: 740 pages for a full walk (74 searches x 10), about 93 pages a night
at the default 9,500-unit budget, so **one full walk or re-walk takes about 8
nights**. As of 2026-10-07: 97 pages done (page 1 of all, page 2 of 23).

## New uploads and the re-walk

YouTube search is **re-ranked by relevance on every request**; a new upload
is inserted wherever it ranks, usually not at the end. So a walk that saved
pages 1-10 last week does not see a video that has since ranked in at #30.
The rolling re-walk handles this: every page is re-fetched about every 8
days, so a new video in a query's top ~500 is picked up within roughly that
lag. What can still be missed:

- a video that ranks into a page and back out between two visits to it
  (most likely for brand-new uploads, whose rank moves fastest);
- a video deleted within days of upload;
- anything beyond a query's top ~500 (decision 7).

## Rejected alternatives (and why)

- **A date-sorted "what's new" pass** (`order=date`, `publishedAfter=` last
  run): catches every new upload on page 1 with a time-based bookmark, and is
  cheap only with a few broad OR queries. **Rejected by the owner** because
  it drops relevance ranking. (Note for later: relevance *matching* still
  applies, since only the *order* changes, but the results are noisier.)
  Revisit if new floods must be captured within a day.
- **Re-walking every page nightly**: impossible; a full pass is ~74,000 units
  against 10,000 a day. Became the rolling re-walk.
- **A tiered re-walk** (page *p* refreshed every *p* x 2 days, so top pages
  more often): offered; the owner chose plain oldest-first.
- **Never re-fetching deep pages, only page 1 every 30 days** (the first
  implementation): misses new uploads that rank below page 1. Replaced.
- **GitHub Actions**: each run starts on a fresh machine, and every file the
  scraper resumes from is gitignored (JSONL bookmark, the 20+ MB video file,
  the 7 MB thumbnail-hash cache, `mycoast.json` for NY place names). It
  would mean a daily bot commit of a growing JSON, or Actions' cache, which
  deletes entries unused for 7 days and would silently reset the bookmark.
- **Hard negatives matched in descriptions**: zeroed 32 of 44 videos in the
  first run, nearly all real flood news (descriptions mention insurance or
  carry "real estate" boilerplate). Now title and tags only; in the
  description they only lower the score.

## Downloading (open, owner's decision pending)

YouTube's Terms of Service forbid downloading except through YouTube's own
features. **Third-party download websites do not change that**: they perform
the same download, and they are unreliable and often unsafe. The tool
research groups actually use is **yt-dlp** (Kinetics, YouTube-8M-style
datasets ship IDs + labels and users fetch the videos themselves). The owner
said they accept the risk, then said not to make download changes yet. If
asked to build it, the agreed sketch is:

- `scraping/video_scraping/youtube_download.py`, reading
  `data/youtube_videos.json` and downloading only videos passing a label
  filter (e.g. the flood-only file);
- output `data/youtube_videos/<video_id>.mp4` at <= 720p, gitignored, never
  redistributed (a release ships IDs + labels only);
- per-video `local_path` / `downloaded_at` / `download_error` so re-runs skip
  done ones; a few seconds between downloads;
- test on ~5 videos first: YouTube may answer this server's IP with "Sign in
  to confirm you're not a bot", which needs browser cookies;
- update the root `CLAUDE.md` "No video bytes, ever" rule in the same change.

Sources that allow downloading, should frames with GPS be needed: Mapillary
(street-level image sequences, GPS per frame, CC-BY-SA), uploaders' own
originals with permission (often still carrying GPS), Wikimedia Commons,
Internet Archive, US federal footage (public domain).

## Operating it

```bash
crontab -l                                         # the daily job (03:30)
tail -30 scrape_data/logs/youtube-cron.log         # what last night did
python3 scraping/video_scraping/youtube_scrape.py --plan        # searches + cost, no API calls
python3 scraping/video_scraping/youtube_scrape.py --no-search   # relabel / re-check only, ~60 units
```

**Viewing them:** `python3 gis_review_server.py`, then the **Videos** tab
(`http://127.0.0.1:8768/videos.html`; `gis_review_web/videos.html` +
`videos.js`). Cards show the cover, duration, flood score, location, title,
date, tags and description; the detail view embeds YouTube's player
(`youtube-nocookie.com`, the only frame the site's CSP allows) above the
three automatic frames. Defaults to flood videos only (score >= the same
`FLOOD_TEXT_THRESHOLD`); filters for location, review decision, sort and a
text search. Decisions (useful / reject / unsure, keys 1-3) live in browser
localStorage under their own key and id scheme (`youtube_review_v1`),
separate from the photo page's. The server reads the JSON at start-up:
restart it to see a newer scrape. The player needs a referrer (YouTube error
153 without one), so the iframe sets its own `referrerPolicy` while the rest
of the site stays `no-referrer`.

A manual run while the nightly one holds the lock exits at once ("another run
holds ..."). Manual runs share the same 10,000-unit daily quota.

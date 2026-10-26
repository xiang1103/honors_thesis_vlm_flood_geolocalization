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
| 8 | **Run WEEKLY from cron on this server** (Sunday 03:30 Eastern, after the midnight-Pacific quota reset; scrape -> visual check -> shot scan in one chain, `run_weekly.sh`; daily until 2026-10-24, owner's change) | GitHub Actions rejected: all scraper state is gitignored and lives here (see "Rejected") |
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
6. **Visual check** (separate, GPU; step 2 of the weekly `run_weekly.sh`,
   right after the scrape): `verification/verify_video_frames.py` (default `--device-map
   free`: the GPU with the most free memory, >= 60 GiB, picked at run time;
   none free -> it skips the week and the videos wait) asks
   the local model "is there any flooding visible?" for each of YouTube's
   three automatic frames, records them in
   `scrape_data/youtube_frame_answers.jsonl` (resume ledger, a cache: never
   delete), writes `flood_visual` back under the scraper's lock and
   rebuilds the flood-only file. Only frames without an answer are checked
   (new videos, earlier fetch failures); with none it exits before loading
   the model, and the write-back is skipped when nothing changed.

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

## PLAN: flood scenes inside each video (owner's design, 2026-10-08) — NOT IMPLEMENTED

Nothing in this section exists yet. It is the agreed plan; build it only when
the owner says to, and confirm the items under "Decisions still open" first.

### Why

A flood video is usually edited: a news package cuts between the anchor, a
flooded street, an interview, the same street again and a map; a compilation
cuts between cities. Treated as one sample, the video mixes places and pads
the flood footage with studio shots, faces and graphics. Geolocalization needs
footage from one place at a time. The three-frame check already shows the
symptom: "SEEN 1/3" videos are often packages where one sampled frame happened
to land on street footage.

### What the final data is (owner's decisions)

- **The data stays videos.** Nothing is cut into clip files or frames in the
  dataset. Each video entry gains the **time ranges** of its flood footage.
- **Only flood sections are kept.** Anchor, interview, map and dry-street
  sections are left out completely -- not stored with `flood: false`.
- **Sections of the same place are grouped.** A *scene* is one place; it can
  appear more than once in a video (cut away, cut back), so a scene holds a
  list of time ranges.
- **The field goes inside each video entry** (not a separate file joined on
  `video_id`).

Terms: a **shot** is a continuous stretch between two cuts (one camera
take); a **scene** is one or more shots of the same place. Frames are used
only internally, to find cuts and to label shots, and are then discarded.

### Proposed field (to be added to the video contract in the root CLAUDE.md)

```json
"scenes": [
  {"scene": 1, "segments": [[12.4, 31.0], [58.2, 70.5]]},
  {"scene": 2, "segments": [[95.0, 121.3]]}
]
```

| key | type | meaning |
|---|---|---|
| `scenes` | list or null | `null` = not segmented yet; `[]` = segmented, no flood footage found; otherwise the flood scenes, in order of first appearance |
| `scene` | int | 1-based, per video |
| `segments` | list of `[start_s, end_s]` | seconds from the video start, 1 decimal, sorted, non-overlapping; every range shows flooding at this scene's place |

Every segment plays without any clip file:
`https://www.youtube-nocookie.com/embed/<video_id>?start=<int start>&end=<ceil end>`.
So the published dataset stays video IDs + timestamps + labels (the Kinetics /
AVA shape); the downloaded files exist only to produce the timestamps.

### Pipeline (one new step per stage, all resumable, all nightly-cron-able)

1. **Download** (prerequisite; see "Downloading" above -- needs the owner's
   go-ahead and the root "No video bytes, ever" rule rewritten in the same
   change). Only videos in the flood-only file; <= 720p; videos longer than a
   cap (proposed 15 min: livestreams run to 7+ hours) skipped or only their
   first 15 min fetched. Bookkeeping in a ledger
   (`scrape_data/youtube_downloads.jsonl`), NOT new fields in the video entry
   (the owner trimmed the entry on 2026-10-07). Files under
   `data/youtube_videos/` (gitignored), never committed, never redistributed.
   Pilot 5 videos first for the "confirm you're not a bot" block.
2. **Shot detection.** TransNetV2 (small neural model, the standard; handles
   fades and dissolves; fast on GPU), with PySceneDetect as a CPU fallback.
   Output per video: the cut times. Shots shorter than ~1 s (flashes,
   transitions) are merged into a neighbour or dropped.
3. **Label each shot.** The local VLM on ~3 keyframes per shot (start,
   middle, end), the same plain question as the frame check: "Is there any
   flooding visible in this image?" A shot is a flood shot if at least one
   keyframe says yes (mirrors the owner's 1-of-3 rule for videos). Answers go
   to a ledger keyed by (video_id, shot start, frame), like
   `youtube_frame_answers.jsonl`. Non-flood shots are dropped here.
4. **Group flood shots into scenes.** Image embeddings (e.g. CLIP) of each
   flood shot's keyframes; shots whose embeddings are close are the same
   place. Adjacent flood shots separated only by a cut of the same view merge
   into one segment; non-adjacent matches become extra segments of the same
   scene. The similarity threshold is set on the pilot, by eye.
5. **Write back** `scenes` into `data/youtube_videos.json` under the
   scraper's lock (like `verify_video_frames.merge()`), then rebuild the
   flood-only file and metadata.
6. **Export rule** (decision open, see below): once a video is segmented,
   whether it stays in `youtube_flood_videos.json` should follow from
   `scenes` -- e.g. "at least one flood scene" -- rather than from the three
   thumbnails.
7. **Review site.** The Videos page lists a video's scenes; clicking a
   segment plays exactly that range (`?start=&end=`), so segmentation errors
   are checked by watching, not by trusting numbers.
8. **Metadata.** New keys in `youtube_videos_meta_data.json` /
   `youtube_flood_videos_meta_data.json` (adding keys is allowed): videos
   segmented, scenes, segments, flood seconds, scenes per video.

### Owner's decisions, 2026-10-14

- **Batch, then delete.** Videos are downloaded one batch at a time, the
  batch is processed together (shot detection and VLM labelling across all
  its videos at once), and the files are **deleted right after**. Only the
  `scenes` timestamps remain; **no video file is kept** between batches or
  after the run. (This settles open decision 5, and narrows the root "No
  video bytes, ever" rule's rewrite to "transient, per batch, deleted after
  processing" -- still to be written in the root CLAUDE.md when built.)
- **Experiment first, short videos only.** No long videos in the first
  runs (settles open decision 4 for now: skip them).
- **Creator chapters are not used.** The YouTube Data API exposes no
  segment/chapter field; chapters exist only as "0:00 Title" lines in the
  description. Measured 2026-10-14: 30 of 4,548 flood-only videos (0.7%)
  have valid chapters, mostly long explainers (median 571 s vs 60 s overall),
  and they mark topics ("What Is a Nor'easter?"), not camera cuts. YouTube's
  auto "key moments" are not in the API. Shot boundaries must come from the
  pixels.

### Experiments (started 2026-10-14)

`flood_depth_experiments/scene_split/` -- one example video at a time, methods compared by
eye before anything touches the scraped data. Downloader:
`scraping/video_scraping/youtube_download.py` (`with downloaded(url) as v:`;
720p video-only H.264, temp dir deleted on exit, duration cap; needs `yt-dlp`
and `deno` in the conda env -- Node 18 is too old for yt-dlp's YouTube JS
challenge). Script: `flood_depth_experiments/scene_split/compare_shot_detectors.py`. Outputs
(results.json, index.html, small keyframes) go to
`scrape_data/experiments/scene_split/<video_id>/`, gitignored; the video file
is never kept.

Run 1, PySceneDetect defaults, `xtBb2eU9d2k` (Indian Express NYC flood
package, 191 s, vertical phone clips on a blurred background, then a mayor's
press conference and an end card): content 19 shots, adaptive 18, histogram
19, hash 15, threshold 1 (no fades to black -- it detects fades, not cuts).
All four cut detectors agree on 11 cuts; hash misses several; adaptive adds 3
inside 30-48 s the others do not see. Visible failure modes: a 0.6 s flash
shot at 99.8 s; one CCTV clip split in two at 108.3 s (same view both sides
-- the scene-grouping step should re-merge it); four quick cuts inside one
subway station (83.8-94.1 s), which are probably real cuts between phone
clips of the same place (again a grouping job). Shots 15-19 (press
conference, end card) are the non-flood footage the flood label must drop.

**Owner's decision after run 1: keep only `content` and `adaptive`**; hash,
histogram and threshold were removed from the experiment (hash missed real
cuts, histogram added nothing over content, threshold only finds fades).
Each run now clears the video's output folder first, so stale keyframes of
dropped detectors cannot linger.

**Content threshold stays at 27 (the default), owner's decision
2026-10-14**, after trying 25 on `xtBb2eU9d2k`. Counter-intuitively 25 gave
FEWER shots (17 vs 19): ContentDetector's default `filter_mode=merge` joins
cut-like frames less than `min_scene_len` (15 frames, 0.6 s) apart, and in a
shaky subway clip (87.5-88.5 s, every frame scoring 28-36, plus periodic
25-27 spikes that look like frame-rate-conversion artifacts) the lower
threshold kept the merged run alive until 92.1 s, swallowing a real hard cut
at 89.84 s (score 50.4). Measured alternatives: 27/suppress 21 shots
(+87.6, +88.2, motion), 25/suppress 24 shots (+31.2, possibly real, and four
in the subway clip). Lesson: never lower the threshold with the merge filter;
if more sensitivity is wanted later, pair it with `filter_mode=suppress` and
let scene grouping re-merge the pieces.

Run 2, **TransNetV2 added** (2026-10-14; PyTorch port `transnetv2-pytorch`,
original weights, CPU, 25 s for the 191 s video vs ~6 s for PySceneDetect).
The script is now `flood_depth_experiments/scene_split/compare_shot_detectors.py`
(methods: content, adaptive, transnetv2). TransNetV2: 17 shots; agrees with
content and adaptive on 13 cuts. It alone found **71.5 s: Metro-North train
clip -> NWS rainfall-risk map** (content and adaptive both kept 48.8-83.8 s as
one shot, i.e. a non-flood graphic glued to flood footage -- the worst kind
of miss, since no later step can split it). It missed **30.6 s** (jet-ski
underpass -> a different flooded road), which content found, and 179.0 s
(press conference -> end card, harmless). No frames were labelled "in
transition" (no gaps between its shots). Adaptive's extra cuts at 34.0 /
37.0 / 46.0 s are unconfirmed (check by watching). Candidate rule from this
one video: **union of content + transnetv2 cuts, then drop shots < ~1 s**;
extra cuts are cheap (scene grouping re-merges), missed cuts are not.

**Owner's decision after run 2: adaptive removed**; the experiment now runs
content (threshold 27) and transnetv2 only.

Run 3, **OmniShotCut v1.5 added** (2026-10-14; `uva-cv-lab/OmniShotCut_v1.5`,
MIT, installed with `pip install --no-deps` from GitHub because its
requirements.txt pins transformers==4.57.3 -- the env keeps 5.6.1; runs on
the freest GPU via `verify_video_frames.pick_free_gpu`, ~28 s incl. first
download). Newer alternatives considered: AutoShot (CVPRW 2023, weights only
on Baidu / a Drive mirror) and PERSIST (Aug 2026 preprint, targets false cuts
from flashes / text overlays, mmaction2-based) -- not added.
On `xtBb2eU9d2k` OmniShotCut found 18 shots and agrees with both others on
13 cuts, and is the only one that LABELS transitions (gaps at 30.9-31.3,
84.0-84.4, 125.5-125.9 s and around the end card). But it **missed 48.8 s
(road -> train) and 71.5 s (train -> weather map)**, leaving one 31.3-84.0 s
shot spanning three places -- worse than content + transnetv2 here. Possible
cause (unconfirmed): this video is vertical clips on blurred backgrounds and
OmniShotCut works at ~128x96, so a cut in the centre strip changes little of
the frame. Not yet tried: its "default" mode (all ranges with transition
labels) and a full-frame video.

Run 4, second video **`i-U6LeACMnk`** ("Flooding A Concern On Long Island",
CBS2 TV news, 112 s, full-frame, 24 fps, snowy scenes, several dissolves):
content 14 shots, transnetv2 25, omnishotcut 17. Judged from the shots on
either side of each cut (one keyframe per shot, so not frame-exact): **all 24
transnetv2 cuts look real**; omnishotcut found 16 (it caught the 3 dissolves
at 22.8 / 31.5 / 58.1 s, labelling them transitions, but missed 8 plain
cuts); content found 13 (missed the dissolves and cuts between
similar-coloured snowy scenes). So omnishotcut's misses are NOT explained by
the vertical-clip format of video 1. **Across both videos: transnetv2 has the
best recall; content once caught a cut transnetv2 missed (video 1, 30.6 s);
omnishotcut has not caught any cut both others missed.**

**Owner's decision 2026-10-15: OmniShotCut removed** (code, package, cached
weights, and its rows in the two result pages, rebuilt from results.json).

Run 5, **AutoShot added** (2026-10-15; Zhu et al., CVPRW 2023, MIT; code and
checkpoint `ckpt_0_200_0.pth` in `/home/liu47/models/AutoShot`, outside the
repo -- checkpoint from the README's community Google Drive mirror, loaded
with `weights_only=True`, all 90 tensors match; authors' inference: 48x27
frames, 100-frame windows keeping the middle 50, threshold 0.296; freest GPU,
~6-8 s per video; env gained `einops`, `gdown`). It found **every cut
transnetv2 found on both videos, plus 30.6 s and 71.5 s on `xtBb2eU9d2k`
(previously each needed a different method) and two cuts no method had
found: 42.2 s on `i-U6LeACMnk` (snowy grate close-up -> boots in slush) and
102.2 s on `xtBb2eU9d2k` (rainy street, red car -> CCTV manhole geyser)**.
Misses: content's 89.0 / 91.9 s inside the subway montage (also missed by
transnetv2; uncertain). Noise: two 1-frame slivers inside the 30.6 s
dissolve (a min-shot-length filter removes them). On these two videos
AutoShot alone covers what needed content + transnetv2 together.

**Owner's combining rule: UNION of transnetv2 + autoshot (2026-10-15)** --
neither model wins outright (each catches cuts the other misses), so both run
on every video and `methods.combine()` merges them; shown as the "union" row
of the comparison pages. Owner's requirements: "more shots the better, never
lose a shot":
- split wherever EITHER model splits; edges from different models within
  0.2 s are the same cut and count once; edges from the same model are
  never merged (a genuine short shot survives);
- a stretch is a gap only if BOTH models leave it out;
- short shots are NOT merged into neighbours; only 1-frame ("0 s") shots
  are removed (measured in frames: a 0.05 s cut-off let 1-frame shots
  through at 24 fps);
- rejected: "keep whichever model returns more shots" (more shots can be
  noise, and it throws away the other model's unique real cuts); averaging
  the two models' per-frame probabilities (loses cuts only one sees strongly).
Result: `i-U6LeACMnk` 27 shots (autoshot 26), `xtBb2eU9d2k` 24 (autoshot 22).
Known side effect: 2-3-frame pieces made of blend frames at cuts BOTH models
found (one model calls them mid-transition, the other puts them in a shot):
31.4 s on `i-U6LeACMnk`; 30.7, 30.8, 83.9, 125.4 s on `xtBb2eU9d2k`. Open
choice for the owner: keep them, or gap frames one model calls transition
when they sit at a cut both found.

**Per-shot flood check + full scan (owner's rule, 2026-10-15)** --
`flood_depth_experiments/scene_split/scan_shots.py`. For each union shot, frames at 10% /
50% / 90% of the shot (shots under 1 s: one middle frame), the frame-check
question plus "Count only flooding in the scene itself, not in text, maps or
graphics"; a shot is a flood shot if >= 2 of 3 frames say yes (1-frame
shots: that frame). Test on the two videos: 25 of 51 shots flood; press
conferences, interviews, anchors, the weather map and end cards dropped;
borderline wet surfaces (a wet train platform, slush, a curb puddle) mostly
rejected -- strict. The scan covers the 4,123 flood-only videos <= 5 min,
4 workers (`--shard i/4`, GPUs 0/2/4/6, started 2026-10-15 with
`setsid nohup`, logs `scrape_data/logs/shot-scan-shard<i>.log`); results in
`scrape_data/youtube_shot_scan/shard_<i>of4.jsonl` (one file per worker --
NFS), one line per video with BOTH models' raw shots (union can be re-derived
without re-downloading), every frame's answer and the per-shot verdict.
Resumable: re-running skips finished videos. Not yet written into
`data/youtube_videos.json` (waits for scene grouping / the `scenes` field).
Lesson: the first start ran TransNetV2 on CPU in all four workers -- 100-400
s per video and a machine load of ~550 on 344 cores; both shot models now
run on the worker's GPU (3 s each, identical cuts) and each worker is capped
at 4 CPU threads. ~700 videos/h total after the fix.

**YouTube bot check, 2026-10-15: now ONE slow worker (owner's decision).**
After ~1 h at ~750 videos/h (4 workers), YouTube answered every download with
"Sign in to confirm you're not a bot" and all four workers stopped themselves
(750 videos done, no failures: 7,543 shots, 4,314 flood; 698 videos with a
flood shot; 9.3 of 14.0 h of footage in flood shots). The scan now runs
through `flood_depth_experiments/scene_split/run_scan.sh`: one worker, a randomised
`--gap` (mean 10 s) between downloads (~180 videos/h); on a bot check
`scan_shots.py` exits 3 and frees its GPU, and the loop probes once every
2 h (`--probe`: metadata only, no download, no model) and restarts when
YouTube allows downloads again. Log: `scrape_data/logs/shot-scan.log`; a lock
stops a second loop. Cookies from a signed-in account were NOT used (owner
did not choose that; it risks the account).

**Cron, and two stops fixed (2026-10-24).** The scan had sat idle
since 2026-10-19: `--probe` tested only the first pending video, which had
gone private, and an `Unavailable` there stopped the loop. The probe now skips
gone videos (up to 10) and tests the next one; the scan itself records them as
permanent errors and moves on. `run_scan.sh` now runs from cron, WEEKLY, as the
last step of `scraping/video_scraping/run_weekly.sh` (Sunday 03:30: scrape ->
visual check -> shot scan; owner, 2026-10-24), with `BLOCK_WAIT_S=0`: on a
YouTube block it stops (exit 3) and the next try is next week, instead of
probing every 2 h. The lock makes a run that is still going end the new one. Also new that day: YouTube refused every media
stream (HTTP 403, https and HLS alike, yt-dlp 2026.10.21, no PO-token
provider) while metadata still worked, so the probe passes but downloads
fail. `scan_shots.py` now treats `STREAM_403_BLOCK` (5) stream 403s in a row as
a block and exits 3, so the run stops (or, run by hand, waits 2 h) rather
than failing through the whole list. Those 403s are logged as transient and retried.

**PO-token provider installed (owner, 2026-10-24) -- it did NOT lift the 403.**
`bgutil-ytdlp-pot-provider` 2.0.2, script mode on Deno (system Node is v18, it
needs >= 22): the pip plugin in the conda env, plus the repo at
`~/bgutil-ytdlp-pot-provider` (the plugin's default `server_home`; set up with
`deno install --allow-scripts=npm:canvas --frozen` in `server/`). Nothing to
keep running: yt-dlp spawns the script when a client needs a token (~7 s
warm; the first cold run took 17 s, over the plugin's 15 s version check, so
it can fail once after a long idle). yt-dlp's default clients (`tv`,
`visionos`) ask for no token; forcing `player_client=mweb` makes it fetch one
("Retrieved a gvs PO Token"), and the stream still answers 403 -- for
"Me at the zoo" (jNQXAC9IVRw) too. So the block is on this server's IP
(130.245.4.197), not a missing token. `youtube_download.py` keeps yt-dlp's
default clients; the provider is there for when one needs a token. The weekly
run retries, so the scan resumes on its own within a week of the block
lifting. Not tried: cookies (owner declined).
**Confirmed IP block (2026-10-24):** the same yt-dlp download of jNQXAC9IVRw
worked from the owner's laptop on another network the same day, while this
server got 403 -- so it is this server's address, not a YouTube-wide change.
**It lifted on its own the next day** (between the 00:06 and 06:06 probes of
2026-10-25, ~1 day after it was noticed, 6 days after the last download). The
6-hour loop (`BLOCK_WAIT_S=21600 run_scan.sh`, started by hand 2026-10-24)
then scanned 211 videos until a bot check at 08:52 and 121 more after the
14:52 probe until another at 15:52 -- the Oct 15-17 pattern of short bot-check
blocks is back, the 403 is gone.

**Proxy fallback (owner, 2026-10-24): proxy AND wait.** `YTDLP_PROXY` (env,
else `.env`; template in `.env.example`) is any yt-dlp proxy URL. Each process
downloads DIRECT first; the first video refused there (stream 403 after its 3
tries, or the bot check -- both per IP) switches that process to the proxy
for good (`_direct_refused` in `youtube_download.py`). The whole yt-dlp run
goes through the proxy, metadata included: stream URLs carry the extracting
IP (`ip=`), so a direct extraction cannot be downloaded through a proxy. If
the proxy is refused too, `STREAM_403_BLOCK` ends the scan (exit 3), the loop
waits 2 h, and the next process starts direct again -- so once the block lifts
the proxy is no longer used. `--probe` now downloads ~10 KB of a real stream (yt-dlp `test` mode), direct and
then through the proxy if set, and exits 3 when every route is refused -- so
while blocked, each 2-hourly retry costs ~4 s and never loads the VLM (before,
a metadata-only probe passed, the VLM loaded, and 5 videos failed).
Each ledger line records `download_via` (`direct` / `proxy`). Tested with a
local CONNECT proxy: switch after the first video, every later request
(youtube.com, manifest and stream hosts) through it. No proxy is configured
yet; until one is, behaviour is direct only, as before.

**Shot prompt v2 (owner, 2026-10-15), used from the 751st video on:** real
flooding seen from STREET LEVEL, surroundings visible, real (not
AI-generated / animated / rendered / game); no for maps, graphics, captions,
anchors, interviews, people filling the frame, aerial/drone, indoor, water
close-ups, roads only wet from rain; plus a note that phone videos have
blurred side copies (judge the sharp middle). Text: `SHOT_PROMPT` in
`scan_shots.py`. On the 2 test videos' 139 saved frames: flood shots 25 -> 19
of 51. Correctly dropped: drain / boots / shovel close-ups, subway stairs and
station, bus interior. A first, stricter draft ("ALL of the following",
"wet roads or small puddles") dropped to 10, rejecting cars driving through
flooded Long Island streets and the kayak in a flooded underpass (the blurred
side bars of vertical video probably read as "not real") -- hence the
softer wording. Left: snowmelt-wet streets are split, one boots-in-slush
frame passes. The first 750 videos keep their v1 answers (each line records
its prompt).

**AI-generated videos (owner, 2026-10-15).** Two layers; the owner's rule is
to drop an AI video WHOLE, and declined a hand-labelled test set (a few
example AI images only).
1. Text, free, all videos: `ai_text_hits()` / `ai_by_text()` in
   `video_signals.py` -- AI-video words in title/tags (#ai, AI generated, AI
   animation, Sora/Veo/Kling..., "What if ..."), and in the description only
   phrases saying the PICTURES are AI ("AI visualizations", "video is
   AI-generated"...), never "AI voice", "AI thumbnail", a denial, or a news
   channel's generic #AI. Uploads before 2024-01-01 never count (all 17
   earlier hits were false). Computed when read, NOT stored (no schema
   change); `is_flood()` excludes these, so the flood file went 4,659 ->
   4,611 (48 out), the master keeps them, metadata counts `ai_by_text`
   (110 in the master). The Videos page badges them.
2. Model, per shot, in the scan: `AI_PROMPT` in `scan_shots.py`, ONE question
   per shot with its frames together, asked as "is this real camera
   footage?" (no = AI). Video is AI if >= `AI_VIDEO_SHARE` (0.5) of answered
   shots are; ledger keeps every shot's `ai` plus `ai_share` / `ai_video`.
   Owner's example images go in `scrape_data/ai_examples/` (gitignored,
   <= 8, shown before each shot's frames). Smoke test on YouTube's 3
   auto-frames, 20 videos their uploaders call AI vs 20 real pre-2023 flood
   videos: "is this AI-generated?" caught 6/20 AI (0 false); the "real
   footage?" wording 18/20 but 3/20 false -- all real phone video with blurred
   side copies or a tilted-photo slideshow; with a note on those, 16/20 AI
   (one miss looks real) and 1/20 false. Not yet used to drop anything
   from the data: the verdict lives in the ledger until the scan results are
   merged (with the `scenes` field). Owner's decision 2026-10-15: keep
   `ai_video: true` in the ledger for now; once the WHOLE scan is done, remove
   those videos (from the flood-only file; the master keeps every video) in
   one pass -- not video by video during the scan.

**Review page, 2026-10-15** (merged into `/videos.html` 2026-10-24): `data_view_server.py` :8768 `/shots.html` -- one
card per scanned video with its shots on a timeline (flood / not flood /
gap); the viewer lists every shot with the frames the model judged and its
answers, and plays any shot's range in the embed. (Per-shot decisions --
Flood / Not flood / Unsure in localStorage -- existed until 2026-10-24 and
were removed; the page is read-only.) It re-reads the ledger when it changes. For it, the scan now
SAVES every sampled frame (320 px JPEG) in
`scrape_data/youtube_shot_scan/frames/<video_id>/shot<k>_<j>.jpg` --
stills, not video; gitignored, never published; `--no-frames` turns it off.
The first 750 videos were scanned before this and have no frames (the page
shows timestamps there and the shot still plays).

**Owner's decision 2026-10-15: PySceneDetect removed entirely** (its content
detector was the last one kept). The experiment now runs transnetv2 and
autoshot, plus their derived union; `scenedetect` uninstalled from the env;
its rows stripped from both result pages (rebuilt from results.json). Every
cut the content detector found on the two test videos is also in the union,
except 89.0 / 91.9 s inside `xtBb2eU9d2k`'s subway montage (uncertain).

Downloader note: YouTube intermittently answers **HTTP 403** on the media
stream (its PO-token / SABR experiments; yt-dlp has no PO-token provider
here). Fresh stream URLs a few seconds later work, so `downloaded()` retries
the whole extraction up to 3 times on a 403 (seen on the second run of
`xtBb2eU9d2k`). If 403s become persistent, the fix is a PO-token provider
plugin for yt-dlp or browser cookies, not more retries.

### Known limits

- **One shot is not always one place.** A dashcam drive is one continuous
  shot through many streets. It becomes one long segment; splitting it by
  place would need GPS (often burned into dashcam frames -- OCR, not planned)
  or fixed time windows. Accept for now.
- **Grouping can merge different places that look alike** (two flooded
  residential streets) or split one place filmed from opposite directions.
  The pilot measures how often.
- **A flooded shot can be a close-up** (water at a doorstep). The plan asks
  only "flooding visible", per the owner; a street-level filter on shots is a
  possible later addition, one more question per keyframe.

### Cost estimate (to be replaced by pilot numbers)

2,145 flood videos (2026-10-08), median 80 s: perhaps 10-40k shots and
30-100k keyframes, i.e. a few GPU-hours of VLM time at ~0.5 s per frame, plus
30-60 GB of downloads at <= 720p.

### Pilot before the full run

50 flood videos, mixed (news packages, dashcam, phone clips, one
compilation). Measure: shots per video, flood shots per video, scenes per
video, how often grouping is visibly wrong (by watching the segments on the
review page), download failure rate. Then fix the thresholds and run the rest.

### Decisions still open (ask before building)

1. **Downloading** -- the owner's design is batch-download-process-delete
   (2026-10-14); still needs the explicit go-ahead to build, the batch size,
   the "short video" cut-off for the experiment, and the root rule rewrite.
2. **The `scenes` field** exactly as proposed above (it changes the video
   contract).
3. **Export rule** once segmented: flood-only file = videos with >= 1 flood
   scene? And does `flood_visual` (the 3-frame check) remain as the first
   filter for what gets downloaded?
4. ~~Long videos~~ -- skipped for the experiment (2026-10-14).
5. ~~Downloaded files after segmentation~~ -- deleted after each batch
   (2026-10-14). Consequence: a re-run with a changed threshold or prompt
   must download again; the shot/answer ledgers keep everything else.

### Files this would add (none exist yet)

- `scraping/video_scraping/youtube_download.py` -- step 1
- `verification/segment_video_scenes.py` -- steps 2-5 (judging lives in
  `verification/`, like `verify_video_frames.py`)
- ledgers in `scrape_data/`: `youtube_downloads.jsonl`,
  `youtube_shots.jsonl`, `youtube_shot_answers.jsonl`

## Operating it

```bash
crontab -l                                         # ONE weekly job, Sunday 03:30: run_weekly.sh
tail scrape_data/logs/youtube-weekly.log           # each step's start and exit code
tail -30 scrape_data/logs/youtube-cron.log         # what the last scrape did
tail -30 scrape_data/logs/youtube-frames-cron.log  # what the last visual check did
tail -30 scrape_data/logs/shot-scan.log            # what the last shot scan did
tail -30 scrape_data/logs/deploy-site.log          # step 4 (2026-10-26): new frames -> R2, site -> Vercel
scraping/video_scraping/run_weekly.sh              # the whole chain by hand
python3 scraping/video_scraping/youtube_scrape.py --plan        # searches + cost, no API calls
python3 scraping/video_scraping/youtube_scrape.py --no-search   # relabel / re-check only, ~60 units
```

**Viewing them:** `python3 data_view_server.py`, then the **Flood videos** tab
(`http://127.0.0.1:8768/videos.html`; `data_view_web/videos.html` +
`videos.js`). Since 2026-10-24 it shows only VALID flood videos: scanned,
at least one flood shot, not `ai_video: true` (`is_valid_flood()` in the
server; unscanned videos are not shown). Cards: frame, timeline, location
badge, title, upload date, duration, flood shots, deepest water (C5). The
detail view embeds YouTube's player (`youtube-nocookie.com`, the only frame
the site's CSP allows) above the shot list; each flood shot shows its
frames and its depth answer under C1-C5. Read-only (the review decisions
were removed 2026-10-24; the separate Shots and Depth pages were merged
into this one the same day). The server reads youtube_videos.json at
start-up -- restart it to see a newer scrape; the ledger and depth run are
re-read when they change. The player needs a referrer (YouTube error
153 without one), so the iframe sets its own `referrerPolicy` while the rest
of the site stays `no-referrer`.

A manual run while the weekly one holds the lock exits at once ("another run
holds ..."). Manual runs share the same 10,000-unit daily quota.

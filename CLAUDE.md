# vlm_flood

Honors thesis project. **The goal is visual geolocalization**: given a
street-level image, recover where it was taken. The dataset being built is
street-view imagery of flooded (and un-flooded) scenes -- SF-XL in spirit, but
flood-focused.

Coordinates are NOT required for the current phase: the near-term goal is
simply to collect images with street-view *geometry*. That is a deliberate
scope decision by the owner, with a known cost recorded under "Dataset
direction" below.

The existing pipeline (crawl news -> classify images) was built before this was
settled and is being re-evaluated against it. Do not assume the news corpus is
the intended final source.

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
scraping/         crawling only        scrape.py, adapters.py, run_crawl.sh, design.md
  api_based_scraping/   superseded GDELT approach: news_scrape.py + news_api_design.md
verification/     judging only         verify_text.py, verify_images_vlm.py, dedupe.py
                  country pass         locate_articles.py, country_codes.py, make_iso_table.py
local_vlm/        model mechanics      backend.py, download_model.py
image_review_web/       + image_review_server.py       human review site  :8765
image_vlm_review_web/   + image_vlm_review_server.py   model results site :8766
data/news_scrape_results.json   THE corpus — source of truth, what everything reads
scrape_data/      ALL intermediates: per-outlet .jsonl, the verifier's .jsonl,
                  crawl logs. Gitignored; recreated by makedirs.
```

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

# audit text scores / remove duplicate images
python3 verification/verify_text.py data/news_scrape_results.json
python3 verification/dedupe.py --dry-run
python3 verification/dedupe.py --apply            # --drop-near is UNSAFE, see below

# review sites
python3 image_review_server.py          # :8765 human labelling, reads the corpus
python3 image_vlm_review_server.py      # :8766 model results, reads final.json
```

Long crawls: `./scraping/run_crawl.sh` (tmux, survives disconnect).

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
                                      countries that flooded; joins on article_url
```

Supporting files: `data/image_hashes.json` (fingerprint cache, makes dedupe
re-runs instant), `/home/liu47/models/Qwen3.8-27B` (weights,
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
- **`scraping/api_based_scraping/`** is the superseded GDELT-discovery
  approach. Its design notes are in `news_api_design.md` (formerly a nested
  `CLAUDE.md`, renamed so it stops being read as agent instructions). The
  reasoning on why no news API returns inline images is still worth reading;
  the code is not in use.

## State (2026-09-11)

```
data/news_scrape_results.json      22M   the corpus
data/verified_images_news.json  15M   image labels
data/image_hashes.json           2.0M   fingerprint cache for dedupe
```

- Corpus: 5,892 articles, 8,679 images, 29 outlets. ~1,870 articles have no
  images and so appear in no verification output.
- The 29 per-outlet `*_flood.json` were deleted once the corpus
  was verified a field-for-field superset; they remain in git history.
- Classified: ~8,638 rows, ~66% `yes` under the street-level prompt.
- 1,624 rows (`cbs` 1,456, `fox` 110, `npr` 27, `ap` 23, `nbc` 6, `cnn` 2)
  still carry the OLD flood-footage prompt, where `yes` meant visible water.
  Mixing them with street-level rows blends two definitions.
- 12 images are permanently unfetchable (dead URLs); they stay pending.

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
~1,600-2,800 of 8,044. Run the strict prompt below over the existing corpus to
get the real number before scraping more news.

**Sources where the geometry is guaranteed, not filtered for:**

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

**Proposed prompt** (not yet applied; `PROMPT` in
`verification/verify_images_vlm.py:50` still selects any ground-level photo and
explicitly does not require flooding):

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
Applying it means clearing existing rows so they read as pending (resume is
prompt-agnostic), ~95 min of free GPU for all 8,044.

**The cost of skipping coordinates.** They cannot be retrofitted: a news photo
with no GPS will never become a geolocalization training example. Uncoordinated
flood street imagery is still useful as the target domain for domain adaptation
and as a qualitative query set, but geolocalization cannot be EVALUATED without
ground truth, so a coordinate-bearing set has to exist eventually even if it is
small and hand-labelled.

## Open work

1. Apply the strict street-view prompt above and re-score; measure real yield.
2. Probe dashcam/YouTube frame extraction and Mapillary coverage as sources.
3. Near-duplicate detection with a signal that works.
4. `:8766` site still says "Flood image results / Kept by model"; relabel.
5. Build a small hand-labelled ground-truth set (the :8765 site exports
   decisions) so model and prompt changes can be measured instead of guessed.

# vlm_flood

Dataset pipeline for VLM flood geolocalization: crawl flood news from 29
outlets, then label every image for whether it is a usable street-level
photograph. Honors thesis project.

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
local_vlm/        model mechanics      backend.py, download_model.py
image_review_web/       + image_review_server.py       human review site  :8765
image_vlm_review_web/   + image_vlm_review_server.py   model results site :8766
data/news_scrape_results.json   THE corpus — source of truth, what everything reads
data/outlets/     empty except for transient <outlet>_flood.jsonl DURING a crawl.
                  Recreated by makedirs; gitignored, so absent in a fresh clone.
```

`scraping/design.md` is the outlet bake-off record: which outlets were
tested, which were rejected and why, and postmortems of real bugs. Read it
before touching `adapters.py` or adding an outlet.

## Commands

```bash
# crawl
python3 scraping/scrape.py --outlets all --resume
python3 scraping/scrape.py --list-outlets

# classify images (local GPU, free, default backend)
python3 local_vlm/download_model.py                        # one time, 55.6 GB
python3 verification/verify_images_vlm.py --workers 8 --device-map cuda:0
python3 verification/verify_images_vlm.py --backend hf      # hosted, costs credits

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
    data/outlets/<outlet>_flood.jsonl     per-outlet, appended+flushed per article,
        |                                 merged and DELETED when the outlet finishes
    data/news_scrape_results.json         THE corpus: what --resume reads and
        |                                 what every downstream reader consumes
verify_images_vlm.py -->
    data/image_vlm_verification_final.json    one row per (article, image) with yes/no
        |
dedupe.py -->  same file, exact duplicates removed
```

Supporting files: `data/image_digests.json` (fingerprint cache, makes dedupe
re-runs instant), `data/logs/`, `/home/liu47/models/Qwen3.8-27B` (weights,
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
source of truth: `--resume` reads it, every outlet's finalize merges into it,
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

**`final.json` is the resume ledger.** `pending = occurrences not in this
file`. So deleting rows makes them pending again: a verifier run after
`dedupe.py` *restores the duplicates it removed*. Deduplication is only stable
if done at classify time (fetch -> hash -> seen? -> reuse answer, skip model).
Not yet implemented.

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
in `data/outlets/` is therefore **unmerged work, not garbage**: re-run that
outlet with `--resume` and it is folded in and cleaned up automatically. Never
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
data/image_vlm_verification_final.json  15M   image labels
data/image_digests.json           2.0M   fingerprint cache for dedupe
```

- Corpus: 5,892 articles, 8,679 images, 29 outlets. ~1,870 articles have no
  images and so appear in no verification output.
- The 29 per-outlet `data/outlets/*_flood.json` were deleted once the corpus
  was verified a field-for-field superset; they remain in git history.
- Classified: ~8,638 rows, ~66% `yes` under the street-level prompt.
- 1,624 rows (`cbs` 1,456, `fox` 110, `npr` 27, `ap` 23, `nbc` 6, `cnn` 2)
  still carry the OLD flood-footage prompt, where `yes` meant visible water.
  Mixing them with street-level rows blends two definitions.
- 12 images are permanently unfetchable (dead URLs); they stay pending.

## Open work

1. Content hash checked at classify time, so dedupe stops being undone.
2. Re-score the 1,624 old-prompt rows (~17 min GPU) for one consistent
   criterion — blocked for AP's 23 by the 403 above.
3. Near-duplicate detection with a signal that works.
4. `:8766` site still says "Flood image results / Kept by model"; `yes` now
   means street-level, not flooding. Relabel.

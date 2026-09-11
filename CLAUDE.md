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
`/home/liu47` is NFS and sits at ~97% used (hundreds of GB free, but check).

## Layout

```
agent_scraping/   crawling only        scrape.py, adapters.py, run_crawl.sh, design.md
verification/     judging only         verify_text.py, verify_images_vlm.py, dedupe.py
local_vlm/        model mechanics      backend.py, download_model.py
image_review_web/       + image_review_server.py       human review site  :8765
image_vlm_review_web/   + image_vlm_review_server.py   model results site :8766
data/outlets/     per-outlet crawl working files (never delete — source of truth)
data/news_scrape_results.json   combined corpus, what every reader consumes
```

`agent_scraping/design.md` is the outlet bake-off record: which outlets were
tested, which were rejected and why, and postmortems of real bugs. Read it
before touching `adapters.py` or adding an outlet.

## Commands

```bash
# crawl
python3 agent_scraping/scrape.py --outlets all --resume
python3 agent_scraping/scrape.py --list-outlets

# classify images (local GPU, free, default backend)
python3 local_vlm/download_model.py                        # one time, 55.6 GB
python3 agent_scraping/combine_outlets.py                  # rebuild corpus by hand
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

Long crawls: `./agent_scraping/run_crawl.sh` (tmux, survives disconnect).

## Data flow

```
scrape.py  --calls verify_text.score_record() INLINE, per article-->
    data/outlets/<outlet>_flood.json      crawl WORKING files, one per outlet
        |  combine_outlets.py, run automatically at the end of every crawl
    data/news_scrape_results.json         CANONICAL corpus, what everything reads
        |
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

**The corpus is derived; the per-outlet files are the source of truth.**
`scrape.py` writes `data/outlets/<outlet>_flood.json` (per-outlet resume,
atomic finalize, so a crash in one outlet cannot damage the other 28), then
merges them into `data/news_scrape_results.json`. Never hand-edit the combined
file -- the next crawl overwrites it. Rebuild it any time with
`python3 agent_scraping/combine_outlets.py`; `--no-combine` skips it.
Switching readers to the corpus did NOT change any `occurrence_id` (they come
from URLs, not file or position), so resume was unaffected -- verified.

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

**JSONL is transient.** Written per result during a run, merged into the JSON
and deleted at the end (`--keep-jsonl` retains it). The merge MUST merge, not
overwrite — under `--resume` the JSONL holds only this run's records. A JSONL
left on disk means a run died before finalizing.

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
- **`scraping_api/`** is the superseded GDELT-based approach and has its own
  `CLAUDE.md`. Ignore it; this file supersedes it.

## State (2026-09-11)

- Corpus: 5,892 articles, 8,679 images, 29 outlets. ~1,870 articles have no
  images and so appear in no verification output.
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

# VLM flood dataset

Collect flood news articles with captioned images from 29 news outlets, then
filter the images down to usable street-level photographs — by model and by eye.

```
scrape  ──►  data/outlets/*_flood.json  ──►  VLM verify  ──►  data/image_vlm_verification_final.json
                      │                                                      │
                      └──► review site :8765 (human)          review site :8766 (model) ◄──┘
```

```
agent_scraping/   crawl news outlets  -> data/outlets/*.json
verification/     verify_text.py (keyword scoring), verify_images_vlm.py
                  (model classification), dedupe.py (duplicate removal)
local_vlm/        model download + GPU inference, used by verify_images_vlm
```

Everything runs locally. Images are never saved to disk: the scrapers store
image URLs, the review sites load them in your browser straight from the
publisher, and the local classifier fetches them into memory for one forward
pass and discards them.

Activate the environment first — the base `python3` is missing
`huggingface_hub` and step 2 will fail without it:

```bash
conda activate /home/liu47/conda_envs/newEnv_local
```

## 1. Scrape

Crawls each outlet's flood section, extracts article text and captioned images,
and scores every article for flood relevance with a keyword filter
(`flood_score`, `flood_verified`). Records are scored, never dropped.

```bash
python3 agent_scraping/scrape.py --outlets all --resume
python3 agent_scraping/scrape.py --outlets guardian,cna --resume   # some outlets
python3 agent_scraping/scrape.py --list-outlets                    # 29 outlets + expected volume
```

Output: one `data/outlets/<outlet>_flood.json` per outlet. `--resume` skips URLs
already collected, so the crawl is safe to kill with Ctrl-C and re-run.

## 2. VLM image verification

Asks a vision model whether each scraped image is a usable street-level
photograph — roads, vehicles, people, buildings — rejecting maps, radar,
satellite views, charts, and headshots. Water is not required: the articles are
already flood-filtered in step 1, so `yes` means the image is usable for
geolocation, not that flooding is visible. Run it after a crawl; it is a
separate pass, not chained to the scraper.

Runs on this machine's GPU by default — free, so prompt experiments cost
nothing. Fetch the weights once (~56 GB, into `/home/liu47/models/`):

```bash
python3 local_vlm/download_model.py                     # one time
python3 verification/verify_images_vlm.py --limit 20  # small test run first
python3 verification/verify_images_vlm.py --workers 4
python3 verification/verify_images_vlm.py --device-map cuda:0   # pin one GPU
```

All model loading and inference lives in `local_vlm/`; the verifier only
orchestrates. To use the hosted API instead (costs HF credits), pass
`--backend hf`; the token is read from `HF_TOKEN`, else a `.env` in the project
root (`cp .env.example .env`), else an interactive prompt.

Output: `data/image_vlm_verification_final.json` — one `yes`/`no` per image.
Re-running resumes from it and never re-pays for an image already classified,
so interruptions and credit limits are safe. Model: `Qwen/Qwen3.8-27B:novita`.

## 3. Review sites

Two separate local sites, each on its own port. Both can run at once.

```bash
python3 image_review_server.py          # http://127.0.0.1:8765  — human review
python3 image_vlm_review_server.py      # http://127.0.0.1:8766  — model results
```

**:8765 — human review.** Every scraped image, filterable by outlet, flood
score, and hints for maps / file photos / duplicates. Label images useful /
reject / unsure (keys `1` `2` `3`, arrows to navigate); *Export decisions*
downloads them as JSON. Decisions are saved in your browser only — they are not
written to the repo, so don't clear site data for `127.0.0.1:8765`.

**:8766 — model results.** Read-only view of what the VLM decided, filterable
by yes / no, with the raw model response per image. Shows only images that have
been verified, so run step 2 first.

## Data

| path | what |
|---|---|
| `data/outlets/<outlet>_flood.json` | articles: title, date, url, text, images, videos, flood score |
| `data/image_vlm_verification_final.json` | one VLM `yes`/`no` per image; resumable store of record |
| `data/logs/` | crawl and verification logs |
| `data/image_digests.json` | cached image fingerprints for deduplication |
| `/home/liu47/models/Qwen3.8-27B` | local model weights (~56 GB, outside the repo) |

Intermediate `.jsonl` files appear next to both outputs while a run is in
progress and are merged in and deleted when it finishes (`--keep-jsonl` to
retain them).

# VLM flood dataset

Collect flood news articles with captioned images from 29 news outlets, then
filter the images down to actual flooding footage — by model and by eye.

```
scrape  ──►  data/outlets/*_flood.json  ──►  VLM verify  ──►  data/image_vlm_verification_final.json
                      │                                                      │
                      └──► review site :8765 (human)          review site :8766 (model) ◄──┘
```

Everything runs locally. No image is ever downloaded: the scrapers store image
URLs, and the review sites load them in your browser straight from the
publisher.

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

Asks a vision model whether each scraped image actually shows flooding. Run it
after a crawl; it is a separate pass, not chained to the scraper.

The token is read from `HF_TOKEN`, else a `.env` in the project root, else an
interactive prompt — first one found wins:

```bash
cp .env.example .env && $EDITOR .env      # HF_TOKEN=hf_...   (.env is gitignored)

python3 agent_scraping/verify_images_vlm.py --limit 20  # small test run first
python3 agent_scraping/verify_images_vlm.py --workers 4
```

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
| `data/logs/` | crawl logs |

Intermediate `.jsonl` files appear next to both outputs while a run is in
progress and are merged in and deleted when it finishes (`--keep-jsonl` to
retain them).

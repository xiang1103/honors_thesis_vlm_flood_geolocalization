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


## 1. Scrape

Crawls each outlet's flood section, extracts article text and captioned images,
and scores every article for flood relevance with a keyword filter
(`flood_score`, `flood_verified`). 

```bash
python3 agent_scraping/scrape.py --outlets all --resume
python3 agent_scraping/scrape.py --outlets guardian,cna --resume   # some outlets
python3 agent_scraping/scrape.py --list-outlets                    # 29 outlets + expected volume
```


## 2. VLM image verification

```bash
python3 local_vlm/download_model.py                     # one time
python3 verification/verify_images_vlm.py --limit 20  # small test run first
python3 verification/verify_images_vlm.py --workers 4
python3 verification/verify_images_vlm.py --device-map cuda:0   # pin one GPU
```

## 3. Review sites

Two separate local sites, each on its own port. Both can run at once.

```bash
python3 image_review_server.py          # http://127.0.0.1:8765  — human review
python3 image_vlm_review_server.py      # http://127.0.0.1:8766  — model results
```

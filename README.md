# Program Flow 

Collect flood news articles with captioned images from 29 news outlets, then
filter the images down to usable street-level photographs — by model and by eye.

```
scrape ──► data/news_scrape_results.json ──► VLM verify ──► data/image_vlm_verification_final.json
                    (the corpus)                                          │
                          └──► review site :8765 (human)   :8766 (model) ◄┘
```

```
scraping/   crawl news outlets  -> data/news_scrape_results.json 

verification/     verify_text.py (keyword scoring) to filter out non-flood relevant reports, verify_images_vlm.py to check for non-relevant images, dedupe.py to check duplicate images  

local_vlm/        local vlm model for running image checks 
``` 

```
image_vlm_verification_final.json: all scraped with images and model-decisions on whether the image is relevant 
```


## 1. Scrape 
Scraping process: 
```
load all collected news (news_scrape_results.json) ──► write new scrpapes to per_outlet.jsonl ──► merge these jsonl into the main corpus
```

```bash
python3 scraping/scrape.py --outlets all --resume
python3 scraping/scrape.py --outlets guardian,cna --resume   # some outlets
python3 scraping/scrape.py --list-outlets                    # 29 outlets + expected volume
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


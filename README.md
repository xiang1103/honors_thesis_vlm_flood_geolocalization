# Program Flow 

Collect flood news articles with captioned images from 29 news outlets, then
filter the images down to usable street-level photographs — by model and by eye.

```
scrape ──► data/news_scrape_results.json ──► VLM verify ──► data/verified_images_news.json
                    (the corpus)                                          │
                          └──► review site :8765 (human)   :8766 (model) ◄┘
```

```
scraping/   crawl news outlets  -> data/news_scrape_results.json 

verification/     verify_text.py (keyword scoring) to filter out non-flood relevant reports, verify_images_vlm.py to check for non-relevant images, dedupe.py to check duplicate images  

local_vlm/        local vlm model for running image checks 
``` 

```
verified_images_news.json: all scraped news that passed the filter and model-decisions on whether the image is relevant, duplicate and dead urls are removed.  
news_scrape_results.json: all news and images collected so far   
meta_data.json: track meta data information of verified news/images 
```


## 1. Scrape 
Scraping process: 
```
1. discover()           walk the outlet's index/tag pages → candidate URLs
  2. filter against seen articles and image urls
  3. fetch each survivor   parse text + extract images (deduped by URL within the article)
  4. score inline          verify_text.score_record() → flood_score, flood_verified
  5. window filters        --since-days, --min-images
  6. append → scrape_data/<outlet>_flood.jsonl    (flushed per article)
  7. finalize()            merge JSONL → corpus, atomic write, delete JSONL
```

```bash
python3 scraping/scrape.py --outlets all
python3 scraping/scrape.py --outlets guardian,cna            # some outlets
python3 scraping/scrape.py --list-outlets                    # 29 outlets + expected volume
``` 

```make_metadata.py``` regenerates meta_data.json file, but the file counts are dynamically updated during scraping and verification


## 2. VLM image verification
Steps: 
```
1. enumerate    images from flood_verified articles only          
  2. load         already processed images           
  3. per URL      fetch → hash → check for duplicate image hashes, skip GPU, else → run the model    
  4. append       each result to scrape_data/…jsonl, flushed per record
  5. merge        existing ∪ this run's JSONL → atomic write
  6. delete       the JSONL                         
  7. refresh     meta_data.json 
``` 

```bash
python3 local_vlm/download_model.py                     # one time
python3 verification/verify_images_vlm.py --limit 20  # small test run first
python3 verification/verify_images_vlm.py --workers 4
python3 verification/verify_images_vlm.py --device-map cuda:0   # pin one GPU
``` 
Alternative VLM model: gemma-4-31B-it 

## 3. Review sites

Two separate local sites, each on its own port. Both can run at once.

```bash
python3 image_review_server.py          # http://127.0.0.1:8765  — human review
python3 image_vlm_review_server.py      # http://127.0.0.1:8766  — model results
```

## Good sites 
or "street-view flooded, crowdsourced, with coordinates," Mapillary already is the social platform 
  you're describing. It's user-contributed street-level imagery whose entire purpose is carrying GPS  
  and heading — and it has capture timestamps, so you can pull the same coordinates before and during 
  a flood event. No other crowdsourced source gives you that pairing.                                 
                                                                                                      
  Also worth checking before building anything: CrisisMMD and MEDIC (disaster social-media images with
  labels, no coords), FloodNet (UAV imagery of post-Harvey flooding, georeferenced but aerial), and   
  Copernicus EMS (flood extents, satellite). None are street-level VPR datasets, but they'll tell you 
  what's been tried.


## Limitations 
- Near identical images (ex: cropped) are not detected by hashing 
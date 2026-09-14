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
verified_images_news.json: all scraped news with images and model-decisions on whether the image is relevant, duplicate and dead urls are removed.  
news_scrape_results.json: all news and images collected so far   
meta_data.json: track meta data information of verified news/images 
```


## 1. Scrape 
Scraping process: 
```
load all collected news (news_scrape_results.json) ──► write new scrpapes to per_outlet.jsonl ──► merge these jsonl into the main corpus
```

```bash
python3 scraping/scrape.py --outlets all
python3 scraping/scrape.py --outlets guardian,cna            # some outlets
python3 scraping/scrape.py --list-outlets                    # 29 outlets + expected volume
``` 

```make_metadata.py``` regenerates meta_data.json file, but the file counts are dynamically updated during scraping and verification


## 2. VLM image verification

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
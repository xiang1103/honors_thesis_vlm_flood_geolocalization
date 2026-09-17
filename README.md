# Program Flow 

Collect flood news articles with captioned images from 29 news outlets, then
filter the images down to usable street-level photographs — by model and by eye.

```
scrape ──► data/news_scrape_results.json ──► Text matching to find flood news + VLM to verify which images are good ──► data/verified_images_news.json
                    (the corpus)                                          │
                                       filter_nyc.py ──► nyc_scraped_images.json
                                                                          │
                          └──────────────► review site :8765 ◄────────────┘
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

## 3. Review site

One local site. It was two -- human review on :8765 and model results on :8766 --
which read the same file and showed the same images; they are merged.

```bash
python3 image_review_server.py          # http://127.0.0.1:8765
```

Each card carries the model's YES/NO verdict, the New York label, and your own
decision. Filters: model answer, search, outlet, New York, your review status,
article flood-check, and sort. Click an image for the full-size viewer, where
1/2/3 record Useful/Reject/Unsure and the arrow keys move through the matches.
"Export decisions" writes them to JSON.

All rows load; the page opens on the model's `yes` images, which is the dataset
itself. `--answer no` or `--answer all` opens elsewhere. New York labels come
from `data/nyc_scraped_images.json` when it exists (`--nyc-file` to point
elsewhere); without it the site runs with that one filter disabled.

Review decisions live only in this browser's localStorage. There is no
server-side copy -- export them before clearing site data.

## 4. GIS flood photos (New York State)

A second source alongside the news crawl: public APIs whose records carry
image URLs, text, and (mostly) coordinates.

```
gis_scrape.py ──► data/gis_flood_images.json ──► GIS review site :8768
```

| source | what | coordinates |
|---|---|---|
| MyCoast | citizen "Flood Watch" / "Storm Reporter" reports (ArcGIS) | all |
| USGS STN | high-water-mark and sensor photos from 10 NY flood events | all |
| Wikimedia Commons | NY flood / Sandy / Ida categories; the only redistributable pixels | ~20% |
| NAPSG PhotoMappers | crowdsourced tropical-cyclone photos (ArcGIS) | all |

Every record is inside New York State (Census boundary); `in_nyc` labels the
five boroughs (NYC Planning boundary, water included). No date filter -- Sandy
(2012) is about a quarter of the images.

```bash
python3 scraping/api_based_scraping/gis_scrape.py                       # all sources, ~8 min (Commons is rate-limited)
python3 scraping/api_based_scraping/gis_scrape.py --sources mycoast,stn # a subset; other sources' rows are kept
```

### GIS review site

```bash
python3 gis_review_server.py                     # http://127.0.0.1:8768
python3 gis_review_server.py --port 8770         # if 8768 is taken
```

To keep it running after you disconnect, start it in tmux:

```bash
tmux new -d -s gis_review 'cd /home/liu47/vlm_flood && /home/liu47/conda_envs/newEnv_local/bin/python gis_review_server.py'
tmux attach -t gis_review        # see the log; Ctrl+B then D to detach
tmux kill-session -t gis_review  # stop it
```

It listens on localhost only; from another machine, tunnel first:
`ssh -L 8768:127.0.0.1:8768 <server>` and open http://127.0.0.1:8768.

Same decisions and keys as the news site (1/2/3, arrow keys, "Export
decisions"). Filters: source, event/type, location (NYC / rest of NY /
unknown), your review status, sort. The viewer links to the source page, the
original image, OpenStreetMap, and Google Street View at the photo's
coordinates. The server reads `data/gis_flood_images.json` once at start --
restart it after re-running `gis_scrape.py`. Decisions are stored in this
browser only, separately from the news site's.

## Good sites 
or "street-view flooded, crowdsourced, with coordinates," Mapillary already is the social platform 
  you're describing. It's user-contributed street-level imagery whose entire purpose is carrying GPS  
  and heading — and it has capture timestamps, so you can pull the same coordinates before and during 
  a flood event. No other crowdsourced source gives you that pairing.                                 
                                                                                                      
  Also worth checking before building anything: CrisisMMD and MEDIC (disaster social-media images with
  labels, no coords), FloodNet (UAV imagery of post-Harvey flooding, georeferenced but aerial), and   
  Copernicus EMS (flood extents, satellite). None are street-level VPR datasets, but they'll tell you 
  what's been tried.


## Future Designs 

**Get Street Images:** 
- if we can find out reports of where flood has happened, use Google Maps API to find the street-view image for that time 


## Limitations 
- Near identical images (ex: cropped) are not detected by hashing 
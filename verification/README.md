# verification

Everything that judges what the crawler collected. Nothing here fetches
articles; nothing in `agent_scraping/` judges them.

```
verify_text.py         keyword flood scoring of ARTICLE TEXT (zero-token)
verify_images_vlm.py   model classification of IMAGES (street-level yes/no)
dedupe.py              duplicate-image removal by content hash
README_VLM_VERIFICATION.md   detailed guide to the image verifier
```

## Order

```
scrape.py ──calls verify_text.score_record() inline, per article
          └─→ data/outlets/*_flood.json        (flood_score, flood_verified)
                      │
verify_images_vlm.py ─┴─→ data/image_vlm_verification_final.json   (yes/no per image)
                      │
dedupe.py ────────────┘   one-time cleanup of that file
```

`verify_text.py` is the exception to "run it yourself": the crawl imports
`score_record()` and calls it per article, so text scoring is not a separate
pass. It also works standalone as an audit CLI over either file shape:

```bash
python3 verification/verify_text.py data/outlets/cbs_flood.json
```

## Nothing here drops records

Both verifiers **label**; neither filters. `flood_score`/`flood_verified` and
the image `answer` are recorded on every record, `no` included, so thresholds
stay tunable without re-crawling or re-classifying. Selecting the subset you
want is the caller's job.

`dedupe.py` is the one exception -- it does delete rows -- which is why exact
(sha256) duplicates are dropped and near (dHash) duplicates only reported
unless `--drop-near` is passed.

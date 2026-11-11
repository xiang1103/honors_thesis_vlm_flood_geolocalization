# scene_split/ -- shots, flood shots, scene labels

Splits flood videos into shots, samples frames, and labels each shot. The
owner's decisions on the scan (shot detectors, union rule, frame sampling, AI
check, speed, YouTube blocks, proxy) are recorded in
`scraping/video_scraping/CLAUDE.md` ("PLAN: flood scenes", "Nightly from cron",
"PO-token provider", "Proxy fallback") -- read that before changing the scan.

| file | what |
|---|---|
| `scan_shots.py` | the scan: download (temp) -> TransNetV2 + AutoShot shots (union) -> 3 frames per shot (10/50/90%) -> VLM "flood?" per frame (>= 2 of 3 = flood shot) -> AI check per shot -> ledger + frames. `--probe` tests downloading |
| `run_scan.sh` | ROUTINE launcher: one slow worker; on a YouTube block waits `BLOCK_WAIT_S` and retries (`BLOCK_WAIT_S=0` = stop, the weekly cron) |
| `methods.py` | shot-boundary methods (transnetv2, autoshot) and `combine` |
| `compare_shot_detectors.py` | one-video side-by-side of detectors (the 2026-10-14 experiment) |
| `scene_filter.py` | VLM label per shot: A direct view, B studio, C multi-picture, D filmed screen, E through glass, F graphic |

Outputs:
- `scrape_data/youtube_shot_scan/shard_<i>of<n>.jsonl` -- the ledger, one line
  per video; one file per worker (NFS: never several writers on one file).
  Resume reads all of them. It is the record of the scan, not unmerged work.
- `scrape_data/youtube_shot_scan/frames/<video>/shot<k>_<j>.jpg` -- the
  sampled frames, 320 px wide (the only pixels kept; every depth experiment
  reads these). Shot ids are `<video>:shot<NNN>`, 1-based, everywhere.
- `scrape_data/experiments/flood_depth/scene_filter/labels_*.jsonl` --
  scene labels, keyed `(id, prompt_id)`.

## Scene filter

Why: MoGe-2 (and the VLM) assume one real scene through one lens; studio
screens, picture-in-picture and through-glass views break its scale (objects
measured 8-25% of true size). `SKIP` = B-F; `skip_ids()` is what
`geometry_depth.py`, `calibrate_dry.py` and `evaluate.py --scene real` use.
Unlabelled shots are kept. Known gap: some "direct" shots still break MoGe's
scale (79 wheels under 5 m measured ~0.09); a geometric second check is
proposed, not built.

## YouTube

Downloads were blocked by IP on 2026-10-24 and worked again from 2026-10-25;
the 2026-11-01 and 11-08 cron scans hit no block. It can recur (same IP).
`scan_shots.py` exits 3 on a block (bot check, or `STREAM_403_BLOCK` stream
403s in a row) so the GPU is freed; never "fix" a block by adding workers --
four workers is what tripped the bot check (2026-10-15).

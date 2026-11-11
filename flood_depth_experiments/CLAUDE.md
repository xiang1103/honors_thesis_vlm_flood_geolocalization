# flood_depth_experiments/ -- water depth from flood video

Experiments on how deep the water is in flood shots from YouTube. Read the root
`CLAUDE.md` first (git, env, video-bytes rule); each subfolder has its own
`CLAUDE.md`.

```
scene_split/      shot scan: download -> shots -> 3 frames each -> "flood?" per shot (VLM)
                  + scene_filter.py: direct view vs studio / screen / through-glass labels
flood_depth/      VLM depth benchmark: the same question under inputs C1-C5     PLAN.md
depth_detection/  geometry depth: MoGe-2 + SAM 3, routes A / B / V
                  design + results: docs/water_depth_segmentation.md
```

Flow: `scene_split/scan_shots.py` writes the shot ledger and the frames
(`scrape_data/youtube_shot_scan/`) -> `flood_depth/` asks the VLM for depth on
every eligible flood shot (`full_5cm/`, shown on the site's Videos page) ->
`depth_detection/` measures depth from geometry on a 50-shot pilot and puts it
next to the VLM's answers (`geometry/report.html`, local only).

## Rules that apply to all three (owner's decisions)

- **Outputs only under `scrape_data/experiments/flood_depth/`** (and the scan's
  `scrape_data/youtube_shot_scan/`). Nothing in `data/`.
- **Depths in centimetres; no human labels planned** (2026-10-21). Do not build
  cm -> level mappings or labelling work unless asked.
- **Show, don't judge** (2026-11-04): nothing here is checked against ground
  truth. Put every method's answer side by side for experts; do not call an
  answer right or wrong in docs or pages.
- **Labels, not filters.** The scene filter, the flood check and the AI check
  label shots; readers skip by rule (`scene_filter.skip_ids()`), nothing is
  deleted.
- **Video bytes are transient** (root `CLAUDE.md`): downloaded one at a time to
  a temp dir, deleted after; only frames, timestamps and labels are kept.
- **Resume ledgers are append + flush JSONL**, keyed by a config / prompt id;
  changing a prompt or config is new work, old rows stay. Final JSON files are
  written atomically (`.tmp` + fsync + `os.replace`).
- **Local VLM: thinking off** (`Qwen3.8-27B`, `/home/liu47/models/`). It takes
  ~15 min to load over NFS; ~55 GB per GPU. Pick GPUs at run time
  (`verify_video_frames.pick_free_gpu`); GPUs 4 / 5 are often taken by others.
- Never `git commit` / `git push`.

## Weekly cron

`scraping/video_scraping/run_weekly.sh` runs the shot scan (`scene_split/run_scan.sh`,
step 3) and the depth catch-up (`flood_depth/run_new.sh`, step 4) every Sunday.
Cron has no `$USER` and `PATH=/usr/bin:/bin` -- test any change to these
scripts under `env -i HOME=$HOME PATH=/usr/bin:/bin` (both broke that way,
fixed 2026-11-10). `depth_detection/` is not in cron.

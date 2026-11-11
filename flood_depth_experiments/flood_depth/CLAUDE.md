# flood_depth/ -- the VLM water-depth benchmark (C1-C5)

**Read `PLAN.md` first**: goal, unit (one shot), conditions, metrics, full
runs, next steps. This file is the short version plus the gotchas.

The local VLM is asked one question -- how deep is the water, as a cm interval
at most 5 cm wide, or X -- under five input conditions:

| | input |
|---|---|
| C1 | one frame (asked of all 3 frames; the key frame j = 1 is the score, the others give within-shot spread) |
| C2 | all 3 frames together |
| C3 | key frame + uploader title / tags / description |
| C4 | key frame + a table of typical object heights (`levels.REFERENCE_HEIGHTS`) |
| C5 | C4 + detected reference objects drawn as numbered boxes (`detect_refs.py`, Grounding DINO) |

| file | what |
|---|---|
| `conditions.py` | the prompts; every condition asks the same `QUESTION` |
| `levels.py` | answer parsing (`parse_depth_range`, `midpoint_cm`, `within_5cm`), legacy L0-L5 levels |
| `sample_items.py` | eligible items: flood shot, not AI video, 3 frames on disk |
| `detect_refs.py` | C5 boxes, cached per detector config. **Grounding DINO is still used here** -- it was removed only from `depth_detection/segment.py` |
| `run_conditions.py` | pilot runner, full replies kept, prompt-aware resume |
| `run_all.py` | full runner over every eligible shot, one compact line per shot |
| `run_all.sh` | detached launcher, one shard per GPU given |
| `run_new.sh` | weekly cron step 4: only shots not yet answered, GPUs picked at run time, foreground |
| `make_video_labels.py` | per-video labels for the site (`video_labels.json`) |
| `evaluate.py` | label-free scoring (+ labels if a `labels.jsonl` ever exists); `--scene real` skips scene-filtered shots |
| `depth_review.py` | data behind the site's depth answers |
| `splits.py` | train/val/test hook by VIDEO, unused |

Outputs, `scrape_data/experiments/flood_depth/`:
- `full_5cm/` -- **the current full run** (cm prompt): `answers_<i>of<n>.jsonl`,
  `refs_*`, `long_*`, `run.json`, `video_labels.json` (read by
  `data_view_server.py` /videos.html and the deployed site).
- `full/` -- the earlier L0-L5 run, kept. `items.jsonl`, `predictions.jsonl`,
  `results.json` -- the 300-item pilot.

## Gotchas

- `run.json` pins the prompt ids: a run with different prompts refuses to
  write into the folder. New prompt = new folder.
- Resume reads every shard's answers file, so the shard count may change
  between runs.
- An answer counts only if the reply IS an interval or X; a number inside a
  sentence is not read. Wider-than-5 cm answers are kept (owner, 2026-10-21).
- C1 varies by up to ~60 cm across one shot's frames; answers cluster on a few
  values, two of which match the C4/C5 height table (100-105, 120-125 cm).
- `run_new.sh` runs under cron: no `$USER`, bare PATH (it died on `$USER`
  2026-11-01 and 11-08; fixed 2026-11-10 with `$(id -un)`). After a run, check
  its exit line in `scrape_data/logs/youtube-weekly.log`; log
  `scrape_data/logs/flood-depth-weekly.log`.
- GPUs are picked by free memory at start, but loading takes ~15 min and other
  users' jobs can fill the GPU meanwhile: on 2026-11-10 shard 1/3 died with CUDA
  OOM on GPU 1. Its shots are simply left unanswered (no retry); the next
  `run_new.sh` picks them up, since resume reads every shard's answers.
- Depths stay in cm; no human labels or labelling page are planned (owner).

# Flood water depth from street-level video — benchmark plan

Started 2026-10-19. Code: `flood_depth_experiments/flood_depth/`. Outputs:
`scrape_data/experiments/flood_depth/` (gitignored, like `scene_split`). Nothing
here writes to `data/`; promoting results into the published data needs the
owner's decisions on schema (root `CLAUDE.md` contracts).

## Goal

Measure how well a VLM can estimate **flood water depth** in street-level flood
footage, and **what input helps**. The experiment varies the model's input
(conditions C1–C5) while the label stays fixed. Later work adds non-VLM
baselines, a measured-depth subset, and train/test splits; the code is
written so those slot in without changes to what exists.

## Why references, not geometry

Depth of water cannot be read off scene geometry: the surface reflects and
hides the ground beneath it, so monocular depth models (Depth Anything V2,
Metric3D, UniDepth, Depth Pro) measure distance to the camera, not water
depth. In one picture the usable cue is a **reference object of known height
that is partly under water**: a person (ankle, knee, waist), a vehicle (tyre,
hub, door sill, bonnet), a curb, a hydrant, a step. Prior work does exactly
this (Chaudhary et al. 2019/2020, body parts and vehicles as rulers;
MediaEval 2018/2019 passability and "water above the knee" tasks). Conditions
C4 and C5 test whether telling the model those references helps.

## Unit: one shot

An **item** is one flood **shot** (a continuous camera take) from the shot
scan (`flood_depth_experiments/scene_split/scan_shots.py`), id `<video_id>:shot<NNN>`.
Depth does not change inside one take, so a shot has ONE label and every
condition is graded against it. A frame-level label would make conditions
incomparable (C1 and C2 would be graded on different things).

Each shot has 3 saved frames (10 / 50 / 90 % of the shot, 320 px wide; files
`scrape_data/youtube_shot_scan/frames/<video_id>/shot<NNN>_<j>.jpg`). The
**key frame** is the middle one (j = 1); C1/C3/C4/C5 use it.

Eligible (`sample_items.py`): the scan called the shot flood (v2 street-level
prompt), the video is not `ai_video`, the shot has 3 frames (>= 1 s), and the
frames are on disk. As of 2026-10-19: **9,910 shots in 1,928 videos**.
Selection caps shots per video (default 3) so long news packages do not
dominate; deterministic by seed; the chosen list is written to `items.jsonl`
and both labels and predictions refer to it by item id.

Pilot: `items.jsonl`, 300 items from 283 videos (seed 0, per-video 3).
Smoke test (5 items x C1-C5, 2026-10-19): ~2-2.5 s per question on one GPU,
so the pilot (300 items, 2,100 questions with C1 on every frame) is ~1.5 h.

Known limitation: frames are 320 px. Curbs and wheel hubs are hard to judge at
that size. Re-extracting key frames at native resolution means downloading
again (transient, per the owner's video rule) and is blocked while YouTube's
bot check holds. `conditions.IMAGE_LONG_SIDE` upscales every condition's
frames to the same 640 px so resolution is not a difference between conditions.

## Label scheme (shared by humans and models — `levels.py`)

Ordinal levels, plus X:

| level | depth | person (guide only) | car (guide only) |
|---|---|---|---|
| L0 | no standing water (dry, wet from rain, puddles only) | — | — |
| L1 | < 15 cm | below ankle | tread only, below curb top |
| L2 | 15–50 cm | up to knee | up to wheel hub / bumper |
| L3 | 50–100 cm | knee to waist | tyre covered, up to bonnet |
| L4 | 100–150 cm | waist to chest | bonnet to windows |
| L5 | > 150 cm | over head | roof, ground-floor windows |
| X | cannot be judged from what is visible | | |

**What is measured:** the height of the water surface above the ground
beneath it, **at the deepest place in the scene where it can be judged**.
Humans use the whole shot (all frames); the label also records which frame(s)
show the evidence.

The person/car columns are for the **human labelling guide**. The model's base
question lists only centimetres (so C4's height table is a real difference).

### Label file — `scrape_data/experiments/flood_depth/labels.jsonl`

One line per (item, annotator, time); the latest line per (item, annotator)
counts:

```json
{"item_id": "-2wHuNoueGo:shot009", "level": "L2", "annotator": "liu47",
 "labelled_at": "2026-10-20T14:03:00+00:00",
 "evidence_frames": [1, 2], "reference": ["person"], "notes": ""}
```

`level` and `annotator` are required; `evidence_frames`, `reference` (person /
car / truck / bus / curb / building / other) and an optional `waterline`
click (`{"frame": j, "x": 0-1, "y": 0-1}`) feed later grounded tasks and
the oracle condition C6. Several annotators: `evaluate.py` uses the majority,
else the lower median of their levels, else X; `--annotator` picks one.

**Labelling must be blind** to the model's answers. A human-ceiling set of
~300 items is labelled independently by two people.

The labelling page (on :8768, server-side storage, keys 0–5 / X) is the next
thing to build; it is NOT built yet.

## Conditions (`conditions.py`)

Every condition asks the same question and accepts the same answers: an exact
integer-centimeter interval no wider than 5 cm (for example `35-40 cm`), or
`X` if depth cannot be judged. Thinking is off by default (the
repo's rule: see root `CLAUDE.md`, "Local model: thinking stays OFF");
`--thinking` runs a separate, recorded variant.

| | input | what it isolates |
|---|---|---|
| C1 | key frame | baseline. Also asked of every frame (`--c1-frames all`) for the within-shot consistency metric |
| C2 | all 3 frames of the shot, in order | temporal context (a reference may appear in only one frame) |
| C3 | key frame + uploader's title, tags, description (≤ 1,500 chars) | text priors ("waist-deep") — report separately, it is partly the uploader's answer |
| C4 | key frame + typical heights table (`levels.REFERENCE_HEIGHTS`) | explicit scale knowledge |
| C5 | C4 + detector boxes drawn and listed | automatic grounding. C5 − C4 = what the detector adds |
| C6 *(not built)* | C4 + the HUMAN-marked reference / waterline | **oracle** — upper bound if grounding were perfect; C6 − C5 = detector error |

C5 detector (`detect_refs.py`): Grounding DINO base
(`/home/liu47/models/grounding-dino-base`, Apache-2.0), open-vocabulary query
of objects with known heights (person, car, truck, bus, bicycle, motorcycle,
fire hydrant, stop sign, parking meter, mailbox, traffic cone, trash can,
door, stairs, curb), box ≥ 0.35, text ≥ 0.25, ≤ 8 boxes, same-label overlap
≥ 0.7 merged. Cached per detector config in `refs.jsonl`. On the pilot:
200 of 290 items got ≥ 1 box; cars 320, people 126, trucks 24 — curbs and
hydrants are almost never found at 320 px.

Prompt identity: `prompt_id` = hash of a condition's fixed text. The
predictions ledger keys on (item, condition, frames, prompt_id, model,
thinking), so an edited prompt is new work and old answers stay comparable.
`prompts.json` keeps each id's text.

## Metrics (`evaluate.py`)

The original pilot labels use the broad ordinal `L0`–`L5` scheme above and
cannot score centimeter intervals. Numeric estimates need numeric human
measurements/ranges before accuracy metrics can be reported. Label-free
condition comparison reports interval overlap and midpoint distance.

With labels (items with a level):
- **QWK** (quadratic-weighted kappa), primary; MAE in levels; exact and
  within-1 accuracy; macro-F1.
- **Coverage**: share of gold-level items where the model gave a level (not X).
- **X on gold X**: when humans say "can't tell", does the model?
- **Human ceiling**: inter-annotator QWK on doubly-labelled items.

Label-free (available now):
- answer distribution, X rate, unparsed replies;
- **C1 within-shot consistency**: flip rate and level range across the
  shot's 3 frames — depth cannot change within a take;
- agreement between conditions (QWK).

`results.json` holds everything; the console prints a compact table.

## Splits (`splits.py`) — hook only

By **video**, never by frame. Schemes: `all` (default now), `video_hash`
(deterministic, ratios configurable), `file:<path>` for hand-made event- or
channel-held-out splits (channel is no longer stored; re-fetch first).
`evaluate.py --split-scheme video_hash --split test`.

## Full runs — every eligible shot (`run_all.py`)

The original coarse-label C1–C5 run completed on 2026-10-20 over all 9,910
eligible shots. Its compact output remains in
`scrape_data/experiments/flood_depth/full/`; logs are
`scrape_data/logs/flood-depth-full-shard<i>of3.log`.

The revised numeric prompt writes to
`scrape_data/experiments/flood_depth/full_5cm/` and logs to
`scrape_data/logs/flood-depth-5cm-shard<i>of<n>.log`. Launch with
`run_all.sh 1 5 7` (started 2026-10-21 on GPUs 1, 5, 7). This is a separate run because the prompt IDs changed; the
old predictions are preserved.

- `run.json` — written once: model, thinking, prompt ids, detector id, image
  size. A run with different prompts refuses to write into the folder.
- `answers_<i>of<n>.jsonl` — one line per shot:
  `{"id":"<video>:shot009","C1":["35-40 cm","40-45 cm","40-45 cm"],"C2":"35-40 cm","C3":"35-40 cm","C4":"35-40 cm","C5":"35-40 cm"}`
  (C1 = frames 0, 1, 2; the key frame is index 1; `null` = unparsed).
- `refs_<i>of<n>.jsonl` — C5 boxes, `{"id", "b": [[label, score, x0, y0, x1, y1], ...]}`.
- `long_<i>of<n>.jsonl` — replies that were not just an answer: re-asked
  with 512 tokens; an answer is accepted only from the reply's last line. The
  only raw replies kept.
- `video_labels.json` — **per-video labels for display** (owner, 2026-10-21),
  rebuilt by `make_video_labels.py` when each shard finishes (or by hand). Per
  video: every judged shot with its `[start, end]` and all answers, and per
  condition a summary label: `deepest` answer (and which shot), `median_cm`
  (`median_level` for the L0–L5 run), `answered`, `x`. The deepest shot is the
  label because the scale measures depth at the deepest place visible. Built
  for both runs (`full/` too).

**Answers outside the requested width are kept (owner, 2026-10-21).** The
prompt asks for an interval at most 5 cm wide, as precise as the evidence
allows, and to give its best 5 cm interval rather than a wider one when
unsure. Whatever numeric answer comes back is stored, normalised by
`levels.parse_depth_range`: `"35-40 cm"`, a wider `"30-40 cm"`, a single value
`"35 cm"`, metres converted (`"1-1.5 m"` → `"100-150 cm"`), or `X`. Only a
reply that IS an answer is read (never a number inside a sentence); `null`
means no answer could be read. `levels.within_5cm()` tells which answers met
the request; `evaluate.py` reports it per condition as `within_5cm_rate`.
Smoke test (10 shots, 70 answers): 64 within 5 cm, 6 X, 0 wider, 0 null.
Seen there, to check in the full run: answers cluster on a few values
(10-15, 30-35, 100-105, 120-125 cm, the last two matching entries in the
C4/C5 heights table); C1 varies by up to ~60 cm across one shot's frames; C5
answers X far more often than the level run did (6 of 10 shots).

No item file: items are rebuilt from the scan ledger (ids name the frames);
C3's text is read from `data/youtube_videos.json` in memory. Pilot answers
and boxes with matching ids are reused. `evaluate.py --full --full-dir
scrape_data/experiments/flood_depth/full_5cm` scores it (the default
`--full-dir` is the L0–L5 run). **Owner, 2026-10-21: answers stay in cm;
human labels are not a concern for now** -- no cm→level mapping, no labelling
work planned.
Speed: ~3,400 questions/h per GPU, 7 questions per shot → ~7 h on 3 GPUs.

## Commands

```bash
conda activate /home/liu47/conda_envs/newEnv_local
python3 flood_depth_experiments/flood_depth/sample_items.py --stats         # pool size
python3 flood_depth_experiments/flood_depth/sample_items.py --n 300         # -> items.jsonl (refuses to overwrite)
python3 flood_depth_experiments/flood_depth/detect_refs.py                  # C5 boxes, ~0.5 s/item
python3 flood_depth_experiments/flood_depth/run_conditions.py               # C1-C5, freest GPU with 60 GiB
python3 flood_depth_experiments/flood_depth/run_conditions.py --conditions C2 --limit 20
python3 flood_depth_experiments/flood_depth/evaluate.py                     # -> results.json
python3 data_view_server.py                                    # browse a full run: :8768/depth.html
python3 data_view_server.py --depth scrape_data/experiments/flood_depth/full   # the L0-L5 run instead
```

`/depth.html` (data from `depth_review.py`) shows, per video, every shot's
three frames, each condition's answer and the exact prompt it was given:
the fixed text, the uploader block (C3), the height table (C4/C5) and the C5
frame with the detector's numbered boxes. Read-only. One card per video with
its shots inside; the server filters, sorts and pages (10 videos a page), so
the browser never downloads the whole run.

## Next steps, in order

1. **Labelling page + guide** (one example image per level), server-side labels
   in `labels.jsonl`. Label the 300 pilot items; a second person labels the
   same 300 → human ceiling.
2. Run C1–C5 on the pilot; read `evaluate.py`; look at disagreements.
3. **C6 oracle** from the labels' reference / waterline fields.
4. Prompt variants: reasoning chain (name the reference, place the waterline,
   then the level), thinking on; level probabilities from the first-token
   logits instead of greedy text (expected level, calibration).
5. **Non-VLM baselines**: majority class; text-only (title regex / LLM);
   zero-shot CLIP/SigLIP per level; frozen DINOv2/SigLIP + linear ordinal head
   (needs the train split); reference-object geometry (detector + SAM water
   mask + pose keypoints, Chaudhary-style); metric monocular depth + water mask.
6. **Other VLMs** (current open Qwen-VL / InternVL / Gemma; one hosted model
   on the test set only), and a LoRA fine-tune on the train split as the
   upper bound.
7. **Measured-depth subset**: USGS STN photos linked to high-water marks
   (1,037 of 1,873 STN photos in `gis_flood_images.json` carry an `hwm_id`;
   HWMs record height above ground — the old `STNServices/HWMs` endpoint now
   redirects to `apps.usgs.gov/stnweb`, find the new path) and MyCoast reports
   (tide-station water level − DEM ground elevation at the GPS point; coarse).
8. Native-resolution key frames once downloads work again.
9. More pairs / tasks: pairwise "which is deeper" within a scene; passability.

# depth_detection/ -- water depth from geometry (MoGe-2 + SAM 3)

**Design, every measurement, and what works / what does not:
`docs/water_depth_segmentation.md`** -- start at "Where routes A and B stand".
Update that doc with every run (dated section + the summary table).

Three routes, all computed on every frame, stored side by side, none
overriding another:

- **A (plane)**: level water surface (median height of waterline points on
  objects + shore) minus a RANSAC plane on visible dry ground, along gravity.
- **B (table size)**: per object standing in water, its top height from
  `reference_sizes.json` (the VLM picks the entry) minus its visible height
  (mask top to its own waterline, rows x MoGe distance / focal).
- **V (VLM size)**: the same, with the VLM's own height estimate, asked
  separately and without the table.
- B / V per frame = median over usable objects; abstain if they spread > 25 cm.

## Files and run order

```bash
conda activate /home/liu47/conda_envs/newEnv_local          # set -a; source .env  (HF_TOKEN)
python3 flood_depth_experiments/depth_detection/geometry_depth.py     # 1. MoGe + SAM 3 + route A + B/V objects (GPU, ~2 min load)
python3 flood_depth_experiments/depth_detection/object_sizes.py       # 2. VLM: two questions per object (GPU, ~15 min load)
python3 flood_depth_experiments/depth_detection/geometry_depth.py --summary   # 3. re-score, no GPU
python3 flood_depth_experiments/depth_detection/make_report.py        # 4. report.html, no GPU
cd scrape_data/experiments/flood_depth/geometry && python3 -m http.server 8770 --bind 127.0.0.1   # view
python3 flood_depth_experiments/depth_detection/calibrate_dry.py --summary     # dry-scene size calibration
```

| file | what |
|---|---|
| `segment.py` | SAM 3 only (Grounding DINO + SAM 2 removed 2026-11-04): water / dry ground / objects masks + every object instance with its phrase |
| `geometry_depth.py` | MoGe-2, plane, gravity, waterline, route A; B/V objects; `ref_objects`, `abstain_reason`, `depth_answer`, `summary`, `vlm_size_check` |
| `reference_sizes.json` | the size table: 30 entries, top height + range + basis + source + SAM 3 phrases. Edit it and re-run `--summary` -- no GPU rerun |
| `object_sizes.py` | VLM per object: free (what, top visible, height) and table (which entry, whole table, no phrase) |
| `calibrate_dry.py` | measures known-size objects on non-flood frames: MoGe scale and mask error |
| `make_report.py` | the local viewer page |

Outputs, `scrape_data/experiments/flood_depth/geometry/`: `shots.jsonl` (the 50
pilot shots, written once), `frames.jsonl` (one row per shot, frame, config),
`object_labels.jsonl`, `overlays/`, `frames/` (unmasked copies), `crops/`,
`summary.json`, `report.html`, `calibration/`. Earlier runs' overlays and
summaries are in their own subfolders / `summary_<name>.json` -- keep them.

## Ids and resume

- Geometry rows are keyed by `config_id()` (hash of every geometry setting and
  `segmenter_id()`); current `geo:299e6b18`. Changing anything there is a new
  config: a full rerun, old rows kept. Readers use the current config only.
- Object labels are keyed `(id, frame, k, config, prompt_id)`. `k` is the
  object's index in that config's `objects_b`, so labels are only valid for
  the config they were made on. Current prompt `objsize:f97f4307`; the first,
  anchored one (`objsize:5e36938d`) is kept for comparison, not scored.
- Abstain rules and the size table are applied at read time: change them and
  re-run `--summary` + `make_report.py`.

## Gotchas (all measured, see the doc)

- **Frames are 320 x 180.** Resolution is the main limit on route B (wheels
  read 0.76 x true, IQR ~0.4) -- native-resolution frames are the biggest open
  lever, possible now that downloads work again.
- **SAM 3's phrase is not the object's identity** (most "bollard" masks were
  other things). Do not map phrase -> table entry directly.
- **Do not lead the VLM**: naming the detector's word or offering only its
  entries made the VLM agree with the detector. Ask for identity without hints.
- **VLM heights can be garbage** (runaway digits, millimetres); `vlm_size()`
  drops anything <= 0 or > `MAX_VLM_SIZE_CM`.
- **Scene filter first**: studio / screen / through-glass shots break MoGe's
  scale; they are skipped by default (`--scene all` to include).
- Route A rarely applies: most flood shots show no dry ground.
- `report.html` and its images are local only; not part of the deployed site.
- Do not mark answers right or wrong (owner): nothing has ground truth.

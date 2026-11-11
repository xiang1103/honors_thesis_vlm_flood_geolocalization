# Water depth from geometry + segmentation

Status: **pilot run** (2026-10-29), **dry-scene calibration** (2026-11-03), scene
filter and routes A / B / V (2026-11-04 to 11-05), code in
`flood_depth_experiments/depth_detection/`. **Start with "Where routes A and B
stand"** below -- the summary of what works and what does not; the dated
sections after it are the measurements it rests on. A non-VLM depth estimate for flood
shots, to be measured on its own first and then, if it holds up, given to the
VLM as extra context (a new condition, C7). It extends the water-depth benchmark
in `flood_depth_experiments/flood_depth/PLAN.md` (read that first; this is its
"Next" item 5, "metric monocular depth + water mask", made concrete).

## The idea in one paragraph

A monocular depth model alone cannot measure water depth: it measures distance
to the camera, and the ground under the water is never visible. But two models
together can measure **how high the water surface sits above the dry ground
around it**. A geometry model (MoGe-2) turns every pixel into a metric 3D point;
a segmentation model says which pixels are water, dry ground and objects. Fit a
plane to the dry ground, extend it under the water, find where the water surface
touches solid things (the waterline), and the height of the waterline above the
plane is the depth.

## Constraints from our data (measured 2026-10-29)

- **Single view, effectively.** Most flood shots are one camera position with
  small movement over a few frames -- little change of viewpoint. Multi-view
  reconstruction (VGGT, pi3, MapAnything, Depth Anything 3) needs parallax to
  beat a single view, so it would add cost without information here. MyCoast and
  other static photos are single images by definition.
- **Low resolution.** Shot-scan frames in `scrape_data/youtube_shot_scan/frames/`
  are 320 px wide: of 400 sampled key frames, 254 are 320x180 (landscape) and
  ~130 are ~320x569 (portrait/Shorts). A waterline on a curb may be a few pixels
  tall. Native-resolution frames depend on downloads working again (blocked
  since 2026-10-24, see `scraping/video_scraping/CLAUDE.md`).
- **3 frames per shot** (key frame = index 1), the same as the VLM conditions.
- **No human depth labels** (owner's decision, 2026-10-21). Accuracy can only be
  checked on the USGS STN high-water-mark photos (PLAN.md, Next item 7).

## Decision: MoGe-2, single view

| | MoGe-2 | VGGT-style multi-view |
|---|---|---|
| input | one image | several images of one scene |
| scale | **metric** (metres) | arbitrary; needs a metric model or known object to scale |
| focal length | estimated (YouTube gives none) | estimated |
| needs camera motion | no | yes -- static shots gain nothing |
| works on MyCoast photos | yes | no |

MoGe-2 outputs, per pixel: a metric 3D point, a surface normal, a validity mask;
plus the camera intrinsics. Each of a shot's 3 frames is processed independently,
which also gives the self-consistency check below for free.

Revisit multi-view only if (a) native-resolution frames with real camera motion
become available and (b) the single-view plane fit turns out to be the weak link.

## Segmentation: what it must find

The segmentation model does **not** find a plane. It labels pixels; the plane is
fitted afterwards from the 3D points of the pixels it labels. Three classes:

| class | used for |
|---|---|
| **water** | its boundary gives the waterline (2D), nothing else |
| **dry ground** -- road, sidewalk, curb top, lawn, parking lot | the ground plane |
| **objects** standing in the water -- vehicles, people, poles, walls, steps, hydrants | waterline points on vertical surfaces |

Candidates, first to try first:

1. **SAM 3**, text prompts ("flood water", "road", "sidewalk", "car", ...).
   Open-vocabulary, masks directly.
2. **Grounding DINO + SAM 2.** Grounding DINO already runs for C5
   (`detect_refs.py`), so its boxes can be reused as SAM prompts.
3. A fixed-class ADE20K model (Mask2Former / OneFormer): has water / road /
   sidewalk, but trained on normal scenes; flooded streets tend to come out as
   all road or all river.

Known hard case for every option: brown, still floodwater that looks like
asphalt. The water/dry-ground boundary is exactly where depth is read, so
segmentation quality there is measured directly (see Evaluation).

## Why not use the water's own 3D points

Pixels inside the water region give wrong 3D points and are **never used to
measure the water level**:

- a reflective surface shows a mirror image of the scene; depth models place
  that image *below* the street (inverted buildings under the road);
- murky, textureless water is filled in by the model's prior, often as a
  continuation of the road.

The water level is only measurable where the surface **touches something
solid**. That contact line is the waterline, and it is found in 2D (the edge of
the water mask), then looked up in the 3D point map at those pixels.

## Two measurements: water surface, and the ground under it

A contact point (where water touches a tyre, curb face, wall) fixes the **top**:
the height of the water surface. Depth also needs the **bottom**, the ground
under that point, which is never visible. Three ways to get it:

| route | ground reference | works when |
|---|---|---|
| **A. dry-ground plane** | fit a plane to visible dry road / sidewalk, extend it under the water; depth = water surface minus the plane below it | dry ground is in view and the street is roughly planar |
| **B. object of known size** | a wheel touches the road and is ~66 cm across, so tyre top minus 66 cm is the ground; likewise a curb (~15 cm), a step, a person's knee | the object is identified and partly above water; needs NO dry ground |
| **C. the object's base elsewhere** | a wall or pole whose bottom is visible where it leaves the water | rare, strong when present |

A is general but rests on the planar-street assumption; B rests on knowing what
the object is. Where both are available they check each other.

## The constraint that makes it robust: water is level

A flood surface is flat and horizontal, so **every waterline point in the frame
sits at the same height along gravity**: contacts on cars, curbs, walls, AND
the shore line where water meets dry ground (the shore is where the ground has
risen to the water level). So:

- one water level per frame: the robust median (3-MAD outliers dropped) of the
  gravity heights of all waterline points, not a per-contact measurement;
- contacts far off that level are segmentation errors (a mask edge ending on a
  shadow) and drop out automatically;
- the IQR of the waterline heights is a label-free confidence (3 cm believable,
  40 cm wrong); the gap between the contact level and the shore level is a
  second check (they should agree);
- depth anywhere = water level minus the ground below it, so depth can be read
  under water that touches nothing (rays through water pixels meet the level
  surface; the extended plane is below).

**Gravity is not the ground plane's normal.** On a sloped street the two differ,
and the slope is exactly what makes route A non-zero (on a flat street with a dry
shore, the water would be 0 cm deep). Gravity is estimated from vertical
surfaces (walls, car sides and poles have horizontal normals, so "up" is the
direction none of them have) and falls back to the plane normal, then to the
camera's up axis.

## Sampling at the contact line

- **Just off the edge, on the solid side.** Depth models blur across object
  boundaries, so the 3D point exactly on the mask edge mixes tyre with water (or
  its reflection). Masks are eroded by 1 px and the point is taken on the object
  / dry-ground side.
- **Water below, not beside or above.** A contact is an object pixel with water
  within 3 px directly BELOW it. Water above an object pixel is an occlusion (a
  car bonnet seen against the water behind it), not a contact.
- **On a vertical surface.** The surface normal at a contact must be roughly
  horizontal (`|n . g| < 0.5`). A "contact" on a horizontal surface is shore.
- **Many weak contacts over one sharp one.** At 320 px a curb waterline is 2-3
  pixels; the level constraint pools all of them.

## Pipeline, per frame

1. **Geometry.** MoGe-2 on the frame -> point map `P[y,x]` (metres, camera
   frame), normals `N[y,x]`, validity mask.
2. **Segmentation.** Water, dry ground, objects, wheels.
3. **Ground plane.** RANSAC on dry-ground points whose normals face up (within
   40 deg); threshold grows with distance (max(2 cm, 1.5% of range)); SVD refit
   on inliers. Record inlier fraction, residual (cm).
4. **Gravity**, as above; record which basis was used and the street slope.
5. **Waterline**: contacts and shore, sampled as above.
6. **Water level**: robust median of their gravity heights; IQR; contact-shore gap.
7. **Route A**: depth at each contact (moved onto the level) down to the plane;
   median and p90 over the water region via rays.
8. **Route B**: for each wheel mask touching water, tyre top (95th percentile
   height) minus 66 cm = ground; depth = level minus that.
9. **Abstain** (`abstain_reason()`, applied to stored numbers so thresholds can
   change without the GPU): no water; no dry ground; plane residual > 5 cm;
   < 8 contact px (`shore_only` if there is shore); waterline IQR > 20 cm;
   contact below the plane by > 5 cm (`contact_below_ground`: the dry surface is
   raised, e.g. a sidewalk above a flooded road); depth > 3 m.

Per shot: the 3 frames' depths, their median, and their spread.

## Segmentation backend actually used

**SAM 3 only** (`facebook/sam3`, owner's decision 2026-11-04). It was gated
until 2026-11-03, so the first flood pilot (2026-10-29) used Grounding DINO
base + SAM 2.1 hiera-large; that backend has since been removed from
`segment.py` to save compute and time. Overlaps resolve objects > water >
ground (a water mask around a half-submerged car otherwise swallows the car,
and its lower edge is the contact we want).

## What this cannot do

- **Deep water with no dry ground in view** -- no plane, abstain.
- **A flat street with nothing standing in the water** -- only shore points,
  which read ~0; abstain rather than answer 0.
- **Ground that changes slope under the water** (a dip, an underpass): the
  extended plane is wrong and the answer is too shallow. Not detectable from one
  frame.
- **Very low resolution**: at 320 px, one pixel can be several cm at 5-10 m;
  the error floor is set by that, not by the models.

It is a different failure profile from the VLM's, which is the point: where the
two agree, the answer is more trustworthy.

## Evaluation (no labels)

1. **Coverage** -- share of shots with a non-null depth, and the abstain reasons.
2. **Self-consistency** -- spread of depth across a shot's 3 frames, compared
   with C1's within-shot spread on the same shots (VLM: up to ~60 cm). This is the
   go / no-go number.
3. **Level checks** -- the waterline IQR, and the gap between the contact level
   and the shore level (`level_gap_cm`); both should be small if the water really
   is level and the masks are right. (Shore height above the plane is NOT a
   check: shore points are dry ground, so they are in the plane fit.)
4. **Agreement with C4/C5** on the same shots -- not accuracy, but large
   systematic gaps point at one side's failure mode.
5. **Overlays** -- write an image per frame (water / ground / object masks,
   waterline coloured by height, the number) and look at a sample by eye.
6. **Accuracy, later** -- USGS STN photos with high-water marks (PLAN.md item 7):
   the only measured ground truth in reach.

## C7: geometry as VLM context

Only if the pilot passes. Two variants, so the difference shows what the VLM
adds beyond the geometry:

| condition | input |
|---|---|
| C7a | C5 + overlay (water tinted, waterline drawn), no number |
| C7b | C7a + the computed depth as text, with confidence and basis, e.g. `geometry estimate: 25 cm (medium; waterline on a car tyre, 3 frames agree within 6 cm)` |

Not planned: giving the VLM a colour-mapped depth image (VLMs read false-colour
maps poorly). C7b anchors the model the way C3's uploader text does, so report it
separately and measure how often the answer simply repeats the given number.
When the geometry abstains, C7b says so rather than omitting the line.

## Code and outputs

`flood_depth_experiments/depth_detection/`:
- `segment.py` -- the masks (SAM 3), with a config id.
- `geometry_depth.py` -- MoGe-2, plane, gravity, waterline, route A, and the geometry half of
  routes B / V (every reference-able object standing in water, its visible height); computes
  B and V from the stored numbers + labels; overlays, summary. `--summary` re-scores without a GPU.
- `reference_sizes.json` -- route B's fixed size table (30 entries, top height above ground,
  range, basis, source, SAM 3 phrases). Edit it and re-run `--summary`; no GPU rerun needed.
- `object_sizes.py` -- the local VLM's two separate answers per object (table entry; own size).
- `calibrate_dry.py` -- measures known-size objects on dry scenes (see
  "Dry-scene calibration"); `--summary` likewise.
- `make_report.py` -- `geometry/report.html`: routes A, B and V per frame, the original frame
  and the masked overlay per frame, every object with its crop, both sizes and both depths,
  next to the VLM's C1-C5 (local only; the frames are
  YouTube stills and are never published).

MoGe is installed in `newEnv_local` as `moge` 3.0.0 + `utils3d_moge`, both
`--no-deps` (its full dependency list pulls gradio and a CUDA-compiled
`flex-gemm`, which MoGe-2 inference does not use).

Experiment outputs only, nothing in `data/` (same rule as PLAN.md):

```
scrape_data/experiments/flood_depth/geometry/
  shots.jsonl     the 50 chosen shots (written once, reused)
  frames.jsonl    one line per (shot, frame, config): every number + abstain reason
  overlays/<video>_shotNNN_<j>.jpg   current config, one per frame: water blue, dry ground
                  green, objects orange, A contacts red, A shore yellow, B/V objects magenta
                  (box, top-to-waterline line, number = the object's k)
  frames/<video>_shotNNN_<j>.jpg     the original frame, unmasked (copy, for the report)
  crops/<video>_shotNNN_<j>_<k>.jpg  each B/V object, cropped and enlarged
  object_labels.jsonl                object_sizes.py's answers, per (shot, frame, k, config, prompt)
  overlays/gdino-sam2/, overlays/sam3/, overlays/sam3-wheels/, summary_gdino-sam2.json,
  summary_sam3.json, summary_sam3-wheels.json   the earlier runs, kept as they were
  summary.json    per route: coverage, abstain reasons, spread vs C1, gap to C4/C5, per shot
  report.html     make_report.py: routes A and B next to the VLM's C1-C5, per shot
```

Ids match the shot-scan ledger and `full_5cm/`, so results join with the VLM
answers. Point maps are not stored (large, recomputable).

## Where routes A and B stand (2026-11-10)

A summary of everything measured so far (50-shot pilot, 46 shots / 138 frames
after the scene filter, frames 320 x 180 px, config `geo:299e6b18`, VLM sizes
prompt `objsize:f97f4307`). **No answer has been checked against ground
truth**; "works" below means the method runs, answers, and is self-consistent
or agrees with independent knowledge -- not that it is right. Every answer is
on `geometry/report.html` for people who know flooding to judge.

| | route A (plane) | route B (table size) | route V (VLM size) |
|---|---|---|---|
| depth = | water level - dry-ground plane, along gravity | table height of an object in water - its visible height | VLM's height of the object - its visible height |
| needs | visible dry ground + a waterline on objects | an object of known size standing in water, top visible | an object standing in water, top visible |
| frames answered (of 138) | 6 | 23 | 27 |
| shots with an answer (of 46) | 5 | 12 | 18 |
| shots answered on all 3 frames | 0 | 3 | 3 |
| within-shot spread, median (VLM C1, same shots) | -- | 13.6 cm (5.0) | 28.1 cm (10.0) |
| main reason for no answer | no dry ground (67), water touches only ground (27), ground not flat (18) | no reference object in water (68), not a table object (14), objects disagree (8) | no reference object in water (68), objects disagree (24) |

Where two routes answer the same frame: B vs V 9 frames, median |diff| 15 cm;
A vs B 4 frames, median |diff| 58 cm.

### Route A: what works

- **The geometry runs on every frame** and every intermediate is stored
  (plane, gravity basis, contacts, shore, level and its IQR), so thresholds
  change without a GPU (`--summary`).
- **It needs no assumed size.** Where it applies it is the only route that
  measures depth from the scene itself.
- **It has built-in self-checks**: water is level, so the contact and shore
  heights must agree (`level_iqr_cm`, `level_gap_cm`), and the ground plane
  must fit (`plane_resid_cm`). These catch bad frames rather than guess.
- **SAM 3 helped**: dry ground found went from 0.1% to 2.1% of the frame
  (median), answering frames from 2 to 5-6.
- A few answers look plausible against the VLM and the overlays (e.g.
  `G91_dMVieV8:shot025` 25-26 cm under the old segmenter, VLM 30-35 cm;
  `OAZKqcG2HTU:shot002` 6.9 cm at a hydrant).

### Route A: what does not work

- **Coverage is ~4% of frames, and that is structural.** Typical YouTube flood
  footage shows no dry ground: the road is under water from edge to edge, or
  the camera is in the water. 67 of 138 frames have no dry ground to fit; 27
  more have water meeting only ground, no object to put a waterline on. Better
  segmentation cannot create ground that is not in view.
- **The extended plane is the weak link.** The plane is fitted on whatever dry
  ground is visible -- lawns, a sloped driveway, a bank -- and extended under
  the water. 18 frames fail the flatness check; the calibration measured
  wheel-hub height above the plane at 0.67 x true with IQR 0.67, i.e. the plane
  extended to 20+ m is off by tens of centimetres. `E7knsQuTRLo:shot006` gave
  197 cm because the "ground" was a river bank.
- **The level constraint is usually violated.** Contact and shore heights
  differ by a median of 25 cm; water cannot do that, so the water mask, the
  contacts or the depth map is off in most frames. This gap is not yet an
  abstain rule (open).
- **No shot answered on all 3 frames**, so its consistency cannot be measured.

### Route B (table sizes): what works

- **3-4x route A's coverage** (23 frames, 12 shots), and it needs no ground
  plane, no gravity and no frame-wide level -- each object carries its own
  waterline.
- **Generalised beyond wheels** (2026-11-05): any object in the 30-entry
  `reference_sizes.json` (tyres, cars by type, buses, curbs, hydrants, cones,
  drums, barriers, guardrails, bollards, mailboxes, bins, garage doors). 182
  objects standing in water were found in 60 frames, against 12 frames with
  wheels.
- **MoGe's scale holds on real street scenes**: licence plates (flat, standard,
  fully visible) measured 1.07-1.09 x true in the dry calibration.
- **Objects in one frame mostly agree**: of 31 frames with usable objects, 8
  disagreed by more than 25 cm. Example, `G91_dMVieV8:shot025` frame 1: a car
  and two tyres gave 30-42 cm; route B answered 34 cm on all three frames
  (33.7, 32.9, 35.3; VLM C1 30-35, 10-15, 30-35).
- **The VLM's identification is usable when it is asked fairly**: offered the
  whole table without the detector's word, it names common objects (car, car
  tyre, person) consistently with its own free description.

### Route B: what does not work

- **Resolution.** At 320 px a wheel is 5-10 px across. The calibration read
  wheels 0.76 x true with IQR ~0.4 (~24 cm on a 64 cm tyre); only 40% landed
  within 20%. Depth = size - visible height inherits all of it: a visible
  height read 23% short makes the water look that much deeper.
- **SAM 3's word is often wrong.** Of 28 "bollard" masks the VLM called ~4
  bollards (the rest mailboxes, boats, trash cans, hydrants, mooring posts);
  most "door" masks were car doors or windows; "curb" masks were hydrants.
  The phrase cannot pick the table entry by itself.
- **The table question was anchored at first**: told the detector's word and
  offered only its entries, the VLM confirmed the detector (9 of 15 "bollard"
  picks were described as something else). Fixed (whole table, no word), but
  it shows the pick is sensitive to the prompt.
- **Less consistent than the VLM it was meant to check**: within-shot spread
  13.6 cm vs C1's 5.0 cm on the same 3 shots.
- **Occlusion and mask edges**: a car hidden behind another, a wheel arch, a
  wake or reflection at the waterline all move the visible height; there is
  no check for them beyond "top cut off by the frame" and the VLM's "top
  visible".
- **The table is unverified** (`checked: false` on all 30 entries) and US-only.

### Route V (VLM sizes): what works and what does not

- **Works**: widest coverage (27 frames, 18 shots), and it is the only route
  for objects not in the table. Where the VLM's free answer and its table pick
  name the same thing, its heights match the table closely: car tyres 70 vs
  64 cm, sedans 147.5 vs 145, people 175 vs 170, small SUVs 170 vs 170.
- **Does not work**: what the VLM thinks it is sizing is unstable. Asked
  freely it sometimes describes a different object than the box holds -- a
  tyre-sized mask called "car" and given 140 cm (depth 108), a heavy tyre
  called a wheel loader and given 300 cm. Hence 24 frames with objects
  disagreeing and a 28 cm within-shot spread. 10 of 182 answers were not
  heights at all (runaway digits, or 1500 for a car), now discarded
  (`MAX_VLM_SIZE_CM`). Route V also depends on the VLM, so it is not an
  independent check on the VLM benchmark.

### What would change the picture

1. **Native-resolution frames.** Downloads work again (since 2026-10-25), so
   this is now actionable: the 50 pilot videos and the calibration's videos
   re-downloaded (transient, per the video-bytes rule) and their frames kept
   at full size. Resolution is the main limit on route B and part of route A's.
2. **Agreement rule for B / V**: count an object only when the VLM's free
   answer and its table pick name the same thing (would remove most of V's
   outliers). Not built -- it decides which answers count.
3. **Contact-vs-shore gap as an abstain rule** for route A.
4. **A hand-picked set of shots with dry ground** to see route A's accuracy
   where it applies, instead of its coverage on random shots.
5. **Some ground truth** (STN high-water marks, PLAN.md step 7) -- until then
   every comparison here is consistency, not accuracy.

## Pilot results (2026-10-29)

50 random eligible flood shots (one per video, seed 0), 150 frames, config
`geo:8805ed22`, ~0.7 s per frame on one GPU.

| | frames |
|---|---|
| route A answered | **2** (1 shot) |
| no dry ground | 93 |
| shore only (no contacts) | 23 |
| no water found | 21 |
| plane fit poor | 10 |
| contact below ground | 1 |

The one answered shot (`G91_dMVieV8:shot025`, cars in a flooded residential
street): 25.4 and 26.0 cm on two frames; the VLM said 30-35 cm (C4, C5) and
10-15 / 30-35 cm across C1's frames. Plausible, but one shot is not evidence.

What the overlays show:
- **Most YouTube flood shots have no dry ground in view.** Of the 93
  `no_dry_ground` frames, 86 have under 1% dry ground. In 73 a ground class WAS
  detected but the water mask covered it, which is mostly correct (the road
  really is under water) and partly the water mask over-reaching onto wet dry
  road. Route A's precondition fails on the typical shot, not on a few.
- **A large share is not a scene at all**: news broadcasts with picture-in-
  picture and banners, split screens, TikTok text, reporters filling the frame,
  windscreen views. Geometry on those is meaningless; they need filtering
  before any depth method, VLM or geometric.
- **Contacts exist where route A cannot use them.** 27 frames have >= 8
  contact pixels (cars and people standing in water), mostly in frames without
  dry ground.
- **Route B (wheels) is not trustworthy yet.** It gave a number on 35 frames,
  but values range from -173 to 153 cm. Some shots are self-consistent
  (`fm3jEamEAvk`: 52 / 47 / 47 cm; `Wb0JQd86SoI`: 34 / 38) and some are not;
  the "wheel" box often covers the whole car, so the "tyre top" is the roof.

Within-shot spread could not be compared with the VLM: no shot was answered on
all 3 frames. The go / no-go question is therefore not answered yet; the
measured finding is that **route A covers ~1% of random flood frames**.

### SAM 3 rerun (2026-11-04)

Same 50 shots, same geometry, `--segmenter sam3` (config `geo:1f983847`);
scored on the 46 the scene filter keeps.

| | gdino-sam2 | sam3 |
|---|---|---|
| dry ground, median share of frame | 0.1% | 2.1% |
| frames with no dry ground | 84 | 67 |
| frames with no water | 19 | 10 |
| plane fit too poor (> 5 cm) | 10 | 18 |
| frames answered / shots with an answer | 2 / 1 | 5 / 4 |

SAM 3 finds far more ground and water; answering frames went from 2 to 5.
No answer has been checked against ground truth -- all of them are shown with
their overlays and the VLM's C1-C5 in `geometry/report.html` for people who
know flooding to judge. The stored numbers, without verdicts:

| shot | segmenter | answer | contact minus shore level | note |
|---|---|---|---|---|
| `G91_dMVieV8:shot025` | gdino-sam2 | 25.4, 26.0 cm (2 of 3 frames) | -5, 21 cm | sam3 declines it: its ground mask adds the lawns beside the road, plane error 7-13 cm |
| `OAZKqcG2HTU:shot002` | sam3 | 6.9 cm (frame 1) | -3 cm | night street, water around a hydrant |
| `uH6EqiDJb0U:shot001` | sam3 | 32.1 cm (frame 1) | 52 cm | |
| `_EkgRTOzd7E:shot009` | sam3 | -3.6, -3.0 cm (frames 2, 3) | 46, 32 cm | |
| `E7knsQuTRLo:shot006` | sam3 | 197.4 cm (frame 1) | -159 cm | river in woodland; "ground" is the bank |
| `iDiB4kzythQ:shot001` | sam3 | 233.9 cm (frame 1) | -165 cm | scene-filtered shot, not in the 46 |

The contact-minus-shore gap is the method's own consistency number: water is
level, so the two should agree. It is recorded but is not an abstain rule.
Whether to make it one is open; the threshold should not be picked from these
answers. Either way route A answers a handful of the 50 shots with either
segmenter.

### Routes A and B together, SAM 3 only (2026-11-04)

Owner's decisions: every prediction runs BOTH routes, stored side by side
(neither overrides the other), and segmentation is SAM 3 only -- the
Grounding DINO + SAM 2 backend is removed from `segment.py` (its pilot rows
stay in `frames.jsonl` under config `geo:8805ed22`). Grounding DINO is still
used by the VLM benchmark's C5 boxes (`detect_refs.py`), which is separate.

Route B was rewritten to be the known-size measurement on its own: per
wheel whose mask has water within 3 px below it, the wheel's waterline is
the median row of those pixels, visible height = (waterline row - top row) x
distance / focal length (MoGe-2's distance and focal), depth = 66 cm -
visible height; the frame's answer is the median over wheels. No ground
plane and no frame-wide water level. Abstains: `no_wheel`,
`no_wheel_in_water`, `wheels_disagree` (> 25 cm apart in one frame),
`visible_taller_than_tyre` (depth < -5 cm), `implausible` (>= 66 cm). The
2026-11-03 calibration measured wheels about 23% small with a wide spread at
320 px; that applies to these numbers too.

Config `geo:ad6cfddb`, the same 50 shots, scored on the 46 the scene filter
keeps:

| | route A | route B |
|---|---|---|
| frames answered (of 138) | 5 | 12 |
| shots with an answer | 4 | 6 |
| shots with all 3 frames answered | 0 | 3 |
| main abstain | no dry ground 67 | no wheel found 91 |
| within-shot spread, median (VLM C1 on the same shots) | -- | 12.7 cm (20.0) |

No frame got an answer from both routes. Every answering shot, without
verdicts (the page shows the overlays for judging):

| shot | route A (per frame) | route B (per frame) | VLM C1 | C4 / C5 |
|---|---|---|---|---|
| `G91_dMVieV8:shot025` | plane fit poor x3 | 35.0, 29.3, 30.0 | 30-35, 10-15, 30-35 | 30-35 / 30-35 |
| `fm3jEamEAvk:shot001` | no dry ground x3 | 44.2, 41.8, 31.5 | 45-50, 45-50, 40-45 | 50-55 / 50-55 |
| `QBIdCeHrNDE:shot001` | -- | 3.7, 14.9, 19.3 | 30-35, 10-15, 10-15 | 45-50 / 30-35 |
| `6sb8krWtsDY:shot001` | -- | 15.6, --, -- | 30-35, 20-25, 10-15 | 30-35 / 30-35 |
| `DLsDHu-0Ru0:shot002` | plane fit poor x3 | --, --, 36.0 | 10-15, 10-15, 20-25 | 30-35 / 30-35 |
| `by1rbFm-w1g:shot002` | -- | --, --, 61.3 | 40-45, 45-50, 20-25 | 100-105 / 100-105 |
| `E7knsQuTRLo:shot006` | 197.4, --, -- | no wheel | 20-25, 10-15, 20-25 | 100-105 / X |
| `OAZKqcG2HTU:shot002` | 7.0, --, -- | no wheel | 10-15 x3 | 30-35 / X |
| `_EkgRTOzd7E:shot009` | --, 0.0, 0.0 | -- | 10-15 x3 | X / X |

(`iDiB4kzythQ:shot001`, A 233.9 cm on frame 1, is a scene-filtered shot.)

### Route B generalised: any reference object, table sizes (B) and VLM sizes (V) (2026-11-05)

Owner's direction: route B must work for any object of known size, not only
wheels; keep a fixed, high-fidelity size table as the reference; also try
the VLM's own size estimate, and show how trustworthy it is.

**Geometry (no size assumed).** SAM 3's object query gained traffic cone,
traffic barrel, concrete barrier, guardrail, bollard, trash can (this also
adds route A contact candidates). Every instance whose phrase can be a table
entry and whose mask has water within 3 px below is kept (>= 20 px, IoU > 0.6
across phrases = one object, at most 10 per frame): top row, own waterline
row, distance, visible height = rows x distance / focal. Config `geo:299e6b18`.

**Size table.** `reference_sizes.json`: what height the object's TOP is above
the ground it stands on, with a range, a basis (standard / geometry from
tyre size codes / typical spec sheets / varies) and a source line. Used only
if the basis is not "varies", it stands on the flooded ground (house doors:
the doorstep is often above the street) and the half-range is <= 20 cm.
Persons, house doors, fences, full-size vans and box trucks are listed but
not used. All entries say `checked: false`: written from known standards and
spec sheets, not yet verified against the documents.

**VLM (object_sizes.py), two separate questions per object**, on the frame
upscaled 3x with a red box: (free) what is it, is its top visible, how high
is its top in cm -- no table shown; (table) which table entry it is -- labels
only, never heights. Route B = table height - visible; route V = VLM height -
visible; per frame, the median over usable objects, abstain if they spread
> 25 cm.

The first table prompt (`objsize:5e36938d`) named the SAM 3 phrase and offered
only that phrase's entries. The pick followed the detector: 9 of 15 objects
picked as "bollard" were named mailbox, trash can, mooring post or dock piling
by the free question. The current prompt (`objsize:f97f4307`) offers the whole
table and does not name the phrase; 128 of 182 picks stayed the same. Both
are kept in `object_labels.jsonl`; only the current prompt is scored.

**Results**, 46 kept shots (138 frames), 182 objects standing in water in 60
frames (car 41, wheel 40, bollard 28, person 19, door 17, guardrail 10, ...):

| | route A | route B (table) | route V (VLM size) |
|---|---|---|---|
| frames answered | 6 | 23 | 27 |
| shots with an answer | 5 | 12 | 18 |
| shots, all 3 frames | 0 | 3 | 3 |
| within-shot spread, median (VLM C1 same shots) | -- | 13.6 cm (5.0) | 28.1 cm (10.0) |
| main abstains | no dry ground 67 | no object in water 68, not in table 14, objects disagree 8 | no object in water 68, objects disagree 24 |

Same frame by two routes: B - V 9 frames, median |diff| 15 cm; A - B and A - V
4 frames each.

**How trustworthy the VLM's sizes are.** 10 of 182 free answers were not
heights (degenerate output like `15000000...`, or 1500 for a car -- millimetres);
sizes <= 0 or > 1000 cm are not answers (`MAX_VLM_SIZE_CM`). For the 109
objects where the VLM picked a table entry and gave a usable height: median
|VLM - table| 15 cm, 50% inside the table's range. It splits sharply by whether the two answers
agree on WHAT the object is:

| entry the VLM picked | n | table cm | VLM median cm | median abs diff |
|---|---|---|---|---|
| passenger-car tyre | 25 | 64 | 70 | 6 |
| adult person | 18 | 170 | 175 | 5 |
| sedan | 14 | 145 | 147.5 | 5 |
| small/mid SUV | 6 | 170 | 170 | 0 |
| bollard | 13 | 107 | 100 | 47 |
| curbside mailbox | 7 | 135 | 120 | 15 |
| SUV / pickup tyre | 7 | 76 | 150 | 74 |
| guardrail | 5 | 79 | 100 | 21 |
| heavy truck / bus tyre | 5 | 104 | 300 | 196 |
| curb | 3 | 15 | 70 | 55 |

Where the free answer names the same thing (car, car wheel, person) the
VLM's sizes sit within ~5 cm of the table. The large gaps are objects the two
questions identify differently: the free question called the "heavy tyre"
objects a wheel loader and sized the whole machine; the "curb" objects a fire
hydrant. So the VLM knows common sizes well, but which object it is sizing
is not stable between questions, and that is what route V's spread (28 cm)
reflects. Not yet a rule: abstaining when the two answers name different
objects.

## Dry-scene calibration (2026-11-03)

Question: can SAM 3 + MoGe-2 measure objects of known size correctly? Tested
on dry street scenes, where every reference is fully visible, so measured /
nominal is the error route B would carry into a flood frame. No labels needed.

Run: `calibrate_dry.py --n 0`, the key frame of one random NON-flood shot per
video: 3,073 frames, 1,193 skipped because SAM 3 found flood water anyway, 539
with at least one reference, 0.47 s per frame. Outputs in
`scrape_data/experiments/flood_depth/geometry/calibration/` (`summary.json`,
`sheet_<class>.jpg` crops sorted by measured / nominal, `crops/`).

Detected and kept by the shape checks:

| reference | detected | kept | main rejections |
|---|---|---|---|
| wheel | 1,702 | 1,034 | 408 not on a car mask, 136 too small, 119 too thin |
| license plate | 386 | 273 | 69 wrong aspect, 44 too small |
| curb | 331 | 331 | (no check) |
| stop sign | 54 | 38 | 15 not square |
| fire hydrant | 24 | 19 | 5 wider than tall |

**Finding 1: MoGe's scale breaks on composite and through-glass frames.**
Objects MoGe placed under 5 m measured at 8-25% of their real size (wheels: 141
instances in 71 videos, median 0.09). The frames: TV studio screens,
picture-in-picture, filming through a windscreen or window, phones held up to
the camera. MoGe anchors its scale on the glass, the frame or the presenter,
and shrinks everything behind it. The few genuine close-ups among them (a van,
a car in a garage) measured 0.90-0.93. The same scene filter the flood pilot
called for would remove most of these; until it exists, the numbers below
use objects at >= 10 m from the camera as a stand-in.

**Finding 2: on real street scenes the sizes are roughly right but loose**
(measured / nominal, objects >= 10 m away, shape checks passed):

| measure | n | median | IQR | within 10% | within 20% |
|---|---|---|---|---|---|
| license plate width, 2d | 219 | 1.09 | 0.42 | 32% | 54% |
| license plate height, 3d | 219 | 1.07 | 0.37 | 31% | 54% |
| wheel diameter, ellipse | 777 | 0.77 | 0.37 | 22% | 40% |
| wheel diameter, 2d | 777 | 0.76 | 0.43 | 21% | 42% |
| wheel diameter, 3d | 777 | 0.65 | 0.40 | 11% | 28% |
| wheel hub above road plane | 759 | 0.67 | 0.67 | 11% | 23% |
| stop sign width, 2d | 28 | 0.83 | 0.53 | 25% | 32% |
| fire hydrant height, 3d | 14 | 0.74 | 0.70 | 14% | 29% |
| curb height, 3d | 222 | 3.25 | 5.96 | 5% | 9% |

- **MoGe's metric scale is about right on real scenes.** License plates are
  flat, standard (30.5 x 15.2 cm in the US) and fully visible, so they test the
  scale more than the masks: median 1.07-1.09, i.e. MoGe's distances are within
  ~10% on average.
- **Wheels read ~23% small**, consistently across methods. Plates being right
  points at the masks, not MoGe. The crop sheet shows why: most wheels are 5-10
  px across at 320 px; masks catch the dark lower part of the tyre, and oblique
  or partly hidden wheels come out as slivers.
- **Spread is the real problem.** An IQR of 0.37 on a 66 cm wheel is ~24 cm;
  only 40% of wheels land within 20% (13 cm). Depth = known size − visible
  height inherits that, before adding the waterline's own error. That is no
  more precise than the VLM (C1's median within-shot spread is ~10 cm on the
  pilot's shots).
- **2d beats 3d.** Pixel extent x (distance / focal) is better than the extent
  of the object's own 3D points, which bleed into the background at the edges
  (worst for stop signs, which stand against the sky: 3d median 8.6).
- **Hub height above the road plane is unusable** (median 0.67, IQR 0.67):
  the plane extended out to a car 20+ m away is off by tens of centimetres.
- **Curbs as masked are unusable**: the "curb" mask includes the curb's top
  and the sidewalk edge, so its vertical extent is mostly the ground receding.

**Verdict.** At 320 px, route B is limited by resolution, not by the idea:
the references are there, and MoGe's scale holds on real scenes, but the
objects are too few pixels for a centimetre-level measurement. Route B
(and route A) should be re-tested on native-resolution frames once YouTube
downloads work again; the calibration is a one-command rerun on them.

## Scene filter (2026-11-04)

`flood_depth_experiments/scene_split/scene_filter.py`: the local VLM (thinking
off) sees each shot's key frame and picks one letter -- A direct view, B TV
studio, C several pictures in one frame, D a filmed screen, E through a
windscreen / window with the glass or interior in frame, F graphic. Labels,
not a filter: one row per shot in
`scrape_data/experiments/flood_depth/scene_filter/labels_*.jsonl`, nothing
deleted. `geometry_depth.py`, `calibrate_dry.py` and `evaluate.py` leave out
B-F by default (`SKIP`); `--scene all` puts them back. Unlabelled shots are
kept. 17,895 shots labelled in ~1 h on 2 GPUs (5,000-9,500 shots/h per GPU),
every reply a letter.

| set | shots | direct | multi-picture | through glass | studio | screen | graphic | left out |
|---|---|---|---|---|---|---|---|---|
| flood shots (eligible) | 14,822 | 13,395 | 661 | 587 | 96 | 60 | 23 | 9.6% |
| calibration frames (dry) | 3,073 | 2,352 | 122 | 220 | 120 | 18 | 241 | 23.5% |
| geometry pilot | 50 | 46 | 1 | 3 | 0 | 0 | 0 | 8% |

**Check against MoGe's measured scale** (calibration, kept masks, measured /
true size, median):

| scene | license plate width | wheel diameter | instances under 5 m |
|---|---|---|---|
| direct | 1.03 (n 230) | 0.76 (n 872) | 9% |
| through glass | 0.90 (n 38) | **0.46** (n 124) | 28% |
| multi-picture | 0.44 (n 3) | **0.09** (n 23) | 68% |
| studio | 0.20 (n 1) | **0.14** (n 15) | 78% |
| graphic | 0.25 (n 1) | -- | 100% |

The flagged scenes are exactly where the scale breaks, through-glass included
(wheels at half size), so it stays in `SKIP`. The filter is not complete:
79 "direct" wheels under 5 m still measure 0.09 -- broken scenes the VLM
called direct. A second, geometric check (MoGe distance < 5 m together with a
reference object at a fraction of its size, or a large flat surface close
to the camera) would catch those; not built. With the filter, the
calibration's >= 10 m numbers barely move (plates 1.01 / 1.13 at 10-20 / 20+ m,
wheels 0.75 / 0.81): the earlier verdict -- scale fine, route B
resolution-limited -- stands. The geometry pilot keeps 46 of 50 shots and
still answers the same 1 shot.

## Next

1. **Scene filter** -- built, see "Scene filter". Open: the geometric second
   check for broken scenes the VLM calls direct.
2. **Native-resolution frames.** The calibration says resolution is the
   bottleneck for route B; rerun `calibrate_dry.py` on full-size frames when
   downloads work. If wheels then land within ~10%, build route B on the flood
   frames with contacts.
3. **Wheel bias**: if the -23% survives at native resolution, correct it
   with the measured ratio or fit the ellipse to the rim edge, not the mask.
4. ~~Rerun the flood pilot with SAM 3~~ -- done, see "SAM 3 rerun": more
   ground and water found, no more usable answers. Open: whether to make
   the contact-vs-shore level gap an abstain rule.
5. A hand-picked pilot of shots that DO show dry ground, to test route A's
   accuracy where it applies.
6. Only then: C7a / C7b in `run_conditions.py`, and the STN high-water-mark
   subset for accuracy.

7. Routes B / V: verify `reference_sizes.json` against the standards / spec
   sheets (`checked: true`); decide whether an object counts only when the
   free and table answers name the same thing.

## Open questions

- Pilot shot selection: random (done: honest coverage, ~1%) vs hand-picked
  with dry ground (not done: tests accuracy where it applies). Report both.
- Upscaling 320 px frames before MoGe-2 / SAM 3: helps the models' input size,
  adds no information. Try both on the pilot.
- Whether contact points on people are usable (legs move, partly occluded) or
  only rigid objects count.

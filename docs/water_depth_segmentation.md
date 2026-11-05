# Water depth from geometry + segmentation

Status: **pilot run** (2026-10-29) and **dry-scene calibration** (2026-11-03), code in
`flood_depth_experiments/depth_detection/`; results under "Pilot results" below. A non-VLM depth estimate for flood
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

`facebook/sam3` was gated on Hugging Face until 2026-11-03, when access was
granted; the calibration uses it. The flood pilot (2026-10-29) used option 2, **Grounding DINO base
(local) + SAM 2.1 hiera-large**, one detector query per class. `segment.py` has
both backends; `--segmenter sam3` switches once access is granted at
https://huggingface.co/facebook/sam3. Overlaps resolve objects > water > ground
(a water box around a half-submerged car otherwise swallows the car, and its
lower edge is the contact we want).

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
- `segment.py` -- the masks (gdino-sam2 or sam3), with a config id.
- `geometry_depth.py` -- MoGe-2, plane, gravity, waterline, routes A and B,
  overlays, summary. `--summary` re-scores stored numbers without a GPU.
- `calibrate_dry.py` -- measures known-size objects on dry scenes (see
  "Dry-scene calibration"); `--summary` likewise.

MoGe is installed in `newEnv_local` as `moge` 3.0.0 + `utils3d_moge`, both
`--no-deps` (its full dependency list pulls gradio and a CUDA-compiled
`flex-gemm`, which MoGe-2 inference does not use).

Experiment outputs only, nothing in `data/` (same rule as PLAN.md):

```
scrape_data/experiments/flood_depth/geometry/
  shots.jsonl     the 50 chosen shots (written once, reused)
  frames.jsonl    one line per (shot, frame, config): every number + abstain reason
  overlays/<video>_shotNNN.jpg   3 frames side by side; water blue, dry ground green,
                  objects orange, contacts red, shore yellow, numbers top-left
  summary.json    coverage, abstain reasons, spread vs C1, gap to C4/C5, per shot
```

Ids match the shot-scan ledger and `full_5cm/`, so results join with the VLM
answers. Point maps are not stored (large, recomputable).

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

## Next

1. **Scene filter first** (both runs point here): drop broadcast graphics,
   picture-in-picture, split screens, through-windscreen / window shots, and
   close-ups, before ANY depth method. A cheap VLM or CLIP question on the key
   frame. Re-score both runs on what survives.
2. **Native-resolution frames.** The calibration says resolution is the
   bottleneck for route B; rerun `calibrate_dry.py` on full-size frames when
   downloads work. If wheels then land within ~10%, build route B on the flood
   frames with contacts.
3. **Wheel bias**: if the -23% survives at native resolution, correct it
   with the measured ratio or fit the ellipse to the rim edge, not the mask.
4. Rerun the flood pilot with SAM 3 (`--segmenter sam3`) and compare water
   masks on the same 150 frames.
5. A hand-picked pilot of shots that DO show dry ground, to test route A's
   accuracy where it applies.
6. Only then: C7a / C7b in `run_conditions.py`, and the STN high-water-mark
   subset for accuracy.

## Open questions

- Pilot shot selection: random (done: honest coverage, ~1%) vs hand-picked
  with dry ground (not done: tests accuracy where it applies). Report both.
- Upscaling 320 px frames before MoGe-2 / SAM 3: helps the models' input size,
  adds no information. Try both on the pilot.
- Whether contact points on people are usable (legs move, partly occluded) or
  only rigid objects count.

"""The input conditions: what the VLM is shown for one item (PLAN.md, "Conditions").

Every condition asks the SAME question (QUESTION) and accepts the same answers
(levels.ANSWERS), so they differ only in their input:

  C1  one frame (the shot's middle frame; the runner can also ask every frame
      separately, for the within-shot consistency metric)
  C2  all 3 sampled frames of the shot, in time order
  C3  the middle frame + the uploader's title, tags and description
  C4  the middle frame + a table of typical object heights (levels.REFERENCE_HEIGHTS)
  C5  C4, with automatically detected reference objects drawn as numbered
      boxes and listed (detect_refs.py). C5 - C4 isolates what the detector adds.

The base question gives depths in centimetres and names no objects, so that C4
measures the height table and nothing else. Each builder returns
(parts, info): `parts` is the ordered list of PIL images and strings handed to
LocalVLM.generate_parts; `info` is recorded in the prediction ledger.

`prompt_id(condition)` hashes the condition's fixed text. A prompt edit
changes it, and run_conditions.py treats the new prompt as new work -- this
experiment compares prompts, so its resume is prompt-AWARE (unlike the
production verifiers, whose resume is prompt-agnostic by the owner's choice).
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from levels import reference_table

PROJECT = Path(__file__).resolve().parents[2]

#: Saved scan frames are 320 px wide. Every condition upscales its frames to
#: the same long side, so resolution is not a difference between conditions
#: (C5 needs the room to draw readable box numbers).
IMAGE_LONG_SIDE = 640
DESCRIPTION_MAX_CHARS = 1500
TAGS_MAX = 30


QUESTION = (
    "Estimate how deep the flood water is: the height of the water surface above the ground "
    "underneath it, at the deepest place in the scene where you can judge it. Phone videos "
    "often show blurred copies of the picture at the sides; judge the sharp picture in the "
    "middle. Give a numeric depth interval in centimeters no wider than 5 cm. Use exact "
    "whole-centimeter endpoints, for example 35-40 cm. Choose the narrowest interval you "
    "can support from the visible evidence; do not use depth categories or a broad range. "
    "If you are unsure, still give your single best 5 cm interval rather than a wider one. "
    "If the depth cannot be judged from what is visible, reply X. Do not explain. Reply only "
    "in the format LOW-HIGH cm (for example 35-40 cm), or X.")
#: "Do not explain" added 2026-10-19: with 3 frames (C2) the model wrote a
#: step-by-step analysis and was cut off before naming a level in 16 of the
#: first 145 shots; the other conditions answered in one token.

SINGLE_INTRO = "This is a frame from a video of a flood."
MULTI_INTRO = ("These are {n} frames from ONE continuous camera shot of a flood, in time order. "
               "The water depth does not change during the shot; use whichever frame shows it best.")
TEXT_INTRO = ("The video's uploader wrote the text below. It may be inaccurate, exaggerated, or "
              "about other parts of the video; the picture is what counts.")
HEIGHTS_INTRO = ("For scale, typical heights above the ground of common objects:\n{table}\n"
                 "Use anything standing in the water whose height you know to judge the depth.")
BOXES_INTRO = ("Numbered boxes have been drawn on the picture around objects that a detector found; "
               "they may help as references. The detector can be wrong or miss things.\n{listing}")
NO_BOXES = "An object detector found no reference objects in this picture."


@lru_cache(maxsize=4096)
def _load(path: str):
    from PIL import Image
    image = Image.open(PROJECT / path).convert("RGB")
    scale = IMAGE_LONG_SIDE / max(image.size)
    return image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)


def frame_image(item: dict, j: int):
    return _load(item["frames"][j]["path"]).copy()


def text_block(item: dict) -> str:
    desc = item.get("description") or ""
    if len(desc) > DESCRIPTION_MAX_CHARS:
        desc = desc[:DESCRIPTION_MAX_CHARS] + " [...]"
    tags = ", ".join((item.get("tags") or [])[:TAGS_MAX])
    return (f"{TEXT_INTRO}\nTitle: {item.get('title') or ''}\nTags: {tags or '(none)'}\n"
            f"Description: {desc or '(none)'}")


def heights_block() -> str:
    return HEIGHTS_INTRO.format(table=reference_table())


# -- C5 drawing ---------------------------------------------------------------

BOX_COLOURS = ["#ff2d2d", "#ffd400", "#00e5ff", "#7cff00", "#ff00d4", "#ff8c00", "#ffffff", "#9b5cff"]


def draw_boxes(image, boxes: list[dict]):
    """Numbered boxes on a copy. `boxes`: [{"label", "score", "box": [x0,y0,x1,y1] in 0-1}]."""
    from PIL import ImageDraw, ImageFont
    out = image.copy()
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.load_default(size=max(14, out.width // 32))
    except TypeError:                                   # Pillow without sized default font
        font = ImageFont.load_default()
    width = max(2, out.width // 240)
    for n, b in enumerate(boxes, start=1):
        colour = BOX_COLOURS[(n - 1) % len(BOX_COLOURS)]
        x0, y0, x1, y1 = (b["box"][0] * out.width, b["box"][1] * out.height,
                          b["box"][2] * out.width, b["box"][3] * out.height)
        draw.rectangle([x0, y0, x1, y1], outline=colour, width=width)
        tag = str(n)
        tx0, ty0, tx1, ty1 = draw.textbbox((x0, y0), tag, font=font)
        draw.rectangle([x0, y0, x0 + (tx1 - tx0) + 6, y0 + (ty1 - ty0) + 6], fill=colour)
        draw.text((x0 + 3, y0 + 1), tag, fill="black", font=font)
    return out


def box_listing(boxes: list[dict]) -> str:
    return "\n".join(f"[{n}] {b['label']}" for n, b in enumerate(boxes, start=1))


# -- builders -----------------------------------------------------------------

def build_c1(item: dict, j: int | None = None, refs=None):
    j = item["key_frame"] if j is None else j
    return [SINGLE_INTRO, frame_image(item, j), QUESTION], {"frames": [j]}


def build_c2(item: dict, j=None, refs=None):
    js = [f["j"] for f in item["frames"]]
    return ([MULTI_INTRO.format(n=len(js)), *(frame_image(item, k) for k in js), QUESTION],
            {"frames": js})


def build_c3(item: dict, j=None, refs=None):
    k = item["key_frame"]
    return [SINGLE_INTRO, frame_image(item, k), text_block(item), QUESTION], {"frames": [k]}


def build_c4(item: dict, j=None, refs=None):
    k = item["key_frame"]
    return [SINGLE_INTRO, frame_image(item, k), heights_block(), QUESTION], {"frames": [k]}


def build_c5(item: dict, j=None, refs: dict | None = None):
    """`refs`: detect_refs.py's row for this item's key frame. Required --
    the runner skips C5 for an item whose detections are not cached yet."""
    if refs is None:
        raise ValueError(f"C5 needs detections for {item['item_id']}; run detect_refs.py first")
    k = item["key_frame"]
    boxes = refs["boxes"]
    image = frame_image(item, k)
    if boxes:
        image = draw_boxes(image, boxes)
        note = BOXES_INTRO.format(listing=box_listing(boxes))
    else:
        note = NO_BOXES
    return ([SINGLE_INTRO, image, note, heights_block(), QUESTION],
            {"frames": [k], "n_boxes": len(boxes), "detector": refs.get("detector")})


CONDITIONS = {"C1": build_c1, "C2": build_c2, "C3": build_c3, "C4": build_c4, "C5": build_c5}

#: The fixed text of each condition, hashed into its prompt id.
_TEMPLATES = {
    "C1": [SINGLE_INTRO, QUESTION],
    "C2": [MULTI_INTRO, QUESTION],
    "C3": [SINGLE_INTRO, TEXT_INTRO, QUESTION, str(DESCRIPTION_MAX_CHARS), str(TAGS_MAX)],
    "C4": [SINGLE_INTRO, heights_block(), QUESTION],
    "C5": [SINGLE_INTRO, BOXES_INTRO, NO_BOXES, heights_block(), QUESTION],
}


def prompt_id(condition: str) -> str:
    text = "\n\x00\n".join(_TEMPLATES[condition] + [str(IMAGE_LONG_SIDE)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def prompt_text(condition: str) -> str:
    """The condition's fixed text, for the ledger and for reading."""
    return "\n---\n".join(_TEMPLATES[condition])

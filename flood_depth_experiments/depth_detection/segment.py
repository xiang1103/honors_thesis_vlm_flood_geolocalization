"""Pixel masks for the geometry depth pipeline: water, dry ground, objects, wheels.

Design: docs/water_depth_segmentation.md, "Segmentation: what it must find".
The masks only say WHAT a pixel is; where it is in 3D comes from MoGe-2
(geometry_depth.py). SAM 3 (facebook/sam3, gated; access granted 2026-11-03)
finds every instance of a text phrase and masks it directly. Grounding DINO +
SAM 2.1 was the backend until then; it was dropped 2026-11-04 (owner: SAM 3
only, to save compute) -- its pilot rows stay in frames.jsonl under their
own config id.

Classes are queried separately (one phrase at a time) so a "road" mask can
never be labelled "car". Overlaps resolve in a fixed order: objects, then
water, then ground. Objects win over water because a water mask drawn around a
half-submerged car tends to swallow the car, and the car's lower edge is the
contact we want. A road mask spans the flooded part of the road too, so dry
ground is whatever ground is left after objects and water are removed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

SAM3_ID = "facebook/sam3"

#: One query per class, phrases separated by periods; SAM 3 gets them one by one.
QUERIES = {
    "water": "flood water. muddy water. water.",
    "ground": "road. street. sidewalk. pavement. grass. parking lot. ground.",
    "object": ("car. truck. bus. van. person. wall. building. curb. pole. fence. "
               "tree trunk. stairs. door. fire hydrant. mailbox. traffic cone. traffic barrel. "
               "concrete barrier. guardrail. bollard. trash can."),
    "wheel": "tire. wheel.",
}
SAM3_THRESHOLD = 0.40
MAX_BOXES = 12          # per class


def segmenter_id() -> str:
    cfg = json.dumps(["sam3", QUERIES, SAM3_THRESHOLD, MAX_BOXES])
    return "sam3:" + hashlib.sha256(cfg.encode()).hexdigest()[:8]


@dataclass
class Masks:
    water: np.ndarray        # bool HxW
    ground: np.ndarray       # dry ground: ground minus water minus objects
    objects: np.ndarray      # anything standing in or near the water
    wheels: list[np.ndarray]  # one bool mask per detected wheel / tyre
    instances: list[tuple[str, float, np.ndarray]]  # every object + wheel instance: (phrase, score, mask)
    labels: dict[str, list[str]]  # class -> the phrases that fired, for the record


class Segmenter:
    def __init__(self, device: str) -> None:
        import torch
        from transformers import Sam3Model, Sam3Processor
        self.torch, self.device = torch, device
        self.sam_proc = Sam3Processor.from_pretrained(SAM3_ID)
        self.sam = Sam3Model.from_pretrained(SAM3_ID).to(device).eval()

    def instances(self, image, phrases: list[str],
                  threshold: float = SAM3_THRESHOLD) -> list[tuple[str, float, np.ndarray]]:
        """Every instance of every phrase as (phrase, score, mask).
        The image is encoded once and reused for all phrases."""
        img = self.sam_proc(images=image, return_tensors="pt").to(self.device)
        sizes = img["original_sizes"].tolist()
        out_list = []
        with self.torch.inference_mode():
            vision = self.sam.get_vision_features(pixel_values=img["pixel_values"])
            for phrase in phrases:
                text = self.sam_proc(text=phrase, return_tensors="pt").to(self.device)
                out = self.sam(vision_embeds=vision, input_ids=text["input_ids"],
                               attention_mask=text.get("attention_mask"))
                r = self.sam_proc.post_process_instance_segmentation(
                    out, threshold=threshold, mask_threshold=0.5, target_sizes=sizes)[0]
                for score, m in list(zip(r["scores"].tolist(), r["masks"]))[:MAX_BOXES]:
                    out_list.append((phrase, round(score, 3), m.cpu().numpy().astype(bool)))
        return out_list

    def _class(self, image, query: str) -> list[tuple[str, float, np.ndarray]]:
        return self.instances(image, [p.strip() for p in query.split(".") if p.strip()])

    def __call__(self, image) -> Masks:
        h, w = image.height, image.width
        found = {c: self._class(image, q) for c, q in QUERIES.items()}

        def union(c: str) -> np.ndarray:
            ms = [m for _, _, m in found[c]]
            return np.logical_or.reduce(ms) if ms else np.zeros((h, w), bool)

        objects = union("object") | union("wheel")
        water = union("water") & ~objects
        ground = union("ground") & ~water & ~objects
        return Masks(water=water, ground=ground, objects=objects,
                     wheels=[m for _, _, m in found["wheel"] if m.any()],
                     instances=[x for x in found["object"] + found["wheel"] if x[2].any()],
                     labels={c: sorted({p for p, _, _ in f}) for c, f in found.items()})

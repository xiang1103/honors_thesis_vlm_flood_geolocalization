"""Pixel masks for the geometry depth pipeline: water, dry ground, objects, wheels.

Design: docs/water_depth_segmentation.md, "Segmentation: what it must find".
The masks only say WHAT a pixel is; where it is in 3D comes from MoGe-2
(geometry_depth.py). Two backends behind one call, `Segmenter(frame) -> Masks`:

  gdino-sam2  (default) Grounding DINO base (local weights, the C5 detector)
              finds boxes for a text query; SAM 2.1 turns each box into a mask.
  sam3        SAM 3 with the same text queries, masks directly. facebook/sam3 is
              GATED on Hugging Face: request access at
              https://huggingface.co/facebook/sam3 with the account behind
              HF_TOKEN, then pass --segmenter sam3.

Classes are queried separately (one detector call each) so a "road" box can
never be labelled "car". Overlaps resolve in a fixed order: objects, then
water, then ground. Objects win over water because a water box drawn around a
half-submerged car tends to swallow the car, and the car's lower edge is the
contact we want. A road box spans the flooded part of the road too, so dry
ground is whatever ground is left after objects and water are removed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

GDINO_PATH = "/home/liu47/models/grounding-dino-base"
SAM2_ID = "facebook/sam2.1-hiera-large"
SAM3_ID = "facebook/sam3"

#: One query per class; Grounding DINO's format (lower case, each phrase ends
#: in a period). SAM 3 gets the phrases one by one.
QUERIES = {
    "water": "flood water. muddy water. water.",
    "ground": "road. street. sidewalk. pavement. grass. parking lot. ground.",
    "object": ("car. truck. bus. van. person. wall. building. curb. pole. fence. "
               "tree trunk. stairs. door. fire hydrant. mailbox."),
    "wheel": "tire. wheel.",
}
BOX_THRESHOLD = 0.30
TEXT_THRESHOLD = 0.25
SAM3_THRESHOLD = 0.40
MAX_BOXES = 12          # per class


def segmenter_id(backend: str) -> str:
    cfg = json.dumps([backend, QUERIES, BOX_THRESHOLD, TEXT_THRESHOLD, SAM3_THRESHOLD, MAX_BOXES])
    return f"{backend}:" + hashlib.sha256(cfg.encode()).hexdigest()[:8]


@dataclass
class Masks:
    water: np.ndarray        # bool HxW
    ground: np.ndarray       # dry ground: ground minus water minus objects
    objects: np.ndarray      # anything standing in or near the water
    wheels: list[np.ndarray]  # one bool mask per detected wheel / tyre
    labels: dict[str, list[str]]  # class -> the phrases that fired, for the record


class Segmenter:
    def __init__(self, backend: str, device: str) -> None:
        import torch
        self.torch, self.device, self.backend = torch, device, backend
        if backend == "gdino-sam2":
            from transformers import (AutoModelForZeroShotObjectDetection, AutoProcessor,
                                      Sam2Model, Sam2Processor)
            self.det_proc = AutoProcessor.from_pretrained(GDINO_PATH, local_files_only=True)
            self.det = AutoModelForZeroShotObjectDetection.from_pretrained(
                GDINO_PATH, local_files_only=True).to(device).eval()
            self.sam_proc = Sam2Processor.from_pretrained(SAM2_ID)
            self.sam = Sam2Model.from_pretrained(SAM2_ID).to(device).eval()
        elif backend == "sam3":
            from transformers import Sam3Model, Sam3Processor
            self.sam_proc = Sam3Processor.from_pretrained(SAM3_ID)
            self.sam = Sam3Model.from_pretrained(SAM3_ID).to(device).eval()
        else:
            raise ValueError(f"unknown segmenter {backend!r}")

    # -- gdino-sam2 ---------------------------------------------------------
    def _boxes(self, image, query: str) -> tuple[list[list[float]], list[str]]:
        inputs = self.det_proc(images=image, text=query, return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            out = self.det(**inputs)
        r = self.det_proc.post_process_grounded_object_detection(
            out, inputs["input_ids"], threshold=BOX_THRESHOLD, text_threshold=TEXT_THRESHOLD,
            target_sizes=[(image.height, image.width)])[0]
        labels = r.get("text_labels", r.get("labels"))
        order = r["scores"].argsort(descending=True)[:MAX_BOXES].tolist()
        return [r["boxes"][i].tolist() for i in order], [str(labels[i]).strip() for i in order]

    def _box_masks(self, image, boxes: list[list[float]]) -> list[np.ndarray]:
        if not boxes:
            return []
        inputs = self.sam_proc(images=image, input_boxes=[boxes], return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            out = self.sam(**inputs, multimask_output=False)
        masks = self.sam_proc.post_process_masks(out.pred_masks.cpu(), inputs["original_sizes"])[0]
        return [m[0].numpy().astype(bool) for m in masks]

    def _class_gdino(self, image, query: str) -> tuple[list[np.ndarray], list[str]]:
        boxes, labels = self._boxes(image, query)
        return self._box_masks(image, boxes), labels

    # -- sam3 ---------------------------------------------------------------
    def instances(self, image, phrases: list[str],
                  threshold: float = SAM3_THRESHOLD) -> list[tuple[str, float, np.ndarray]]:
        """SAM 3 only: every instance of every phrase as (phrase, score, mask).
        The image is encoded once and reused for all phrases."""
        if self.backend != "sam3":
            raise ValueError("instances() needs the sam3 backend")
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

    def _class_sam3(self, image, query: str) -> tuple[list[np.ndarray], list[str]]:
        found = self.instances(image, [p.strip() for p in query.split(".") if p.strip()])
        return [m for _, _, m in found], [p for p, _, _ in found]

    def __call__(self, image) -> Masks:
        h, w = image.height, image.width
        get = self._class_gdino if self.backend == "gdino-sam2" else self._class_sam3
        found = {c: get(image, q) for c, q in QUERIES.items()}

        def union(ms: list[np.ndarray]) -> np.ndarray:
            return np.logical_or.reduce(ms) if ms else np.zeros((h, w), bool)

        objects = union(found["object"][0] + found["wheel"][0])
        water = union(found["water"][0]) & ~objects
        ground = union(found["ground"][0]) & ~water & ~objects
        return Masks(water=water, ground=ground, objects=objects,
                     wheels=[m for m in found["wheel"][0] if m.any()],
                     labels={c: sorted(set(l)) for c, (_, l) in found.items()})

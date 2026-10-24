"""Train / val / test assignment -- a hook, not yet used (PLAN.md, "Splits").

Splits are by VIDEO, never by item or frame: shots of one video share the
place, the camera and often the water, so a frame-level split leaks. Every
scheme maps a video_id to a split name; items inherit their video's split.

Schemes:
  all             everything is "all" (the default while there is no training)
  video_hash      deterministic from sha256(video_id): stable as items are added,
                  needs no stored state. Ratios via --split-ratios train,val,test
  file:<path>     a JSON {video_id: split} written by hand or by another script --
                  the way to do event- or channel-held-out splits (channel_title is
                  no longer stored in youtube_videos.json, owner's trim 2026-10-07,
                  so a channel split needs it re-fetched first). Videos missing
                  from the file get "unassigned".
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEFAULT_RATIOS = (0.6, 0.1, 0.3)
NAMES = ("train", "val", "test")


def _video_hash_split(video_id: str, ratios: tuple[float, ...]) -> str:
    u = int(hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:8], 16) / 2 ** 32
    total = sum(ratios)
    edge = 0.0
    for name, r in zip(NAMES, ratios):
        edge += r / total
        if u < edge:
            return name
    return NAMES[len(ratios) - 1]


def assign(items: list[dict], scheme: str = "all",
           ratios: tuple[float, ...] = DEFAULT_RATIOS) -> dict[str, str]:
    """item_id -> split name."""
    if scheme == "all":
        return {it["item_id"]: "all" for it in items}
    if scheme == "video_hash":
        return {it["item_id"]: _video_hash_split(it["video_id"], ratios) for it in items}
    if scheme.startswith("file:"):
        mapping = json.loads(Path(scheme[5:]).read_text(encoding="utf-8"))
        return {it["item_id"]: mapping.get(it["video_id"], "unassigned") for it in items}
    raise ValueError(f"unknown split scheme {scheme!r}: use all, video_hash or file:<path>")

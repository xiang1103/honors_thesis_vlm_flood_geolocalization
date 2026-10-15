"""Shot-boundary methods under test, shared by every experiment script.

A method maps a local video file to a list of shots [(start_s, end_s), ...].
`cuts()` turns shots into cut times (the start of every shot but the first).

Kept (owner, 2026-10-14):
  content     PySceneDetect ContentDetector, threshold 27 (the default; 25 was
              tried and LOST real cuts with the default merge filter)
  transnetv2  TransNetV2, PyTorch port `transnetv2-pytorch` (original weights),
              CPU, transition probability > 0.5 (its default)
Dropped: PySceneDetect adaptive, hash, histogram, threshold; OmniShotCut v1.5
(owner, 2026-10-15, after it missed more cuts than TransNetV2 on both test
videos) -- see
scraping/video_scraping/CLAUDE.md, "Experiments".
"""
from __future__ import annotations

from pathlib import Path

Shots = list[tuple[float, float]]

#: TransNetV2's own default: a frame is a transition if its probability > 0.5.
TRANSNET_THRESHOLD = 0.5


def content(path: Path, fps: float) -> Shots:
    from scenedetect import ContentDetector, detect
    return [(round(s.get_seconds(), 2), round(e.get_seconds(), 2))
            for s, e in detect(str(path), ContentDetector(threshold=27))]


def transnetv2(path: Path, fps: float) -> Shots:
    """predict_video returns, per frame, the probability that the frame is a
    shot transition; predictions_to_scenes turns runs of non-transition frames
    into shots [first_frame, last_frame] (inclusive). Frames labelled as
    transition belong to neither neighbour, so shots can have gaps."""
    import torch
    from transnetv2_pytorch import TransNetV2
    model = TransNetV2(device="cpu")
    model.eval()
    with torch.no_grad():
        _, single_frame, _ = model.predict_video(str(path), quiet=True)
    probs = single_frame.cpu().numpy() if hasattr(single_frame, "cpu") else single_frame
    scenes = model.predictions_to_scenes(probs.reshape(-1), threshold=TRANSNET_THRESHOLD)
    return [(round(a / fps, 2), round((b + 1) / fps, 2)) for a, b in scenes]


#: name -> (description, function). Order is display order.
METHODS = {
    "content": ("PySceneDetect ContentDetector(threshold=27) -- hard cuts", content),
    "transnetv2": (f"TransNetV2 (neural, PyTorch port, CPU), transition probability > "
                   f"{TRANSNET_THRESHOLD} -- cuts and gradual transitions", transnetv2),
}


def run(name: str, path: Path, fps: float, duration_s: float) -> Shots:
    shots = METHODS[name][1](path, fps)
    return shots or [(0.0, round(duration_s, 2))]      # no cut found: one shot


def cuts(shots: Shots) -> list[float]:
    """Cut times: where every shot after the first begins."""
    return [s for s, _ in shots[1:]]

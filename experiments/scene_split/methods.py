"""Shot-boundary methods under test, shared by every experiment script.

A method maps a local video file to a list of shots [(start_s, end_s), ...].
`cuts()` turns shots into cut times (the start of every shot but the first).

Kept (owner, 2026-10-14):
  transnetv2  TransNetV2, PyTorch port `transnetv2-pytorch` (original weights),
              CPU, transition probability > 0.5 (its default)
Added (owner, 2026-10-15):
  autoshot    AutoShot (Zhu et al., CVPRW 2023, MIT), TransNetV2's architecture
              family found by neural architecture search, trained on short-form
              video. Code + checkpoint in /home/liu47/models/AutoShot (outside the
              repo; checkpoint ckpt_0_200_0.pth from the README's Google Drive
              mirror, loaded with weights_only=True). Authors' inference
              exactly: 48x27 frames, 100-frame windows, middle 50 kept, sigmoid,
              their best-F1 threshold 0.296. Freest GPU if one has room, else CPU.
Dropped (owner's decisions; reasons in scraping/video_scraping/CLAUDE.md,
"Experiments"): PySceneDetect entirely -- adaptive, hash, histogram and
threshold on 2026-10-14, content (threshold 27) on 2026-10-15, once the
transnetv2 + autoshot union covered every cut it had found; OmniShotCut v1.5
(2026-10-15, missed more cuts than TransNetV2 on both test videos).
"""
from __future__ import annotations

from pathlib import Path

Shots = list[tuple[float, float]]

#: TransNetV2's own default: a frame is a transition if its probability > 0.5.
TRANSNET_THRESHOLD = 0.5


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


AUTOSHOT_DIR = Path("/home/liu47/models/AutoShot")
#: The authors' best-F1 threshold on their SHOT dataset (their eval script).
AUTOSHOT_THRESHOLD = 0.296
AUTOSHOT_MIN_FREE_GIB = 4


def frames_to_shots(transition: "np.ndarray", threshold: float, fps: float) -> Shots:
    """Per-frame transition flags -> shots of non-transition frames, the
    convention TransNetV2 uses (transition frames belong to no shot)."""
    flags = (transition > threshold).astype("uint8")
    shots, start, prev = [], 0, 0
    for i, t in enumerate(flags):
        if prev == 1 and t == 0:
            start = i
        if prev == 0 and t == 1 and i != 0:
            shots.append((start, i))
        prev = t
    if len(flags) and flags[-1] == 0:
        shots.append((start, len(flags)))
    return [(round(a / fps, 2), round(b / fps, 2)) for a, b in shots]


def autoshot(path: Path, fps: float) -> Shots:
    import sys
    import ffmpeg
    import numpy as np
    import torch
    sys.path.insert(0, str(AUTOSHOT_DIR))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "verification"))
    from verify_video_frames import pick_free_gpu   # also sets CUDA_DEVICE_ORDER=PCI_BUS_ID
    from supernet_flattransf_3_8_8_8_13_12_0_16_60 import TransNetV2Supernet

    gpu = pick_free_gpu(AUTOSHOT_MIN_FREE_GIB)
    device = torch.device(f"cuda:{gpu}" if gpu is not None else "cpu")
    if gpu is not None:
        # The model file builds some tensors with a bare .cuda() (= the
        # current device), so the chosen GPU must also be the current one.
        torch.cuda.set_device(gpu)
    model = TransNetV2Supernet().eval()
    state = torch.load(AUTOSHOT_DIR / "ckpt_0_200_0.pth", map_location="cpu", weights_only=True)["net"]
    model.load_state_dict(state)                       # all 90 tensors match (checked 2026-10-15)
    model.to(device)

    raw, _ = (ffmpeg.input(str(path))
              .output("pipe:", format="rawvideo", pix_fmt="rgb24", s="48x27")
              .run(capture_stdout=True, capture_stderr=True))
    frames = np.frombuffer(raw, np.uint8).reshape([-1, 27, 48, 3])
    # utils.get_batches: pad 25 frames before, 25 + remainder after; windows of
    # 100 every 50; keep each window's middle 50 predictions.
    remainder = (50 - len(frames) % 50) % 50
    padded = np.concatenate([frames[:1]] * 25 + [frames] + [frames[-1:]] * (remainder + 25), 0)
    preds = []
    with torch.no_grad():
        for i in range(0, len(padded) - 50, 50):
            batch = torch.from_numpy(padded[i:i + 100].transpose((3, 0, 1, 2))[np.newaxis].astype("float32")).to(device)
            out = model(batch)
            out = out[0] if isinstance(out, tuple) else out
            preds.append(torch.sigmoid(out[0]).reshape(-1).cpu().numpy()[25:75])
    transition = np.concatenate(preds)[:len(frames)]
    return frames_to_shots(transition, AUTOSHOT_THRESHOLD, fps)


#: name -> (description, function). Order is display order.
METHODS = {
    "transnetv2": (f"TransNetV2 (neural, PyTorch port, CPU), transition probability > "
                   f"{TRANSNET_THRESHOLD} -- cuts and gradual transitions", transnetv2),
    "autoshot": (f"AutoShot (CVPRW 2023, NAS-designed, trained on short videos), transition probability > "
                 f"{AUTOSHOT_THRESHOLD} -- cuts and gradual transitions", autoshot),
}


#: Combining rule, owner's design 2026-10-15 ("more shots the better; never
#: lose a shot"):
#:   * split wherever EITHER model splits (every shot start / end of both is a
#:     boundary); boundaries within SAME_CUT_S are the same cut found by both
#:     models a frame or two apart, and count once;
#:   * a stretch is a gap (left out, like a transition) only if BOTH models
#:     leave it out; if either puts it in a shot, it stays in a shot;
#:   * short shots are NOT merged into neighbours; only "0 s" shots -- shorter
#:     than MIN_SHOT_FRAMES frames, i.e. a single frame (AutoShot makes these
#:     inside smooth transitions) -- are removed, leaving a gap. Measured in
#:     frames, not seconds: at 24 fps one frame is 0.042 s, which a 0.05 s
#:     cut-off on rounded times let through.
SAME_CUT_S = 0.2
MIN_SHOT_FRAMES = 2


def combine(*shot_lists: Shots, fps: float) -> Shots:
    def inside(t: float, shots: Shots) -> bool:
        return any(a <= t < b for a, b in shots)

    # Every shot edge of every model, with the model it came from.
    points = sorted({(t, m) for m, shots in enumerate(shot_lists) for a, b in shots for t in (a, b)})
    bounds: list[float] = []
    cluster_models: set[int] = set()
    for t, m in points:
        # The same cut found by two DIFFERENT models a frame or two apart is
        # one boundary. Two edges from the SAME model are never merged -- that
        # would delete that model's (short) shot.
        if bounds and t - bounds[-1] <= SAME_CUT_S and m not in cluster_models:
            cluster_models.add(m)
        elif not bounds or t != bounds[-1]:
            bounds.append(t)
            cluster_models = {m}
    combined = []
    for a, b in zip(bounds, bounds[1:]):
        mid = (a + b) / 2
        if round((b - a) * fps) >= MIN_SHOT_FRAMES and any(inside(mid, shots) for shots in shot_lists):
            combined.append((round(a, 2), round(b, 2)))
    return combined


#: Methods computed from other methods' shots (no extra model run):
#: name -> (description, inputs, function).
DERIVED = {
    "union": ("UNION of transnetv2 + autoshot: split wherever either splits (cuts within "
              f"{SAME_CUT_S} s count once), gap only where both leave a gap, no merging, "
              f"1-frame shots removed", ("transnetv2", "autoshot"), combine),
}


def run(name: str, path: Path, fps: float, duration_s: float) -> Shots:
    shots = METHODS[name][1](path, fps)
    return shots or [(0.0, round(duration_s, 2))]      # no cut found: one shot


def cuts(shots: Shots) -> list[float]:
    """Cut times: where every shot after the first begins."""
    return [s for s, _ in shots[1:]]

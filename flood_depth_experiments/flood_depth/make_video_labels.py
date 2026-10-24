#!/usr/bin/env python3
"""Per-VIDEO depth labels from a full run, for display (owner, 2026-10-21).

run_all.py writes one line per SHOT; a display wants one entry per VIDEO:
which shots were judged, when they play, what each condition answered, and
one summary label per condition. This regroups the answers -- it asks the
model nothing -- and writes <run dir>/video_labels.json:

    {"run": {model, prompt_ids, created, answers: "cm" | "level", built_at},
     "videos": {
       "<video_id>": {
         "shots": [{"s": 9, "t": [38.33, 43.63],
                    "C1": ["35-40 cm", "35-40 cm", "40-45 cm"], "C2": "35-40 cm", ...}, ...],
         "label": {"C1": {"deepest": "40-45 cm", "deepest_shot": 9, "median_cm": 37.5,
                          "answered": 4, "x": 1}, "C2": {...}, ...}}}}

  s / t        shot number (as in the frame files) and its [start, end] in
               seconds -- play it with embed_url?start=<int(start)>&end=<ceil(end)>
  C1           the 3 frames' answers (key frame = index 1); C2-C5 one answer each
  label        per condition, over the video's shots (C1: key-frame answers):
    deepest      the deepest answer -- the scale measures depth "at the deepest
                 place where it can be judged", so a video's label is its
                 deepest shot, not an average
    deepest_shot which shot that was
    median_cm    median of the answers' midpoints (cm runs only; null otherwise)
    median_level median level (L0-L5 runs only)
    answered / x shots with a numeric answer / shots answered X; nulls are neither

Works for both runs: cm intervals (full_5cm/) and L0-L5 levels (full/).
run_all.py rebuilds it when a shard finishes; run it by hand any time:

    python3 experiments/flood_depth/make_video_labels.py                          # full_5cm
    python3 experiments/flood_depth/make_video_labels.py --dir scrape_data/experiments/flood_depth/full
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from levels import ORDINAL, UNKNOWN, level_index, midpoint_cm, parse_depth_range  # noqa: E402
from sample_items import OUT_DIR  # noqa: E402

CONDS = ["C1", "C2", "C3", "C4", "C5"]
KEY_FRAME = 1


def shot_times(video_ids: set[str]) -> dict[tuple[str, int], list[float]]:
    """(video_id, shot number) -> [start, end], from the shot-scan ledger."""
    from scan_shots import LEDGER_DIR, read_ledger
    out = {}
    for vid, row in read_ledger(LEDGER_DIR).items():
        if vid in video_ids:
            for k, shot in enumerate(row.get("shots", [])):
                out[(vid, k + 1)] = [shot["start"], shot["end"]]
    return out


def depth_key(answer: str | None, kind: str) -> float | None:
    """Sortable depth of one answer; None for X / null / unparsable."""
    if kind == "level":
        return level_index(answer)
    return midpoint_cm(parse_depth_range(answer or ""))


def summarise(shots: list[dict], cond: str, kind: str) -> dict:
    answers = [(s["s"], s[cond][KEY_FRAME] if cond == "C1" else s[cond]) for s in shots]
    scored = [(depth_key(a, kind), n, a) for n, a in answers]
    numeric = [(d, n, a) for d, n, a in scored if d is not None]
    out = {"deepest": None, "deepest_shot": None, "answered": len(numeric),
           "x": sum(a == UNKNOWN for _, a in answers)}
    if numeric:
        d, n, a = max(numeric, key=lambda x: (x[0], -x[1]))
        out.update(deepest=a, deepest_shot=n)
    if kind == "cm":
        out["median_cm"] = round(statistics.median(d for d, _, _ in numeric), 1) if numeric else None
    else:
        out["median_level"] = ORDINAL[statistics.median_low(int(d) for d, _, _ in numeric)] if numeric else None
    return out


def build(run_dir: Path) -> dict:
    from run_all import read_answers
    head = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    answers = read_answers(run_dir)
    kind = "level" if any(a in ORDINAL for line in answers.values() for a in line["C1"]) else "cm"
    by_video: dict[str, list[dict]] = {}
    for iid, line in answers.items():
        vid, shot = iid.rsplit(":shot", 1)
        by_video.setdefault(vid, []).append({"s": int(shot), **{c: line[c] for c in CONDS}})
    times = shot_times(set(by_video))
    videos = {}
    for vid in sorted(by_video):
        shots = sorted(by_video[vid], key=lambda s: s["s"])
        shots = [{"s": s["s"], "t": times.get((vid, s["s"])), **{c: s[c] for c in CONDS}} for s in shots]
        videos[vid] = {"shots": shots, "label": {c: summarise(shots, c, kind) for c in CONDS}}
    return {"run": {"model": head["model"], "prompt_ids": head["prompt_ids"], "created": head.get("created"),
                    "answers": kind, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "videos": len(videos), "shots": len(answers)},
            "videos": videos}


def write(run_dir: Path) -> Path:
    data = build(run_dir)
    path = run_dir / "video_labels.json"
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")      # shards may finish at the same time
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"), ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    print(f"video labels: {data['run']['videos']} videos, {data['run']['shots']} shots -> {path}", flush=True)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=OUT_DIR / "full_5cm", help="a run_all.py output folder")
    args = ap.parse_args()
    write(args.dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

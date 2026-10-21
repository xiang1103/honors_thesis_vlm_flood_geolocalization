#!/usr/bin/env python3
"""Score every condition: against human labels when they exist, label-free always.

Inputs (all in scrape_data/experiments/flood_depth/):
  items.jsonl         the benchmark items (sample_items.py)
  predictions.jsonl   model answers (run_conditions.py)
  labels.jsonl        human labels, OPTIONAL -- format in PLAN.md, "Label file":
                      {"item_id", "level": "L0".."L5" | "X", "annotator", "labelled_at", ...}

A "system" is one (condition, prompt_id, model, thinking); each is scored on
the answer it gave for the item (C1: the key frame; C2: all frames together).

Label-free (works today, no labels):
  - answer distribution, X rate, unparsed replies
  - C1 within-shot spread in estimated centimeters
  - numeric condition agreement (interval overlap and midpoint difference)

With legacy labels (items whose gold label is a level, L0-L5):
  - QWK (quadratic-weighted kappa, primary), MAE in levels, exact accuracy,
    within-1 accuracy, macro-F1; coverage = share where the system gave a level
  - gold X: how often the system also says X
  - inter-annotator agreement when >= 2 annotators labelled the same items
    (the human ceiling for QWK)

    python3 experiments/flood_depth/evaluate.py
    python3 experiments/flood_depth/evaluate.py --split-scheme video_hash --split test
    python3 experiments/flood_depth/evaluate.py --annotator alice      # one annotator as gold
    python3 experiments/flood_depth/evaluate.py --full     # run_all.py's compact answers, every shot
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from levels import ANSWERS, ORDINAL, UNKNOWN, interval_cm, level_index, parse_depth_range, within_5cm  # noqa: E402
from run_conditions import read_predictions  # noqa: E402
from sample_items import OUT_DIR, read_items  # noqa: E402
import splits  # noqa: E402

LABELS_FILE = OUT_DIR / "labels.jsonl"
RESULTS_FILE = OUT_DIR / "results.json"           # pilot; --full writes full/results.json


# -- labels -------------------------------------------------------------------

def read_labels(path: Path = LABELS_FILE) -> dict[str, dict[str, dict]]:
    """item_id -> annotator -> that annotator's LATEST label."""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("level") not in ANSWERS:
            continue
        prev = out[row["item_id"]].get(row["annotator"])
        if prev is None or row.get("labelled_at", "") >= prev.get("labelled_at", ""):
            out[row["item_id"]][row["annotator"]] = row
    return dict(out)


def gold(labels: dict[str, dict[str, dict]], annotator: str | None) -> dict[str, str]:
    """item_id -> gold level. One annotator if named; otherwise one label is
    used as is, and several are resolved by majority, else the lower median
    of their levels (if all are levels), else X."""
    out = {}
    for item_id, by_ann in labels.items():
        if annotator is not None:
            if annotator in by_ann:
                out[item_id] = by_ann[annotator]["level"]
            continue
        levels = [r["level"] for r in by_ann.values()]
        top, count = Counter(levels).most_common(1)[0]
        if count > len(levels) / 2:
            out[item_id] = top
        elif all(lv in ORDINAL for lv in levels):
            out[item_id] = ORDINAL[statistics.median_low([level_index(lv) for lv in levels])]
        else:
            out[item_id] = UNKNOWN
    return out


# -- metrics ------------------------------------------------------------------

def qwk(a: list[int], b: list[int]) -> float | None:
    if len(a) < 2:
        return None
    from sklearn.metrics import cohen_kappa_score
    k = cohen_kappa_score(a, b, labels=list(range(len(ORDINAL))), weights="quadratic")
    return None if k != k else round(float(k), 3)          # NaN when one side is constant


def ordinal_scores(gold_idx: list[int], pred_idx: list[int]) -> dict:
    from sklearn.metrics import f1_score
    n = len(gold_idx)
    if n == 0:
        return {"n": 0}
    diffs = [abs(g - p) for g, p in zip(gold_idx, pred_idx)]
    return {"n": n, "qwk": qwk(gold_idx, pred_idx),
            "mae": round(sum(diffs) / n, 3),
            "exact": round(sum(d == 0 for d in diffs) / n, 3),
            "within1": round(sum(d <= 1 for d in diffs) / n, 3),
            "macro_f1": round(float(f1_score(gold_idx, pred_idx, labels=list(range(len(ORDINAL))),
                                             average="macro", zero_division=0)), 3)}


def score_against_gold(answers: dict[str, str | None], gold_levels: dict[str, str]) -> dict:
    labelled = [i for i in gold_levels if i in answers]
    gold_ord = [i for i in labelled if gold_levels[i] in ORDINAL]
    gold_x = [i for i in labelled if gold_levels[i] == UNKNOWN]
    answered = [i for i in gold_ord if answers[i] in ORDINAL]
    s = ordinal_scores([level_index(gold_levels[i]) for i in answered],
                       [level_index(answers[i]) for i in answered])
    s["coverage"] = round(len(answered) / len(gold_ord), 3) if gold_ord else None
    s["gold_levels"] = len(gold_ord)
    s["gold_x"] = len(gold_x)
    s["x_on_gold_x"] = round(sum(answers[i] == UNKNOWN for i in gold_x) / len(gold_x), 3) if gold_x else None
    return s


def c1_consistency(preds: list[dict], key_frames: dict[str, int]) -> dict:
    """Per-shot spread of numeric C1 estimates across frames."""
    by_item: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for p in preds:
        if p["condition"] == "C1" and len(p["frames"]) == 1:
            interval = interval_cm(parse_depth_range(p["answer"] or ""))
            if interval:
                by_item[p["item_id"]].append(interval)
    multi = {i: v for i, v in by_item.items() if len(v) >= 2}
    if not multi:
        return {"shots": 0}
    ranges = [max((a + b) / 2 for a, b in v) - min((a + b) / 2 for a, b in v)
              for v in multi.values()]
    flips = sum(len(set(v)) > 1 for v in multi.values())
    return {"shots": len(multi), "flip_rate": round(flips / len(multi), 3),
            "mean_range_cm": round(sum(ranges) / len(ranges), 2),
            "max_range_cm": round(max(ranges), 2)}


def range_agreement(a: dict[str, str | None], b: dict[str, str | None]) -> dict:
    """Compare numeric systems by interval overlap and midpoint distance."""
    pairs = []
    for item_id in a.keys() & b.keys():
        ia = interval_cm(parse_depth_range(a[item_id] or ""))
        ib = interval_cm(parse_depth_range(b[item_id] or ""))
        if not ia or not ib:
            continue
        pairs.append((*ia, *ib))
    if not pairs:
        return {"n": 0, "overlap": None, "mean_midpoint_difference_cm": None}
    overlaps = [max(al, bl) <= min(ah, bh) for al, ah, bl, bh in pairs]
    diffs = [abs((al + ah - bl - bh) / 2) for al, ah, bl, bh in pairs]
    return {"n": len(pairs), "overlap": round(sum(overlaps) / len(pairs), 3),
            "mean_midpoint_difference_cm": round(sum(diffs) / len(diffs), 2)}


# -- main ---------------------------------------------------------------------

def compact_predictions(folder: Path) -> tuple[list[dict], list[dict]]:
    """run_all.py's compact lines expanded into prediction rows, plus the
    items they cover (key frame 1, 3 frames). Prompt ids etc. from run.json."""
    from run_all import read_answers
    head = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    preds, items = [], []
    for iid, line in read_answers(folder).items():
        items.append({"item_id": iid, "video_id": iid.rsplit(":", 1)[0], "key_frame": head["key_frame"]})
        base = {"item_id": iid, "model": head["model"], "thinking": head["thinking"]}
        for j, answer in enumerate(line["C1"]):
            preds.append({**base, "condition": "C1", "frames": [j], "prompt_id": head["prompt_ids"]["C1"],
                          "answer": answer})
        for cond in ("C2", "C3", "C4", "C5"):
            frames = [0, 1, 2] if cond == "C2" else [head["key_frame"]]
            preds.append({**base, "condition": cond, "frames": frames, "prompt_id": head["prompt_ids"][cond],
                          "answer": line[cond]})
    return preds, items


def system_name(p: dict) -> str:
    return f"{p['condition']}|{p['prompt_id']}|{p['model']}|{'think' if p.get('thinking') else 'nothink'}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items", type=Path, default=OUT_DIR / "items.jsonl")
    ap.add_argument("--labels", type=Path, default=LABELS_FILE)
    ap.add_argument("--annotator", default=None, help="use this annotator's labels as gold")
    ap.add_argument("--split-scheme", default="all", help="all | video_hash | file:<path> (see splits.py)")
    ap.add_argument("--split-ratios", default=",".join(map(str, splits.DEFAULT_RATIOS)))
    ap.add_argument("--split", default=None, help="score only this split (e.g. test)")
    ap.add_argument("--full", action="store_true", help="score run_all.py's compact answers (every shot)")
    ap.add_argument("--full-dir", type=Path, default=OUT_DIR / "full")
    args = ap.parse_args()

    if args.full:
        all_preds, items = compact_predictions(args.full_dir)
    else:
        items = read_items(args.items)
        all_preds = list(read_predictions().values())
    split_of = splits.assign(items, args.split_scheme, tuple(float(x) for x in args.split_ratios.split(",")))
    if args.split:
        items = [it for it in items if split_of[it["item_id"]] == args.split]
    item_ids = {it["item_id"] for it in items}
    key_frames = {it["item_id"]: it["key_frame"] for it in items}
    preds = [p for p in all_preds if p["item_id"] in item_ids]

    # the answer each system gives for each item
    systems: dict[str, dict[str, str | None]] = defaultdict(dict)
    per_system_preds: dict[str, list[dict]] = defaultdict(list)
    for p in preds:
        name = system_name(p)
        per_system_preds[name].append(p)
        if p["condition"] == "C2" or p["frames"] == [key_frames[p["item_id"]]]:
            systems[name][p["item_id"]] = p["answer"]

    labels = read_labels(args.labels)
    labels = {i: v for i, v in labels.items() if i in item_ids}
    gold_levels = gold(labels, args.annotator)

    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "items": len(items), "split_scheme": args.split_scheme, "split": args.split,
                    "labelled_items": len(gold_levels), "annotator": args.annotator, "systems": {}}
    print(f"{len(items)} items{f' (split {args.split})' if args.split else ''}, "
          f"{len(gold_levels)} labelled, {len(systems)} systems")

    for name in sorted(systems):
        answers = systems[name]
        dist = Counter(a or "unparsed" for a in answers.values())
        entry = {"answered_items": len(answers),
                 "distribution": dict(sorted(dist.items())),
                 "x_rate": round(dist[UNKNOWN] / len(answers), 3) if answers else None}
        widths = [within_5cm(parse_depth_range(a or "")) for a in answers.values()]
        numeric = [w for w in widths if w is not None]
        if numeric:      # cm-interval prompts: how often the 5 cm request was met
            entry["numeric_answers"] = len(numeric)
            entry["within_5cm_rate"] = round(sum(numeric) / len(numeric), 3)
        if name.startswith("C1|"):
            entry["c1_within_shot"] = c1_consistency(per_system_preds[name], key_frames)
        if gold_levels:
            numeric_predictions = any(parse_depth_range(a or "") not in (None, UNKNOWN)
                                      for a in answers.values())
            entry["vs_gold"] = ({"not_comparable": "Numeric depth intervals require numeric human labels; "
                                                    "the available labels are ordinal L0-L5."}
                                if numeric_predictions else score_against_gold(answers, gold_levels))
        report["systems"][name] = entry

    # agreement between systems, label-free
    agree = {}
    for a, b in itertools.combinations(sorted(systems), 2):
        a_numeric = any(parse_depth_range(v or "") not in (None, UNKNOWN) for v in systems[a].values())
        b_numeric = any(parse_depth_range(v or "") not in (None, UNKNOWN) for v in systems[b].values())
        if a_numeric and b_numeric:
            agree[f"{a} ~ {b}"] = range_agreement(systems[a], systems[b])
        else:
            both = [i for i in systems[a] if systems[a][i] in ORDINAL and systems[b].get(i) in ORDINAL]
            agree[f"{a} ~ {b}"] = {"n": len(both), "qwk": qwk([level_index(systems[a][i]) for i in both],
                                                              [level_index(systems[b][i]) for i in both])}
    report["system_agreement"] = agree

    # human ceiling
    annotators = sorted({ann for by_ann in labels.values() for ann in by_ann})
    iaa = {}
    for a, b in itertools.combinations(annotators, 2):
        both = [i for i, by in labels.items() if by.get(a, {}).get("level") in ORDINAL
                and by.get(b, {}).get("level") in ORDINAL]
        if both:
            iaa[f"{a} ~ {b}"] = ordinal_scores([level_index(labels[i][a]["level"]) for i in both],
                                               [level_index(labels[i][b]["level"]) for i in both])
    report["inter_annotator"] = iaa

    # print a compact table
    for name, e in report["systems"].items():
        line = f"  {name}: n={e['answered_items']} dist={e['distribution']}"
        if "c1_within_shot" in e:
            line += f" within-shot={e['c1_within_shot']}"
        if "vs_gold" in e:
            g = e["vs_gold"]
            line += (f"\n      vs gold: QWK {g.get('qwk')} MAE {g.get('mae')} exact {g.get('exact')} "
                     f"within1 {g.get('within1')} F1 {g.get('macro_f1')} coverage {g.get('coverage')} "
                     f"X-on-X {g.get('x_on_gold_x')}")
        print(line)
    for pair, s in agree.items():
        print(f"  agree {pair}: {s}")
    for pair, s in iaa.items():
        print(f"  annotators {pair}: {s}")

    results_file = args.full_dir / "results.json" if args.full else RESULTS_FILE
    results_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = results_file.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, results_file)
    print(f"-> {results_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

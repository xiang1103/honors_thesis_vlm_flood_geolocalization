#!/usr/bin/env python3
"""Convert the crawl's JSONL into a plain JSON array (and filter it).

Why two formats: the crawler streams JSONL because it appends+flushes per
article, so an interrupted run still leaves a valid file and `--resume` can
read back what's done. A JSON array can't be appended to safely -- it needs the
closing bracket, so a crash mid-crawl corrupts it. Once the crawl is finished
that constraint is gone, and a plain JSON array is nicer to consume.

Also normalises key order to title, outlet, date, url, text, images, ... which
matters because a re-scored or hand-edited record can otherwise drift.

Usage
-----
  PY=/home/liu47/miniconda3/bin/python3

  $PY export.py ../data/cbs_flood.jsonl                      # -> ../data/cbs_flood.json
  $PY export.py ../data/cbs_flood.jsonl -o ../data/vlm.json --verified-only --min-images 1
  $PY export.py ../data/cbs_flood.jsonl --in-place           # just reorder the JSONL
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape import FIELD_ORDER, ordered   # noqa: E402


def read_jsonl(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="input .jsonl")
    ap.add_argument("-o", "--out", help="output .json (default: src with .json)")
    ap.add_argument("--verified-only", action="store_true",
                    help="keep only flood_verified records")
    ap.add_argument("--min-images", type=int, default=0,
                    help="keep only records with >= N captioned images")
    ap.add_argument("--min-score", type=float, default=None,
                    help="keep only records with flood_score >= X")
    ap.add_argument("--indent", type=int, default=2, help="0 for compact")
    ap.add_argument("--in-place", action="store_true",
                    help="rewrite the JSONL itself with normalised key order")
    args = ap.parse_args()

    recs = [ordered(r) for r in read_jsonl(args.src)]
    total = len(recs)

    if args.in_place:
        with open(args.src, "w") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"reordered {total} records in place -> {args.src}")
        print(f"key order: {', '.join(FIELD_ORDER)}")
        return

    kept = recs
    if args.verified_only:
        kept = [r for r in kept if r.get("flood_verified")]
    if args.min_images:
        kept = [r for r in kept if len(r.get("images") or []) >= args.min_images]
    if args.min_score is not None:
        kept = [r for r in kept if (r.get("flood_score") or 0) >= args.min_score]

    out = args.out or os.path.splitext(args.src)[0] + ".json"
    with open(out, "w") as f:
        json.dump(kept, f, ensure_ascii=False,
                  indent=(args.indent or None))
        f.write("\n")

    imgs = sum(len(r.get("images") or []) for r in kept)
    print(f"{args.src}: {total} records -> kept {len(kept)} ({imgs} captioned images)")
    print(f"wrote {out}  ({os.path.getsize(out):,} bytes)")
    print(f"key order: {', '.join(FIELD_ORDER)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Find duplicate images in data/mycoast.json by CONTENT and drop the repeats.

`dedupe.py` works on the verifier's flat one-row-per-image file. MyCoast is
shaped differently -- one row per REPORT with its images nested -- so a
duplicate here is an image entry, not a row. Reports are never dropped: a
report whose every image was a repeat keeps its coordinates, time and text
with an empty `images` list, like the reports that never had any.

Fingerprints are the same as dedupe.py's (`local_vlm.backend.image_digests`):
sha256 of the decoded RGB pixels, plus a 64-bit dHash. They are cached by URL
in the same --cache file, so dedupe.py and this script never fetch an image
twice between them.

  --dry-run   fetch + fingerprint + report, change nothing   (do this first)
  --apply     drop exact duplicates and rewrite the file

Exact duplicates (identical pixels) are dropped. Near duplicates (small dHash
distance: resizes, re-encodes) are REPORTED only -- see the --drop-near note in
CLAUDE.md for why dHash alone is not trusted to delete anything.

Survivor of a duplicate group: the image in the EARLIEST report (date_utc, then
report_id), first position within it -- the first upload is the original, a
later report reusing the photo is the copy. Each dropped image is recorded on
its report under `duplicate_images` with the `record_id` it duplicates, so
review decisions keyed on the dropped record_id can still be traced.

Images that could not be fetched have no digest and are never dropped.

mycoast_scrape.py merges fresh reports OVER existing ones, so a re-scrape
restores the dropped images. Re-run this afterwards; with the cache it costs
only the new images.

    python3 verification/dedupe_mycoast.py --dry-run
    python3 verification/dedupe_mycoast.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dedupe import DEFAULT_CACHE, fetch_digests, near_duplicate_clusters  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT / "data" / "mycoast.json"


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    """Atomic replace, fsynced first (NFS), partial .tmp removed on failure."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--near-distance", type=int, default=4,
                    help="dHash Hamming distance reported as a near duplicate "
                         "(default 4). Never dropped.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    reports = json.loads(args.input.read_text(encoding="utf-8"))
    # (report, position, image) for every nested image, in survivor order.
    ordered = sorted(reports, key=lambda r: (r.get("date_utc") or "9999",
                                             r["report_id"]))
    entries = [(r, i, img) for r in ordered
               for i, img in enumerate(r.get("images") or [])]
    print(f"reports: {len(reports)}, images: {len(entries)}")

    cache = {}
    if args.cache.exists():
        cache = json.loads(args.cache.read_text(encoding="utf-8"))
    urls = sorted({img["image_url"] for _, _, img in entries})
    cache = fetch_digests(urls, args.workers, args.timeout, cache)
    args.cache.write_text(json.dumps(cache), encoding="utf-8")
    print(f"digest cache: {args.cache}")

    by_sha: dict[str, list[tuple]] = defaultdict(list)
    unhashed = 0
    for entry in entries:
        digests = cache.get(entry[2]["image_url"])
        if digests:
            entry[2]["image_sha256"] = digests["image_sha256"]
            entry[2]["image_dhash"] = digests["image_dhash"]
            by_sha[digests["image_sha256"]].append(entry)
        else:
            unhashed += 1

    dup_groups = [g for g in by_sha.values() if len(g) > 1]
    same_report = sum(len({id(r) for r, _, _ in g}) == 1 for g in dup_groups)
    droppable = sum(len(g) - 1 for g in dup_groups)
    print(f"\ndistinct image contents : {len(by_sha)}")
    print(f"images with no digest   : {unhashed} (kept: could not be fetched)")
    print(f"exact-duplicate groups  : {len(dup_groups)} "
          f"({same_report} within one report, "
          f"{len(dup_groups) - same_report} across reports)")
    print(f"images droppable as exact duplicates: {droppable}")
    for group in dup_groups[:5]:
        print("   ", [(r["report_id"], img["record_id"]) for r, _, img in group])

    # Near duplicates among the survivors, reported only.
    survivors = {sha: g[0] for sha, g in by_sha.items()}
    cluster_of = near_duplicate_clusters(
        [(sha, e[2]["image_dhash"]) for sha, e in survivors.items()],
        args.near_distance)
    clusters: dict[str, list[str]] = defaultdict(list)
    for sha, rep in cluster_of.items():
        clusters[rep].append(sha)
    multi = sorted((v for v in clusters.values() if len(v) > 1),
                   key=len, reverse=True)
    print(f"near-duplicate clusters (dHash distance <= {args.near_distance}): "
          f"{len(multi)}, {sum(len(v) - 1 for v in multi)} further images"
          f"  [reported only -- inspect before trusting]")
    for group in multi[:5]:
        print("    ", [survivors[s][2]["image_url"] for s in group[:3]])

    if args.dry_run:
        print("\ndry run: nothing written.")
        return 0

    drop: dict[int, dict] = {}          # id(image) -> the record it duplicates
    for group in dup_groups:
        keeper = group[0][2]
        for _, _, img in group[1:]:
            drop[id(img)] = keeper
    touched = 0
    for report in reports:
        images = report.get("images") or []
        kept = [img for img in images if id(img) not in drop]
        if len(kept) == len(images):
            continue
        touched += 1
        seen = {d["image_url"] for d in report.get("duplicate_images", [])}
        report.setdefault("duplicate_images", []).extend(
            {"image_url": img["image_url"],
             "record_id": img.get("record_id"),
             "image_sha256": img["image_sha256"],
             "duplicate_of_record_id": drop[id(img)].get("record_id")}
            for img in images
            if id(img) in drop and img["image_url"] not in seen)
        report["images"] = kept
        report["image_count"] = len(kept)

    write_json(args.input, reports)
    left = sum(len(r.get("images") or []) for r in reports)
    print(f"\ndropped {len(drop)} duplicate images from {touched} reports; "
          f"{left} images remain across {len(reports)} reports")
    print(f"written: {args.input}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Find duplicate images by CONTENT and drop the duplicate rows.

The occurrence id keys `(article_url, image_url)`. It is an identity key for
resume, not a similarity key -- so the same photograph served from two URLs
(a CDN re-crop, a wire photo syndicated to several outlets) is two rows and
nothing in the pipeline notices. This fetches every image, fingerprints it, and
collapses rows that are the same picture.

Two passes, deliberately separate:

  --dry-run   fetch + fingerprint + report, change nothing   (do this first)
  --apply     drop duplicate rows and rewrite the file

Exact duplicates -- identical sha256 of the decoded pixels, i.e. the same file
served from two URLs -- are always dropped. Near duplicates (small dHash
Hamming distance: re-crops, resizes, re-compressions) are reported by default
and dropped only with --drop-near, because "close enough to be the same photo"
is a threshold judgement about the dataset rather than a fact.

Rows whose image could not be fetched keep no digest and are never dropped:
absence of proof is not proof of duplication.

Digests are cached to --cache so re-running costs no network.

    python3 verification/dedupe.py --dry-run
    python3 verification/dedupe.py --apply --drop-near
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_vlm.backend import FETCH_HEADERS, image_digests  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = PROJECT / "data" / "image_verification.json"
DEFAULT_CACHE = PROJECT / "data" / "image_hashes.json"


def fetch_digests(urls: list[str], workers: int, timeout: int,
                  cache: dict[str, Any]) -> dict[str, Any]:
    """URL -> digests, skipping anything already cached. Failures are cached
    too (as None) so a dead URL is not re-fetched on every run."""
    import requests
    from PIL import Image
    import io

    todo = [u for u in urls if u not in cache]
    if not todo:
        print(f"all {len(urls)} URLs already fingerprinted (cache hit)")
        return cache

    print(f"fingerprinting {len(todo)} of {len(urls)} URLs "
          f"({len(urls) - len(todo)} cached) with {workers} workers ...")
    session = requests.Session()

    def one(url: str):
        response = session.get(url, headers=FETCH_HEADERS, timeout=timeout)
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
        return image_digests(image)

    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, u): u for u in todo}
        for future in as_completed(futures):
            url = futures[future]
            try:
                cache[url] = future.result()
            except Exception as exc:
                cache[url] = None
                failed += 1
                if failed <= 5:
                    print(f"  fetch failed: {type(exc).__name__} {str(exc)[:70]}")
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(todo)} ({failed} failed)", flush=True)
    print(f"fingerprinted {done - failed}, failed {failed}")
    return cache


def keep_rank(row: dict[str, Any]) -> tuple:
    """Which row survives a duplicate group. Deterministic on purpose, so the
    same input always yields the same file: prefer a row whose ARTICLE passed
    the text flood check, then the earliest article, then the lowest id."""
    return (
        0 if row.get("article_flood_verified") else 1,
        str(row.get("article_date") or "9999"),
        str(row.get("occurrence_id") or ""),
    )


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def near_duplicate_clusters(items: list[tuple[str, str]], max_distance: int
                            ) -> dict[str, str]:
    """Group keys whose dHashes are within `max_distance`, via union-find.

    Union-find, not pairwise grouping, because near-duplication is transitive
    in practice: a 1200px crop, an 800px crop and a 400px thumbnail of one
    photo should collapse to ONE cluster even if the widest and narrowest are
    more than `max_distance` apart. Returns key -> cluster representative.
    """
    parent = {k: k for k, _ in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    values = [(k, int(h, 16)) for k, h in items]
    # O(n^2) on ~8k items is ~32M cheap int ops -- seconds, and exact. Blocking
    # on high bits would be faster but silently misses pairs that differ there.
    for i in range(len(values)):
        ki, vi = values[i]
        for j in range(i + 1, len(values)):
            kj, vj = values[j]
            if (vi ^ vj).bit_count() <= max_distance:
                union(ki, kj)
    return {k: find(k) for k, _ in items}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--near-distance", type=int, default=4,
                    help="dHash Hamming distance counted as a near duplicate "
                         "in the report (default 4). Never dropped.")
    ap.add_argument("--drop-near", action="store_true",
                    help="also drop near duplicates (crops/resizes) within "
                         "--near-distance, keeping one row per cluster.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    payload = json.loads(args.results.read_text(encoding="utf-8"))
    rows = payload["results"]
    print(f"rows: {len(rows)}")

    cache = {}
    if args.cache.exists():
        cache = json.loads(args.cache.read_text(encoding="utf-8"))
    urls = sorted({r["image_url"] for r in rows})
    cache = fetch_digests(urls, args.workers, args.timeout, cache)
    args.cache.write_text(json.dumps(cache), encoding="utf-8")
    print(f"digest cache: {args.cache}")

    # Attach digests; a row whose image could not be fetched keeps no digest
    # and is never treated as a duplicate -- absence of proof is not proof.
    for row in rows:
        digests = cache.get(row["image_url"])
        if digests:
            row["image_sha256"] = digests["image_sha256"]
            row["image_dhash"] = digests["image_dhash"]

    by_sha: dict[str, list[dict]] = defaultdict(list)
    unhashed = 0
    for row in rows:
        sha = row.get("image_sha256")
        if sha:
            by_sha[sha].append(row)
        else:
            unhashed += 1

    dup_groups = {s: g for s, g in by_sha.items() if len(g) > 1}
    droppable = sum(len(g) - 1 for g in dup_groups.values())
    print(f"\ndistinct image contents : {len(by_sha)}")
    print(f"rows with no digest     : {unhashed} (kept: could not be fetched)")
    print(f"exact-duplicate groups  : {len(dup_groups)}")
    print(f"rows droppable as exact duplicates: {droppable}")

    disagree = [g for g in dup_groups.values()
                if len({r["answer"] for r in g}) > 1]
    print(f"duplicate groups whose answers DISAGREE: {len(disagree)}"
          f"{'  <- same picture, different verdict' if disagree else ''}")
    for group in disagree[:3]:
        print("   ", [(r["outlet"], r["answer"]) for r in group])

    # Near duplicates, reported only.
    survivors = {}
    for sha, group in by_sha.items():
        survivors[sha] = min(group, key=keep_rank)
    hashes = [(s, r["image_dhash"]) for s, r in survivors.items()
              if r.get("image_dhash")]
    cluster_of = near_duplicate_clusters(hashes, args.near_distance)
    clusters: dict[str, list[str]] = defaultdict(list)
    for sha, rep in cluster_of.items():
        clusters[rep].append(sha)
    near_extra = sum(len(v) - 1 for v in clusters.values())
    multi = [v for v in clusters.values() if len(v) > 1]
    print(f"near-duplicate clusters (dHash distance <= {args.near_distance}): "
          f"{len(multi)}, collapsing {near_extra} further rows"
          f"{'' if args.drop_near else '  [reported only -- pass --drop-near]'}")
    biggest = sorted(multi, key=len, reverse=True)[:3]
    for group in biggest:
        caps = [survivors[s].get("caption", "")[:46] for s in group[:2]]
        print(f"    cluster of {len(group)}: {caps}")

    if args.dry_run:
        print("\ndry run: nothing written.")
        return 0

    final_keep = dict(survivors)
    if args.drop_near:
        # One survivor per near-duplicate cluster, chosen by the same rule.
        best: dict[str, dict] = {}
        for sha, row in survivors.items():
            rep = cluster_of.get(sha, sha)
            if rep not in best or keep_rank(row) < keep_rank(best[rep]):
                best[rep] = row
        final_keep = {r["image_sha256"]: r for r in best.values()}

    keep_ids = {id(r) for r in final_keep.values()}
    kept = [r for r in rows
            if not r.get("image_sha256") or id(r) in keep_ids]
    print(f"\nkeeping {len(kept)} rows, dropping {len(rows) - len(kept)}"
          f"  (exact{' + near' if args.drop_near else ''})")

    cur = payload.get("current_prompt") or payload.get("prompt")
    on_cur = sum(r.get("prompt") == cur for r in kept)
    payload["results"] = kept
    payload["summary"].update({
        "stored_occurrences": len(kept),
        "on_current_prompt": on_cur,
        "on_earlier_prompt": len(kept) - on_cur,
        "distinct_prompts": len({r.get("prompt") for r in kept}),
        "yes": sum(r["answer"] == "yes" for r in kept),
        "no": sum(r["answer"] == "no" for r in kept),
        "deduplicated_by": ("image_sha256+image_dhash" if args.drop_near
                            else "image_sha256"),
        "near_duplicate_distance": args.near_distance if args.drop_near else None,
        "dropped_duplicates": len(rows) - len(kept),
    })
    tmp = args.results.with_name(args.results.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, args.results)
    print(f"written: {args.results}")
    print(f"summary: {payload['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

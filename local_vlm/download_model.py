#!/usr/bin/env python3
"""Fetch the vision model to local disk, once.

Downloads into a plain directory (not the HF cache) so the path is explicit and
greppable: every later load points at it with `local_files_only=True`, which
guarantees no silent network fallback if the directory is wrong.

    python3 local_vlm/download_model.py
    python3 local_vlm/download_model.py --repo Qwen/Qwen3.8-27B --dest /home/liu47/models/...

Resumable: re-running skips files already present, so an interrupted transfer
costs only what it had left.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from huggingface_hub import snapshot_download

DEFAULT_REPO = "Qwen/Qwen3.8-27B"
DEFAULT_DEST = Path("/home/liu47/models")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--dest", type=Path, default=None,
                    help="target directory (default: /home/liu47/models/<repo name>)")
    ap.add_argument("--workers", type=int, default=8,
                    help="parallel file downloads (default: 8)")
    args = ap.parse_args()

    dest = args.dest or (DEFAULT_DEST / args.repo.split("/")[-1])
    dest.mkdir(parents=True, exist_ok=True)

    free = shutil.disk_usage(dest).free
    print(f"repo        : {args.repo}")
    print(f"destination : {dest}")
    print(f"free space  : {free / 1e9:.1f} GB")
    if free < 70e9:
        print("WARNING: under 70 GB free; the model is ~56 GB plus transfer headroom.")

    path = snapshot_download(
        repo_id=args.repo,
        local_dir=str(dest),
        max_workers=args.workers,
    )
    total = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    print(f"\ndone: {path}")
    print(f"on disk: {total / 1e9:.1f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

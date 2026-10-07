#!/usr/bin/env python3
"""Write data/youtube_flood_videos.json: the flood-only video dataset.

    python3 scraping/video_scraping/export_flood_videos.py

A DERIVED file, regenerated in full from the master `data/youtube_videos.json`
(every video found, with labels) -- never edited, never merged into. The
master stays the scraper's working state and the source of truth; this is
the file to hand out or train on. `youtube_scrape.py` calls `export()` at the
end of every run, so the two never drift.

Same record format as the master (CLAUDE.md "REQUIRED data format for
VIDEOS"), same order (newest first). Which videos are in it is `is_flood()`,
owner's choice 2026-10-06/07:

  * `flood_text_relevant` -- the text score passes FLOOD_TEXT_THRESHOLD (0.3);
  * ALL flood videos, New York or not (`in_ny` is kept as a label);
  * `available` -- still public and embeddable; a video deleted since it was
    found cannot be watched, so it is not part of a dataset (it stays in the
    master with its labels).

When the visual check (`flood_visual`) exists, it belongs in `is_flood()`.
Changing the rule needs no API calls: re-run this script.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_DIR = HERE.parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scraping" / "api_based_scraping"))
sys.path.insert(0, str(PROJECT_DIR))
from gis_scrape import write_json  # noqa: E402

DEFAULT_INPUT = PROJECT_DIR / "data" / "youtube_videos.json"
DEFAULT_OUTPUT = PROJECT_DIR / "data" / "youtube_flood_videos.json"


def is_flood(video: dict[str, Any]) -> bool:
    return bool(video.get("flood_text_relevant")) and bool(video.get("available"))


def export(input_path: Path = DEFAULT_INPUT, output_path: Path | None = None) -> Path:
    """Write the flood-only file (and its metadata) from `input_path`.
    Default output sits next to the input, so a scratch run never touches data/."""
    output_path = output_path or input_path.with_name(
        input_path.name.replace("_videos.json", "_flood_videos.json"))
    rows = json.loads(input_path.read_text(encoding="utf-8"))
    flood = [r for r in rows if is_flood(r)]
    write_json(output_path, flood)
    print(f"wrote {output_path}: {len(flood)} flood videos of {len(rows)} "
          f"({sum(r.get('in_ny') is True for r in flood)} in NY)")

    import make_youtube_metadata                 # REQUIRED with every data-file change
    make_youtube_metadata.refresh(output_path)
    return output_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=None,
                    help=f"default: next to the input ({DEFAULT_OUTPUT.name} for the default input)")
    args = ap.parse_args()
    export(args.input, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

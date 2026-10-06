#!/usr/bin/env python3
"""Write data/youtube_videos_meta_data.json: a snapshot of the YouTube video set.

    python3 make_youtube_metadata.py

Every count comes from `data/youtube_videos.json` alone, so it describes what
is shipped, not what a run fetched. `youtube_scrape.py` calls `refresh()` at
the end of every run (CLAUDE.md: a scrape regenerates its metadata).

  all / available          videos, thumbnails, total duration, date range
  flood_text_score         videos per score band (signal 1); the cut is the reader's
  flood_visual             classified by the VLM yet, and how many said yes
  in_ny / in_nyc           true / false / unknown -- unknown is the common case
  ny_basis                 how often each New York signal fired
  by_category / by_year / top_channels / top_queries
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT / "scraping" / "video_scraping"))
from video_signals import FLOOD_TEXT_THRESHOLD  # noqa: E402
DEFAULT_INPUT = PROJECT / "data" / "youtube_videos.json"
DEFAULT_OUTPUT = PROJECT / "data" / "youtube_videos_meta_data.json"   # == refresh()'s derived default

#: Score bands, lower bound inclusive.
BANDS = [0.0, 0.2, 0.4, 0.6, 0.8]

NOTE = (
    "One row per YouTube video; no video bytes are stored, only metadata and i.ytimg.com "
    "thumbnails. flood_text_score is the text signal only (the video is ABOUT a flood); "
    "flood_visual is the VLM over thumbnails (flooding is VISIBLE). in_ny / in_nyc are "
    "null when unknown, which is not the same as false."
)


def tri(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return {"true": sum(r.get(key) is True for r in rows),
            "false": sum(r.get(key) is False for r in rows),
            "unknown": sum(r.get(key) is None for r in rows)}


def band(score: float) -> str:
    lo = max(b for b in BANDS if score >= b)
    hi = next((b for b in BANDS if b > lo), 1.0)
    return f"{lo:.1f}-{hi:.1f}"


def block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dates = sorted(r["published_utc"][:10] for r in rows if r.get("published_utc"))
    return {
        "videos": len(rows),
        "thumbnails": sum(len(r.get("thumbnails") or []) for r in rows),
        "hours": round(sum(r.get("duration_s") or 0 for r in rows) / 3600, 1),
        "earliest": dates[0] if dates else None,
        "latest": dates[-1] if dates else None,
    }


def build(rows: list[dict[str, Any]], source_file: str) -> dict[str, Any]:
    visual = [r["flood_visual"] for r in rows if r.get("flood_visual")]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": source_file,
        "note": NOTE,
        "all": block(rows),
        "available": block([r for r in rows if r.get("available")]),
        "flood_text_score": dict(sorted(Counter(band(r["flood_text_score"]) for r in rows).items())),
        "flood_text_relevant": sum(bool(r.get("flood_text_relevant")) for r in rows),
        "flood_text_threshold": FLOOD_TEXT_THRESHOLD,
        "hard_negative": sum(bool(r["flood_text_hits"]["negative"]) for r in rows),
        "after_known_ny_flood": sum(bool(r.get("flood_event_date")) for r in rows),
        "flood_visual": {
            "classified": len(visual),
            "yes": sum(v.get("flood") == "yes" for v in visual),
        },
        "in_ny": tri(rows, "in_ny"),
        "in_nyc": tri(rows, "in_nyc"),
        "with_coordinates": sum(r.get("lat") is not None for r in rows),
        "ny_basis": dict(Counter(b for r in rows for b in r.get("ny_basis") or []).most_common()),
        "thumbnails_without_digest": sum(not t.get("image_sha256")
                                         for r in rows for t in r.get("thumbnails") or []),
        "by_category": dict(Counter(r.get("category_id") or "(none)" for r in rows).most_common()),
        "by_year": dict(sorted(Counter((r.get("published_utc") or "?")[:4] for r in rows).items())),
        "top_channels": dict(Counter(r.get("channel_title") for r in rows).most_common(20)),
        "top_queries": dict(Counter(q for r in rows for q in r.get("queries") or []).most_common(20)),
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic replace, fsynced first (NFS), partial .tmp removed on failure."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def refresh(input_path: Path = DEFAULT_INPUT, output_path: Path | None = None) -> dict[str, Any]:
    """Metadata for `input_path`, written next to it as <stem>_meta_data.json
    unless `output_path` is given -- so a scratch run never touches data/."""
    output_path = output_path or input_path.with_name(f"{input_path.stem}_meta_data.json")
    rows = json.loads(input_path.read_text(encoding="utf-8"))
    try:
        source_file = str(input_path.resolve().relative_to(PROJECT))
    except ValueError:
        source_file = str(input_path)
    meta = build(rows, source_file)
    write_json(output_path, meta)
    a = meta["all"]
    print(f"wrote {output_path}: {a['videos']} videos, {a['thumbnails']} thumbnails, "
          f"in_ny {meta['in_ny']['true']}, {a['earliest']} .. {a['latest']}, "
          f"{meta['thumbnails_without_digest']} thumbnails without digest")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=None,
                    help=f"default: <input stem>_meta_data.json ({DEFAULT_OUTPUT.name} for the default input)")
    args = ap.parse_args()
    refresh(args.input, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

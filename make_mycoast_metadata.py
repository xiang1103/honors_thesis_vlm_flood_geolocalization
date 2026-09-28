#!/usr/bin/env python3
"""Write data/mycoast_meta_data.json: a snapshot of the MyCoast dataset.

    python3 make_mycoast_metadata.py

Every count comes from `data/mycoast.json` alone, AFTER dedupe -- so run it
last (`./scraping/update_mycoast.sh` does). Reproduces the hand-made file of
2026-09-24 field for field (checked against the 1,893-report snapshot it
described), so the key set and meanings are unchanged:

  all / in_nyc / outside_nyc   reports, images, and how photos spread over reports
  earliest / latest            from `local_time`, reports dated before 2000
                               excluded -- they are listed in `odd_timestamps`
  *_by_county                  REPORTS per `county`, most first
  duplicate_images_removed_by_scraper
                               images dedupe dropped as exact repeats of an
                               earlier report's photo (`duplicate_images`)
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent
DEFAULT_INPUT = PROJECT / "data" / "mycoast.json"
DEFAULT_OUTPUT = PROJECT / "data" / "mycoast_meta_data.json"

#: Reports whose local date is before this are timestamp errors (1935, ...),
#: not history: MyCoast began in 2011.
ODD_BEFORE = "2000"

NOTE = (
    "One MyCoast report can carry several photos, so images > reports. in_nyc comes "
    "from the report's lat/lon (nyc_basis='coordinates' on every record); 'outside_nyc' "
    "reports are elsewhere in New York State. Dates are local_time; reports dated before "
    "2000 are left out of earliest/latest and listed under odd_timestamps."
)


def is_odd(row: dict[str, Any]) -> bool:
    return (row.get("local_time") or "")[:4] < ODD_BEFORE


def block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = [len(r["images"]) for r in rows]
    multi = [n for n in counts if n > 1]
    dated = sorted(r["local_time"][:10] for r in rows if not is_odd(r))
    return {
        "reports": len(rows),
        "images": sum(counts),
        "reports_with_multiple_images": len(multi),
        "images_in_multi_image_reports": sum(multi),
        "mean_images_per_report": round(sum(counts) / len(rows), 2) if rows else 0,
        "images_per_report": {str(n): c for n, c in sorted(Counter(counts).items())},
        "earliest": dated[0] if dated else None,
        "latest": dated[-1] if dated else None,
    }


def by_county(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(r.get("county") or "(unknown)" for r in rows).most_common())


def build(rows: list[dict[str, Any]], source_file: str) -> dict[str, Any]:
    nyc = [r for r in rows if r.get("in_nyc")]
    rest = [r for r in rows if not r.get("in_nyc")]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": source_file,
        "note": NOTE,
        "all": block(rows),
        "in_nyc": block(nyc),
        "outside_nyc": block(rest),
        "nyc_by_county": by_county(nyc),
        "outside_nyc_by_county": by_county(rest),
        "by_report_type": dict(Counter(r.get("report_type") for r in rows).most_common()),
        "duplicate_images_removed_by_scraper": sum(len(r.get("duplicate_images") or []) for r in rows),
        # File order (newest first), as mycoast.json keeps it.
        "odd_timestamps": [r["report_id"] for r in rows if is_odd(r)],
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()

    rows = json.loads(args.input.read_text(encoding="utf-8"))
    try:
        source_file = str(args.input.resolve().relative_to(PROJECT))
    except ValueError:
        source_file = str(args.input)
    meta = build(rows, source_file)
    write_json(args.output, meta)
    a = meta["all"]
    print(f"wrote {args.output}: {a['reports']} reports, {a['images']} images, "
          f"{meta['in_nyc']['reports']} in NYC, {a['earliest']} .. {a['latest']}, "
          f"{len(meta['odd_timestamps'])} odd timestamps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

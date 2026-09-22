"""Compile `data/article_countries.json` into country totals ready to plot.

Two artefacts, because the two shapes answer different questions:

* `data/country_totals.json` -- one row per country, for a choropleth. Join it
  to a world map on `alpha_3` (Natural Earth and the standard world TopoJSON
  both key on it); `alpha_2` is there for ISO 3166-2 work later.
* `data/country_rows.csv` -- one row per (article, country), flat, for pandas
  and for any per-year or per-outlet cut.

SECONDARY MENTIONS ARE INCLUDED in both, by the owner's decision. An article
about Mali that also reports "flooding in neighbouring country Niger" counts
for both. `mention_rank` (0 = the first country the model listed, which is
reliably the article's subject) is carried on every row, so the narrower
"one country per article" view is a filter -- `mention_rank == 0` -- and never
needs the GPU again. The totals file reports both counts per country.

Unresolved names (Kosovo: ISO 3166-1 has no code for it) are kept in their own
block rather than dropped or forced into a neighbour, so the map data stays
clean and the omission stays visible.

    python3 verification/compile_countries.py
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, payload: Any) -> None:
    """Atomic, fsynced replace -- /home/liu47 is NFS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def compile_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One flat row per (article, country), secondary mentions included."""
    rows = []
    for article in results:
        for rank, country in enumerate(article.get("countries") or []):
            rows.append({
                "article_url": article["article_url"],
                "outlet": article.get("outlet") or "",
                "article_date": article.get("article_date") or "",
                "year": (article.get("article_date") or "")[:4],
                "alpha_2": country.get("alpha_2", ""),
                "alpha_3": country.get("alpha_3", ""),
                "country_name": country.get("country_name", ""),
                "raw_country": country.get("raw_country", ""),
                "region": country.get("region", ""),
                # 0 is the article's subject; >0 is a country the article
                # mentions as also flooded. Both are real, and this is the
                # column that lets a plot choose.
                "mention_rank": rank,
                "confidence": country.get("confidence", ""),
                "resolution": country.get("resolution", ""),
                "article_title": article.get("article_title") or "",
            })
    return rows


def compile_totals(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Per-country counts, plus the unresolved names and how often they appear."""
    by_country: dict[str, dict[str, Any]] = {}
    unresolved: collections.Counter[str] = collections.Counter()

    for row in rows:
        if not row["alpha_3"]:
            unresolved[row["raw_country"] or "(blank)"] += 1
            continue
        entry = by_country.setdefault(row["alpha_3"], {
            "alpha_3": row["alpha_3"],
            "alpha_2": row["alpha_2"],
            "country_name": row["country_name"],
            "articles": 0,          # every mention, primary + secondary
            "primary": 0,           # mention_rank == 0
            "secondary": 0,
            "outlets": collections.Counter(),
            "years": collections.Counter(),
        })
        entry["articles"] += 1
        entry["primary" if row["mention_rank"] == 0 else "secondary"] += 1
        entry["outlets"][row["outlet"]] += 1
        if row["year"]:
            entry["years"][row["year"]] += 1

    totals = []
    for entry in by_country.values():
        totals.append({
            **{k: v for k, v in entry.items() if k not in ("outlets", "years")},
            "top_outlets": dict(entry["outlets"].most_common(3)),
            "by_year": dict(sorted(entry["years"].items())),
        })
    totals.sort(key=lambda e: (-e["articles"], e["alpha_3"]))
    return totals, dict(unresolved.most_common())


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile country totals for plotting.")
    parser.add_argument("--results", type=Path,
                        default=ROOT / "data" / "article_countries.json")
    parser.add_argument("--totals", type=Path,
                        default=ROOT / "data" / "country_totals.json")
    parser.add_argument("--rows", type=Path,
                        default=ROOT / "data" / "country_rows.csv")
    args = parser.parse_args()

    if not args.results.exists():
        raise SystemExit(f"{args.results} does not exist -- run locate_articles.py first.")
    with args.results.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    results = payload.get("results")
    if not results:
        raise ValueError(f"{args.results} holds no results")

    rows = compile_rows(results)
    totals, unresolved = compile_totals(rows)

    fieldnames = list(rows[0].keys())
    args.rows.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.rows.with_name(args.rows.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.rows)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    summary = {
        "articles_judged": len(results),
        "articles_with_a_country": sum(1 for r in results if r.get("countries")),
        "mentions_total": len(rows),
        "mentions_primary": sum(1 for r in rows if r["mention_rank"] == 0),
        "mentions_secondary": sum(1 for r in rows if r["mention_rank"] > 0),
        "countries_resolved": len(totals),
        "mentions_unresolved": sum(unresolved.values()),
        "counts_include": "all mentions, primary and secondary",
        "join_key": "alpha_3",
    }
    write_json(args.totals, {
        "summary": summary,
        "unresolved": unresolved,
        "countries": totals,
    })

    print(json.dumps(summary, indent=2))
    print(f"\nWrote {args.totals}\nWrote {args.rows}")
    print("\nTop 15 by article mentions (primary + secondary):")
    for entry in totals[:15]:
        print(f"  {entry['alpha_3']}  {entry['articles']:5d}  "
              f"({entry['primary']} primary + {entry['secondary']} secondary)  "
              f"{entry['country_name']}")
    if unresolved:
        print(f"\nUnresolved, excluded from the map: {unresolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

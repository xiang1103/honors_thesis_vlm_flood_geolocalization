"""Generate `data/iso_3166.json`, the country reference table.

Run once. The table is a standard -- 249 entries that change every few years --
so it is CHECKED IN as data rather than looked up at runtime. That keeps the
pipeline free of a `pycountry` dependency: `country_codes.py` reads this JSON
and imports nothing.

    pip install pycountry
    python3 verification/make_iso_table.py

The starter alias file (`data/country_aliases.json`) is written only if it does
not already exist -- it is hand-maintained from then on, and regenerating the
ISO table must never silently discard aliases added by hand.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Newsroom wordings that ISO does not carry, name -> alpha_2. Every code here
#: is checked against the generated table before the file is written, so a typo
#: fails the run instead of quietly producing a country that resolves to
#: nothing. Sub-national entries are deliberate: at country granularity a flood
#: in Scotland IS a flood in GB, and news names the constituent country far
#: more often than "United Kingdom".
STARTER_ALIASES: dict[str, str] = {
    # United States
    "usa": "US", "us": "US", "u s": "US", "u s a": "US", "america": "US",
    "united states of america": "US",
    # United Kingdom and its constituent countries
    "uk": "GB", "u k": "GB", "britain": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    # ISO spells these differently from every newsroom
    "vietnam": "VN", "laos": "LA", "south korea": "KR", "north korea": "KP",
    "czech republic": "CZ", "turkey": "TR", "ivory coast": "CI",
    "cape verde": "CV", "swaziland": "SZ", "macedonia": "MK", "burma": "MM",
    "east timor": "TL", "vatican": "VA", "vatican city": "VA",
    "macau": "MO", "taiwan": "TW", "palestine": "PS", "brunei": "BN",
    "micronesia": "FM", "gaza": "PS", "west bank": "PS", "uae": "AE", "holland": "NL", "bosnia": "BA",
    "syria": "SY", "iran": "IR", "russia": "RU", "bolivia": "BO",
    "venezuela": "VE", "tanzania": "TZ", "moldova": "MD",
    # The two Congos: news uses initialisms for one and the city for both.
    "drc": "CD", "dr congo": "CD", "democratic republic of congo": "CD",
    "democratic republic of the congo": "CD", "congo kinshasa": "CD",
    "congo brazzaville": "CG", "republic of the congo": "CG",
}


def build(pycountry_module: Any) -> list[dict[str, str]]:
    rows = []
    for country in pycountry_module.countries:
        row = {
            "alpha_2": country.alpha_2,
            "alpha_3": country.alpha_3,
            "numeric": country.numeric,
            "name": country.name,
        }
        for optional in ("official_name", "common_name"):
            value = getattr(country, optional, None)
            if value:
                row[optional] = value
        rows.append(row)
    return sorted(rows, key=lambda r: r["alpha_2"])


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


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Generate the ISO 3166-1 table.")
    parser.add_argument("--output", type=Path, default=root / "data" / "iso_3166.json")
    parser.add_argument("--aliases", type=Path,
                        default=root / "data" / "country_aliases.json")
    args = parser.parse_args()

    try:
        import pycountry
    except ImportError:
        raise SystemExit("pycountry is not installed: pip install pycountry")

    countries = build(pycountry)
    known = {c["alpha_2"] for c in countries}
    unknown = sorted({v for v in STARTER_ALIASES.values() if v not in known})
    if unknown:
        raise SystemExit(f"STARTER_ALIASES point at codes ISO does not have: {unknown}")

    write_json(args.output, {
        "standard": "ISO 3166-1",
        "source": f"pycountry {getattr(pycountry, '__version__', 'unknown')}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(countries),
        "countries": countries,
    })
    print(f"Wrote {args.output}  ({len(countries)} countries)")

    if args.aliases.exists():
        print(f"Kept {args.aliases} (hand-maintained; not overwritten)")
    else:
        write_json(args.aliases, {
            "_comment": ("Newsroom wordings ISO does not carry, normalised name "
                         "-> alpha_2. Add a line whenever an unresolved country "
                         "shows up in the locate_articles summary, then re-run "
                         "country_codes.py -- no GPU pass needed."),
            "countries": dict(sorted(STARTER_ALIASES.items())),
        })
        print(f"Wrote {args.aliases}  ({len(STARTER_ALIASES)} aliases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

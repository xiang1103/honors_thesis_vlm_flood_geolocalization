"""Turn the country names an LLM wrote into ISO 3166-1 codes.

This is the second half of the country pass and it deliberately contains no
model: `locate_articles.py` reads prose and emits NAMES, this turns names into
codes by exact lookup. They are split because they fail differently and cost
differently -- the model is good at "which countries flooded" and the table is
perfect at codes, and re-running this over an existing result file takes
seconds and no GPU. So an alias added by hand is one re-run away from being
applied to every article, instead of an hour of inference.

    python3 verification/make_iso_table.py                     # once
    python3 verification/country_codes.py data/article_countries.json

The cascade, first hit wins, per country:

  exact       the normalised name is an ISO name, official name or common name
  alias       it is in data/country_aliases.json ("britain", "drc", "usa")
  code_guess  the name resolved to nothing but the model's own alpha-2 is real
  unresolved  none of the above -- recorded, never guessed at

`name_over_code` is not a separate step but a flag: the name resolved AND the
model's code resolved AND they disagreed. The NAME wins, because that is the
word that was in the article; the code is the model's recall. Every such row is
counted in the summary, because a systematic disagreement means the prompt is
wrong, not the table.

Nothing is fuzzy-matched. An unmatched name stays unmatched and visible in
`summary.unresolved`, sorted by how many rows it blocks -- that list is the
work queue for the alias file. A silent 87%-similar match is how Western
Australia quietly becomes South Australia.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TABLE = ROOT / "data" / "iso_3166.json"
DEFAULT_ALIASES = ROOT / "data" / "country_aliases.json"

#: Wordings that wrap a country name rather than naming a different place.
#: Stripped before the second lookup attempt, so "the Republic of Zambia" and
#: "Zambia" are the same key.
QUALIFIER_RE = re.compile(
    r"^(the\s+)?((federal|bolivarian|islamic|plurinational|united|socialist|"
    r"democratic|people s|federative|oriental|co operative|independent|"
    r"arab|hellenic|kingdom|republic|state|states|commonwealth|union|of|and)\s+)+"
)

CODE_RE = re.compile(r"^[A-Za-z]{2}$")


def norm(text: str | None) -> str:
    """Casefolded, accent-stripped, punctuation-free form used as the key.

    Accents go because news copy is inconsistent about them (Hà Nội, Cote
    d'Ivoire, Curacao) while the ISO table always carries them; punctuation
    goes because "U.S.", "U.S", and "US" are the same word.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    return re.sub(r"^the\s+", "", text)


class CountryResolver:
    """The ISO table plus the alias file, indexed for lookup.

    Index priority matters: `name` and `common_name` outrank `official_name`,
    so if one country's official name ever collides with another's short name,
    the short name keeps the key. Collisions are reported rather than resolved
    silently -- see `collisions`.
    """

    def __init__(self, table: dict[str, Any], aliases: dict[str, str]) -> None:
        self.countries = {c["alpha_2"]: c for c in table["countries"]}
        self.collisions: list[str] = []

        self._index: dict[str, str] = {}
        for priority, field in ((0, "name"), (0, "common_name"), (1, "official_name")):
            for country in table["countries"]:
                value = country.get(field)
                if not value:
                    continue
                key = norm(value)
                existing = self._index.get(key)
                if existing is None:
                    self._index[key] = country["alpha_2"]
                    self._priority = getattr(self, "_priority", {})
                    self._priority[key] = priority
                elif existing != country["alpha_2"] and priority >= self._priority[key]:
                    self.collisions.append(f"{value!r}: {existing} vs {country['alpha_2']}")

        bad = {a: c for a, c in aliases.items() if c not in self.countries}
        if bad:
            raise ValueError(f"Aliases point at codes ISO does not have: {bad}")
        self._aliases = {norm(a): c for a, c in aliases.items()}

    # -- display ----------------------------------------------------------
    def display_name(self, alpha_2: str) -> str:
        """The name to put on a map legend.

        `common_name` first: ISO calls TW "Taiwan, Province of China" and VN
        "Viet Nam", and neither belongs in a chart label.
        """
        country = self.countries[alpha_2]
        return country.get("common_name") or country["name"]

    # -- the cascade ------------------------------------------------------
    def _by_name(self, name: str) -> tuple[str | None, str]:
        key = norm(name)
        if not key:
            return None, "unresolved"
        if (hit := self._index.get(key)):
            return hit, "exact"
        if (hit := self._aliases.get(key)):
            return hit, "alias"
        stripped = QUALIFIER_RE.sub("", key).strip()
        if stripped and stripped != key:
            if (hit := self._index.get(stripped)):
                return hit, "exact"
            if (hit := self._aliases.get(stripped)):
                return hit, "alias"
        return None, "unresolved"

    def resolve(self, name: str | None, code_guess: str | None = None) -> dict[str, Any]:
        """One country name (+ the model's code) -> a resolved row.

        Always returns a row. An unresolved country keeps its raw strings so
        the next run, after an alias is added, can resolve it without the GPU.
        """
        raw_name = (name or "").strip()
        raw_code = (code_guess or "").strip().upper()

        by_name, how = self._by_name(raw_name)
        by_code = raw_code if (CODE_RE.match(raw_code) and raw_code in self.countries) else None

        alpha_2 = by_name or by_code
        if alpha_2 is None:
            return {
                "country_name": "", "alpha_2": "", "alpha_3": "",
                "raw_country": raw_name, "raw_code": raw_code,
                "resolution": "unresolved", "name_code_agree": None,
            }
        agree = None if (by_name is None or by_code is None) else (by_name == by_code)
        resolution = how if by_name else "code_guess"
        if agree is False:
            resolution = "name_over_code"
        return {
            "country_name": self.display_name(alpha_2),
            "alpha_2": alpha_2,
            "alpha_3": self.countries[alpha_2]["alpha_3"],
            "raw_country": raw_name,
            "raw_code": raw_code,
            "resolution": resolution,
            "name_code_agree": agree,
        }


def load_resolver(table_path: Path = DEFAULT_TABLE,
                  alias_path: Path = DEFAULT_ALIASES) -> CountryResolver:
    """Read the table and aliases, raising rather than degrading.

    Same reasoning as `scrape.finalize()`: a missing table that produced an
    empty index would resolve nothing, mark all 7,471 articles unresolved, and
    still exit 0. A missing ALIAS file is different and is tolerated -- it is
    an optional overlay, and its absence costs a handful of rows, not all.
    """
    if not table_path.exists():
        raise SystemExit(
            f"{table_path} is missing. Generate it first:\n"
            f"    pip install pycountry && python3 verification/make_iso_table.py"
        )
    with table_path.open("r", encoding="utf-8") as handle:
        table = json.load(handle)
    if not table.get("countries"):
        raise ValueError(f"{table_path} holds no countries")

    aliases: dict[str, str] = {}
    if alias_path.exists():
        with alias_path.open("r", encoding="utf-8") as handle:
            aliases = json.load(handle).get("countries", {})
    return CountryResolver(table, aliases)


# --------------------------------------------------------------------------
# Re-resolving an existing result file
# --------------------------------------------------------------------------

def reresolve(payload: dict[str, Any], resolver: CountryResolver) -> dict[str, Any]:
    """Re-run the lookup over a `locate_articles.py` result file, in place.

    Reads each country's `raw_country`/`raw_code` -- the model's own words,
    which the deliverable keeps forever -- so this is idempotent and can be run
    after every alias edit.
    """
    for article in payload.get("results", []):
        article["countries"] = [
            {**country, **resolver.resolve(country.get("raw_country"),
                                           country.get("raw_code"))}
            for country in article.get("countries", [])
        ]
    payload["summary"] = {**payload.get("summary", {}), **summarise(payload["results"])}
    return payload


def summarise(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolution counts, per-country totals, and the unresolved work queue."""
    rows = [c for a in articles for c in a.get("countries", [])]
    resolutions = Counter(c.get("resolution") for c in rows)
    unresolved = Counter(
        c.get("raw_country", "") for c in rows if c.get("resolution") == "unresolved"
    )
    totals = Counter(c["alpha_3"] for c in rows if c.get("alpha_3"))
    disagreements = Counter(
        f"{c['raw_country']} -> {c['alpha_2']} (model said {c['raw_code']})"
        for c in rows if c.get("resolution") == "name_over_code"
    )
    return {
        "country_mentions": len(rows),
        "resolved_mentions": sum(1 for c in rows if c.get("alpha_2")),
        "articles_with_a_country": sum(
            1 for a in articles if any(c.get("alpha_2") for c in a.get("countries", []))
        ),
        "distinct_countries": len(totals),
        "by_resolution": dict(resolutions),
        "articles_by_country_alpha3": dict(totals.most_common()),
        "unresolved": dict(unresolved.most_common()),
        "name_over_code": dict(disagreements.most_common(20)),
    }


def write_json(path: Path, payload: Any) -> None:
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
    parser = argparse.ArgumentParser(
        description="Re-resolve country names in a locate_articles.py result file."
    )
    parser.add_argument("results", type=Path, nargs="?",
                        default=ROOT / "data" / "article_countries.json")
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    parser.add_argument("--output", type=Path, default=None,
                        help="Where to write (default: overwrite the input).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change and write nothing.")
    args = parser.parse_args()

    resolver = load_resolver(args.table, args.aliases)
    if resolver.collisions:
        print("Name collisions in the ISO table (first wins):", file=sys.stderr)
        for line in resolver.collisions:
            print(f"  {line}", file=sys.stderr)

    if not args.results.exists():
        raise SystemExit(f"{args.results} does not exist -- run locate_articles.py first.")
    with args.results.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    before = summarise(payload.get("results", []))
    payload = reresolve(payload, resolver)
    after = payload["summary"]

    print(f"resolved {before['resolved_mentions']} -> {after['resolved_mentions']}"
          f" of {after['country_mentions']} country mentions")
    print(json.dumps({k: after[k] for k in ("by_resolution", "distinct_countries")}, indent=2))
    if after["unresolved"]:
        print("\nUnresolved, most blocking first -- add these to "
              f"{args.aliases} and re-run:")
        for name, count in list(after["unresolved"].items())[:25]:
            print(f"  {count:5d}  {name!r}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0
    write_json(args.output or args.results, payload)
    print(f"\nWrote {args.output or args.results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

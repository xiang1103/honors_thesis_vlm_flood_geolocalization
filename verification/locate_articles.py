"""Read every flood article and record WHICH COUNTRIES FLOODED.

One pass over `data/news_scrape_results.json`, restricted to the articles
`verify_text.py` marked `flood_verified`. Images are irrelevant here, so this
reads the CORPUS rather than `verified_images_news.json`: the corpus is the
superset, and ~1,650 flood articles carry no image at all and would otherwise
be invisible.

Per article the model returns a LIST of countries, not one. Floodlist is 44% of
the flood corpus and its house style is the multi-country round-up; a single
`country` field would silently pick a winner on a large share of the data.

Country names go through `country_codes.py` to become ISO 3166-1 codes. The
model's own words are kept on every row (`raw_country`, `raw_code`), so adding
an alias later re-resolves the whole file in seconds without touching the GPU:

    python3 verification/make_iso_table.py            # once, needs pycountry
    python3 verification/locate_articles.py --limit 50 --device-map cuda:0
    python3 verification/locate_articles.py --device-map cuda:0
    python3 verification/country_codes.py             # after editing aliases

What this is NOT: a location for any PHOTOGRAPH. An article about flooding in
Spain routinely carries a picture taken in Portugal -- measured at 180 of 243
in the New York pass. These are article-level countries, for counting and
mapping coverage, and they are not image labels.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from country_codes import CountryResolver, load_resolver, summarise, write_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------

#: Three rules here are load-bearing, each written against a way the answer
#: goes wrong on this corpus:
#:
#: (1) COUNTRY, not region. "Northern Italy", "the Midwest" and "Kerala" cannot
#:     be resolved to an ISO code; `region` exists to catch that detail without
#:     it contaminating the field that has to resolve.
#: (2) Flooding HAPPENED there. Flood copy is dense with countries that did not
#:     flood -- aid donors, an agency's headquarters, a scientist's university,
#:     and above all comparisons to earlier disasters elsewhere.
#: (3) The territories are their own entries. ISO gives HK, MO, TW and PR codes
#:     and the owner wants them counted separately; a model left to itself
#:     folds them into CN and US.
PROMPT = """You are reading a news article about flooding. List every COUNTRY where this article reports that flooding happened.

Outlet: {outlet}
Date: {date}
Headline: {title}
Article text (may be truncated):
\"\"\"{text}\"\"\"

Rules:
- Name COUNTRIES, never a state, province or region. If the article says "Kerala" write "India"; if it says "northern Italy" write "Italy"; if it says "Texas" write "United States". Put the sub-national detail in "region" instead.
- Include a country only if the article says flooding, flash flooding or flood damage actually occurred there in the event it is reporting.
- Do NOT include a country that appears only as a comparison to an earlier disaster, as a source of aid or rescue teams, as the base of an agency, official or scientist, or as somewhere a forecast merely mentions.
- Hong Kong, Macau, Taiwan and Puerto Rico each count as their own country here, never as part of China or the United States.
- One entry per country. If several places in one country flooded, that is still one entry; name the most prominent in "region".
- "country_code" is the ISO 3166-1 alpha-2 code: US, GB, IN, BR, PH.
- If the article names no country, or is not about a specific flood event, return an empty list.

Reply with one JSON object and nothing else:
{{"floods": [{{"country": "India", "country_code": "IN", "region": "Kerala", "confidence": "high", "evidence": "at most 12 words quoted from the article"}}]}}"""

#: How much article body the model sees. A flood lede names the country in its
#: first paragraph; a round-up names the rest within a few hundred words more.
#: Raise it with --text-budget if the round-ups look truncated.
TEXT_BUDGET = 2500

CONFIDENCES = ("high", "medium", "low")

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
UNTERMINATED_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def build_prompt(article: dict[str, Any], text_budget: int) -> str:
    text = (article.get("text") or "").strip()
    if len(text) > text_budget:
        text = text[:text_budget] + " ... [truncated]"
    return PROMPT.format(
        outlet=article.get("outlet") or "unknown",
        date=article.get("date") or "unknown",
        title=(article.get("title") or "").strip() or "(no headline)",
        text=text or "(no article text)",
    )


def json_objects(body: str) -> list[str]:
    """Every balanced top-level {...} in `body`, in order.

    A regex cannot bracket-match, and `evidence` is quoted prose that routinely
    contains punctuation a naive pattern would stop at. String state is tracked
    so a brace inside a quote does not open a level.
    """
    found: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                found.append(body[start:index + 1])
    return found


def parse_floods(text: str) -> list[dict[str, str]] | None:
    """The `floods` list from a model reply, or None if it never produced one.

    Thinking blocks are stripped and the LAST valid object wins, for the two
    reasons `backend.parse_answer` documents: a truncated `<think>` would
    otherwise be parsed as content, and anything JSON-shaped earlier in a reply
    is the model echoing the output format from the prompt.

    None and [] are different and both are real: None is "no parseable answer,
    try again", [] is "the model read it and found no country". Only [] is
    recorded.
    """
    body = UNTERMINATED_THINK_RE.sub(" ", THINK_BLOCK_RE.sub(" ", text))
    for blob in reversed(json_objects(body)):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict) or "floods" not in parsed:
            continue
        floods = parsed.get("floods")
        if not isinstance(floods, list):
            continue
        out: list[dict[str, str]] = []
        for entry in floods:
            if not isinstance(entry, dict):
                continue
            country = str(entry.get("country", "") or "").strip()
            code = str(entry.get("country_code", "") or "").strip()
            if not country and not code:
                continue
            confidence = str(entry.get("confidence", "") or "").strip().lower()
            out.append({
                "raw_country": country,
                "raw_code": code,
                # Deliberately NOT resolved: the owner wants country only. It
                # is free here (same generation) and means a later decision to
                # break out provinces needs no second GPU pass.
                "region": str(entry.get("region", "") or "").strip(),
                # Out of range is downgraded rather than rejected: the country
                # is the useful part and a retry would repeat the slip.
                "confidence": confidence if confidence in CONFIDENCES else "low",
                "evidence": str(entry.get("evidence", "") or "").strip(),
            })
        return out
    return None


def dedupe(countries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeats of the same resolved country within one article.

    "One entry per country" is in the prompt and is mostly obeyed, but a
    round-up that returns India twice would double its weight on the map. Keyed
    on the RESOLVED code so "UK" and "Britain" collapse too; unresolved rows
    fall back to their raw text so they are not all merged into one.
    """
    seen: set[str] = set()
    out = []
    for country in countries:
        key = country.get("alpha_2") or f"raw:{country.get('raw_country', '').lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(country)
    return out


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def read_corpus(path: Path) -> list[dict[str, Any]]:
    """Flood-verified articles, in corpus order. Raises on a bad read.

    Loud for the reason `scrape.finalize()` is loud: an empty read here would
    look like a finished run over zero articles and exit 0.
    """
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array in {path}")
    articles = [a for a in payload if a.get("flood_verified") and a.get("url")]
    if not articles:
        raise ValueError(f"{path} yielded no flood-verified articles")
    return articles


def read_ledger(path: Path) -> dict[str, dict[str, Any]]:
    """Articles judged so far, keyed by url.

    The ledger is the resume state and lives under scrape_data/ beside the
    other intermediates. Only `completed` rows count: an article whose reply
    could not be parsed stays pending, so re-running retries it.
    """
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read the ledger {path}: {exc}. Move it aside to start "
            f"over rather than deleting judgements."
        ) from exc
    return {
        str(r["article_url"]): r
        for r in payload.get("results", [])
        if r.get("article_url") and r.get("status") == "completed"
    }


@contextlib.contextmanager
def exclusive_run(lock_target: Path):
    """Refuse to start while another run holds the same output files.

    Two runs would read the same ledger, judge overlapping halves, and the
    slower one's merge would overwrite the faster one's with a stale copy --
    both reporting success. flock is released by the kernel however the process
    dies, so a crash leaves no stale lock to clear by hand.
    """
    lock_path = lock_target.with_name(lock_target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(
                f"Another run already holds {lock_path}. Wait for it, or kill "
                f"it first -- two runs would overwrite each other's results."
            )
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        handle.close()


def record_for(article: dict[str, Any], countries: list[dict[str, Any]],
               output: str, status: str, model_name: str) -> dict[str, Any]:
    """One article's row in the ledger and the deliverable.

    Article identity, the model's raw reply and the resolved countries all live
    on the same record, so `article_url` joins this back to the corpus and to
    `verified_images_news.json` without a second file.
    """
    return {
        "article_url": article["url"],
        "article_title": article.get("title") or "",
        "article_date": article.get("date") or "",
        "outlet": article.get("outlet") or "unknown",
        "flood_score": article.get("flood_score"),
        "image_count": len(article.get("images") or []),
        "status": status,
        "countries": countries,
        "model": model_name,
        "model_output": output,
        "judged_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record which countries flooded, per flood article."
    )
    parser.add_argument("--corpus", type=Path,
                        default=ROOT / "data" / "news_scrape_results.json")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "data" / "article_countries.json")
    parser.add_argument("--ledger", type=Path,
                        default=ROOT / "scrape_data" / "article_country_judgements.json")
    parser.add_argument("--model-path", default=None,
                        help="Local weights (default: local_vlm.DEFAULT_MODEL_PATH).")
    parser.add_argument("--device-map", default="auto",
                        help="accelerate device_map: 'auto', 'cuda:0', ... Pin a free GPU.")
    parser.add_argument("--batch-size", type=int, default=32,
                        help="Articles per forward pass.")
    parser.add_argument("--text-budget", type=int, default=TEXT_BUDGET,
                        help="Characters of article body shown to the model.")
    parser.add_argument("--max-new-tokens", type=int, default=320,
                        help="Reply budget. A round-up listing 8 countries needs room.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Judge at most this many pending articles (trial runs).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    history_path = args.ledger.with_suffix(".jsonl")

    resolver = load_resolver()
    if resolver.collisions:
        print("ISO table name collisions (first wins):", file=sys.stderr)
        for line in resolver.collisions:
            print(f"  {line}", file=sys.stderr)

    articles = read_corpus(args.corpus)
    print(f"Flood-verified articles: {len(articles)} "
          f"({sum(1 for a in articles if not a.get('images'))} carry no image)")

    with exclusive_run(args.ledger):
        ledger = read_ledger(args.ledger)
        pending = [a for a in articles if a["url"] not in ledger]
        if args.limit is not None:
            pending = pending[:args.limit]
        print(f"To judge: {len(pending)}   already judged: {len(ledger)}")

        if pending:
            sys.path.insert(0, str(ROOT))
            from local_vlm import DEFAULT_MODEL_PATH, LocalVLM

            model_path = args.model_path or DEFAULT_MODEL_PATH
            llm = LocalVLM(
                model_path,
                device_map=args.device_map,
                max_new_tokens=args.max_new_tokens,
                enable_thinking=False,   # see backend.THINK_BLOCK_RE
            )
            print(f"Loading {model_path} on device_map={args.device_map} ...")
            llm.load()
            print("Loaded.")

            history_path.parent.mkdir(parents=True, exist_ok=True)
            invalid = 0
            started = time.monotonic()
            with history_path.open("a", encoding="utf-8") as history:
                for offset in range(0, len(pending), args.batch_size):
                    chunk = pending[offset:offset + args.batch_size]
                    prompts = [build_prompt(a, args.text_budget) for a in chunk]
                    try:
                        outputs = llm.generate_text_batch(prompts)
                    except Exception as exc:  # noqa: BLE001
                        outputs = [f"{type(exc).__name__}: {exc}"] * len(chunk)
                    parsed = [parse_floods(o) for o in outputs]

                    # A reply that did not parse is usually a round-up that ran
                    # out of tokens mid-list, so retry just those with double
                    # the budget. Cheap: it is a handful per batch, not a pass.
                    retry = [i for i, p in enumerate(parsed) if p is None]
                    if retry:
                        try:
                            again = llm.generate_text_batch(
                                [prompts[i] for i in retry],
                                max_new_tokens=args.max_new_tokens * 2,
                            )
                        except Exception as exc:  # noqa: BLE001
                            again = [f"{type(exc).__name__}: {exc}"] * len(retry)
                        for slot, output in zip(retry, again):
                            outputs[slot] = output
                            parsed[slot] = parse_floods(output)

                    for article, output, floods in zip(chunk, outputs, parsed):
                        if floods is None:
                            invalid += 1
                            record = record_for(article, [], output,
                                                "invalid_output", llm.name)
                        else:
                            resolved = dedupe([
                                {**f, **resolver.resolve(f["raw_country"], f["raw_code"])}
                                for f in floods
                            ])
                            record = record_for(article, resolved, output,
                                                "completed", llm.name)
                            ledger[article["url"]] = record
                        history.write(json.dumps(record, ensure_ascii=False) + "\n")
                    history.flush()

                    done = min(offset + args.batch_size, len(pending))
                    rate = done / max(time.monotonic() - started, 1e-6)
                    left = (len(pending) - done) / rate if rate else 0
                    print(f"  {done}/{len(pending)}  {rate:.1f}/s  "
                          f"eta {left/60:.0f}m  unparsed {invalid}", flush=True)

        results = [ledger[a["url"]] for a in articles if a["url"] in ledger]
        summary = {
            "flood_articles": len(articles),
            "articles_judged": len(results),
            "articles_pending": len(articles) - len(results),
            **summarise(results),
        }
        write_json(args.ledger, {"summary": summary, "prompt": PROMPT, "results": results})
        write_json(args.output, {
            "summary": summary,
            "prompt": PROMPT,
            "iso_table": "data/iso_3166.json",
            "join_key": "article_url",
            "results": results,
        })
        if history_path.exists():
            try:
                history_path.unlink()
            except OSError as exc:
                print(f"Warning: could not remove {history_path}: {exc}", file=sys.stderr)

    print(f"\nWrote {args.output}")
    print(json.dumps({k: v for k, v in summary.items()
                      if k != "articles_by_country_alpha3"}, indent=2)[:2000])
    top = list(summary["articles_by_country_alpha3"].items())[:15]
    print("\nTop countries: " + ", ".join(f"{c} {n}" for c, n in top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

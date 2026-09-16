"""Find the New York City images inside data/verified_images_news.json.

use rule-based to find article mentioning nyc, then llm to verify nyc based off image captions 
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Stage 1: the gazetteer
# --------------------------------------------------------------------------

#: Terms that mean New York City and essentially nothing else, anywhere in the
#: English-language flood corpus. A hit here makes the article a candidate on
#: its own. Neighbourhood names are included because a caption often names the
#: neighbourhood and never the city ("Bay Ridge", "the Rockaways").
STRONG_TERMS = [
    r"new york city", r"nyc\b", r"n\.y\.c\.", r"five boroughs",
    r"manhattan", r"brooklyn", r"staten island", r"the bronx", r"bronx\b",
    r"harlem", r"rockaway", r"coney island", r"bay ridge", r"gowanus",
    r"red hook", r"bushwick", r"greenpoint", r"park slope", r"crown heights",
    r"bed-?stuy", r"bedford-stuyvesant", r"bensonhurst", r"canarsie",
    r"sheepshead bay", r"brighton beach", r"borough park", r"sunset park",
    r"astoria", r"long island city", r"jackson heights", r"elmhurst",
    r"forest hills", r"howard beach", r"mott haven", r"hunts point",
    r"soundview", r"throgs neck", r"riverdale", r"washington heights",
    r"inwood", r"tribeca", r"soho\b", r"lower east side", r"east village",
    r"greenwich village", r"upper west side", r"upper east side",
    r"battery park", r"times square", r"city hall park",
    r"oakwood beach", r"midland beach", r"new dorp", r"tottenville",
    r"fdr drive", r"belt parkway", r"cross bronx", r"brooklyn bridge",
    r"manhattan bridge", r"verrazz?ano", r"\bbqe\b", r"grand central",
    r"penn station", r"staten island ferry", r"laguardia", r"\bjfk\b",
    r"\bmta\b", r"\bnypd\b", r"\bfdny\b", r"\bnycha\b",
    r"metropolitan transportation authority", r"con ?edison", r"east river",
    r"mayor adams", r"eric adams", r"de blasio", r"mamdani",
    r"new york city emergency management",
]

#: Weaker than STRONG but still specifically New York: enough to make an
#: article a candidate, not enough to call the photograph. "New York" alone is
#: here rather than in STRONG because it is also the state -- a Catskills flood
#: is "New York" and is not this dataset.
ANCHOR_TERMS = [r"new york", r"nyc\b", r"new yorkers?", r"n\.y\."]

#: Place names that ARE New York City neighbourhoods but are far more often
#: something else in a global flood corpus. Measured on this corpus, taken
#: alone they produced almost pure noise: "Richmond" 63 articles (British
#: Columbia, Virginia, New South Wales), "Jamaica" 14 (the country), "Queens" 3
#: (Queensland), "Flushing" 4 (the verb), "Corona" 1 (a town in Mexico),
#: "Chelsea" 1 (Queensland). They therefore count only when the article also
#: carries an ANCHOR_TERM. The storm names sit here for the same reason:
#: Sandy and Ida both hit far more than New York.
AMBIGUOUS_TERMS = [
    r"\bqueens\b", r"\bchelsea\b", r"williamsburg", r"\bjamaica\b",
    r"\bflushing\b", r"\bcorona\b", r"\bmidtown\b", r"\brichmond\b",
    r"hurricane sandy", r"superstorm sandy", r"hurricane ida",
]

STRONG_RE = [re.compile(p, re.I) for p in STRONG_TERMS]
ANCHOR_RE = [re.compile(p, re.I) for p in ANCHOR_TERMS]
AMBIGUOUS_RE = [re.compile(p, re.I) for p in AMBIGUOUS_TERMS]

#: Bumped when the lists above change, so a results file says which gazetteer
#: produced it. Stage 1 is cheap to re-run; stage 2 is not.
RULES_VERSION = "nyc-gazetteer-v1"


def matched_terms(patterns: list[re.Pattern[str]], blob: str) -> list[str]:
    """Every distinct term from `patterns` occurring in `blob`, lowercased.

    The terms are recorded on each row, not just the boolean: when a candidate
    turns out to be junk, the reason is visible without re-running the regex.
    """
    return sorted({m.group(0).lower() for p in patterns for m in p.finditer(blob)})


def classify_article(title: str, text: str) -> dict[str, Any] | None:
    """Rule verdict for one article, or None if it never mentions New York.

    The tier is kept because it is a real prior on the LLM stage: `strong`
    articles are about New York City, `anchor_only` ones may be about New York
    State or merely mention the city in passing.
    """
    blob = f"{title}\n{text}"
    strong = matched_terms(STRONG_RE, blob)
    anchor = matched_terms(ANCHOR_RE, blob)
    # An ambiguous name is only evidence in the presence of a New York anchor.
    ambiguous = matched_terms(AMBIGUOUS_RE, blob) if anchor else []

    if strong:
        tier = "strong"
    elif ambiguous:
        tier = "ambiguous+anchor"
    elif anchor:
        tier = "anchor_only"
    else:
        return None
    return {
        "rule_tier": tier,
        "rule_terms_strong": strong,
        "rule_terms_ambiguous": ambiguous,
        "rule_terms_anchor": anchor,
    }


# --------------------------------------------------------------------------
# Stage 2: the per-image judge
# --------------------------------------------------------------------------

LABELS = ("nyc", "nyc_metro_not_nyc", "elsewhere", "unknown")
CONFIDENCES = ("high", "medium", "low")

#: The label set is deliberately four-way, not yes/no. A binary forces the
#: New Jersey and Westchester photographs that fill New York flood coverage
#: into one side or the other; `nyc_metro_not_nyc` keeps them recoverable if
#: the project later decides the metro area counts.
#:
#: The two load-bearing instructions are (a) judge the CAMERA, not the article's
#: subject, and (b) the caption outranks the body. Both were written against
#: the measured failure: "Heavy rain kills two in New Jersey as subway and
#: roads flooded in New York" carries a photograph captioned "flash flood in
#: Plainfield, New Jersey".
JUDGE_PROMPT = """You are given a news photograph's caption and the article it appeared in. Decide WHERE THE PHOTOGRAPH ITSELF WAS TAKEN.

Outlet: {outlet}
Date: {date}
Headline: {title}
Photo caption: {caption}
Article text (may be truncated):
\"\"\"{text}\"\"\"

Rules:
- Judge where the CAMERA stood, not what the article is about. An article about New York City frequently carries photographs taken elsewhere.
- The caption describes THIS photograph and wins whenever it disagrees with the article text. The article text is context for an unclear caption, nothing more.
- "New York City" means the five boroughs only: Manhattan, Brooklyn, Queens, the Bronx, Staten Island.
- New Jersey (Hoboken, Newark, Plainfield, Jersey City), Westchester, Long Island outside Brooklyn and Queens, and Connecticut are NOT New York City, even when the article is about a New York storm. Label those nyc_metro_not_nyc.
- Upstate New York and anywhere else in New York State is NOT New York City. Label that elsewhere.
- If the caption names no place but the article is wholly about a single New York City event, you may answer nyc with confidence "low" or "medium". If the article covers several places and the caption names none, answer unknown.

Reply with one JSON object and nothing else:
{{"label": "nyc" or "nyc_metro_not_nyc" or "elsewhere" or "unknown", "confidence": "high" or "medium" or "low", "place": "the most specific place name you can justify, or an empty string", "evidence": "a short quote from the caption or article that decides it"}}"""

#: How much article body the judge sees. The corpus median is 3.5k characters,
#: so this truncates only the long features, and the caption -- which is what
#: actually decides the answer -- is always shown in full above it.
TEXT_BUDGET = 6000

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
UNTERMINATED_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def parse_judgement(text: str) -> dict[str, Any] | None:
    """The LAST valid JSON object in a model reply, validated, or None.

    Same two hazards `backend.parse_answer` guards against, for the same
    reason: thinking blocks are stripped first, and the last candidate wins,
    because anything JSON-shaped earlier in the reply is the model echoing the
    output format from the prompt rather than answering.

    Returning None rather than a default lets a non-answer be retried instead
    of silently recorded as `unknown`.
    """
    body = UNTERMINATED_THINK_RE.sub(" ", THINK_BLOCK_RE.sub(" ", text))

    # Scan for balanced top-level objects; a regex cannot bracket-match, and
    # `evidence` routinely contains braces-free but comma-heavy quoted prose.
    candidates: list[str] = []
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
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0:
                    candidates.append(body[start:index + 1])

    for blob in reversed(candidates):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        label = str(parsed.get("label", "")).strip().lower()
        if label not in LABELS:
            continue
        confidence = str(parsed.get("confidence", "")).strip().lower()
        return {
            "nyc_label": label,
            # An out-of-range confidence is downgraded, not rejected: the label
            # is the useful part and a retry would probably repeat the slip.
            "nyc_confidence": confidence if confidence in CONFIDENCES else "low",
            "nyc_place": str(parsed.get("place", "") or "").strip(),
            "nyc_evidence": str(parsed.get("evidence", "") or "").strip(),
        }
    return None


def build_prompt(row: dict[str, Any], article: dict[str, Any]) -> str:
    text = (article.get("text") or "").strip()
    if len(text) > TEXT_BUDGET:
        text = text[:TEXT_BUDGET] + " ... [truncated]"
    return JUDGE_PROMPT.format(
        outlet=row.get("outlet") or "unknown",
        date=(row.get("article_date") or article.get("date") or "unknown"),
        title=(row.get("article_title") or article.get("title") or "").strip(),
        caption=((row.get("caption") or "").strip() or "(no caption)"),
        text=text or "(no article text)",
    )


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def read_verified(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or "results" not in payload:
        raise ValueError(f"Expected the verifier's {{summary, results}} object in {path}")
    return payload


def read_corpus(path: Path) -> dict[str, dict[str, Any]]:
    """article_url -> article. Raises rather than returning an empty map.

    Same reason `scrape.finalize()` raises: an article lookup that silently
    came back empty would send every image to the judge with no context and
    look like a successful run.
    """
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return {a["url"]: a for a in payload if a.get("url")}


def read_existing(path: Path, history_path: Path) -> dict[str, dict[str, Any]]:
    """Rows already judged, keyed by occurrence_id -- this run's resume ledger.

    Reads the canonical JSON and then the intermediate JSONL, later wins. The
    JSONL matters because it is only merged when a run FINISHES: a run killed
    partway leaves its judgements there and nowhere else, and CLAUDE.md is
    explicit that a `.jsonl` left in scrape_data/ is unmerged work, not
    garbage. Skipping it would silently re-pay the GPU for every row the killed
    run had already judged.

    Resume is prompt-agnostic, matching verify_images_vlm.py: editing
    JUDGE_PROMPT does not re-judge rows that already have a label. Every row
    records the exact `nyc_prompt` and `nyc_model` behind it, so a mixed file
    stays legible. To re-judge everything, move both files aside.
    """
    known: dict[str, dict[str, Any]] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        for row in (payload.get("results", []) if isinstance(payload, dict) else []):
            if row.get("occurrence_id") and row.get("nyc_status") == "completed":
                known[str(row["occurrence_id"])] = row
    if history_path.exists():
        with history_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue      # a run killed mid-write leaves a torn last line
                if row.get("occurrence_id") and row.get("nyc_status") == "completed":
                    known[str(row["occurrence_id"])] = row
    return known


def write_output(
    path: Path,
    history_path: Path,
    candidates: list[dict[str, Any]],
    known: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Merge this run's JSONL into the canonical JSON, atomically.

    MUST merge, not overwrite: `known` carries forward rows whose article has
    since dropped out of the candidate set (a gazetteer edit, a narrower
    --limit), which would otherwise be lost along with the GPU time they cost.
    """
    latest: dict[str, dict[str, Any]] = dict(known)
    if history_path.exists():
        with history_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("occurrence_id") and row.get("nyc_status") == "completed":
                    latest[str(row["occurrence_id"])] = row

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        oid = candidate["occurrence_id"]
        if oid in latest and oid not in seen:
            ordered.append(latest[oid])
            seen.add(oid)
    ordered.extend(row for oid, row in latest.items() if oid not in seen)

    def count(field: str, value: str) -> int:
        return sum(1 for r in ordered if r.get(field) == value)

    nyc_rows = [r for r in ordered if r.get("nyc_label") == "nyc"]
    summary = {
        "candidate_occurrences": len(candidates),
        "judged_occurrences": len(seen),
        "pending_occurrences": len(candidates) - len(seen),
        "carried_occurrences": len(ordered) - len(seen),
        "stored_occurrences": len(ordered),
        "candidate_articles": len({c["article_url"] for c in candidates}),
        "by_label": {label: count("nyc_label", label) for label in LABELS},
        "by_confidence": {c: count("nyc_confidence", c) for c in CONFIDENCES},
        "by_rule_tier": {
            tier: count("rule_tier", tier)
            for tier in ("strong", "ambiguous+anchor", "anchor_only")
        },
        # The headline number: what a caller would actually take.
        "nyc_images": len(nyc_rows),
        "nyc_images_high_or_medium": sum(
            1 for r in nyc_rows if r.get("nyc_confidence") in ("high", "medium")
        ),
        "nyc_articles": len({r["article_url"] for r in nyc_rows}),
        "on_current_prompt": sum(r.get("nyc_prompt") == JUDGE_PROMPT for r in ordered),
        "distinct_prompts": len({r.get("nyc_prompt") for r in ordered}),
    }
    payload = {
        "summary": summary,
        "rules_version": RULES_VERSION,
        "current_prompt": JUDGE_PROMPT,
        "prompt": JUDGE_PROMPT,
        "labels": list(LABELS),
        "results": ordered,
    }
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
    return summary


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Label the New York City images in the verified-image results."
    )
    parser.add_argument("--verified", type=Path,
                        default=root / "data" / "verified_images_news.json",
                        help="Verifier output to read (default: data/verified_images_news.json).")
    parser.add_argument("--corpus", type=Path,
                        default=root / "data" / "news_scrape_results.json",
                        help="Corpus supplying article text (default: data/news_scrape_results.json).")
    parser.add_argument("--final-output", type=Path,
                        default=root / "data" / "nyc_scraped_images.json",
                        help="Canonical labelled output; also the resume ledger.")
    parser.add_argument("--output", type=Path,
                        default=root / "scrape_data" / "nyc_scraped_images.jsonl",
                        help="Intermediate append-only JSONL, merged and deleted at the end.")
    parser.add_argument("--keep-jsonl", action="store_true",
                        help="Keep the intermediate JSONL after the merge.")
    parser.add_argument("--answer", default="yes", choices=("yes", "no", "any"),
                        help="Which verifier rows to consider (default: yes, the street-level ones).")
    parser.add_argument("--rules-only", action="store_true",
                        help="Run stage 1 and report candidates without loading the model.")
    parser.add_argument("--model-path", default=None,
                        help="Local model weights (default: local_vlm.DEFAULT_MODEL_PATH).")
    parser.add_argument("--device-map", default="auto",
                        help="accelerate device_map: 'auto', 'cuda:0', ... Pin a free GPU.")
    parser.add_argument("--max-new-tokens", type=int, default=200,
                        help="Generation cap for the JSON verdict.")
    parser.add_argument("--retries", type=int, default=3,
                        help="Attempts before a row is recorded as invalid_output.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Judge at most this many images; for testing.")
    return parser.parse_args()


@contextlib.contextmanager
def exclusive_run(final_path: Path):
    """Refuse to start while another run holds the same output files.

    Two concurrent runs corrupt each other in a way neither can detect. The
    real incident: a run thought to be dead was still going, a second started,
    read the JSONL as it stood and judged the rest -- then the first finished,
    merged, and DELETED the JSONL while the second still had it open. The
    second's work went to an unlinked inode, its merge found no history file,
    and it overwrote the first's complete output with its own stale copy. Both
    runs reported success.

    flock is released by the kernel when the process dies however it dies, so
    a crashed run leaves no stale lock to clear by hand.
    """
    lock_path = final_path.with_name(final_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(
                f"Another run already holds {lock_path}. Wait for it to finish, "
                f"or kill it first -- two runs would overwrite each other's results."
            )
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        handle.close()


def main() -> int:
    args = parse_args()

    verified = read_verified(args.verified)
    corpus = read_corpus(args.corpus)

    rows = verified["results"]
    if args.answer != "any":
        rows = [r for r in rows if r.get("answer") == args.answer]
    print(f"Verifier rows considered: {len(rows)} (answer={args.answer})")

    # -- stage 1 ----------------------------------------------------------
    article_rules: dict[str, dict[str, Any] | None] = {}
    candidates: list[dict[str, Any]] = []
    missing_articles = 0
    for row in rows:
        url = row.get("article_url")
        article = corpus.get(url)
        if article is None:
            missing_articles += 1
            continue
        if url not in article_rules:
            article_rules[url] = classify_article(
                article.get("title") or "", article.get("text") or ""
            )
        rules = article_rules[url]
        if rules is None:
            continue
        caption = row.get("caption") or ""
        candidates.append({
            **row,
            **rules,
            # Recorded separately because a place named in the caption is far
            # stronger evidence than the same name buried in the body.
            "rule_caption_terms": matched_terms(STRONG_RE + AMBIGUOUS_RE, caption),
        })

    tiers: dict[str, int] = {}
    for c in candidates:
        tiers[c["rule_tier"]] = tiers.get(c["rule_tier"], 0) + 1
    print(f"Stage 1: {len(candidates)} candidate images across "
          f"{len({c['article_url'] for c in candidates})} articles  {tiers}")
    if missing_articles:
        print(f"  ({missing_articles} rows had no matching corpus article)")

    if args.rules_only:
        print("--rules-only: stopping before the model.")
        return 0
    if not candidates:
        print("Nothing to judge.")
        return 0

    # -- stage 2 ----------------------------------------------------------
    # Held until the merge lands: reading the ledger, judging, and replacing the
    # output must be one critical section or a second run interleaves with it.
    lock = exclusive_run(args.final_output)
    lock.__enter__()

    known = read_existing(args.final_output, args.output)
    pending = [c for c in candidates if c["occurrence_id"] not in known]
    done = len(candidates) - len(pending)
    if args.limit is not None and args.limit < len(pending):
        deferred = len(pending) - args.limit
        pending = pending[:args.limit]
    else:
        deferred = 0
    print(f"Stage 2: {len(pending)} to judge, {done} already judged"
          + (f", {deferred} deferred by --limit" if deferred else ""))

    if pending:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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

        args.output.parent.mkdir(parents=True, exist_ok=True)
        # The same caption under the same article is the same question; a few
        # outlets repeat one caption across an article's images.
        prompt_cache: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        with args.output.open("a", encoding="utf-8") as history:
            for index, candidate in enumerate(pending, start=1):
                article = corpus[candidate["article_url"]]
                prompt = build_prompt(candidate, article)

                judgement = prompt_cache.get(prompt)
                output = ""
                if judgement is None:
                    for attempt in range(1, args.retries + 1):
                        try:
                            output = llm.generate_text(prompt)
                        except Exception as exc:  # noqa: BLE001
                            output = f"{type(exc).__name__}: {exc}"
                            continue
                        judgement = parse_judgement(output)
                        if judgement is not None:
                            prompt_cache[prompt] = judgement
                            break

                if judgement is None:
                    record = {
                        **candidate,
                        "nyc_status": "invalid_output",
                        "nyc_model_output": output,
                        "nyc_model": llm.name,
                        "nyc_prompt": JUDGE_PROMPT,
                        "nyc_judged_at": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    record = {
                        **candidate,
                        **judgement,
                        "nyc_status": "completed",
                        "nyc_model_output": output,
                        "nyc_model": llm.name,
                        "nyc_prompt": JUDGE_PROMPT,
                        "nyc_judged_at": datetime.now(timezone.utc).isoformat(),
                    }
                    counts[judgement["nyc_label"]] = counts.get(judgement["nyc_label"], 0) + 1

                history.write(json.dumps(record, ensure_ascii=False) + "\n")
                history.flush()
                if index % 25 == 0 or index == len(pending):
                    print(f"  {index}/{len(pending)}  {counts}")

    try:
        summary = write_output(args.final_output, args.output, candidates, known)
        if not args.keep_jsonl and args.output.exists():
            try:
                args.output.unlink()
            except OSError as exc:
                print(f"Warning: could not remove {args.output}: {exc}", file=sys.stderr)
    finally:
        lock.__exit__(None, None, None)

    print(f"\nWrote {args.final_output}")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

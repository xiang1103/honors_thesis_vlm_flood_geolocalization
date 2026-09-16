"""Find the New York images and articles in the classified flood dataset.

ONE pipeline, no modes. Every flood-related article in
``data/verified_images_news.json`` is examined, and two independent checks run:

* the ARTICLE is checked by RULE -- a New York gazetteer over its headline and
  body (see STRONG_TERMS);
* every IMAGE is checked by an LLM reading its own caption, with the article
  supplied as context.

Two checks rather than one because neither alone is right. A caption is the
only thing that describes THIS photograph -- an article about New York City
routinely carries pictures taken in Houston or Nepal, measured at 180 of 243 in
an earlier pass. But a caption alone is not enough either: RNZ captions
"Brooklyn residents clearing gutters" about a suburb of Wellington, and only
the surrounding article says which Brooklyn it is.

What is kept:

* an image whose own LLM check passes -- and its article comes with it;
* every image of an article whose rule check passes.

So a New York article keeps its whole set, and a New York photograph is never
lost just because its article was about somewhere else.

Everything kept is written to ``data/nyc_scraped_images.json``, articles and
images together.

    python3 verification/filter_nyc.py --device-map cuda:0
"""

from __future__ import annotations

import argparse
import collections
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

#: An image is New York if the LLM puts it in the five boroughs, or in the
#: surrounding metro area. The metro label is kept distinct rather than merged
#: so a later decision to drop New Jersey is a filter, not another GPU run.
KEEP_LABELS = ("nyc", "nyc_metro_not_nyc")


def read_verified(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or "results" not in payload:
        raise ValueError(f"Expected the verifier's {{summary, results}} object in {path}")
    return payload


def read_corpus(path: Path) -> dict[str, dict[str, Any]]:
    """article_url -> article. Raises rather than returning an empty map.

    Same reason `scrape.finalize()` raises: an article lookup that silently
    came back empty would send every image to the LLM with no context and still
    look like a successful run.
    """
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return {a["url"]: a for a in payload if a.get("url")}


def read_ledger(path: Path) -> dict[str, dict[str, Any]]:
    """Every image judged so far, keyed by occurrence_id.

    The ledger is an INTERNAL file under scrape_data/, not the deliverable. It
    exists because the deliverable holds only what passed: if resume read that
    instead, every rejected image would read as pending and the whole corpus
    would be re-judged on the next run -- an hour of GPU to rediscover the same
    negatives. Nothing about it is a mode or a flag; it is always written and
    always read.
    """
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read the judgement ledger {path}: {exc}. "
            f"Move it aside to start over rather than deleting judgements."
        ) from exc
    return {
        str(r["occurrence_id"]): r
        for r in payload.get("results", [])
        if r.get("occurrence_id") and r.get("nyc_status") == "completed"
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace `path` atomically, fsynced before the rename.

    fsync because /home/liu47 is NFS and page-cache bytes are not durable
    bytes; the partial .tmp is removed on failure so a botched write leaves the
    previous file untouched rather than half-replaced.
    """
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


@contextlib.contextmanager
def exclusive_run(lock_target: Path):
    """Refuse to start while another run holds the same output files.

    Two concurrent runs corrupt each other in a way neither can detect. The
    real incident: a run thought to be dead was still going, a second started,
    read the ledger as it stood and judged the rest -- then the first finished,
    merged, and DELETED the intermediate while the second still had it open.
    The second's work went to an unlinked inode, its merge found no history
    file, and it overwrote the first's complete output with its own stale copy.
    Both runs reported success.

    flock is released by the kernel when the process dies however it dies, so
    a crashed run leaves no stale lock to clear by hand.
    """
    lock_path = lock_target.with_name(lock_target.name + ".lock")
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


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Keep the New York images and articles in the flood dataset."
    )
    parser.add_argument("--verified", type=Path,
                        default=root / "data" / "verified_images_news.json",
                        help="Classified dataset to read.")
    parser.add_argument("--corpus", type=Path,
                        default=root / "data" / "news_scrape_results.json",
                        help="Corpus supplying article text.")
    parser.add_argument("--output", type=Path,
                        default=root / "data" / "nyc_scraped_images.json",
                        help="The deliverable: the New York articles and images.")
    parser.add_argument("--model-path", default=None,
                        help="Local model weights (default: local_vlm.DEFAULT_MODEL_PATH).")
    parser.add_argument("--device-map", default="auto",
                        help="accelerate device_map: 'auto', 'cuda:0', ... Pin a free GPU.")
    parser.add_argument("--batch-size", type=int, default=32,
                        help="Captions judged per forward pass.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ledger_path = (Path(__file__).resolve().parents[1]
                   / "scrape_data" / "nyc_judgements.json")
    history_path = ledger_path.with_suffix(".jsonl")

    verified = read_verified(args.verified)
    corpus = read_corpus(args.corpus)
    rows = [r for r in verified["results"] if r.get("article_url") in corpus]
    skipped = len(verified["results"]) - len(rows)
    print(f"Flood-related images: {len(rows)} across "
          f"{len({r['article_url'] for r in rows})} articles"
          + (f"  ({skipped} rows had no corpus article)" if skipped else ""))

    # -- the rule check, on articles --------------------------------------
    article_rules: dict[str, dict[str, Any]] = {}
    for url in {r["article_url"] for r in rows}:
        article = corpus[url]
        article_rules[url] = classify_article(
            article.get("title") or "", article.get("text") or ""
        )
    rule_pass = {u for u, v in article_rules.items() if v is not None}
    print(f"Rule check: {len(rule_pass)} articles mention New York")

    # -- the LLM check, on every image ------------------------------------
    with exclusive_run(ledger_path):
        ledger = read_ledger(ledger_path)
        pending = [r for r in rows if r["occurrence_id"] not in ledger]
        print(f"LLM check: {len(pending)} captions to judge, "
              f"{len(rows) - len(pending)} already judged")

        if pending:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from local_vlm import DEFAULT_MODEL_PATH, LocalVLM

            model_path = args.model_path or DEFAULT_MODEL_PATH
            llm = LocalVLM(
                model_path,
                device_map=args.device_map,
                max_new_tokens=160,
                enable_thinking=False,   # see backend.THINK_BLOCK_RE
            )
            print(f"Loading {model_path} on device_map={args.device_map} ...")
            llm.load()
            print("Loaded.")

            history_path.parent.mkdir(parents=True, exist_ok=True)
            counts: dict[str, int] = {}
            started = time.monotonic()
            with history_path.open("a", encoding="utf-8") as history:
                for offset in range(0, len(pending), args.batch_size):
                    chunk = pending[offset:offset + args.batch_size]
                    prompts = [build_prompt(r, corpus[r["article_url"]]) for r in chunk]
                    try:
                        outputs = llm.generate_text_batch(prompts)
                    except Exception as exc:  # noqa: BLE001
                        outputs = [f"{type(exc).__name__}: {exc}"] * len(chunk)

                    for row, output in zip(chunk, outputs):
                        judgement = parse_judgement(output)
                        record = {
                            **row,
                            **(judgement or {}),
                            "nyc_status": "completed" if judgement else "invalid_output",
                            "nyc_model_output": output,
                            "nyc_model": llm.name,
                            "nyc_prompt": JUDGE_PROMPT,
                            "nyc_judged_at": datetime.now(timezone.utc).isoformat(),
                        }
                        if judgement:
                            ledger[row["occurrence_id"]] = record
                            counts[judgement["nyc_label"]] = counts.get(
                                judgement["nyc_label"], 0) + 1
                        history.write(json.dumps(record, ensure_ascii=False) + "\n")
                    history.flush()

                    done = min(offset + args.batch_size, len(pending))
                    rate = done / max(time.monotonic() - started, 1e-6)
                    left = (len(pending) - done) / rate if rate else 0
                    print(f"  {done}/{len(pending)}  {rate:.1f}/s  "
                          f"eta {left/60:.0f}m  {counts}", flush=True)

        summary = write_results(
            ledger_path, args.output, rows, ledger, article_rules, rule_pass, corpus
        )
        if history_path.exists():
            try:
                history_path.unlink()
            except OSError as exc:
                print(f"Warning: could not remove {history_path}: {exc}", file=sys.stderr)

    print(f"\nWrote {args.output}")
    print(json.dumps(summary, indent=2))
    return 0


def write_results(
    ledger_path: Path,
    output_path: Path,
    rows: list[dict[str, Any]],
    ledger: dict[str, dict[str, Any]],
    article_rules: dict[str, dict[str, Any]],
    rule_pass: set[str],
    corpus: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Write the full ledger, then the New York deliverable derived from it."""
    judged = [ledger[r["occurrence_id"]] for r in rows if r["occurrence_id"] in ledger]
    write_json(ledger_path, {
        "summary": {"judged": len(judged), "of_images": len(rows)},
        "rules_version": RULES_VERSION,
        "prompt": JUDGE_PROMPT,
        "results": judged,
    })

    # An image passes on its own verdict; an article passes on the rule check.
    image_pass = {
        r["occurrence_id"] for r in judged if r.get("nyc_label") in KEEP_LABELS
    }
    articles_from_images = {
        r["article_url"] for r in rows if r["occurrence_id"] in image_pass
    }
    kept_articles = rule_pass | articles_from_images

    kept: list[dict[str, Any]] = []
    for row in rows:
        oid = row["occurrence_id"]
        by_image = oid in image_pass
        by_article = row["article_url"] in rule_pass
        if not (by_image or by_article):
            continue
        rules = article_rules.get(row["article_url"]) or {}
        kept.append({
            **(ledger.get(oid) or row),
            **rules,
            "kept_by_image": by_image,
            "kept_by_article": by_article,
        })

    articles = []
    for url in sorted(kept_articles):
        article = corpus[url]
        rules = article_rules.get(url) or {}
        articles.append({
            "article_url": url,
            "article_title": article.get("title") or "",
            "article_date": article.get("date") or "",
            "outlet": article.get("outlet") or "unknown",
            "flood_score": article.get("flood_score"),
            "kept_by_rule": url in rule_pass,
            "kept_by_image": url in articles_from_images,
            "image_count": sum(1 for k in kept if k["article_url"] == url),
            **rules,
        })

    labels = collections.Counter(
        k.get("nyc_label") or "unjudged" for k in kept
    )
    summary = {
        "flood_images_examined": len(rows),
        "flood_articles_examined": len({r["article_url"] for r in rows}),
        "images_judged": len(judged),
        "images_kept": len(kept),
        "articles_kept": len(articles),
        "kept_by_image_check": len(image_pass),
        "kept_by_article_rule_only": sum(
            1 for k in kept if k["kept_by_article"] and not k["kept_by_image"]
        ),
        "articles_passing_rule": len(rule_pass),
        "articles_added_by_an_image": len(articles_from_images - rule_pass),
        "kept_by_label": dict(labels),
        "ledger": str(ledger_path),
    }
    write_json(output_path, {
        "summary": summary,
        "rules_version": RULES_VERSION,
        "prompt": JUDGE_PROMPT,
        "keep_labels": list(KEEP_LABELS),
        "articles": articles,
        "results": kept,
    })
    return summary


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify scraped news images with a Hugging Face vision-language model.

The script passes remote image URLs to the inference provider. It never
downloads or saves image files locally.

Output follows the same two-stage shape as scrape.py: each classification is
appended to a JSONL as soon as it lands (so a crash or a credit limit loses
nothing mid-run), and at the end that JSONL is MERGED into the canonical
data/image_verification.json and deleted. The canonical JSON is therefore
the store of record, and the thing a re-run reads to know which images have
already been paid for. Pass --keep-jsonl to retain the intermediate file.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from huggingface_hub import InferenceClient


MODEL = "Qwen/Qwen3.8-27B:novita"
#: Selects usable STREET-LEVEL imagery, not flood imagery. Flood relevance is
#: already established upstream at the article level by verify.py's
#: flood_score/flood_verified, so every image reaching this prompt comes from a
#: flood story; what this pass decides is whether the photo carries the
#: ground-level detail geolocation needs. A `yes` therefore does NOT assert
#: that water is visible.
#:
#: Resume is deliberately PROMPT-AGNOSTIC: editing this string does not
#: re-classify images that already have an answer. Re-running is expensive, and
#: an existing answer is treated as good enough to keep. The consequence is
#: that after an edit the canonical JSON holds answers from more than one
#: prompt; each row records the exact `prompt` and `model` that produced it, so
#: the mix is always visible per row, and the run prints how many rows predate
#: the current prompt. To re-score everything under a new prompt, move the
#: canonical JSON aside so every occurrence reads as pending.
PROMPT = (
    "Is this a street-level photograph? Answer yes if the image shows anything "
    "a street view would contain: roads, streets, cars or other vehicles, "
    "people, buildings, houses, storefronts, signs, or similar ground-level "
    "surroundings. Water does not need to be present, and flooding is not "
    "required. Answer no only if the image is not a ground-level photograph of "
    "a real place, such as a map, radar or weather graphic, satellite or "
    "aerial view, chart, diagram, logo, screenshot, or a portrait or headshot "
    "with no surroundings visible. Answer yes or no."
)

ANSWER_RE = re.compile(r"\b(yes|no)\b", re.IGNORECASE)
FATAL_HTTP_CODES = {401, 402, 403}
#: Consecutive fatal-looking responses required before the whole run stops. A
#: real auth or credit failure fails EVERY request, so it reaches this in
#: seconds; a one-off 403 from a single image does not. Stopping on the first
#: one previously ended a 6,529-call run after 10 calls, on an image that
#: succeeded on retry.
FATAL_STREAK_LIMIT = 3
TRANSIENT_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


def load_env_file(path: Path) -> int:
    """Read KEY=VALUE lines from a .env into os.environ. Returns how many were set.

    Deliberately does NOT overwrite a variable that is already exported: a shell
    that has HF_TOKEN set is being explicit, and should win over a file that may
    be stale. Missing file is not an error -- the .env is one of three ways to
    supply the token, not a requirement.

    No dependency on python-dotenv; this is the whole format that matters here.
    """
    if not path.is_file():
        return 0
    loaded = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]          # strip matching quotes, keep inner ones
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def resolve_token(env_file: Path) -> tuple[str, str]:
    """HF token plus a description of where it came from, for the run log.

    Order: an exported HF_TOKEN, then the .env, then an interactive prompt.
    The token itself is never printed.
    """
    token = os.environ.get("HF_TOKEN")
    if token:
        return token, "the HF_TOKEN environment variable"

    if load_env_file(env_file):
        token = os.environ.get("HF_TOKEN")
        if token:
            return token, f"{env_file}"

    return getpass.getpass("HF token: "), "the interactive prompt"


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Classify every scraped image URL as flood footage or not."
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=project_root / "data" / "news_scrape_results.json",
        help="The corpus written by scraping/scrape.py "
             "(default: data/news_scrape_results.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=project_root / "scrape_data" / "image_verification.jsonl",
        help="Intermediate append-only JSONL; merged into --final-output and "
             "deleted when the run finishes.",
    )
    parser.add_argument(
        "--final-output",
        type=Path,
        default=project_root / "data" / "image_verification.json",
        help="Canonical JSON with the latest valid result for each occurrence. "
             "This is the resumable store of record.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=project_root / ".env",
        help="File to read HF_TOKEN from when it is not already exported "
             "(default: the project root .env). Ignored if missing.",
    )
    parser.add_argument(
        "--keep-jsonl",
        action="store_true",
        help="Keep the intermediate JSONL instead of deleting it after the merge.",
    )
    parser.add_argument(
        "--backend",
        choices=("local", "hf"),
        default="local",
        help="'local' runs the model on this machine's GPU (free, default); "
             "'hf' calls the hosted Hugging Face inference API (costs credits).",
    )
    parser.add_argument(
        "--model-path",
        default=None,
        help="Directory holding the local model weights "
             "(default: local_vlm.DEFAULT_MODEL_PATH). Used by --backend local.",
    )
    parser.add_argument(
        "--device-map",
        default="auto",
        help="accelerate device_map for the local model: 'auto', 'cuda:0', "
             "etc. Pin a single free GPU to leave the others alone.",
    )
    parser.add_argument("--model", default=MODEL,
                        help="Hosted model id. Used by --backend hf.")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent API requests (default: 4).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum distinct image URLs to call in this run; useful for testing.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Attempts for transient provider/network failures.",
    )
    return parser.parse_args()


def read_articles(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return payload


def make_occurrence_id(article_url: str, image_url: str) -> str:
    """Stable identity for one image as it appears in one article.

    Deliberately NOT positional. An earlier scheme hashed the image's index
    within the article too, so that the same URL used twice in one article
    stayed two rows -- but Adapter.images() dedupes by URL per article
    (adapters.py `seen`), and scrape.py's feed-image merge does the same, so
    that state cannot be emitted. The index bought nothing and made the key
    change whenever a publisher inserted a photo mid-article, orphaning every
    classification below it. Caption and index are metadata, not identity:
    a re-crawl that rewrites a caption must not strand a paid-for result.
    """
    value = f"{article_url}\n{image_url}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def iter_image_occurrences(corpus: Path) -> Iterable[dict[str, Any]]:
    """One occurrence per (article, image) across the whole corpus.

    Reads the combined corpus, not the per-outlet files: those are the crawl's
    working files, merged into this one at the end of every crawl. Article
    ORDER here differs from the old per-outlet walk, which changes
    `article_index` -- but not `occurrence_id`, which is derived from URLs
    alone, so resume is unaffected by the switch.
    """
    for article_index, article in enumerate(read_articles(corpus)):
        article_url = str(article.get("url") or "")
        for image_index, image in enumerate(article.get("images") or []):
            image_url = str(image.get("url") or "").strip()
            if not image_url:
                continue
            yield {
                "occurrence_id": make_occurrence_id(article_url, image_url),
                "image_url": image_url,
                "caption": image.get("caption") or "",
                "image_source": image.get("source"),
                "image_index": image_index,
                "article_index": article_index,
                "article_title": article.get("title") or "",
                "article_url": article_url,
                "article_date": article.get("date"),
                "article_flood_score": article.get("flood_score"),
                "article_flood_verified": article.get("flood_verified"),
                "outlet": article.get("outlet") or "",
                "source_file": corpus.name,
            }


def is_completed(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") == "completed"
        and result.get("answer") in {"yes", "no"}
    )


def read_existing_results(
    final_path: Path, history_path: Path, prompt: str, model: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]],
           dict[str, dict[str, str]], int]:
    """Completed classifications on disk.

    Returns (by id, url_cache, sha_cache, off_prompt_count). Two caches, each
    catching a different kind of repeat:

      url_cache  same image_url -> never re-fetched, never re-classified.
      sha_cache  same PIXELS behind a different url -> fetched (we cannot know
                 the hash without the bytes) but never re-classified.

    The sha cache is what stops the same wire photo, syndicated across outlets
    under different URLs, from being sent to the GPU once per outlet and
    entering the results file as several independent rows.

    EVERY completed result is reusable, whatever prompt or model produced it --
    an answer already paid for is never re-requested just because the criteria
    changed since. `prompt` and `model` are used only to count how many stored
    rows predate the current run, which is reported and changes nothing.

    The canonical JSON is read first because it is the store of record: the
    history JSONL is deleted once merged, so on a normal re-run the canonical
    file is the ONLY thing standing between us and paying the provider a second
    time for images already classified. The JSONL is read second, and wins on
    conflict, because when it does exist it is a crashed run's un-merged tail.
    """
    latest: dict[str, dict[str, Any]] = {}
    url_cache: dict[str, dict[str, str]] = {}
    sha_cache: dict[str, dict[str, str]] = {}
    off_prompt: set[str] = set()

    def absorb(result: Any) -> None:
        if not is_completed(result):
            return
        occurrence_id = result.get("occurrence_id")
        if occurrence_id:
            latest[str(occurrence_id)] = result
            if result.get("prompt") != prompt or result.get("model") != model:
                off_prompt.add(str(occurrence_id))
            else:
                off_prompt.discard(str(occurrence_id))
        answer_pair = {
            "answer": str(result["answer"]),
            "model_output": str(result.get("model_output") or ""),
        }
        image_url = result.get("image_url")
        if image_url:
            url_cache[str(image_url)] = answer_pair
        # Rows classified before content hashing was added carry no sha; they
        # simply do not contribute to this cache.
        sha = result.get("image_sha256")
        if sha:
            sha_cache[str(sha)] = answer_pair

    if final_path.exists():
        try:
            payload = json.loads(final_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(
                f"Refusing to run: {final_path} exists but is unreadable ({exc}). "
                "It is the record of what has already been classified; move it "
                "aside deliberately if you really mean to start over."
            )
        rows = payload.get("results") if isinstance(payload, dict) else None
        for result in rows or []:
            absorb(result)

    if history_path.exists():
        with history_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    print(
                        f"Warning: ignoring malformed line {line_number} "
                        f"in {history_path}",
                        file=sys.stderr,
                    )
                    continue
                absorb(result)

    return latest, url_cache, sha_cache, len(off_prompt)


#: A status is only read out of free text when it is stated AS a status. The
#: previous version matched any bare 4xx/5xx-looking number anywhere in the
#: message -- and the message embeds the image URL, which for these CDNs is
#: full of numbers (`cropW=1200`, `width=862`, `height=485`). That turned an
#: ordinary transient failure into a phantom 401/402/403 and stopped the run.
_URL_RE = re.compile(r"https?://\S+")
_STATUS_RE = re.compile(
    r"(?:status(?:\s*code)?|HTTP(?:/\d(?:\.\d)?)?|response)\D{0,3}\b([45]\d\d)\b",
    re.IGNORECASE,
)


def extract_http_status(exc: BaseException) -> int | None:
    """HTTP status behind a provider exception, or None if not determinable."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    text = _URL_RE.sub("<url>", str(exc))      # never read a status out of a URL
    match = _STATUS_RE.search(text)
    return int(match.group(1)) if match else None


def response_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif getattr(item, "text", None):
                parts.append(str(item.text))
        return " ".join(parts).strip()
    return str(content or "").strip()


def provider_image_url(image_url: str) -> str:
    """Return a provider-safe URL without changing the recorded scraped URL.

    NPR's Brightspot resize URLs wrap an HTTP S3 origin in their query string.
    Some inference providers reject that nested URL with HTTP 400. The same
    image is available from the origin over HTTPS, so submit that URL instead.
    """
    parsed = urlsplit(image_url)
    if parsed.hostname and parsed.hostname.endswith("brightspotcdn.com"):
        origin = parse_qs(parsed.query).get("url", [None])[0]
        if origin:
            if origin.startswith("http://"):
                origin = "https://" + origin.removeprefix("http://")
            return origin
    return image_url


def classify_url(
    image_url: str,
    token: str,
    model: str,
    retries: int,
    stop_event: threading.Event,
) -> dict[str, Any]:
    if stop_event.is_set():
        return {"status": "cancelled"}

    client = InferenceClient(api_key=token)
    submitted_url = provider_image_url(image_url)
    for attempt in range(1, retries + 1):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": submitted_url},
                            },
                        ],
                    }
                ],
                max_tokens=96,
                temperature=0,
            )
            text = response_text(completion.choices[0].message.content)
            match = ANSWER_RE.search(text)
            if not match:
                if attempt < retries:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                result = {
                    "status": "invalid_output",
                    "model_output": text,
                    "error": "Model output did not contain yes or no.",
                    "attempts": attempt,
                }
                if submitted_url != image_url:
                    result["submitted_image_url"] = submitted_url
                return result
            result = {
                "status": "completed",
                "answer": match.group(1).lower(),
                "model_output": text,
                "attempts": attempt,
            }
            if submitted_url != image_url:
                result["submitted_image_url"] = submitted_url
            return result
        except Exception as exc:  # provider exceptions vary by hub version
            status = extract_http_status(exc)
            fatal = status in FATAL_HTTP_CODES
            transient = status in TRANSIENT_HTTP_CODES or status is None
            # Deliberately does NOT stop the run here. Whether an auth/credit
            # failure is real is a question about the RUN, not one request, so
            # the consumer loop decides after seeing a streak of them.
            if fatal or not transient or attempt >= retries:
                result = {
                    "status": "error",
                    "http_status": status,
                    "error": str(exc),
                    "attempts": attempt,
                    "fatal": fatal,
                }
                if submitted_url != image_url:
                    result["submitted_image_url"] = submitted_url
                return result
            time.sleep(min(2 ** (attempt - 1), 8))

    raise AssertionError("unreachable")


def result_record(
    occurrence: dict[str, Any],
    classification: dict[str, Any],
    model: str,
    reused: bool,
) -> dict[str, Any]:
    return {
        **occurrence,
        "model": model,
        "prompt": PROMPT,
        **classification,
        "reused_for_duplicate_url": reused,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def append_result(handle: Any, result: dict[str, Any]) -> None:
    handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    handle.flush()


def write_final_results(
    history_path: Path,
    final_path: Path,
    occurrences: list[dict[str, Any]],
    known: dict[str, dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Merge this run's history into the canonical JSON, atomically.

    MUST merge, not overwrite. `known` holds what the canonical file already
    contained, so a result whose article has since left the data dir (an outlet
    file renamed, a narrower --data-dir, an article dropped by a re-crawl) is
    carried forward rather than silently dropped. Dropping it is unrecoverable
    now that the history JSONL does not outlive the run, and re-acquiring it
    means paying the provider again.
    """
    latest: dict[str, dict[str, Any]] = dict(known or {})
    if history_path.exists():
        with history_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if is_completed(result) and result.get("occurrence_id"):
                    latest[str(result["occurrence_id"])] = result

    # Current data-dir order first (that is the order the review site reads),
    # then anything carried over from earlier runs.
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for occurrence in occurrences:
        occurrence_id = occurrence["occurrence_id"]
        if occurrence_id in latest and occurrence_id not in seen:
            ordered.append(latest[occurrence_id])
            seen.add(occurrence_id)
    ordered.extend(r for oid, r in latest.items() if oid not in seen)

    # Resume is prompt-agnostic, so rows scored under an earlier prompt are kept
    # rather than re-run. That makes a mixed file normal, not a fault -- report
    # the mix so it is visible without inspecting every row.
    rows_on_current_prompt = sum(r.get("prompt") == PROMPT for r in ordered)
    summary = {
        "source_occurrences": len(occurrences),
        "completed_occurrences": len(seen),
        "missing_occurrences": len(occurrences) - len(seen),
        "carried_occurrences": len(ordered) - len(seen),
        "stored_occurrences": len(ordered),
        "on_current_prompt": rows_on_current_prompt,
        "on_earlier_prompt": len(ordered) - rows_on_current_prompt,
        "distinct_prompts": len({r.get("prompt") for r in ordered}),
        "yes": sum(r["answer"] == "yes" for r in ordered),
        "no": sum(r["answer"] == "no" for r in ordered),
    }
    payload = {
        "summary": summary,
        # The prompt this run would use -- NOT necessarily the one behind every
        # row. Each row carries its own `prompt`; that is the authoritative one.
        "current_prompt": PROMPT,
        "prompt": PROMPT,
        "results": ordered,
    }
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = final_path.with_name(final_path.name + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary_path, final_path)
    return summary


def discard_history(history_path: Path, keep: bool) -> None:
    """Drop the intermediate JSONL. Only ever called after the atomic replace
    in write_final_results has landed, so the results are already durable."""
    if keep or not history_path.exists():
        return
    try:
        history_path.unlink()
    except OSError as exc:
        print(f"Warning: could not remove {history_path}: {exc}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    if args.retries < 1:
        raise SystemExit("--retries must be at least 1")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    if not args.corpus.is_file():
        raise SystemExit(
            f"No corpus at {args.corpus}. It is written by the crawl:\n"
            f"  python3 scraping/scrape.py --outlets all --resume"
        )
    occurrences = list(iter_image_occurrences(args.corpus))
    # The model NAME must be known before reading existing results, or the
    # off-prompt count compares stored rows against the hosted model id even on
    # a local run and reports almost everything as stale.
    if args.backend == "local":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from local_vlm import DEFAULT_MODEL_PATH
        model_path = args.model_path or DEFAULT_MODEL_PATH
        model_name = f"local:{Path(model_path).name}"
    else:
        model_path = None
        model_name = args.model

    completed, url_cache, sha_cache, off_prompt = read_existing_results(
        args.final_output, args.output, PROMPT, model_name
    )
    # (resume is prompt-agnostic; `model` here only feeds the informational count)
    pending = [o for o in occurrences if o["occurrence_id"] not in completed]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for occurrence in pending:
        grouped.setdefault(occurrence["image_url"], []).append(occurrence)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"Found {len(occurrences)} image occurrences, "
        f"{len({o['image_url'] for o in occurrences})} distinct URLs."
    )
    print(
        f"Already completed: {len(completed)} occurrences. "
        f"Pending: {len(pending)} occurrences."
    )
    if sha_cache:
        print(
            f"Known image contents: {len(sha_cache)} "
            f"(a pending image whose pixels match one of these is fetched but "
            f"not re-classified)."
        )
    if off_prompt:
        print(
            f"Note: {off_prompt} stored result(s) came from a different prompt or "
            f"model and are being KEPT as-is, not re-classified. Each row records "
            f"the prompt and model that produced it."
        )
    if not pending:
        summary = write_final_results(
            args.output, args.final_output, occurrences, completed
        )
        discard_history(args.output, args.keep_jsonl)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from make_metadata import DEFAULT_VERIFICATION, refresh_quietly
        # Canonical metadata only ever comes from the canonical file.
        if args.final_output.resolve() == DEFAULT_VERIFICATION.resolve():
            refresh_quietly(args.final_output)
        print(f"Nothing to classify. Canonical results: {args.final_output}")
        print(f"Summary: {summary}")
        return 0

    # One place decides what a single classification means; everything below is
    # backend-agnostic and keeps working unchanged for either path.
    stop_event = threading.Event()
    if args.backend == "local":
        from local_vlm import LocalVLM

        # The sha cache is shared with the backend, which extends it as the
        # run proceeds -- duplicates discovered mid-run are reused too.
        vlm = LocalVLM(model_path, device_map=args.device_map,
                       sha_cache=sha_cache)
        assert vlm.name == model_name
        print(f"Loading {vlm.model_path} onto {args.device_map} ...", flush=True)
        load_started = time.monotonic()
        try:
            vlm.load()
        except Exception as exc:
            raise SystemExit(
                f"Could not load the local model: {type(exc).__name__}: {exc}\n"
                f"Download it first:  python3 local_vlm/download_model.py"
            )
        print(f"Loaded in {time.monotonic() - load_started:.0f}s.", flush=True)

        def classify(image_url: str) -> dict[str, Any]:
            return vlm.classify(image_url, PROMPT, args.retries, stop_event)
    else:
        token, token_source = resolve_token(args.env_file)
        if not token:
            raise SystemExit("No Hugging Face token supplied.")
        print(f"Using HF token from {token_source}.")

        def classify(image_url: str) -> dict[str, Any]:
            return classify_url(image_url, token, args.model, args.retries,
                                stop_event)

    counts = {"yes": 0, "no": 0, "invalid_output": 0, "error": 0}
    written = 0
    model_calls = 0
    fatal_streak = 0

    with args.output.open("a", encoding="utf-8") as output_handle:
        # Materialize any duplicate URLs that were classified in an earlier run.
        for image_url in list(grouped):
            cached = url_cache.get(image_url)
            if not cached:
                continue
            for occurrence in grouped.pop(image_url):
                classification = {
                    "status": "completed",
                    "answer": cached["answer"],
                    "model_output": cached["model_output"],
                    "attempts": 0,
                }
                append_result(
                    output_handle,
                    result_record(occurrence, classification, model_name, True),
                )
                counts[cached["answer"]] += 1
                written += 1

        urls = list(grouped)
        if args.limit is not None:
            urls = urls[: args.limit]

        futures: dict[Future[dict[str, Any]], str] = {}
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for image_url in urls:
                futures[executor.submit(classify, image_url)] = image_url

            total_calls = len(futures)
            for future in as_completed(futures):
                image_url = futures[future]
                classification = future.result()
                if classification.get("status") == "cancelled":
                    continue
                model_calls += 1
                occurrences_for_url = grouped[image_url]
                for duplicate_index, occurrence in enumerate(occurrences_for_url):
                    append_result(
                        output_handle,
                        result_record(
                            occurrence,
                            classification,
                            model_name,
                            duplicate_index > 0,
                        ),
                    )
                    written += 1

                status = classification.get("status")
                answer = classification.get("answer")
                if answer in {"yes", "no"}:
                    counts[str(answer)] += len(occurrences_for_url)
                elif status in counts:
                    counts[str(status)] += len(occurrences_for_url)

                print(
                    f"[{model_calls}/{total_calls}] "
                    f"{answer or status}: {image_url[:110]}",
                    flush=True,
                )
                # Only an unbroken run of fatal responses means the account,
                # not the image, is the problem. Anything that succeeds or
                # fails differently clears it.
                if classification.get("fatal"):
                    fatal_streak += 1
                    print(
                        f"  auth/credit-style response "
                        f"({classification.get('http_status')}), "
                        f"{fatal_streak}/{FATAL_STREAK_LIMIT} in a row: "
                        f"{str(classification.get('error'))[:160]}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if fatal_streak >= FATAL_STREAK_LIMIT:
                        stop_event.set()
                        print(
                            f"{FATAL_STREAK_LIMIT} consecutive auth/credit failures; "
                            "stopping safely. Re-run later to resume.",
                            file=sys.stderr,
                            flush=True,
                        )
                else:
                    fatal_streak = 0

    print(
        "Finished this run: "
        f"{model_calls} model calls, {written} occurrence records; "
        f"yes={counts['yes']}, no={counts['no']}, "
        f"invalid={counts['invalid_output']}, errors={counts['error']}."
    )
    summary = write_final_results(
        args.output, args.final_output, occurrences, completed
    )
    discard_history(args.output, args.keep_jsonl)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from make_metadata import DEFAULT_VERIFICATION, refresh_quietly
    # Canonical metadata only ever comes from the canonical file.
    if args.final_output.resolve() == DEFAULT_VERIFICATION.resolve():
        refresh_quietly(args.final_output)
    print(f"Canonical results: {args.final_output}")
    if args.keep_jsonl:
        print(f"History kept: {args.output}")
    print(f"Canonical summary: {summary}")
    return 1 if counts["error"] or counts["invalid_output"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

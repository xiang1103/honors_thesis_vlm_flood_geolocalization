"""Verify scraped news images with a Hugging Face vision-language model.

The script passes remote image URLs to the inference provider. It never
downloads or saves image files locally. Results are appended to JSONL so an
interrupted or credit-limited run can be resumed without repeating work.
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
PROMPT = (
    "Is this an image that contains flooding footage? The image must show "
    "standing water covering ground, roads, or building foundations, or "
    "significant rising water levels that submerge dry land structures. The "
    "water must be visible in the foreground or midground as the primary or "
    "dominant element indicating inundation. Answer yes or no."
)

ANSWER_RE = re.compile(r"\b(yes|no)\b", re.IGNORECASE)
FATAL_HTTP_CODES = {401, 402, 403}
TRANSIENT_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Classify every scraped image URL as flood footage or not."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=project_root / "data" / "outlets",
        help="Directory containing *_flood.json article files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=project_root / "data" / "image_vlm_verification.jsonl",
        help="Append-only JSONL result file.",
    )
    parser.add_argument(
        "--final-output",
        type=Path,
        default=project_root / "data" / "image_vlm_verification_final.json",
        help="Canonical JSON with the latest valid result for each occurrence.",
    )
    parser.add_argument("--model", default=MODEL)
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


def make_occurrence_id(article_url: str, image_index: int, image_url: str) -> str:
    value = f"{article_url}\n{image_index}\n{image_url}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def iter_image_occurrences(data_dir: Path) -> Iterable[dict[str, Any]]:
    for source_file in sorted(data_dir.glob("*_flood.json")):
        for article_index, article in enumerate(read_articles(source_file)):
            article_url = str(article.get("url") or "")
            for image_index, image in enumerate(article.get("images") or []):
                image_url = str(image.get("url") or "").strip()
                if not image_url:
                    continue
                yield {
                    "occurrence_id": make_occurrence_id(
                        article_url, image_index, image_url
                    ),
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
                    "outlet": article.get("outlet") or source_file.stem.removesuffix(
                        "_flood"
                    ),
                    "source_file": source_file.name,
                }


def read_existing_results(
    output_path: Path,
) -> tuple[set[str], dict[str, dict[str, str]]]:
    completed: set[str] = set()
    url_cache: dict[str, dict[str, str]] = {}
    if not output_path.exists():
        return completed, url_cache

    with output_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                print(
                    f"Warning: ignoring malformed line {line_number} in {output_path}",
                    file=sys.stderr,
                )
                continue
            if result.get("status") == "completed" and result.get("answer") in {
                "yes",
                "no",
            }:
                occurrence_id = result.get("occurrence_id")
                image_url = result.get("image_url")
                if occurrence_id:
                    completed.add(str(occurrence_id))
                if image_url:
                    url_cache[str(image_url)] = {
                        "answer": str(result["answer"]),
                        "model_output": str(result.get("model_output") or ""),
                    }
    return completed, url_cache


def extract_http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
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
            if fatal:
                stop_event.set()
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
) -> dict[str, int]:
    latest: dict[str, dict[str, Any]] = {}
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
                if result.get("status") == "completed" and result.get("answer") in {
                    "yes",
                    "no",
                }:
                    occurrence_id = result.get("occurrence_id")
                    if occurrence_id:
                        latest[str(occurrence_id)] = result

    ordered = [
        latest[o["occurrence_id"]]
        for o in occurrences
        if o["occurrence_id"] in latest
    ]
    summary = {
        "source_occurrences": len(occurrences),
        "completed_occurrences": len(ordered),
        "missing_occurrences": len(occurrences) - len(ordered),
        "yes": sum(r["answer"] == "yes" for r in ordered),
        "no": sum(r["answer"] == "no" for r in ordered),
    }
    payload = {
        "summary": summary,
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


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    if args.retries < 1:
        raise SystemExit("--retries must be at least 1")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    occurrences = list(iter_image_occurrences(args.data_dir))
    completed, url_cache = read_existing_results(args.output)
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
    if not pending:
        summary = write_final_results(args.output, args.final_output, occurrences)
        print(f"Nothing to classify. History: {args.output}")
        print(f"Canonical results: {args.final_output}")
        print(f"Summary: {summary}")
        return 0

    token = os.environ.get("HF_TOKEN") or getpass.getpass("HF token: ")
    if not token:
        raise SystemExit("No Hugging Face token supplied.")

    counts = {"yes": 0, "no": 0, "invalid_output": 0, "error": 0}
    written = 0
    model_calls = 0

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
                    result_record(occurrence, classification, args.model, True),
                )
                counts[cached["answer"]] += 1
                written += 1

        urls = list(grouped)
        if args.limit is not None:
            urls = urls[: args.limit]

        stop_event = threading.Event()
        futures: dict[Future[dict[str, Any]], str] = {}
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for image_url in urls:
                futures[
                    executor.submit(
                        classify_url,
                        image_url,
                        token,
                        args.model,
                        args.retries,
                        stop_event,
                    )
                ] = image_url

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
                            args.model,
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
                if classification.get("fatal"):
                    print(
                        "Fatal provider response; stopping safely. Re-run later to resume.",
                        file=sys.stderr,
                        flush=True,
                    )

    print(
        "Finished this run: "
        f"{model_calls} model calls, {written} occurrence records; "
        f"yes={counts['yes']}, no={counts['no']}, "
        f"invalid={counts['invalid_output']}, errors={counts['error']}."
    )
    summary = write_final_results(args.output, args.final_output, occurrences)
    print(f"History: {args.output}")
    print(f"Canonical results: {args.final_output}")
    print(f"Canonical summary: {summary}")
    return 1 if counts["error"] or counts["invalid_output"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Local GPU inference for the image classifier.

Everything about running the model lives here: loading it, fetching and
decoding the image, prompting, and normalising the answer. `verification/verify_images_vlm.py`
keeps the orchestration it already had -- occurrence flattening, URL dedupe,
resume, JSONL merge -- and only swaps where a single classification comes from.

The returned dict is deliberately the SAME shape the hosted path returns
(`status`, `answer`, `model_output`, `attempts`, and on failure `error`), so
the caller needs no branching per backend.

One real behavioural difference from the hosted API: there, the provider
fetched the image URL. Here we fetch it ourselves, into memory. Nothing is
written to disk.
"""
from __future__ import annotations

import hashlib
import io
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

ANSWER_RE = re.compile(r"\b(yes|no)\b", re.IGNORECASE)

#: This is a REASONING model: its chat template defaults to thinking on at
#: reasoning_effort='xhigh'. Left on, it emits an analysis before the verdict,
#: and that analysis routinely contains the phrase "yes/no" -- so taking the
#: first regex match parsed the word out of the question instead of the answer,
#: and every result came back a confident, meaningless "yes".
THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
UNTERMINATED_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def image_digests(image) -> dict[str, str]:
    """Content fingerprints for one decoded image.

    `image_sha256` hashes the raw RGB pixel buffer, so it is identical only for
    byte-identical pictures -- the same file served from two URLs. It is
    deliberately NOT a hash of the response body: that would differ for the
    same image re-encoded at a different quality.

    `image_dhash` is a 64-bit difference hash (resize to 9x8 grey, compare
    horizontally adjacent pixels). Close images give close hashes, so Hamming
    distance finds re-crops and resizes that sha256 cannot. Stored, never
    acted on automatically -- near-duplicate is a judgement, not a fact.
    """
    sha = hashlib.sha256(image.tobytes()).hexdigest()

    small = image.convert("L").resize((9, 8))
    pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            bits = (bits << 1) | int(left > right)
    return {"image_sha256": sha, "image_dhash": f"{bits:016x}"}


def parse_answer(text: str) -> str | None:
    """Final yes/no verdict in a model reply, or None if it never gives one.

    Thinking is stripped first, then the LAST match wins: the verdict comes at
    the end, while anything resembling an answer earlier is the model restating
    the question. Returning None (rather than guessing) is what lets a
    non-answer be retried instead of silently recorded.
    """
    body = THINK_BLOCK_RE.sub(" ", text)
    body = UNTERMINATED_THINK_RE.sub(" ", body)   # truncated mid-thought
    matches = ANSWER_RE.findall(body)
    return matches[-1].lower() if matches else None

#: Browser-ish UA. Some publisher CDNs answer 403 to an obvious bot, and unlike
#: the hosted path there is no provider fetching on our behalf to absorb that.
FETCH_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

#: Upper bound on vision tokens per image. News photos arrive at wildly
#: different resolutions and the token cost scales with pixel count, so a cap
#: keeps latency and memory predictable across 8k images.
DEFAULT_MAX_PIXELS = 1280 * 28 * 28


class LocalVLM:
    """A loaded vision-language model, shared across worker threads.

    Loading is lazy and idempotent: the first `classify()` pays for it, later
    calls reuse it. Generation is serialised behind a lock -- one GPU, one
    forward pass at a time -- while image fetching and decoding stay outside
    the lock so network I/O from several workers overlaps with compute.
    """

    def __init__(
        self,
        model_path: str | Path,
        device_map: str = "auto",
        dtype: str = "bfloat16",
        max_new_tokens: int = 96,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        fetch_timeout: int = 30,
        enable_thinking: bool = False,
    ) -> None:
        self.model_path = str(model_path)
        self.device_map = device_map
        self.dtype = dtype
        self.max_new_tokens = max_new_tokens
        self.max_pixels = max_pixels
        self.fetch_timeout = fetch_timeout
        # Off by default: the verdict is what we store, the reasoning is cost.
        # With it on, max_new_tokens must be raised a long way or the reply is
        # cut off mid-thought and never reaches an answer.
        self.enable_thinking = enable_thinking

        self._model = None
        self._processor = None
        self._load_lock = threading.Lock()
        self._gpu_lock = threading.Lock()
        self._session = requests.Session()

    # -- identity ---------------------------------------------------------
    @property
    def name(self) -> str:
        """Recorded as each row's `model`, so local answers are tellable from
        hosted ones in the results file."""
        return f"local:{Path(self.model_path).name}"

    # -- loading ----------------------------------------------------------
    def load(self) -> None:
        """Load weights onto the GPU. Safe to call from several threads."""
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor

            dtype = getattr(torch, self.dtype)
            # local_files_only: fail loudly on a wrong path instead of silently
            # re-downloading 56 GB from the hub.
            self._processor = AutoProcessor.from_pretrained(
                self.model_path,
                local_files_only=True,
                max_pixels=self.max_pixels,
            )
            model = AutoModelForImageTextToText.from_pretrained(
                self.model_path,
                dtype=dtype,
                device_map=self.device_map,
                local_files_only=True,
            )
            model.eval()
            self._model = model

    # -- image ------------------------------------------------------------
    def fetch_image(self, url: str):
        """URL -> (RGB PIL image, digests). Raises on failure."""
        from PIL import Image

        response = self._session.get(
            url, headers=FETCH_HEADERS, timeout=self.fetch_timeout
        )
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
        return image, image_digests(image)

    # -- inference --------------------------------------------------------
    def _generate(self, image, prompt: str) -> str:
        import torch

        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        }]
        with self._gpu_lock:
            inputs = self._processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
                enable_thinking=self.enable_thinking,
            ).to(self._model.device)
            with torch.inference_mode():
                generated = self._model.generate(
                    **inputs,
                    max_new_tokens=self.max_new_tokens,
                    do_sample=False,          # deterministic, matches temperature=0
                )
            # Keep only the newly generated tail, not the echoed prompt.
            trimmed = generated[0][inputs["input_ids"].shape[1]:]
            return self._processor.decode(
                trimmed, skip_special_tokens=True
            ).strip()

    def classify(
        self,
        image_url: str,
        prompt: str,
        retries: int = 3,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Classify one image URL. Mirrors the hosted path's return shape."""
        if stop_event is not None and stop_event.is_set():
            return {"status": "cancelled"}

        self.load()

        for attempt in range(1, retries + 1):
            try:
                image, digests = self.fetch_image(image_url)
            except Exception as exc:
                # A dead or blocked image URL is permanent for this image and
                # unrelated to the model, so do not burn every retry on it.
                return {
                    "status": "error",
                    "error": f"image fetch failed: {type(exc).__name__}: {exc}",
                    "attempts": attempt,
                    "fatal": False,
                }
            try:
                text = self._generate(image, prompt)
            except Exception as exc:
                if attempt < retries:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                return {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "attempts": attempt,
                    "fatal": False,
                }

            answer = parse_answer(text)
            if answer:
                return {
                    "status": "completed",
                    "answer": answer,
                    "model_output": text,
                    "attempts": attempt,
                    **digests,
                }
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            return {
                "status": "invalid_output",
                "model_output": text,
                "error": "Model output did not contain yes or no.",
                "attempts": attempt,
            }

        raise AssertionError("unreachable")

"""Legacy ordinal labels, object-height references, and answer parsers.

Human labels still use L0-L5/X. Current model prompts return numeric
centimeter intervals (or X); the old level parser remains for the pilot data.
"""
from __future__ import annotations

import re

#: level -> (lower cm, upper cm, short description). Upper bound None = open.
LEVELS: dict[str, tuple[int, int | None, str]] = {
    "L0": (0, 0, "no standing water on the ground: dry, wet from rain, or only puddles"),
    "L1": (0, 15, "under 15 cm"),
    "L2": (15, 50, "15 to 50 cm"),
    "L3": (50, 100, "50 cm to 1 m"),
    "L4": (100, 150, "1 m to 1.5 m"),
    "L5": (150, None, "over 1.5 m"),
}
ORDINAL = list(LEVELS)                    # "L0".."L5"
UNKNOWN = "X"
ANSWERS = ORDINAL + [UNKNOWN]


def level_index(level: str | None) -> int | None:
    """"L3" -> 3; X / None / anything else -> None."""
    return ORDINAL.index(level) if level in ORDINAL else None


#: Heights of common street objects, in cm, measured from the ground. Given to
#: the model ONLY in conditions C4 and C5; C4 - C1 measures what the table adds.
#: Typical values for US streets; not exact for any one object.
REFERENCE_HEIGHTS: list[tuple[str, str]] = [
    ("curb top", "about 15 cm"),
    ("adult ankle", "about 10 cm"),
    ("adult knee", "about 50 cm"),
    ("adult waist / hip", "about 100 cm"),
    ("adult chest", "about 130 cm"),
    ("adult head top", "about 170 cm"),
    ("car wheel hub (centre of the wheel)", "about 33 cm"),
    ("car tyre top", "about 65 cm"),
    ("car door sill / bottom of the doors", "about 30 cm"),
    ("car bumper top", "about 50 cm"),
    ("car bonnet / hood", "about 90 cm"),
    ("car roof (sedan)", "about 145 cm"),
    ("SUV / pickup roof", "about 180 cm"),
    ("bus or truck wheel top", "about 100 cm"),
    ("fire hydrant top", "about 70 cm"),
    ("parking meter head", "about 120 cm"),
    ("mailbox (USPS blue box) top", "about 125 cm"),
    ("house front-door step", "about 15-20 cm per step"),
    ("door handle", "about 100 cm"),
    ("bottom of a stop sign", "about 210 cm"),
]


def reference_table() -> str:
    return "\n".join(f"- {name}: {height}" for name, height in REFERENCE_HEIGHTS)


THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
UNTERMINATED_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
ANSWER_RE = re.compile(r"\b(L\s?[0-5]|X)\b", re.IGNORECASE)
#: One standalone depth answer: an optional lead-in ("about", "Depth:"), a
#: number, optionally "-" / "to" and a second number, and an optional unit.
#: No unit means cm. Any width is accepted (owner, 2026-10-21: an answer
#: outside the requested 5 cm is still kept, not dropped).
_NUM = r"(\d{1,4}(?:[.,]\d+)?)"
_UNIT = r"(cm|centimet(?:er|re)s?|m|met(?:er|re)s?)?"
DEPTH_ANSWER_RE = re.compile(
    r"^\W*(?:(?:approximately|approx\.?|about|around|roughly|estimated|estimate|depth|answer)\W*)*"
    rf"~?\s*{_NUM}\s*{_UNIT}\s*(?:(?:-|–|—|to)\s*~?\s*{_NUM}\s*{_UNIT})?\W*$",
    re.IGNORECASE)


def parse_level(text: str) -> str | None:
    """The model's final level ("L0".."L5" or "X"), or None if none is given.

    Same discipline as local_vlm.backend.parse_answer: strip thinking, take the
    LAST match (an earlier one is the model restating the scale). A bare digit
    is NOT accepted -- "15 cm" must not parse as L1.
    """
    body = THINK_BLOCK_RE.sub(" ", text)
    body = UNTERMINATED_THINK_RE.sub(" ", body)
    matches = ANSWER_RE.findall(body)
    if not matches:
        return None
    last = matches[-1].upper().replace(" ", "")
    return last if last in ANSWERS else None


def _cm(number: str, unit: str | None) -> float:
    value = float(number.replace(",", "."))
    return value * 100 if unit and unit.lower().startswith("m") else value


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def parse_depth_range(text: str) -> str | None:
    """A standalone depth answer, normalised: ``"35-40 cm"``, ``"35 cm"`` (a
    single value) or ``"X"``; None if the reply is not one.

    Any interval width is kept -- the prompt asks for at most 5 cm, but a
    wider answer is still the model's estimate (owner, 2026-10-21); use
    interval_cm() / within_5cm() to measure it. Metres are converted to cm, a
    reversed interval is put in order. Only a reply that IS an answer
    is read, so a number inside an explanation is never taken for one.
    """
    body = THINK_BLOCK_RE.sub(" ", text)
    body = UNTERMINATED_THINK_RE.sub(" ", body).strip().strip("*_` ")
    if body.upper().rstrip(".! ") == UNKNOWN:
        return UNKNOWN
    match = DEPTH_ANSWER_RE.fullmatch(body)
    if not match:
        return None
    n1, u1, n2, u2 = match.groups()
    if n2 is None:
        return f"{_fmt(_cm(n1, u1))} cm"
    # "1-1.5 m": a unit written only after the second number applies to both
    low, high = sorted((_cm(n1, u1 or u2), _cm(n2, u2)))
    return f"{_fmt(low)}-{_fmt(high)} cm"


def interval_cm(answer: str | None) -> tuple[float, float] | None:
    """A normalised answer -> (low, high) in cm; a single value is (v, v).
    None for X, null, or anything else."""
    if not answer or answer == UNKNOWN or not answer.endswith(" cm"):
        return None
    parts = answer[:-3].split("-")
    try:
        values = [float(x) for x in parts]
    except ValueError:
        return None
    return (values[0], values[-1]) if len(values) in (1, 2) else None


def midpoint_cm(answer: str | None) -> float | None:
    iv = interval_cm(answer)
    return None if iv is None else (iv[0] + iv[1]) / 2


def within_5cm(answer: str | None) -> bool | None:
    """True if the answer is an interval at most 5 cm wide (what was asked);
    False for a wider interval or a single value; None if not numeric."""
    iv = interval_cm(answer)
    if iv is None:
        return None
    return "-" in answer and iv[1] - iv[0] <= 5

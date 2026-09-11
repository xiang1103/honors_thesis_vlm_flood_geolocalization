"""
Verify collected news are actually flood-related 

CBS's flooding tag is human-curated, so precision is already high at the source

Records are SCORED, never dropped -- the threshold stays tunable after the fact
without re-crawling. If an audit later shows this is insufficient, run an LLM
pass over the low-scoring tail only.
"""
import re

# Strong terms: essentially unambiguous flood-event vocabulary.
STRONG = re.compile(
    r"\b(flood(ing|ed|s|water|waters|plain)?|flash\s+flood|floodwater|"
    r"inundat(e|ed|ion)|levee|storm\s+surge|deluge|overflow(ed|ing)?\s+(its\s+)?banks|"
    r"high\s+water|water\s+rescue|swollen\s+river|torrential)\b", re.I)

# Weak terms: flood-adjacent, only meaningful alongside a strong hit.
WEAK = re.compile(
    r"\b(rain(fall|storm)?|storm|hurricane|typhoon|monsoon|evacuat(e|ed|ion)|"
    r"river|dam|creek|levee|rescue|submerged|washed\s+out|mudslide|landslide)\b", re.I)

# "a flood of X" -- the metaphor that produced false positives for the
# keyword-based approach in ../scraping_api/ (e.g. "flood of demand").
METAPHOR = re.compile(
    r"\bflood(ed|ing)?\s+of\s+(demand|orders|calls|messages|applications|"
    r"immigrants|migrants|money|cash|tears|memories|complaints|requests)\b", re.I)


def score_record(rec):
    """Attach flood_score (0.0-1.0) and flood_verified (bool). Mutates & returns."""
    title = rec.get("title") or ""
    text = rec.get("text") or ""
    caps = " ".join(i.get("caption", "") for i in rec.get("images", []))

    body = f"{text} {caps}"
    strong_t = len(STRONG.findall(title))
    strong_b = len(STRONG.findall(body))
    weak_b = len(WEAK.findall(body))
    metaphor = len(METAPHOR.findall(f"{title} {body}"))

    # Density matters more than raw count: a 4000-word article with one
    # "flood" mention is probably not a flood story.
    words = max(len(body.split()), 1)
    density = strong_b / words * 1000.0          # strong hits per 1000 words

    s = 0.0
    if strong_t:
        s += 0.40                                 # flood term in the headline
    s += min(strong_b, 10) / 10.0 * 0.30          # body prevalence
    s += min(density / 5.0, 1.0) * 0.20           # body density
    s += min(weak_b, 10) / 10.0 * 0.10            # supporting context
    s -= min(metaphor * 0.25, 0.50)               # metaphorical usage penalty

    s = max(0.0, min(1.0, s))
    rec["flood_score"] = round(s, 3)

    # The `strong_b >= 2` floor exists to stop a long article with one passing
    # "flood" mention from verifying. It assumes a full article body -- which
    # feed-only outlets (NYT, Washington Post) do not have: their text is a
    # ~150-character summary, so a genuine flood story like "Nepal's Flood
    # Relief Workers Feel the Pain of Trump's Cuts" scored 0.63 and still
    # failed to verify. Scale the requirement to the body actually available:
    # in a short body, a flood headline plus one strong body term is as much
    # evidence as the text can carry.
    short_body = words < 60
    enough = strong_b >= 2 or (short_body and strong_b >= 1 and strong_t)
    rec["flood_verified"] = bool(s >= 0.35 and enough)
    return rec


def load_records(path):
    """Records from either on-disk shape.

    The crawl's intermediate is JSONL (one object per line); the file it
    finalizes to is a pretty-printed JSON array, and the JSONL is deleted on
    completion. Accepting only JSONL meant this CLI could not audit any file
    that normally survives a run.
    """
    import json

    with open(path) as handle:
        text = handle.read().strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: verify_text.py <data/outlets/<outlet>_flood.json | records.jsonl>\n"
            "Re-scores every record and reports the lowest-scoring titles."
        )
    path = sys.argv[1]
    n = ok = 0
    lo = []
    for rec in load_records(path):
        r = score_record(rec)
        n += 1
        ok += r["flood_verified"]
        if not r["flood_verified"]:
            lo.append((r["flood_score"], (r.get("title") or "")[:70]))
    print(f"{path}: {n} records, {ok} verified ({ok/max(n,1)*100:.1f}%)")
    for s, t in sorted(lo)[:10]:
        print(f"  low {s:.2f}  {t}")

#!/usr/bin/env python3
"""Experiment: split ONE YouTube video into shots with several detectors, side by side.

Step 1 of the "flood scenes" plan (scraping/video_scraping/CLAUDE.md): before
anything runs on the scraped data, compare shot-detection methods on a single
example by eye. This script:

  1. downloads the video ONCE (youtube_download.downloaded -- a temporary file,
     deleted when the script ends, owner's rule 2026-10-14);
  2. runs each method on that same file:
       transnetv2  TransNetV2, a neural shot-boundary model trained on hard
                   cuts AND gradual transitions (fades, dissolves, wipes);
                   PyTorch port `transnetv2-pytorch` with the original
                   weights, run on CPU (it sees 48x27 frames; the shared GPUs
                   are left alone). Frames it labels as "transition" are left
                   out of both neighbouring shots, so its shots can have gaps.
       autoshot    AutoShot (CVPRW 2023), same model family as TransNetV2,
                   trained on short-form video; authors' threshold 0.296.
       union       (derived, no model run) transnetv2 + autoshot combined:
                   split wherever either splits, keep every real shot, drop
                   only 0 s shots (methods.combine)
     (PySceneDetect -- all five of its detectors -- and OmniShotCut were tried
     and dropped by the owner; see scraping/video_scraping/CLAUDE.md,
     "Experiments")
  3. grabs the middle frame of every shot as a small JPEG keyframe;
  4. writes, under scrape_data/experiments/scene_split/<video_id>/ (gitignored):
       results.json   every detector's shots, settings and run time
       index.html     side-by-side comparison: a timeline per detector and a
                      card per shot (keyframe + time range) that PLAYS exactly
                      that range from YouTube (embed ?start=&end=)
       keyframes/     the JPEGs the page shows

The video file itself is never kept. The keyframes are small stills for
looking at; delete the folder when done.

    python3 experiments/scene_split/compare_shot_detectors.py https://www.youtube.com/watch?v=xtBb2eU9d2k
    python3 -m http.server 8790 --directory scrape_data/experiments/scene_split/xtBb2eU9d2k
    # then open http://127.0.0.1:8790/  (served over http so the YouTube player gets a referrer)
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "scraping" / "video_scraping"))
from youtube_download import downloaded  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import methods  # noqa: E402  -- the methods under test (methods.METHODS)

OUT_ROOT = PROJECT / "scrape_data" / "experiments" / "scene_split"
KEYFRAME_WIDTH = 320


def run_detector(path: Path, name: str, fps: float, duration_s: float) -> tuple[methods.Shots, float]:
    t0 = time.monotonic()
    shots = methods.run(name, path, fps, duration_s)
    return shots, time.monotonic() - t0


def save_keyframes(path: Path, shots: list[tuple[float, float]], out_dir: Path, prefix: str) -> list[str]:
    """The middle frame of each shot, KEYFRAME_WIDTH wide, as JPEG."""
    import cv2
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(path))
    names = []
    for i, (start, end) in enumerate(shots, 1):
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + end) / 2 * 1000)
        ok, frame = cap.read()
        name = f"{prefix}_{i:03d}.jpg"
        if ok:
            h, w = frame.shape[:2]
            frame = cv2.resize(frame, (KEYFRAME_WIDTH, round(h * KEYFRAME_WIDTH / w)))
            cv2.imwrite(str(out_dir / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
            names.append(name)
        else:
            names.append("")
    cap.release()
    return names


def clock(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:04.1f}"


def write_html(out: Path, result: dict) -> None:
    vid, dur = result["video_id"], result["duration_s"]
    sections = []
    for d in result["detectors"]:
        marks = "".join(f'<span class="cut" style="left:{100 * s / dur:.3f}%"></span>'
                        for s, _ in d["shots"][1:])
        cards = []
        for i, ((s, e), kf) in enumerate(zip(d["shots"], d["keyframes"]), 1):
            src = (f"https://www.youtube-nocookie.com/embed/{vid}"
                   f"?start={math.floor(s)}&end={math.ceil(e)}&autoplay=1&rel=0")
            img = f'<img src="keyframes/{kf}" alt="shot {i}" loading="lazy">' if kf else '<div class="noimg">no frame</div>'
            cards.append(
                f'<figure class="shot" data-src="{html.escape(src)}">{img}'
                f'<figcaption><b>#{i}</b> {clock(s)}–{clock(e)} <span>({e - s:.1f} s)</span></figcaption></figure>')
        sections.append(f"""
<section>
  <h2>{html.escape(d["name"])} <small>{len(d["shots"])} shots · {d["seconds"]:.1f} s to run</small></h2>
  <p class="desc">{html.escape(d["description"])}</p>
  <div class="timeline">{marks}</div>
  <div class="shots">{''.join(cards)}</div>
</section>""")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shot detection: {html.escape(vid)}</title>
<style>
  :root {{ color-scheme: dark; --bg:#07111a; --panel:#0d1b26; --line:#274052; --text:#f3f8fb; --muted:#9cb2c1; --blue:#70c6f1; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui, sans-serif; }}
  header, main {{ max-width:1400px; margin:0 auto; padding:20px 24px; }}
  h1 {{ margin:0 0 4px; font-size:1.5rem; }} .meta {{ color:var(--muted); margin:0; }}
  #player {{ position:sticky; top:0; z-index:2; background:var(--bg); padding:10px 0; display:none; }}
  #player iframe {{ width:min(100%, 720px); aspect-ratio:16/9; border:0; display:block; }}
  section {{ border-top:1px solid var(--line); padding:18px 0; }}
  h2 {{ margin:0; font-size:1.15rem; }} h2 small {{ color:var(--muted); font-weight:400; margin-left:8px; }}
  .desc {{ color:var(--muted); margin:4px 0 10px; font-size:.9rem; }}
  .timeline {{ position:relative; height:14px; background:var(--panel); border:1px solid var(--line); border-radius:4px; margin-bottom:12px; }}
  .cut {{ position:absolute; top:0; bottom:0; width:2px; background:var(--blue); }}
  .shots {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); gap:10px; }}
  .shot {{ margin:0; background:var(--panel); border:1px solid var(--line); border-radius:6px; overflow:hidden; cursor:pointer; }}
  .shot:hover {{ border-color:var(--blue); }}
  .shot img, .noimg {{ width:100%; aspect-ratio:16/9; object-fit:cover; display:block; background:#000; }}
  figcaption {{ padding:5px 8px; font-size:.8rem; font-variant-numeric:tabular-nums; }} figcaption span {{ color:var(--muted); }}
</style></head><body>
<header>
  <h1>{html.escape(result["title"])}</h1>
  <p class="meta">{html.escape(vid)} · {clock(dur)} · {result["width"]}x{result["height"]} @ {result["fps"]} fps ·
     run {html.escape(result["run_at"])} · click a shot to play exactly that range</p>
  <div id="player"><iframe id="frame" title="YouTube player" allow="autoplay; encrypted-media; picture-in-picture; fullscreen"></iframe></div>
</header>
<main>{''.join(sections)}</main>
<script>
  for (const card of document.querySelectorAll(".shot")) {{
    card.addEventListener("click", () => {{
      document.querySelector("#player").style.display = "block";
      document.querySelector("#frame").src = card.dataset.src;
      window.scrollTo({{ top: 0, behavior: "smooth" }});
    }});
  }}
</script>
</body></html>
"""
    (out / "index.html").write_text(page, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="YouTube URL or video id")
    ap.add_argument("--max-duration", type=float, default=300, help="refuse longer videos (seconds)")
    args = ap.parse_args()

    from datetime import datetime, timezone
    with downloaded(args.url, max_duration_s=args.max_duration) as video:
        out = OUT_ROOT / video.video_id
        # A fresh result each run: keyframes of detectors no longer in the
        # list (or of an older run) must not linger next to the new ones.
        import shutil
        shutil.rmtree(out, ignore_errors=True)
        print(f"{video.video_id}: {video.title!r}, {video.duration_s:.0f} s, "
              f"{video.width}x{video.height} @ {video.fps} fps (temporary file)", flush=True)
        result = {
            "video_id": video.video_id, "title": video.title, "duration_s": video.duration_s,
            "width": video.width, "height": video.height, "fps": video.fps,
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "detectors": [],
        }
        shots_by = {}
        for name, (description, _) in [*methods.METHODS.items(),
                                       *((n, (d, None)) for n, (d, _, _) in methods.DERIVED.items())]:
            if name in methods.DERIVED:
                t0 = time.monotonic()
                _, inputs, fn = methods.DERIVED[name]
                shots = fn(*(shots_by[i] for i in inputs), fps=video.fps or 25.0)
                seconds = time.monotonic() - t0
            else:
                shots, seconds = run_detector(video.path, name, video.fps or 25.0, video.duration_s)
            shots_by[name] = shots
            keyframes = save_keyframes(video.path, shots, out / "keyframes", name)
            result["detectors"].append({"name": name, "description": description, "seconds": round(seconds, 2),
                                        "shots": shots, "keyframes": keyframes})
            lengths = sorted(e - s for s, e in shots)
            print(f"  {name:9s} {len(shots):3d} shots  median {lengths[len(lengths) // 2]:5.1f} s  "
                  f"shortest {lengths[0]:4.1f} s  ({seconds:.1f} s)", flush=True)
    # the video file is gone from here on
    (out / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    write_html(out, result)
    print(f"wrote {out}/index.html and results.json; video file deleted")
    print(f"view: python3 -m http.server 8790 --directory {out}   then http://127.0.0.1:8790/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

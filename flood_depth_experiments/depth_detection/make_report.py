#!/usr/bin/env python3
"""One HTML page for the geometry pilot: routes A, B and V next to the VLM, per shot.

Reads geometry_depth.py's stored output for the current config and
object_sizes.py's labels (no GPU) and writes scrape_data/experiments/flood_depth/geometry/report.html,
next to the overlays it links (relative paths, so the folder can be copied
or served as is). No answer is marked right or wrong: the page puts every
method's number and the overlay side by side so people who know flooding
can judge. Local only: the frames are YouTube stills and are never published.

    python3 flood_depth_experiments/depth_detection/make_report.py
    cd scrape_data/experiments/flood_depth/geometry && python3 -m http.server 8770   # then open /report.html
"""
from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from geometry_depth import (FRAMES_PER_SHOT, MAX_REF_SPREAD_CM, OUT_DIR, REF_ENTRIES, REF_TABLE,  # noqa: E402
                            abstain_reason, choose_shots, config_id, depth_answer, object_labels,
                            read_frames, read_vlm, ref_objects, ref_usable, vlm_size)
from scene_filter import SKIP, read_scenes  # noqa: E402

ROUTES = [("A", "water level − dry-ground plane"),
          ("B", "table size − visible height"),
          ("V", "VLM's size − visible height")]
#: full_5cm's shot-level conditions (C1 is per frame, shown in the frame columns)
VLM_SHOT_CONDITIONS = [("C2", "3 frames together"), ("C3", "+ uploader text"),
                       ("C4", "+ typical object heights"), ("C5", "+ detector boxes")]

CSS = """
:root { --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#ddd; --card:#f6f6f4; --ok:#1d7a3a; --skip:#a33; }
@media (prefers-color-scheme: dark) { :root { --bg:#161616; --fg:#eee; --muted:#999; --line:#333; --card:#202020;
  --ok:#5fc37f; --skip:#e07a7a; } }
body { background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:0 auto;
  max-width:1500px; padding:16px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 8px; }
.muted { color:var(--muted); } table { border-collapse:collapse; margin:8px 0; }
td, th { border-bottom:1px solid var(--line); padding:4px 10px; text-align:left; vertical-align:top; }
th { font-weight:600; } .num { text-align:right; font-variant-numeric:tabular-nums; }
.shot { background:var(--card); border-radius:8px; padding:10px 12px; margin:14px 0; overflow-x:auto; }
.shot h3 { font-size:15px; margin:0 0 6px; } .skip { color:var(--skip); font-weight:600; }
.ans { color:var(--ok); font-weight:600; } img { max-width:100%; display:block; margin:4px 0; border-radius:4px; }
.frames td { width:33%; } .frames img { width:100%; max-width:480px; }
.obj td, .obj th { font-size:12.5px; padding:3px 8px; } .obj img { height:64px; width:auto; margin:0; }
details { margin:8px 0; } summary { cursor:pointer; } ul.muted { margin:4px 0 8px; padding-left:20px; }
.legend span { display:inline-block; padding:1px 8px; border-radius:3px; margin-right:6px; color:#000; }
nav label { margin-right:14px; cursor:pointer; }
"""
JS = """
const boxes = document.querySelectorAll('nav input');
for (const box of boxes) box.addEventListener('change', () => {
  const on = {}; boxes.forEach(b => on[b.value] = b.checked);
  document.querySelectorAll('.shot').forEach(s => {
    const hide = (on.A && !s.dataset.a) || (on.B && !s.dataset.b) || (on.V && !s.dataset.v)
      || (on.real && s.dataset.skip);
    s.style.display = hide ? 'none' : '';
  });
});
"""


#: abstain codes -> plain words (frame level and object level)
REASONS = {
    "no_water": "no flood water found",
    "error": "processing error",
    "not_measured": "not measured (older run)",
    "no_dry_ground": "no dry ground to fit",
    "plane_fit_poor": "dry ground is not flat",
    "shore_only": "water touches only ground, no object",
    "no_waterline": "no waterline found",
    "level_not_flat": "waterline heights disagree",
    "contact_below_ground": "waterline below the ground",
    "implausible": "implausible result",
    "no_object_in_water": "no reference object in water",
    "not_labelled": "VLM not asked yet",
    "top_cut_off": "object's top is cut off by the frame",
    "top_hidden": "VLM: object's top is hidden",
    "not_in_table": "VLM: not a table object",
    "size_varies": "table: size varies too much",
    "no_vlm_size": "VLM gave no usable height",
    "visible_taller_than_size": "visible part taller than the object's height",
    "objects_disagree": f"objects disagree by > {MAX_REF_SPREAD_CM:g} cm",
}


def reason(code: str | None) -> str:
    return html.escape(REASONS.get(code or "", code or "?"))


def fmt(v) -> str:
    return "--" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else html.escape(str(v)))


def frame_cell(r: dict | None, route: str) -> str:
    if r is None:
        return '<span class="muted">not run</span>'
    d = depth_answer(r, route)
    why = abstain_reason(r, route)
    ds = sorted(o["depth_cm"] for o in ref_objects(r, route) if o["depth_cm"] is not None) if route != "A" else []
    span = (f"{len(ds)} object{'s' * (len(ds) > 1)}" + (f", {ds[0]:g}–{ds[-1]:g} cm" if len(ds) > 1 else "")) \
        if ds else ""
    if d is None:
        extra = f"<br><span class='muted'>{span}</span>" if why == "objects_disagree" else ""
        return f"<span class='muted'>{reason(why)}</span>{extra}"
    return f"<span class='ans'>{d:g} cm</span>" + (f"<br><span class='muted'>{span}</span>" if span else "")


def objects_table(sid: str, frames: dict) -> str:
    """Every object routes B/V looked at in this shot, with both sizes and depths."""
    labels = object_labels()
    rows = []
    for j in range(FRAMES_PER_SHOT):
        r = frames.get((sid, j))
        if not r or "objects_b" not in r:
            continue
        b = {o["k"]: o for o in ref_objects(r, "B")}
        v = {o["k"]: o for o in ref_objects(r, "V")}
        vid, num = sid.split(":")
        for o in r["objects_b"]:
            k = o["k"]
            lab = labels.get((sid, j, k), {})
            e = REF_ENTRIES.get(lab.get("entry") or "")
            crop = Path("crops") / f"{vid}_{num}_{j}_{k}.jpg"
            img = (f"<img loading='lazy' src='{crop.as_posix()}' alt=''>" if (OUT_DIR / crop).exists() else "")
            ent = (f"{html.escape(e['label'])} <span class='muted'>{e['top_cm']} cm ({e['min_cm']}–{e['max_cm']})"
                   f"{'' if ref_usable(e) else ', not used: size varies'}</span>") if e else \
                ("<span class='muted'>none / can't tell</span>" if lab else "<span class='muted'>not asked</span>")

            def dcell(x):
                return f"<span class='ans'>{x['depth_cm']:g}</span>" if x["depth_cm"] is not None else \
                    f"<span class='muted'>{reason(x['why'])}</span>"
            vsz = fmt(vlm_size(lab)) if vlm_size(lab) is not None or lab.get("vlm_top_cm") is None \
                else "<span class='muted'>implausible</span>"
            rows.append(
                f"<tr><td>{j + 1}</td><td>{k}</td><td>{img}</td><td>{html.escape(o['phrase'])}</td>"
                f"<td>{fmt(lab.get('vlm_object'))}</td><td>{fmt({True: 'yes', False: 'no'}.get(lab.get('top_visible')))}</td>"
                f"<td>{ent}</td><td class='num'>{vsz}</td>"
                f"<td class='num'>{o['visible_cm']:g} <span class='muted'>({o['visible_px']:g} px, {o['dist_m']:g} m)</span></td>"
                f"<td class='num'>{dcell(b[k])}</td><td class='num'>{dcell(v[k])}</td></tr>")
    if not rows:
        return "<p class='muted'>No reference object standing in water in any frame.</p>"
    return (f"<details><summary>Objects in water used by routes B / V ({len(rows)})</summary><table class='obj'><tr><th>frame</th><th>#</th><th>object</th><th>SAM 3 phrase</th>"
            "<th>VLM: what it is</th><th>top visible</th><th>table entry (VLM's pick) → size</th>"
            "<th class='num'>VLM size cm</th><th class='num'>visible cm</th><th class='num'>B depth cm</th>"
            "<th class='num'>V depth cm</th></tr>" + "".join(rows) + "</table></details>")


def main() -> int:
    shots = choose_shots(0, 0)
    scenes = read_scenes()
    vlm = read_vlm()
    frames = read_frames(config_id())
    p = OUT_DIR / "summary.json"
    summ = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    sr = summ.get("routes", {})

    out = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width,initial-scale=1'><title>Geometry depth pilot</title>"
           f"<style>{CSS}</style></head><body>",
           "<h1>Geometry depth pilot</h1>",
           "<p class='muted'>50 random flood shots, 3 frames each; MoGe-2 for 3D, SAM 3 for masks. "
           "No answer is checked against ground truth -- they are shown side by side for people to judge. "
           "Design: docs/water_depth_segmentation.md.</p>"
           "<ul class='muted'><li><b>A</b> -- water level minus the dry-ground plane, where water touches objects.</li>"
           "<li><b>B</b> -- for each object standing in water: its height from the size table (the VLM says which "
           "entry it is) minus the part visible above the water.</li>"
           "<li><b>V</b> -- the same, but the height is the VLM's own estimate, asked without the table.</li>"
           "<li>B and V give a frame one answer, the median of its objects, only if they agree within "
           f"{MAX_REF_SPREAD_CM:g} cm.</li></ul>",
           "<p class='legend'><span style='background:#2870ff'>water</span><span style='background:#28c846'>dry "
           "ground</span><span style='background:#ff961e'>objects</span><span style='background:#ff0000'>A: "
           "contact</span><span style='background:#ffeb00'>A: shore</span><span style='background:#ff00ff'>B/V: "
           "object box, top to waterline, numbered</span></p>",
           f"<h2>Summary</h2><p class='muted'>{fmt(summ.get('shots'))} shots scored, "
           f"{fmt(summ.get('shots_skipped_by_scene'))} left out by the scene filter.</p>"
           "<table><tr><th></th>" + "".join(f"<th class='num'>route {r}</th>" for r, _ in ROUTES) + "</tr>"]
    for key, label in [("frames_answered", "frames answered"), ("shots_with_answer", "shots with an answer"),
                       ("shots_all_3_answered", "shots, all 3 frames answered")]:
        out.append(f"<tr><td>{label}</td>"
                   + "".join(f"<td class='num'>{fmt(sr.get(r, {}).get(key))}</td>" for r, _ in ROUTES) + "</tr>")
    out.append("<tr><td>within-shot spread, median cm (VLM C1 on the same shots)</td>" + "".join(
        f"<td class='num'>{fmt(sr.get(r, {}).get('within_shot_spread_cm', {}).get('geometry_median'))} "
        f"({fmt(sr.get(r, {}).get('within_shot_spread_cm', {}).get('vlm_C1_same_shots_median'))})</td>"
        for r, _ in ROUTES) + "</tr>")
    out.append("<tr><td>abstain reasons (frames)</td>" + "".join(
        "<td>" + "<br>".join(f"{html.escape(k)}: {v}" for k, v in sr.get(r, {}).get("abstain_reasons", {}).items())
        + "</td>" for r, _ in ROUTES) + "</tr></table>")
    diffs = summ.get("route_differences_cm", {})
    if diffs:
        out.append("<p class='muted'>Same frame answered by two routes: " + "; ".join(
            f"{k.replace('_minus_', ' − ')}: {d['n_frames']} frames, median {fmt(d['median'])} cm, "
            f"median |diff| {fmt(d['abs_median'])} cm" for k, d in diffs.items()) + "</p>")

    chk = summ.get("vlm_size_check") or {}
    out.append("<details><summary><b>VLM heights vs the table</b></summary><p class='muted'>For every object where the VLM picked a table "
               "entry, its own size answer (asked separately, without the table) next to the table's size. "
               f"{fmt(chk.get('objects'))} objects; median |VLM − table| {fmt(chk.get('abs_error_median_cm'))} cm; "
               f"VLM inside the table's range: {fmt(chk.get('share_inside_table_range'))}.</p>"
               "<table><tr><th>entry</th><th class='num'>n</th><th class='num'>table cm (range)</th>"
               "<th class='num'>VLM median cm</th><th class='num'>median VLM − table</th>"
               "<th class='num'>median |VLM − table|</th><th class='num'>VLM inside range</th></tr>")
    for eid, d in chk.get("by_entry", {}).items():
        out.append(f"<tr><td>{html.escape(REF_ENTRIES[eid]['label'])}</td><td class='num'>{d['n']}</td>"
                   f"<td class='num'>{d['table_cm']} ({d['range_cm'][0]}–{d['range_cm'][1]})</td>"
                   f"<td class='num'>{d['vlm_median_cm']:g}</td><td class='num'>{d['vlm_minus_table_median_cm']:g}</td>"
                   f"<td class='num'>{d['abs_error_median_cm']:g}</td>"
                   f"<td class='num'>{d['vlm_inside_table_range']} / {d['n']}</td></tr>")
    out.append("</table></details>")

    out.append("<details><summary><b>The size table</b> (reference_sizes.json, "
               f"{len(REF_TABLE['entries'])} entries; used = fixed size, half-range ≤ "
               f"{REF_TABLE['max_half_range_cm']} cm, stands on the flooded ground)</summary>"
               "<table><tr><th>entry</th><th>SAM 3 phrases</th><th class='num'>top cm</th><th class='num'>range</th>"
               "<th>basis</th><th>used</th><th>source</th></tr>")
    for e in REF_TABLE["entries"]:
        out.append(f"<tr><td>{html.escape(e['label'])}</td><td>{html.escape(', '.join(e['phrases']))}</td>"
                   f"<td class='num'>{e['top_cm']}</td><td class='num'>{e['min_cm']}–{e['max_cm']}</td>"
                   f"<td>{e['basis']}</td><td>{'yes' if ref_usable(e) else 'no'}</td>"
                   f"<td class='muted'>{html.escape(e['source'])}</td></tr>")
    out.append("</table></details>")

    out.append("<h2>Shots</h2><nav><label><input type='checkbox' value='A'> only shots route A answers</label>"
               "<label><input type='checkbox' value='B'> only shots route B answers</label>"
               "<label><input type='checkbox' value='V'> only shots route V answers</label>"
               "<label><input type='checkbox' value='real'> hide scene-filtered shots</label></nav>")

    def answered(sid: str, route: str) -> bool:
        return any(depth_answer(frames.get((sid, j)) or {}, route) is not None for j in range(FRAMES_PER_SHOT))

    order = sorted(shots, key=lambda s: (-sum(answered(s["id"], r) for r, _ in ROUTES), s["id"]))
    for shot in order:
        sid = shot["id"]
        vid, num = sid.split(":")
        scene = scenes.get(sid)
        skip = scene in SKIP
        v = vlm.get(sid, {})
        flags = " ".join(f"data-{r.lower()}='{'1' if answered(sid, r) else ''}'" for r, _ in ROUTES)
        out.append(f"<div class='shot' {flags} data-skip='{'1' if skip else ''}'>"
                   f"<h3>{html.escape(sid)} "
                   f"<a href='https://www.youtube.com/watch?v={html.escape(vid)}' target='_blank'>video</a> "
                   + (f"<span class='skip'>scene: {html.escape(scene)} (left out of the summary)</span>" if skip
                      else f"<span class='muted'>scene: {html.escape(scene or 'unlabelled')}</span>")
                   + "</h3>")
        out.append("<table class='frames'><tr><th></th>"
                   + "".join(f"<th>frame {j + 1}</th>" for j in range(FRAMES_PER_SHOT)) + "</tr>")
        for kind, label in (("frames", "original"), ("overlays", "masks")):
            cells = []
            for j in range(FRAMES_PER_SHOT):
                img = Path(kind) / f"{vid}_{num}_{j}.jpg"
                cells.append(f"<td><a href='{img.as_posix()}' target='_blank'><img loading='lazy' "
                             f"src='{img.as_posix()}' alt='{label} {j + 1}'></a></td>"
                             if (OUT_DIR / img).exists() else "<td class='muted'>--</td>")
            out.append(f"<tr><td>{label}</td>{''.join(cells)}</tr>")
        for route, what in ROUTES:
            out.append(f"<tr><td>route {route} <span class='muted'>({what})</span></td>"
                       + "".join(f"<td>{frame_cell(frames.get((sid, j)), route)}</td>"
                                 for j in range(FRAMES_PER_SHOT)) + "</tr>")
        c1 = v.get("C1") or []
        out.append("<tr><td>VLM C1 <span class='muted'>(one frame)</span></td>"
                   + "".join(f"<td>{fmt(c1[j] if j < len(c1) else None)}</td>" for j in range(FRAMES_PER_SHOT))
                   + "</tr>")
        out.append("<tr><td>VLM, whole shot</td><td colspan='3'>" + " &nbsp;·&nbsp; ".join(
            f"<span title='{html.escape(what)}'>{cond} <b>{fmt(v.get(cond))}</b></span>"
            for cond, what in VLM_SHOT_CONDITIONS) + " <span class='muted'>(C2 3 frames, C3 + text, "
            "C4 + typical heights, C5 + boxes)</span></td></tr>")
        out.append("</table>")
        out.append(objects_table(sid, frames))
        out.append("</div>")
    out.append(f"<script>{JS}</script></body></html>")

    path = OUT_DIR / "report.html"
    tmp = path.with_suffix(".html.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

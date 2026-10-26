#!/usr/bin/env python3
"""Build a static copy of the data_view site for hosting (Vercel or any static host).

data_view_server.py answers two data requests from files on this machine;
this script writes those answers out once, next to the pages, so the result
needs no server:

    site_build/
      index.html, videos.html, app.js, videos.js, gis.css   data_view_web/, as-is
      shared/styles.css                                     the news site's stylesheet
      api/images.json                                       MyCoast photos (load_catalog)
      api/videos.json                                       valid flood videos, trimmed
      frames/<video_id>/shot<k>_<j>.jpg                     the scan's stills (--frames)
      vercel.json                                           the server's security headers

It reuses the server's own loaders, so the published copy shows exactly what
`python3 data_view_server.py` shows on the day it is built.

    python3 export_site.py                        # -> site_build/, flood-shot frames inside it
    python3 export_site.py --frames none          # pages + data only (frames fall back to YouTube's cover)
    python3 export_site.py --frames-url https://pub-xxxx.r2.dev/frames
                                                  # frames hosted elsewhere (R2): NOT copied into
                                                  # the build; their list goes to FRAMES_MANIFEST
                                                  # for deploy_site.sh to upload

Frames are synced, not re-copied: an unchanged file is left alone and a frame
no longer wanted is deleted, so a daily run only writes what is new. The
output folder carries a marker file and the script refuses to write into a
folder without one (other than an empty or missing one), so a wrong --out
cannot wipe something else.

At the end it prints the build's file count and size against Vercel's CLI
limits, which are smaller than this site can be: check them before deploying.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import data_view_server as server

PROJECT = Path(__file__).resolve().parent
DEFAULT_OUT = PROJECT / "site_build"
MARKER = ".data_view_export"
#: With --frames-url: the frames the page will ask for, one path per line
#: relative to the scan's frames folder (rclone --files-from). Outside the
#: build so it is never published.
FRAMES_MANIFEST = PROJECT / "scrape_data" / "site_frames.txt"
PAGES = ["index.html", "videos.html", "app.js", "videos.js", "gis.css"]

#: What videos.js reads; everything else in the server's payload is dropped.
VIDEO_KEYS = ["video_id", "title", "published", "duration_s", "location", "description",
              "source_url", "embed_url", "cover_url", "has_frames",
              "n_shots", "n_flood", "flood_s", "flood_share", "depth_label", "shots"]
SHOT_KEYS = ["start", "end", "flood", "answers", "times", "depth"]

#: Vercel's documented CLI deployment limits (docs/limits, checked
#: 2026-10-24; a changelog says CLI limits were lifted, so re-check).
VERCEL_HOBBY_UPLOAD_MB = 100
VERCEL_MAX_FILES = 15_000


def prepare_out(out: Path) -> None:
    """Create `out`, or confirm it is an earlier build (MARKER present)."""
    if out.exists() and any(out.iterdir()) and not (out / MARKER).is_file():
        sys.exit(f"{out} exists, is not empty and has no {MARKER}: refusing to write into it.")
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("written by export_site.py; safe to delete\n", encoding="utf-8")
    # The marker is ours, not the site's; .vercel/ (the CLI's project link) is kept.
    (out / ".vercelignore").write_text(f"{MARKER}\n", encoding="utf-8")


def write_json(path: Path, value) -> None:
    """Atomic, like every data file in the repo: a killed run leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def copy_if_changed(src: Path, dst: Path) -> bool:
    """Copy unless dst already has src's size and mtime; True if it copied."""
    st = src.stat()
    if dst.is_file():
        dt = dst.stat()
        if dt.st_size == st.st_size and int(dt.st_mtime) == int(st.st_mtime):
            return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def build_photos() -> dict:
    catalog = server.load_catalog(server.DEFAULT_RESULTS)
    for item in catalog["items"]:
        item.pop("id", None)            # the retired review-decision id
    catalog.pop("review_id_scheme", None)
    return catalog


def build_videos(shots_dir: Path, depth_dir: Path, videos_file: Path) -> dict:
    videos = server.FloodVideoCatalog(shots_dir, depth_dir, server.load_video_catalog(videos_file))
    catalog = json.loads(videos.payload())
    catalog["items"] = [{k: item[k] for k in VIDEO_KEYS} for item in catalog["items"]]
    for item in catalog["items"]:
        item["shots"] = [{k: shot[k] for k in SHOT_KEYS} for shot in item["shots"]]
    catalog.pop("source_dir", None)     # a path on this machine
    if catalog.get("depth"):
        catalog["depth"].pop("run_dir", None)
    return catalog


def wanted_frames(catalog: dict, mode: str, frames_dir: Path) -> dict[Path, Path]:
    """relative path in the build -> source frame. Marks every shot whose
    frames are not shipped `frames: false` (videos.js shows its times)."""
    wanted: dict[Path, Path] = {}
    for item in catalog["items"]:
        for k, shot in enumerate(item["shots"]):
            ship = item["has_frames"] and mode != "none" and (mode == "all" or shot["flood"])
            if not ship:
                shot["frames"] = False
                continue
            for j in range(len(shot["answers"])):
                rel = Path(item["video_id"]) / f"shot{k + 1:03d}_{j}.jpg"
                if (frames_dir / rel).is_file():
                    wanted[rel] = frames_dir / rel
    return wanted


def sync_frames(wanted: dict[Path, Path], dest: Path) -> tuple[int, int]:
    """(copied, deleted). Frames already in the build are kept as they are."""
    copied = sum(copy_if_changed(src, dest / rel) for rel, src in wanted.items())
    deleted = 0
    if dest.is_dir():
        keep = {dest / rel for rel in wanted}
        for path in dest.rglob("*.jpg"):
            if path not in keep:
                path.unlink()
                deleted += 1
        for folder in sorted((p for p in dest.iterdir() if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
    return copied, deleted


def vercel_config() -> dict:
    """The server's headers, for every path. JSON is revalidated on each visit
    (it changes daily); frames never change once written, so they cache."""
    headers = [{"key": k, "value": v} for k, v in server.SECURITY_HEADERS.items()]
    return {
        "headers": [
            {"source": "/(.*)", "headers": headers},
            {"source": "/api/(.*)", "headers": [{"key": "Cache-Control", "value": "no-cache"}]},
            {"source": "/frames/(.*)",
             "headers": [{"key": "Cache-Control", "value": "public, max-age=604800"}]},
        ],
    }


def build_size(out: Path) -> tuple[int, int]:
    files = [p for p in out.rglob("*") if p.is_file() and p.name not in (MARKER, ".vercelignore")
             and ".vercel" not in p.relative_to(out).parts]
    return len(files), sum(p.stat().st_size for p in files)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="build folder (default %(default)s)")
    ap.add_argument("--frames", choices=["flood", "all", "none"], default="flood",
                    help="which scan frames to ship: flood shots only (default), every shot, or none")
    ap.add_argument("--frames-url", default="",
                    help="frames are hosted at this base URL instead of /frames; --frames still picks which")
    ap.add_argument("--shots", type=Path, default=server.DEFAULT_SHOTS)
    ap.add_argument("--depth", type=Path, default=server.DEFAULT_DEPTH)
    ap.add_argument("--videos", type=Path, default=server.DEFAULT_VIDEOS)
    args = ap.parse_args()
    out = args.out.resolve()
    prepare_out(out)

    for name in PAGES:
        copy_if_changed(server.STATIC_DIR / name, out / name)
    copy_if_changed(server.SHARED_STYLES, out / "shared" / "styles.css")

    photos = build_photos()
    write_json(out / "api" / "images.json", photos)

    videos = build_videos(args.shots, args.depth, args.videos)
    wanted = wanted_frames(videos, args.frames, args.shots / "frames")
    if args.frames_url:
        videos["frames_base"] = args.frames_url.rstrip("/")
        FRAMES_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        tmp = FRAMES_MANIFEST.with_name(FRAMES_MANIFEST.name + ".tmp")
        tmp.write_text("".join(f"{rel.as_posix()}\n" for rel in sorted(wanted)), encoding="utf-8")
        os.replace(tmp, FRAMES_MANIFEST)
        copied, deleted = sync_frames({}, out / "frames")     # none in the build itself
    else:
        copied, deleted = sync_frames(wanted, out / "frames")
    write_json(out / "api" / "videos.json", videos)
    write_json(out / "vercel.json", vercel_config())

    files, size = build_size(out)
    vs = videos["summary"]
    where = f"listed in {FRAMES_MANIFEST} for {args.frames_url}" if args.frames_url else \
        f"{copied:,} copied into the build"
    print(f"photos: {photos['summary']['images']:,}   videos: {vs['videos']:,} "
          f"({vs['with_depth']:,} with depth)   frames: {len(wanted):,} ({where}; {deleted:,} removed from the build)")
    print(f"build: {out}  {files:,} files, {size / 1e6:,.1f} MB")
    if files > VERCEL_MAX_FILES or size > VERCEL_HOBBY_UPLOAD_MB * 1e6:
        print(f"note: above Vercel's documented CLI limits ({VERCEL_MAX_FILES:,} files; "
              f"{VERCEL_HOBBY_UPLOAD_MB} MB upload on Hobby). Deploy with --archive=tgz, "
              f"host the frames elsewhere (--frames none --frames-url ...), or check the current limits.")


if __name__ == "__main__":
    main()

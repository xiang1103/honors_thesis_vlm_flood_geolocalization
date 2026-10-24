#!/usr/bin/env python3
"""Download ONE YouTube video into a temporary file that is deleted afterwards.

Owner's rule (2026-10-14): videos are downloaded only to be processed and are
deleted right after; nothing but the derived timestamps is kept. So the only
interface is a context manager -- the file exists inside the `with` block and
is removed when the block ends, whether it ended normally or by an exception:

    from youtube_download import downloaded

    with downloaded("https://www.youtube.com/watch?v=xtBb2eU9d2k") as video:
        video.path        # /tmp/.../xtBb2eU9d2k.mp4
        video.video_id, video.title, video.duration_s, video.width, video.height, video.fps
    # gone here

Accepts a watch URL, a youtu.be / shorts / embed URL, or a bare 11-char id.

What is fetched, and why:
  * VIDEO ONLY, no audio -- shot detection and the VLM never use sound; it
    also avoids an ffmpeg merge step.
  * <= 720p, H.264 (avc1) mp4 preferred: decodes everywhere (OpenCV, PySceneDetect,
    ffmpeg); falls back to any <= 720p stream if there is no H.264 one.
  * A duration cap, checked from metadata BEFORE downloading ("no long videos").

YouTube's player needs a JavaScript challenge solved (yt-dlp's EJS). The
server's Node (18) is too old for it, so Deno (pip package `deno`, in the
conda env's bin/) is used, passed by absolute path so this works under cron.

Proxy (2026-10-24): YouTube refused every media stream to this server's IP
(HTTP 403, with or without a PO token), so a proxy can be set in
$YTDLP_PROXY or YTDLP_PROXY in .env (the environment wins), any URL yt-dlp's
`proxy` option takes (http://user:pass@host:port, socks5://...). Each process
tries DIRECT first; after the first video refused there (stream 403, or
the bot check -- both are per IP), it uses the proxy for the rest of the run. The whole yt-dlp run goes through it,
not just the download: stream URLs carry the IP that extracted them. A new
process (run_scan.sh restarts one after every block) tries direct again, so
when the block lifts the proxy stops being used. `Video.via` says which route
fetched each file. No proxy set = direct only, as before.

Errors are raised as DownloadError subclasses with a reason, so a caller can
tell "YouTube wants cookies" (bot check on this server's IP) from "video gone".

Downloading is against YouTube's Terms of Service; the owner accepted that
(2026-10-06) on the condition above: transient, never stored, never shipped.

    python3 scraping/video_scraping/youtube_download.py <url-or-id>     # smoke test: download, report, delete
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

#: Default cap for the experiment phase ("no long videos"); callers may pass
#: a different one.
MAX_DURATION_S = 300

#: A 403 on the media stream is usually transient: YouTube is experimenting
#: with tying stream URLs to tokens ("PO token" / SABR), so one set of URLs is
#: sometimes refused while a fresh extraction a few seconds later works (seen
#: 2026-10-14 on xtBb2eU9d2k: 403, then success on the next try). yt-dlp's own
#: retries do not cover it, so the whole extraction is retried here.
STREAM_403_ATTEMPTS = 3
STREAM_403_WAIT_S = 5

#: Where YTDLP_PROXY is read from when it is not in the environment.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

#: Set once direct downloads are refused (403) in this process; from then on
#: the proxy is used. Never reset: a new process tries direct again.
_direct_refused = False

#: Deno next to this interpreter (the conda env's bin/), found without PATH.
DENO = Path(sys.executable).with_name("deno")

#: Best <= 720p video-only H.264 mp4; else best <= 720p video-only of any
#: codec; else the best <= 720p progressive file (video+audio) as a last resort.
FORMAT = ("bv*[height<=720][vcodec^=avc1][ext=mp4]"
          "/bv*[height<=720]"
          "/b[height<=720]")

ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})|^([A-Za-z0-9_-]{11})$")


class DownloadError(RuntimeError):
    pass


class TooLong(DownloadError):
    pass


class NeedsCookies(DownloadError):
    """YouTube's bot check ("Sign in to confirm you're not a bot") for this IP."""


class Unavailable(DownloadError):
    """Private, deleted, age-restricted, region-blocked, members-only ..."""


@dataclass(frozen=True)
class Video:
    video_id: str
    path: Path
    title: str
    duration_s: float
    width: int | None
    height: int | None
    fps: float | None
    format_id: str
    via: str = "direct"         # "direct" or "proxy"


def video_id_of(url_or_id: str) -> str:
    m = ID_RE.search(url_or_id.strip())
    if not m:
        raise ValueError(f"not a YouTube URL or 11-character video id: {url_or_id!r}")
    return m.group(1) or m.group(2)


def proxy_url(env_file: Path = ENV_FILE) -> str | None:
    """$YTDLP_PROXY wins over .env (same rule as the YouTube API key)."""
    if os.environ.get("YTDLP_PROXY"):
        return os.environ["YTDLP_PROXY"]
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() == "YTDLP_PROXY":
                return value.strip().strip("\"'") or None
    return None


def _options(out_dir: Path, proxy: str | None = None) -> dict:
    opts = {
        "format": FORMAT,
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "overwrites": True,
    }
    if proxy:
        opts["proxy"] = proxy
    if DENO.exists():
        opts["js_runtimes"] = {"deno": {"path": str(DENO)}}
    return opts


def _classify(exc: Exception) -> DownloadError:
    text = str(exc)
    if "confirm you" in text and "bot" in text:
        return NeedsCookies(f"YouTube bot check on this server: {text}")
    if any(k in text for k in ("Private video", "Video unavailable", "This video is not available",
                               "removed", "members-only", "age", "not available in your country")):
        return Unavailable(text)
    return DownloadError(text)


def _fetch(url: str, vid: str, tmp: Path, max_duration_s: float | None, proxy: str | None) -> tuple[dict, float]:
    """Extract and download into `tmp`; returns (info, duration)."""
    import yt_dlp

    for attempt in range(1, STREAM_403_ATTEMPTS + 1):
        with yt_dlp.YoutubeDL(_options(tmp, proxy)) as ydl:
            try:
                info = ydl.extract_info(url, download=False)    # metadata only
            except yt_dlp.utils.DownloadError as exc:
                raise _classify(exc) from None
            duration = float(info.get("duration") or 0)
            if max_duration_s is not None and duration > max_duration_s:
                raise TooLong(f"{vid} is {duration:.0f} s, over the {max_duration_s:.0f} s cap")
            try:
                return ydl.process_ie_result(info, download=True), duration
            except yt_dlp.utils.DownloadError as exc:
                if "HTTP Error 403" in str(exc) and attempt < STREAM_403_ATTEMPTS:
                    print(f"  {vid}: stream refused (403), fresh URLs in {STREAM_403_WAIT_S} s "
                          f"(attempt {attempt}/{STREAM_403_ATTEMPTS})", file=sys.stderr, flush=True)
                    for leftover in tmp.iterdir():
                        leftover.unlink()
                    time.sleep(STREAM_403_WAIT_S)
                    continue
                raise _classify(exc) from None
    raise AssertionError("unreachable")


@contextlib.contextmanager
def downloaded(url_or_id: str, max_duration_s: float | None = MAX_DURATION_S) -> Iterator[Video]:
    """Download into a fresh temporary directory; delete it when the block ends."""
    global _direct_refused

    vid = video_id_of(url_or_id)
    url = f"https://www.youtube.com/watch?v={vid}"
    proxy = proxy_url()
    tmp = Path(tempfile.mkdtemp(prefix=f"yt_{vid}_"))
    try:
        via = "proxy" if proxy and _direct_refused else "direct"
        try:
            info, duration = _fetch(url, vid, tmp, max_duration_s, proxy if via == "proxy" else None)
        except DownloadError as exc:
            ip_block = isinstance(exc, NeedsCookies) or "HTTP Error 403" in str(exc)
            if not (via == "direct" and proxy and ip_block):
                raise
            _direct_refused = True
            print(f"  {vid}: direct route blocked ({type(exc).__name__}); using the proxy for the rest of this run",
                  file=sys.stderr, flush=True)
            for leftover in tmp.iterdir():
                leftover.unlink()
            via = "proxy"
            info, duration = _fetch(url, vid, tmp, max_duration_s, proxy)
        files = [p for p in tmp.iterdir() if p.is_file() and not p.name.endswith(".part")]
        if len(files) != 1:
            raise DownloadError(f"expected one file in {tmp}, found {[p.name for p in files]}")
        yield Video(
            video_id=vid,
            path=files[0],
            title=info.get("title") or "",
            duration_s=duration,
            width=info.get("width"),
            height=info.get("height"),
            fps=info.get("fps"),
            format_id=str(info.get("format_id") or ""),
            via=via,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    with downloaded(sys.argv[1]) as video:
        size = video.path.stat().st_size
        print(f"{video.video_id}: {video.title!r}")
        print(f"  {video.duration_s:.0f} s, {video.width}x{video.height} @ {video.fps} fps, "
              f"format {video.format_id}, {size / 1e6:.1f} MB at {video.path}, via {video.via}")
        path = video.path
    print(f"  deleted: {not path.exists() and not path.parent.exists()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

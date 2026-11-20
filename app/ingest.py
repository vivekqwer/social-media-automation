from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

import os
import re
import subprocess
import sys

from .config import COOKIES_PATH, YT_COOKIES_FILE, YT_PROXY, ffmpeg_bin

# yt-dlp's ranged download checks PATH for ffmpeg, ignoring ffmpeg_location
try:
    os.environ["PATH"] = str(Path(ffmpeg_bin()).parent) + os.pathsep + os.environ["PATH"]
except RuntimeError:
    pass
# the JS runtime yt-dlp needs for full-quality YouTube formats may live next to this Python (pip install deno)
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]


def _opts(extra: dict | None = None) -> dict:
    o = {"quiet": True, "no_warnings": True, "ffmpeg_location": ffmpeg_bin(), "allowed_extractors": ["(?i)youtube.*"]}
    if YT_COOKIES_FILE:
        o["cookiefile"] = YT_COOKIES_FILE
    elif COOKIES_PATH.exists():
        o["cookiefile"] = str(COOKIES_PATH)
    if YT_PROXY:
        o["proxy"] = YT_PROXY
    if extra:
        o.update(extra)
    return o


def is_cc(license_str: str | None) -> bool:
    return bool(license_str) and "creative commons" in license_str.lower()


def _fetch_info_ytdlp(url: str) -> dict:
    with yt_dlp.YoutubeDL(_opts({"skip_download": True})) as y:
        info = y.extract_info(url, download=False)
    return {
        "video_id": info["id"],
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
        "license": info.get("license"),
        "description": info.get("description") or "",
        "tags": info.get("tags") or [],
        "duration": int(info.get("duration") or 0),
        "heatmap": info.get("heatmap") or [],
    }


def download_section(video_id: str, start: float, end: float, out_dir: Path) -> Path:
    """Download only [start, end] of the video (no full-video download)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    s, e = int(start), int(end) + 1
    cached = out_dir / f"src_{s}_{e}.mp4"
    if cached.exists():
        return cached
    opts = _opts(
        {
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
            "merge_output_format": "mp4",
            "outtmpl": str(out_dir / f"src_{s}_{e}.%(ext)s"),
            "download_ranges": download_range_func(None, [(s, e)]),
            "force_keyframes_at_cuts": True,
            # YouTube sometimes drops long-lived connections; let ffmpeg resume instead of ending the file early
            "external_downloader_args": {"ffmpeg_i": ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]},
        }
    )
    url = f"https://www.youtube.com/watch?v={video_id}"
    for attempt in (1, 2, 3):
        if attempt < 3:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=True)
            path = Path(info["requested_downloads"][0]["filepath"])
            if path != cached:
                path.replace(cached)
        else:  # last resort: whole video, then cut the section locally (only for videos up to 30 minutes)
            if int(info.get("duration") or 0) > 1800:
                raise RuntimeError("The video section downloaded incompletely. Please try again.")
            _download_whole_and_cut(url, cached, s, e, out_dir)
        # the clip may run past the end of the video, so compare with what can exist
        want = max(1, min(e, int(info.get("duration") or e)) - s)
        if stream_seconds(cached, "v:0") >= want * 0.9:
            break
        cached.unlink(missing_ok=True)  # never render a cut-short clip
        if attempt == 3:
            raise RuntimeError("The video section downloaded incompletely. Please try again.")
    if 0 < stream_seconds(cached, "a:0") < want * 0.9:  # ranged audio downloads sometimes stop early
        _repair_audio(video_id, cached, s, want, out_dir)
    return cached


def _download_whole_and_cut(url: str, cached: Path, s: int, e: int, out_dir: Path):
    opts = _opts({"format": "bv*[height<=1080]+ba/b[height<=1080]/b", "merge_output_format": "mp4",
                  "outtmpl": str(out_dir / "whole.%(ext)s")})
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    whole = Path(info["requested_downloads"][0]["filepath"])
    subprocess.run([ffmpeg_bin(), "-y", "-ss", str(s), "-i", str(whole), "-t", str(e - s), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(cached)], capture_output=True, text=True)
    whole.unlink(missing_ok=True)


def stream_seconds(path: Path, sel: str) -> float:
    """How many seconds of one stream (e.g. 'a:0', 'v:0') are actually in the file. 0 if absent."""
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path), "-map", "0:" + sel, "-c", "copy", "-f", "null", "-"],
                       capture_output=True, text=True)
    t = re.findall(r"time=(\d+):(\d+):([\d.]+)", r.stderr)
    if not t:
        return 0.0
    h, m, sec = t[-1]
    return int(h) * 3600 + int(m) * 60 + float(sec)


def _repair_audio(video_id: str, clip_file: Path, start: int, length: int, out_dir: Path):
    """Fetch the whole audio track (small) and put the right [start, start+length] part back under the video."""
    opts = _opts({"format": "ba[ext=m4a]/ba", "outtmpl": str(out_dir / "audio_full.%(ext)s")})
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    audio = Path(info["requested_downloads"][0]["filepath"])
    fixed = out_dir / "fixed.mp4"
    r = subprocess.run([ffmpeg_bin(), "-y", "-i", str(clip_file), "-ss", str(start), "-t", str(length), "-i", str(audio),
                        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", str(fixed)],
                       capture_output=True, text=True)
    audio.unlink(missing_ok=True)
    if r.returncode == 0:
        fixed.replace(clip_file)


def video_id_from_url(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def fetch_info(url: str) -> dict:
    """Video details. Uses the official API when a channel is connected (reliable from servers), else yt-dlp."""
    vid = video_id_from_url(url)
    if vid:
        try:
            from . import youtube

            info = youtube.api_video_info(vid)
            if info:
                return info
        except Exception:  # noqa: BLE001  (no channel connected, quota, API disabled...) -> fall back
            pass
    return _fetch_info_ytdlp(url)


def is_bot_block(err) -> bool:
    m = str(err)
    return "Sign in to confirm" in m or "not a bot" in m


def friendly_error(msg) -> str:
    m = str(msg)
    if "Sign in to confirm" in m or "not a bot" in m:
        return ("YouTube blocked this server (bot check). Start the Helper PC (My channels → Helper PC) "
                "and press Re-render — your PC fetches the video. Or save cookies under “YouTube access for the server”.")
    if "Private video" in m or "Video unavailable" in m or "members-only" in m.lower():
        return "This video is private, removed or members-only, so it can't be used."
    if "Requested format is not available" in m:
        return "YouTube did not offer a downloadable format for this video right now. Try again later."
    return m[-600:]

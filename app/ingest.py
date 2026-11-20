from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

import os
import re

from .config import COOKIES_PATH, YT_COOKIES_FILE, YT_PROXY, ffmpeg_bin

# yt-dlp's ranged download checks PATH for ffmpeg, ignoring ffmpeg_location
try:
    os.environ["PATH"] = str(Path(ffmpeg_bin()).parent) + os.pathsep + os.environ["PATH"]
except RuntimeError:
    pass


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
        }
    )
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    path = Path(info["requested_downloads"][0]["filepath"])
    if path != cached:
        path.replace(cached)
    return cached


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


def friendly_error(msg) -> str:
    m = str(msg)
    if "Sign in to confirm" in m or "not a bot" in m:
        return ("YouTube blocked this server (bot check) while downloading. Open My channels → "
                "“YouTube access for the server”, save cookies from a spare Google account, then press Re-render.")
    if "Private video" in m or "Video unavailable" in m or "members-only" in m.lower():
        return "This video is private, removed or members-only, so it can't be used."
    if "Requested format is not available" in m:
        return "YouTube did not offer a downloadable format for this video right now. Try again later."
    return m[-600:]

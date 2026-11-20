from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

import os

from .config import YT_COOKIES_FILE, YT_PROXY, ffmpeg_bin

# yt-dlp's ranged download checks PATH for ffmpeg, ignoring ffmpeg_location
try:
    os.environ["PATH"] = str(Path(ffmpeg_bin()).parent) + os.pathsep + os.environ["PATH"]
except RuntimeError:
    pass


def _opts(extra: dict | None = None) -> dict:
    o = {"quiet": True, "no_warnings": True, "ffmpeg_location": ffmpeg_bin()}
    if YT_COOKIES_FILE:
        o["cookiefile"] = YT_COOKIES_FILE
    if YT_PROXY:
        o["proxy"] = YT_PROXY
    if extra:
        o.update(extra)
    return o


def is_cc(license_str: str | None) -> bool:
    return bool(license_str) and "creative commons" in license_str.lower()


def fetch_info(url: str) -> dict:
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

"""Captions from YouTube's own subtitle tracks (manual or auto-generated). No AI service, no extra cost."""
import glob
import json
import re
import tempfile
from pathlib import Path

import yt_dlp

from .ingest import _opts

PREFERRED = ["hi-orig", "hi", "en-orig", "en"]
MAX_CHARS = 38  # per caption line chunk (fits a 9:16 screen)
NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|[♪♫♬]+")


def _pick(info: dict, want: str) -> str:
    avail = set(info.get("subtitles") or {}) | set(info.get("automatic_captions") or {})
    if want and want != "auto":
        for k in (want + "-orig", want):
            if k in avail:
                return k
        raise ValueError(f"No '{want}' captions on this video")
    cands = []
    lang = info.get("language")
    if lang:
        cands += [lang + "-orig", lang]
    cands += PREFERRED
    for k in cands:
        if k in avail:
            return k
    raise ValueError("This video has no captions available")


def _chunks(text: str, s: float, e: float) -> list[dict]:
    """Split long text into short chunks, giving each a share of the time proportional to its length."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > MAX_CHARS:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    total = sum(len(x) for x in lines) or 1
    out, t = [], s
    for ln in lines:
        d = (e - s) * len(ln) / total
        out.append({"s": round(t, 2), "e": round(t + d, 2), "t": ln})
        t += d
    return out


def parse_json3(data: dict, start: float, end: float) -> list[dict]:
    cues: list[dict] = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        text = NOISE.sub("", "".join(sg.get("utf8", "") for sg in segs)).replace("\n", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        t0 = ev["tStartMs"] / 1000
        t1 = t0 + (ev.get("dDurationMs") or 0) / 1000
        if t1 <= start or t0 >= end:
            continue
        s, e = max(t0, start) - start, min(t1, end) - start
        if e - s < 0.2:
            continue
        if cues and cues[-1]["t"] == text:  # rolling auto-captions repeat lines
            cues[-1]["e"] = round(max(cues[-1]["e"], e), 2)
            continue
        if cues and cues[-1]["e"] > s:  # never overlap the previous cue
            cues[-1]["e"] = round(s, 2)
        cues.extend(_chunks(text, s, e))
    cues.sort(key=lambda c: c["s"])
    for a, b in zip(cues, cues[1:]):  # one caption on screen at a time
        if a["e"] > b["s"]:
            a["e"] = round(b["s"], 2)
    return [c for c in cues if c["e"] - c["s"] >= 0.2][:400]


def fetch_cues(video_id: str, start: float, end: float, lang: str = "auto") -> list[dict]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(_opts({"skip_download": True})) as y:
        info = y.extract_info(url, download=False)
    key = _pick(info, lang)
    tmp = tempfile.mkdtemp()
    opts = _opts({
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [key],
        "subtitlesformat": "json3",
        "outtmpl": str(Path(tmp) / "subs.%(ext)s"),
    })
    with yt_dlp.YoutubeDL(opts) as y:
        y.extract_info(url, download=True)
    files = glob.glob(str(Path(tmp) / "subs*.json3"))
    if not files:
        raise ValueError("Captions could not be downloaded (YouTube may be rate-limiting this server)")
    with open(files[0], encoding="utf-8") as f:
        cues = parse_json3(json.load(f), start, end)
    if not cues:
        raise ValueError("No speech captions inside this clip's time range")
    return cues

"""Hot-moment detection: 'most replayed' heatmap + timestamps people mention in comments."""
import re

import httpx
import yt_dlp

from .config import YT_API_KEY
from .ingest import _opts

TS_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")


def parse_timestamps(texts: list[str], duration: int) -> list[int]:
    out = []
    for t in texts:
        for h, m, s in TS_RE.findall(t or ""):
            sec = int(h or 0) * 3600 + int(m) * 60 + int(s)
            if 0 <= sec < duration:
                out.append(sec)
    return out


def _comments_api(video_id: str, limit: int) -> list[str]:
    texts, token = [], None
    while len(texts) < limit:
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": 100,
            "order": "relevance",
            "textFormat": "plainText",
            "key": YT_API_KEY,
        }
        if token:
            params["pageToken"] = token
        r = httpx.get("https://www.googleapis.com/youtube/v3/commentThreads", params=params, timeout=15)
        if r.status_code != 200:
            break
        data = r.json()
        texts += [i["snippet"]["topLevelComment"]["snippet"]["textDisplay"] for i in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return texts


def _comments_ytdlp(video_id: str, limit: int) -> list[str]:
    opts = _opts(
        {
            "skip_download": True,
            "getcomments": True,
            "extractor_args": {"youtube": {"max_comments": [str(limit), str(limit), "0", "0"], "comment_sort": ["top"]}},
        }
    )
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    return [c.get("text", "") for c in (info.get("comments") or [])]


def fetch_comments(video_id: str, limit: int = 300) -> list[str]:
    try:
        return _comments_api(video_id, limit) if YT_API_KEY else _comments_ytdlp(video_id, limit)
    except Exception:
        return []


def rank_moments(duration: int, heatmap: list[dict], comment_secs: list[int], clip_len: int = 30, top_n: int = 8):
    if duration <= 0:
        return []
    clip_len = min(clip_len, duration)
    heat = [0.0] * duration
    for seg in heatmap or []:
        a, b, v = int(seg["start_time"]), int(seg["end_time"]), float(seg["value"])
        for i in range(max(a, 0), min(b + 1, duration)):
            heat[i] = v
    com = [0.0] * duration
    for s in comment_secs:  # spread each mention over +-4s
        for i in range(max(s - 4, 0), min(s + 5, duration)):
            com[i] += 1.0

    def prefix(arr):
        p = [0.0]
        for x in arr:
            p.append(p[-1] + x)
        return p

    ph, pc = prefix(heat), prefix(com)
    step = 5
    wins = []
    for s in range(0, max(duration - clip_len, 0) + 1, step):
        e = s + clip_len
        h = (ph[e] - ph[s]) / clip_len
        c = pc[e] - pc[s]
        wins.append((s, e, h, c))
    if not wins:
        return []
    max_h = max(w[2] for w in wins) or 1.0
    max_c = max(w[3] for w in wins) or 1.0
    wh, wc = (0.6, 0.4) if heatmap and comment_secs else ((1.0, 0.0) if heatmap else (0.0, 1.0))
    scored = [
        {"start": s, "end": e, "score": round(wh * h / max_h + wc * c / max_c, 3), "heat": round(h, 3), "comments": int(c)}
        for s, e, h, c in wins
    ]
    scored.sort(key=lambda m: m["score"], reverse=True)
    picked = []
    for m in scored:
        if m["score"] <= 0:
            break
        if all(m["end"] <= p["start"] or m["start"] >= p["end"] for p in picked):
            picked.append(m)
        if len(picked) >= top_n:
            break
    return picked

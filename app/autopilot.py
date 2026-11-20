"""Autopilot: channel name/URL in -> latest videos -> hot moments -> clips -> render -> metadata -> scheduled."""
from datetime import timedelta
from urllib.parse import quote

import yt_dlp
from sqlalchemy import func

from .db import Channel, Clip, Project, SessionLocal, utcnow
from .hot import fetch_comments, parse_timestamps, rank_moments
from .ingest import _opts, fetch_info, is_cc
from .metadata import auto_metadata
from .render import run_render


def resolve_channel(query: str) -> tuple[str, str]:
    """Return (channel_base_url, display_name) from a name, @handle or URL."""
    q = query.strip()
    if q.startswith("http"):
        base = q.split("?")[0].rstrip("/")
        for suffix in ("/videos", "/featured", "/shorts", "/streams"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        return base, base.rsplit("/", 1)[-1]
    if q.startswith("@"):
        return f"https://www.youtube.com/{q}", q
    url = f"https://www.youtube.com/results?search_query={quote(q)}&sp=EgIQAg%3D%3D"  # channels-only filter
    with yt_dlp.YoutubeDL(_opts({"extract_flat": True, "playlistend": 1})) as y:
        info = y.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise ValueError(f"No channel found for '{q}'")
    e = entries[0]
    return e.get("url") or e.get("channel_url"), e.get("title") or e.get("channel") or q


def list_video_ids(channel_url: str, n: int) -> list[str]:
    with yt_dlp.YoutubeDL(_opts({"extract_flat": True, "playlistend": n})) as y:
        info = y.extract_info(channel_url.rstrip("/") + "/videos", download=False)
    return [e["id"] for e in (info.get("entries") or []) if e and e.get("id")]


def _next_slot(s, gap_hours: int):
    last = s.query(func.max(Clip.publish_at)).scalar()
    base = max(utcnow(), last) if last else utcnow()
    return base + timedelta(hours=gap_hours)


def run_channel(channel_id: int):
    with SessionLocal() as s:
        ch = s.get(Channel, channel_id)
        log = []
        try:
            if not ch.resolved_url:
                ch.resolved_url, ch.name = resolve_channel(ch.query)
                s.commit()
            seen = set(ch.seen or [])
            ids = [i for i in list_video_ids(ch.resolved_url, ch.videos_per_run * 4) if i not in seen]
            made = 0
            for vid in ids:
                if made >= ch.videos_per_run:
                    break
                seen.add(vid)
                info = fetch_info(f"https://www.youtube.com/watch?v={vid}")
                if ch.mode == "cc" and not is_cc(info["license"]):
                    log.append(f"skip {vid}: not CC")
                    continue
                p = Project(url=f"https://www.youtube.com/watch?v={vid}", mode=ch.mode,
                            permission_note=ch.permission_note, channel_ref=ch.id, **info)
                secs = parse_timestamps(fetch_comments(vid), p.duration)
                p.moments = rank_moments(p.duration, p.heatmap, secs, ch.clip_len, ch.clips_per_video)
                s.add(p)
                s.commit()
                if not p.moments:
                    log.append(f"skip {vid}: no hot moments found")
                    continue
                made += 1
                for m in p.moments:
                    c = Clip(project_id=p.id, start=m["start"], end=m["end"], aspect=ch.aspect, effects=["fade"],
                             destination_id=ch.destination_id)
                    s.add(c)
                    s.commit()
                    run_render(c.id)
                    s.refresh(c)
                    if c.status != "rendered":
                        log.append(f"clip {c.id} failed: {c.error}")
                        continue
                    auto_metadata(c.id)
                    s.refresh(c)
                    c.publish_at, c.status = _next_slot(s, ch.gap_hours), "queued"
                    s.commit()
                    log.append(f"queued clip {c.id} for {c.publish_at}")
            ch.seen = list(seen)
            ch.last_status = "; ".join(log) or "no new videos"
        except Exception as e:  # noqa: BLE001
            ch.last_status = f"error: {str(e)[:300]}"
        ch.last_run = utcnow()
        s.commit()


def autopilot_tick():
    with SessionLocal() as s:
        ids = [c.id for c in s.query(Channel).filter(Channel.auto.is_(True)).all()]
    for i in ids:
        run_channel(i)

"""Autopilot: channel name/URL in -> latest videos -> hot moments -> clips -> render -> metadata -> scheduled."""
import os
import re
from datetime import timedelta
from urllib.parse import quote

import yt_dlp
from sqlalchemy import func

from .db import Channel, Clip, Destination, Project, SessionLocal, utcnow
from . import quota, youtube
from . import workers
from .captions import fetch_cues, parse_json3
from .hot import fetch_comments, fetch_heatmap, parse_timestamps, rank_moments
from .ingest import _opts, fetch_info, is_cc
from .metadata import auto_metadata
from .render import run_render

MAX_QUEUED_PER_DEST = 30  # don't pile up an unbounded backlog
# clips that already own a publish time (incl. ones still waiting for the helper PC or rendering)
HOLDS_SLOT = ["fetching", "rendering", "draft", "rendered", "queued", "uploading", "uploaded"]


def _resolve_ytdlp(query: str) -> tuple[str, str]:
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


def _list_ytdlp(channel_url: str, n: int) -> list[str]:
    with yt_dlp.YoutubeDL(_opts({"extract_flat": True, "playlistend": n})) as y:
        info = y.extract_info(channel_url.rstrip("/") + "/videos", download=False)
    return [e["id"] for e in (info.get("entries") or []) if e and e.get("id")]


def _slot_from_times(after, post_times: str, tz_offset: int):
    """First daily slot (user's local clock) strictly after `after` (naive UTC). None if no valid times."""
    times = sorted({tuple(int(x) for x in t.strip().split(":")) for t in (post_times or "").split(",")
                    if re.match(r"^\d{1,2}:\d{2}$", t.strip()) and int(t.strip().split(":")[0]) < 24 and int(t.strip().split(":")[1]) < 60})
    if not times:
        return None
    local_after = after - timedelta(minutes=tz_offset or 0)
    for d in range(0, 90):
        day = (local_after + timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        for h, m in times:
            cand = day + timedelta(hours=h, minutes=m)
            if cand > local_after:
                return cand + timedelta(minutes=tz_offset or 0)
    return None


def resolve_channel(query: str) -> tuple[str, str]:
    try:
        return youtube.api_resolve_channel(query)  # official API: not blocked on servers
    except Exception:  # noqa: BLE001
        return _resolve_ytdlp(query)


def list_video_ids(channel_url: str, n: int) -> list[str]:
    try:
        return youtube.api_list_video_ids(channel_url, n)
    except Exception:  # noqa: BLE001
        return _list_ytdlp(channel_url, n)


def _next_slot(s, gap_hours: int, dest_id, post_times: str | None = None, tz_offset: int = 0):
    """Next free publish slot for this destination (only counts clips still holding a future slot)."""
    now = utcnow()
    last = (
        s.query(func.max(Clip.publish_at))
        .filter(
            Clip.destination_id == dest_id,
            Clip.status.in_(HOLDS_SLOT),
            Clip.publish_at.isnot(None),
            Clip.publish_at > now,
        )
        .scalar()
    )
    base = max(last, now) if last else now
    slot = _slot_from_times(base, post_times, tz_offset) if post_times else None
    slot = slot or (base + timedelta(hours=gap_hours))
    # all channels share one daily upload budget: move to the next day while that day is full
    cap = quota.capacity()
    for _ in range(60):
        day = quota.pt_day_of(slot)
        lo, hi = slot - timedelta(hours=36), slot + timedelta(hours=36)
        load = sum(1 for (t,) in s.query(Clip.publish_at).filter(
            Clip.status.in_(HOLDS_SLOT), Clip.publish_at.between(lo, hi)) if quota.pt_day_of(t) == day)
        if load < cap:
            break
        nxt = slot + timedelta(days=1)
        slot = (_slot_from_times(nxt.replace(hour=0, minute=0) - timedelta(minutes=1), post_times, tz_offset) if post_times else None) or nxt
    return slot


def _process_video(s, ch: Channel, vid: str, log: list) -> bool:
    """Returns True if the video yielded clips."""
    info = fetch_info(f"https://www.youtube.com/watch?v={vid}")
    if (ch.mode == "cc" or ch.kind == "topic") and not is_cc(info["license"]):
        log.append(f"skip {vid}: not CC")
        return False
    p = Project(url=f"https://www.youtube.com/watch?v={vid}", mode=ch.mode,
                permission_note=ch.permission_note, channel_ref=ch.id, **info)
    if not p.heatmap:
        p.heatmap = fetch_heatmap(vid)
    secs = parse_timestamps(fetch_comments(vid), p.duration)
    p.moments = rank_moments(p.duration, p.heatmap, secs, ch.clip_len, ch.clips_per_video)
    s.add(p)
    s.commit()
    if not p.moments and workers.any_recent():
        # the server can't see "most replayed" / comments (YouTube blocks it): the helper PC reads them
        p.analysis_pending, p.analysis_since, p.analysis_tries = True, utcnow(), 0
        s.commit()
        log.append(f"{vid}: helper PC is finding the best moment")
        return True
    _make_clips(s, ch, p, log)
    return True


def _fallback_moments(p: Project, length: int, n: int) -> list:
    """No replay/comment signal at all: evenly spread slices from the middle part of the video."""
    dur = float(p.duration or 0)
    if dur <= length:
        return [{"start": 0.0, "end": dur or float(length)}] if dur > 5 else []
    marks = [0.3, 0.5, 0.7, 0.4, 0.6][:max(1, n)]
    return [{"start": round(dur * f, 1), "end": round(min(dur, dur * f + length), 1)} for f in marks]


def make_clips_for_project(pid: int):
    """Continue an autopilot video after the helper PC's analysis (or after giving up on it)."""
    with SessionLocal() as s:
        p = s.get(Project, pid)
        ch = s.get(Channel, p.channel_ref) if p and p.channel_ref else None
        if not ch or s.query(Clip).filter(Clip.project_id == pid).count():
            return
        log = []
        _make_clips(s, ch, p, log)
        ch.last_status = ((ch.last_status or "") + "; " + "; ".join(log))[-1000:]
        s.commit()


def _make_clips(s, ch: Channel, p: Project, log: list):
    moments = p.moments or _fallback_moments(p, ch.clip_len, ch.clips_per_video)
    if not moments:
        log.append(f"skip {p.video_id}: video too short")
        return
    if not p.moments:
        log.append(f"{p.video_id}: no replay data, using the middle of the video")
    for m in moments[:ch.clips_per_video]:
        c = Clip(project_id=p.id, start=m["start"], end=m["end"], aspect=ch.aspect, effects=["fade"],
                 destination_id=ch.destination_id)
        s.add(c)
        s.commit()
        if ch.captions:
            try:
                c.captions = parse_json3(p.caption_track, c.start, c.end) if p.caption_track else \
                    fetch_cues(p.video_id, c.start, c.end)
                if c.captions:
                    c.caption_style = {"on": True, "lang": "auto", "pos": "bottom", "size": "M", "color": "#ffffff"}
                s.commit()
            except Exception as e:  # noqa: BLE001
                log.append(f"clip {c.id}: no captions ({str(e)[:70]})")
        run_render(c.id)
        s.refresh(c)
        if c.status == "fetching":  # helper PC will fetch the video; render finishes later
            auto_metadata(c.id)
            s.refresh(c)
            if not ch.review_first:
                c.publish_at = _next_slot(s, ch.gap_hours, ch.destination_id, ch.post_times, ch.tz_offset or 0)
                c.autoqueue = True  # becomes 'queued' as soon as the render completes
                s.commit()
            log.append(f"clip {c.id}: waiting for helper PC")
            continue
        if c.status != "rendered":
            log.append(f"clip {c.id} failed: {c.error}")
            continue
        auto_metadata(c.id)
        s.refresh(c)
        if ch.review_first:
            log.append(f"clip {c.id} ready for review")
            continue
        c.publish_at, c.status, c.autoqueue = _next_slot(s, ch.gap_hours, ch.destination_id, ch.post_times, ch.tz_offset or 0), "queued", True
        s.commit()
        log.append(f"queued clip {c.id} for {c.publish_at}")


def run_channel(channel_id: int):
    with SessionLocal() as s:
        ch = s.get(Channel, channel_id)
        if not ch:
            return
        log = []
        try:
            dest = s.get(Destination, ch.destination_id) if ch.destination_id else None
            if dest and dest.paused:
                ch.last_status = f"waiting: uploads to {dest.name} are paused (press Resume on the dashboard)"
                ch.last_run = utcnow()
                s.commit()
                return
            topic = ch.kind == "topic"
            if not ch.resolved_url:
                if topic:
                    ch.resolved_url, ch.name = "topic", f"Topic: {ch.query}"
                else:
                    ch.resolved_url, ch.name = resolve_channel(ch.query)
                s.commit()
            backlog = s.query(Clip).filter(Clip.destination_id == ch.destination_id,
                                           Clip.status.in_(["queued", "fetching", "rendering"])).count()
            if backlog >= MAX_QUEUED_PER_DEST:
                ch.last_status = f"paused: {backlog} clips already queued"
                ch.last_run = utcnow()
                s.commit()
                return
            seen = set(ch.seen or [])
            if topic:  # Creative Commons videos only, found by YouTube search
                ids = youtube.api_search_cc(ch.query, seen, ch.videos_per_run * 3)
                if not ids:
                    log.append("no new Creative Commons videos found for this topic" if quota.can_search()
                               else "search skipped: today's YouTube quota is kept for uploads")
            else:
                ids = [i for i in list_video_ids(ch.resolved_url, ch.videos_per_run * 4) if i not in seen]
            made = 0
            for vid in ids:
                if made >= ch.videos_per_run:
                    break
                seen.add(vid)  # mark first, so a failing video is never retried forever or duplicated
                ch.seen = list(seen)
                s.commit()
                try:
                    if _process_video(s, ch, vid, log):
                        made += 1
                except Exception as e:  # noqa: BLE001
                    s.rollback()
                    log.append(f"{vid}: {str(e)[:150]}")
            ch.last_status = "; ".join(log) or "no new videos"
        except Exception as e:  # noqa: BLE001
            s.rollback()
            ch = s.get(Channel, channel_id)
            ch.last_status = f"error: {str(e)[:300]}"
        ch.last_run = utcnow()
        s.commit()


INTERVAL = timedelta(hours=int(os.getenv("AUTOPILOT_INTERVAL_HOURS") or "6"))


def autopilot_tick():
    """Run every source whose interval has passed since its last run (called every 15 minutes)."""
    due = utcnow() - INTERVAL
    with SessionLocal() as s:
        ids = [c.id for c in s.query(Channel).filter(Channel.auto.is_(True)).all() if not c.last_run or c.last_run < due]
    for i in ids:
        try:
            run_channel(i)
        except Exception:  # noqa: BLE001
            continue


def resume_after_analysis(pid: int):
    """The helper PC finished (or gave up) reading a video: continue the Quick upload or the autopilot source."""
    with SessionLocal() as s:
        p = s.get(Project, pid)
        quick = bool(p and p.quick)
    if quick:
        from .quick import quick_run

        quick_run(pid)
    else:
        make_clips_for_project(pid)

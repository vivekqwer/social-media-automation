"""Quick upload: video link in -> best moment (or whole video) -> captions -> render -> thumbnail -> scheduled upload."""
from datetime import datetime, timezone

from . import workers
from .captions import fetch_cues, parse_json3
from .db import Clip, Destination, Project, SessionLocal, utcnow, main_destination
from .hot import fetch_comments, fetch_heatmap, parse_timestamps, rank_moments
from .metadata import auto_metadata
from .render import run_render

CLIP_LEN = {"shorts": 35, "video": 60}
CAPTION_STYLE = {"on": True, "lang": "auto", "pos": "bottom", "size": "M", "color": "#ffffff"}


def _set(s, p: Project, **kw):
    q = dict(p.quick or {})
    q.update(kw)
    p.quick = q
    s.commit()


def _utc(iso: str | None):
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def quick_run(pid: int):
    with SessionLocal() as s:
        p = s.get(Project, pid)
        if not p or not p.quick or p.quick.get("clip_id"):
            return
        q = dict(p.quick)
        try:
            dest = (s.get(Destination, q["destination_id"]) if q.get("destination_id") else None) \
                or main_destination(s)
            if not dest:
                raise ValueError("Connect your YouTube channel first (My channels).")
            fmt = q.get("format", "shorts")
            if q.get("length") == "whole" and fmt == "video":
                start, end = 0.0, float(p.duration)
            else:
                want = min(CLIP_LEN[fmt], p.duration or CLIP_LEN[fmt])
                if not p.moments:
                    _set(s, p, state="analyzing", step="Finding the best moment")
                    if not p.heatmap:
                        p.heatmap = fetch_heatmap(p.video_id)
                    secs = parse_timestamps(fetch_comments(p.video_id), p.duration)
                    p.moments = rank_moments(p.duration, p.heatmap, secs, want, 3)
                    s.commit()
                    if not p.moments and workers.any_recent("analyze") and not q.get("helper_analysed"):
                        p.analysis_pending = True  # the helper PC reads replays/comments/captions from home internet
                        p.analysis_since, p.analysis_tries, p.analysis_lease = utcnow(), 0, None
                        _set(s, p, state="analyzing", step="Your helper PC is reading the video")
                        return
                if p.moments:
                    ranges = [(float(m["start"]), float(m["end"])) for m in p.moments]
                else:  # no signal at all: slices from the middle part of the video
                    dur = float(p.duration or want)
                    ranges = [(max(0.0, dur * f), min(dur, dur * f + want)) for f in (0.3, 0.5, 0.7)]
            count = max(1, min(3, int(q.get("count") or 1)))
            if q.get("length") == "whole" and fmt == "video":
                ranges = [(start, end)]
            ranges = _distinct(ranges)[:count]
            first_at = _utc(q.get("publish_at"))
            ids = []
            from . import acl

            # "Automatic upload without review" switched off for this account: the clip waits in Clips for approval
            auto = acl.auto_upload()
            for i, (start, end) in enumerate(ranges):
                c = Clip(project_id=p.id, start=start, end=end, aspect="9:16" if fmt == "shorts" else "16:9",
                         effects=["fade"], overlays=[], destination_id=dest.id, autoqueue=auto,
                         publish_at=first_at if i == 0 else _later_slot(s, dest.id, first_at))
                s.add(c)
                s.commit()
                if q.get("captions", True) and acl.feature("clips.captions"):
                    try:
                        cues = parse_json3(p.caption_track, start, end) if p.caption_track else fetch_cues(p.video_id, start, end)
                        if cues:
                            c.captions, c.caption_style = cues, dict(CAPTION_STYLE)
                            s.commit()
                    except Exception:  # noqa: BLE001  (no captions on this video) -> upload without
                        pass
                ids.append(c.id)
            _set(s, p, clip_id=ids[0], clip_ids=ids, state="rendering", step="Cutting and rendering")
        except Exception as e:  # noqa: BLE001
            _set(s, p, state="failed", step=str(e)[:300])
            return
    for cid in ids:
        auto_metadata(cid)  # credit + original tags/description + keywords, so it can queue itself after render
        run_render(cid)


def _distinct(ranges: list) -> list:
    """Moments that don't overlap each other (so 3 clips are 3 different parts of the video)."""
    out = []
    for a, b in ranges:
        if all(b <= x or a >= y for x, y in out):
            out.append((a, b))
    return out


def _later_slot(s, dest_id: int, after):
    """The next free daily slot after the first clip (respects the daily upload budget)."""
    from datetime import timedelta

    from .autopilot import _next_slot

    slot = _next_slot(s, 12, dest_id)
    if after and slot <= after:
        slot = after + timedelta(hours=12)
    return slot

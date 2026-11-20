import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from google.auth.exceptions import RefreshError
from sqlalchemy import func, update

from . import quota, youtube
from .autopilot import autopilot_tick
from .config import MAX_UPLOADS_PER_DAY, UPLOAD_LEAD_HOURS
from .crypto import TokenError
from .db import Clip, Destination, Project, SessionLocal, utcnow
from .jobs import executor
from .monitor import check_uploads
from .policy import check_edit

PT = ZoneInfo("America/Los_Angeles")  # YouTube API quota resets at midnight Pacific
_quota_blocked_until: datetime | None = None


def _pacific_midnight(now: datetime, offset_days: int = 0) -> datetime:
    pt = now.replace(tzinfo=timezone.utc).astimezone(PT)
    day = pt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)
    return day.astimezone(timezone.utc).replace(tzinfo=None)


def recover():
    """After a crash/redeploy: don't leave clips stuck in transient states."""
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.status.in_(["uploading", "rendering"])).all():
            if c.status == "uploading":
                c.status = "uploaded" if c.youtube_id else "queued"
            else:
                c.status, c.error = "failed", "Interrupted by restart; render again."
        s.commit()


def process_queue():
    """Upload queued clips whose publish time is within the lead window (YouTube holds them private until publishAt)."""
    global _quota_blocked_until
    now = utcnow()
    if _quota_blocked_until and now < _quota_blocked_until:
        return
    with SessionLocal() as s:
        since = _pacific_midnight(now)
        used = s.query(Clip).filter(Clip.attempted_at >= since).count()
        paused = {d.id for d in s.query(Destination).filter(Destination.paused.is_(True))}
        connected = s.query(Destination).count() > 0
        rows = s.query(Clip.id, Clip.destination_id, Clip.publish_at).filter(Clip.status == "queued").all()
        per_dest = {}
        for did, n in s.query(Clip.destination_id, func.count()).filter(Clip.attempted_at >= since).group_by(Clip.destination_id):
            per_dest[did] = n
    if not connected:
        return  # keep everything queued until a channel is connected
    # fair share: the channel with the fewest uploads today goes first, then the earliest publish time
    rows = [r for r in rows if r.destination_id not in paused]
    rows.sort(key=lambda r: (per_dest.get(r.destination_id, 0), r.publish_at or now))
    ids = [r.id for r in rows]

    for cid in ids:
        if used >= MAX_UPLOADS_PER_DAY or not quota.can_upload():
            break
        with SessionLocal() as s:
            c = s.get(Clip, cid)
            if not c or c.status != "queued" or c.destination_id in paused:
                continue
            if c.publish_at and c.publish_at > now + timedelta(hours=UPLOAD_LEAD_HOURS):
                continue
            p = s.get(Project, c.project_id)
            errs = check_edit(p, c)
            if errs:
                c.status, c.error = "failed", "; ".join(errs)
                s.commit()
                continue
            # atomic claim: only one worker can flip queued -> uploading
            res = s.execute(update(Clip).where(Clip.id == cid, Clip.status == "queued").values(status="uploading"))
            s.commit()
            if res.rowcount != 1:
                continue
            s.refresh(c)
            try:
                publish_at = c.publish_at if c.publish_at and c.publish_at > now + timedelta(minutes=15) else None
                c.youtube_id = youtube.upload(c.destination_id, c.output_path, c.title, c.description, c.tags or [], publish_at)
                c.status, c.error = "uploaded", None
                if c.thumb_path and Path(c.thumb_path).exists():
                    youtube.push_thumbnail(c)  # a refusal is retried by retry_thumbnails()
            except (RefreshError, TokenError):
                c.status, c.error = "queued", "Reconnect your YouTube channel (login expired, revoked or unreadable)."
                s.commit()
                paused.add(c.destination_id)  # other channels keep uploading
                continue
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "quotaExceeded" in msg or "uploadLimitExceeded" in msg:
                    c.status, c.error = "queued", "YouTube quota/upload limit reached; will retry after reset."
                    _quota_blocked_until = _pacific_midnight(now, 1)
                    quota.exhaust()
                    s.commit()
                    break
                c.status, c.error = "failed", msg[:500]
            c.attempted_at = utcnow()
            used += 1
            s.commit()


def retry_thumbnails():
    """Thumbnails YouTube refused (e.g. phone not verified yet): try again, per channel, until they go through."""
    with SessionLocal() as s:
        since = utcnow() - timedelta(days=30)
        rows = s.query(Clip).filter(Clip.thumb_pending.is_(True), Clip.youtube_id.isnot(None),
                                    Clip.status == "uploaded", Clip.attempted_at >= since).order_by(Clip.id).all()
        tried = set()
        for c in rows:
            if not c.thumb_path or not Path(c.thumb_path).exists():
                c.thumb_pending = False
                continue
            if c.destination_id in tried and c.thumb_pending:
                continue
            if quota.DAILY_UNITS - quota.used() < quota.UPLOAD_COST + quota.COST["thumbnail"]:
                break  # keep the budget for uploads
            ok = youtube.push_thumbnail(c)
            if not ok:
                tried.add(c.destination_id)  # one refusal per channel per round is enough
        s.commit()


def start():
    recover()
    sch = BackgroundScheduler(timezone="UTC")
    sch.add_job(process_queue, "interval", minutes=1, max_instances=1, coalesce=True)
    hours = int(os.getenv("AUTOPILOT_INTERVAL_HOURS") or "6")
    sch.add_job(lambda: executor.submit(autopilot_tick), "interval", hours=hours, max_instances=1, coalesce=True)
    sch.add_job(lambda: executor.submit(retry_thumbnails), "interval", minutes=30, max_instances=1, coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))
    # watch uploaded videos for deletions / copyright rejections / blocks (1 quota unit per 50 videos)
    every = int(os.getenv("MONITOR_INTERVAL_HOURS") or "2")
    sch.add_job(lambda: executor.submit(check_uploads), "interval", hours=every, max_instances=1, coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3))
    sch.start()
    return sch

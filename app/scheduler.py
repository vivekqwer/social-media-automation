import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from google.auth.exceptions import RefreshError
from sqlalchemy import update

from . import youtube
from .autopilot import autopilot_tick
from .config import MAX_UPLOADS_PER_DAY, UPLOAD_LEAD_HOURS
from .crypto import TokenError
from .db import Clip, Destination, Project, SessionLocal, utcnow
from .jobs import executor
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
        used = s.query(Clip).filter(Clip.attempted_at >= _pacific_midnight(now)).count()
        ids = [i for (i,) in s.query(Clip.id).filter(Clip.status == "queued").order_by(Clip.publish_at)]
        connected = s.query(Destination).count() > 0
    if not connected:
        return  # keep everything queued until a channel is connected

    for cid in ids:
        if used >= MAX_UPLOADS_PER_DAY:
            break
        with SessionLocal() as s:
            c = s.get(Clip, cid)
            if not c or c.status != "queued":
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
            except (RefreshError, TokenError):
                c.status, c.error = "queued", "Reconnect your YouTube channel (login expired, revoked or unreadable)."
                s.commit()
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "quotaExceeded" in msg or "uploadLimitExceeded" in msg:
                    c.status, c.error = "queued", "YouTube quota/upload limit reached; will retry after reset."
                    _quota_blocked_until = _pacific_midnight(now, 1)
                    s.commit()
                    break
                c.status, c.error = "failed", msg[:500]
            c.attempted_at = utcnow()
            used += 1
            s.commit()


def start():
    recover()
    sch = BackgroundScheduler(timezone="UTC")
    sch.add_job(process_queue, "interval", minutes=1, max_instances=1, coalesce=True)
    hours = int(os.getenv("AUTOPILOT_INTERVAL_HOURS") or "6")
    sch.add_job(lambda: executor.submit(autopilot_tick), "interval", hours=hours, max_instances=1, coalesce=True)
    sch.start()
    return sch

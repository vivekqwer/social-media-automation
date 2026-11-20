import os
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import youtube
from .autopilot import autopilot_tick
from .config import MAX_UPLOADS_PER_DAY, UPLOAD_LEAD_HOURS
from .db import Clip, Project, SessionLocal, utcnow
from .jobs import executor
from .policy import check_edit


def process_queue():
    """Upload queued clips whose publish time is within the lead window (YouTube holds them private until publishAt)."""
    now = utcnow()
    with SessionLocal() as s:
        done_today = s.query(Clip).filter(Clip.status == "uploaded", Clip.created_at >= now - timedelta(days=1)).count()
        due = (
            s.query(Clip)
            .filter(Clip.status == "queued")
            .order_by(Clip.publish_at)
            .all()
        )
        for c in due:
            if done_today >= MAX_UPLOADS_PER_DAY:
                break
            if c.publish_at and c.publish_at > now + timedelta(hours=UPLOAD_LEAD_HOURS):
                continue
            p = s.get(Project, c.project_id)
            errs = check_edit(p, c)
            if errs:
                c.status, c.error = "failed", "; ".join(errs)
                s.commit()
                continue
            c.status = "uploading"
            s.commit()
            try:
                publish_at = c.publish_at if c.publish_at and c.publish_at > now + timedelta(minutes=15) else None
                c.youtube_id = youtube.upload(c.destination_id, c.output_path, c.title, c.description, c.tags or [], publish_at)
                c.status, c.error = "uploaded", None
                done_today += 1
            except Exception as e:  # noqa: BLE001
                c.status, c.error = "failed", str(e)[:500]
            s.commit()


def start():
    sch = BackgroundScheduler(timezone="UTC")
    sch.add_job(process_queue, "interval", minutes=1, max_instances=1, coalesce=True)
    hours = int(os.getenv("AUTOPILOT_INTERVAL_HOURS", "6"))
    sch.add_job(lambda: executor.submit(autopilot_tick), "interval", hours=hours, max_instances=1, coalesce=True)
    sch.start()
    return sch

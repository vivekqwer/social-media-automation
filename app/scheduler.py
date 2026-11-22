import logging
import time
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

log = logging.getLogger(__name__)
from .monitor import check_uploads
from .policy import check_edit

PT = ZoneInfo("America/Los_Angeles")  # YouTube API quota resets at midnight Pacific
_quota_blocked: dict[int, datetime] = {}  # customer -> blocked until


def _pacific_midnight(now: datetime, offset_days: int = 0) -> datetime:
    pt = now.replace(tzinfo=timezone.utc).astimezone(PT)
    day = pt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)
    return day.astimezone(timezone.utc).replace(tzinfo=None)


def recover():
    """After a crash/redeploy (Railway restarts on every deploy): pick up interrupted work by itself."""
    rerender, requick = [], []
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.status.in_(["uploading", "rendering"])).all():
            if c.status == "uploading":
                c.status = "uploaded" if c.youtube_id else "queued"
            else:
                c.status, c.error = "draft", None
                rerender.append(c.id)  # render it again automatically
        for c in s.query(Clip).filter(Clip.status == "draft", Clip.autoqueue.is_(True)).all():
            if c.id not in rerender:
                rerender.append(c.id)  # Quick/autopilot clip that never got rendered
        for p in s.query(Project).filter(Project.quick.isnot(None)).all():
            q = p.quick or {}
            if q.get("state") in ("starting", "analyzing", "rendering") and not q.get("clip_id") and not p.analysis_pending:
                requick.append(p.id)
        s.commit()
    from .quick import quick_run
    from .render import run_render
    for cid in rerender:
        executor.submit(run_render, cid)
    for pid in requick:
        executor.submit(quick_run, pid)


def _tg(text: str, buttons=None):
    try:
        from .telegram import notify

        notify(text, buttons)
    except Exception:  # noqa: BLE001  (Telegram must never break uploads)
        pass


def _e(t) -> str:
    return str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def check_helper():
    """Tell the owner on Telegram when jobs wait and no helper PC is online (at most every 2 hours)."""
    try:
        from .main import _helper_state
        from .telegram import notify_once

        with SessionLocal() as s:
            hp = _helper_state(s)
        if hp["waiting"] and not hp["online"]:
            notify_once("helper", f"⚠️ Helper PC is offline and {hp['waiting']} job(s) are waiting. Turn the PC on.", 120)
    except Exception:  # noqa: BLE001
        pass


def sweep_stale():
    """Jobs waiting for a helper PC that never comes: stop waiting, with a clear message."""
    now = utcnow()
    resume = []
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.status == "fetching", Clip.fetch_since.isnot(None),
                                      Clip.fetch_since < now - timedelta(hours=24)).all():
            c.status, c.job_lease = "failed", None
            c.error = ("No helper PC fetched this video for 24 hours. Start the helper on your PC "
                       "(or run worker\\install_autostart.bat once), then press Re-download video.")
        for c in s.query(Clip).filter(Clip.status == "fetching", Clip.fetch_since.is_(None)).all():
            c.fetch_since = now  # clips from before this field existed
        for c in s.query(Clip).filter(Clip.tts_pending.isnot(None), Clip.tts_pending != "",
                                      Clip.created_at < now - timedelta(hours=6), Clip.job_lease.is_(None)).all():
            c.tts_pending = None
            c.error = "The helper PC did not make the voice-over in time. Start the helper and press Make voice again."
        for p in s.query(Project).filter(Project.analysis_pending.is_(True)).all():
            if (p.analysis_since or p.created_at) < now - timedelta(hours=6):
                p.analysis_pending = False  # continue without the helper's analysis
                if p.quick:  # only Quick uploads keep this flag; autopilot videos have no quick state
                    p.quick = {**p.quick, "helper_analysed": True}
                resume.append(p.id)
        s.commit()
    if resume:
        from .autopilot import resume_after_analysis
        for pid in resume:
            executor.submit(resume_after_analysis, pid)


def cleanup_disk():
    """Keep the /data volume from filling up: downloaded sources of old uploads, leftovers, orphaned folders."""
    import shutil

    from .config import DATA_DIR

    root, now = DATA_DIR / "clips", utcnow()
    if not root.exists():
        return
    with SessionLocal() as s:
        clips = {c.id: c for c in s.query(Clip).all()}
    for d in root.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        c = clips.get(int(d.name))
        age_h = (time.time() - d.stat().st_mtime) / 3600  # real clock: a naive UTC datetime's .timestamp() reads as local time
        if c is None:
            if age_h > 2:  # clip discarded: remove its folder
                shutil.rmtree(d, ignore_errors=True)
            continue
        src = d / "src"
        mine = src / "upload.mp4"  # my own uploaded file: the only copy, so it stays far longer than a download
        if c.status == "uploaded" and c.attempted_at and c.attempted_at < now - timedelta(days=3):
            if mine.exists() and c.attempted_at > now - timedelta(days=30):
                continue
            shutil.rmtree(src, ignore_errors=True)  # the video is on YouTube; keep out.mp4 + thumbnail only
            (d / "body.mp4").unlink(missing_ok=True)
            continue
        if (c.status == "queued" and c.output_path and Path(c.output_path).exists() and c.thumb_path
                and Path(c.thumb_path).exists() and age_h > 1 and not mine.exists() and not (src / "reel.mp4").exists()):
            # ready and waiting for its upload: only the finished video and thumbnail are needed. The downloaded part
            # can be fetched again by itself if the clip is ever edited; this keeps a big queue inside the server disk
            for f in (src.iterdir() if src.exists() else []):
                f.unlink(missing_ok=True)
            if (d / "out.mp4").exists():
                (d / "body.mp4").unlink(missing_ok=True)
            continue
        keep = f"src_{int(c.start or 0)}_{int(c.end or 0) + 1}.mp4"
        if src.exists():
            for f in src.iterdir():
                stale = (time.time() - f.stat().st_mtime) / 3600 > 24
                if f.name not in (keep, "upload.mp4", "reel.mp4") and (stale or not f.name.endswith(".part")):
                    if f.name.endswith(".part") and not stale:
                        continue
                    f.unlink(missing_ok=True)  # an old trim, a partial download or a whole-video leftover


def process_queue():
    """Upload queued clips whose publish time is within the lead window (YouTube holds them private until publishAt)."""
    from . import acl, tenant as _t

    now, tid = utcnow(), _t.current()
    if acl.upload_block():  # paused by the administrator, or uploading is switched off for this account:
        return  # everything stays queued, quietly (the customer's dashboard says why)
    day_cap = acl.uploads_cap(MAX_UPLOADS_PER_DAY)  # the account's own uploads-a-day limit, if it has one
    _quota_blocked_until = _quota_blocked.get(tid)
    if _quota_blocked_until and now < _quota_blocked_until:
        return
    with SessionLocal() as s:
        from .db import adopt_orphans

        adopt_orphans(s)  # a clip whose channel was disconnected goes to the main channel instead of failing
        since = _pacific_midnight(now)
        used = s.query(Clip).filter(Clip.attempted_at >= since).count()
        paused = {d.id for d in s.query(Destination).filter(Destination.paused.is_(True))}
        paused |= acl.over_cap_channels(s)  # beyond the plan's channel limit: kept, but its uploads wait
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
        if used >= day_cap or not quota.can_upload():
            break
        if acl.upload_block():  # paused while this round was running: stop before the next video
            break
        now = utcnow()  # uploads take minutes: judge every clip's time against the real clock
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
                c.status, c.error, c.attempted_at = "uploaded", None, utcnow()
                s.commit()  # saved at once: a crash now must never upload the video twice
                acl.record_here("uploads.youtube", f"Uploaded to YouTube: {(c.title or '')[:120]} (#{c.id})")
                _tg(f"⬆️ Uploaded: <b>{_e(c.title)}</b>\nhttps://youtu.be/{c.youtube_id}"
                    + (f"\nGoes live {c.publish_at:%d %b %H:%M} UTC" if publish_at else "\nLive now"))
                if c.thumb_path and Path(c.thumb_path).exists():
                    youtube.push_thumbnail(c)  # a refusal is retried by retry_thumbnails()
                try:  # Instagram + Facebook (optional module; never blocks YouTube)
                    from .meta import crosspost, status as meta_status

                    if meta_status()["connected"]:
                        executor.submit(crosspost, c.id)
                except Exception:  # noqa: BLE001
                    pass
            except (RefreshError, TokenError):
                c.status, c.error = "queued", "Reconnect your YouTube channel (login expired, revoked or unreadable)."
                d = s.get(Destination, c.destination_id)
                if d and not d.paused:  # stop retrying (and spending quota) until the owner reconnects
                    d.paused, d.paused_at = True, utcnow()
                    d.paused_reason = "The channel's Google login expired. Press Connect a YouTube channel to log in again, then Resume."
                s.commit()
                paused.add(c.destination_id)  # other channels keep uploading
                continue
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "quotaExceeded" in msg or "uploadLimitExceeded" in msg:
                    c.status, c.error = "queued", "YouTube quota/upload limit reached; will retry after reset."
                    _quota_blocked[tid] = _quota_blocked_until = _pacific_midnight(now, 1)
                    quota.exhaust()
                    s.commit()
                    break
                c.status, c.error = "failed", msg[:500]
                _tg(f"❌ Upload failed: <b>{_e(c.title)}</b>\n{_e(msg[:300])}")
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


def for_each_customer(fn):
    """A timed job that runs once for every private copy (the owner's and each active customer's), as that customer.
    One customer's failure never stops the others."""
    def run():
        from . import tenant
        from .accounts import active_tenants

        try:
            ids = active_tenants()
        except Exception:  # noqa: BLE001  (accounts table unreachable: still serve the owner)
            ids = [tenant.OWNER]
        for tid in ids:
            try:
                with tenant.use(tid):
                    fn()
            except Exception as e:  # noqa: BLE001
                log.exception("timed job %s failed for customer %s", getattr(fn, "__name__", "job"), tid)
                try:
                    from .guide import record_error

                    with tenant.use(tid):
                        record_error("timed job: " + getattr(fn, "__name__", "job"), e)
                except Exception:  # noqa: BLE001
                    pass
    run.__name__ = getattr(fn, "__name__", "job")
    return run


def when_on(key: str, fn):
    """A timed job that belongs to something the Super Admin can switch off per account (see app/acl.py):
    for an account that has it off the job is skipped quietly — no error, nothing queued."""
    def run():
        from . import acl

        if acl.feature(key):
            return fn()
        return None
    run.__name__ = getattr(fn, "__name__", "job")
    return run


def start():
    for_each_customer(recover)()
    sch = BackgroundScheduler(timezone="UTC")

    def add(fn, *args, **kw):
        sch.add_job(for_each_customer(fn), *args, **kw)

    # Every timed job, and what decides whether it runs for a customer (the owner's own copy always runs everything):
    #   process_queue        uploads paused / "Upload to YouTube at all" / uploads-a-day limit  -> inside process_queue
    #   autopilot_tick       sources stopped / each source type / clips-a-day / storage / AI reels -> autopilot.run_channel
    #   poll (Telegram)      "Telegram"
    #   swap_weak_thumbnails "Thumbnails"
    #   weekly_report        "Analytics page"
    #   check_replies        "Permission mails to creators"
    #   refresh_inbox        "Permission inbox"
    #   viral_refresh        "Viral today page" or "Viral picks" (viral.refresh; picks only with "Viral picks")
    #   scan_if_due          "Competitor tracker"
    #   daily_summary        Telegram (telegram.enabled)
    #   learn_daily          auto-tune switch of the account; sources only when "Change sources" is on -> learn.daily
    #   always: sweep_stale, check_helper, cleanup_disk, retry_thumbnails, check_uploads, sync_all, sync_all_watch_time
    add(process_queue, "interval", minutes=1, max_instances=1, coalesce=True)
    # check often; each source runs only when its own interval has passed (deploys can't reset or pause this)
    add(lambda: executor.submit(autopilot_tick), "interval", minutes=15, max_instances=1, coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5))
    add(sweep_stale, "interval", minutes=15, max_instances=1, coalesce=True)
    # Telegram remote control (standalone: does nothing until it is set up)
    from .telegram import daily_summary, poll
    add(when_on("telegram.use", poll), "interval", seconds=20, max_instances=1, coalesce=True)
    add(check_helper, "interval", minutes=30, max_instances=1, coalesce=True)
    from .growth import swap_weak_thumbnails, weekly_report
    add(when_on("clips.thumbnail", lambda: executor.submit(swap_weak_thumbnails)), "cron", hour=6, minute=0, max_instances=1,
                coalesce=True)
    add(when_on("page.analytics", lambda: executor.submit(weekly_report)), "cron", day_of_week="sun", hour=15, minute=35,
                max_instances=1, coalesce=True)  # Sunday 9:05 PM IST
    from .permissions import check_replies, refresh_inbox
    add(when_on("gmail.send", lambda: executor.submit(check_replies)), "interval", minutes=15, max_instances=1, coalesce=True)
    add(when_on("permissions.manage", lambda: executor.submit(refresh_inbox)), "cron", hour=4, minute=30, max_instances=1,
                coalesce=True)  # 10 AM IST
    from .viral import refresh as viral_refresh
    add(lambda: executor.submit(viral_refresh), "interval", hours=2, max_instances=1, coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=4))  # viral board + 5 daily picks
    from .competitors import scan_if_due
    add(when_on("ideas.competitors", lambda: executor.submit(scan_if_due)), "interval", hours=1, max_instances=1, coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=8))
    add(daily_summary, "cron", hour=15, minute=30, max_instances=1, coalesce=True)  # 9:00 PM IST
    add(cleanup_disk, "interval", hours=6, max_instances=1, coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10))
    add(lambda: executor.submit(retry_thumbnails), "interval", minutes=30, max_instances=1, coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))
    # watch uploaded videos for deletions / copyright rejections / blocks (1 quota unit per 50 videos)
    every = int(os.getenv("MONITOR_INTERVAL_HOURS") or "2")
    add(lambda: executor.submit(check_uploads), "interval", hours=every, max_instances=1, coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3))
    # live channel numbers (subscribers, views, every video) for the Analytics page
    from .stats import sync_all, sync_all_watch_time
    add(sync_all_watch_time, "interval", hours=3, max_instances=1, coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))
    add(sync_all, "interval", minutes=int(os.getenv("STATS_INTERVAL_MINUTES") or "10"), max_instances=1,
                coalesce=True, next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))
    from .metadata import freshen_due; add(freshen_due, "interval", minutes=20, max_instances=1, coalesce=True)  # noqa: E702  fresh keywords before upload
    from .learn import daily as learn_daily  # Ideas -> What works: auto-tune once a day (checked hourly, acts once)
    add(learn_daily, "interval", hours=1, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=12))
    from .payments import expire_due

    def plans_ending():  # over all accounts at once, so it runs a single time, in the owner's copy
        from . import tenant

        with tenant.use(tenant.OWNER):
            expire_due()
    sch.add_job(plans_ending, "cron", hour=3, minute=30, max_instances=1, coalesce=True)  # 9 AM IST: warn 3 days before, end paid plans
    from .phone import events as phone_events
    add(phone_events, "interval", minutes=10, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3))  # live / 10 clips / views / subscribers / needs you
    sch.start()
    return sch

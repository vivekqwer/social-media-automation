import logging
import threading
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
                if c.youtube_id:
                    from .comments import after_upload

                    after_upload(c)  # its first comment, as if the upload had finished normally
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
    from .comments import recover_stuck
    recover_stuck()
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
        from . import watchdog

        if watchdog.enabled("helper"):
            return  # the watchdog sends one clear message per offline time instead of one every 2 hours
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
    try:  # the watchdog's rule: a job keeps waiting while a helper that can do it was seen in the last 2 days
        from . import watchdog, workers

        keep = watchdog.enabled("helper")
    except Exception:  # noqa: BLE001
        keep = False
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.status == "fetching", Clip.fetch_since.isnot(None),
                                      Clip.fetch_since < now - timedelta(hours=24)).all():
            if keep:
                p = s.get(Project, c.project_id)
                if workers.any_recent("reel" if p is not None and p.mode == "original" else "source"):
                    continue  # the PC was on lately (an internet outage, a night off): the clip waits for it
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
    from . import archive

    try:
        from .meta import status as meta_status

        st = meta_status()
        meta_on = bool(st["connected"] and st["on"])
    except Exception:  # noqa: BLE001  (unknown: treat a cross-post as possible, so the file is kept)
        meta_on = True
    with SessionLocal() as s:
        try:  # an ask to get a video back that no helper answered in 3 days ends, with a plain note
            archive.expire_restores(s, now)
            s.commit()
        except Exception as e:  # noqa: BLE001
            s.rollback()
            log.warning("archive: ending old get-back asks: %s", str(e)[:200])
        clips = {c.id: c for c in s.query(Clip).all()}
        for d in root.iterdir():
            if not d.is_dir() or not d.name.isdigit():
                continue
            c = clips.get(int(d.name))
            if c is None or c.status != "uploaded":
                continue
            try:  # on YouTube and saved on the PC (sha256 confirmed): the server's copy goes, the information stays
                archive.free_uploaded(s, c, d, meta_on, now)
                s.commit()
            except Exception as e:  # noqa: BLE001  (one clip's problem never stops the clean-up of the others)
                s.rollback()
                log.warning("archive clean-up of clip %s: %s", c.id, str(e)[:200])
    for d in root.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        for part in d.glob("pc.*.part"):  # a PC video that broke off while it was being sent (asked again later)
            try:
                if time.time() - part.stat().st_mtime > 2 * 3600:
                    part.unlink(missing_ok=True)
            except OSError:
                pass
        c = clips.get(int(d.name))
        age_h = (time.time() - d.stat().st_mtime) / 3600  # real clock: a naive UTC datetime's .timestamp() reads as local time
        if c is None:
            if age_h > 2:  # clip discarded: remove its folder
                shutil.rmtree(d, ignore_errors=True)
            continue
        src = d / "src"
        mine = src / "upload.mp4"  # my own uploaded file: the only copy, so it stays until the PC has saved it
        if c.status == "uploaded":
            if c.attempted_at and c.attempted_at < now - timedelta(days=3) and src.exists():
                for f in src.iterdir():  # a downloaded section can be fetched again; my own file cannot
                    if f.is_file() and f.name != "upload.mp4":
                        f.unlink(missing_ok=True)
                (d / "body.mp4").unlink(missing_ok=True)  # out.mp4 has the same video
            continue  # out.mp4 stays until the PC has a confirmed copy (archive.free_uploaded above)
        if (c.status == "queued" and c.output_path and Path(c.output_path).exists() and c.thumb_path
                and Path(c.thumb_path).exists() and age_h > 1 and not mine.exists() and not (src / "reel.mp4").exists()):
            # ready and waiting for its upload: only the finished video and thumbnail are needed. The downloaded part
            # can be fetched again by itself if the clip is ever edited; this keeps a big queue inside the server disk
            for f in (src.iterdir() if src.exists() else []):
                if f.is_file():
                    f.unlink(missing_ok=True)
            if (d / "out.mp4").exists():
                (d / "body.mp4").unlink(missing_ok=True)
            continue
        keep = f"src_{int(c.start or 0)}_{int(c.end or 0) + 1}.mp4"
        if src.exists():
            for f in src.iterdir():
                if not f.is_file():
                    continue  # never a folder (unlink would stop the whole clean-up)
                stale = (time.time() - f.stat().st_mtime) / 3600 > 24
                if f.name not in (keep, "upload.mp4", "reel.mp4") and (stale or not f.name.endswith(".part")):
                    if f.name.endswith(".part") and not stale:
                        continue
                    f.unlink(missing_ok=True)  # an old trim, a partial download or a whole-video leftover


_queue_runs: dict[int, int] = {}  # customer -> upload rounds running right now (the watchdog: an "uploading" clip with
_queue_lock = threading.Lock()   # no round running for its customer was left behind, e.g. by a database error)


def queue_running(tid: int) -> bool:
    with _queue_lock:
        return _queue_runs.get(int(tid), 0) > 0


def process_queue():
    """Upload queued clips whose publish time is within the lead window (YouTube holds them private until publishAt)."""
    from . import tenant as _t

    tid = _t.current()
    with _queue_lock:
        _queue_runs[tid] = _queue_runs.get(tid, 0) + 1
    try:
        return _process_queue()
    finally:
        with _queue_lock:
            _queue_runs[tid] = max(0, _queue_runs.get(tid, 0) - 1)


def _process_queue():
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
        if not quota.shared_room(quota.UPLOAD_COST):  # a customer on the owner's Google project: the owner's uploads first
            break
        if acl.upload_block():  # paused while this round was running: stop before the next video
            break
        now = utcnow()  # uploads take minutes: judge every clip's time against the real clock
        with SessionLocal() as s:
            c = s.get(Clip, cid)
            if not c or c.status != "queued" or c.destination_id in paused:
                continue
            if c.publish_at and c.publish_at > now + timedelta(hours=quota.upload_lead_hours()):
                continue
            if c.pc_item_id:  # a video from the folder on the PC (app/pcfolder.py): only with its file here and a time it
                from . import pcfolder  # can still keep — a missed time is moved to a new day by pcfolder.due, never late

                if not pcfolder.ready(c, now):
                    continue
            p = s.get(Project, c.project_id)
            errs = check_edit(p, c)
            if errs:
                c.status, c.error = "failed", "; ".join(errs)
                s.commit()
                continue
            from . import strikes

            if p is not None and p.mode not in ("original", "own") and strikes.is_blocked(p.channel_url):
                # the last gate: this creator is on the never-use list after a strike (strikes.py) — nothing of theirs goes up any more
                c.held_was, c.status, c.autoqueue, c.error = "queued", strikes.HELD, False, strikes.HELD_NOTE
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
                from .comments import after_upload
                after_upload(c)  # first comment: posted by post_due once the video is public
                s.commit()  # saved at once: a crash now must never upload the video twice
                if c.pc_item_id:  # from the PC folder: only now (uploaded is saved) the server's copy goes
                    from . import pcfolder

                    try:
                        pcfolder.after_upload(s, c)
                        s.commit()
                    except Exception as e:  # noqa: BLE001  (the clean-up deletes it later: it is saved on the PC)
                        s.rollback()
                        log.warning("PC video of clip %s: after the upload: %s", c.id, str(e)[:200])
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
            if not quota.shared_room(quota.COST["thumbnail"]):
                break  # a customer on the owner's Google project: the owner's uploads first
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
        from .jobs import enter, leave

        for tid in ids:
            if not enter(tid):  # this copy is being restored from a backup right now: next round
                continue
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
            finally:
                leave(tid)
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
    run.__name__ = _job_name(fn)
    return run


# every timed job added by start(): key -> when its last round started / ended and how many rounds run right now. The
# watchdog's guard (app/watchdog.py) restarts a job that stalled (one round hangs, so no new round may start).
JOB_STATE: dict[str, dict] = {}
BOOT = time.time()
SCH = None


def _job_name(fn) -> str:
    """A readable name for a timed job: `lambda: executor.submit(viral_refresh)` -> "viral_refresh"."""
    base = getattr(fn, "__name__", "job")
    if base == "<lambda>":
        code = getattr(fn, "__code__", None)
        names = [x for x in ((code.co_names + code.co_freevars) if code else ()) if x not in ("executor", "submit")]
        base = names[-1] if names else "job"
    return base


def _tracked(key: str, fn):
    def run():
        st = JOB_STATE.setdefault(key, {"started": 0.0, "done": 0.0, "running": 0})
        st["started"] = time.time()
        st["running"] = st.get("running", 0) + 1
        try:
            return fn()
        finally:
            st["running"] = max(0, st.get("running", 0) - 1)
            st["done"] = time.time()
    run.__name__ = key
    return run


def start():
    global SCH
    for_each_customer(recover)()
    sch = BackgroundScheduler(timezone="UTC")

    def add(fn, *args, **kw):
        base = _job_name(fn)
        key, n = base, 1
        while sch.get_job(key) is not None:
            n += 1
            key = f"{base}#{n}"
        JOB_STATE.setdefault(key, {"started": 0.0, "done": 0.0, "running": 0})
        sch.add_job(_tracked(key, for_each_customer(fn)), *args, id=key, name=key, **kw)

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
    #   post_comments        "First comment under new videos" (and the account's own on/off in My channels)
    #   strike_mails         "Permission mails to creators" (YouTube's notices in Gmail — they only open a case and tell the
    #                        owner —, the request to the creator — sent by itself only when the account switched that on in
    #                        Alerts → Strikes and the address is YouTube's own — and their answer)
    #   pc_due               "Videos from a folder on their PC"; uploads paused -> no file is asked for (inside pc_due)
    #   phone_events         "Phone notifications" (phone.enabled() also checks it, so no push goes out when it is off)
    #   freshen_due          "Keyword helper" (keywords refreshed before upload)
    #   meta_due             "Instagram + Facebook" (Shorts that became public; Instagram posts finished; PC get-back)
    #   thumb_tests          "Thumbnails" (with swap_weak_thumbnails: the A/B test of the second thumbnail)
    #   learn_daily          "Auto-tune" (and the account's own auto-tune switch; sources only with "Change sources")
    #   strike_mails         sending the request needs "Strike responder" too (strikes.send_due); reading YouTube's notices
    #                        and the creators' answers stays with "Permission mails to creators"
    #   always: daily_backup, sweep_stale, check_helper, cleanup_disk, retry_thumbnails, check_uploads, sync_all, sync_all_watch_time,
    #           strike_watch (a strike YouTube itself shows: never-use list, held clips, scheduled videos kept private — no switch),
    #           recover, unpark_due (only drafts the Super Admin parked), plans_ending (owner's copy, all accounts)
    # Budget: a customer on the owner's Google project spends only what the owner's due uploads leave (quota.shared_room)
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
    from .metadata import freshen_due; freshen_due = when_on("keywords.use", freshen_due); add(freshen_due, "interval", minutes=20, max_instances=1, coalesce=True)  # noqa: E702  fresh keywords before upload
    from .learn import daily as learn_daily  # Ideas -> What works: auto-tune once a day (checked hourly, acts once)
    add(when_on("ideas.autotune", learn_daily), "interval", hours=1, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=12))
    from .payments import expire_due

    def plans_ending():  # over all accounts at once, so it runs a single time, in the owner's copy
        from . import tenant

        with tenant.use(tenant.OWNER):
            expire_due()
    # 9 AM IST: warn 3 days before, end paid plans. Safe to repeat, so it also runs 5 minutes after every start, and a
    # run missed by a restart at 3:30 still happens within the hour
    sch.add_job(plans_ending, "cron", hour=3, minute=30, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sch.add_job(plans_ending, "date", run_date=datetime.now(timezone.utc) + timedelta(minutes=5), misfire_grace_time=3600)
    from .backup import daily as daily_backup
    add(daily_backup, "interval", hours=6, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=15))  # one backup a day per account (skips when today's exists)
    from .phone import events as phone_events
    add(when_on("phone.use", phone_events), "interval", minutes=10, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3))  # live / 10 clips / views / subscribers / needs you
    from .comments import post_due as post_comments
    add(when_on("uploads.comment", post_comments), "interval", minutes=10, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=6))  # first comment under videos that went public
    from .meta import post_due as meta_due
    add(when_on("meta.use", meta_due), "interval", minutes=10, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5))  # Instagram Reels + Facebook when a Short is public
    from .pcfolder import due as pc_due
    add(when_on("clips.pc_folder", pc_due), "interval", minutes=2, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=4))  # PC-folder videos: ask for files, move missed times
    from .autopilot import unpark_due
    add(unpark_due, "interval", hours=1, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=12))  # parked clips back, a few a day (once a day)
    from .strikes import mail_round as strike_mails, watch as strike_watch
    add(strike_watch, "interval", minutes=10, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=7))  # protect the channel after a strike
    # in the scheduler's own thread, NOT the single render worker (Gmail can be slow; the round never waits for a lock)
    add(when_on("gmail.send", strike_mails), "interval", minutes=15, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=9))  # YouTube's notices, the request, the answer
    from . import watchdog
    add(watchdog.run, "interval", minutes=10, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=11))  # self-repair of known problems (app/watchdog.py)
    sch.start()
    SCH = sch
    watchdog.start_guard(sch)  # its own thread: finds a stalled timed job even when the scheduler's threads are all stuck
    return sch

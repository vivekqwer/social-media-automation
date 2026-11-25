"""Self-repair ("watchdog"): a fixed set of safe, reversible fixes for problems that are known to happen, and one plain
message to the owner for everything it cannot fix.

No code changes itself and no AI touches the app: every remedy below is ordinary code, written and tested in advance.
`run()` is a timed job (every 10 minutes, for every account, as that account). `start_guard()` starts one extra thread
that watches the timed jobs themselves (it must work even when every scheduler thread is stuck).

Remedies (each can be switched off on its own; "Self-repair" switches all of them off):
  stuck      a clip left in "rendering" with no render job for it, or in "uploading" with no upload round running,
             goes back a step with the same atomic claims the app uses (render: draft + render again, like a restart;
             upload: back to "queued" with its slot — only when the channel's video list, read after the upload
             stopped, does NOT have a video with its title; else the owner is asked to look, nothing is uploaded)
  retry      a failed clip with a passing reason (no helper for 24 hours while a helper is online now, network / 5xx,
             "database is locked", Google's own hiccups) is tried again after 30 min, 2 h and 8 h, then given up with
             one message. Never: copyright, not Creative Commons, strikes / held creators, private or removed sources,
             rule checks, logins, the daily limit. A failed UPLOAD is queued again only with the same title check.
  late       a queued clip whose publish time passed more than an hour ago gets the next free daily slot (18:00 India
             time, after the last scheduled day) and a note — never a late upload without a time
  disk       under 1.0 GB free: the existing clean-up (scheduler.cleanup_disk) at once, the archive rule
             (archive.free_uploaded: on YouTube AND saved on the PC with the same sha256), oldest first, and broken-off
             ".part" files older than 6 hours. Nothing else is ever deleted. Still low: one message.
  login      an expired YouTube or Gmail login cannot be fixed by the server: one message a day with the button to press
  helper     the helper PC offline for 30 minutes while jobs wait: one message; jobs waiting longer than 24 hours keep
             waiting while a helper was seen in the last 2 days (scheduler.sweep_stale)
  errors     the same place in "System errors" more than ERR_PER_HOUR times in an hour: a passing cause (database
             busy, network, Google) is marked "handled" (and only reported when it lasts 3 hours); "No space left" runs
             the disk remedy; anything else is one message a day with the details to copy. The list also clears
             itself: a passing fault not seen again for 6 hours is resolved (a plain line in Fixed by itself), any
             other error quiet for 3 days moves to "Seen before, quiet now"; neither counts, nothing is deleted
  scheduler  a timed job that has not finished a round for 3x its interval (at least 90 minutes) is started again
             (3 times a day at most); after that, one message
Already in the app, so not repeated here: thumbnails YouTube refused are retried every 30 minutes
(scheduler.retry_thumbnails); after "quotaExceeded" uploads wait for the reset and go on by themselves
(scheduler._quota_blocked + quota.exhaust, kept across restarts in the quota counter).

Bounds: every fix has a per-item count (kept in the setting "watchdog_state") and a per-day cap per account; the
state holds only the last 7-14 days. Every fix is written to the activity log and to Alerts -> System
"Fixed by itself". Messages go by e-mail always (the owner's own Gmail, its own daily count), and to the phone (ntfy),
Telegram and WhatsApp when those are set up; each message at most once a day, at most ALERTS_PER_DAY a day.
"""
import html
import json
import logging
import re
import threading
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, update

from . import tenant
from .db import Clip, Destination, Project, SessionLocal, Worker, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

CFG_KEY, STATE_KEY = "watchdog_cfg", "watchdog_state"
REMEDIES = {
    "stuck": "Clips stuck while being made or uploaded go back a step",
    "retry": "Clips that failed for a passing reason are tried again (3 times at most)",
    "late": "A waiting clip whose time passed gets the next free day",
    "disk": "Server disk almost full: safe clean-up",
    "login": "An expired YouTube or Gmail login: one message a day",
    "helper": "Helper PC offline: one message; waiting jobs keep waiting",
    "errors": "Repeating system errors are grouped and reported once",
    "scheduler": "A stalled timed job is started again",
}
EXISTING = {"thumbs": "Refused thumbnails are sent again every 30 minutes (already in the app)",
            "quota": "After YouTube's daily limit, uploads wait for the reset and go on by themselves (already in the app)"}
CAP = {"stuck": 20, "retry": 20, "late": 60, "disk": 4, "errors": 10, "scheduler": 6}  # fixes a day, per account
MAX_TRIES = {"stuck": 3, "retry": 3, "late": 10}  # fixes of the same clip, ever (the state keeps 14 days)
BACKOFF = [timedelta(minutes=30), timedelta(hours=2), timedelta(hours=8)]
STUCK_RENDER = timedelta(minutes=30)
STUCK_UPLOAD = timedelta(minutes=20)
UPLOAD_UNSURE = timedelta(hours=6)  # the channel's video list could not be read this long: ask the owner
LATE_BY = timedelta(hours=1)
LOW_GB = 1.0
PART_AGE_H = 6
HELPER_OFF = timedelta(minutes=30)
ERR_PER_HOUR = 6
ERR_LASTS = timedelta(hours=3)
STALL_MIN = 90 * 60  # seconds
ALERTS_PER_DAY = 12
KEEP_FIXED = timedelta(days=7)
KEEP_ITEMS = timedelta(days=14)
IST = timedelta(hours=5, minutes=30)

HELPER_NOTE = "No helper PC fetched this video for 24 hours"
NEVER = ("copyright", "creative commons", "not cc", "strike", "never-use", "held", "private, removed or members-only",
         "private video", "video unavailable", "members-only", "removed", "deleted", "permission", "too long",
         "rejected", "quotaexceeded", "uploadlimitexceeded", "quota", "reconnect", "login", "not allowed", "blocked")
TRANSIENT = re.compile(
    r"database is locked|\b50[0234]\b|backendError|internalError|transientError|Service Unavailable|Bad Gateway|"
    r"Gateway Time-?out|timed out|Timeout|time-out|Connection (?:reset|aborted|refused|error)|RemoteDisconnected|"
    r"ConnectError|ReadError|WriteError|Temporary failure in name resolution|Name or service not known|"
    r"Network is unreachable|Max retries exceeded|EOF occurred|SSLError|BrokenPipe|IncompleteRead|rateLimitExceeded|"
    r"getaddrinfo failed", re.I)
DISK_FULL = re.compile(r"No space left on device|Errno 28|disk (?:is )?full", re.I)

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


# ---------------------------------------------------------------- switches
def allowed() -> bool:
    """The Super Admin's per-account switch "Self-repair" (always on in the owner's own copy unless set off below)."""
    try:
        from . import acl

        return acl.feature("watchdog.repair")
    except Exception:  # noqa: BLE001
        return True


def config() -> dict:
    try:
        v = json.loads(get_setting(CFG_KEY) or "{}")
        v = v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        v = {}
    return {"on": v.get("on", True) is not False, "off": sorted(k for k in (v.get("off") or []) if k in REMEDIES)}


def save_config(on: bool, off: list) -> dict:
    set_setting(CFG_KEY, json.dumps({"on": bool(on), "off": sorted({k for k in off or [] if k in REMEDIES})}))
    return config()


def enabled(remedy: str | None = None) -> bool:
    try:
        cfg = config()
        return bool(cfg["on"] and allowed() and (remedy is None or remedy not in cfg["off"]))
    except Exception:  # noqa: BLE001  (unknown: behave as before the watchdog existed)
        return False


# ---------------------------------------------------------------- state (one small setting per account)
def _iso(dt) -> str:
    return dt.isoformat()


def _dt(v):
    try:
        return datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def _load() -> dict:
    try:
        st = json.loads(get_setting(STATE_KEY) or "{}")
        st = st if isinstance(st, dict) else {}
    except (ValueError, TypeError):
        st = {}
    for k, empty in (("used", {}), ("items", {}), ("seen", {}), ("fixed", []), ("told", {}), ("errs", {}),
                     ("handled", {}), ("resolved", {}), ("quiet", {})):
        if not isinstance(st.get(k), type(empty)):
            st[k] = empty
    st.setdefault("day", "")
    st.setdefault("alerts_today", 0)
    return st


def _save(st: dict):
    set_setting(STATE_KEY, json.dumps(st, separators=(",", ":")))


def _roll(st: dict, now):
    day = now.strftime("%Y-%m-%d")
    if st.get("day") != day:
        st["day"], st["used"], st["alerts_today"] = day, {}, 0


def _prune(st: dict, now):
    st["fixed"] = [f for f in st["fixed"] if (_dt(f.get("at")) or now) > now - KEEP_FIXED][-300:]
    st["items"] = {k: v for k, v in st["items"].items() if (_dt(v.get("at")) or now) > now - KEEP_ITEMS}
    st["told"] = {k: v for k, v in st["told"].items() if (_dt(v) or now) > now - timedelta(days=7)}
    st["handled"] = {k: v for k, v in st["handled"].items() if (_dt(v.get("at") if isinstance(v, dict) else v) or now)
                     > now - timedelta(days=2)}


def _cap_ok(st: dict, remedy: str) -> bool:
    return int(st["used"].get(remedy, 0)) < CAP.get(remedy, 10)


def _use(st: dict, remedy: str):
    st["used"][remedy] = int(st["used"].get(remedy, 0)) + 1


def _item(st: dict, key: str, now) -> dict:
    it = st["items"].get(key)
    if not isinstance(it, dict):
        it = st["items"][key] = {"n": 0, "at": _iso(now), "first": _iso(now)}
    return it


def _log_activity(text: str):
    try:
        from . import acl

        tid = tenant.current()
        acl.record(acl._account_of(tid), "system", "watchdog", "Fixed by itself: " + text[:440], tid)
    except Exception:  # noqa: BLE001
        pass


def _fixed(st: dict, remedy: str, text: str, now, clip_id: int | None = None):
    st["fixed"].append({"at": _iso(now), "r": remedy, "t": text[:400], "kind": "fixed", "clip": clip_id})
    _use(st, remedy)
    _log_activity(text)


# ---------------------------------------------------------------- telling the owner
def deliver(text: str) -> dict:
    """One message to the owner: e-mail always (own daily count), phone (ntfy), Telegram and WhatsApp when set up.
    Never raises."""
    msg = "Raji Studio could not fix this by itself.\n" + text
    sent = {}
    try:
        from .gmail import notify_owner

        sent["mail"] = bool(notify_owner(msg, kind="watchdog"))
    except Exception as e:  # noqa: BLE001
        log.warning("watchdog mail: %s", type(e).__name__)
    try:
        from . import phone

        if phone.enabled():
            sent["phone"] = bool(phone.push(msg, title="Raji Studio needs you", tags="warning", priority=4))
    except Exception as e:  # noqa: BLE001
        log.warning("watchdog phone: %s", type(e).__name__)
    try:
        from . import telegram

        if telegram.enabled():
            telegram._call("sendMessage", chat_id=telegram.chat_id(), text=html.escape(msg)[:4000],
                           disable_web_page_preview=True)
            sent["telegram"] = True
    except Exception as e:  # noqa: BLE001
        log.warning("watchdog telegram: %s", type(e).__name__)
    try:
        from . import whatsapp

        if whatsapp.enabled():
            sent["whatsapp"] = whatsapp.send(msg)
    except Exception as e:  # noqa: BLE001
        log.warning("watchdog whatsapp: %s", type(e).__name__)
    return sent


def _alert(st: dict, key: str, text: str, now, every: timedelta = timedelta(hours=24)) -> bool:
    last = _dt(st["told"].get(key))
    if last and now - last < every:
        return False
    if int(st.get("alerts_today") or 0) >= ALERTS_PER_DAY:
        return False
    st["told"][key] = _iso(now)
    st["alerts_today"] = int(st.get("alerts_today") or 0) + 1
    st["fixed"].append({"at": _iso(now), "r": key.split(":")[0], "t": text[:600], "kind": "alert"})
    deliver(text)
    return True


# ---------------------------------------------------------------- helpers
def _when(t) -> str:
    w = t + IST
    return f"{w:%d %b %Y} {w:%H:%M} India time"


def _name(c) -> str:
    return f"“{(c.title or '').strip()[:80]}” (clip #{c.id})" if (c.title or "").strip() else f"clip #{c.id}"


def classify(error) -> str | None:
    """'helper' / 'transient' for a reason worth trying again; None for everything else (it stays failed)."""
    e = str(error or "")
    low = e.lower()
    if HELPER_NOTE.lower() in low:
        return "helper"
    if any(w in low for w in NEVER):
        return None
    return "transient" if PASSING.search(e) else None


def _helper_online(kind: str) -> bool:
    from .workers import ONLINE_WINDOW, can_do, central_online

    with SessionLocal() as s:
        rows = s.query(Worker).filter(Worker.approved.is_(True), Worker.last_seen >= utcnow() - ONLINE_WINDOW).all()
        if any(can_do(w.can, kind) for w in rows):
            return True
    try:
        return central_online(kind)
    except Exception:  # noqa: BLE001
        return False


def on_youtube(s, c, since, synced_after) -> str:
    """Is a video with this clip's title on its channel? Read only from the channel's video list the app already keeps
    (stats.sync_channel, every 10 minutes). 'yes' / 'no', or 'unknown' when that list was not read after
    `synced_after` (then nothing may be uploaded again)."""
    from .db import ChannelStat, VideoStat

    last = s.query(func.max(ChannelStat.at)).filter(ChannelStat.destination_id == c.destination_id).scalar()
    if not last or last < synced_after:
        return "unknown"
    title = (c.title or "").strip()[:100]
    if not title:
        return "unknown"
    mine = {y for (y,) in s.query(Clip.youtube_id).filter(Clip.youtube_id.isnot(None))}
    for v in s.query(VideoStat).filter(VideoStat.destination_id == c.destination_id).all():
        if (v.title or "").strip()[:100] == title and v.video_id not in mine:
            return "yes"
    return "no"


def daily_slot(s, c):
    """The next free daily slot of the clip's channel (18:00 India time), after its last scheduled day."""
    from .autopilot import _next_slot

    return _next_slot(s, 24, c.destination_id, "18:00", -330)


def _give_up(st: dict, now, key: str, text: str):
    it = st["items"].get(key) or {}
    if not it.get("told"):
        if _alert(st, key, text, now, every=timedelta(days=30)):
            it["told"] = True
            st["items"][key] = it


# ---------------------------------------------------------------- 1. stuck clips
def _stuck(st: dict, now) -> dict:
    from . import jobs, scheduler

    tid = tenant.current()
    out = {"render": 0, "upload": 0, "waiting": 0}
    with SessionLocal() as s:
        rows = s.query(Clip.id, Clip.status).filter(Clip.status.in_(["rendering", "uploading"])).all()
    cur = {f"{st_}:{cid}" for cid, st_ in rows}
    for k in [k for k in st["seen"] if k.split(":")[0] in ("rendering", "uploading") and k not in cur]:
        st["seen"].pop(k, None)
    for k in cur:
        st["seen"].setdefault(k, _iso(now))
    pending = jobs.queued("run_render", tid)
    for cid, status in rows:
        first = _dt(st["seen"].get(f"{status}:{cid}")) or now
        key = f"stuck:{cid}"
        if status == "rendering":
            if now - first < STUCK_RENDER or cid in pending or jobs.busy(tid):
                continue
        elif now - first < STUCK_UPLOAD or scheduler.queue_running(tid):
            continue
        it = _item(st, key, now)
        if it["n"] >= MAX_TRIES["stuck"]:
            with SessionLocal() as s:
                c = s.get(Clip, cid)
                nm = _name(c) if c else f"clip #{cid}"
            _give_up(st, now, key, f"{nm} keeps stopping while it is being {'made' if status == 'rendering' else 'uploaded'}. "
                                   "It was put back 3 times. Open it in Clips and press Render (or Retry); if it stops "
                                   "again, open Setup guide → If something goes wrong → Copy details and send them to "
                                   "the person who looks after the system.")
            continue
        if not _cap_ok(st, "stuck"):
            break
        done = _rerender(cid, st, now, it) if status == "rendering" else _upload_lost(cid, st, now, first, it)
        if done == "waiting":
            out["waiting"] += 1
        elif done:
            out["render" if status == "rendering" else "upload"] += 1
            st["seen"].pop(f"{status}:{cid}", None)
    return out


def _rerender(cid: int, st: dict, now, it: dict) -> bool:
    from .jobs import executor
    from .render import run_render

    with SessionLocal() as s:
        res = s.execute(update(Clip).where(Clip.id == cid, Clip.status == "rendering").values(status="draft", error=None))
        s.commit()
        if res.rowcount != 1:
            return False
        c = s.get(Clip, cid)
        nm = _name(c)
    executor.submit(run_render, cid)
    it["n"], it["at"] = it["n"] + 1, _iso(now)
    _fixed(st, "stuck", f"{nm} had stopped while it was being made (no job was working on it). It is being made again.", now, cid)
    return True


def _upload_lost(cid: int, st: dict, now, first, it: dict):
    """An "uploading" clip with no upload round running. Never uploaded twice: back to the queue only when the channel's
    video list, read after it stopped, does not have its title."""
    from .comments import after_upload

    with SessionLocal() as s:
        c = s.get(Clip, cid)
        if c is None or c.status != "uploading":
            return False
        nm = _name(c)
        if c.youtube_id:  # the upload finished and was saved; only the last step was lost
            res = s.execute(update(Clip).where(Clip.id == cid, Clip.status == "uploading").values(status="uploaded"))
            s.commit()
            if res.rowcount != 1:
                return False
            s.refresh(c)
            after_upload(c)
            s.commit()
            it["n"], it["at"] = it["n"] + 1, _iso(now)
            _fixed(st, "stuck", f"{nm} was on YouTube already but still showed “uploading”. Marked as uploaded.", now, cid)
            return True
        seen = on_youtube(s, c, first - timedelta(hours=1), first + timedelta(minutes=15))
        if seen == "unknown" and now - first <= UPLOAD_UNSURE:
            return "waiting"
        if seen in ("unknown", "yes"):  # it may be on YouTube: never sent again by itself; the owner decides
            why = ("a video with the same title is already on your channel" if seen == "yes" else
                   "the channel's video list could not be read since, so it is not known whether it reached YouTube")
            res = s.execute(update(Clip).where(Clip.id == cid, Clip.status == "uploading", Clip.youtube_id.is_(None))
                            .values(status="failed", error=(f"Stopped half-way through its upload; {why}. It was not "
                                                            "uploaded again, so it cannot appear twice. Check YouTube "
                                                            "Studio first.")[:500]))
            s.commit()
            if res.rowcount == 1:
                it["n"] = MAX_TRIES["stuck"]
                _give_up(st, now, f"stuck:{cid}", f"{nm} stopped half-way through its upload, and {why}. It was NOT "
                                                  "uploaded again, so it cannot appear twice. Look in YouTube Studio: if "
                                                  "the video is there, remove this clip; if not, press Retry on the clip "
                                                  "(Alerts → Not working).")
            return False
        slot = c.publish_at
        if not slot or slot < now + timedelta(minutes=30):
            from .autopilot import SLOT_LOCK

            with SLOT_LOCK:
                slot = daily_slot(s, c)
        res = s.execute(update(Clip).where(Clip.id == cid, Clip.status == "uploading", Clip.youtube_id.is_(None))
                        .values(status="queued", publish_at=slot,
                                error="The upload was interrupted; it is waiting for its turn again (fixed by itself)."))
        s.commit()
        if res.rowcount != 1:
            return False
    it["n"], it["at"] = it["n"] + 1, _iso(now)
    _fixed(st, "stuck", f"{nm} had stopped half-way through its upload (it is not on the channel). It waits for its "
                        f"turn again, going live {_when(slot)}.", now, cid)
    return True


# ---------------------------------------------------------------- 2. retry failed clips
def retry_wait(st: dict, cid: int) -> datetime | None:
    """When the watchdog tries this failed clip again (None: it does not)."""
    it = st["items"].get(f"retry:{cid}")
    if not isinstance(it, dict):
        return None
    n = int(it.get("n") or 0)
    if n >= MAX_TRIES["retry"]:
        return None
    base = _dt(it.get("last")) or _dt(it.get("first"))
    return base + BACKOFF[n] if base else None


def _retry(st: dict, now) -> dict:
    out = {"retried": 0, "requeued": 0, "waiting": 0, "given_up": 0}
    with SessionLocal() as s:
        rows = s.query(Clip.id, Clip.error).filter(Clip.status == "failed").order_by(Clip.id).limit(500).all()
    failed = {cid for cid, _e in rows}
    for k in [k for k in st["items"] if k.startswith("retry:") and int(k[6:]) not in failed]:
        it = st["items"][k]
        it.pop("first", None)  # it left "failed" (fixed, or retried by hand): a new failure starts a new back-off
        it["out"] = True
    for cid, err in rows:
        kind = classify(err)
        if kind is None:
            continue
        key = f"retry:{cid}"
        it = st["items"].get(key)
        if not isinstance(it, dict):
            it = st["items"][key] = {"n": 0, "at": _iso(now), "first": _iso(now)}
        if it.pop("out", None):
            it["first"], it["at"] = _iso(now), _iso(now)
        it.setdefault("first", _iso(now))
        n = int(it.get("n") or 0)
        if n >= MAX_TRIES["retry"]:
            with SessionLocal() as s:
                c = s.get(Clip, cid)
                nm = _name(c) if c else f"clip #{cid}"
            _give_up(st, now, key, f"{nm} failed again after 3 tries by itself. The last reason: "
                                   f"{_short(err)}. Open Alerts → Broken and press Retry when the cause is gone.")
            out["given_up"] += 1
            continue
        if now < (_dt(it.get("last")) or _dt(it["first"])) + BACKOFF[n]:
            out["waiting"] += 1
            continue
        if not _cap_ok(st, "retry"):
            break
        res = _retry_one(cid, kind, st, now, it)
        if res:
            out[res] += 1
    return out


def _short(err) -> str:
    from .alerts import reason

    return reason(err)[:200]


def _retry_one(cid: int, kind: str, st: dict, now, it: dict) -> str | None:
    from . import strikes
    from .jobs import executor
    from .policy import check_edit
    from .render import run_render

    with SessionLocal() as s:
        c = s.get(Clip, cid)
        if c is None or c.status != "failed":
            return None
        p = s.get(Project, c.project_id)
        nm, why = _name(c), _short(c.error)
        if p is None or (p.mode not in ("original", "own") and strikes.is_blocked(p.channel_url)):
            return None
        if kind == "helper" and not _helper_online("reel" if p.mode == "original" else "source"):
            return None  # only once a helper is online again (not counted as a try)
        upload = bool(c.output_path) and Path(c.output_path).exists()
        if upload:  # the video was made; its upload broke off: never twice on the channel
            seen = on_youtube(s, c, (c.attempted_at or now) - timedelta(hours=3), (c.attempted_at or now) + timedelta(minutes=15))
            if seen == "unknown":
                if now - (c.attempted_at or now) > UPLOAD_UNSURE:  # the list is not being read (e.g. the login): give up
                    it["n"] = MAX_TRIES["retry"]
                    _give_up(st, now, f"retry:{cid}", f"The upload of {nm} broke off ({why[:120]}), and the channel's video "
                                                      "list could not be read since, so it was NOT uploaded again. Look in "
                                                      "YouTube Studio; if the video is not there, press Retry on the clip.")
                return None  # wait until the channel's video list was read after the failure
            it["n"], it["last"], it["at"] = int(it.get("n") or 0) + 1, _iso(now), _iso(now)
            if seen == "yes":
                it["n"] = MAX_TRIES["retry"]
                _give_up(st, now, f"retry:{cid}", f"The upload of {nm} broke off, but a video with the same title is on "
                                                  "your channel, so it was NOT uploaded again. Look in YouTube Studio; "
                                                  "if that video is not this clip, press Retry on the clip.")
                return None
            slot = c.publish_at
            if c.pc_item_id is None and (not slot or slot < now + LATE_BY):
                from .autopilot import SLOT_LOCK

                with SLOT_LOCK:
                    slot = daily_slot(s, c)
            res = s.execute(update(Clip).where(Clip.id == cid, Clip.status == "failed")
                            .values(status="queued", publish_at=slot,
                                    error=f"Tried again by itself after: {why}"[:500]))
            s.commit()
            if res.rowcount != 1:
                return None
            _fixed(st, "retry", f"The upload of {nm} had failed ({why[:120]}). It waits for its turn again"
                                + (f", going live {_when(slot)}." if slot else "."), now, cid)
            return "requeued"
        if c.pc_item_id or check_edit(p, c):
            return None  # a PC-folder video is never made on the server; a clip that breaks a rule stays failed
        res = s.execute(update(Clip).where(Clip.id == cid, Clip.status == "failed")
                        .values(status="rendering", error=None, fetch_tries=0, job_lease=None, fetch_since=None))
        s.commit()
        if res.rowcount != 1:
            return None
    it["n"], it["last"], it["at"] = int(it.get("n") or 0) + 1, _iso(now), _iso(now)
    executor.submit(run_render, cid)
    _fixed(st, "retry", f"{nm} had failed ({why[:120]}). It is being made again (try {it['n']} of 3).", now, cid)
    return "retried"


# ---------------------------------------------------------------- 3. queued clips whose time passed
def _late(st: dict, now) -> dict:
    from .autopilot import SLOT_LOCK

    out = {"moved": 0}
    with SLOT_LOCK, SessionLocal() as s:
        rows = s.query(Clip).filter(Clip.status == "queued", Clip.pc_item_id.is_(None), Clip.publish_at.isnot(None),
                                    Clip.publish_at < now - LATE_BY).order_by(Clip.publish_at, Clip.id).all()
        for c in rows:
            key = f"late:{c.id}"
            it = _item(st, key, now)
            if it["n"] >= MAX_TRIES["late"]:
                _give_up(st, now, key, f"{_name(c)} has missed its day {MAX_TRIES['late']} times — uploads seem to be "
                                       "stopped (a paused channel, an expired login, or the daily limit). Open the "
                                       "Dashboard → Problems right now.")
                continue
            if not _cap_ok(st, "late"):
                break
            old = c.publish_at
            slot = daily_slot(s, c)
            res = s.execute(update(Clip).where(Clip.id == c.id, Clip.status == "queued", Clip.publish_at == old)
                            .values(publish_at=slot, error=(f"Moved to {_when(slot)} by itself: its time "
                                                            f"({_when(old)}) passed before it could go up.")[:500]))
            s.commit()
            if res.rowcount != 1:
                continue
            it["n"], it["at"] = it["n"] + 1, _iso(now)
            _fixed(st, "late", f"{_name(c)} missed its time ({_when(old)}); moved to the next free day, {_when(slot)}.", now, c.id)
            out["moved"] += 1
    return out


# ---------------------------------------------------------------- 5. disk
def _free_gb():
    from .autopilot import disk_free_gb

    return disk_free_gb()


def stale_parts(root: Path, now_ts: float | None = None) -> int:
    """Broken-off ".part" files (a send or a restore that stopped) older than PART_AGE_H hours, inside the clip folders
    only. Nothing else."""
    now_ts = now_ts or time.time()
    n = 0
    if not root.exists():
        return 0
    for d in root.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        for f in list(d.glob("*.part")) + list((d / "src").glob("*.part") if (d / "src").is_dir() else []):
            try:
                if f.is_file() and not f.is_symlink() and now_ts - f.stat().st_mtime > PART_AGE_H * 3600:
                    f.unlink()
                    n += 1
            except OSError:
                pass
    return n


def _disk(st: dict, now, force: bool = False) -> dict:
    from . import archive, scheduler
    from .config import DATA_DIR

    free = _free_gb()
    out = {"free_gb": free, "freed": 0, "parts": 0}
    if free is None or free >= LOW_GB:
        st["seen"].pop("disk_low", None)
        return out
    st["seen"].setdefault("disk_low", _iso(now))
    last = _dt(st["seen"].get("disk_at"))
    if not force and last and now - last < timedelta(hours=1):
        return out
    if not _cap_ok(st, "disk"):
        return out
    st["seen"]["disk_at"] = _iso(now)
    scheduler.cleanup_disk()  # the app's own safe clean-up first
    out["parts"] = stale_parts(DATA_DIR / "clips")
    try:
        from .meta import status as meta_status

        ms = meta_status()
        meta_on = bool(ms["connected"] and ms["on"])
    except Exception:  # noqa: BLE001  (unknown: a cross-post is possible, so its file is kept)
        meta_on = True
    with SessionLocal() as s:  # uploaded + saved on the PC (same sha256): the oldest uploads first, until there is room
        rows = s.query(Clip).filter(Clip.status == "uploaded", Clip.archived_at.isnot(None)) \
            .order_by(Clip.attempted_at, Clip.id).all()
        for c in rows:
            f = _free_gb()
            if f is not None and f >= LOW_GB:
                break
            try:
                if archive.free_uploaded(s, c, archive.folder(c), meta_on, now):
                    out["freed"] += 1
                s.commit()
            except Exception as e:  # noqa: BLE001
                s.rollback()
                log.warning("watchdog disk: clip %s: %s", c.id, str(e)[:200])
    after = _free_gb()
    out["free_gb"] = after
    _fixed(st, "disk", f"The server disk had {free:.2f} GB free. Ran the safe clean-up: {out['freed']} uploaded video(s) "
                       f"that are saved on your PC and {out['parts']} broken-off part file(s) removed; now "
                       f"{(after or 0):.2f} GB free.", now)
    if after is not None and after < LOW_GB:
        _alert(st, "disk:low", f"The server disk is almost full ({after:.2f} GB free) even after the safe clean-up. "
                               "Nothing that exists only on the server was deleted. Keep the helper PC on so uploaded "
                               "videos move to it (they are then removed from the server by themselves), or make the "
                               "Railway volume bigger (Railway → the service → Volume → Size).", now)
    return out


# ---------------------------------------------------------------- 7. logins
def _login(st: dict, now) -> dict:
    out = {"youtube": 0, "gmail": 0}
    try:
        from .main import _dest_dict
    except Exception:  # noqa: BLE001
        _dest_dict = None
    with SessionLocal() as s:
        dests = s.query(Destination).all()
        bad = []
        for d in dests:
            need = bool(d.paused and "login expired" in (d.paused_reason or ""))
            if not need and _dest_dict is not None:
                try:
                    need = bool(_dest_dict(d).get("needs_reconnect"))
                except Exception:  # noqa: BLE001
                    need = False
            if need:
                bad.append((d.id, d.name or "your channel"))
    for did, name in bad:
        if _alert(st, f"login:yt:{did}", f"The YouTube login of {name} expired or was taken back, so its uploads wait "
                                         "(nothing is lost). Press: My channels → “Connect a YouTube channel” and "
                                         "choose the same channel. Then press Resume on that channel.", now):
            out["youtube"] += 1
    try:
        from .gmail import connected_email

        if connected_email() and get_setting("gmail_broken"):
            if _alert(st, "login:gmail", "The Gmail login expired, so mails (to you and to creators) cannot be sent. "
                                         "Press: My channels → Gmail → “Connect Gmail” and choose the same account.", now):
                out["gmail"] += 1
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------- 8. helper offline
def _helper(st: dict, now) -> dict:
    from .main import _helper_state

    with SessionLocal() as s:
        hp = _helper_state(s)
    if not hp["stuck"]:
        st["seen"].pop("helper_off", None)  # (the message itself stays once a day: a helper that comes and goes never floods)
        return {"offline": False}
    first = _dt(st["seen"].setdefault("helper_off", _iso(now))) or now
    if now - first >= HELPER_OFF:
        _alert(st, "helper:off", f"Your helper PC has been offline for {int((now - first).total_seconds() // 60)} minutes "
                                 f"and {hp['stuck']} job(s) are waiting for it. Start the PC and check its internet; "
                                 "the helper starts by itself. Nothing is lost: the jobs keep waiting for it.", now)
    return {"offline": True, "waiting": hp["stuck"]}


# ---------------------------------------------------------------- 9. repeating system errors
def err_key(e: dict) -> str:
    raw = f"{e.get('type')}|{e.get('line')}|{e.get('text')}"
    return f"{zlib.crc32(raw.encode('utf-8', 'replace')):08x}"


def _mark_handled(st: dict, k: str, why: str, now) -> timedelta:
    """Remember that the watchdog took care of this error; returns how long it has been going on."""
    h = st["handled"].get(k)
    first = _dt(h.get("first")) if isinstance(h, dict) else None
    last = _dt(h.get("at")) if isinstance(h, dict) else None
    if not first or not last or now - last > timedelta(hours=2):
        first = now  # it had stopped for a while: a new episode
    st["handled"][k] = {"at": _iso(now), "first": _iso(first), "why": why}
    return now - first


def _errors(st: dict, now) -> dict:
    from .guide import recent_errors

    out = {"grouped": 0, "handled": 0, "alerted": 0}
    hour = now - timedelta(hours=1)
    groups: dict[str, list] = {}
    for e in recent_errors():
        k = err_key(e)
        n = int(e.get("count") or 1)
        hist = [h for h in st["errs"].get(k, []) if isinstance(h, list) and len(h) == 2]
        if not hist or hist[-1][1] != n:
            hist.append([_iso(now), n])
        base = [h for h in hist if (_dt(h[0]) or now) <= hour]
        start = base[-1][1] if base else hist[0][1]  # the count an hour ago (or when first seen)
        st["errs"][k] = ([base[-1]] if base else []) + [h for h in hist if (_dt(h[0]) or now) > hour]
        delta = n - start
        if delta > 0:
            groups.setdefault(str(e.get("where") or "?"), []).append((k, e, delta))
    st["errs"] = {k: v for k, v in st["errs"].items() if v and (_dt(v[-1][0]) or now) > now - timedelta(days=2)}
    for where, items in groups.items():
        total = sum(d for _k, _e, d in items)
        if total < ERR_PER_HOUR:
            continue
        out["grouped"] += 1
        text = " ".join(str(e.get("text") or "") + " " + str(e.get("type") or "") for _k, e, _d in items)
        if DISK_FULL.search(text) and enabled("disk"):
            _disk(st, now, force=True)
            for k, _e, _d in items:
                _mark_handled(st, k, "disk", now)
            out["handled"] += 1
            continue
        if PASSING.search(text):
            lasted = max(_mark_handled(st, k, "passing", now) for k, _e, _d in items)
            if not any(f.get("r") == "errors" and f.get("w") == where and (_dt(f.get("at")) or now) > hour
                       for f in st["fixed"]) and _cap_ok(st, "errors"):
                _fixed(st, "errors", f"“{where}” failed {total} times in an hour for a passing reason (the database "
                                     "was busy, or the network / Google had a hiccup). It is tried again by itself.", now)
                st["fixed"][-1]["w"] = where
            out["handled"] += 1
            if lasted and lasted >= ERR_LASTS:
                e0 = items[0][1]
                if _alert(st, f"errors:{where}", f"“{where}” has kept failing for {int(lasted.total_seconds() // 3600)} hours "
                                                 f"({total} times in the last hour). It usually passes by itself; when it "
                                                 "does not, open Setup guide → If something goes wrong → Copy details and "
                                                 "send them to the person who looks after the system. Details: "
                                                 f"{e0.get('type')}: {str(e0.get('text') or '')[:200]}", now):
                    out["alerted"] += 1
            continue
        e0 = items[0][1]
        if _alert(st, f"errors:{where}", f"“{where}” failed {total} times in the last hour, for a reason the system cannot "
                                         "fix by itself. Open Setup guide → If something goes wrong → Copy details and "
                                         "send them to the person who looks after the system. Details: "
                                         f"{e0.get('type')}: {str(e0.get('text') or '')[:200]}"
                                         + (f" ({e0.get('line')})" if e0.get("line") else ""), now):
            out["alerted"] += 1
    out.update(_settle(st, now))
    return out


RESOLVE_AFTER = timedelta(hours=6)  # a passing fault not seen again this long: resolved ("Fixed by itself")
QUIET_AFTER = timedelta(days=3)  # any other error not seen again this long: "Seen before, quiet now"
PASSING = re.compile(TRANSIENT.pattern + r"|QueuePool limit|connection timed out|Internal error encountered|HttpError 5\d\d",
                     re.I)


def _err_at(e: dict):
    return _dt(str(e.get("at") or "").rstrip("Z"))


def passing_kind(text: str) -> str:
    """What a passing fault was, in plain words."""
    if re.search(r"QueuePool|connection timed out", text, re.I):
        return "a short database rush"
    if re.search(r"database is locked", text, re.I):
        return "a short database lock"
    if re.search(r"backendError|Internal error encountered|HttpError 5\d\d|googleapis", text, re.I):
        return "a passing hiccup at Google / YouTube"
    if re.search(r"\b50[234]\b|Bad Gateway|Service Unavailable|Gateway Time-?out", text, re.I):
        return "a short outage while the server restarted"
    return "a short network drop"


def _settle(st: dict, now) -> dict:
    """System errors clear themselves: a passing fault not seen again for 6 hours is resolved (a plain line in Fixed by
    itself); any other error quiet for 3 days moves to "Seen before, quiet now". Nothing is deleted; when an error comes
    back (its time changes) it shows again as usual."""
    from .alerts import _where
    from .guide import recent_errors

    out = {"resolved": 0, "quiet": 0}
    live = set()
    for e in recent_errors():
        k, at = err_key(e), _err_at(e)
        if not at:
            continue
        live.add(k)
        stamp = str(e.get("at"))
        text = f"{e.get('type')} {e.get('text')}"
        if PASSING.search(text):
            if now - at >= RESOLVE_AFTER and (st["resolved"].get(k) or {}).get("at") != stamp:
                w = at + IST
                line = f"{_where(e.get('where'))}: {passing_kind(text)} on {w.day} {w:%b} — it did not come back."
                st["resolved"][k] = {"at": stamp, "line": line}
                st["fixed"].append({"at": _iso(now), "r": "errors", "t": line[:400], "kind": "fixed", "clip": None})
                _log_activity(line)
                out["resolved"] += 1
        elif now - at >= QUIET_AFTER and st["quiet"].get(k) != stamp:
            st["quiet"][k] = stamp
            out["quiet"] += 1
    st["resolved"] = {k: v for k, v in st["resolved"].items() if k in live}
    st["quiet"] = {k: v for k, v in st["quiet"].items() if k in live}
    return out


def settled() -> tuple[set, set]:
    """(resolved, quiet) System errors as (key, time) pairs: left out of the System errors tab and its count."""
    if not enabled("errors"):
        return set(), set()
    try:
        st = _load()
    except Exception:  # noqa: BLE001
        return set(), set()
    return ({(k, v.get("at")) for k, v in st["resolved"].items() if isinstance(v, dict)},
            {(k, v) for k, v in st["quiet"].items()})


def handled_keys() -> set:
    """System errors the watchdog took care of in the last 2 hours (shown as handled, left out of the Problems list)."""
    if not enabled("errors"):
        return set()
    try:
        now = utcnow()
        return {k for k, h in _load()["handled"].items()
                if isinstance(h, dict) and (_dt(h.get("at")) or now - timedelta(days=1)) > now - timedelta(hours=2)}
    except Exception:  # noqa: BLE001
        return set()


def pending_retries() -> dict:
    """{clip id: when it is tried again} for failed clips the watchdog will still try by itself."""
    if not enabled("retry"):
        return {}
    try:
        st = _load()
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for k in st["items"]:
        if k.startswith("retry:") and not st["items"][k].get("out"):
            w = retry_wait(st, int(k[6:]))
            if w:
                out[int(k[6:])] = w
    return out


# ---------------------------------------------------------------- the timed job
REMEDY_FN = {"stuck": _stuck, "retry": _retry, "late": _late, "disk": _disk, "login": _login, "helper": _helper,
             "errors": _errors}


def run(now=None) -> dict:
    """Every 10 minutes for each account (as that account). Idempotent: a second run right after changes nothing."""
    if not enabled():
        return {"off": True}
    tid = tenant.current()
    with _locks_guard:
        lock = _locks.setdefault(tid, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"busy": True}
    try:
        now = now or utcnow()
        st = _load()
        _roll(st, now)
        out = {}
        for name, fn in REMEDY_FN.items():
            if not enabled(name):
                continue
            try:
                out[name] = fn(st, now)
            except Exception as e:  # noqa: BLE001  (one remedy's problem never stops the others)
                log.exception("watchdog %s", name)
                out[name] = {"error": type(e).__name__}
                try:
                    from .guide import record_error

                    record_error("watchdog: " + name, e)
                except Exception:  # noqa: BLE001
                    pass
        _prune(st, now)
        _save(st)
        return out
    finally:
        lock.release()


def public() -> dict:
    """For Alerts -> System: what was fixed by itself (last 7 days), with counts, and the switches."""
    st = _load()
    now = utcnow()
    rows = [f for f in st["fixed"] if (_dt(f.get("at")) or now) > now - KEEP_FIXED]
    counts = {}
    for f in rows:
        if f.get("kind") == "fixed":
            counts[f.get("r")] = counts.get(f.get("r"), 0) + 1
    cfg = config()
    quiet = []
    try:  # System errors quiet for 3 days: kept, shown apart, not counted
        from .alerts import _where
        from .guide import recent_errors

        _r, q = settled()
        for e in recent_errors():
            if (err_key(e), str(e.get("at"))) in q:
                quiet.append({"at": e.get("at"), "title": _where(e.get("where")), "count": int(e.get("count") or 1),
                              "tech": f"{e.get('type')}: {e.get('text')}"})
    except Exception:  # noqa: BLE001
        quiet = []
    return {"on": enabled(), "allowed": allowed(), "config": cfg, "quiet": quiet,
            "remedies": [{"key": k, "name": v, "on": k not in cfg["off"]} for k, v in REMEDIES.items()],
            "existing": [{"key": k, "name": v} for k, v in EXISTING.items()],
            "counts": counts, "fixed_total": sum(counts.values()),
            "items": [{"at": f.get("at") + "Z" if f.get("at") else None, "remedy": f.get("r"), "kind": f.get("kind"),
                       "text": f.get("t"), "clip_id": f.get("clip")} for f in reversed(rows[-100:])]}


# ---------------------------------------------------------------- 10. the timed jobs themselves (own thread)
_guard_started = False


def _interval(job) -> float | None:
    """Seconds between two rounds of a timed job (a cron job: the gap between its next two times)."""
    from datetime import timezone

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    if isinstance(job.trigger, IntervalTrigger):
        return job.trigger.interval.total_seconds()
    if isinstance(job.trigger, CronTrigger):
        t1 = job.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        t2 = job.trigger.get_next_fire_time(t1, t1 + timedelta(seconds=1)) if t1 else None
        return (t2 - t1).total_seconds() if t1 and t2 else None
    return None  # a one-time job


def check_jobs(sch, now_ts: float | None = None) -> dict:
    """Restart timed jobs that stalled; tell the owner when that does not help. Runs in the owner's copy."""
    from . import scheduler

    now_ts = now_ts or time.time()
    out = {"restarted": [], "stalled": []}
    with tenant.use(tenant.OWNER):
        if not enabled("scheduler"):
            return out
        st = _load()
        now = utcnow()
        _roll(st, now)
        if sch is None or not getattr(sch, "running", False):
            _alert(st, "scheduler:down", "The server's timed jobs (uploads, sources, checks) have stopped. Restart the "
                                         "server: Railway → the service → ⋮ → Restart. Nothing is lost.", now)
            _save(st)
            return out
        restarts = st.setdefault("restarts", {})
        if st.get("restarts_day") != st["day"]:
            st["restarts_day"], restarts = st["day"], {}
            st["restarts"] = restarts
        for job in sch.get_jobs():
            key = job.id.split("~")[0]
            if key not in scheduler.JOB_STATE:
                continue  # not one of the timed jobs of scheduler.start() (e.g. the one-time plan check)
            every = _interval(job)
            if not every:
                continue
            limit = max(3 * every, STALL_MIN)
            js = scheduler.JOB_STATE.get(key) or {}
            started, done, running = js.get("started", 0.0), js.get("done", 0.0), js.get("running", 0)
            hung = running > 0 and now_ts - started > limit
            idle = now_ts - max(started, done, scheduler.BOOT) > limit
            if not (hung or idle):
                continue
            out["stalled"].append(key)
            log.warning("timed job %s stalled (%s)", key, "a round never ended" if hung else "no round started")
            n = int(restarts.get(key, 0))
            if n >= 3 or not _cap_ok(st, "scheduler"):
                _alert(st, f"scheduler:{key}", f"The timed job “{key}” has stopped and starting it again did not help. "
                                               "Restart the server: Railway → the service → ⋮ → Restart. Nothing is lost.", now)
                continue
            try:
                sch.remove_job(job.id)
                sch.add_job(job.func, trigger=job.trigger, id=f"{key}~{n + 1}", name=job.name,
                            max_instances=job.max_instances, coalesce=job.coalesce,
                            misfire_grace_time=job.misfire_grace_time)
            except Exception as e:  # noqa: BLE001
                log.warning("watchdog: restarting %s: %s", key, type(e).__name__)
                continue
            restarts[key] = n + 1
            scheduler.JOB_STATE[key] = {"started": now_ts, "done": now_ts, "running": 0}
            _fixed(st, "scheduler", f"The timed job “{key}” had stalled ({'one round never ended' if hung else 'no round started'} "
                                    f"for {int(limit // 60)} minutes). It was started again.", now)
            out["restarted"].append(key)
        _prune(st, now)
        _save(st)
    return out


def start_guard(sch, every: int = 600):
    """One daemon thread, outside the scheduler, that checks the timed jobs every `every` seconds."""
    global _guard_started
    if _guard_started:
        return
    _guard_started = True

    def loop():
        while True:
            time.sleep(every)
            try:
                check_jobs(sch)
            except Exception:  # noqa: BLE001
                log.exception("watchdog guard")

    threading.Thread(target=loop, name="watchdog-guard", daemon=True).start()

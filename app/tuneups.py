"""Shorter clips and more public posts a day (Nov 2026: 35 s clips, one post a day at 18:00 IST got few views).

* Clip length: new sources make 20 s clips (db.Channel.clip_len default). migrate_owner() sets the owner's existing
  sources that still have the old standard lengths (30 / 35 s) to 20 s, once (setting MIG_LEN). Clips already made,
  queued or on YouTube are never made again.
* Natural end (switch "clips.short"): when the source video's captions are at hand (the helper PC's whole caption
  track, or the track fetched for captions anyway), a new clip ends in a pause of the speech within +-3 s of its
  planned end instead of in the middle of a word (natural_end). Without captions the planned end stays.
* Public posts per day (Super Admin: POST /api/admin/posts-per-day, 1-5): the upload times of the copy's sources
  (except viral picks, which keep their own times). migrate_owner() sets the owner's copy to 2 a day at 13:00 and
  19:00 IST once (setting MIG_POSTS) — only when the YouTube budget holds it (quota.capacity(): every channel with an
  active source x posts a day). Clips that already have a publish time keep it; new slots follow the new times.
* Five a day "from today" (owner, 27 Nov 2026): migrate_five() sets the owner's copy to 5 a day (09:00 12:00 15:00
  18:00 21:00 IST) once (setting MIG_FIVE), when the budget holds it, then re-spaces the waiting clips (a day may hold
  up to n videos, counting the ones already on YouTube) and pull_forward() moves videos ALREADY on YouTube (private,
  scheduled) to EARLIER free slots from today on — videos.update, uploads first (quota.can_update / shared_room).
  pull_forward_due runs every hour in copies whose posts-per-day was set by set_posts_per_day ("pull": true).
"""
import json
import logging
import re

from .db import Channel, Destination, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

NEW_LEN = 20
OLD_LENS = (30, 35)  # the lengths new sources got before (db default 30; niche / viral / permission sources 35)
WINDOW = 3.0  # seconds the end may move to reach a pause
MIN_LEN = 8.0
MIG_LEN = "mig_clip_len_20"
MIG_POSTS = "mig_posts_per_day_2"
MIG_FIVE = "mig_posts_per_day_5"
POSTS_KEY = "posts_per_day"  # {"n", "times", "at"}
IST = -330  # JS getTimezoneOffset() of India
TIMES = {1: "18:00", 2: "13:00, 19:00", 3: "10:00, 14:00, 19:00", 4: "10:00, 13:00, 16:00, 19:00",
         5: "09:00, 12:00, 15:00, 18:00, 21:00"}
_PUNCT = (".", "?", "!", "।", "…")


# ---------------------------------------------------------------- a clip that ends in a pause
def _pauses(track: dict) -> list[tuple[float, float]]:
    """(time, strength) of the pauses in a json3 caption track: gaps between spoken words, line ends of manual
    captions, sentence ends."""
    words, lines = [], []
    for ev in (track or {}).get("events") or []:
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        t0, dur = ev["tStartMs"] / 1000, (ev.get("dDurationMs") or 0) / 1000
        texts = [(t0 + (sg.get("tOffsetMs") or 0) / 1000, (sg.get("utf8") or "").strip()) for sg in segs]
        texts = [(t, x) for t, x in texts if x]
        if not texts:
            continue
        words += texts
        if len(texts) == 1 and dur > 0:  # a manual caption line: it ends at t0 + its duration
            lines.append((t0 + dur, texts[-1][1].endswith(_PUNCT)))
    words.sort()
    out = []
    for (a, ta), (b, _tb) in zip(words, words[1:]):
        gap, stop = b - a, ta.endswith(_PUNCT)
        if gap >= 0.9 or (stop and gap >= 0.4):
            out.append((round(b - 0.12, 2), gap + (1.0 if stop else 0.0)))
    starts = [t for t, _x in words]
    for end, stop in lines:
        nxt = next((t for t in starts if t >= end - 0.01), None)
        gap = (nxt - end) if nxt is not None else 2.0
        if gap >= 0.3 or stop:
            out.append((round(end + min(0.15, max(gap, 0) / 2), 2), gap + (1.0 if stop else 0.0)))
    return out


def natural_end(track: dict | None, start: float, end: float, duration: float | None = None,
                window: float = WINDOW) -> float | None:
    """A better end for a clip [start, end]: the strongest pause in the speech within +-window seconds of `end`
    (ties: the one closest to `end`). None = no caption data or no pause there (the planned end stays)."""
    if not track:
        return None
    try:
        cands = [(t, w) for t, w in _pauses(track) if abs(t - end) <= window and t - start >= MIN_LEN
                 and (not duration or t <= float(duration))]
    except Exception:  # noqa: BLE001  (odd caption data never stops a clip)
        log.exception("natural end")
        return None
    if not cands:
        return None
    t, _w = max(cands, key=lambda x: (round(x[1], 1), -abs(x[0] - end)))
    return round(t, 2) if abs(t - end) >= 0.05 else None


# ---------------------------------------------------------------- public posts a day
def active_channels(s) -> list[int]:
    """Channels (Destination ids) that have at least one active source other than viral picks."""
    ids = {d for (d,) in s.query(Channel.destination_id).filter(Channel.auto.is_(True), Channel.destination_id.isnot(None),
                                                                (Channel.kind.is_(None)) | (Channel.kind != "viral"))}
    real = {d for (d,) in s.query(Destination.id)}
    return sorted(ids & real)


def fits_budget(n: int, s=None) -> tuple[bool, str]:
    from . import quota

    own = s is None
    s = s or SessionLocal()
    try:
        chans = max(1, len(active_channels(s)))
    finally:
        if own:
            s.close()
    need, cap = n * chans, quota.capacity()
    if need > cap:
        return False, (f"{n} a day on {chans} channel(s) is {need} uploads a day, but this Google project's YouTube "
                       f"budget holds {cap} a day")
    return True, f"{need} of {cap} uploads a day"


def clean_times(times: str | None, n: int) -> str:
    if not times:
        return TIMES[n]
    parts = [x.strip() for x in str(times).split(",") if x.strip()]
    ok = [p.zfill(5) for p in parts if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", p)]
    if len(ok) != n or len(set(ok)) != n:
        raise ValueError(f"Give exactly {n} different times like 13:00, 19:00 (24-hour clock).")
    return ", ".join(sorted(ok))


def set_posts_per_day(n: int, times: str | None = None, tz_offset: int = IST, check_budget: bool = True) -> dict:
    """Upload times of every source of this copy except viral picks. Clips that already have a time keep it."""
    if n not in TIMES:
        raise ValueError("Public posts per day must be 1 to 5.")
    t = clean_times(times, n)
    with SessionLocal() as s:
        ok, why = fits_budget(n, s)
        if check_budget and not ok:
            raise ValueError(why + ". Nothing was changed.")
        rows = s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "viral")).all()
        for ch in rows:
            ch.post_times, ch.tz_offset = t, int(tz_offset)
        s.commit()
        changed = len(rows)
    # "pull": the hourly pull_forward_due may move videos already on YouTube to earlier free slots of this copy
    set_setting(POSTS_KEY, json.dumps({"n": n, "times": t, "tz_offset": int(tz_offset), "pull": True,
                                       "at": utcnow().isoformat() + "Z"}))
    return {"n": n, "times": t, "tz_offset": int(tz_offset), "sources": changed, "budget": why}


def posts_status() -> dict:
    from . import quota

    try:
        v = json.loads(get_setting(POSTS_KEY) or "{}")
    except (TypeError, ValueError):
        v = {}
    with SessionLocal() as s:
        chans = active_channels(s)
    return {"n": v.get("n"), "times": v.get("times"), "tz_offset": v.get("tz_offset"), "channels": len(chans),
            "capacity": quota.capacity(), "choices": {str(k): t for k, t in TIMES.items()}}


def short_status() -> dict:
    from .hooks import on

    with SessionLocal() as s:
        lens = sorted({c.clip_len for c in s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "reel"))
                       if c.clip_len})
    return {"on": on("clips.short"), "new_len": NEW_LEN, "window": WINDOW, "source_lengths": lens,
            "migrated": bool(get_setting(MIG_LEN))}


# ---------------------------------------------------------------- once, in the owner's copy
def migrate_clip_len() -> int:
    """The owner's sources with the old standard length (30 / 35 s) make 20 s clips from now on. Once."""
    if get_setting(MIG_LEN):
        return 0
    with SessionLocal() as s:
        rows = s.query(Channel).filter(Channel.clip_len.in_(OLD_LENS), (Channel.kind.is_(None)) | (Channel.kind != "reel")).all()
        for ch in rows:
            ch.clip_len = NEW_LEN
        s.commit()
        n = len(rows)
    set_setting(MIG_LEN, json.dumps({"at": utcnow().isoformat() + "Z", "sources": n}))
    if n:
        log.info("clip length: %s source(s) set to %s s", n, NEW_LEN)
    return n


def migrate_posts() -> dict:
    """The owner's copy: 2 public posts a day (13:00 and 19:00 IST), once — only when the YouTube budget holds it.
    When it does not, nothing changes and the next start tries again."""
    if get_setting(MIG_POSTS) or get_setting(MIG_FIVE):
        return {"done": False, "why": "already done"}
    with SessionLocal() as s:
        has = s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "viral")).count()
    if not has:
        return {"done": False, "why": "no sources yet"}
    ok, why = fits_budget(2)
    if not ok:
        log.warning("posts a day: not changed (%s)", why)
        return {"done": False, "why": why}
    res = set_posts_per_day(2, TIMES[2], IST)
    try:
        res["respaced"] = respace_waiting()  # two a day from now on, not after the clips already timed
    except Exception:  # noqa: BLE001
        log.exception("posts a day: re-spacing the waiting clips failed")
    set_setting(MIG_POSTS, json.dumps({"at": utcnow().isoformat() + "Z", **res}))
    return {"done": True, **res}


def migrate_five(background: bool = True) -> dict:
    """The owner (27 Nov 2026): "from today, 5 videos must go public every day". Once: 5 a day at 09:00 12:00 15:00
    18:00 21:00 IST — only when the YouTube budget holds it (else nothing changes and the next start tries again) —,
    the waiting clips re-spaced, and one pull_forward run (in its own thread at start-up: YouTube calls take a while;
    the hourly pull_forward_due goes on from there)."""
    if get_setting(MIG_FIVE):
        return {"done": False, "why": "already done"}
    with SessionLocal() as s:
        has = s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "viral")).count()
    if not has:
        return {"done": False, "why": "no sources yet"}
    ok, why = fits_budget(5)
    if not ok:
        log.warning("five a day: not changed (%s)", why)
        return {"done": False, "why": why}
    res = set_posts_per_day(5, TIMES[5], IST)
    try:  # clips due in the next 48 h keep their time (they are being made / uploaded); the line after closes up
        res["respaced"] = respace_waiting(ahead=True)
    except Exception:  # noqa: BLE001
        log.exception("five a day: re-spacing the waiting clips failed")
    set_setting(MIG_FIVE, json.dumps({"at": utcnow().isoformat() + "Z", **res}, default=str))
    if background:
        pull_forward_soon()
    else:
        res["pulled"] = pull_forward()
    return {"done": True, **res}


def migrate_owner() -> dict:
    """At start-up, the owner's copy only. Never raises."""
    from . import tenant
    from .hooks import on

    out = {}
    try:
        with tenant.use(tenant.OWNER):
            if on("clips.short"):
                out["clip_len"] = migrate_clip_len()
            out["five"] = migrate_five()  # 5 a day from today (once); when the budget cannot hold it, the 2 a day below
            out["posts"] = migrate_posts()
    except Exception:  # noqa: BLE001
        log.exception("tune-ups at start-up")
    return out


# ---------------------------------------------------------------- waiting clips onto the new daily times, now
WAITING = ("fetching", "rendering", "draft", "rendered", "queued")
FILL_LEAD_H = 48  # the first moved slot is at least this far away (the clip still has to be made / uploaded)  # holds a slot, not on YouTube yet


def respace_waiting(dry: bool = False, times: str | None = None, tz: int | None = None, ahead: bool = False) -> dict:
    """Owner (26 Nov 2026): two a day must start now, not after the ~260 clips already timed at one a day.
    Every clip that holds a future slot but is NOT on YouTube yet gets the next free slot of the new daily times, per
    channel, in its current order, from two days ahead. Videos already on YouTube keep their time (changing it costs a
    YouTube update each) and count toward their day: a day holds up to n videos (27 Nov 2026: five a day), never two
    at the same time. Same order, same clips; a second run changes nothing.
    ahead=True (the hourly pull_forward_due): clips due within the next FILL_LEAD_H hours are left where they are
    (they count toward their day) — only the line further ahead closes up."""
    from collections import Counter
    from datetime import timedelta

    from sqlalchemy import update

    from .autopilot import SLOT_LOCK, _slot_from_times, fit_capacity
    from .db import Clip, Project

    try:
        v = json.loads(get_setting(POSTS_KEY) or "{}")
    except (TypeError, ValueError):
        v = {}
    times = times or v.get("times")
    tz = int(tz if tz is not None else (v.get("tz_offset") if v.get("tz_offset") is not None else IST))
    if not times:
        return {"moved": 0, "why": "posts per day not set"}
    n = len([x for x in str(times).split(",") if x.strip()])  # videos a day may hold
    now = utcnow()
    out = {"moved": 0, "first": None, "last": None, "channels": 0, "dry": dry}
    with SLOT_LOCK, SessionLocal() as s:
        viral = {i for (i,) in s.query(Channel.id).filter(Channel.kind == "viral")}
        dests = [d for (d,) in s.query(Clip.destination_id).filter(
            Clip.status.in_(WAITING), Clip.youtube_id.is_(None), Clip.publish_at.isnot(None), Clip.publish_at > now)
            .distinct()]
        for dest in dests:
            rows = (s.query(Clip).filter(Clip.destination_id == dest, Clip.status.in_(WAITING), Clip.youtube_id.is_(None),
                                         Clip.publish_at.isnot(None), Clip.publish_at > now)
                    .order_by(Clip.publish_at, Clip.id).all())
            keep = set()
            for c in rows:  # these follow their own times (viral picks, the PC folder's 18:00, own-topic best hours)
                p = s.get(Project, c.project_id)
                if (c.kw or {}).get("basis") or (p is not None and p.channel_ref in viral):  # PC-folder clips follow it too
                    keep.add(c.id)
                elif ahead and c.publish_at < now + timedelta(hours=FILL_LEAD_H):
                    keep.add(c.id)
            rows = [c for c in rows if c.id not in keep]
            if not rows:
                continue
            # videos already waiting on YouTube keep their time (moving one costs a YouTube update: pull_forward does
            # that, only earlier). A day may hold up to n videos: the ones on YouTube that day (and the clips that keep
            # their own times) count, and no clip goes at the exact time of one of them. Days are filled from two days
            # ahead (time for the clip to be made and uploaded) — also a day freed when a video was kept private
            day = lambda t: (t - timedelta(minutes=tz)).date()  # noqa: E731  (the IST day of a naive-UTC time)
            held = [t for (t,) in s.query(Clip.publish_at).filter(
                Clip.destination_id == dest, Clip.youtube_id.isnot(None), Clip.kept_private.isnot(True),
                Clip.removed_at.is_(None), Clip.publish_at.isnot(None), Clip.publish_at > now,
                Clip.yt_state.is_(None) | Clip.yt_state.in_([x for x in FINE_STATES if x is not None]))]
            held += [c.publish_at for c in s.query(Clip).filter(Clip.id.in_(keep))] if keep else []
            per_day, taken = Counter(day(t) for t in held), set(held)
            base = now + timedelta(hours=FILL_LEAD_H)
            plan, old = [], {c.id: c.publish_at for c in rows}
            for c in rows:  # free their slots first so the daily budget check only counts what is placed
                c.publish_at = None
            s.flush()
            prev = base
            for c in rows:
                slot = _slot_from_times(prev, times, tz) or (prev + timedelta(hours=12))
                for _ in range(4000):
                    if per_day[day(slot)] < n and slot not in taken:
                        fit = fit_capacity(s, slot, times, tz, skip_id=c.id)  # the shared daily upload budget
                        if fit == slot:
                            break
                        slot = fit  # the next day's first time: checked again
                        continue
                    slot = _slot_from_times(slot, times, tz) or (slot + timedelta(hours=12))
                c.publish_at = slot
                s.flush()
                per_day[day(slot)] += 1
                taken.add(slot)
                plan.append((c.id, c.status, old[c.id], slot))
                prev = slot
            s.rollback()  # nothing is kept from the planning pass; the real change is one guarded update per clip
            moved = 0
            if not dry:
                for cid, status, was, slot in plan:
                    if was == slot:
                        continue
                    r = s.execute(update(Clip).where(Clip.id == cid, Clip.status == status, Clip.youtube_id.is_(None),
                                                     Clip.publish_at == was).values(publish_at=slot))
                    moved += r.rowcount
                s.commit()
            else:
                moved = sum(1 for _c, _st, was, slot in plan if was != slot)
            out["moved"] += moved
            out["channels"] += 1
            first, last = plan[0][3], plan[-1][3]
            out["first"] = min(filter(None, [out["first"], first])) if out["first"] else first
            out["last"] = max(filter(None, [out["last"], last])) if out["last"] else last
    for k in ("first", "last"):
        if out[k] is not None:
            out[k] = out[k].isoformat() + "Z"
    if not dry and out["moved"]:
        try:
            from . import acl

            acl.record_here("admin.posts", f"Waiting clips moved to {times} ({out['moved']} clips, until {out['last'][:10]})")
        except Exception:  # noqa: BLE001
            log.exception("posts a day: could not log the re-spacing")
    return out


# ---------------------------------------------------------------- videos already on YouTube: earlier, from today
PULL_PER_RUN = 20  # videos moved per run at most (each: 1 + 50 quota units, after every upload that is due)
PULL_MIN_AHEAD = 20  # minutes: the earliest slot a video may be moved to
FINE_STATES = (None, "", "ok", "private", "scheduled")  # last seen on YouTube: still there (not removed / rejected)


def _posts() -> dict:
    try:
        v = json.loads(get_setting(POSTS_KEY) or "{}")
    except (TypeError, ValueError):
        v = {}
    return v if isinstance(v, dict) else {}


def _pull_plan(s, dest: int, n: int, times: str, tz: int, now, limit: int, viral: set) -> list:
    """[(clip id, youtube id, old time, new time)]: the latest scheduled videos of this channel go to the earliest free
    slots from now + PULL_MIN_AHEAD on. A day holds up to n videos (everything that holds a slot that day counts, the
    ones already public today too), never two at the same time, and a video only ever moves to an EARLIER day.
    Taking them from the end moves as few videos as possible (one YouTube update fills one free slot)."""
    from collections import Counter
    from datetime import timedelta

    from .autopilot import HOLDS_SLOT, _slot_from_times
    from .db import Clip, Project

    day = lambda t: (t - timedelta(minutes=tz)).date()  # noqa: E731  (the local day of a naive-UTC time)
    local = now - timedelta(minutes=tz)
    day0 = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=tz)  # today 00:00 local, in UTC
    rows = s.query(Clip).filter(Clip.destination_id == dest, Clip.status.in_(HOLDS_SLOT), Clip.kept_private.isnot(True),
                                Clip.removed_at.is_(None), Clip.publish_at.isnot(None), Clip.publish_at >= day0).all()
    rows = [c for c in rows if c.yt_state in FINE_STATES]
    per_day, taken = Counter(day(c.publish_at) for c in rows), {c.publish_at for c in rows}
    start = now + timedelta(minutes=PULL_MIN_AHEAD)

    def movable(c) -> bool:
        if c.status != "uploaded" or not c.youtube_id or c.publish_at <= start:
            return False
        k = c.kw or {}
        if k.get("basis") or k.get("pull_skip"):  # own-topic best hours / YouTube showed it is not movable
            return False
        p = s.get(Project, c.project_id)
        return not (p is not None and p.channel_ref in viral)  # viral picks keep their own times

    cands = sorted((c for c in rows if movable(c)), key=lambda c: (c.publish_at, c.id), reverse=True)
    plan = []
    for c in cands:
        if len(plan) >= limit:
            break
        slot, found = _slot_from_times(start, times, tz), None
        while slot is not None and day(slot) < day(c.publish_at):
            if per_day[day(slot)] < n and slot not in taken:
                found = slot
                break
            slot = _slot_from_times(slot, times, tz)
        if found is None:
            break  # no free slot on an earlier day for the latest video: none for the earlier ones either
        plan.append((c.id, c.youtube_id, c.publish_at, found))
        per_day[day(found)] += 1
        per_day[day(c.publish_at)] -= 1
        taken.add(found)
        taken.discard(c.publish_at)
    return plan


def pull_forward(n: int | None = None, limit: int = PULL_PER_RUN, dry: bool = False) -> dict:
    """Owner (27 Nov 2026): "from today, 5 videos must go public every day". Videos ALREADY on YouTube (private with a
    future publish time; not kept private, not removed, not viral picks) move to EARLIER free slots of the daily times,
    from the first one 20 minutes ahead on, so every day from today holds up to n videos (_pull_plan). Each move is one
    youtube.set_publish_time (1 + 50 units), only while today's budget still holds every upload that is due
    (quota.can_update) and, on the owner's Google project, the owner's uploads (quota.shared_room). The clip's time in
    the database changes only after YouTube accepted it. Stops on YouTube's quota error; an older login skips its
    channel. At most `limit` moves a run."""
    from datetime import timedelta

    from sqlalchemy import update

    from . import quota, youtube
    from .autopilot import HOLDS_SLOT, SLOT_LOCK
    from .comments import _uploads_due_here
    from .db import Clip

    v = _posts()
    times = v.get("times")
    tz = int(v.get("tz_offset") if v.get("tz_offset") is not None else IST)
    n = int(n or v.get("n") or (len(str(times).split(",")) if times else 0))
    out = {"moved": 0, "planned": 0, "skipped": 0, "failed": 0, "stopped": "", "dry": dry, "moves": []}
    if not times or n < 1:
        out["stopped"] = "posts per day not set"
        return out
    cost = quota.COST["update"] + quota.COST["read"]
    day = lambda t: (t - timedelta(minutes=tz)).date()  # noqa: E731
    halt, bad_dest = False, set()
    for _round in range(3):  # again (at most twice) only when YouTube showed some planned videos cannot move
        now = utcnow()
        with SLOT_LOCK, SessionLocal() as s:
            viral = {i for (i,) in s.query(Channel.id).filter(Channel.kind == "viral")}
            dests = sorted({d for (d,) in s.query(Clip.destination_id).filter(
                Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.kept_private.isnot(True),
                Clip.removed_at.is_(None), Clip.publish_at > now).distinct() if d is not None and d not in bad_dest})
            plans = {d: _pull_plan(s, d, n, times, tz, now, limit - out["moved"], viral) for d in dests}
        out["planned"] += sum(len(p) for p in plans.values())
        if dry:
            out["moves"] = [{"clip": cid, "from": a.isoformat() + "Z", "to": b.isoformat() + "Z"}
                            for p in plans.values() for cid, _v, a, b in p]
            return out
        marked = 0
        for dest, plan in plans.items():
            if halt:
                break
            for cid, vid, was, slot in plan:
                if out["moved"] >= limit or out["failed"] >= 2:
                    halt = True
                    break
                if not quota.can_update(_uploads_due_here()) or not quota.shared_room(cost):
                    out["stopped"], halt = "today's YouTube budget is kept for uploads", True
                    break
                with SLOT_LOCK:  # no other slot hand-out between the check, YouTube and the database
                    with SessionLocal() as s:  # still the same video at the same time, and the slot still free?
                        c = s.get(Clip, cid)
                        if (c is None or c.status != "uploaded" or c.youtube_id != vid or c.publish_at != was
                                or c.kept_private or c.removed_at is not None):
                            out["skipped"] += 1
                            continue
                        others = [t for (t,) in s.query(Clip.publish_at).filter(
                            Clip.destination_id == dest, Clip.id != cid, Clip.status.in_(HOLDS_SLOT),
                            Clip.kept_private.isnot(True), Clip.removed_at.is_(None),
                            Clip.publish_at.between(slot - timedelta(hours=30), slot + timedelta(hours=30)))]
                        if slot in others or sum(1 for t in others if day(t) == day(slot)) >= n:
                            out["skipped"] += 1
                            continue
                    try:
                        r = youtube.set_publish_time(dest, vid, slot)
                    except youtube.NeedsReconnect:
                        out["stopped"] = f"channel {dest}: connect it again (older login)"
                        bad_dest.add(dest)
                        break  # this channel's other videos wait until it is reconnected; other channels go on
                    except youtube.NotScheduled as e:
                        _pull_skip(cid, str(e))
                        out["skipped"] += 1
                        marked += 1
                        continue
                    except Exception as e:  # noqa: BLE001
                        m = str(e)
                        if "quotaExceeded" in m or "dailyLimitExceeded" in m:
                            quota.exhaust()
                            out["stopped"], halt = "YouTube's daily budget is used up", True
                            break
                        log.warning("pull forward clip %s: %s", cid, m[:200])
                        out["failed"] += 1  # a refused update costs quota too
                        continue
                    if not r.get("changed"):
                        _pull_skip(cid, "YouTube's publish time is already earlier")
                        out["skipped"] += 1
                        marked += 1
                        continue
                    with SessionLocal() as s:  # YouTube accepted: now the database
                        s.execute(update(Clip).where(Clip.id == cid, Clip.youtube_id == vid, Clip.publish_at == was)
                                  .values(publish_at=slot))
                        s.commit()
                out["moved"] += 1
                out["moves"].append({"clip": cid, "from": was.isoformat() + "Z", "to": slot.isoformat() + "Z"})
        if halt or not marked or out["moved"] >= limit:
            break
    if out["moved"]:
        try:
            from . import acl

            first = min(m["to"] for m in out["moves"])
            acl.record_here("admin.posts", f"{out['moved']} scheduled video(s) moved earlier to fill {n} a day "
                                           f"(from {first[:16].replace('T', ' ')} UTC)")
        except Exception:  # noqa: BLE001
            log.exception("pull forward: could not log the moves")
        log.info("pull forward: %s video(s) moved earlier", out["moved"])
    return out


def _pull_skip(cid: int, why: str):
    """This video is not moved again (YouTube shows it public / unscheduled, or its time already earlier)."""
    from .db import Clip

    with SessionLocal() as s:
        c = s.get(Clip, cid)
        if c is not None:
            k = dict(c.kw or {})
            k["pull_skip"] = why[:120]
            c.kw = k
            s.commit()


def pull_forward_due() -> dict:
    """Timed job (every hour, every copy): only where posts-per-day was set by set_posts_per_day ("pull": true).
    A copy that never chose it, or chose it before this existed, is left alone."""
    if not _posts().get("pull"):
        return {"moved": 0, "stopped": "not set for this copy"}
    try:  # free (database only): waiting clips further ahead close up into days freed by moves / parked clips
        respace_waiting(ahead=True)
    except Exception:  # noqa: BLE001
        log.exception("pull forward: re-spacing the waiting clips failed")
    return pull_forward()


def pull_forward_soon():
    """One pull_forward run in its own thread, in the copy that is running now (YouTube calls take a while: a page
    request or the start-up never waits for them)."""
    import threading

    from . import tenant

    tid = tenant.current()

    def run():
        try:
            with tenant.use(tid):
                log.info("pull forward: %s", pull_forward())
        except Exception:  # noqa: BLE001
            log.exception("pull forward failed")
    threading.Thread(target=run, name="pull-forward", daemon=True).start()

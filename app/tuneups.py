"""Shorter clips and more public posts a day (Nov 2026: 35 s clips, one post a day at 18:00 IST got few views).

* Clip length: new sources make 20 s clips (db.Channel.clip_len default). migrate_owner() sets the owner's existing
  sources that still have the old standard lengths (30 / 35 s) to 20 s, once (setting MIG_LEN). Clips already made,
  queued or on YouTube are never made again.
* Natural end (switch "clips.short"): when the source video's captions are at hand (the helper PC's whole caption
  track, or the track fetched for captions anyway), a new clip ends in a pause of the speech within +-3 s of its
  planned end instead of in the middle of a word (natural_end). Without captions the planned end stays.
* Public posts per day (Super Admin: POST /api/admin/posts-per-day, 1-3): the upload times of the copy's sources
  (except viral picks, which keep their own times). migrate_owner() sets the owner's copy to 2 a day at 13:00 and
  19:00 IST once (setting MIG_POSTS) — only when the YouTube budget holds it (quota.capacity(): every channel with an
  active source x posts a day). Clips that already have a publish time keep it; new slots follow the new times.
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
POSTS_KEY = "posts_per_day"  # {"n", "times", "at"}
IST = -330  # JS getTimezoneOffset() of India
TIMES = {1: "18:00", 2: "13:00, 19:00", 3: "10:00, 14:00, 19:00"}
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
        raise ValueError("Public posts per day must be 1, 2 or 3.")
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
    set_setting(POSTS_KEY, json.dumps({"n": n, "times": t, "tz_offset": int(tz_offset), "at": utcnow().isoformat() + "Z"}))
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
    if get_setting(MIG_POSTS):
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


def migrate_owner() -> dict:
    """At start-up, the owner's copy only. Never raises."""
    from . import tenant
    from .hooks import on

    out = {}
    try:
        with tenant.use(tenant.OWNER):
            if on("clips.short"):
                out["clip_len"] = migrate_clip_len()
            out["posts"] = migrate_posts()
    except Exception:  # noqa: BLE001
        log.exception("tune-ups at start-up")
    return out


# ---------------------------------------------------------------- waiting clips onto the new daily times, now
WAITING = ("fetching", "rendering", "draft", "rendered", "queued")  # holds a slot, not on YouTube yet


def respace_waiting(dry: bool = False, times: str | None = None, tz: int | None = None) -> dict:
    """Owner (26 Nov 2026): two a day must start now, not after the ~260 clips already timed at one a day.
    Every clip that holds a future slot but is NOT on YouTube yet gets the next slot of the new daily times, per
    channel, in its current order, starting after the last video of that channel already on YouTube (those keep
    their time: changing it costs a YouTube update each). Same order, same clips; a second run changes nothing."""
    from datetime import timedelta

    from sqlalchemy import func, update

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
                if c.pc_item_id or (c.kw or {}).get("basis") or (p is not None and p.channel_ref in viral):
                    keep.add(c.id)
            rows = [c for c in rows if c.id not in keep]
            if not rows:
                continue
            on_yt = (s.query(func.max(Clip.publish_at)).filter(Clip.destination_id == dest, Clip.youtube_id.isnot(None))
                     .scalar())
            base = max(now + timedelta(hours=2), on_yt or now)
            plan, old = [], {c.id: c.publish_at for c in rows}
            for c in rows:  # free their slots first so the daily budget check only counts what is placed
                c.publish_at = None
            s.flush()
            prev = base
            for c in rows:
                slot = _slot_from_times(prev, times, tz) or (prev + timedelta(hours=12))
                slot = fit_capacity(s, slot, times, tz, skip_id=c.id)
                c.publish_at = slot
                s.flush()
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

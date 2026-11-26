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

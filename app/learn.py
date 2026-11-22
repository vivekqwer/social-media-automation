"""What works on the channel, and making more of it ("Analytics se topics").

* model()      pure: views per day of every uploaded clip older than 2 days, compared with its channel's usual
               (median), grouped by source, word, clip length, upload hour and picture layout. A group needs at
               least MIN_N videos before anything is said or done about it.
* insights()   the Ideas page card: plain sentences with the numbers behind them, topic ideas, auto-tune state.
* daily()      once a day per account (auto-tune, on unless switched off): winning sources make +1 video per run
               (at most twice their own setting, never above the account's limits), weak ones -1 (never below 1).
               A source is never paused, nothing is deleted. Each source's own setting is remembered, so
               "Undo auto-tune" puts every source back. Winning words get a small boost in the keyword helper.
Upload hours are only shown as advice: the publish-time spreading (quota, upload times) is not changed here.
"""
import json
import logging
import statistics
from datetime import datetime, timedelta

from . import keywords as kw
from .db import Channel, Clip, Destination, Project, SessionLocal, VideoStat, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

MIN_AGE_H = 48  # a video's first two days say little: it is counted after that
MIN_N = 5  # a group needs this many videos before anything is said or changed
WIN, WEAK = 1.5, 0.6  # median views per day vs the channel's usual
MAX_RUN = 20  # the most videos per run a source may have (same as the Sources form)
STALE_DAYS = 2  # numbers older than this (YouTube not read) -> nothing is changed
LOG_KEEP = 60
LENGTHS = [(20, "up to 20 s"), (40, "20–40 s"), (60, "40–60 s"), (10 ** 9, "over 1 min")]
LAYOUT = {"full": "whole picture (blurred sides)", "wide": "wide window", "fill": "filled (cropped)", "16:9": "wide 16:9 video"}
IST = -330  # JS getTimezoneOffset() of India: local = UTC - offset
FILLER = set("shorts short video videos clip clips best moment part full official new latest viral trending india indian "
             "hindi english today day watch".split())

# settings (each account has its own, in its own copy)
K_ON, K_ORIG, K_LOG, K_DAY, K_WORDS = "autotune", "autotune_orig", "autotune_log", "autotune_day", "autotune_words"
K_HELD = "autotune_held"  # winning sources that were not raised today, and why (limits, full queue)


# ---------------------------------------------------------------- the model (pure: no database, no clock)
def words_of(title: str, tags=None, kw_items=None, skip: str = "") -> set:
    """The topic words of one clip (singular/plural folded), without filler and the credited channel's name."""
    text = " ".join([str(title or "")] + [str(t) for t in (tags or [])]
                    + [str(i.get("k") or "") for i in (kw_items or []) if i.get("used", True)])
    own = kw.stems(skip)
    return {w for w in kw.topic_stems(text) if len(w) >= 3 and not w.isdigit() and w not in FILLER
            and w not in kw.JUNK_TAGS and w not in own}


def length_bucket(seconds: float) -> str:
    for top, name in LENGTHS:
        if (seconds or 0) <= top:
            return name
    return LENGTHS[-1][1]


def _vpd(views, published_at, now) -> float:
    return (views or 0) / max(0.5, (now - published_at).total_seconds() / 86400)


def model(stats: list, clips: list, now: datetime, min_n: int = MIN_N) -> dict:
    """stats: every public video of my channels [{video_id, dest, views, likes, published_at}]
    clips: my uploaded clips [{youtube_id, source_id, source, words, length, tz, layout}]
    Returns per dimension the groups with enough videos: {key, label, n, ratio, vpd, like_rate, verdict}.
    ratio = median over the group of (video's views per day / its channel's median views per day)."""
    cut = now - timedelta(hours=MIN_AGE_H)
    old = [v for v in stats if v.get("published_at") and v["published_at"] <= cut]
    by_dest = {}
    for v in old:
        by_dest.setdefault(v.get("dest"), []).append(_vpd(v.get("views"), v["published_at"], now))
    usual = {d: statistics.median(x) for d, x in by_dest.items() if len(x) >= min_n}
    sv = {v["video_id"]: v for v in old}
    rows = []
    for c in clips:
        v = sv.get(c.get("youtube_id"))
        if not v or not usual.get(v.get("dest")):
            continue
        vpd = _vpd(v.get("views"), v["published_at"], now)
        hour = (v["published_at"] - timedelta(minutes=c.get("tz") if c.get("tz") is not None else IST)).hour
        rows.append({**c, "vpd": vpd, "ratio": vpd / usual[v["dest"]], "hour": hour,
                     "like": (v.get("likes") or 0) / v["views"] if v.get("views") else None})
    out = {"videos": len(rows), "channel_videos": len(old), "needed": min_n, "enough": len(rows) >= min_n,
           "usual": round(statistics.median(list(usual.values())), 1) if usual else None,
           "sources": [], "words": [], "lengths": [], "hours": [], "layouts": []}
    if not out["enough"]:
        return out
    likes = [r["like"] for r in rows if r["like"] is not None]
    like_all = statistics.median(likes) if likes else None

    def group(dim, keyf, label=lambda k: str(k), skip_common=False):
        g = {}
        for r in rows:
            ks = keyf(r)
            for k in (ks if isinstance(ks, (set, list, tuple)) else [ks]):
                if k is not None and k != "":
                    g.setdefault(k, []).append(r)
        res = []
        for k, rs in g.items():
            if len(rs) < min_n or (skip_common and len(rs) > 0.7 * len(rows)):
                continue  # too few to say anything / a word in nearly every title says nothing
            ratio = statistics.median(r["ratio"] for r in rs)
            lk = [r["like"] for r in rs if r["like"] is not None]
            res.append({"key": k, "label": label(k), "n": len(rs), "ratio": round(ratio, 2),
                        "vpd": round(statistics.median(r["vpd"] for r in rs), 1),
                        "like_rate": round(100 * statistics.median(lk), 2) if lk else None,
                        "verdict": "win" if ratio >= WIN else "weak" if ratio <= WEAK else "usual"})
        out[dim] = sorted(res, key=lambda e: (-e["ratio"], -e["n"]))

    names = {r["source_id"]: r.get("source") or f"Source {r['source_id']}" for r in rows if r.get("source_id")}
    group("sources", lambda r: r.get("source_id"), lambda k: names.get(k, str(k)))
    group("words", lambda r: set(r.get("words") or ()), skip_common=True)
    group("lengths", lambda r: length_bucket(r.get("length") or 0))
    group("hours", lambda r: r["hour"], lambda h: f"{h:02d}:00")
    group("layouts", lambda r: r.get("layout"), lambda k: LAYOUT.get(k, k))
    out["like_rate"] = round(100 * like_all, 2) if like_all is not None else None
    return out


# ---------------------------------------------------------------- reading the account's data
def gather(s, now: datetime | None = None) -> tuple[list, list]:
    stats = [{"video_id": v.video_id, "dest": v.destination_id, "views": v.views, "likes": v.likes,
              "published_at": v.published_at}
             for v in s.query(VideoStat).filter(VideoStat.privacy == "public", VideoStat.published_at.isnot(None))]
    chans = {c.id: c for c in s.query(Channel).all()}
    clips = []
    rows = (s.query(Clip, Project).join(Project, Project.id == Clip.project_id)
            .filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.removed_at.is_(None)).all())
    for c, p in rows:
        ch = chans.get(p.channel_ref) if p.channel_ref else None
        layout = "16:9" if c.aspect == "16:9" else (None if p.mode == "original" else (c.fit or "full"))
        clips.append({"youtube_id": c.youtube_id, "source_id": ch.id if ch else None,
                      "source": (ch.name or ch.query) if ch else None,
                      "words": words_of(c.title, c.tags, (c.kw or {}).get("items"), "" if p.mode == "original" else p.channel or ""),
                      "length": max(0.0, float(c.end or 0) - float(c.start or 0)),
                      "tz": ch.tz_offset if ch and ch.tz_offset else None, "layout": layout})
    return stats, clips


def current(now: datetime | None = None) -> dict:
    now = now or utcnow()
    with SessionLocal() as s:
        stats, clips = gather(s, now)
        fresh = s.query(VideoStat.updated_at).order_by(VideoStat.updated_at.desc()).first()
    m = model(stats, clips, now)
    m["stale"] = bool(fresh and fresh[0] and fresh[0] < now - timedelta(days=STALE_DAYS))
    return m


# ---------------------------------------------------------------- settings
def enabled() -> bool:
    return (get_setting(K_ON) or "on") != "off"  # on unless the account switched it off


def _load(key, default):
    try:
        v = json.loads(get_setting(key) or "null")
        return default if v is None else v
    except ValueError:
        return default


def _save(key, value):
    set_setting(key, json.dumps(value, ensure_ascii=False))


def _note(text: str):
    """One line in the card's history and the account's activity log."""
    entries = _load(K_LOG, [])
    entries.append({"at": utcnow().isoformat() + "Z", "text": text})
    _save(K_LOG, entries[-LOG_KEEP:])
    log.info("auto-tune: %s", text)
    try:
        from . import acl

        acl.record_here("Auto-tune", text)
    except Exception:  # noqa: BLE001
        pass


def win_words() -> list:
    """Word stems the keyword helper prefers a little (empty when auto-tune is off)."""
    try:
        return list(_load(K_WORDS, [])) if enabled() else []
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- auto-tune
def _runs_per_day(ch) -> float:
    from .autopilot import INTERVAL

    hours = ch.interval_hours or INTERVAL.total_seconds() / 3600
    return 24 / max(1, hours)


def _per_run(ch, vpr=None) -> int:
    v = ch.videos_per_run if vpr is None else vpr
    return (v or 0) * (1 if ch.kind == "reel" else (ch.clips_per_video or 1))


def _why_not_more(s, ch, sources) -> str:
    """'' when a source may make one more video per run, else the reason in plain words."""
    from . import acl
    from .autopilot import MAX_QUEUED_PER_DEST

    blocked = acl.source_block(ch.kind, bool(ch.auto_scripts))
    if blocked:
        return blocked
    cap = acl.limit("clips_per_day")
    if cap is not None:
        made = sum(_runs_per_day(c) * _per_run(c) for c in sources)
        if made + _runs_per_day(ch) * (_per_run(ch, ch.videos_per_run + 1) - _per_run(ch)) > cap:
            return f"your account's limit of {cap} clips a day"
    waiting = s.query(Clip).filter(Clip.destination_id == ch.destination_id,
                                   Clip.status.in_(["queued", "fetching", "rendering"])).count()
    if waiting >= int(MAX_QUEUED_PER_DEST * 0.8):
        return f"{waiting} clips are already waiting to go up on that channel"
    return ""


def daily(force: bool = False) -> list[str]:
    """The once-a-day step (the timed job calls it every hour; it acts once per day). Returns what it changed."""
    now = utcnow()
    day = now.strftime("%Y-%m-%d")
    if not force and get_setting(K_DAY) == day:
        return []
    set_setting(K_DAY, day)
    if not enabled():
        return []
    _save(K_HELD, [])
    from . import acl

    m = current(now)
    if not m["enough"] or m["stale"]:
        _save(K_WORDS, [])
        return []
    _save(K_WORDS, [w["key"] for w in m["words"] if w["verdict"] == "win"][:10])
    _research_topics([w["key"] for w in m["words"] if w["verdict"] == "win"][:3])
    if not acl.feature("sources.edit"):
        return []  # changing sources is switched off for this account: nothing is changed for it either
    verdict = {e["key"]: e for e in m["sources"]}
    orig = {str(k): v for k, v in _load(K_ORIG, {}).items()}
    done, held = [], {}
    with SessionLocal() as s:
        sources = [c for c in s.query(Channel).filter(Channel.auto.is_(True)).all() if c.kind != "viral"]
        live = {str(c.id) for c in s.query(Channel.id)}
        for k in [k for k in orig if k not in live]:
            orig.pop(k)  # the source was deleted
        for ch in sources:
            rec = orig.get(str(ch.id))
            if rec and ch.videos_per_run != rec.get("set"):
                orig.pop(str(ch.id))  # the owner changed it by hand since: his number is the new own setting
                rec = None
            base = rec["orig"] if rec else (ch.videos_per_run or 1)
            e = verdict.get(ch.id)
            if not e:
                continue  # not enough videos of this source yet
            cur, new, why = ch.videos_per_run or 1, None, ""
            name = ch.name or ch.query or f"source {ch.id}"
            if e["verdict"] == "win":
                top = min(MAX_RUN, 2 * base)
                if cur < top:
                    stop = _why_not_more(s, ch, sources)
                    if stop:
                        held[str(ch.id)] = f"“{name}” does well but was not raised: {stop}."
                        continue
                    new = cur + 1
                    why = f"its clips get {e['ratio']:g}× your usual views per day ({e['n']} videos)"
            elif e["verdict"] == "weak" and cur > 1:
                new = cur - 1
                why = f"its clips get only {e['ratio']:g}× your usual views per day ({e['n']} videos)"
            elif e["verdict"] == "usual" and cur != base:  # back to normal: one step towards its own setting
                new = cur + (1 if cur < base else -1)
                why = f"its clips are back to your usual views ({e['ratio']:g}×)"
            if new is None or new == cur:
                continue
            ch.videos_per_run = new
            if new == base:
                orig.pop(str(ch.id), None)
            else:
                orig[str(ch.id)] = {"orig": base, "set": new, "name": name}
            text = f"{'+1' if new > cur else '−1'} video per run for “{name}” ({cur} → {new}) because {why}."
            done.append(text)
        s.commit()
    _save(K_ORIG, orig)
    _save(K_HELD, list(held.values()))
    for t in done:
        _note(t)
    return done


def undo() -> dict:
    """Every source back to its own setting; auto-tune switched off (so it does not change them again tomorrow)."""
    orig = _load(K_ORIG, {})
    back = []
    with SessionLocal() as s:
        for k, rec in orig.items():
            ch = s.get(Channel, int(k))
            if ch and ch.videos_per_run == rec.get("set"):  # a number changed by hand since is left alone
                back.append(f"“{rec.get('name') or ch.name}” {ch.videos_per_run} → {rec['orig']}")
                ch.videos_per_run = rec["orig"]
        s.commit()
    _save(K_ORIG, {})
    set_setting(K_ON, "off")
    _save(K_WORDS, [])
    _note("Undo: auto-tune switched off" + (" and every source put back to its own setting (" + "; ".join(back) + ")."
                                            if back else "; no source had been changed."))
    return {"restored": len(back), "on": False}


def switch(on: bool) -> dict:
    set_setting(K_ON, "on" if on else "off")
    if not on:
        _save(K_WORDS, [])
    _note("Auto-tune switched " + ("on." if on else "off. Sources keep their current numbers (press Undo to put them back)."))
    return {"on": on}


# ---------------------------------------------------------------- topic ideas
def _research_topics(words: list):
    """Daily: what people type about the best words (cached a day, so the card costs nothing). Never raises."""
    for w in words:
        try:
            kw.research(w, "clip")
        except Exception:  # noqa: BLE001
            pass


def topics(m: dict, limit: int = 10) -> list[dict]:
    """5-10 topic ideas from the winning words: what people type (saved suggestions), today's trending lists,
    and the winning words together. No network request here."""
    from .autopilot import trend_phrase

    good = [w for w in m.get("words") or [] if w["verdict"] == "win"][:6] or \
        [w for w in m.get("words") or [] if w["ratio"] >= 1.1][:4]
    seen, out = kw._Seen(), []

    def add(phrase, why, shortest=2):
        phrase = kw._norm(phrase)
        if phrase and shortest <= len(phrase.split()) <= 6 and len(phrase) <= 60 and not kw._bad(phrase, set()) \
                and len(out) < limit and seen.add(phrase):
            out.append({"topic": phrase, "why": why})

    trend = []
    try:
        trend = kw.trending_now()["items"]
    except Exception:  # noqa: BLE001
        pass
    for w in good:
        hit = kw._cache_get(w["key"], "en")
        for p in (hit[0] if hit else [])[:3]:
            if w["key"] in kw.topic_stems(p):
                add(p, f"people search this · “{w['key']}” does {w['ratio']:g}× on your channel")
        for it in trend:
            if w["key"] in {kw._stem(t) for t in it["words"]}:
                add(trend_phrase(it["title"], it["tags"]), f"trending now · “{w['key']}” does {w['ratio']:g}× on your channel")
                break
    for w in good:  # nothing saved about them yet (the daily check looks them up): the word itself
        if len(out) < 5:
            add(w["key"], f"your clips about it do {w['ratio']:g}× your usual", 1)
    return out


# ---------------------------------------------------------------- the card
def _say(m: dict, on: bool, changed: dict) -> list[str]:
    lines = []
    for e in m["sources"]:
        if e["verdict"] == "win":
            act = " — making more of them" if on and str(e["key"]) in changed else " — worth making more of them" if not on else ""
            lines.append(f"Clips from “{e['label']}” get {e['ratio']:g}× more views than your usual{act}.")
        elif e["verdict"] == "weak":
            act = " — making fewer of them" if on and str(e["key"]) in changed else ""
            lines.append(f"Clips from “{e['label']}” get only {e['ratio']:g}× your usual views{act}.")
    best = [w for w in m["words"] if w["verdict"] == "win"][:5]
    if best:
        lines.append("Titles with " + ", ".join(f"“{w['key']}” ({w['ratio']:g}×)" for w in best) + " do best"
                     + (" — the keyword helper now prefers them." if on else "."))
    for dim, what in (("lengths", "Best clip length"), ("hours", "Best upload hour"), ("layouts", "Best Shorts picture")):
        top = m[dim][0] if m[dim] else None
        if top and top["ratio"] >= 1.2 and len(m[dim]) > 1:
            lines.append(f"{what}: {top['label']} ({top['ratio']:g}× your usual, {top['n']} videos).")
    return lines


def insights() -> dict:
    m = current()
    on = enabled()
    orig = _load(K_ORIG, {})
    with SessionLocal() as s:
        names = {c.id: (c.name or c.query or f"Source {c.id}") for c in s.query(Channel)}
        now_vpr = {c.id: c.videos_per_run for c in s.query(Channel)}
        times = " ".join(c.post_times or "" for c in s.query(Channel).filter(Channel.auto.is_(True)))
    changed = {k: {"source": names.get(int(k), v.get("name")), "orig": v["orig"], "now": now_vpr.get(int(k))}
               for k, v in orig.items() if int(k) in names}
    hour_tip = ""
    if m.get("hours") and m["hours"][0]["ratio"] >= 1.2 and len(m["hours"]) > 1 and m["hours"][0]["label"] not in times:
        hour_tip = (f"None of your sources uploads at {m['hours'][0]['label']} yet. To use it: Sources → Edit → Upload times. "
                    "(Upload times are never changed by themselves.)")
    for e in m["sources"]:
        e["key"] = str(e["key"])
    return {**m, "lines": _say(m, on, changed) if m["enough"] else [], "hour_tip": hour_tip,
            "topics": topics(m) if m["enough"] else [],
            "autotune": {"on": on, "changed": list(changed.values()), "log": _load(K_LOG, [])[-12:][::-1],
                         "held": _load(K_HELD, []) if on else [], "last_run": get_setting(K_DAY)}}

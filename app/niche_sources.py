"""Pick your niche -> sources made for you (Get started step 7, and Sources -> "Change my niche").

Each phrase of a picked niche (app/niches.py), or of a topic the user typed, becomes one ordinary topic source of this
account: Creative Commons only, running by itself. A niche with a trending category also gets one "Trending (CC clips)"
source. They are normal sources in every way (Run, Edit, auto-tune in app/learn.py, limits in app/acl.py); they only
carry `Channel.niche`, so a niche can be removed again as one: its sources are switched off (never deleted) and every
clip they made stays.

How much they make: every pick and every Remove re-plans ALL of the account's niche sources together (rebalance):
* at most MAX_NICHES niches / typed topics are on at once;
* at most budget() niche sources are on (2 × the clips a day aimed at: TARGET_PER_DAY, or the account's clips-a-day
  limit when lower); the niches' first phrases come first, the rest wait switched off (or are not made yet);
* every niche source that is on runs every 24 × on / per_day hours (6 to 48), so together they make about per_day
  clips a day. 48 h stays the slowest (the Edit form's limit, and a slower source looks broken) — the budget is what
  keeps the total down.

Everything here works in the copy of the signed-in account (tenant): sources are never made anywhere else.
"""
import math

from . import acl, niches
from .db import Channel, Project, SessionLocal, main_destination

DEFAULT_TIMES = "10:00, 18:00"
IST = -330  # JS getTimezoneOffset() of India
TARGET_PER_DAY = 6  # clips a day all niche sources together aim for (about one Google project's daily uploads)
MAX_HOURS = 48  # slowest run interval (main.ChannelEdit allows 1-48)
MAX_NICHES = 4  # niches + typed topics switched on at once
MAX_KEY = 40
PER_RUN = {"videos_per_run": 1, "clips_per_video": 1, "clip_len": 35, "aspect": "9:16", "gap_hours": 12, "captions": True}
TRENDS = {"26": "food & how-to", "22": "people & blogs", "24": "entertainment", "23": "comedy", "20": "gaming",
          "17": "sports", "28": "science & tech"}  # the same names as main.TREND_CATS
HONEST = ("Videos are only taken from Creative Commons videos (their owners allow reuse) or from creators who gave you "
          "permission. Some subjects have few such videos.")


class NicheError(ValueError):
    pass


def _label(key: str) -> str:
    if key in niches.BY_KEY:
        return niches.BY_KEY[key]["label"]
    return key.split(":", 1)[-1].replace("-", " ") if key.startswith("custom:") else key


def _times(s, tz_offset: int | None) -> tuple[str, int]:
    """The account's upload times: those of its newest source that has some, else 10:00 and 18:00 on the user's clock."""
    ch = (s.query(Channel).filter(Channel.post_times.isnot(None), Channel.post_times != "",
                                  (Channel.kind.is_(None)) | (Channel.kind != "viral"))
          .order_by(Channel.id.desc()).first())
    if ch:
        return ch.post_times, ch.tz_offset or 0
    return DEFAULT_TIMES, IST if tz_offset is None else int(tz_offset)


def per_day() -> int:
    """Clips a day all niche sources of this account aim for together."""
    cap = acl.limit("clips_per_day")
    return max(1, min(cap, TARGET_PER_DAY) if cap else TARGET_PER_DAY)


def budget() -> int:
    """Niche sources that may be on at once: each runs at most every MAX_HOURS, so this many make about per_day()."""
    return max(1, MAX_HOURS * per_day() // 24)


def _interval(n_on: int) -> int:
    return min(MAX_HOURS, max(6, math.ceil(24 * max(1, n_on) / per_day())))


def _interleave(groups: list[list[tuple]]) -> list[tuple]:
    """First entry of every group, then the second of every group, ... (no (kind, query) twice)."""
    out, seen = [], set()
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i < len(g) and (g[i][1], g[i][2].lower()) not in seen:
                seen.add((g[i][1], g[i][2].lower()))
                out.append(g[i])
    return out


def wanted(keys: list[str], topic: str = "") -> list[tuple[str, str, str]]:
    """(niche key, kind, query) of every source the picks ask for: the niches' first phrases first; a niche's trending
    source after its phrases."""
    groups = []
    for k in keys:
        n = niches.BY_KEY[k]
        groups.append([(k, "topic", p) for p in n["phrases"]] + ([(k, "trending", n["trending"])] if n.get("trending") else []))
    if niches.clean_topic(topic):
        groups.append([(niches.custom_key(topic), "topic", p) for p in niches.phrases_for_topic(topic)])
    return _interleave(groups)


def _active(s) -> list[str]:
    """Niches of this account with at least one source on, oldest first."""
    out = []
    for c in s.query(Channel).filter(Channel.niche.isnot(None), Channel.auto.is_(True)).order_by(Channel.id):
        if c.niche not in out:
            out.append(c.niche)
    return out


def _plan(s, keys: list[str], topics: dict, trending_ok: bool) -> list[tuple[str, str, str]]:
    """Every source the active niches ask for, in the order they get a place (first phrases first)."""
    groups = []
    for k in keys:
        if k in niches.BY_KEY:
            n = niches.BY_KEY[k]
            g = [(k, "topic", p) for p in n["phrases"]]
            if n.get("trending") and trending_ok:
                g.append((k, "trending", n["trending"]))
        else:  # a typed topic: the sources it already has, or the phrases of the topic just typed
            rows = s.query(Channel).filter(Channel.niche == k).order_by(Channel.id).all()
            g = [(k, r.kind or "topic", r.query or "") for r in rows if r.kind != "trending" or trending_ok] if rows else \
                [(k, "topic", p) for p in niches.phrases_for_topic(topics.get(k, ""))]
        groups.append(g)
    return _interleave(groups)


def _apply(s, active: list[str], topics: dict, review_first: bool, tz_offset: int | None, trending_ok: bool) -> dict:
    """Bring the account's niche sources in line with `active` (see the module text). Returns what changed."""
    dest = main_destination(s)
    if not dest:
        raise NicheError("Connect your YouTube channel first, then pick your niche.")
    plan = _plan(s, active, topics, trending_ok)
    room_on = budget()
    on_list, waiting = plan[:room_on], plan[room_on:]
    mine = {(c.kind or "channel", (c.query or "").strip().lower()): c
            for c in s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "viral")).order_by(Channel.id)}
    cap = acl.limit("sources")
    room = None if cap is None else cap - acl.source_count()
    times, tz = _times(s, tz_offset)
    made, back, kept, keep_on, skipped = [], [], 0, set(), 0
    for key, kind, query in on_list:
        old = mine.get((kind, query.lower()))
        if old is not None:
            if not old.niche:
                kept += 1  # the user's own source with the same words: left exactly as it is
                continue
            if old.niche not in active:
                old.niche = key  # e.g. a trending source two niches share: it now belongs to the niche still on
            if not old.auto:
                old.auto = True
                back.append(old)
            keep_on.add(old.id)
            continue
        if room is not None and room <= 0:
            skipped += 1
            continue
        ch = Channel(kind=kind, query=query, mode="cc", permission_note="", auto=True, niche=key,
                     destination_id=dest.id, review_first=bool(review_first), post_times=times, tz_offset=tz, **PER_RUN)
        if kind == "topic":
            ch.resolved_url, ch.name = "topic", f"{_label(key)}: {query}"[:300]
        else:
            ch.name = "Trending (CC clips): " + TRENDS.get(query, "all")
        s.add(ch)
        s.flush()
        made.append(ch)
        keep_on.add(ch.id)
        if room is not None:
            room -= 1
    parked = 0
    for c in s.query(Channel).filter(Channel.niche.isnot(None), Channel.auto.is_(True)).all():
        if c.id not in keep_on:  # beyond the budget, or its niche is no longer on
            c.auto, parked = False, parked + 1
    s.flush()
    on = s.query(Channel).filter(Channel.niche.isnot(None), Channel.auto.is_(True)).all()
    hours = _interval(len(on))
    for c in on:  # ALL niche sources of the account, not only the new ones
        c.interval_hours = hours
    return {"made": made, "back": back, "kept": kept, "skipped": skipped, "parked": parked, "waiting": len(waiting),
            "hours": hours, "on": len(on), "cap": cap}


def choose(keys: list[str], topic: str = "", review_first: bool = True, tz_offset: int | None = None) -> dict:
    """Make (or switch back on) the sources of the picked niches / typed topic, and re-plan all niche sources.
    Respects the account's switches and limits. Raises NicheError (400) or HTTPException (403 from app/acl.py)."""
    raw = list(dict.fromkeys(str(k)[:MAX_KEY] for k in (keys or [])))
    keys = [k for k in raw if k in niches.BY_KEY]
    topic = niches.clean_topic(topic)
    acl.need("source.topic")  # niches are topic sources: that switch must be on
    trending_ok = acl.allowed("source.trending")
    with SessionLocal() as s:
        known = {k for (k,) in s.query(Channel.niche).filter(Channel.niche.isnot(None)).distinct()}
        again = [k for k in raw if k.startswith("custom:") and k in known]  # a typed topic removed earlier
        if not keys and not topic and not again:
            raise NicheError("Pick at least one niche, or write your own topic (words, not a link).")
        topics = {}
        new = keys + again
        if topic:
            ck = niches.custom_key(topic)
            topics[ck] = topic
            new.append(ck)
        before = _active(s)
        active = before + [k for k in new if k not in before]
        if len(active) > MAX_NICHES:
            raise NicheError(f"You can have up to {MAX_NICHES} niches or own topics at once (you have "
                             f"{len(before)}: {', '.join(_label(k) for k in before)}). Remove one first in Sources → "
                             "Change my niche.")
        if acl.limit("sources") is not None and acl.limit("sources") - acl.source_count() <= 0:
            mine = {(c.kind or "channel", (c.query or "").lower()) for c in s.query(Channel)}
            if all((w[1], w[2].lower()) not in mine for w in _plan(s, [k for k in new if k not in before], topics, trending_ok)):
                acl.need_source_room()  # nothing to switch back on and no room at all: the plan's usual message (403)
        r = _apply(s, active, topics, review_first, tz_offset, trending_ok)
        s.commit()
        names, back_names = [c.name for c in r["made"]], [c.name or c.query for c in r["back"]]
    warnings = [f"“{_label(k)}”: {niches.LOW_CC_NOTE}" for k in keys if niches.BY_KEY[k].get("low_cc")]
    if topic and niches.low_cc(topic):
        warnings.append(f"“{topic}”: {niches.LOW_CC_NOTE}")
    why = []
    if r["skipped"]:
        why.append(f"Your account can have {r['cap']} sources (switched-off ones count too), so {r['skipped']} more were not "
                   f"made. Delete sources you no longer need in the list below, or {acl.ASK} for more.")
    if r["waiting"]:
        why.append(f"To stay near {per_day()} clips a day, {r['on']} sources work for your niches; {r['waiting']} more "
                   "phrases wait. Fewer niches give each one more phrases.")
    if not trending_ok and any(niches.BY_KEY[k].get("trending") for k in keys):
        why.append("Trending sources are switched off for your account, so only topic sources were made.")
    return {"made": names, "turned_on": back_names, "already": r["kept"], "skipped": r["skipped"], "parked": r["parked"],
            "why": " ".join(why), "warnings": warnings, "interval_hours": r["hours"], **state()}


def remove(key: str) -> dict:
    """Switch this niche's sources off (nothing is deleted; clips already made stay) and re-plan the others: a trending
    source another niche also uses stays on, and the niches left may get more phrases."""
    key = str(key)[:MAX_KEY]
    with SessionLocal() as s:
        rows = s.query(Channel).filter(Channel.niche == key).all()
        if not rows:
            raise NicheError("That niche has no sources in your account.")
        was_on = {c.id for c in rows if c.auto}
        active = [k for k in _active(s) if k != key]
        for c in rows:
            c.auto = False
        s.flush()
        if active:
            _apply(s, active, {}, True, None, acl.allowed("source.trending"))
        s.commit()
        n = sum(1 for c in s.query(Channel).filter(Channel.id.in_(was_on)) if not c.auto) if was_on else 0
    return {"switched_off": n, **state()}


THIN_WORDS = ("no new Creative Commons", "every video found", "not CC")


def state() -> dict:
    """The catalogue and this account's niches: sources (on / all), videos used so far, plain warnings."""
    with SessionLocal() as s:
        rows = s.query(Channel).filter(Channel.niche.isnot(None)).order_by(Channel.id).all()
        used = {}
        ids = [c.id for c in rows]
        if ids:
            for (ref,) in s.query(Project.channel_ref).filter(Project.channel_ref.in_(ids)):
                used[ref] = used.get(ref, 0) + 1
    mine = {}
    for c in rows:
        m = mine.setdefault(c.niche, {"key": c.niche, "label": _label(c.niche), "custom": c.niche.startswith("custom:"),
                                      "sources": 0, "on": 0, "videos": 0, "ran": 0, "thin_runs": 0, "phrases": []})
        m["sources"] += 1
        m["on"] += 1 if c.auto else 0
        m["videos"] += used.get(c.id, 0)
        m["phrases"].append(c.query if c.kind == "topic" else c.name)
        if c.last_run:
            m["ran"] += 1
            m["thin_runs"] += 1 if any(w in (c.last_status or "") for w in THIN_WORDS) else 0
    out = []
    for m in mine.values():
        warn = []
        n = niches.BY_KEY.get(m["key"])
        if (n and n.get("low_cc")) or (m["custom"] and niches.low_cc(m["label"])):
            warn.append(niches.LOW_CC_NOTE)
        if m["ran"] and m["videos"] < 2 and m["thin_runs"]:
            warn.append(f"The first searches for “{m['label']}” found little Creative Commons video so far. "
                        "A broader topic, or one of the ready-made niches, usually finds more.")
        m["warnings"] = warn
        m["active"] = m["on"] > 0
        for k in ("ran", "thin_runs"):
            m.pop(k)
        out.append(m)
    cap = acl.limit("sources")
    return {"catalog": [{"key": n["key"], "label": n["label"], "phrases": n["phrases"], "trending": bool(n.get("trending")),
                         "warning": niches.LOW_CC_NOTE if n.get("low_cc") else ""} for n in niches.NICHES],
            "mine": out, "room": None if cap is None else max(0, cap - acl.source_count()),
            "max_niches": MAX_NICHES, "per_day": per_day(), "budget": budget(),
            "topic_on": acl.allowed("source.topic"), "trending_on": acl.allowed("source.trending"), "note": HONEST}


def preview(topic: str, suggest: bool = True) -> dict:
    """What a typed topic would become: its phrases, a warning, and what people also search (keyword helper)."""
    t = niches.clean_topic(topic)
    out = {"topic": t, "phrases": niches.phrases_for_topic(t), "warning": niches.LOW_CC_NOTE if niches.low_cc(t) else "",
           "suggest": []}
    if t and suggest:
        try:
            from . import keywords as kw

            have = {p.lower() for p in out["phrases"]}
            out["suggest"] = [k for k in kw.research(t, "clip")["keywords"] if k.lower() not in have][:6]
        except Exception:  # noqa: BLE001  (suggestions are a nice extra; never a reason to fail)
            pass
    return out

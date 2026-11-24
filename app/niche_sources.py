"""Pick your niche -> sources made for you (Get started step 7, and Sources -> "Change my niche").

Each phrase of a picked niche (app/niches.py), or of a topic the user typed, becomes one ordinary topic source of this
account: Creative Commons only, running by itself. A niche with a trending category also gets one "Trending (CC clips)"
source. They are normal sources in every way (Run, Edit, auto-tune in app/learn.py, limits in app/acl.py); they only
carry `Channel.niche`, so a niche can be removed again as one: its sources are switched off (never deleted) and every
clip they made stays.

Everything here works in the copy of the signed-in account (tenant): sources are never made anywhere else.
"""
import math

from . import acl, niches
from .db import Channel, Project, SessionLocal, main_destination

DEFAULT_TIMES = "10:00, 18:00"
IST = -330  # JS getTimezoneOffset() of India
TARGET_PER_DAY = 6  # clips a day all niche sources together aim for (about one Google project's daily uploads)
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


def _interval(n_sources: int) -> int:
    """Hours between runs, so all niche sources together make about the account's clips a day (or TARGET_PER_DAY)."""
    cap = acl.limit("clips_per_day")
    per_day = max(1, min(cap, TARGET_PER_DAY) if cap else TARGET_PER_DAY)
    return min(48, max(6, math.ceil(24 * max(1, n_sources) / per_day)))


def wanted(keys: list[str], topic: str = "") -> list[tuple[str, str, str]]:
    """(niche key, kind, query) of every source the picks ask for: the niches' first phrases first, so a small source
    limit still gives every picked niche something; a niche's trending source after its phrases."""
    groups = []
    for k in keys:
        n = niches.BY_KEY[k]
        g = [(k, "topic", p) for p in n["phrases"]]
        if n.get("trending"):
            g.append((k, "trending", n["trending"]))
        groups.append(g)
    if niches.clean_topic(topic):
        groups.append([(niches.custom_key(topic), "topic", p) for p in niches.phrases_for_topic(topic)])
    out, seen = [], set()
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i < len(g) and (g[i][1], g[i][2].lower()) not in seen:
                seen.add((g[i][1], g[i][2].lower()))
                out.append(g[i])
    return out


def choose(keys: list[str], topic: str = "", review_first: bool = True, tz_offset: int | None = None) -> dict:
    """Make (or switch back on) the sources of the picked niches / typed topic. Respects the account's switches and
    limits. Raises NicheError (400) or HTTPException (403 from app/acl.py)."""
    raw = list(dict.fromkeys(str(k) for k in (keys or [])))
    keys = [k for k in raw if k in niches.BY_KEY]
    again = [k for k in raw if k.startswith("custom:")]  # a typed topic removed earlier: its sources back on
    topic = niches.clean_topic(topic)
    if not keys and not topic and not again:
        raise NicheError("Pick at least one niche, or write your own topic (words, not a link).")
    acl.need("source.topic")  # niches are topic sources: that switch must be on
    trending_ok = acl.allowed("source.trending")
    want = [w for w in wanted(keys, topic) if w[1] != "trending" or trending_ok]
    cap = acl.limit("sources")
    made, back, kept, skipped = [], [], [], 0
    with SessionLocal() as s:
        dest = main_destination(s)
        if not dest:
            raise NicheError("Connect your YouTube channel first, then pick your niche.")
        mine = {(c.kind or "channel", (c.query or "").strip().lower()): c
                for c in s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "viral"))}
        room = None if cap is None else cap - acl.source_count()
        times, tz = _times(s, tz_offset)
        if room is not None and room <= 0 and not again and all((w[1], w[2].lower()) not in mine for w in want):
            acl.need_source_room()  # nothing to switch back on and no room at all: the plan's usual message (403)
        changed = []
        for key, kind, query in want:
            old = mine.get((kind, query.lower()))
            if old is not None:
                if old.niche and not old.auto:  # removed earlier: switched back on (nothing new is made)
                    old.auto = True
                    back.append(old)
                    changed.append(old)
                else:
                    kept.append(old)  # already there (made by the user or by this niche): left as it is
                continue
            if room is not None and room <= 0:
                skipped += 1
                continue
            ch = Channel(kind=kind, query=query, mode="cc", permission_note="", auto=True, niche=key,
                         destination_id=dest.id, review_first=bool(review_first), post_times=times, tz_offset=tz,
                         **PER_RUN)
            if kind == "topic":
                ch.resolved_url, ch.name = "topic", f"{_label(key)}: {query}"[:300]
            else:
                ch.name = "Trending (CC clips): " + TRENDS.get(query, "all")
            s.add(ch)
            made.append(ch)
            changed.append(ch)
            if room is not None:
                room -= 1
        for k in again:
            for c in s.query(Channel).filter(Channel.niche == k, Channel.auto.is_(False)).all():
                c.auto = True
                back.append(c)
                changed.append(c)
        s.flush()
        hours = _interval(s.query(Channel).filter(Channel.niche.isnot(None), Channel.auto.is_(True)).count())
        for c in changed:
            c.interval_hours = hours
        s.commit()
        names = [c.name for c in made]
        back_names = [c.name or c.query for c in back]
    warnings = [f"“{_label(k)}”: {niches.LOW_CC_NOTE}" for k in keys if niches.BY_KEY[k].get("low_cc")]
    if topic and niches.low_cc(topic):
        warnings.append(f"“{topic}”: {niches.LOW_CC_NOTE}")
    why = ""
    if skipped:
        why = (f"Your account can have {cap} sources, so {skipped} more were not made. Remove a niche or a source, "
               f"or {acl.ASK} for more.")
    elif not trending_ok and any(niches.BY_KEY[k].get("trending") for k in keys):
        why = "Trending sources are switched off for your account, so only topic sources were made."
    return {"made": names, "turned_on": back_names, "already": len(kept), "skipped": skipped, "why": why,
            "warnings": warnings, "interval_hours": hours, **state()}


def remove(key: str) -> dict:
    """Switch every source of this niche off. Nothing is deleted; clips already made stay."""
    with SessionLocal() as s:
        rows = s.query(Channel).filter(Channel.niche == key).all()
        if not rows:
            raise NicheError("That niche has no sources in your account.")
        n = 0
        for c in rows:
            if c.auto:
                c.auto, n = False, n + 1
        s.commit()
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

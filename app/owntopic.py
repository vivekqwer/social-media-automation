"""My own video + a few words about it -> title, description, tags, thumbnail text and an upload time, chosen from
what people are searching and watching now.

Right after the upload (before the render):
  1. YouTube's search suggestions for the topic (the keyword helper's free source, cached for a day)
  2. one official YouTube Data API search when today's search budget allows (100 units, see app/quota.py): this
     week's most-viewed videos and Shorts in India for the topic, then their titles, tags and views (1 unit)
     — when the budget is short, the last saved search for this topic and the trending lists the app already saved
  3. title / description (hook line, 3-5 hashtags, a line about the video, the channel's call to subscribe) / tags
     (inside YouTube's 500 characters) / thumbnail text
  4. an upload time: the best hour from Ideas -> What works when there are enough numbers, else the next free slot
Everything is only a proposal on the clip: it stays editable and nothing goes up before the owner approves it.
No credit lines: it is the owner's own video. Nothing here promises views.
"""
import json
import logging
import re
from collections import Counter
from datetime import timedelta

from . import keywords as kw
from . import quota
from .db import Clip, Destination, Project, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
IST = timedelta(hours=5, minutes=30)
TOP_N = 5  # videos named in "based on"
CACHE_DAYS = 14


def _search_live(topic: str) -> list[dict]:
    """This week's most-viewed videos (and Shorts) in India for the topic. Spends 100 + 1 units; raises on failure."""
    from . import youtube

    yt = youtube.client()
    since = (utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = yt.search().list(part="id", q=topic, type="video", order="viewCount", regionCode="IN", publishedAfter=since,
                         safeSearch="moderate", maxResults=25).execute()
    quota.spend("search")
    quota.note_search()
    ids = [it["id"]["videoId"] for it in r.get("items", []) if (it.get("id") or {}).get("videoId")]
    if not ids:
        return []
    v = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(ids)).execute()
    quota.spend("read")
    out = []
    for it in v.get("items", []):
        sn, st = it.get("snippet") or {}, it.get("statistics") or {}
        dur = youtube._iso_seconds((it.get("contentDetails") or {}).get("duration", ""))
        out.append({"title": sn.get("title", ""), "channel": sn.get("channelTitle", ""), "tags": (sn.get("tags") or [])[:30],
                    "views": int(st.get("viewCount") or 0), "short": 0 < dur <= 180, "video_id": it.get("id")})
    return sorted(out, key=lambda x: -x["views"])


def _ckey(topic: str) -> str:
    return "owntopic:" + "-".join(kw.tokens(topic))[:80]


def _saved_trending(topic: str) -> list[dict]:
    """Videos on the trending lists the app already saved that share the topic's words (no request)."""
    need = set(kw._sig(kw.tokens(topic)))
    out = []
    for it in kw.trending_now()["items"]:
        if need and len(need & it["words"]) >= min(2, len(need)):
            out.append({"title": it["title"], "channel": "", "tags": it["tags"][:30], "views": 0, "short": None})
    return out


def find(topic: str) -> dict:
    """What people search and watch for this topic now. {"videos", "suggest", "net", "source", "at", "why_cached"}."""
    topic = " ".join(str(topic or "").split())[:120]
    sug = kw.research(topic, "clip") if topic else {"keywords": [], "net": "offline"}
    videos, source, why = [], "live", ""
    from . import youtube

    if not youtube.configured():
        source, why = "saved", "YouTube's search is not set up for this account"
    elif not quota.can_search():
        source, why = "saved", "today's YouTube search budget is used (it resets at 1:30 PM IST)"
    elif quota.on_shared_budget() and quota.search_block():  # on the owner's Google project: a fair, capped turn only
        quota.note_turn(False)
        source, why = "saved", quota.search_block()
    else:
        try:
            videos = _search_live(topic)
            if quota.on_shared_budget():
                quota.note_turn(True)
            set_setting(_ckey(topic), json.dumps({"at": utcnow().isoformat() + "Z", "videos": videos[:25]}))
        except Exception as e:  # noqa: BLE001  (a failed search never stops the upload)
            log.warning("own video topic search: %s", str(e)[:200])
            source, why = "saved", "YouTube's search did not answer just now"
            if "quotaExceeded" in str(e) or "dailyLimitExceeded" in str(e):
                quota.exhaust()  # the Google project's budget is used up (other apps may share it): show it as full
                why = "YouTube's daily budget is used up (it resets at 1:30 PM IST)"
    at = utcnow().isoformat() + "Z"
    if source == "saved":
        try:
            old = json.loads(get_setting(_ckey(topic)) or "{}")
        except ValueError:
            old = {}
        if old.get("videos"):
            videos, at = old["videos"], old.get("at") or at
            why += f" — using the search saved {old.get('at', '')[:10]}"
        else:
            videos = _saved_trending(topic)
            why += " — using the trending lists saved earlier" if videos else " — only the search suggestions were used"
    return {"topic": topic, "videos": videos, "suggest": list(sug.get("keywords") or [])[:30], "net": sug.get("net", "offline"),
            "source": source, "why_cached": why, "at": at}


# ---------------------------------------------------------------- writing it
def _cap(phrase: str) -> str:
    return " ".join(w if w.isupper() or not w[:1].isalpha() else w[:1].upper() + w[1:] for w in phrase.split())


def _ranked(topic: str, found: dict) -> list[dict]:
    """Keywords, strongest first: typed suggestions that share the topic's words, then tags of the top videos."""
    need = set(kw._sig(kw.tokens(topic)))
    items, seen = [], kw._Seen()

    def add(k, src):
        k = kw._tidy(k)
        if not k or len(k) > 60 or kw._junk_tag(k):
            return
        if seen.add(k):
            items.append({"k": k, "src": [src], "hot": src == "trending"})

    if topic:
        add(topic.lower(), "extra")
    for k in found["suggest"]:
        if not need or set(kw._sig(kw.tokens(k))) & need:
            add(k, "typed")
    counts = Counter()
    own = set(kw.tokens(topic))
    for v in found["videos"][:15]:
        for t in v.get("tags") or []:
            t = kw._tidy(t)
            if not t or kw._bad(t, own) or len(kw.tokens(t)) > 6:
                continue
            on_topic = not need or set(kw._sig(kw.tokens(t))) & need  # tags of the top videos that name the topic count more
            counts[t] += (2 if on_topic else 1) * (1 + (v.get("views") or 0) / 1e6)
    for t, _n in counts.most_common(20):
        add(t, "trending")
    return items


def _channel_name(s, c: Clip) -> str:
    d = s.get(Destination, c.destination_id) if c.destination_id else None
    return (d.name if d else "") or ""


def compose(topic: str, found: dict, clip: Clip, channel: str, own_title: str = "") -> dict:
    from .metadata import MAX_HASHTAGS, _cut, _fit_tags
    from .thumbs import default_text

    items = _ranked(topic, found)
    shorts = clip.aspect == "9:16"
    typed = [i["k"] for i in items if "typed" in i["src"]]
    lead = typed[0] if typed else topic
    head = _cap(lead)[:70]
    mine = own_title.strip()
    camera = re.fullmatch(r"(?i)(vid|img|mov|pxl|dsc|video|whatsapp video)?[\W_]*[\d\W_]+.*", mine or "x") is not None
    rest = mine if mine and not camera and kw.topic_stems(mine) - kw.topic_stems(lead) else ""
    if not rest and len(typed) > 1:
        rest = _cap(typed[1])
    title = (f"{head} | {rest}" if rest and len(head) + len(rest) + 3 <= 100 else head)[:100]
    tags = [h for h in (kw.hashtag(i["k"]) for i in items) if h]
    hashtags = ((["shorts"] if shorts else []) + [h for h in dict.fromkeys(tags)])[:5 if shorts else 4]
    if len(hashtags) < 3:
        hashtags += [h for h in ("india", "trending") if h not in hashtags][:3 - len(hashtags)]
    hashtags = hashtags[:MAX_HASHTAGS]
    hook = f"{_cap(lead)} — watch till the end!" if shorts else f"{_cap(lead)}: everything in one video."
    about = f"This video is about {topic}." if topic else ""
    more = [k for k in typed[1:3]]
    if more:
        about += f" Also: {', '.join(more)}."
    cta = f"Subscribe to {channel} for more videos like this." if channel else "Subscribe for more videos like this."
    description = _cut("\n".join([hook, "", " ".join("#" + h for h in hashtags), "", about, "", cta]).strip(), 4900)
    ranked = [i["k"] for i in items]
    final_tags = _fit_tags(ranked, ["shorts"] if shorts else [])
    used = {t.lower() for t in final_tags}
    for i in items:
        i["used"] = i["k"].lower() in used
    return {"title": title, "description": description, "tags": final_tags, "thumb_text": default_text(head) or head.upper()[:34],
            "items": items, "lead": lead, "about": about}


def best_slot(s, clip: Clip, now=None):
    """(publish time, why). The strongest hour of Ideas -> What works when there are enough numbers; else the next
    free slot of the channel (the same rule every source uses)."""
    now = now or utcnow()
    try:
        from .learn import current

        m = current(now)
    except Exception:  # noqa: BLE001
        m = {}
    hours = m.get("hours") or []
    if m.get("enough") and hours and hours[0].get("ratio", 0) >= 1.2:
        h = int(hours[0]["key"])
        from .autopilot import fit_capacity

        t = (now + IST + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        for _ in range(48):
            if t.hour == h:
                break
            t += timedelta(hours=1)
        # the same daily upload budget as every other clip: a full day moves it to the same hour on the next day
        slot = fit_capacity(s, t - IST, f"{h:02d}:00", int(IST.total_seconds() // -60), skip_id=clip.id)
        return slot, (f"{h:02d}:00 India time — your videos published at that hour get {hours[0]['ratio']}× your usual "
                         f"views ({hours[0]['n']} videos, Ideas → What works)")
    from .autopilot import _next_slot

    return _next_slot(s, 12, clip.destination_id), "the next free upload time of the channel (not enough numbers yet to pick the best hour)"


def fill(clip_id: int, topic: str) -> dict | None:
    """Research the topic now and write everything on the clip (a proposal; nothing is queued). Never raises."""
    try:
        found = find(topic)
        with SessionLocal() as s:
            c = s.get(Clip, clip_id)
            if not c or c.youtube_id or c.status in ("uploading", "uploaded") or c.meta_edited:
                return None
            p = s.get(Project, c.project_id)
            meta = compose(found["topic"], found, c, _channel_name(s, c), p.title if p else "")
            slot, slot_why = best_slot(s, c)
            top = [{"title": v["title"][:100], "views": v.get("views") or 0, "channel": v.get("channel", ""), "short": v.get("short")}
                   for v in found["videos"][:TOP_N]]
            what = "Shorts and videos" if any(v.get("short") for v in top) else "videos"
            based = (f"based on: top {len(top)} {what} for “{found['topic']}” in India this week" if top and found["source"] == "live"
                     else f"based on: {len(top)} saved {what} for “{found['topic']}”" if top else f"based on: what people type for “{found['topic']}”")
            based += f" and {len([i for i in meta['items'] if 'typed' in i['src']])} search suggestions"
            note = "Chosen from what people are searching and watching now — " + based + "."
            if found["why_cached"]:
                note += f" Not a live search: {found['why_cached']}."
            c.title, c.description, c.tags, c.thumb_text = meta["title"], meta["description"], meta["tags"], meta["thumb_text"][:120]
            c.kw = {"v": 1, "at": found["at"], "lead": meta["lead"], "hint": found["topic"],
                    "net": "online" if found["source"] == "live" else "stale", "items": meta["items"], "note": note,
                    "basis": {"topic": found["topic"], "source": found["source"], "why_cached": found["why_cached"],
                              "videos": top, "suggest": found["suggest"][:10], "slot": slot.isoformat() + "Z" if slot else None,
                              "slot_why": slot_why, "line": based}}
            if slot and not c.publish_at:
                c.publish_at = slot
            if p:
                p.description = meta["about"] or p.description or ""  # a line about the video (Instagram/Facebook text)
                p.tags = meta["tags"]
            s.commit()
            return c.kw
    except Exception:  # noqa: BLE001
        log.exception("own video topic: could not fill clip %s", clip_id)
        return None


def fill_then_render(clip_id: int, topic: str):
    """Background job of an own upload with a topic: write everything first, so the thumbnail uses its text."""
    from .render import run_render

    fill(clip_id, topic)
    run_render(clip_id)

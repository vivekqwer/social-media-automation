"""Hook titles: a short curiosity / emotion title instead of "<the source video's title> | Best Moment".

Plain templates (no AI service), in the channel's title language (English / Hinglish / Hindi). The template is picked
from the clip id's place among the clips of the same source video, so the 2-3 clips cut from one video always get
DIFFERENT titles, and the same clip always gets the same title (re-runs are stable).

Only the title changes. The description (credit lines, licence line, the original description) is written exactly as
before by app/metadata.py. Never touched: hand-edited clips (meta_edited), my own uploaded videos (the owner typed that
title), clips written from a search on my own video's topic (kw["basis"]), my own reels (mode "original").

Also here: the slow timed job that gives videos already on YouTube (private, publish time still ahead) the new title
(retitle_due, switch "titles.retitle"), at most RETITLE_PER_DAY a day, uploads always first.

Switches (app/acl.py): "titles.hook", "titles.retitle". The Super Admin can also switch either off for one copy
(the owner's own included) with POST /api/admin/growth-fixes (setting growth_fixes_off).
"""
import hashlib
import json
import logging
import re
from datetime import timedelta

from .db import Clip, Project, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

AIM = 60  # visible characters a title should stay within (YouTube shows about this much on a phone)
HARD_MAX = 100  # YouTube's own limit
RETITLE_PER_DAY = 20  # videos.update calls a day per account (50 + 1 units each)
RETITLE_PER_RUN = 3
RETITLE_MIN_AHEAD = timedelta(hours=2)  # never touch a video that goes public within this time
OFF_KEY = "growth_fixes_off"  # this copy's own "off" list, set by the Super Admin (also works in the owner's copy)

# {t} = the topic phrase. Neutral wording: no claim the video cannot keep ("gone wrong", "viral", prices...).
TEMPLATES = {
    "en": [
        "You Won't Believe This {t} 😱",
        "Nobody Expected This {t} 🤯",
        "Wait For The End… {t} 😳",
        "This {t} Is Something Else 🔥",
        "{t} Like You've Never Seen 👀",
        "Is This The Best {t} Ever? 🤔",
        "I Can't Stop Watching This {t} 😍",
        "Everyone Is Talking About This {t} 🔥",
        "The Craziest {t} Moment 😱",
        "Watch Till The End — {t} 👀",
        "Why Is Nobody Talking About This {t}? 🤔",
        "{t}: The Moment Everyone Missed 😮",
        "Would You Have Guessed This? {t} 🤯",
        "This {t} Moment Hits Different 🔥",
    ],
    "hinglish": [
        "Yeh {t} Dekh Ke Hosh Ud Jayenge 😱",
        "Kya Aapne Yeh {t} Dekha? 👀",
        "{t} Ka Aisa Scene Kabhi Nahi Dekha 🔥",
        "Aisa {t} Pehle Kabhi Nahi Dekha 🤯",
        "{t} — End Tak Dekhna 😳",
        "{t} Ka Sabse Crazy Moment 😱",
        "Yeh {t} Sach Mein Hai? 😮",
        "{t} Dekh Ke Yakeen Nahi Hoga 🔥",
        "Sab Log Is {t} Ki Baat Kar Rahe Hain 🔥",
        "{t} Ka Yeh Moment Miss Mat Karna 👀",
        "Bhai Yeh {t} Toh Kamaal Hai 😍",
        "Kya Aap Yeh {t} Try Karoge? 🤔",
        "{t} — Iske Baad Kya Hua? 😳",
    ],
    "hi": [
        "यह {t} देखकर होश उड़ जाएंगे 😱",
        "क्या आपने यह {t} देखा? 👀",
        "{t} का ऐसा नज़ारा कभी नहीं देखा 🔥",
        "ऐसा {t} पहले कभी नहीं देखा 🤯",
        "{t} — आखिर तक ज़रूर देखें 😳",
        "{t} का सबसे कमाल पल 😍",
        "यकीन नहीं होगा! {t} 😮",
        "{t} देखकर सब हैरान 😱",
        "सब इस {t} की बात कर रहे हैं 🔥",
        "{t} का यह पल मिस मत करना 👀",
        "भाई यह {t} तो कमाल है 😍",
        "क्या आप यह {t} आज़माएंगे? 🤔",
        "{t} — इसके बाद क्या हुआ? 😳",
    ],
}

# a source title that is a whole statement ("Women are THIRSTING over ...") stays a statement: {t} first
SENTENCE = {
    "en": ["{t} 😱 Wait For The End", "{t}?! 🤯", "{t} — Nobody Expected This 😳", "{t} 👀 Watch Till The End",
           "{t} — Would You Believe It? 🤔"],
    "hinglish": ["{t} 😱 End Tak Dekhna", "{t} — Yakeen Nahi Hoga 🤯", "{t} 👀 Kya Scene Hai", "{t} — Iske Baad Kya Hua? 😳",
                 "{t} — Aapka Kya Kehna Hai? 🤔"],
    "hi": ["{t} 😱 आखिर तक देखें", "{t} — यकीन नहीं होगा 🤯", "{t} 👀 क्या नज़ारा है", "{t} — इसके बाद क्या हुआ? 😳",
           "{t} — आपकी क्या राय है? 🤔"],
}


# ---------------------------------------------------------------- the switches
def on(key: str) -> bool:
    """Is this new behaviour on for the copy being worked in? The account's Super Admin switch (app/acl.py) and the
    copy's own off list (growth_fixes_off: the only way to switch something off in the owner's own copy)."""
    try:
        from . import acl

        if not acl.feature(key):
            return False
    except Exception:  # noqa: BLE001  (accounts table unreachable: the copy's own list still counts)
        pass
    return key not in off_list()


def off_list() -> list[str]:
    try:
        v = json.loads(get_setting(OFF_KEY) or "[]")
    except (TypeError, ValueError):
        v = []
    return [str(x) for x in v] if isinstance(v, list) else []


def set_off(key: str, off: bool) -> list[str]:
    cur = [k for k in off_list() if k != key] + ([key] if off else [])
    set_setting(OFF_KEY, json.dumps(sorted(set(cur))))
    return off_list()


# ---------------------------------------------------------------- the topic phrase
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍⬀-⯿⌀-⏿]+")
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_JUNK = re.compile(r"(?i)\b(?:official(?:\s+(?:music\s+)?video)?|reaction|reacting|react(?:s|ing)?\s+to|reacts?|full\s+video|"
                   r"full\s+episode|episode\s*\d*|ep\s*\d+|part\s*\d+|pt\s*\d+|shorts?|#\w+|4k|8k|uhd|hd|fhd|"
                   r"\d{3,4}p|\d{2,3}\s*fps|hdr|(?:19|20)\d{2}|must\s+watch|best\s+moments?(?:\s+dekho)?|"
                   r"viral|trending)\b")
_SPLIT = re.compile(r"\s+[-–—]\s+|[|•·:!?।\"“”]+")
# a topic phrase stops at the first of these (they start what is said ABOUT the topic)
_CUT = set("""must be is are was were will would should can could gets got goes went makes made has have had did does do
    vs versus vs. that which who when where while after before because but so if then than just only even not no never
    i we you they he she my our your his her their me us them here there now ever really very""".split())
_SMALL = set("a an the of in on at to for and or with by from over about into".split())
_GENERIC = set("video videos clip clips moment moments thing things stuff best top".split())


_COMMON = set("""old new big hot top bad sad mad all and are the for you its how why who was not one two six ten man men
    boy kid day eat buy try see own out off get got say pro max fun car bus""".split())


def _case(words: list[str], shouting: bool) -> list[str]:
    out = []
    for w in words:
        caps = w.isupper() and len(w) > 1
        if caps and (len(w) > 4 or (shouting and (len(w) > 3 or w.lower() in _COMMON))):  # shouting -> normal case (GTA, USA, BBQ stay)
            w = w[:1] + w[1:].lower()
        elif w.islower() and w.isascii():
            w = w[:1].upper() + w[1:]
        out.append(w)
    return [w.lower() if i and w.lower() in _SMALL else w for i, w in enumerate(out)]


def _core(title: str, channel: str = "") -> tuple[str, str]:
    """(phrase, kind) of a source title. kind "noun" = a topic ("Indian Street Food"); "sentence" = the title is a
    statement about someone ("Women Are Thirsting over Duke Dennis") and is kept whole, shortened."""
    t = _EMOJI.sub(" ", str(title or ""))
    t = _BRACKETS.sub(" ", t)
    t = _JUNK.sub(" ", t)
    chan = re.sub(r"\W+", "", str(channel or "")).lower()
    parts = [" ".join(p.split()) for p in _SPLIT.split(t)]
    parts = [p for p in parts if p and re.sub(r"\W+", "", p).lower() != chan]
    if channel and str(channel).strip():  # "... by <channel>"
        parts = [re.sub(r"(?i)\bby\s+" + re.escape(str(channel).strip()) + r"\b", " ", p).strip() for p in parts]
        parts = [" ".join(p.split()) for p in parts if p.strip()]
    pick = next((p for p in parts if len(re.findall(r"[^\W\d_]{2,}", p)) >= 2), parts[0] if parts else "")
    words = [w.strip(".,;'\"’‘") for w in pick.split()]
    words = [w for w in words if w and w not in ("&", "+", "x", "X")]
    letters = [w for w in words if re.search(r"[A-Za-z]", w)]
    shouting = bool(letters) and sum(1 for w in letters if w.isupper() and len(w) > 1) >= max(2, len(letters) * 0.6)
    keep, cut = [], False
    for w in words:
        lw = w.lower()
        if lw in _CUT:
            if keep:
                cut = True
                break
            continue
        keep.append(w)
        if len(keep) >= 6:
            break

    def trim(ws):
        ws = list(ws)
        while ws and (ws[-1].lower() in _SMALL or ws[-1].lower() in _GENERIC or ws[-1].lower() in _CUT):
            ws.pop()
        while ws and ws[0].lower() in ("the", "a", "an", "this", "these", "that"):
            ws.pop(0)
        return ws

    keep = trim(keep)
    doing = bool(keep) and keep[0].lower().endswith("ing") and len(keep[0]) > 4  # "Making 1000 Momos ..." is a statement
    if not doing and (len(keep) >= 2 or (keep and not cut)):
        return " ".join(_case(keep, shouting))[:48].strip(), "noun"
    whole = trim(words[:9])  # one word before "is / are / must ...": the whole statement is the hook
    if not whole:
        return "", "noun"
    phrase = " ".join(_case(whole, shouting))
    while len(phrase) > 44 and len(whole) > 3:
        whole = trim(whole[:-1])
        phrase = " ".join(_case(whole, shouting))
    return phrase, "sentence"


def core_topic(title: str, channel: str = "") -> str:
    """The plain topic of a source title: no channel name, no 'Reaction', no '|' parts, no 'Official', no years,
    no 4K / 60fps, no emoji, ALL-CAPS shouting written normally. '' when nothing usable is left."""
    return _core(title, channel)[0]


def _nice_phrase(phrase: str, title: str) -> str:
    """A search phrase / hint written like a title, spelled as the source title spells each word (GTA stays GTA)."""
    spell = {}
    for w in str(title or "").split():
        w = w.strip(".,;:!?|()[]{}\"'“”‘’#-–—")
        if w and not (w.isupper() and len(w) > 3):
            spell.setdefault(w.lower(), w)
    words = [spell.get(w.lower(), w) for w in str(phrase or "").split()]
    words = [w for w in words if w.lower() not in ("hindi", "in", "shorts", "short", "video", "videos")] or words
    return " ".join(_case(words, False))


def topic_phrase(project: Project, lead: str | None = None, hint: str | None = None) -> tuple[str, str]:
    """(phrase, kind) the title is about: the clip's own hint (typed by the owner) > the keyword plan's lead phrase
    (when short) > the cleaned core topic of the source title."""
    title = str(project.title or "")
    for cand in (hint, lead):
        cand = " ".join(str(cand or "").split())
        if cand and len(cand.split()) <= 5 and len(cand) <= 40:
            return _nice_phrase(cand, title), "noun"
    phrase, kind = _core(title, project.channel or "")
    if phrase:
        return phrase, kind
    return (" ".join(title.split()[:4])[:40] or "This"), "sentence"


# ---------------------------------------------------------------- the title
def _key_hash(text: str) -> int:
    return int(hashlib.sha1(str(text or "").encode()).hexdigest()[:8], 16)


def sibling_rank(clip: Clip) -> int:
    """This clip's place among the clips of the same source video (0 = the first one made)."""
    if not getattr(clip, "id", None) or not getattr(clip, "project_id", None):
        return 0
    try:
        with SessionLocal() as s:
            return s.query(Clip).filter(Clip.project_id == clip.project_id, Clip.id < clip.id).count()
    except Exception:  # noqa: BLE001
        return int(clip.id or 0)


def _shorten(topic: str, n: int) -> str:
    words = topic.split()
    while len(words) > 2 and len(" ".join(words)) > n:
        words.pop()
        while len(words) > 1 and words[-1].lower() in _SMALL:
            words.pop()
    return " ".join(words)


def hook_title(project: Project, clip: Clip, lang: str = "en", lead: str | None = None, hint: str | None = None,
               rank: int | None = None) -> str:
    """A short hook title for one clip (deterministic: the same clip always gets the same title)."""
    lang = lang if lang in TEMPLATES else "en"
    if hint is None:
        hint = ((getattr(clip, "kw", None) or {}).get("hint") or "")
    topic, kind = topic_phrase(project, lead, hint)
    pool = SENTENCE[lang] if kind == "sentence" else TEMPLATES[lang]
    rank = sibling_rank(clip) if rank is None else rank
    start = (_key_hash(project.video_id or project.title or "") + rank) % len(pool)
    best = None
    for k in range(len(pool)):  # this clip's own template first; one that keeps the title short if it is too long
        tpl = pool[(start + k) % len(pool)]
        room = AIM - len(tpl.replace("{t}", ""))
        t = tpl.replace("{t}", _shorten(topic, max(room, 12)))
        if best is None:
            best = t
        if len(t) <= AIM:
            return t
        if k >= 2:
            break
    return (best or topic)[:HARD_MAX].rstrip()


def applies(project: Project) -> bool:
    """Does the builder write a hook title for clips of this video? Never for my own reels / my own uploaded videos
    (mode "original": the owner's words). Hand-edited clips never reach the builder (metadata.needs_refresh /
    freshen_clip); the retitle job checks them itself (candidates / new_title_for)."""
    if project is None or project.mode == "original":
        return False
    return on("titles.hook")


# ---------------------------------------------------------------- videos already on YouTube (private, scheduled)
def _count_key() -> str:
    from . import quota

    return "retitle_n:" + quota._pt_day()


def _log_line(entry: dict):
    try:
        rows = json.loads(get_setting("retitle_log") or "[]")
    except (TypeError, ValueError):
        rows = []
    rows = ([entry] + [r for r in rows if isinstance(r, dict)])[:50]
    set_setting("retitle_log", json.dumps(rows, ensure_ascii=False))


def status() -> dict:
    try:
        rows = json.loads(get_setting("retitle_log") or "[]")
    except (TypeError, ValueError):
        rows = []
    try:
        today = int(get_setting(_count_key()) or 0)
    except (TypeError, ValueError):
        today = 0
    with SessionLocal() as s:
        done = sum(1 for (k,) in s.query(Clip.kw).filter(Clip.youtube_id.isnot(None)) if (k or {}).get("retitled", {}).get("at"))
    return {"on": on("titles.retitle") and on("titles.hook"), "today": today, "per_day": RETITLE_PER_DAY, "done": done,
            "recent": rows[:20]}


def candidates(s, now=None) -> list:
    """Clips on YouTube, still private with a publish time ahead, written by the system, not retitled yet."""
    now = now or utcnow()
    rows = s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.publish_at.isnot(None),
                                Clip.publish_at > now + RETITLE_MIN_AHEAD, Clip.meta_edited.isnot(True),
                                Clip.removed_at.is_(None), Clip.kept_private.isnot(True)) \
        .order_by(Clip.publish_at, Clip.id).all()
    out = []
    for c in rows:
        k = c.kw or {}
        if k.get("retitled") or k.get("basis"):
            continue
        if c.yt_state and c.yt_state not in ("ok", "private", "scheduled"):
            continue
        out.append(c)
    return out


def new_title_for(s, c: Clip) -> str | None:
    """The hook title this uploaded clip would get now (None = it keeps its title)."""
    from .metadata import lang_of

    p = s.get(Project, c.project_id)
    if p is None or c.meta_edited or (c.kw or {}).get("basis") or not applies(p):
        return None
    k = c.kw or {}
    t = hook_title(p, c, lang_of(c), lead=k.get("lead"), hint=k.get("hint") or "")
    return t if t and t != (c.title or "") else None


def _mark(cid: int, rec: dict):
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        if c is None:
            return
        k = dict(c.kw or {})
        k["retitled"] = rec
        c.kw = k
        s.commit()


def retitle_due(limit: int | None = None) -> dict:
    """Timed job (switch "titles.retitle", every hour per account): up to RETITLE_PER_RUN videos a run and
    RETITLE_PER_DAY a day get the hook title on YouTube (videos.update, snippet: description, tags, category and
    language are sent back exactly as YouTube has them). Uploads first: only when today's budget still holds every
    upload that is due (quota.can_update) and, on the owner's Google project, the owner's uploads (shared_room).
    A video whose title was changed on YouTube by hand is left alone. Each video is done once (kw["retitled"])."""
    from . import acl, quota, youtube
    from .comments import _uploads_due_here

    out = {"done": 0, "skipped": 0, "stopped": ""}
    if not (on("titles.retitle") and on("titles.hook")):
        out["stopped"] = "switched off"
        return out
    try:
        today = int(get_setting(_count_key()) or 0)
    except (TypeError, ValueError):
        today = 0
    room = min(RETITLE_PER_DAY - today, RETITLE_PER_RUN if limit is None else limit)
    if room <= 0:
        out["stopped"] = "today's retitles are done"
        return out
    with SessionLocal() as s:
        todo = [(c.id, c.destination_id, c.youtube_id, c.title or "", new_title_for(s, c)) for c in candidates(s)]
    bad_dest = set()
    for cid, did, vid, old, new in todo:
        if out["done"] >= room:
            break
        if not new:
            _mark(cid, {"at": None, "skip": "keeps its title"})
            out["skipped"] += 1
            continue
        if did in bad_dest:
            continue
        cost = quota.COST["update"] + quota.COST["read"]
        if not quota.can_update(_uploads_due_here()) or not quota.shared_room(cost):
            out["stopped"] = "today's YouTube budget is kept for uploads"
            break
        try:
            yt = youtube.comment_client(did)
            r = yt.videos().list(part="snippet,status", id=vid, maxResults=1).execute()
            quota.spend("read")
            items = r.get("items") or []
            if not items:
                _mark(cid, {"at": None, "skip": "not on YouTube any more"})
                out["skipped"] += 1
                continue
            sn, st = items[0].get("snippet") or {}, items[0].get("status") or {}
            if st.get("privacyStatus") != "private" or not st.get("publishAt"):
                _mark(cid, {"at": None, "skip": "already public or no publish time"})
                out["skipped"] += 1
                continue
            if " ".join(str(sn.get("title") or "").split()) != " ".join(old.split()):
                _mark(cid, {"at": None, "skip": "title changed on YouTube by hand"})
                out["skipped"] += 1
                continue
            body = {"title": new[:HARD_MAX], "description": sn.get("description") or "", "categoryId": sn.get("categoryId") or "24"}
            for f in ("tags", "defaultLanguage", "defaultAudioLanguage"):
                if sn.get(f):
                    body[f] = sn[f]
            quota.spend("update")
            yt.videos().update(part="snippet", body={"id": vid, "snippet": body}).execute()
        except youtube.NeedsReconnect:
            bad_dest.add(did)  # an older login without the wider scope: tried again after the channel is reconnected
            continue
        except Exception as e:  # noqa: BLE001
            m = str(e)
            if "quotaExceeded" in m or "dailyLimitExceeded" in m:
                quota.exhaust()
                out["stopped"] = "YouTube's daily budget is used up"
                break
            log.warning("retitle clip %s: %s", cid, m[:200])
            with SessionLocal() as s:
                c = s.get(Clip, cid)
                tries = int(((c.kw or {}).get("retitle_tries") or 0)) + 1 if c else 3
            if tries >= 3:
                _mark(cid, {"at": None, "skip": "YouTube refused: " + m[:120]})
            else:
                with SessionLocal() as s:
                    c = s.get(Clip, cid)
                    if c:
                        k = dict(c.kw or {})
                        k["retitle_tries"] = tries
                        c.kw = k
                        s.commit()
            continue
        at = utcnow().isoformat() + "Z"
        with SessionLocal() as s:
            c = s.get(Clip, cid)
            if c is not None:
                c.title = new[:200]
                k = dict(c.kw or {})
                k["retitled"] = {"at": at, "old": old[:200]}
                c.kw = k
                s.commit()
        today += 1
        set_setting(_count_key(), str(today))
        out["done"] += 1
        _log_line({"at": at, "clip": cid, "video": vid, "old": old[:120], "new": new[:120]})
        log.info("retitled clip %s (%s): %r -> %r", cid, vid, old, new)
        acl.record_here("titles.retitle", f"New title on YouTube for clip #{cid}: “{new[:80]}” (was “{old[:80]}”)")
    return out

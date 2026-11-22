"""Keyword research from free sources only: what people really type into YouTube search.

Three sources. No paid service, no AI, no YouTube Data API quota:
  * typed     YouTube's own search suggestions. Their ORDER is the only popularity signal YouTube gives away
              (most typed first). There are no search-volume numbers, and none are invented here.
  * video     the source video's own tags and the hashtags in its description
  * trending  lists the app already saves (viral board, competitor outliers, India trending) - no extra request

Every suggestion request is cached for a day per customer (settings rows "kwc:<hash>"), so repeated use costs
nothing and Google is never hammered. When the network fails the last cached answer is used; with no cache the
video's own tags carry on alone. Nothing in this module raises because of the network.
"""
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from functools import lru_cache

import httpx

from .db import Setting, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

URL = "https://suggestqueries.google.com/complete/search"
REGION = (os.getenv("KEYWORD_REGION") or "IN").upper()  # whose searches count (India by default)
CACHE_HOURS = 24  # suggestions change slowly
KEEP_DAYS = 7  # older cache rows are deleted
TIMEOUT = 6
BUDGET = {"clip": 9, "full": 16}  # most requests one research may make (cached answers are free)
MAX_ITEMS = 40
TRUSTED_TAGS = 6  # the creator's first tags are his main keywords; later ones must have something to do with the video
_down_until = 0.0  # the network, not a customer: after two failures in a row nothing is asked for 10 minutes

# a word: letters/digits of any script. Python's \w leaves out Devanagari vowel signs, which would cut Hindi words apart
_WORD = re.compile(r"(?:[^\W_]|[ऀ-ॣ०-෿])+")
_HASHTAG = re.compile(r"#((?:[^\W]|[ऀ-ॣ०-෿])+)")
DEVA = re.compile(r"[ऀ-ॿ]")

# words that say nothing about what a video is about (so they may appear in any phrase)
NEUTRAL = set("""a an the of in on at to for and or with from by is are was be vs how what why when where which who
    best top new latest video videos short shorts clip clips viral trending most all my your this that it i you we do
    does can ka ki ke ko se me mein hai hain kya kaise kyu kyun kyon aur par
    का की के को से में है हैं क्या कैसे क्यों और पर वीडियो""".split())
QUESTION = set("how what why when where which who can does do is are should kaise kya kyu kyun kyon kab kaha kahan kaun "
               "कैसे क्या क्यों कब कहाँ कहां कौन".split())
HINGLISH = set("hindi kaise kya kyu kyun kyon kab kaha kahan kaun hai hain banaye banane banate banaen banaye tarika vidhi "
               "ghar desi wala wali ka ki ke mein".split())
# never useful on a clip, wherever they come from
DROP = set("download downloader apk mp3 mp4 ringtone torrent pdf whatsapp xxx sex sexy porn nude".split())
# true of some videos only: allowed when the video's own title says so
MISLEADING = set("full live free official".split())
JUNK_TAGS = set("""shorts short viral trending youtube video videos subscribe like share fyp foryou foryoupage explore
    reels reel ytshorts ytshort youtubeshorts youtubeshort shortsfeed shortsvideo shortvideo viralvideo viralvideos
    viralshorts viralshort trendingshorts trendingvideo trendingnow new hd 4k tiktok instagram status shortsviral
    youtubevideo comment""".split())
_GENERIC_LABEL_WORDS = set("music records songs films movies studios pictures star colors bhajan bhakti aarti wave news "
                           "aditya dharma".split())
SMALL = set("a an the of in on at to for and or with vs ka ki ke ko se me mein".split())


# ---------- words ----------
def tokens(text) -> list[str]:
    """Lower-case words of any script (Hindi words stay whole)."""
    t = unicodedata.normalize("NFC", str(text or "")).lower().replace("‌", "").replace("‍", "")
    t = re.sub(r"['’]s(?![^\W_])", "", t)  # kabita's -> kabita
    return _WORD.findall(t)


def _sig(toks) -> list[str]:
    """The words that carry the topic."""
    return [t for t in toks if t not in NEUTRAL and (len(t) > 1 or t.isdigit() or DEVA.search(t))]


def _norm(p) -> str:
    return " ".join(str(p or "").replace("​", " ").split()).strip(" ,.;:|#\"'-").lower()


def _tidy(p) -> str:
    """A tag as the creator wrote it, without symbols around it."""
    p = re.sub(r"[<>\",]", " ", str(p or "").replace("​", " "))
    return " ".join(p.split()).strip(" .;:|#'-")


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.isascii() and t.endswith("s") and not t.endswith("ss") else t


def stems(text) -> set:
    """Every word of a text, singular/plural folded together."""
    return {_stem(t) for t in tokens(text)}


def topic_stems(text) -> set:
    """The topic words of a text, singular/plural folded together."""
    return {_stem(t) for t in _sig(tokens(text))}


def _compact(p) -> str:
    return "".join(_stem(t) for t in tokens(p))


def dupkey(p) -> str:
    """Same words in any order, singular or plural -> the same key."""
    return " ".join(sorted({_stem(t) for t in tokens(p)} - {"a", "an", "the"}))


class _Seen:
    """Drops duplicates and near-duplicates (word order, plural, 'street food' / 'streetfood')."""

    def __init__(self):
        self.keys, self.compact = set(), set()

    def add(self, p) -> bool:
        k, c = dupkey(p), _compact(p)
        if not k or k in self.keys or c in self.compact:
            return False
        self.keys.add(k)
        self.compact.add(c)
        return True


def hashtag(p) -> str | None:
    """'butter chicken recipe' -> 'butterchickenrecipe' (None when it would not make a good hashtag)."""
    h = "".join(tokens(p))
    return h if 3 <= len(h) <= 30 and not h.isdigit() else None


# ---------- risky words, labels, other creators ----------
@lru_cache(maxsize=1)
def _patterns():
    from .permissions import LABELS
    from .youtube import RISKY

    names = [w.strip() for w in LABELS.pattern.split("|") if w.strip() and w.strip().lower() not in _GENERIC_LABEL_WORDS]
    label = re.compile(r"(?<![^\W_])(?:" + "|".join(re.escape(n) for n in names) + r")(?![^\W_])", re.I)
    return RISKY, label


def _bad(phrase: str, own_words: set) -> bool:
    """Risky (label / film / song words), spammy or misleading - unless the video's own title uses the word."""
    toks = set(tokens(phrase))
    if toks & DROP or (toks & MISLEADING) - own_words:
        return True
    risky, label = _patterns()
    for rx in (risky, label):
        for m in rx.finditer(phrase):
            if not set(tokens(m.group(0))) <= own_words:
                return True
    return False


def _is_creator(phrase: str, creators: dict, vocab: set) -> bool:
    """Contains the name of another creator the app knows (and the name is not simply the topic itself)."""
    c = _compact(phrase)
    return any(name in c and not words <= vocab for name, words in creators.items())


def _junk_tag(k: str) -> bool:
    c = "".join(tokens(k))
    return not c or c in JUNK_TAGS or (c.isdigit() and len(c) != 4)


# ---------- what is trending now (data the app already saved; no request) ----------
def trending_now() -> dict:
    """{"items": [{"title", "tags", "words"}], "creators": {compact name: its words}} from the viral board,
    the competitor tracker and the India trending lists."""
    items, creators = [], {}

    def add(title, tags, channel):
        title, tags = str(title or ""), [str(t) for t in (tags or []) if t]
        if title or tags:
            items.append({"title": title, "tags": tags, "words": set(tokens(title + " " + " ".join(tags)))})
        name = _compact(channel)
        if len(name) >= 5:
            creators[name] = set(tokens(channel))

    try:
        for day in json.loads(get_setting("viral_days") or "[]")[-2:]:
            board = json.loads(get_setting("viral:" + day) or "{}") or {}
            for bucket in ("videos", "shorts"):
                for v in (board.get(bucket) or {}).values():
                    add(v.get("title"), [], v.get("channel"))
    except Exception:  # noqa: BLE001
        pass
    try:
        comp = json.loads(get_setting("competitors") or "{}") or {}
        for v in comp.get("outliers") or []:
            add(v.get("title"), [], v.get("channel"))
        for c in comp.get("channels") or []:
            add("", [], c.get("name"))
    except Exception:  # noqa: BLE001
        pass
    try:
        with SessionLocal() as s:
            rows = [r.value for r in s.query(Setting).filter(Setting.key.like("trending:%")).all()]
        for raw in rows:
            for v in (json.loads(raw or "{}") or {}).get("items") or []:
                add(v.get("title"), v.get("tags"), v.get("channel"))
    except Exception:  # noqa: BLE001
        pass
    return {"items": items, "creators": creators}


# ---------- one cached request ----------
def _get(q: str, hl: str, gl: str) -> list[str]:
    """The only network call in this module (tests replace it). Up to 10 suggestions, most typed first."""
    if os.getenv("KEYWORDS_OFFLINE"):
        raise RuntimeError("keyword network switched off")
    r = httpx.get(URL, params={"client": "firefox", "ds": "yt", "q": q, "hl": hl, "gl": gl}, timeout=TIMEOUT,
                  headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()
    return [str(x) for x in data[1]][:10] if isinstance(data, list) and len(data) > 1 else []


def _ckey(q: str, hl: str) -> str:
    return "kwc:" + hashlib.sha1(f"{REGION}|{hl}|{q}".encode()).hexdigest()[:32]


def _cache_get(q: str, hl: str):
    """(suggestions, age in hours) or None."""
    try:
        d = json.loads(get_setting(_ckey(q, hl)) or "")
        return list(d["items"]), (utcnow() - datetime.fromisoformat(d["at"])).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return None


def _cache_put(q: str, hl: str, items: list):
    try:
        set_setting(_ckey(q, hl), json.dumps({"at": utcnow().isoformat(), "q": q, "hl": hl, "items": items},
                                             ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def purge_cache() -> int:
    """Delete cached suggestions older than a week (called by the timed job)."""
    cut, n = utcnow() - timedelta(days=KEEP_DAYS), 0
    with SessionLocal() as s:
        for row in s.query(Setting).filter(Setting.key.like("kwc:%")).all():
            try:
                old = datetime.fromisoformat(json.loads(row.value)["at"]) < cut
            except Exception:  # noqa: BLE001
                old = True
            if old:
                s.delete(row)
                n += 1
        s.commit()
    return n


class _Run:
    """One research: a small request budget; it stops asking after two failures in a row."""

    def __init__(self, depth: str = "clip", fresh: bool = False):
        self.left, self.fresh, self.fails = BUDGET.get(depth, BUDGET["clip"]), fresh, 0
        self.ok = self.stale = self.asked = 0

    def fetch(self, q: str, hl: str = "en") -> list[str]:
        global _down_until
        q = _norm(q)[:120]
        if not q:
            return []
        hit = _cache_get(q, hl)
        if hit and hit[1] < (1.0 if self.fresh else CACHE_HOURS):
            self.ok += 1
            return hit[0]
        if self.left <= 0 or self.fails >= 2 or time.monotonic() < _down_until:
            self.stale += 1 if hit else 0
            return hit[0] if hit else []
        self.left -= 1
        self.asked += 1
        try:
            items = _get(q, hl, REGION)
        except Exception as e:  # noqa: BLE001
            self.fails += 1
            if self.fails >= 2:
                _down_until = time.monotonic() + 600
            log.info("keywords: no answer for a suggestion request (%s)", str(e)[:80])
            self.stale += 1 if hit else 0
            return hit[0] if hit else []
        self.fails = 0
        self.ok += 1
        _cache_put(q, hl, items)
        if self.asked > 1:
            time.sleep(0.1)  # be polite
        return items

    def state(self) -> str:
        """ok = real answers (new or cached today) | stale = old cached answers only | offline = nothing."""
        return "ok" if self.ok else ("stale" if self.stale else "offline")


def _rank(results: list) -> list[dict]:
    """[(weight, suggestions)] -> phrases with a score: earlier in a list and seen in more lists = stronger."""
    table: dict[str, dict] = {}
    for weight, items in results:
        for pos, it in enumerate(items[:10]):
            k = _norm(it)
            key = dupkey(k)
            if not key:
                continue
            e = table.setdefault(key, {"k": k, "score": 0.0, "n": 0})
            e["score"] += weight * (10 - pos) / 10
            e["n"] += 1
    out = sorted(table.values(), key=lambda e: -e["score"])
    top = out[0]["score"] if out else 1.0
    for e in out:
        e["score"] = e["score"] / top
    return out


# ---------- the research tool (Ideas page): a topic typed by the owner ----------
def research(topic: str, depth: str = "full", fresh: bool = False) -> dict:
    topic = _norm(topic)[:120]
    out = {"topic": topic, "keywords": [], "groups": {"top": [], "questions": [], "long": [], "hindi": []},
           "trending": [], "net": "offline"}
    if not topic:
        return out
    run = _Run(depth, fresh)
    india = REGION == "IN"
    qs = [(1.0, topic, "en")] + ([(0.9, topic, "hi")] if india else [])
    if depth == "full":
        hi = "hi" if india else "en"
        qs += [(0.6, "how to " + topic, "en"), (0.6, "best " + topic, "en"), (0.5, "why " + topic, "en"),
               (0.5, topic + " vs", "en")]
        qs += [(0.6, topic + " in hindi", hi), (0.6, topic + " kaise", hi)] if india else []
        qs += [(0.25, f"{topic} {c}", "en") for c in "abchrs"]  # the long tail: counts little for the order
    ranked = _rank([(w, run.fetch(q, hl)) for w, q, hl in qs])
    own = set(tokens(topic))
    trend = trending_now()
    seen, words = _Seen(), []
    for e in ranked:
        k = e["k"]
        if len(k) > 80 or _bad(k, own) or _is_creator(k, trend["creators"], own) or not seen.add(k):
            continue
        words.append(k)
    out["keywords"], out["net"] = words[:60], run.state()
    for k in out["keywords"]:
        tk = tokens(k)
        if tk and (tk[0] in QUESTION or set(tk) & (QUESTION - NEUTRAL | {"kaise", "kya", "kyu", "kyun", "kyon"})):
            out["groups"]["questions"].append(k)
        if DEVA.search(k) or set(tk) & HINGLISH - {"ka", "ki", "ke", "mein"} or len(set(tk) & HINGLISH) >= 2:
            out["groups"]["hindi"].append(k)
        if len(tk) >= max(4, len(own) + 2):
            out["groups"]["long"].append(k)
    out["groups"]["top"] = out["keywords"][:12]
    # the same topic on today's trending lists (their tags; nothing is asked from YouTube for this)
    need, tseen = set(_sig(tokens(topic))), _Seen()
    for it in trend["items"]:
        if need and len(need & it["words"]) >= min(2, len(need)):
            for t in it["tags"]:
                t = _tidy(t).lower()
                if t and not _junk_tag(t) and not _bad(t, own) and set(_sig(tokens(t))) & need and tseen.add(t):
                    out["trending"].append(t)
    out["trending"] = out["trending"][:12]
    return out


def suggest(seed: str, limit: int = 25, expand: int = 8) -> list[str]:
    """Search suggestions for a topic, most typed first (kept for older callers)."""
    return research(seed, "full" if expand > 2 else "clip")["keywords"][:limit]


# ---------- keywords for one video ----------
def topic_words(title: str, channel: str = "") -> list[str]:
    """The words of the part of a title that names the topic (first part with two or more topic words)."""
    chan = _compact(channel)
    parts = [p for p in re.split(r"\s+[-–—]\s+|[|•·:()\[\]{}!?।\"“”]+", re.sub(r"#\S+", " ", str(title or "")))
             if p.strip()]
    best = None
    for p in parts:
        tk = tokens(p)
        if chan and _compact(p) == chan:
            continue
        if len(_sig(tk)) >= 2:
            best = tk
            break
        best = best or (tk if _sig(tk) else None)
    words = (best or tokens(title))[:5]
    while len(words) > 1 and words[-1] in NEUTRAL:
        words.pop()
    return words


def plan(title: str, tags=None, description: str = "", channel: str = "", extra=None, hint: str = "",
         fresh: bool = False, depth: str = "clip") -> dict:
    """Choose the keywords for one video. Never raises; with no network the video's own tags are used.

    Returns {"at", "lead", "top", "hashtags", "items": [{"k", "src": [...], "hot"}], "net", "hint", "topic"}:
      lead      the strongest phrase people type that this video is really about (None when unknown)
      top       the few strongest phrases, for the first lines of the description
      items     every chosen keyword, strongest first; src = typed | video | trending | extra
    """
    title, description, hint = str(title or ""), str(description or ""), _norm(hint)[:120]
    tags = [str(t) for t in (tags or []) if t]
    own_words = set(tokens(title)) | set(tokens(hint))
    core = set(_sig(own_words))
    chan = _compact(channel)
    trend = trending_now()
    creators = {n: w for n, w in trend["creators"].items() if n != chan}

    # 1. the video's own keywords: every tag and every hashtag in its description, minus junk
    own, seen = [], _Seen()
    hashtags = [re.sub(r"(?<=[a-z])(?=[A-Z])", " ", h.replace("_", " ")) for h in _HASHTAG.findall(description)]
    for raw in tags + hashtags:
        k = _tidy(raw)
        if (not k or len(k) > 60 or len(tokens(k)) > 7 or _junk_tag(k) or _bad(k, own_words) or _compact(k) == chan
                or _is_creator(k, creators, core) or not seen.add(k)):
            continue
        own.append(k)
    ext = core | {t for k in own for t in _sig(tokens(k))}
    if DEVA.search(title) or ext & {"hindi", "हिंदी", "हिन्दी"}:
        ext |= {"hindi", "हिंदी", "हिन्दी"}
    ext.add(str(utcnow().year))

    # 2. what people type: the title's topic (longest form that people really type), its Hindi-region ranking,
    #    the creator's first tags and the owner's own hint as seeds
    run, results = _Run(depth, fresh), []
    words = topic_words(title, channel) or tokens(hint)[:5]
    full = " ".join(words)
    working = full
    if full:
        a = run.fetch(full)
        results.append((1.0, a))
        b = run.fetch(" ".join(words[:3])) if len(words) >= 4 else []
        results.append((0.9, b))
        if len(a) < 3 and len(b) >= 3:
            working = " ".join(words[:3])
        elif len(a) < 3 and len(words) > 2:
            c = run.fetch(" ".join(words[:2]))
            results.append((0.8, c))
            working = " ".join(words[:2]) if c else full
        if REGION == "IN":
            results.append((0.8, run.fetch(working, "hi")))
            if "hindi" in ext and "hindi" not in tokens(working) and not DEVA.search(working):
                results.append((0.6, run.fetch(working + " in hindi", "hi")))
    if hint and hint != full:
        results.append((1.0, run.fetch(hint)))
    seeds = [k for k in own if 1 < len(tokens(k)) <= 5 or (len(tokens(k)) == 1 and len(k) >= 4)]
    seeds = [k for k in seeds if dupkey(k) not in (dupkey(full), dupkey(working))][:3]
    for k in seeds:
        results.append((0.7, run.fetch(k.lower())))
    anchor = core | {t for k in seeds for t in _sig(tokens(k))}

    table: dict[str, dict] = {}

    def put(k, src, score, hot=False):
        key = dupkey(k)
        e = table.get(key)
        if e is None:
            cmp = _compact(k)
            e = next((x for x in table.values() if x["c"] == cmp), None)
        if e is None:
            e = table[key] = {"k": k, "c": _compact(k), "src": [], "score": 0.0, "typed": 0.0, "hot": False}
        if src not in e["src"]:
            e["src"].append(src)
        e["score"] += score
        e["typed"] += score if src == "typed" else 0.0
        e["hot"] = e["hot"] or hot
        return e

    for e in _rank(results):
        k = e["k"]
        sg = set(_sig(tokens(k)))
        if (not sg or len(k) > 60 or len(tokens(k)) > 8 or sg - ext or not sg & anchor or _bad(k, own_words)
                or _is_creator(k, creators, ext)):
            continue  # not about this video, another creator's name, or risky
        put(k, "typed", e["score"] * (0.55 + 0.45 * len(sg & core) / len(sg)))
    # every tag of the source video - but a tag far down the creator's list that shares no word with the title, the
    # creator's first tags or anything people type about this video is left out (tag lists are often stuffed with
    # other topics, and unrelated tags are against YouTube's rules)
    linked = anchor | {t for e in table.values() for t in _sig(tokens(e["k"]))} | \
        {t for k in own[:TRUSTED_TAGS] for t in _sig(tokens(k))}
    for i, k in enumerate(own):
        sg = set(_sig(tokens(k)))
        strong = bool(sg & core)
        if strong or i < TRUSTED_TAGS or sg & linked:
            put(k, "video", 0.8 * (1 - i / (len(own) + 2)) * (1.0 if strong else 0.45))

    # 3. trending now: mark what is also on today's lists, and add their tags when they are about this video
    for it in trend["items"]:
        for t in it["tags"]:
            t = _tidy(t).lower()
            sg = set(_sig(tokens(t)))
            if (t and sg and len(t) <= 60 and not _junk_tag(t) and sg & core and len(sg - ext) <= 1
                    and len(sg & core) * 2 >= len(sg) and not _bad(t, own_words) and not _is_creator(t, creators, ext)):
                put(t, "trending", 0.0, True)
    for e in table.values():
        sg = set(_sig(tokens(e["k"])))
        if not e["hot"] and (len(sg) >= 2 or (len(sg) == 1 and len(next(iter(sg))) >= 5)):
            e["hot"] = any(sg <= it["words"] for it in trend["items"])
        if e["hot"]:
            e["score"] = e["score"] * 1.25 + 0.3
            if "trending" not in e["src"]:
                e["src"].append("trending")
    try:  # words that do best on this channel (Ideas -> What works, auto-tune) count a little more; only phrases
        from .learn import win_words  # already chosen for this video are touched, so nothing off-topic comes in

        best = set(win_words())
    except Exception:  # noqa: BLE001
        best = set()
    if best:
        for e in table.values():
            if topic_stems(e["k"]) & best:
                e["score"] *= 1.2
                e["typed"] *= 1.2
    for i, k in enumerate(extra or []):  # the owner's own picks always lead
        k = _tidy(k)
        if k and not _bad(k, own_words | set(tokens(k))):
            put(k, "extra", 3.0 - i * 0.01)

    ranked = sorted(table.values(), key=lambda e: -e["score"])[:MAX_ITEMS]

    # the title's lead phrase: the most typed one (tags and trending do not count here), made only of this video's
    # own words, 2-6 words
    lead = None
    for e in sorted(ranked, key=lambda e: -e["typed"]):
        tk = tokens(e["k"])
        sg = set(_sig(tk))
        if e["typed"] > 0 and sg and sg <= anchor and len(e["k"]) <= 45 and (2 <= len(tk) <= 6 or len(words) == 1):
            lead = e["k"]
            break
    top, said = ([lead] if lead else []), topic_stems(lead or "")
    for e in ranked:  # the next strongest phrases that say something new (for the description's first lines)
        sg = topic_stems(e["k"])
        strong = "typed" in e["src"] or "extra" in e["src"] or ("video" in e["src"] and set(_sig(tokens(e["k"]))) & anchor)
        if len(top) < 4 and strong and sg - said and len(e["k"]) <= 45:
            top.append(e["k"])
            said |= sg
    tags_out, hseen = [], set()
    for e in ranked:
        h = hashtag(e["k"]) if len(tokens(e["k"])) <= 3 else None
        if h and h not in hseen and h not in JUNK_TAGS and len(tags_out) < 4:
            hseen.add(h)
            tags_out.append(h)
    return {"v": 1, "at": utcnow().isoformat() + "Z", "topic": working, "hint": hint, "lead": lead, "top": top,
            "hashtags": tags_out, "net": run.state(),
            "items": [{"k": e["k"], "src": e["src"], "hot": bool(e["hot"])} for e in ranked]}


def explain(items: list, net: str) -> str:
    """One plain line for the dashboard: what the system did."""
    used = [i for i in items if i.get("used", True)]
    typed = sum(1 for i in used if "typed" in i["src"])
    video = sum(1 for i in used if "video" in i["src"])
    hot = sum(1 for i in used if i.get("hot"))
    if net == "offline" or not typed:
        return ("YouTube's search suggestions could not be reached, so the original video's own keywords were used"
                if net == "offline" else "Nothing people type matched this video closely, so its own keywords were used") \
            + (f" ({video})" if video else "") + ". The system tries again by itself before the upload."
    return (f"Chosen by the system: {typed} phrase{'s' if typed != 1 else ''} people type in YouTube search (most typed first)"
            + (f", {video} from the original video's own tags" if video else "")
            + (f", {hot} trending now" if hot else "") + ".")

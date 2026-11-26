import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import update

from . import keywords as kw
from .db import Clip, Project, SessionLocal, get_setting, set_setting, utcnow
from .keywords import suggest  # noqa: F401  (older callers and tests reach it through this module)

log = logging.getLogger(__name__)

TAG_LIMIT = 500  # YouTube: all tags of a video together, counted its own way (see tag_chars)
MAX_TAGS = 30
MAX_HASHTAGS = 15  # with more than 15 hashtags YouTube ignores every hashtag on the video
STALE_HOURS = 24  # keywords older than this are researched again before the upload
SOON_HOURS = 6  # the timed job looks at clips that go up within this many hours
META_FIELDS = ("title", "description", "tags")
_HASH = re.compile(r"#(?=[^\W_]|[ऀ-ॣ०-෿])")

# Title language of a channel (My channels → the channel → Title language). The credit lines, the licence line and
# the original description stay in English whatever is chosen; only the title's hook, the description's first
# lines and the hashtags follow the language. Plain templates: no translation service, no AI.
LANGS = ("en", "hinglish", "hi")
LANG_NAMES = {"en": "English", "hinglish": "Hinglish", "hi": "Hindi"}
HOOK = {"en": "Best Moment", "hinglish": "Best Moment Dekho", "hi": "सबसे बढ़िया पल"}
LANG_TAGS = {"en": [], "hinglish": ["hindi"], "hi": ["हिंदी", "hindi"]}
INTRO = {  # {p} = the strongest phrase, {who} = the credited creator
    "hinglish": {"original": "{p}.", "own": "{p} — meri full video ka sabse best moment.",
                 "who": "{p} — {who} ki video ka sabse best moment.", "none": "{p} — full video ka sabse best moment.",
                 "about": "Is video mein: {x}.", "and": " aur "},
    "hi": {"original": "{p}।", "own": "{p} — मेरी पूरी वीडियो का सबसे बढ़िया पल।",
           "who": "{p} — {who} की वीडियो का सबसे बढ़िया पल।", "none": "{p} — पूरी वीडियो का सबसे बढ़िया पल।",
           "about": "इस वीडियो में: {x}।", "and": " और "},
}


def lang_of(clip) -> str:
    """The title language of the channel the clip goes to ("en" when unknown)."""
    did = getattr(clip, "destination_id", None)
    if not did:
        return "en"
    try:
        from .db import Destination

        with SessionLocal() as s:
            d = s.get(Destination, did)
            v = (d.title_lang if d else None) or "en"
    except Exception:  # noqa: BLE001
        return "en"
    return v if v in LANGS else "en"


def _hook_line(project: Project, lang: str) -> str:
    """The description's first line on a Hindi / Hinglish channel when no keyword research is there to lead it."""
    if lang == "en":
        return ""
    t = INTRO[lang]
    p, who = _clean(project.title).strip(), _clean(project.channel).strip()
    if project.mode == "original":
        return t["original"].format(p=p)
    if project.mode == "own":
        return t["own"].format(p=p)
    return t["who"].format(p=p, who=who) if who else t["none"].format(p=p)


LICENSE_LINE = {
    "cc": "License: Creative Commons Attribution (CC BY). Changes made: clipped and edited.",
    "permission": "Used with the creator's permission.",
    "commentary": "Edited with original commentary/transformation.",
}


def affiliate_lines(text: str) -> list[str]:
    """Owner's affiliate rules (keywords -> line) that match this video's words."""
    import json as _json

    from .db import get_setting

    try:
        rules = _json.loads(get_setting("affiliate_rules") or "[]")
    except ValueError:
        return []
    words = set(re.findall(r"\w+", (text or "").lower()))
    out = []
    for r in rules:
        keys = [k.strip().lower() for k in (r.get("keywords") or "").split(",") if k.strip()]
        if r.get("text") and any((set(k.split()) <= words) for k in keys):
            out.append(r["text"].strip())
    return out[:5]


def _with_affiliates(meta: dict, text: str) -> dict:
    lines = affiliate_lines(text)
    if lines:
        meta["description"] = _cut(meta["description"] + "\n\n" + "\n".join(lines)
                                   + "\n(Some links are affiliate links — I may earn a small commission.)", 4900)
    return meta


def _cut(s: str, n: int) -> str:
    return s.encode()[:n].decode(errors="ignore")  # YouTube limits description by bytes


def _clean(s: str) -> str:
    return (s or "").replace("<", "").replace(">", "")


def _build_original(project: Project, clip: Clip, keywords: list[str], lang: str = "en") -> dict:
    """My own reel: no credit block, the script itself is the description."""
    title = _clean(project.title)[:100]
    shorts = clip.aspect == "9:16"
    hashtags = " ".join((["#shorts"] if shorts else []) + ["#" + re.sub(r"\W", "", k) for k in keywords[:3] if k]
                        + ["#" + h for h in LANG_TAGS.get(lang, [])])
    hook = _hook_line(project, lang) if lang != "en" and project.mode != "original" else ""
    description = _cut(((hook + "\n" if hook else "") + hashtags + "\n\n" + _clean(project.description)).strip(), 4900)
    tags, total = [], 0
    for t in keywords + (["shorts"] if shorts else []):
        t = _clean(t).strip()
        if t and t.lower() not in (x.lower() for x in tags) and total + len(t) + 3 <= 480:
            tags.append(t)
            total += len(t) + 3
    return {"title": title, "description": description, "tags": tags}


def build(project: Project, clip: Clip, keywords: list[str], lang: str | None = None) -> dict:
    """lang None = the title language of the clip's channel (English unless the owner chose Hinglish / Hindi)."""
    lang = lang_of(clip) if lang is None else (lang if lang in LANGS else "en")
    words = " ".join([project.title or "", " ".join(project.tags or []), " ".join(keywords)])
    if project.mode == "original":
        return _with_affiliates(_build_original(project, clip, keywords, lang), words)
    if project.mode == "own":  # my own long video -> Short: link back to the full video, no credit block
        meta = _build_original(project, clip, keywords, lang)
        meta["title"] = (_clean(project.title) + " | " + HOOK[lang])[:100]
        meta["description"] = _cut(meta["description"].split("\n\n")[0] + f"\n\nFull video: https://youtu.be/{project.video_id}"
                                    + "\n\n" + _clean(project.description)[:1500], 4900)
        return _with_affiliates(meta, words)
    return _with_affiliates(_build(project, clip, keywords, lang), words)


def _credit_block(project: Project, clip: Clip) -> str:
    """Credit, source link, licence note and the original description: the part nothing else may change."""
    return "\n".join([
        f"Original video: {_clean(project.title)}",
        f"Credit: {_clean(project.channel)} - {project.channel_url}",
        f"Source: https://youtu.be/{project.video_id}?t={int(clip.start)}",
        LICENSE_LINE.get(project.mode, ""),
        "",
        "--- Original description ---",
        _clean(project.description)[:1500],
    ])


def _build(project: Project, clip: Clip, keywords: list[str], lang: str = "en") -> dict:
    base = _clean(project.title)
    title = f"{base} | {HOOK[lang]}"
    if len(title) > 100:
        title = base[:100]
    shorts = clip.aspect == "9:16"
    tag_src = [t for t in (list(project.tags or [])[:3] + keywords) if t][:3]
    hashtags = " ".join((["#shorts"] if shorts else []) + ["#" + re.sub(r"\W", "", t) for t in tag_src]
                        + ["#" + h for h in LANG_TAGS[lang]])
    hook = [_hook_line(project, lang)] if lang != "en" else []
    description = _cut("\n".join(hook + [hashtags, "", _credit_block(project, clip)]).strip(), 4900)

    tags, total = [], 0
    extra = [project.channel] + (["shorts"] if shorts else [])
    for t in list(project.tags or []) + keywords + extra:
        t = _clean(t).strip()
        if not t or t.lower() in (x.lower() for x in tags):
            continue
        if total + len(t) + 3 > 480:
            break
        tags.append(t)
        total += len(t) + 3
    return {"title": title, "description": description, "tags": tags}


# ---------- the keyword helper: title, description and tags written from researched keywords ----------
def tag_chars(tags) -> int:
    """Length of a tag list the way YouTube counts its 500 limit: the commas between tags count, and a tag with a
    space counts two more (YouTube treats it as wrapped in quotation marks)."""
    tags = [t for t in tags if t]
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def _tag(t) -> str:
    return " ".join(_clean(str(t or "")).replace(",", " ").replace('"', " ").split())


def _fit_tags(ranked: list, last: list) -> list[str]:
    """Strongest first, inside YouTube's limit. `last` (the credited channel, 'shorts') always keeps its place at
    the end; a keyword that does not fit is skipped so that a shorter one still can."""
    seen, tail, out = set(), [], []
    for t in map(_tag, last):
        if t and t.lower() not in seen:
            seen.add(t.lower())
            tail.append(t)
    room = TAG_LIMIT - tag_chars(tail) - (1 if tail else 0)
    for t in map(_tag, ranked):
        if not t or t.lower() in seen:
            continue
        if len(out) + len(tail) >= MAX_TAGS:
            break
        if tag_chars(out + [t]) > room:
            continue
        seen.add(t.lower())
        out.append(t)
    return out + tail


def _nice(phrase: str, title: str, caps: bool = True) -> str:
    """A search phrase spelled the way the source title spells each word (GTA stays GTA). caps=True writes the
    other words like a title; caps=False leaves them as typed (for use inside a sentence)."""
    spell = {}
    for w in str(title or "").split():
        w = w.strip(".,;:!?|()[]{}\"'“”‘’#-–—")
        spell.setdefault(w.lower(), w)
    out = []
    for i, w in enumerate(phrase.split()):
        out.append(spell[w] if w in spell else w if not caps or (i and w in kw.SMALL) else w[:1].upper() + w[1:])
    return " ".join(out)


def _is_own_upload(project: Project) -> bool:
    return project.mode == "original" and (project.license or "").endswith("(my own video)")


def _lead_title(project: Project, plan: dict, today: str) -> str:
    """Lead with the strongest phrase people search for - unless the title's first 60 characters already say it,
    or the owner typed the title himself (his own uploaded video)."""
    lead, base = plan.get("lead"), _clean(project.title)
    if not lead or not base or _is_own_upload(project):
        return today
    if kw.topic_stems(lead) <= kw.stems(base[:60]):
        return today
    head, want = _nice(_clean(lead), base), kw.topic_stems(lead)
    parts = re.split(r"\s+[|–—-]\s+", base)
    if any(kw.topic_stems(p) == want for p in parts):  # the same phrase stands later in the title: say it once
        base = " | ".join(p for p in parts if kw.topic_stems(p) != want)
    room = 100 - len(head) - 3
    rest = base if len(base) <= room else base[:room + 1].rsplit(" ", 1)[0].rstrip(" |-:,.")
    return (f"{head} | {rest}" if rest else head)[:100]


def _intro(project: Project, plan: dict, lang: str = "en") -> list[str]:
    """The first two lines of the description: the strongest phrases, in plain sentences (in the channel's language)."""
    top = [_nice(_clean(t).strip(), _clean(project.title), caps=False) for t in (plan.get("top") or []) if t]
    if not top:
        return [_hook_line(project, lang)] if lang != "en" else []
    p1, who = top[0][:1].upper() + top[0][1:], _clean(project.channel).strip()
    if lang != "en":
        t = INTRO[lang]
        first = t["original" if project.mode == "original" else "own" if project.mode == "own" else "who" if who else "none"]
        return [first.format(p=p1, who=who)] + ([t["about"].format(x=t["and"].join(top[1:3]))] if top[1:] else [])
    if project.mode == "original":
        first = f"{p1}."
    elif project.mode == "own":
        first = f"{p1} — the best moment from my full video."
    else:
        first = f"{p1} — the best moment from this video by {who}." if who else f"{p1} — the best moment from the full video."
    return [first] + ([f"This video is about {' and '.join(top[1:3])}."] if top[1:] else [])


def _cap_hashtags(text: str, allowed: int) -> str:
    """Cut a copied description before the hashtag that would take the video over YouTube's limit."""
    marks = [m.start() for m in _HASH.finditer(text)]
    return text if len(marks) <= allowed else text[:marks[max(0, allowed)]].rstrip()


def compose(project: Project, clip: Clip, plan: dict, lang: str | None = None) -> dict:
    """Title, description and tags from a keyword plan (keywords.plan). The credit lines, the licence note and the
    original description are the same lines build() writes; only what stands before them is new:
        two plain lines with the strongest phrases / 3-5 hashtags / the unchanged block."""
    lang = (plan.get("lang") or lang_of(clip)) if lang is None else lang
    lang = lang if lang in LANGS else "en"
    items = plan.get("items") or []
    ranked = [i["k"] for i in items]
    shorts = clip.aspect == "9:16"
    title = _lead_title(project, plan, build(project, clip, [], lang)["title"])
    hook = HOOK[lang]
    if lang != "en" and hook not in title and len(title) + 3 + len(hook) <= 100:  # the title also says it in the language
        title = f"{title} | {hook}"
    if project.mode == "original":
        fixed, copied = _clean(project.description), ""
    elif project.mode == "own":
        fixed, copied = f"Full video: https://youtu.be/{project.video_id}\n\n", _clean(project.description)[:1500]
    else:
        copied = _clean(project.description)[:1500]
        block = _credit_block(project, clip)
        fixed = block[:len(block) - len(copied)]
    intro = _intro(project, plan, lang)
    taken = len(_HASH.findall(title)) + len(_HASH.findall("\n".join(intro))) + len(_HASH.findall(fixed))
    own_tags = [h for h in LANG_TAGS[lang] if h not in (plan.get("hashtags") or [])]
    mine = ((["shorts"] if shorts else []) + list(plan.get("hashtags") or [])[:3 if shorts else 4]
            + own_tags)[:max(0, MAX_HASHTAGS - taken)]
    copied = _cap_hashtags(copied, MAX_HASHTAGS - taken - len(mine))
    parts = intro + ([""] if intro else []) + ([" ".join("#" + h for h in mine), ""] if mine else []) + [fixed + copied]
    description = _cut("\n".join(parts).strip(), 4900)
    last = ([] if project.mode in ("original", "own") else [project.channel]) + (["shorts"] if shorts else [])
    tags = _fit_tags(ranked, last)
    used = {t.lower() for t in tags}
    for i in items:
        i["used"] = _tag(i["k"]).lower() in used
    words = " ".join([project.title or "", " ".join(project.tags or []), " ".join(ranked)])
    return _with_affiliates({"title": title, "description": description, "tags": tags}, words)


def propose(clip_id: int, extra_keywords: list[str] | None = None, hint: str | None = None, fresh: bool = False) -> dict:
    """Research the keywords of a clip and write title/description/tags from them WITHOUT saving anything.
    Returns {"title", "description", "tags", "kw": the record that is stored on the clip when it is used}."""
    with SessionLocal() as s:
        clip = s.get(Clip, clip_id)
        project = s.get(Project, clip.project_id)
    if hint is None:
        hint = (clip.kw or {}).get("hint") or ""
    lang = lang_of(clip)
    try:
        plan = kw.plan(project.title, project.tags or [], project.description or "",
                       "" if project.mode == "original" else project.channel or "", extra=extra_keywords, hint=hint, fresh=fresh,
                       lang=lang)
        meta = compose(project, clip, plan, lang)
    except Exception:  # noqa: BLE001  (research must never stop a clip: fall back to credit + the video's own tags)
        log.exception("keywords: research failed for clip %s", clip_id)
        plan = {"at": utcnow().isoformat() + "Z", "lead": None, "hint": hint, "net": "offline", "items": []}
        meta = build(project, clip, list(extra_keywords or []), lang)
    meta["kw"] = {"v": 1, "at": plan["at"], "lead": plan.get("lead"), "hint": plan.get("hint") or "", "net": plan["net"],
                  "items": plan["items"], "note": kw.explain(plan["items"], plan["net"])}
    if lang != "en":
        meta["kw"]["lang"] = lang
    return meta


def auto_metadata(clip_id: int, extra_keywords: list[str] | None = None, hint: str | None = None, fresh: bool = False) -> dict:
    """Write title, description and tags from researched keywords and remember which keywords were chosen.
    After this the clip counts as written by the system again (not edited by hand)."""
    meta = propose(clip_id, extra_keywords, hint, fresh)
    with SessionLocal() as s:
        clip = s.get(Clip, clip_id)
        clip.title, clip.description, clip.tags = meta["title"], meta["description"], meta["tags"]
        clip.kw, clip.meta_edited = meta["kw"], False
        s.commit()
    return meta


def mark_hand_edit(clip: Clip, field: str, value):
    """The editor is about to change `field`: if it is the title, the description or the tags, remember that the
    owner wrote them himself. From then on the keyword helper never rewrites this clip (until 'Use these')."""
    if field not in META_FIELDS:
        return
    old = getattr(clip, field)
    if field == "tags":
        same = [str(t).strip() for t in (old or [])] == [str(t).strip() for t in (value or [])]
    else:
        text = lambda x: str(x or "").replace("\r\n", "\n").replace("\r", "\n").strip()  # noqa: E731
        same = text(old) == text(value)
    if not same:
        clip.meta_edited = True


def needs_refresh(clip: Clip, now: datetime | None = None) -> bool:
    """Keywords written by the system that are older than a day (or were made while YouTube could not be reached).
    A clip the owner edited by hand, and a clip from before the keyword helper existed, is never touched."""
    if clip.meta_edited or clip.youtube_id or clip.status in ("uploading", "uploaded"):
        return False
    rec = clip.kw or {}
    if rec.get("basis"):  # my own video, written from a search on its topic (app/owntopic.py): the owner checks it
        return False
    if not rec:
        return not clip.title  # nothing written yet -> write it; an older clip with a title may be hand-made
    if (rec.get("lang") or "en") != lang_of(clip):  # the channel's title language was changed since
        return True
    try:
        age = (now or utcnow()) - datetime.fromisoformat(str(rec.get("at") or "").rstrip("Z"))
    except ValueError:
        return True
    return age > timedelta(hours=STALE_HOURS) or (rec.get("net") == "offline" and age > timedelta(minutes=30))


def freshen_clip(clip_id: int, force: bool = False) -> bool:
    """Research again and rewrite title/description/tags of a clip that has not gone up yet. Never raises.
    True = rewritten. Nothing changes when the owner edited the clip, when the keywords are fresh, or when
    YouTube's suggestions cannot be reached (what is there stays, and it is tried again later)."""
    try:
        with SessionLocal() as s:
            clip = s.get(Clip, clip_id)
            if not clip or clip.meta_edited or clip.youtube_id or clip.status in ("uploading", "uploaded"):
                return False
            if not force and not needs_refresh(clip):
                return False
            status = clip.status
            extras = [i["k"] for i in ((clip.kw or {}).get("items") or []) if "extra" in (i.get("src") or [])]
        meta = propose(clip_id, extras) if extras else propose(clip_id)
        if meta["kw"]["net"] == "offline":
            return False
        with SessionLocal() as s:  # only if nothing happened meanwhile (an upload starting, an edit by hand)
            done = s.execute(update(Clip).where(Clip.id == clip_id, Clip.status == status, Clip.meta_edited.isnot(True),
                                                Clip.youtube_id.is_(None))
                             .values(title=meta["title"], description=meta["description"], tags=meta["tags"], kw=meta["kw"]))
            s.commit()
            return done.rowcount == 1
    except Exception:  # noqa: BLE001
        log.exception("keywords: could not refresh clip %s", clip_id)
        return False


def freshen_due(limit: int = 4) -> int:
    """Timed job: clips are often made days before they go up. The ones that will be sent to YouTube within the
    next few hours get their keywords researched again first (a few per run; hand-edited clips are left alone)."""
    from . import quota

    now = utcnow()
    horizon = now + timedelta(hours=quota.upload_lead_hours() + SOON_HOURS)  # uploads start that long before publish time
    with SessionLocal() as s:
        rows = s.query(Clip).filter(
            (Clip.status == "queued") | (Clip.autoqueue.is_(True) & Clip.status.in_(["fetching", "draft", "rendering"])),
            Clip.youtube_id.is_(None), Clip.meta_edited.isnot(True),
            (Clip.publish_at.is_(None)) | (Clip.publish_at <= horizon)).order_by(Clip.publish_at, Clip.id).all()
        # with a long lead everything waiting is inside the horizon: only the ones that go up in the next two days matter
        rows = rows[:max(8, quota.capacity() * 2)]
        due = [c.id for c in rows if needs_refresh(c, now)][:limit]
    done = sum(1 for cid in due if freshen_clip(cid))
    day = now.strftime("%Y-%m-%d")
    if get_setting("kwc_purged") != day:  # once a day: forget suggestions older than a week
        set_setting("kwc_purged", day)
        kw.purge_cache()
    return done

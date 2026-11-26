"""Strike responder: when YouTube removes one of my videos for a strike-like reason, protect the channel, ask the
claimant humbly to take a copyright complaint back, and prepare the words for YouTube.

What is possible, honestly:
* The YouTube API cannot list strikes, cannot name the claimant, and has NO call for an appeal or a counter-notification.
  Those exist only in YouTube Studio, and a counter-notification is a legal statement the owner must make in person.
  So YouTube itself is never answered from here: the text is prepared and the owner is told exactly where to paste it.
* A request to the CLAIMANT (the creator) to retract the complaint is an official way to clear a copyright strike.
  That mail can go out from the owner's own Gmail.

The one rule that keeps this safe while nobody watches: ONLY YouTube's own status of a video acts.
* The video monitor (app/monitor.py) saw the video change to rejected:<reason>, or a fresh videos.list shows it
  rejected. That is a confirmed case.
* A notice mail in the owner's Gmail (matched to a video or not) only opens / updates a case and tells the owner.
  It never blocks, holds, makes private or pauses anything by itself. Mails that are not strikes (Content ID claims,
  a removed comment, a shared video...) are ignored.
* The first run for an account records the rejected videos that are already there as history: only a CHANGE seen
  after that acts.

What happens for a confirmed case:
* copyright: the creator goes on the never-use list (setting "strike_blocked"), which every source type honours; the
  source(s) that point at that creator's channel are stopped; every waiting clip made from their videos gets the status
  "held" (no file is deleted); their clips that are on YouTube but scheduled (private, with a publish time) are set to
  private with no publish time, within the day's YouTube budget; uploads to the channel are paused.
* Community Guidelines / Terms of Use / trademark / legal: uploads to the channel are paused (as the monitor does) and
  the owner is told. The creator is NOT blocked by itself (they did not complain); the card offers
  "Stop using this creator".

The mail to the creator (copyright cases): account setting "strike_reply" = {"auto": bool}, default OFF. Even when ON,
a request goes out by itself only for a confirmed case, once per struck video (an atomic claim on
strike_cases.mail_state), at most 3 a day, and only when the address comes from a labelled claimant line of YouTube's
own notice and the claimant's name is the source creator's channel name. Every other address waits for the owner's
Send. The answer is read from the same Gmail thread.

Every round (strike_watch, strike_mails) sends the owner at most ONE summary message.
Nothing here keeps per-customer state in memory except a lock: sessions and settings follow the current customer.
"""
import copy
import json
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from sqlalchemy import Integer, String, cast, update
from sqlalchemy.exc import IntegrityError

from . import tenant
from .db import (Alert, Channel, Clip, Destination, Project, SessionLocal, Setting, StrikeCase, get_setting, set_setting,
                 utcnow)

log = logging.getLogger(__name__)

# what the video monitor saw -> the kind of case ("removed", claims and country blocks are not cases: see above)
KIND_OF_STATE = {"rejected:copyright": "copyright", "rejected:inappropriate": "guidelines", "rejected:termsOfUse": "guidelines",
                 "rejected:trademark": "other", "rejected:legal": "other"}
KIND_LABEL = {"copyright": "Copyright complaint", "guidelines": "Community Guidelines", "other": "Removed by YouTube"}
WAITING = ("draft", "fetching", "rendering", "rendered", "queued", "failed")  # not on YouTube yet -> "held"
HELD = "held"
HELD_NOTE = ("Held: the creator of this video is on the never-use list after a strike on your channel, so it will not be "
             "uploaded. Nothing was deleted. See Alerts → Strikes.")
PRIVATE_NOTE = ("Kept private on YouTube: the creator of this video is on the never-use list after a strike on your channel, "
                "so it will not go public. See Alerts → Strikes.")
BLOCKED_SKIP = "is on the never-use list (after a strike on your channel)"
BLOCK_KEY = "strike_blocked"  # {creator key: {name, url, at, case}}
SETTING = "strike_reply"  # {"auto": bool}
FAIL_KEY = "strike_private_fail"  # {clip id: {tries, error, at, next, title, youtube_id, told}}
SEEN_KEY = "strike_mail_seen"  # Gmail message ids already read
INIT_KEY = "strike_watch_init"  # {"at": first run, "history": {clip id: rejected state already there then}}
SENT_PREFIX = "strike_sent:"  # requests sent on one day (auto and by hand together)
AUTO_PER_DAY = 3  # requests sent by themselves in one day
HAND_PER_DAY = 10  # all requests in one day, the ones the owner sends by hand included
QUICK_TRIES, RETRY_SOON, RETRY_LATER = 3, timedelta(minutes=2), timedelta(hours=1)  # keeping a scheduled video private
FRESH = timedelta(hours=36)  # only a strike this new pauses the channel again (an old one was paused when it happened)
WATCH_REPLY_DAYS = 90
RETRACT_HELP = "https://support.google.com/youtube/answer/2807691"
STUDIO = "https://studio.youtube.com/"
MAIL_QUERY = ('from:youtube.com newer_than:90d '
              '{copyright strike takedown "community guidelines" removed violation warning}')
_UC = re.compile(r"(UC[\w-]{20,})")
_locks: dict[int, threading.Lock] = {}
_run = threading.local()  # the messages of the current round (one summary at its end)


def _iso(dt) -> str | None:
    return (dt.isoformat() + "Z") if isinstance(dt, datetime) else None


def _load(key: str, default):
    try:
        v = json.loads(get_setting(key) or "null")
    except (ValueError, TypeError):
        return default
    return v if isinstance(v, type(default)) else default


def _note(where: str, exc: BaseException):
    try:
        from .alerts import note

        note(where, exc)
    except Exception:  # noqa: BLE001  (reporting a problem must never cause one)
        pass


def _send(text: str):
    try:
        from .telegram import notify

        notify(text)
    except Exception as e:  # noqa: BLE001
        log.warning("strike notify: %s", str(e)[:200])


def _tell(text: str, extra: str = ""):
    """A message to the owner (Telegram / phone / own inbox). Inside a round it is collected, and the round sends ONE
    summary at its end. extra (a long text such as the ready request) is added only when it is the round's one item.
    Never raises."""
    lines = getattr(_run, "lines", None)
    if lines is not None:
        lines.append((text, extra))
        return
    _send(text + extra)


MSG_LIMIT = 3800


@contextmanager
def _one_message():
    """Everything told during this block goes out as one message (Telegram allows about 4000 characters)."""
    if getattr(_run, "lines", None) is not None:  # already inside a round: the outer one sends
        yield
        return
    _run.lines = []
    try:
        yield
    finally:
        lines, _run.lines = _run.lines, None
        if lines:
            if len(lines) == 1:
                _send(lines[0][0] + lines[0][1])
            else:
                lines = [t for t, _x in lines]
                lines.sort(key=lambda t: 0 if "Could NOT" in t else 1)  # an alarm is never cut off at the end
                head, out, left = f"🚨 <b>Strikes: {len(lines)} things to know</b>", [], 0
                size = len(head)
                for i, t in enumerate(lines):
                    if out and size + len(t) + 8 > MSG_LIMIT:
                        left = len(lines) - i
                        break
                    out.append(t)
                    size += len(t) + 8
                _send(head + "\n\n" + "\n\n— — —\n\n".join(out)
                      + (f"\n\n… and {left} more in Alerts → Strikes." if left else ""))


def _e(t) -> str:
    return str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- the account's choice
def settings() -> dict:
    raw = _load(SETTING, {})
    return {"auto": raw.get("auto") is True}


def save_settings(auto: bool) -> dict:
    set_setting(SETTING, json.dumps({"auto": bool(auto)}))
    return settings()


def settings_public() -> dict:
    """What the dashboard's switch row shows."""
    from . import acl, gmail

    off = next((k for k in ("strikes.reply", "gmail.send") if not acl.allowed(k)), None)
    allowed = off is None
    return {**settings(), "gmail": gmail.connected_email() or "", "allowed": allowed, "per_day": AUTO_PER_DAY,
            "sent_today": _sent_today(),
            "note": "" if allowed else acl.message(off)}


# ---------------------------------------------------------------- the never-use list
def creator_key(ref) -> str:
    """What a creator is known by: the channel id (UC…) when the address has one, else the tidy channel address."""
    t = str(ref or "").strip()
    m = _UC.search(t)
    if m:
        return m.group(1)
    t = re.sub(r"^https?://", "", t, flags=re.I)
    t = re.sub(r"^(www\.|m\.)", "", t, flags=re.I).split("?")[0].rstrip("/").lower()
    for suffix in ("/videos", "/featured", "/shorts", "/streams", "/about"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t[:200] if t.startswith("youtube.com/") and len(t) > len("youtube.com/") else ""


def blocked() -> dict:
    return _load(BLOCK_KEY, {})


def is_blocked(ref) -> bool:
    """Is this creator (a channel id or any address of their channel) on the never-use list?"""
    key = creator_key(ref)
    return bool(key) and key in blocked()


def blocked_message(name: str = "") -> str:
    return (f"{name or 'This creator'} {BLOCKED_SKIP}. Their videos are not used any more. To allow them again: "
            "Alerts → Strikes → “Use this creator again”.")


def _block(key: str, name: str, url: str, case_id: int):
    if not key:
        return
    bl = blocked()
    if key not in bl:
        bl[key] = {"name": name or "", "url": url or "", "at": _iso(utcnow()), "case": case_id}
        set_setting(BLOCK_KEY, json.dumps(bl))


def _unblock(key: str):
    bl = blocked()
    if bl.pop(key, None) is not None:
        set_setting(BLOCK_KEY, json.dumps(bl))


def _resync_blocked():
    """The list is also written in the cases: a creator who was blocked by a case that was not released ("Use this
    creator again") stays on it — even when the setting is lost (a backup restored on a new server brings the cases
    but not this setting), and even when an older case of the same creator was released before."""
    with SessionLocal() as s:
        rows = [(c.id, c.creator_key, c.data or {}) for c in s.query(StrikeCase).filter(StrikeCase.creator_key != "",
                                                                                      StrikeCase.creator_key.isnot(None))]
    if not rows:
        return
    bl = blocked()
    for cid, key, d in rows:
        if key not in bl and (d.get("did") or {}).get("blocked") and not d.get("released"):
            _block(key, d.get("source_channel") or "", d.get("source_channel_url") or "", cid)
            bl = blocked()


# ---------------------------------------------------------------- small helpers on a case
def _data(case) -> dict:
    return copy.deepcopy(case.data or {})


def _put(case, d: dict):
    case.data, case.updated_at = d, utcnow()  # a new object: the JSON column is saved


def _log(d: dict, text: str):
    d.setdefault("log", []).append({"at": _iso(utcnow()), "text": text})
    d["log"] = d["log"][-40:]


def _facts(s, c: Clip, p: Project | None) -> dict:
    dest = s.get(Destination, c.destination_id) if c.destination_id else None
    desc = c.description or ""
    credited = bool(p and ((p.video_id and p.video_id in desc) or (p.channel and p.channel.lower() in desc.lower())))
    lines = [x.strip() for x in desc.splitlines() if x.strip().lower().startswith(("original video:", "credit:", "source:"))]
    return {"title": c.title or (p.title if p else "") or f"Clip #{c.id}", "my_channel": dest.name if dest else "",
            "dest_id": c.destination_id, "uploaded_at": _iso(c.attempted_at), "publish_at": _iso(c.publish_at),
            "source_title": (p.title if p else "") or "", "source_channel": (p.channel if p else "") or "",
            "source_channel_url": (p.channel_url if p else "") or "",
            "source_url": (f"https://youtu.be/{p.video_id}" if p and p.video_id else (p.url if p else "")) or "",
            "source_mode": (p.mode if p else "") or "", "license": (p.license if p else "") or "",
            "permission_note": (p.permission_note if p else "") or "", "clip_range": [c.start, c.end],
            "credited": credited, "credit_lines": lines[:4], "source_found_at": _iso(p.created_at) if p else None}


def _creator_projects(s, key: str) -> list[int]:
    if not key:
        return []
    return [pid for pid, url in s.query(Project.id, Project.channel_url) if creator_key(url) == key]


def _chunks(ids: list, n: int = 400):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


STATE_WORDS = {"rejected:copyright": "removed for copyright", "rejected:inappropriate": "removed for the Community Guidelines",
               "rejected:termsOfUse": "removed for the Terms of Service", "rejected:trademark": "removed for a trademark complaint",
               "rejected:legal": "removed for a legal reason"}


def _new_case(s, c: Clip, kind: str, state: str, fresh: bool, confirmed: bool) -> StrikeCase:
    p = s.get(Project, c.project_id) if c.project_id else None
    d = _facts(s, c, p)
    d["fresh"], d["confirmed"] = bool(fresh), bool(confirmed)
    own = d["source_mode"] in ("original", "own")  # my own content: there is no other creator to stop or to write to
    case = StrikeCase(clip_id=c.id, youtube_id=c.youtube_id, kind=kind, state=state, status="open",
                      creator_key="" if own else creator_key(d["source_channel_url"]), mail_state="none",
                      created_at=utcnow(), updated_at=utcnow())
    if confirmed:
        _log(d, f"YouTube shows this video as {STATE_WORDS.get(state, 'removed')}.")
    case.data = d
    s.add(case)
    s.flush()
    return case


def _confirm(s, case, state: str, fresh: bool):
    """YouTube itself shows the video of a case (opened by a mail) as rejected: from now on it is a confirmed case."""
    d = _data(case)
    d["confirmed"], d["fresh"] = True, bool(fresh or d.get("fresh"))
    case.state, case.kind = state, KIND_OF_STATE[state]
    _log(d, f"YouTube itself now shows this video as {STATE_WORDS.get(state, 'removed')}.")
    if case.mail_state in (None, "", "none", "ready", "no_email"):  # not sent yet: written again with what is true now
        case.mail_state = "none"
        d.pop("mail", None)
        d.pop("mail_checked", None)
    _put(case, d)


# ---------------------------------------------------------------- what YouTube itself says (the only thing that acts)
def _history(s, changed) -> dict:
    """{clip id: state} of the rejections that were already there when the responder first ran for this account (they
    are history: never acted on). The first run writes it; a clip whose state changed since leaves it."""
    raw = _load(INIT_KEY, {})
    if raw.get("at"):
        hist = {str(k): v for k, v in (raw.get("history") or {}).items()}
        if hist:
            ids = [int(k) for k in hist if str(k).isdigit()]
            now = {}
            for part in _chunks(ids):
                now.update({str(i): st for i, st in s.query(Clip.id, Clip.yt_state).filter(Clip.id.in_(part))})
            # stays history while the video still shows that rejection or a passing state (removed for a while, a
            # check that did not see it); it leaves only when YouTube shows it fine again or rejected for a new reason
            kept = {k: v for k, v in hist.items()
                    if now.get(k) == v or (now.get(k) != "ok" and now.get(k) not in KIND_OF_STATE)}
            if kept != hist:
                set_setting(INIT_KEY, json.dumps({**raw, "history": kept}))
            hist = kept
        return hist
    rows = s.query(Clip.id, Clip.yt_state).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None),
                                                  Clip.removed_at.is_(None), Clip.yt_state.in_(list(KIND_OF_STATE))).all()
    hist = {str(i): st for i, st in rows if i not in changed}  # a change the monitor saw just now is not history
    set_setting(INIT_KEY, json.dumps({"at": _iso(utcnow()), "history": hist}))
    return hist


def scan_monitor(changed=()) -> list[int]:
    """Cases that YouTube's own status confirms now -> their ids (new cases, and mail cases that are confirmed now).
    changed: clip ids whose state the video monitor saw change in this very check."""
    out, changed = [], {int(x) for x in changed or ()}
    with SessionLocal() as s:
        hist = _history(s, changed)
        rows = s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.removed_at.is_(None),
                                    Clip.yt_state.in_(list(KIND_OF_STATE))).all()
        for c in rows:
            if hist.get(str(c.id)) == c.yt_state:
                continue  # it was already like this before the responder ran for the first time
            since = None  # when the monitor first saw this state (its alert row)
            for a in s.query(Alert).filter(Alert.clip_id == c.id).order_by(Alert.id.desc()).limit(20):
                if (a.details or {}).get("state") == c.yt_state:
                    since = a.created_at
                    break
            fresh = bool(since and since > utcnow() - FRESH)
            case = s.query(StrikeCase).filter(StrikeCase.clip_id == c.id).order_by(StrikeCase.id.desc()).first()
            if case is None:
                out.append(_new_case(s, c, KIND_OF_STATE[c.yt_state], c.yt_state, fresh, True).id)
            elif case.status == "open" and not (case.data or {}).get("confirmed"):
                _confirm(s, case, c.yt_state, fresh)
                out.append(case.id)
            elif case.status != "open" and not (case.data or {}).get("confirmed"):
                # the earlier case was only a mail (marked solved / taken back) and YouTube now really rejected the
                # video: a new strike, with the full protection
                out.append(_new_case(s, c, KIND_OF_STATE[c.yt_state], c.yt_state, fresh, True).id)
        for case in s.query(StrikeCase).filter(StrikeCase.status == "open", StrikeCase.clip_id.isnot(None)).all():
            d = case.data or {}
            seen = d.get("yt_seen") or ""
            if d.get("confirmed") or case.id in out or seen not in KIND_OF_STATE:
                continue
            c = s.get(Clip, case.clip_id)  # a fresh videos.list (after YouTube's mail) showed the video rejected
            if c is None or c.removed_at or hist.get(str(c.id)) == seen:
                continue
            _confirm(s, case, seen, bool(d.get("fresh")))
            out.append(case.id)
        s.commit()
    return out


def _ask_youtube(case_id: int):
    """A fresh notice names one of my videos: ask YouTube for that video's own status (videos.list, 1 unit)."""
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        c = s.get(Clip, case.clip_id) if case and case.clip_id else None
        if c is None or not c.youtube_id or (case.data or {}).get("confirmed"):
            return
        dest_id, yid = c.destination_id, c.youtube_id
    state = ""
    try:
        from . import monitor, youtube

        state = monitor._state(youtube.video_status(dest_id, [yid]).get(yid))[0]
        said = (f"YouTube's own status of the video: {STATE_WORDS[state]}." if state in KIND_OF_STATE else
                "YouTube's own status of the video does not show a strike-like removal, so nothing was changed.")
    except Exception as e:  # noqa: BLE001  (login, quota, network: the video check every 2 hours asks again)
        log.warning("strike: video status: %s", str(e)[:200])
        said = "YouTube could not be asked about the video right now; the video check (every 2 hours) asks again."
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        d = _data(case)
        if state in KIND_OF_STATE:
            d["yt_seen"], d["yt_seen_at"] = state, _iso(utcnow())
        _log(d, said)
        _put(case, d)
        s.commit()


# ---------------------------------------------------------------- YouTube's notice mails (they never act by themselves)
_COPY = re.compile(r"copyright|takedown|infring", re.I)
_GUIDE = re.compile(r"community guidelines|guidelines strike|violat\w* (of )?(our|youtube'?s|the) (community )?(guidelines|polic)|"
                    r"\bwarning\b", re.I)
_HARD = re.compile(r"strike|take[\s-]?down|taken down|removed|removal|deleted|violat|\bwarning\b", re.I)
_GOOD = re.compile(r"\bretracted\b|\breinstated\b|\brestored\b|has expired|have expired|withdrawn|"
                   r"strike (has been |was |is )?(removed|lifted|resolved)|appeal (was|has been|is) (approved|accepted|successful)|"
                   r"good news", re.I)
# good news in the TEXT must be said plainly about the strike / removal (a strike mail itself explains how a
# retraction works, and a Content ID "claim released" is not about a strike)
_GOOD_BODY = re.compile(r"(strike|removal request|takedown(?: notice| request)?|copyright complaint)\b[^.\n]{0,80}?"
                        r"\b(has been|have been|was|were|is now) (retracted|withdrawn|removed|lifted|resolved|cancell?ed)|"
                        r"\b(claimant|complainant|copyright owner) (has )?(retracted|withdrew|withdrawn)|"
                        r"\b(video|content) (has been|was) (reinstated|restored)|"
                        r"\bappeal (was|has been) (approved|accepted|successful)|\bstrike (has )?expired", re.I)
_NEW_REMOVAL = re.compile(r"taken down|take[\s-]?down notice|removed from youtube|received a|got a|you (now )?have \d", re.I)
# not a strike: Content ID, a removed comment / playlist / post, a video someone shared
_CONTENT_ID = re.compile(r"copyright[\s-]protected content|not a copyright strike|this is not a strike|content\s?id|"
                         r"claim(s|ed)? (was |were |has been |have been )?(released|removed|dropped)|claimed some", re.I)
_NOT_STRIKE = re.compile(r"\bcommented on\b|\breplied to\b|\bnew comments?\b|\b(your|a) comments? (was|were|has been|have been) (removed|deleted|held)|\bremoved (a|your) comments?\b|"
                         r"\bcomments? you (posted|left|made)|\bshared an? (private |unlisted )?video with you|"
                         r"\bhas shared (a|an) |\bshared a (private |unlisted )?video\b|"
                         r"\b(your|a) (playlist|post|community post) (was|has been) (removed|deleted)|"
                         r"\bremoved your (playlist|post|community post)", re.I)
_NOT_A_VIDEO = re.compile(r"\bcomment(s|ed|ing)?\b|\brepl(y|ies|ied)\b|\bmention(s|ed)?\b|\bplaylists?\b|\bpost\b|\bshared\b", re.I)
_STRIKE_WORDS = re.compile(r"strike|take[\s-]?down|taken down|removed|removal|deleted", re.I)
_CLAIMANT = [
    re.compile(r"(?im)^[ \t>*•\-]*(?:claimant|copyright owner|complainant|removed by|content removed by|claimed by|"
               r"rights? ?holder|submitted by|reported by)\s*[:\-–]\s*(.{2,120})$"),
    re.compile(r"(?i)(?:removal request|takedown (?:notice|request)|copyright complaint|complaint|notification)\s+"
               r"(?:from|by|submitted by)\s+([^\n.,;:(]{2,80})"),
    re.compile(r"(?i)\b(?:claimed|removed|taken down) by\s+([^\n.,;:(]{2,80})"),
]
_CLAIMANT_NAME_LINE = re.compile(r"(?im)^[ \t>*•\-]*(?:claimant|copyright owner|complainant)\s*[:\-–]\s*(.{2,160})$")
# an address is taken only from a line that labels it as the claimant's
_CLAIMANT_MAIL_LINE = re.compile(r"(?im)^[ \t>*•\-]*((?:claimant|copyright owner|complainant)(?:'s)?"
                                 r"(?:\s+(?:e-?mail(?:\s+address)?|contact(?:\s+e-?mail)?))?\s*[:\-–]\s*.{3,200})$")
_TITLE_LINE = re.compile(r"(?im)^[ \t>*•\-]*(?:video title|video)\s*:\s*(.{1,200}?)\s*$")
_VIDEO_URL = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^\s\"'<>]*&)?v=|shorts/|video/|embed/)|youtu\.be/|"
                        r"studio\.youtube\.com/video/)([A-Za-z0-9_-]{6,32})")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_OWN_DOMAINS = ("youtube.com", "google.com", "googlemail.com")


def _from_youtube(sender: str) -> bool:
    """youtube.com (or a sub-domain of it) only — the same as MAIL_QUERY."""
    addr = parseaddr(sender or "")[1].lower()
    dom = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    return dom == "youtube.com" or dom.endswith(".youtube.com")


def _name_key(t) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(t or "").casefold())


def parse_notice(msg: dict) -> dict | None:
    """One Gmail message -> what kind of notice it is, or None when it is not a removal notice from YouTube at all.
    type: "strike" | "resolved" (good news) | "claim" (Content ID: no strike) | "other" (about a comment, a shared
    video...). Only "strike" and "resolved" are used. The wording of these mails changes, so nothing depends on one
    layout; but nothing a mail says acts by itself either (see the top of this file)."""
    if not isinstance(msg, dict) or not _from_youtube(msg.get("from") or ""):
        return None
    subject = re.sub(r"\s+", " ", str(msg.get("subject") or "")).strip()
    body = str(msg.get("text") or msg.get("snippet") or "")
    text = subject + "\n" + body
    if not (_HARD.search(subject) or _COPY.search(subject) or _GUIDE.search(subject)):
        return None  # newsletters and the like mention these words in the text; a notice says it in the subject
    c, g = _COPY.search(body), _GUIDE.search(body)
    kind = ("copyright" if _COPY.search(subject) else "guidelines" if _GUIDE.search(subject) else
            "copyright" if c and (not g or c.start() < g.start()) else "guidelines" if g else "other")
    if _CONTENT_ID.search(text) or (re.search(r"\bclaim", subject, re.I) and not _STRIKE_WORDS.search(subject)):
        typ = "claim"  # a Content ID claim: no strike, nothing to answer
    elif _NOT_STRIKE.search(text) or _NOT_A_VIDEO.search(subject):
        typ = "other"  # a removed comment / playlist / post, a shared video: not a strike on a video
    elif _GOOD.search(subject) or (_GOOD_BODY.search(body) and not _NEW_REMOVAL.search(subject)):
        typ = "resolved"
    else:
        typ = "strike"
    claimant = ""
    for rx in _CLAIMANT:
        m = rx.search(body)
        if m:
            claimant = re.sub(r"\s+", " ", _EMAIL.sub("", m.group(1))).strip(" .,:;\"'“”()<>[]")[:120]
            if claimant:
                break
    named = _CLAIMANT_NAME_LINE.search(body)
    named = re.sub(r"\s+", " ", _EMAIL.sub("", named.group(1))).strip(" .,:;\"'“”()<>[]")[:120] if named else ""
    me = ""
    try:
        from . import gmail

        me = (gmail.connected_email() or "").lower()
    except Exception:  # noqa: BLE001
        pass
    address, line = "", ""
    for m in _CLAIMANT_MAIL_LINE.finditer(body):
        for a in _EMAIL.findall(m.group(1)):
            a = a.strip(".").lower()
            if a != me and not a.rsplit("@", 1)[-1].endswith(_OWN_DOMAINS) and not a.endswith((".png", ".jpg", ".gif")):
                address, line = a, re.sub(r"\s+", " ", m.group(1)).strip()[:200]
                break
        if address:
            break
    at = None
    try:
        at = (datetime.fromtimestamp(int(msg.get("internal_ms")) / 1000, timezone.utc).replace(tzinfo=None)
              if msg.get("internal_ms") else None)
    except (TypeError, ValueError, OverflowError, OSError):
        at = None
    return {"id": str(msg.get("id") or ""), "type": typ, "kind": kind, "subject": subject[:300],
            "from": str(msg.get("from") or "")[:200], "date": str(msg.get("date") or "")[:80],
            "excerpt": re.sub(r"\s+", " ", body).strip()[:400], "text": text,
            "video_ids": list(dict.fromkeys(_VIDEO_URL.findall(text))), "titles": [m.strip(" \"'“”") for m in _TITLE_LINE.findall(body)],
            "claimant": claimant, "claimant_named": named, "claimant_email": address, "claimant_email_line": line,
            "about_video": typ == "resolved" or (typ == "strike" and bool(_HARD.search(subject))),
            "hard_subject": typ == "strike" and bool(_HARD.search(subject)), "at": at}


def _norm(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().casefold()


def _link(s, n: dict):
    """The uploaded clip a notice is about: (clip row or None, how it was matched). A link to the video or its id;
    else an exact title from a "Video title:" / "Video:" line, only when exactly one of my clips has that title."""
    ups = s.query(Clip.id, Clip.youtube_id, Clip.title, Clip.yt_state, Clip.attempted_at) \
        .filter(Clip.youtube_id.isnot(None)).all()
    by_id = {u.youtube_id: u for u in ups if u.youtube_id}
    for vid in n["video_ids"]:  # a link to one of my videos
        if vid in by_id:
            return by_id[vid], "video link"
    text = n["text"]
    hits = [(m.start(), u) for u in ups if u.youtube_id and len(u.youtube_id) >= 8
            for m in [re.search(r"(?<![\w-])" + re.escape(u.youtube_id) + r"(?![\w-])", text)] if m]
    if hits:
        return min(hits, key=lambda h: h[0])[1], "video id"
    found = {}
    for t in n["titles"]:
        want = _norm(t)
        same = [u for u in ups if u.title and _norm(u.title) == want] if len(want) >= 3 else []
        if len(same) > 1:
            return None, ""  # two of my clips have this title: it cannot be told which one
        if same:
            found[same[0].id] = same[0]
    if len(found) == 1:
        return next(iter(found.values())), "video title"
    return None, ""


def scan_mail() -> dict:
    """Read YouTube's recent notice mails (Gmail connected) and open / update cases. Returns {"new": [case ids], ...}.
    Nothing here acts on the channel: see the top of this file."""
    from . import gmail

    out = {"new": [], "read": 0, "notices": 0, "old": 0}
    if not gmail.connected_email():
        return out
    try:
        ids = gmail.search(MAIL_QUERY, 40)
    except Exception as e:  # noqa: BLE001  (Gmail away, login expired: next round)
        log.warning("strike mail search: %s", str(e)[:200])
        _note("Reading YouTube's notices in Gmail", e)
        return out
    seen = [str(x) for x in _load(SEEN_KEY, [])]
    have = set(seen)
    for mid in reversed([i for i in ids if i not in have]):  # oldest first
        try:
            msg = gmail.read_message(mid)
        except Exception as e:  # noqa: BLE001  (not marked as read: tried again next round)
            log.warning("strike mail read: %s", str(e)[:200])
            _note("Reading YouTube's notices in Gmail", e)
            break
        out["read"] += 1
        try:
            n = parse_notice(msg)
            if n and n["type"] in ("strike", "resolved"):
                used, cid = _apply_notice(n)
                out["notices"] += 1 if used else 0
                out["old"] += 1 if used and not _fresh(n) else 0
                if cid:
                    out["new"].append(cid)
        except Exception as e:  # noqa: BLE001  (a layout nobody expected must never stop the others)
            log.warning("strike mail parse: %s", str(e)[:200])
            _note("Understanding a notice from YouTube", e)
        seen.append(mid)
    if out["read"]:
        set_setting(SEEN_KEY, json.dumps(seen[-600:]))
    if out["old"]:
        _tell(f"📨 {out['old']} older notice{'' if out['old'] == 1 else 's'} from YouTube (more than 36 hours old) "
              f"{'was' if out['old'] == 1 else 'were'} found in your Gmail and listed in Alerts → Strikes. Nothing was changed "
              "because of them.")
    return out


def _fresh(n: dict) -> bool:
    return bool(n.get("at") and n["at"] > utcnow() - FRESH)


def _notice_row(n: dict, how: str = "") -> dict:
    return {"id": n["id"], "subject": n["subject"], "from": n["from"], "date": n["date"], "excerpt": n["excerpt"],
            "kind": n["kind"], "type": n["type"], "matched_by": how}


def _apply_notice(n: dict | None) -> tuple[bool, int | None]:
    """Attach a notice to its case (opening one when needed). Returns (was it used, the id of a NEW case or None).
    It never blocks, holds, makes private or pauses anything: only YouTube's own status of the video does that."""
    if not n or n.get("type") not in ("strike", "resolved") or not n.get("about_video"):
        return False, None
    fresh = _fresh(n)
    ask = None
    with SessionLocal() as s:
        clip_row, how = _link(s, n)
        if clip_row is None:
            if n["type"] != "strike" or s.query(StrikeCase).filter(StrikeCase.notice_id == n["id"]).first():
                return False, None  # good news about no video of mine / already kept
            d = {"title": n["subject"] or "A notice from YouTube", "notices": [_notice_row(n)], "claimant": n["claimant"],
                 "fresh": fresh, "unmatched": True}
            _log(d, "A notice from YouTube was found in your Gmail. It matched none of the videos uploaded from here, so "
                    "nothing was changed.")
            case = StrikeCase(notice_id=n["id"], kind=n["kind"], state="mail", status="open", creator_key="",
                              mail_state="none", data=d, created_at=utcnow(), updated_at=utcnow())
            s.add(case)
            s.commit()
            if fresh:
                _tell(f"📨 <b>A notice from YouTube</b> could not be matched to one of your videos:\n<i>{_e(n['subject'])}</i>\n"
                      "Nothing was changed. Open Alerts → Strikes and check YouTube Studio.")
            return True, case.id
        case = s.query(StrikeCase).filter(StrikeCase.clip_id == clip_row.id).order_by(StrikeCase.id.desc()).first()
        if n["type"] == "resolved":
            if how not in ("video link", "video id") or not case or case.status != "open":
                return False, None  # good news counts only for the very same video id
            d = _data(case)
            if any(x.get("id") == n["id"] for x in d.get("notices") or []):
                return False, None
            d.setdefault("notices", []).append(_notice_row(n, how))
            d["resolved_by"] = "youtube"
            _log(d, f"YouTube sent good news about this video: “{n['subject'][:120]}”.")
            case.status = "retracted"
            _put(case, d)
            s.commit()
            _tell(f"✅ YouTube sent good news about <b>{_e(d.get('title'))}</b>:\n<i>{_e(n['subject'])}</i>\n"
                  "Check in YouTube Studio that the strike is gone, then press Resume uploads (Alerts → Strikes).")
            return True, None
        new_id = None
        if case is None:
            c = s.get(Clip, clip_row.id)
            case = _new_case(s, c, n["kind"], "mail", fresh, False)
            new_id = case.id
        d = _data(case)
        if any(x.get("id") == n["id"] for x in d.get("notices") or []):
            s.commit()
            return False, new_id
        d.setdefault("notices", []).append(_notice_row(n, how))
        d["notices"] = d["notices"][-8:]
        if n["claimant"] and not d.get("claimant"):
            d["claimant"] = n["claimant"]
        got_address = bool(n["claimant_email"] and not d.get("claimant_email"))
        if got_address:
            d["claimant_email"], d["claimant_email_line"] = n["claimant_email"], n["claimant_email_line"]
            d["claimant_named"] = n["claimant_named"]
        if not d.get("confirmed") and case.kind == "other" and n["kind"] != "other":
            case.kind = n["kind"]  # the mail says what it is about (YouTube's own status decides once it is there)
        if fresh and not d.get("confirmed"):
            d["fresh"] = True
        _log(d, f"YouTube's notice was found in your Gmail (“{n['subject'][:100]}”, matched by the {how}).")
        if got_address and case.mail_state in ("ready", "no_email"):
            case.mail_state = "none"  # written again, to the address YouTube gave
            d.pop("mail", None)
            d.pop("mail_checked", None)
        _put(case, d)
        s.commit()
        if fresh and n["type"] == "strike" and not d.get("confirmed"):
            ask = case.id
        used = (True, new_id)
    if ask:
        _ask_youtube(ask)
    return used


# ---------------------------------------------------------------- protection (confirmed cases only)
def _stop_creator(s, case, d: dict, by_owner: bool = False):
    """The creator of a case goes on the never-use list and their channel source(s) stop."""
    key = case.creator_key or ""
    if not key:
        return
    clip = s.get(Clip, case.clip_id) if case.clip_id else None
    p = s.get(Project, clip.project_id) if clip and clip.project_id else None
    _block(key, d.get("source_channel") or "", d.get("source_channel_url") or "", case.id)
    did = d.setdefault("did", {})
    did["blocked"] = True
    did["blocked_by"] = "you" if by_owner else "system"
    d.pop("released", None)
    _log(d, ("You put " if by_owner else "") + f"{d.get('source_channel') or 'The creator'}"
         + (" on" if by_owner else " was put on") + " the never-use list: no source uses their videos again.")
    stopped = list(did.get("sources") or [])
    for ch in s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind == "channel")).all():
        if ch.mode == "own":
            continue
        if creator_key(ch.resolved_url) == key or creator_key(ch.query) == key or (p is not None and p.channel_ref == ch.id):
            if ch.auto:
                ch.auto = False
            ch.last_status = "stopped: this creator is on the never-use list after a strike on your channel (Alerts → Strikes)"
            if not any(x.get("id") == ch.id for x in stopped):
                stopped.append({"id": ch.id, "name": ch.name or ch.query or f"Source #{ch.id}"})
    did["sources"] = stopped
    if stopped:
        _log(d, "Stopped the source" + ("s" if len(stopped) > 1 else "") + ": "
             + ", ".join("“" + x["name"] + "”" for x in stopped) + ".")


def _protect(case_id: int):
    """Once per confirmed COPYRIGHT case: the creator on the never-use list and their source(s) stopped. (Waiting clips
    are held and scheduled videos kept private by enforce(), which follows the list.)"""
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        if not case or not case.clip_id or case.kind != "copyright":
            return
        d = _data(case)
        if not d.get("confirmed") or (d.get("did") or {}).get("at"):
            return
        _stop_creator(s, case, d)
        d.setdefault("did", {})["at"] = _iso(utcnow())
        _put(case, d)
        s.commit()


def block_creator(case_id: int) -> dict:
    """The owner's press "Stop using this creator" (any case with a creator): never-use list, source(s) stopped, their
    waiting clips held, their scheduled videos kept private."""
    with SessionLocal() as s:
        case = _case(s, case_id)
        if not case.creator_key:
            raise ValueError("This case has no other creator to stop: the video was your own, or the notice matched none "
                             "of your videos.")
        d = _data(case)
        _stop_creator(s, case, d, by_owner=True)
        _put(case, d)
        s.commit()
    res = enforce()
    with SessionLocal() as s:
        return {**res, "case": public(s, _case(s, case_id))}


def _why(e: BaseException) -> str:
    """Why a change on YouTube or a mail did not work, in plain words (never the raw error text)."""
    name, m = type(e).__name__, str(e)
    if name == "NeedsReconnect":
        return ("this channel was connected before the system could change videos — connect it once more (My channels → "
                "Connect a YouTube channel → the same channel, tick every box)")
    if name in ("RefreshError", "TokenError") or "invalid_grant" in m:
        return "the Google login expired — connect it again (My channels)"
    if "quotaExceeded" in m or "dailyLimitExceeded" in m:
        return "today's YouTube allowance is used up — it is tried again after the daily reset"
    if name == "HttpError":
        status, why, msg = 0, "", ""
        try:
            from .comments import _reason

            status, why, msg = _reason(e)
        except Exception:  # noqa: BLE001
            pass
        if why in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT") or "insufficient" in str(msg).lower():
            return ("this channel's login may not change videos — connect it once more (My channels → Connect a YouTube "
                    "channel → the same channel, tick every box)")
        try:
            status = int(status or getattr(getattr(e, "resp", None), "status", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        if status == 404:
            return "YouTube does not know this video any more"
        if status == 403:
            return "YouTube refused the change for this video"
        if status >= 500:
            return "YouTube did not answer properly — it is tried again later"
        return "YouTube refused the change — it is tried again later"
    if isinstance(e, (TimeoutError, ConnectionError)) or "timed out" in m.lower() or "timeout" in name.lower():
        return "the connection broke off before an answer came"
    if isinstance(e, OSError):
        return "the connection to Google did not work"
    return "something unexpected went wrong"


def _uploads_waiting() -> int:
    try:
        from .comments import uploads_waiting

        return uploads_waiting()
    except Exception as e:  # noqa: BLE001
        log.warning("strike: uploads waiting: %s", str(e)[:150])
        return 0


def _budget_ok(publish_at, waiting) -> bool:
    """Is there room in today's YouTube budget for one videos.update (and its read)? Uploads keep their share; a video
    that would go public before the next reset is kept private whenever the budget still holds the call at all."""
    from . import quota

    if quota.can_update(waiting() if callable(waiting) else waiting):
        return True
    soon = publish_at is not None and publish_at <= quota.next_reset() + timedelta(hours=1)
    return soon and quota.DAILY_UNITS - quota.used() >= quota.COST["update"] + quota.COST["read"]


BUDGET_WHY = "today's YouTube allowance is kept for uploads — it is tried again after the daily reset"


def enforce() -> dict:
    """For every creator on the never-use list: hold what waits, and keep scheduled videos private. Safe to repeat —
    it runs with every round, so a clip that was being made or uploaded when the strike came is caught afterwards."""
    out = {"held": 0, "private": 0, "private_failed": 0}
    bl = blocked()
    if not bl:
        return out
    now = utcnow()
    with SessionLocal() as s:
        pids = [pid for pid, url in s.query(Project.id, Project.channel_url) if creator_key(url) in bl]
        if not pids:
            return out
        rows = []
        for part in _chunks(pids):
            rows += s.query(Clip.id, Clip.status, Clip.destination_id, Clip.youtube_id, Clip.publish_at, Clip.title) \
                .filter(Clip.project_id.in_(part),
                        (Clip.status.in_(WAITING)) | ((Clip.status == "uploaded") & (Clip.kept_private.isnot(True))
                                                      & (Clip.publish_at > now) & (Clip.removed_at.is_(None)))).all()
        for r in rows:
            if r.status in WAITING:  # the claim is on the status it had: an upload that just started is left to finish
                res = s.execute(update(Clip).where(Clip.id == r.id, Clip.status == r.status)
                                .values(status=HELD, held_was=r.status, autoqueue=False, job_lease=None, error=HELD_NOTE))
                out["held"] += res.rowcount
        s.commit()
    sched = sorted([r for r in rows if r.status == "uploaded" and r.youtube_id], key=lambda r: r.publish_at or now)
    if sched:
        from . import quota

        fails = _load(FAIL_KEY, {})
        changed, need = False, []

        def waiting():  # counted once per round, only when it is needed
            if not need:
                need.append(_uploads_waiting())
            return need[0]
        for r in sched:
            f = fails.get(str(r.id)) or {}
            if (f.get("next") or "") > _iso(utcnow()):  # tried a moment ago / budget used up: not again in the same breath
                out["private_failed"] += 1
                continue
            if not _budget_ok(r.publish_at, waiting):
                ok, why, counted = False, BUDGET_WHY, False
                wait_until = quota.next_reset()
            else:
                ok, why, counted = _make_private(r.id, r.destination_id, r.youtube_id)
                wait_until = None
            if ok:
                out["private"] += 1
                if fails.pop(str(r.id), None) is not None:
                    changed = True
                continue
            out["private_failed"] += 1
            tries = int(f.get("tries") or 0) + (1 if counted else 0)
            # it is tried until the video's publish time: every few minutes at first, then once an hour (no giving up —
            # the owner may reconnect the channel tomorrow, and the video must still not go public next week)
            if wait_until is None and "allowance" in why:
                wait_until = quota.next_reset()
            nxt = wait_until or (utcnow() + (RETRY_SOON if tries <= QUICK_TRIES else RETRY_LATER))
            f = {"tries": tries, "error": why, "at": _iso(utcnow()), "next": _iso(nxt),
                 "title": r.title or f"Clip #{r.id}", "youtube_id": r.youtube_id, "publish_at": _iso(r.publish_at),
                 "told": bool(f.get("told"))}
            # a video kept back only by the budget is not an alarm while its publish time is still days away
            alarm = not (why == BUDGET_WHY and r.publish_at and r.publish_at > quota.next_reset() + timedelta(days=1))
            if not f["told"] and alarm:
                f["told"] = True
                _tell(f"🚨 <b>Could NOT keep a scheduled video private</b>: {_e(f['title'])}\nIts creator is on the never-use "
                      f"list after a strike, and it goes public on {r.publish_at:%d %b %Y %H:%M} UTC.\nWhy: {_e(why)}.\nOpen "
                      f"YouTube Studio now and set it to Private yourself: https://studio.youtube.com/video/{r.youtube_id}/edit")
            fails[str(r.id)] = f
            changed = True
        if changed:
            set_setting(FAIL_KEY, json.dumps(fails))
    return out


def _make_private(clip_id: int, dest_id, youtube_id: str) -> tuple[bool, str, bool]:
    """(done, why not, does this failure count as a try)."""
    from . import youtube

    try:
        youtube.keep_private(dest_id, youtube_id)
    except LookupError:
        pass  # the video is gone from YouTube: it cannot go public either
    except Exception as e:  # noqa: BLE001
        m = str(e)
        return False, _why(e), not ("quotaExceeded" in m or "dailyLimitExceeded" in m)
    with SessionLocal() as s:
        s.execute(update(Clip).where(Clip.id == clip_id).values(kept_private=True, live_noted=True, error=PRIVATE_NOTE))
        s.execute(update(Clip).where(Clip.id == clip_id, Clip.comment_id.is_(None),
                                     (Clip.comment_state.like("waiting%")) | (Clip.comment_state == "no permission"))
                  .values(comment_state="off"))  # a private video gets no first comment
        s.commit()
    return True, "", False


def _counts(s, case, cache: dict | None = None) -> dict:
    """What is held / kept private / still public for the creator of a case, read fresh from the clips."""
    key = case.creator_key or ""
    out = {"held": 0, "private": [], "failed": [], "public": []}
    if not key:
        return out
    cache = cache if cache is not None else {}
    if key not in cache:
        pids = _creator_projects(s, key)
        rows = []
        for part in _chunks(pids):
            rows += s.query(Clip.id, Clip.status, Clip.title, Clip.youtube_id, Clip.kept_private, Clip.publish_at, Clip.yt_state,
                            Clip.removed_at).filter(Clip.project_id.in_(part)).all()
        cache[key] = rows
    now, fails = utcnow(), _load(FAIL_KEY, {})
    for r in cache[key]:
        if r.status == HELD:
            out["held"] += 1
        elif r.status == "uploaded" and r.youtube_id:
            row = {"clip_id": r.id, "title": r.title or f"Clip #{r.id}", "youtube_id": r.youtube_id, "publish_at": _iso(r.publish_at)}
            if r.kept_private:
                out["private"].append(row)
            elif str(r.id) in fails and r.id != case.clip_id:
                out["failed"].append({**row, "error": fails[str(r.id)].get("error") or "",
                                      "went_public": bool(r.publish_at and r.publish_at <= now)})
            elif r.id != case.clip_id and not r.removed_at and (r.yt_state or "ok") == "ok" and not (r.publish_at and r.publish_at > now):
                out["public"].append(row)
    return out


def _done_lines(case, d: dict, cnt: dict, paused: bool) -> list[str]:
    """What the system (or the owner) already did, in plain sentences."""
    did, lines = d.get("did") or {}, []
    creator = d.get("source_channel") or "the creator"
    for x in did.get("sources") or []:
        lines.append(f"Stopped the source “{x.get('name')}”.")
    if did.get("blocked"):
        if d.get("released"):
            lines.append(f"{creator} was on the never-use list; you allowed them again.")
        elif did.get("blocked_by") == "you":
            lines.append(f"You put {creator} on the never-use list — no source will use their videos again.")
        else:
            lines.append(f"Put {creator} on the never-use list — no source will use their videos again.")
    if cnt["held"]:
        n = cnt["held"]
        lines.append(f"Held {n} waiting clip{'' if n == 1 else 's'} made from their videos ({'it' if n == 1 else 'they'} will not "
                     "upload; nothing was deleted).")
    if cnt["private"]:
        n = len(cnt["private"])
        lines.append(f"Kept {n} video{'' if n == 1 else 's'} of theirs private on YouTube ({'it' if n == 1 else 'they'} will not go public; "
                     "nothing was deleted).")
    if did.get("paused") and paused:
        lines.append(f"Paused uploads to {d.get('my_channel') or 'the channel'}.")
    return lines


def _pause(case_id: int):
    """A confirmed case: the channel's uploads wait (as the video monitor does), and the reason says plainly what was
    done and how to resume."""
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        if not case or not case.clip_id:
            return
        d = _data(case)
        if not d.get("confirmed"):
            return
        dest = s.get(Destination, d.get("dest_id")) if d.get("dest_id") else None
        if dest is None or (d.get("did") or {}).get("paused") is not None:
            return
        mine = bool(dest.paused) and "login expired" not in (dest.paused_reason or "") and not (dest.paused_reason or "").startswith("Paused ")
        if not dest.paused and d.get("fresh"):
            dest.paused, dest.paused_at = True, utcnow()
            mine = True
        d.setdefault("did", {})["paused"] = bool(dest.paused)
        if mine and d.get("fresh"):  # paused because of this strike (by the monitor or just now): say what happened
            done = _done_lines(case, d, _counts(s, case), False)
            what = _what(case, d)
            dest.paused_reason = (f"“{(d.get('title') or '')[:80]}”: {what} What the system did: "
                                  + (" ".join(done) if done else "paused the uploads.")
                                  + " Uploads to this channel wait until you press Resume uploads — do that only when the "
                                    "strike is dealt with (Alerts → Strikes shows what is left to do).")[:1500]
            _log(d, f"Uploads to {dest.name or 'the channel'} are paused until you press Resume uploads.")
        _put(case, d)
        s.commit()


def _what(case, d: dict) -> str:
    if d.get("unmatched"):
        return ("YouTube sent a notice that could not be matched to one of the videos uploaded from here. Nothing was "
                "changed because of it.")
    who = f" by {d['claimant']}" if d.get("claimant") and case.kind == "copyright" else ""
    if not d.get("confirmed"):
        about = {"copyright": f"a copyright notice{who}", "guidelines": "a Community Guidelines notice"}.get(case.kind, "a notice")
        return (f"A mail that looks like YouTube's {about} names this video. YouTube itself does not show the video as removed "
                "(yet), so the system has not changed anything. If YouTube confirms it, the channel is protected by itself.")
    if case.kind == "copyright":
        return (f"YouTube removed this video after a copyright complaint{who}. This usually comes with a copyright strike "
                "(three strikes close a channel).")
    if case.kind == "guidelines":
        if case.state == "rejected:termsOfUse":
            return ("YouTube removed this video for breaking its Terms of Service. This was YouTube's decision, not a "
                    "complaint from the creator of the original video.")
        return ("YouTube removed this video for breaking its Community Guidelines. This can come with a Community Guidelines "
                "strike. It was YouTube's decision, not a complaint from the creator of the original video.")
    if case.state == "rejected:trademark":
        return ("YouTube removed this video after a trademark complaint. Who complained is not known here — it need not be the "
                "creator of the original video.")
    if case.state == "rejected:legal":
        return ("YouTube removed this video for a legal reason. Who asked for it is not known here — it need not be the "
                "creator of the original video.")
    return "YouTube removed this video."


# ---------------------------------------------------------------- the humble request to the creator
MAILS = [
    ("An apology about a clip from your video",
     "Hello {name},\n\nI am writing to apologise. I posted a short clip from your video \"{orig}\" on my channel, and a copyright "
     "complaint was filed about it. I am sorry for using your work in a way you did not want.\n\n"
     "The clip was \"{title}\" ({link}).{credit}{why}\n\n{action}I have also stopped everything else: no more clips from your "
     "videos will be posted on my channel.\n\nIf you feel able to, may I kindly ask you to retract the complaint? YouTube explains "
     "how here: {help}\nA copyright strike puts my small channel at risk, so I would be very grateful. If you would rather not, I "
     "understand and respect your decision.\n\nThank you for reading this, and sorry again.\n\n{sign}"),
    ("Sorry about my clip of \"{orig_short}\"",
     "Hi {name},\n\nMy channel shared a short clip taken from your video \"{orig}\", and it received a copyright complaint. "
     "I should have asked you first, and I am sorry that I did not.\n\n"
     "My clip: \"{title}\" ({link}).{credit}{why}\n\n{action}From now on nothing from your videos will appear on my channel.\n\n"
     "I have one humble request: would you consider retracting the complaint? The steps are on this YouTube help page: {help}\n"
     "My channel is small and a strike could close it, so it would mean a lot to me. Whatever you decide, I accept it, and I "
     "will not use your content again.\n\nThank you for your time.\n\n{sign}"),
    ("A sincere apology and a small request",
     "Dear {name},\n\nPlease accept my apology. A short clip from your video \"{orig}\" was posted on my channel, and it got a "
     "copyright complaint. It was never my wish to upset you or to take anything away from your work.\n\n"
     "The clip was called \"{title}\" ({link}).{credit}{why}\n\n{action}I will not post any more clips of your videos.\n\n"
     "Could I politely ask you to withdraw the complaint? YouTube describes how to retract it here: {help}\n"
     "A strike is a real danger for a small channel like mine, and your help would be a great kindness. If the answer is no, "
     "I fully understand.\n\nWith thanks and respect,\n\n{sign}"),
]


def compose(case, d: dict, name: str = "") -> tuple[str, str]:
    """(subject, body) of the request, in the owner's name. Three wordings; one creator always gets the same one."""
    from .permissions import _clean_title, mail_identity

    sign = mail_identity()[0]
    subj, body = MAILS[sum(map(ord, case.creator_key or str(case.id))) % len(MAILS)]
    orig = re.sub(r"\s+", " ", d.get("source_title") or "").strip()[:120] or "your video"
    short = _clean_title(d.get("source_title") or "")
    if short == "aapki latest video":
        short = "your video"
    title = re.sub(r"\s+", " ", d.get("title") or "").strip()[:120]
    link = f"https://youtu.be/{case.youtube_id}" if case.youtube_id else ""
    credit = " It named you as the creator and linked to your original video." if d.get("credited") else ""
    mode, lic = d.get("source_mode"), (d.get("license") or "").lower()
    why = ""
    if mode == "cc" and "creative commons" in lic:
        why = (" I had used it because the video was marked with a Creative Commons licence on YouTube, and I am sorry if I "
               "misunderstood that.")
    elif mode == "permission":
        why = " I had used it on the understanding that you had allowed it earlier, and I am sorry if I misunderstood."
    action = "The clip is no longer on YouTube. " if (case.state or "").startswith("rejected") else ""
    who = re.sub(r"\s+", " ", name or d.get("source_channel") or "").strip()[:80] or "there"
    fill = dict(name=who, orig=orig, orig_short=short, title=title, link=link, credit=credit, why=why, action=action,
                help=RETRACT_HELP, sign=sign)
    return subj.format(**fill)[:150], body.format(**fill)


def _recipient(case, d: dict) -> tuple[str, str, bool]:
    """(address, where it came from, may it go out by itself). YouTube's notice (a labelled claimant line), then the
    permission inbox, then their channel description. Only the first can go out by itself, and only when the claimant
    named there IS the source creator's channel."""
    from . import gmail

    me = (gmail.connected_email() or "").lower()
    good = lambda a: bool(a) and bool(gmail.EMAIL_RE.match(a)) and a.lower() != me  # noqa: E731
    if good(d.get("claimant_email")):
        line = d.get("claimant_email_line") or ""
        named, creator = _name_key(d.get("claimant_named")), _name_key(d.get("source_channel"))
        auto = bool(named) and named == creator
        return d["claimant_email"], "YouTube's notice" + (f" (the line “{line[:120]}”)" if line else ""), auto
    key = case.creator_key or ""
    if key.startswith("UC"):
        try:
            from . import permissions

            c = permissions._load().get(key) or {}
            if good(c.get("email")):
                return c["email"], "your permission inbox", False
            if not d.get("email_checked"):
                d["email_checked"] = True
                found = permissions.lookup_emails([key]).get(key)
                if good(found):
                    return found, "their channel description", False
        except Exception as e:  # noqa: BLE001
            _note("Looking up a creator's e-mail after a strike", e)
    return "", "", False


def prepare_mail(case_id: int):
    """Write the request and find where to send it (nothing is sent here)."""
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        if (not case or case.kind != "copyright" or not case.clip_id or case.status != "open"
                or case.mail_state not in (None, "", "none")):
            return
        d = _data(case)
        if d.get("mail_checked"):
            return
        d["mail_checked"] = True
        if d.get("source_mode") in ("original", "own") or not (d.get("source_channel") or d.get("claimant")):
            _put(case, d)  # my own content, or nobody to write to
            s.commit()
            return
        to, where, auto = _recipient(case, d)
        name = d.get("claimant") if where.startswith("YouTube's notice") and d.get("claimant") else ""
        subject, body = compose(case, d, name)
        d["mail"] = {"to": to, "to_from": where, "auto": auto, "subject": subject, "body": body, "written_at": _iso(utcnow())}
        _log(d, f"The request to {name or d.get('source_channel') or 'the creator'} was written"
             + ((f" (to {to}, from {where})" + ("." if auto else " — it waits for you to check the address and press Send."))
                if to else ". No e-mail address was found for them."))
        case.mail_state = "ready" if to else "no_email"
        _put(case, d)
        s.commit()


def _sent_today() -> int:
    try:
        return int(get_setting(SENT_PREFIX + utcnow().strftime("%Y-%m-%d")) or 0)
    except (TypeError, ValueError):
        return 0


def _reserve(cap: int) -> str | None:
    """Take one of today's sends, atomically (check and count in one UPDATE). -> the day's key, or None when the cap
    is reached."""
    key = SENT_PREFIX + utcnow().strftime("%Y-%m-%d")
    with SessionLocal() as s:
        if s.get(Setting, key) is None:
            try:
                s.add(Setting(key=key, value="0"))
                s.commit()
            except IntegrityError:  # another send made the row at the same moment
                s.rollback()
        n = cast(Setting.value, Integer)
        res = s.execute(update(Setting).where(Setting.key == key, n < cap).values(value=cast(n + 1, String)))
        s.commit()
        return key if res.rowcount == 1 else None


def _unreserve(key: str):
    """Nothing went out: give the day's send back."""
    with SessionLocal() as s:
        n = cast(Setting.value, Integer)
        s.execute(update(Setting).where(Setting.key == key, n > 0).values(value=cast(n - 1, String)))
        s.commit()


def send_request(case_id: int, to: str | None = None, by: str = "hand") -> dict:
    """Send the request from the owner's Gmail. Once per struck video: the claim on mail_state is atomic, and a send
    that may or may not have gone out is never repeated by itself. The day's limit is taken atomically too.
    Raises ValueError with words for the owner."""
    from . import acl, gmail

    for key in ("strikes.reply", "gmail.send"):  # the Super Admin's switches (also for the automatic round)
        if not acl.allowed(key):
            raise ValueError(acl.message(key))
    if not gmail.connected_email():
        raise ValueError("Gmail is not connected. Connect it in My channels → Gmail, then press Send again.")
    cap = AUTO_PER_DAY if by == "auto" else HAND_PER_DAY
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        if not case:
            raise KeyError(case_id)
        d = _data(case)
        mail = d.get("mail") or {}
        if case.mail_state == "sent":
            raise ValueError("The request was already sent for this video. It is never sent twice.")
        if case.mail_state == "sending":
            raise ValueError("The request is being sent right now.")
        if case.kind != "copyright" or not mail.get("body"):
            raise ValueError("There is no request to send for this case.")
        if case.status == "solved":
            raise ValueError("This case is marked as solved, so nothing is sent for it.")
        if by == "auto" and not (mail.get("auto") and d.get("confirmed")):
            raise ValueError("This request waits for the owner's Send.")
        if to is not None and str(to).strip():
            to = str(to).strip()
            if not gmail.EMAIL_RE.match(to):
                raise ValueError("That email address does not look right.")
            mail["to"], mail["to_from"] = to, "typed by you"
        if not mail.get("to"):
            raise ValueError("No e-mail address is known for this creator. Find it on their channel's About page "
                             "(“View email address”), paste it into the box and press Send again.")
        day = _reserve(cap)
        if not day:
            raise ValueError(f"{cap} requests were already sent today — more would look like spam. The rest go tomorrow.")
        was = case.mail_state
        d["mail"] = mail
        _put(case, d)
        s.commit()
        res = s.execute(update(StrikeCase).where(StrikeCase.id == case_id, StrikeCase.mail_state == was)
                        .values(mail_state="sending", updated_at=utcnow()))
        s.commit()
        if res.rowcount != 1:
            _unreserve(day)
            raise ValueError("The request is being sent right now.")
    err, sent, state = None, None, "interrupted"
    try:
        sent = gmail.send(mail["to"], mail["body"], mail.get("subject"), kind="strike")
    except ValueError as e:  # refused before anything left (a bad address): it can be sent again
        err, state = str(e), ("ready" if was in ("ready", "interrupted") else was)
        _unreserve(day)
    except Exception as e:  # noqa: BLE001  (it may or may not have gone out: never again by itself, and it counts)
        err, state = _why(e), "interrupted"
        _note("Sending the request to a creator after a strike", e)
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        d = _data(case)
        m = d.setdefault("mail", mail)
        if sent:
            m.update(sent_at=_iso(utcnow()), thread_id=sent.get("thread_id"), message_id=sent.get("message_id"),
                     sent_by=by, error=None)
            case.mail_state = "sent"
            _log(d, f"The request was sent to {m.get('to')}" + (" by itself." if by == "auto" else " (you pressed Send)."))
        else:
            m["error"] = err
            case.mail_state = state
            _log(d, f"The request could not be sent: {err}")
        _put(case, d)
        s.commit()
        title, to_addr = d.get("title"), m.get("to")
    if not sent:
        raise ValueError(err if state != "interrupted" else
                         f"Sending did not finish ({err}). Look in Gmail's Sent folder: if the mail is not there, press Send again.")
    try:
        acl.record_here("gmail.send", f"Sent a request to retract a copyright complaint (case #{case_id})",
                        "system" if by == "auto" else "user")
    except Exception:  # noqa: BLE001
        pass
    if by == "auto":
        _tell(f"📧 The humble request about <b>{_e(title)}</b> was sent to {_e(to_addr)}. Their answer will show in "
              "Alerts → Strikes.")
    return {"sent": True, "to": to_addr}


def send_due() -> int:
    """The automatic round: a ready request goes out by itself only when the account switched that on, YouTube itself
    confirmed the strike, and the address came from YouTube's notice naming the creator — at most 3 a day."""
    from . import acl, gmail

    if not settings()["auto"] or not acl.feature("gmail.send") or not acl.feature("strikes.reply") or not gmail.connected_email():
        return 0
    with SessionLocal() as s:
        ids = [c.id for c in s.query(StrikeCase).filter(StrikeCase.kind == "copyright", StrikeCase.status == "open",
                                                        StrikeCase.mail_state == "ready").order_by(StrikeCase.id)
               if (c.data or {}).get("confirmed") and ((c.data or {}).get("mail") or {}).get("auto")]
    n = 0
    for cid in ids:
        if _sent_today() >= AUTO_PER_DAY:
            break
        try:
            send_request(cid, by="auto")
            n += 1
        except (ValueError, KeyError) as e:
            log.info("strike request %s not sent: %s", cid, str(e)[:150])
    return n


_RETRACT = re.compile(
    r"\b(retract\w*|withdr[ae]w\w*|rescind\w*|"
    r"(removed?|released?|cancel+ed|cancel|taken back|took back|take back|lifted|dropped|drop|closed) (it|(the |my |our |this |that )?"
    r"(copyright )?(claim|strike|complaint|takedown|notice|request|report))|"
    r"(claim|strike|complaint|takedown|notice|request|report) ((has been|have been|is|was|will be|got) )?"
    r"(removed|released|retracted|withdrawn|cancel+ed|lifted|dropped|closed|taken back))\b", re.I)
_NOT = re.compile(r"\b(not|no|never|won'?t|wont|cannot|can'?t|cant|don'?t|dont|refuse\w*|unless|only if|if you|pay|paid|money|fee|"
                  r"price|nahi|nahin|nhi|mat)\b|\?", re.I)


def reads_as_retracted(text: str) -> bool:
    """A reply that says the complaint is taken back — without a refusal, a condition or a question in it."""
    t = re.sub(r"\s+", " ", text or "").strip()
    return bool(t) and bool(_RETRACT.search(t)) and not _NOT.search(t)


def _own_words(snippet: str, mail_body: str) -> str:
    """The creator's new words only: the quoted copy of my request (which itself says "retract") is cut off."""
    from .permissions import _own_words as base

    t = base(snippet or "")
    start = re.sub(r"\s+", " ", mail_body or "").strip()[:18]
    if start:
        i = re.sub(r"\s+", " ", t).find(start)
        if i >= 0:
            t = re.sub(r"\s+", " ", t)[:i]
    return t.strip()


def check_replies() -> int:
    """Look into the thread of every sent request for an answer. A new one is shown on the case and told to the owner;
    one that reads like "retracted" marks the case and says the uploads can be resumed."""
    from . import gmail

    if not gmail.connected_email():
        return 0
    with SessionLocal() as s:
        ids = [i for (i,) in s.query(StrikeCase.id).filter(StrikeCase.mail_state == "sent", StrikeCase.status != "solved",
                                                           StrikeCase.updated_at > utcnow() - timedelta(days=WATCH_REPLY_DAYS))]
    found = 0
    for cid in ids:
        with SessionLocal() as s:
            case = s.get(StrikeCase, cid)
            d = _data(case)
        mail = d.get("mail") or {}
        if not mail.get("thread_id"):
            continue
        try:
            r = gmail.reply_in_thread(mail["thread_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("strike reply check: %s", str(e)[:150])
            _note("Reading a creator's answer after a strike", e)
            continue
        if not r or r.get("id") == (d.get("reply") or {}).get("id"):
            continue
        words = _own_words(r.get("snippet", ""), mail.get("body", ""))
        took_back = reads_as_retracted(words)
        with SessionLocal() as s:
            case = s.get(StrikeCase, cid)
            d = _data(case)
            d["reply"] = {"id": r.get("id"), "from": r.get("from"), "date": r.get("date"), "text": (r.get("snippet") or "")[:500],
                          "retracted": took_back, "seen_at": _iso(utcnow())}
            _log(d, f"{r.get('from') or 'The creator'} answered" + (": it reads like the complaint is taken back." if took_back else "."))
            if took_back and case.status == "open":
                case.status = "retracted"
            _put(case, d)
            s.commit()
            title = d.get("title")
        found += 1
        if took_back:
            _tell(f"✅ <b>{_e(r.get('from'))}</b> answered about <b>{_e(title)}</b> and it reads like the complaint is taken back:\n"
                  f"<i>{_e(words[:300])}</i>\n\nCheck in YouTube Studio that the strike is gone. Then the uploads can be resumed: "
                  "Alerts → Strikes → Resume uploads.")
        else:
            _tell(f"📩 <b>{_e(r.get('from'))}</b> answered your request about <b>{_e(title)}</b>:\n<i>{_e((r.get('snippet') or '')[:400])}</i>\n\n"
                  "Read it in Alerts → Strikes and answer from your Gmail.")
    return found


# ---------------------------------------------------------------- the words for YouTube (never sent from here)
COUNTER_WARNING = ("A counter-notification is a legal statement made under penalty of perjury. YouTube passes it, with your "
                   "name and contact details, to the person who complained, and they can take you to court. File it only "
                   "if the licence or permission really covers this clip. If you are not sure, do not file it — ask the "
                   "creator to retract instead (above).")
STEPS_VIDEO = ["Open YouTube Studio (studio.youtube.com) and choose Content on the left.",
               "Find the video. In the Restrictions column, point at the word under it (Copyright, or Removed) and press See details.",
               "Press Select action.",
               "Choose the action (see the line below), paste the text, read every line and correct what is not true, then send it."]
STEPS_CHANNEL = ["Or: studio.youtube.com → Dashboard → the “Channel violations” card → open the strike → Appeal."]


def youtube_text(case, d: dict) -> dict:
    """What the owner can paste into YouTube Studio: {text, action, warning, note, steps}. text '' = nothing to file."""
    from .permissions import mail_identity

    sign = mail_identity()[0]
    title, link = d.get("title") or "", (f"https://youtu.be/{case.youtube_id}" if case.youtube_id else "")
    creator, orig, src = d.get("source_channel") or "", d.get("source_title") or "", d.get("source_url") or ""
    steps = STEPS_VIDEO + STEPS_CHANNEL
    if d.get("unmatched"):
        return {"text": "", "action": "", "warning": "", "steps": [
            "Open YouTube Studio (studio.youtube.com) → Dashboard and look at the “Channel violations” card.",
            "Open the strike or warning shown there: it names the video and what you can do."],
            "note": "This notice matched none of the videos uploaded from here, so no text could be prepared."}
    if case.kind == "guidelines":
        about = f" It is a short clip of “{orig}” by {creator}, shared with credit." if orig and creator and d.get("credited") else ""
        text = (f"Hello YouTube team,\n\nMy video “{title}”" + (f" ({link})" if link else "") + " was removed for a Community "
                f"Guidelines violation.{about}\n\nI have read the Community Guidelines again and I respectfully ask you to take "
                "another look at this video. I did not mean to break any rule, and I believe it may have been flagged by mistake. "
                "If I am wrong, I apologise and I will be more careful with what I upload.\n\nThank you for your time and for "
                f"reviewing my appeal.\n\n{sign}")
        return {"text": text, "action": "Choose “Appeal”.", "warning": "", "steps": steps,
                "note": "An appeal can usually be sent only once for a video, so read the text and add anything that explains "
                        "the video before you send it. The appeal box is short: shorten the text if it does not fit."}
    if case.kind != "copyright":
        return {"text": "", "action": "", "warning": "", "steps": steps,
                "note": "Open the video's details in YouTube Studio: they say what the complaint was and which actions YouTube offers."}
    mode, lic = d.get("source_mode"), d.get("license") or ""
    rng = d.get("clip_range") or [None, None]
    part = ""
    try:
        part = f" (from {int(rng[0]) // 60}:{int(rng[0]) % 60:02d} to {int(rng[1]) // 60}:{int(rng[1]) % 60:02d})"
    except (TypeError, ValueError):
        pass
    head = (f"Video: “{title}”" + (f" — {link}" if link else "") + f"\nIt is a short clip{part} of “{orig}” by {creator}"
            + (f": {src}" if src else "") + "\n\nWhy I believe I was allowed to use it:\n")
    credit = ("- My video's description credits the creator and links to the original video:\n  "
              + "\n  ".join(d.get("credit_lines") or []) + "\n") if d.get("credit_lines") else ""
    if mode == "cc" and "creative commons" in lic.lower():
        day = (d.get("source_found_at") or "")[:10]
        text = (head + f"- When my system read the original video{(' (on ' + day + ')') if day else ''}, YouTube showed it with this "
                f"licence: “{lic}”. That licence allows reuse with credit.\n" + credit
                + "\nI believe the removal was a mistake or a misidentification, because the material was used under that licence.")
        note = ("This is only a draft of the facts the system holds. If the creator has changed the licence since, or says the "
                "video was never theirs to license, do not file it.")
    elif mode == "permission" and (d.get("permission_note") or "").strip():
        text = (head + f"- The creator gave me permission. My record of it: {d['permission_note'].strip()[:600]}\n" + credit
                + "\nI believe the removal was a mistake, because the creator had allowed this use.")
        note = ("This is only a draft of the facts the system holds. If the creator took the permission back, or it did not "
                "cover this video, do not file it.")
    else:
        return {"text": "", "action": "", "warning": COUNTER_WARNING, "steps": steps,
                "note": "The system holds no licence and no permission for this clip, so there is nothing true to file with "
                        "YouTube. Do not send a counter-notification. Ask the creator to retract (above), or let the strike run "
                        "out — YouTube says a copyright strike expires after 90 days when Copyright School is completed."}
    return {"text": text, "action": "Choose “Submit counter-notification” — only after reading the warning.",
            "warning": COUNTER_WARNING, "steps": steps, "note": note}


# ---------------------------------------------------------------- what the dashboard shows
def _left_lines(case, d: dict, cnt: dict, paused: bool, yt: dict) -> list[str]:
    left = []
    mail, reply = d.get("mail") or {}, d.get("reply") or {}
    if case.status == "solved":
        return ["Nothing — you marked this as solved."]
    for f in cnt["failed"]:
        left.append(f"Set “{f['title']}” to Private yourself in YouTube Studio — the system could not ({f['error']})."
                    if not f.get("went_public") else
                    f"“{f['title']}” went public because the system could not keep it private ({f['error']}). Set it to Private "
                    "in YouTube Studio.")
    if case.clip_id and not d.get("confirmed") and case.status == "open":
        left.append("Open YouTube Studio and look at this video. If YouTube shows it as removed, the system protects the "
                    "channel by itself within minutes; if not, the mail was not about a strike — press “Mark as solved”.")
    if case.status == "retracted":
        left.append("It reads like the complaint is taken back. Check in YouTube Studio that the strike is gone, then press "
                    "Resume uploads." if paused else
                    "It reads like the complaint is taken back. Check in YouTube Studio that the strike is gone.")
    elif case.kind == "copyright" and not d.get("unmatched"):
        if case.mail_state == "ready" and mail.get("auto") and d.get("confirmed"):
            left.append("Read the request to the creator below and press “Send the request now” — or switch on automatic sending.")
        elif case.mail_state == "ready":
            left.append(f"The request to the creator is ready — send it by hand: check that {mail.get('to')} (from "
                        f"{mail.get('to_from') or 'unknown'}) is really the creator's address, then press “Send the request now”.")
        elif case.mail_state == "no_email":
            left.append("No e-mail address was found for this creator. Find it on their channel's About page (“View email "
                        "address”), paste it below and press Send — or copy the text and send it yourself.")
        elif case.mail_state == "interrupted":
            left.append("Sending the request did not finish. Look in Gmail's Sent folder; if it is not there, press Send again.")
        elif case.mail_state == "sent" and not reply:
            left.append("Wait for the creator's answer — it shows here when it comes.")
        elif reply:
            left.append("Read the creator's answer below and reply to them from your Gmail.")
    if case.status == "open":
        if yt.get("text"):
            left.append("YouTube itself cannot be answered by the system. The text for YouTube is ready: press “Copy the text for "
                        "YouTube”, then “Open YouTube Studio” and follow the steps.")
        elif case.kind in ("copyright", "other") or d.get("unmatched"):
            left.append("Open YouTube Studio and read what YouTube says about this video.")
        if d.get("confirmed") and case.kind != "copyright" and case.creator_key and case.creator_key not in blocked():
            left.append("The creator of the original video was not stopped (this was not their complaint). If you do not want "
                        "to use their videos any more, press “Stop using this creator”.")
    if cnt["public"]:
        n = len(cnt["public"])
        left.append(f"{n} other video{'' if n == 1 else 's'} from this creator {'is' if n == 1 else 'are'} public on your channel. "
                    f"The same person can complain about {'it' if n == 1 else 'them'} too — consider “Make their other videos private”.")
    if paused and case.status != "retracted":
        left.append("Uploads to this channel are paused. Press Resume uploads only when the strike is dealt with.")
    return left


def public(s, case, cache: dict | None = None) -> dict:
    """One case as the dashboard card needs it."""
    d = case.data or {}
    cnt = _counts(s, case, cache)
    dest = s.get(Destination, d.get("dest_id")) if d.get("dest_id") else None
    paused = bool(dest and dest.paused and "login expired" not in (dest.paused_reason or ""))
    yt = youtube_text(case, d)
    mail, reply = d.get("mail") or {}, d.get("reply") or {}
    key = case.creator_key or ""
    is_blocked_now = bool(key) and key in blocked()
    out = {
        "id": case.id, "kind": case.kind, "kind_label": KIND_LABEL.get(case.kind, "Notice"), "status": case.status,
        "state": case.state, "at": _iso(case.created_at), "updated_at": _iso(case.updated_at),
        "title": d.get("title") or "", "clip_id": case.clip_id, "youtube_id": case.youtube_id,
        "my_channel": d.get("my_channel") or "", "dest_id": dest.id if dest else None, "paused": paused,
        "creator": d.get("source_channel") or "", "creator_url": d.get("source_channel_url") or "",
        "source_title": d.get("source_title") or "", "source_url": d.get("source_url") or "",
        "claimant": d.get("claimant") or "", "unmatched": bool(d.get("unmatched")),
        "confirmed": bool(d.get("confirmed")), "what": _what(case, d), "done": _done_lines(case, d, cnt, paused),
        "held": cnt["held"], "private": cnt["private"], "private_failed": cnt["failed"], "public_others": cnt["public"][:30],
        "blocked": is_blocked_now, "released": bool(d.get("released")),
        "can_block": bool(key) and not is_blocked_now and case.status != "solved",
        "notices": [{k: n.get(k) for k in ("subject", "date", "excerpt", "matched_by")} for n in d.get("notices") or []],
        "mail": None, "reply": None,
        "youtube": {**yt, "studio_url": (f"https://studio.youtube.com/video/{case.youtube_id}/copyright"
                                         if case.youtube_id and case.kind == "copyright" else STUDIO),
                    "violations_url": STUDIO},
        "log": (d.get("log") or [])[-12:],
    }
    if case.kind == "copyright" and mail.get("body"):
        out["mail"] = {"state": case.mail_state, "to": mail.get("to") or "", "to_from": mail.get("to_from") or "",
                       "auto": bool(mail.get("auto")) and bool(d.get("confirmed")),
                       "subject": mail.get("subject") or "", "body": mail.get("body") or "", "sent_at": mail.get("sent_at"),
                       "sent_by": mail.get("sent_by") or "", "error": mail.get("error") or "",
                       "can_send": case.status != "solved" and case.mail_state in ("ready", "no_email", "interrupted")}
    if reply:
        out["reply"] = {"from": reply.get("from") or "", "date": reply.get("date") or "", "text": reply.get("text") or "",
                        "retracted": bool(reply.get("retracted"))}
    out["left"] = _left_lines(case, d, cnt, paused, yt)
    return out


def listing() -> list[dict]:
    with SessionLocal() as s:
        cache: dict = {}
        rows = s.query(StrikeCase).order_by(StrikeCase.id.desc()).limit(100).all()
        return [public(s, c, cache) for c in rows]


def board_items() -> list[dict]:
    """The cases as items of the Alerts page's Strikes tab (alerts.board)."""
    out = []
    for c in listing():
        level = ("bad" if c["confirmed"] else "warn") if c["status"] == "open" else "warn" if c["status"] == "retracted" else "info"
        out.append({"id": f"strike:{c['id']}:{c['status']}", "tab": "strikes", "level": level, "kind": "strike_case",
                    "label": {"open": "Strike" if c["confirmed"] else "Notice", "retracted": "Taken back?",
                              "solved": "Solved"}.get(c["status"], "Strike"),
                    "at": None if c["status"] == "open" else c["updated_at"],
                    "title": f"{c['kind_label']}: {c['title']}", "text": c["what"], "clip_id": c["clip_id"], "case": c,
                    "actions": []})
    return out


# ---------------------------------------------------------------- what the owner can press
def _case(s, case_id: int) -> StrikeCase:
    case = s.get(StrikeCase, case_id)
    if not case:
        raise KeyError(case_id)
    return case


def solve(case_id: int) -> dict:
    """Mark as solved: the case leaves the "needs you" list. Nothing else changes (the never-use list, held clips and
    the pause stay as they are)."""
    with SessionLocal() as s:
        case = _case(s, case_id)
        if case.status != "solved":
            d = _data(case)
            _log(d, "You marked this as solved.")
            case.status = "solved"
            _put(case, d)
            s.commit()
        return public(s, case)


def release(case_id: int) -> dict:
    """"Use this creator again": off the never-use list, and the held clips come back for review (never straight to
    upload). Sources stay stopped until the owner starts them; videos set to private stay private."""
    from pathlib import Path

    with SessionLocal() as s:
        case = _case(s, case_id)
        key = case.creator_key or ""
        if not key:
            raise ValueError("This case has no creator to allow again.")
        _unblock(key)
        n = 0
        for part in _chunks(_creator_projects(s, key)):
            for c in s.query(Clip).filter(Clip.project_id.in_(part), Clip.status == HELD).all():
                ready = c.held_was in ("queued", "rendered") and c.output_path and Path(c.output_path).exists()
                c.status, c.held_was, c.autoqueue, c.error = ("rendered" if ready else "draft"), None, False, None
                n += 1
        for other in s.query(StrikeCase).filter(StrikeCase.creator_key == key, StrikeCase.id != case.id).all():
            od = _data(other)  # every case of this creator: none of them puts the creator back on the list
            od["released"] = True
            _put(other, od)
        d = _data(case)
        d["released"] = True
        _log(d, f"You allowed this creator again: off the never-use list, {n} held clip{'' if n == 1 else 's'} back in Clips "
                "for review. Stopped sources stay stopped until you start them; videos set to private stay private.")
        _put(case, d)
        s.commit()
        return {"released": n, "case": public(s, case)}


def hide_public(case_id: int, limit: int = 25) -> dict:
    """The owner's press: set the creator's other public videos on the channel to private (not deleted)."""
    from . import quota

    with SessionLocal() as s:
        case = _case(s, case_id)
        rows = _counts(s, case)["public"][:limit]
        dests = {cid: did for cid, did in s.query(Clip.id, Clip.destination_id).filter(Clip.id.in_([r["clip_id"] for r in rows]))} \
            if rows else {}
    done, failed, why = 0, 0, ""
    waiting = _uploads_waiting() if rows else 0
    for r in rows:
        if not quota.can_update(waiting):  # the budget is kept for uploads: the rest after the reset
            failed, why = failed + len(rows) - done - failed, BUDGET_WHY
            break
        ok, reason, _ = _make_private(r["clip_id"], dests.get(r["clip_id"]), r["youtube_id"])
        if ok:
            done += 1
        else:
            failed, why = failed + 1, reason
            if "allowance" in reason:
                break
    with SessionLocal() as s:
        case = _case(s, case_id)
        d = _data(case)
        _log(d, f"You pressed “Make their other videos private”: {done} set to private" + (f", {failed} could not be changed ({why})." if failed else "."))
        _put(case, d)
        s.commit()
        return {"done": done, "failed": failed, "why": why, "case": public(s, case)}


# ---------------------------------------------------------------- the rounds
def _announce(case_id: int):
    """Tell the owner once what happened, what was done, and what is left (with the ready mail when it waits).
    A case opened by a mail only (not confirmed by YouTube) is told once as such; when YouTube confirms it, once more."""
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        if not case or not case.clip_id:
            return
        d = _data(case)
        flag = "told" if d.get("confirmed") else "told_mail"
        if d.get(flag):
            return
        c = public(s, case)
        d[flag] = True
        _put(case, d)
        s.commit()
    if flag == "told_mail":
        if not d.get("fresh"):
            return  # an older mail (first look into Gmail): counted in the round's one line, not told one by one
        _tell(f"📨 <b>YouTube's notice in your Gmail</b>: {_e(c['title'])}" + (f" ({_e(c['my_channel'])})" if c["my_channel"] else "")
              + f"\n{_e(c['what'])}\nOpen Alerts → Strikes.")
        return
    auto = settings()["auto"]
    text = (f"🚨 <b>{_e(c['kind_label'])}</b>: {_e(c['title'])}" + (f" ({_e(c['my_channel'])})" if c["my_channel"] else "")
            + f"\n{_e(c['what'])}\n\n<b>What the system did:</b>\n" + "\n".join("• " + _e(x) for x in (c["done"] or ["Recorded the case."]))
            + "\n\n<b>What is left for you:</b>\n" + "\n".join("• " + _e(x) for x in c["left"]))
    m = c.get("mail")
    if m and m["state"] in ("ready", "no_email"):
        if m["state"] == "ready" and auto and m.get("auto"):
            text += f"\n\nThe humble request goes to {_e(m['to'])} by itself within a few minutes."
        else:
            text += ("\n\n<b>The request to the creator" + (f" ({_e(m['to'])}, from {_e(m['to_from'])} — send it by hand)" if m["to"]
                                                            else " — no e-mail found, send it yourself")
                     + ":</b> Alerts → Strikes.")
            _tell(text, "\n" + _e(m["body"])[:2000])
            return
    _tell(text)


def _first_response(case_id: int):
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        confirmed = bool(case and (case.data or {}).get("confirmed"))
    steps = []
    if confirmed:  # only YouTube's own status acts; a copyright case also stops the creator
        steps += [("protecting the channel after a strike", _protect), ("holding clips after a strike", lambda _i: enforce()),
                  ("pausing uploads after a strike", _pause)]
    steps += [("writing the request after a strike", prepare_mail), ("telling the owner about a strike", _announce)]
    for step, fn in steps:
        try:
            fn(case_id)
        except Exception as e:  # noqa: BLE001  (one step failing must not stop the next: protection first)
            log.exception("strike %s: %s", case_id, step)
            _note("Strike responder: " + step, e)


def _unfinished() -> list[int]:
    """Cases whose first response did not finish (a restart in the middle), or whose mail has to be written again."""
    with SessionLocal() as s:
        rows = s.query(StrikeCase).filter(StrikeCase.status == "open", StrikeCase.clip_id.isnot(None)).all()
        out = []
        for c in rows:
            d = c.data or {}
            confirmed = bool(d.get("confirmed"))
            if ((confirmed and (not d.get("told") or (c.kind == "copyright" and not (d.get("did") or {}).get("at"))))
                    or (not confirmed and not d.get("told_mail"))
                    or (c.kind == "copyright" and c.mail_state in (None, "", "none") and not d.get("mail_checked"))):
                out.append(c.id)
        return out


def _lock() -> threading.Lock:
    return _locks.setdefault(tenant.current(), threading.Lock())


def _respond(new: list[int]):
    for cid in new:
        _first_response(cid)
    for cid in _unfinished():
        if cid not in new:
            _first_response(cid)


def watch(changed=None) -> dict:
    """The round that needs no switch: what YouTube itself shows (the video monitor's states, and videos.list after a
    notice), and protection — at once and again and again. changed: clip ids whose state the monitor saw change just
    now (they are never taken for history on the first run). One message to the owner at most."""
    lock = _lock()
    got = lock.acquire(timeout=60) if changed else lock.acquire(blocking=False)  # a change the monitor saw is not lost
    if not got:
        return {"busy": True}
    try:
        with _one_message():
            new = scan_monitor(changed or ())
            _respond(new)
            _resync_blocked()
            res = enforce()
            return {"new": len(new), **res}
    finally:
        lock.release()


def mail_round() -> dict:
    """The Gmail round (only for accounts that may send creator mails): YouTube's notices, requests that are due,
    and the creators' answers. It runs in the scheduler's own thread (not the render worker) and never waits for the
    lock: a busy round is simply the next one. One message to the owner at most."""
    lock = _lock()
    if not lock.acquire(blocking=False):
        return {"busy": True}
    try:
        with _one_message():
            got = scan_mail()
            confirmed = scan_monitor()  # a fresh videos.list after a notice may have confirmed a case
            _respond(list(dict.fromkeys(got["new"] + confirmed)))
            if confirmed:
                enforce()
            with SessionLocal() as s:  # a send that was cut off by a restart is never repeated by itself
                s.query(StrikeCase).filter(StrikeCase.mail_state == "sending",
                                           StrikeCase.updated_at < utcnow() - timedelta(minutes=10)) \
                    .update({"mail_state": "interrupted"}, synchronize_session=False)
                s.commit()
            sent = send_due()
            replies = check_replies()
            return {"new": len(got["new"]), "notices": got["notices"], "confirmed": len(confirmed), "sent": sent,
                    "replies": replies}
    finally:
        lock.release()

"""Strike responder: when a strike or a copyright complaint comes, protect the channel at once, ask the claimant
humbly to take it back, and prepare the words for YouTube.

What is possible, honestly:
* The YouTube API cannot list strikes, cannot name the claimant, and has NO call for an appeal or a counter-notification.
  Those exist only in YouTube Studio, and a counter-notification is a legal statement the owner must make in person.
  So YouTube itself is never answered from here: the text is prepared and the owner is told exactly where to paste it.
* A request to the CLAIMANT (the creator) to retract the complaint is an official way to clear a copyright strike.
  That mail can go out from the owner's own Gmail.

How a case is found (one record per struck video, table strike_cases):
* the video monitor saw the video rejected for copyright / the Community Guidelines / a legal or trademark complaint;
* YouTube's own notice mail in the owner's Gmail (when Gmail is connected) names one of the uploaded videos — by its
  id or its title. A notice that matches no video is kept too, so the owner sees it.
  A plain "removed" (most often the owner deleting a video) and a Content ID claim / country block are NOT cases: they
  stay in the Alerts tabs as before, and do not come with a strike.

What happens by itself, at once, with no switch (protect the channel first):
* the creator goes on the never-use list (setting "strike_blocked"), which every source type honours;
* the source(s) that point at that creator's channel are stopped;
* every waiting clip made from that creator's videos gets the status "held" (it cannot upload; no file is deleted);
* clips of that creator that are already on YouTube but scheduled (private, with a publish time) are set to private
  with no publish time, so they never go public; a failure is recorded, retried, and the owner is told loudly;
* uploads to the channel are paused (as before), and the reason says in plain words what was done and how to resume.

The mail to the creator (copyright cases): account setting "strike_reply" = {"auto": bool}, default OFF. ON: sent by
itself, once per struck video, never twice (an atomic claim on strike_cases.mail_state), at most 3 a day, only while
"Permission mails to creators" is allowed for the account. OFF: it waits in Alerts → Strikes with a Send button.
The answer is read from the same Gmail thread.

Nothing here keeps per-customer state in memory except a lock: sessions and settings follow the current customer.
"""
import copy
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from sqlalchemy import update

from . import tenant
from .db import Alert, Channel, Clip, Destination, Project, SessionLocal, StrikeCase, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

# what the video monitor saw -> the kind of case ("removed", claims and country blocks are not cases: see above)
KIND_OF_STATE = {"rejected:copyright": "copyright", "rejected:inappropriate": "guidelines", "rejected:termsOfUse": "guidelines",
                 "rejected:trademark": "other", "rejected:legal": "other"}
KIND_LABEL = {"copyright": "Copyright complaint", "guidelines": "Community Guidelines", "other": "Removed by YouTube"}
WAITING = ("draft", "fetching", "rendering", "rendered", "queued", "failed")  # not on YouTube yet -> "held"
HELD = "held"
HELD_NOTE = ("Held: a strike came from the creator of this video, so it will not be uploaded. Nothing was deleted. "
             "See Alerts → Strikes.")
PRIVATE_NOTE = ("Kept private on YouTube: a strike came from the creator of this video, so it will not go public. "
                "See Alerts → Strikes.")
BLOCKED_SKIP = "is on the never-use list (a strike came from them)"
BLOCK_KEY = "strike_blocked"  # {creator key: {name, url, at, case}}
SETTING = "strike_reply"  # {"auto": bool}
FAIL_KEY = "strike_private_fail"  # {clip id: {tries, error, at, title, youtube_id, told}}
SEEN_KEY = "strike_mail_seen"  # Gmail message ids already read
AUTO_PER_DAY = 3  # requests sent by themselves in one day
HAND_PER_DAY = 10  # all requests in one day, the ones the owner sends by hand included
QUICK_TRIES, RETRY_SOON, RETRY_LATER = 3, timedelta(minutes=2), timedelta(hours=1)  # keeping a scheduled video private
FRESH = timedelta(hours=36)  # only a strike this new pauses the channel again (an old one was paused when it happened)
WATCH_REPLY_DAYS = 90
RETRACT_HELP = "https://support.google.com/youtube/answer/2807691"
STUDIO = "https://studio.youtube.com/"
MAIL_QUERY = ('from:(youtube.com) newer_than:90d '
              '{copyright strike takedown "community guidelines" removed violation warning}')
_UC = re.compile(r"(UC[\w-]{20,})")
_locks: dict[int, threading.Lock] = {}


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


def _tell(text: str):
    """A message to the owner (Telegram / phone / own inbox). Never raises."""
    try:
        from .telegram import notify

        notify(text)
    except Exception as e:  # noqa: BLE001
        log.warning("strike notify: %s", str(e)[:200])


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

    allowed = acl.allowed("gmail.send")
    return {**settings(), "gmail": gmail.connected_email() or "", "allowed": allowed, "per_day": AUTO_PER_DAY,
            "sent_today": _sent_today(),
            "note": "" if allowed else acl.message("gmail.send")}


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
    """The list is also written in the cases: a creator of a case who is missing from it (a backup restored on a new
    server brings the cases but not this setting) goes back on — unless the owner allowed them again."""
    with SessionLocal() as s:
        rows = [(c.id, c.creator_key, c.data or {}) for c in s.query(StrikeCase).filter(StrikeCase.creator_key != "",
                                                                                      StrikeCase.creator_key.isnot(None))]
    if not rows:
        return
    bl = blocked()
    allowed_again = {key for _i, key, d in rows if d.get("released")}
    for cid, key, d in rows:
        if key not in bl and key not in allowed_again and (d.get("did") or {}).get("blocked"):
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


def _new_case(s, c: Clip, kind: str, state: str, fresh: bool) -> StrikeCase:
    p = s.get(Project, c.project_id) if c.project_id else None
    d = _facts(s, c, p)
    d["fresh"] = bool(fresh)
    own = d["source_mode"] in ("original", "own")  # my own content: there is no other creator to stop or to write to
    case = StrikeCase(clip_id=c.id, youtube_id=c.youtube_id, kind=kind, state=state, status="open",
                      creator_key="" if own else creator_key(d["source_channel_url"]), mail_state="none",
                      created_at=utcnow(), updated_at=utcnow())
    _log(d, {"copyright": "A copyright complaint was found for this video.",
             "guidelines": "A Community Guidelines removal was found for this video."}.get(kind, "YouTube removed this video."))
    case.data = d
    s.add(case)
    s.flush()
    return case


# ---------------------------------------------------------------- detection: the video monitor
def scan_monitor() -> list[int]:
    """Uploaded videos the monitor saw rejected for a strike-like reason and that have no case yet -> new case ids."""
    new = []
    with SessionLocal() as s:
        rows = s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None),
                                    Clip.yt_state.in_(list(KIND_OF_STATE))).all()
        if not rows:
            return new
        have = {cid for (cid,) in s.query(StrikeCase.clip_id).filter(StrikeCase.clip_id.isnot(None))}
        for c in rows:
            if c.id in have:
                continue
            since = None  # when the monitor first saw this state (its alert row)
            for a in s.query(Alert).filter(Alert.clip_id == c.id).order_by(Alert.id.desc()).limit(20):
                if (a.details or {}).get("state") == c.yt_state:
                    since = a.created_at
                    break
            case = _new_case(s, c, KIND_OF_STATE[c.yt_state], c.yt_state, bool(since and since > utcnow() - FRESH))
            new.append(case.id)
        s.commit()
    return new


# ---------------------------------------------------------------- detection: YouTube's notice mails
_COPY = re.compile(r"copyright|content id|takedown|infring", re.I)
_GUIDE = re.compile(r"community guidelines|guidelines strike|violat\w* (of )?(our|youtube'?s|the) (community )?(guidelines|polic)|"
                    r"\bwarning\b", re.I)
_HARD = re.compile(r"strike|take[\s-]?down|taken down|removed|removal|deleted|violat|\bwarning\b", re.I)
_GOOD = re.compile(r"retract|released|reinstated|\brestored\b|has expired|have expired|withdrawn|resolved|"
                   r"strike (has been |was |is )?(removed|lifted)|appeal (was|has been|is) (approved|accepted|successful)|"
                   r"good news", re.I)
# a subject this clear, with no video that matches, stops the uploads until the owner has looked
_STRONG = re.compile(r"strike|take[\s-]?down|(video|content|channel).{0,60}(removed|taken down|deleted)|"
                     r"(removed|taken down|deleted).{0,60}(video|content)", re.I)
_NOT_A_VIDEO = re.compile(r"\bcomments?\b|\bplaylists?\b|\bpost\b", re.I)
_CLAIMANT = [
    re.compile(r"(?im)^[ \t>*•\-]*(?:claimant|copyright owner|complainant|removed by|content removed by|claimed by|"
               r"rights? ?holder|submitted by|reported by)\s*[:\-–]\s*(.{2,120})$"),
    re.compile(r"(?i)(?:removal request|takedown (?:notice|request)|copyright complaint|complaint|notification)\s+"
               r"(?:from|by|submitted by)\s+([^\n.,;:(]{2,80})"),
    re.compile(r"(?i)\b(?:claimed|removed|taken down) by\s+([^\n.,;:(]{2,80})"),
]
_TITLE_LINE = re.compile(r"(?im)^[ \t>*•\-]*(?:video title|video name|video|title)\s*[:\-–]\s*(.{3,200})$")
_VIDEO_URL = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^\s\"'<>]*&)?v=|shorts/|video/|embed/)|youtu\.be/|"
                        r"studio\.youtube\.com/video/)([A-Za-z0-9_-]{6,32})")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_OWN_DOMAINS = ("youtube.com", "google.com", "googlemail.com")


def _from_youtube(sender: str) -> bool:
    addr = parseaddr(sender or "")[1].lower()
    dom = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    return bool(dom) and (dom in _OWN_DOMAINS[:2] or dom.endswith((".youtube.com", ".google.com")))


def parse_notice(msg: dict) -> dict | None:
    """One Gmail message -> what kind of notice it is, or None when it is not a strike / removal notice from YouTube.
    The wording of these mails changes, so nothing here depends on one layout: the sender must be YouTube, the subject
    or the text must speak of a removal / strike / copyright, and everything else (video, claimant, address) is
    picked up when it is there."""
    if not isinstance(msg, dict) or not _from_youtube(msg.get("from") or ""):
        return None
    subject = re.sub(r"\s+", " ", str(msg.get("subject") or "")).strip()
    body = str(msg.get("text") or msg.get("snippet") or "")
    text = subject + "\n" + body
    if not (_HARD.search(subject) or _COPY.search(subject) or _GUIDE.search(subject)):
        return None  # newsletters and the like mention these words in the text; a notice says it in the subject
    if _GOOD.search(subject):  # the subject only: a strike mail itself explains further down how a retraction works
        kind, typ = ("copyright" if _COPY.search(text) else "guidelines" if _GUIDE.search(text) else "other"), "resolved"
    else:
        if _COPY.search(subject):
            kind = "copyright"
        elif _GUIDE.search(subject):
            kind = "guidelines"
        else:
            c, g = _COPY.search(body), _GUIDE.search(body)
            kind = "copyright" if c and (not g or c.start() < g.start()) else "guidelines" if g else "other"
        typ = "strike"
        if kind == "copyright" and re.search(r"\bclaim", subject, re.I) and not re.search(
                r"strike|take[\s-]?down|taken down|removed|removal|deleted", subject, re.I):
            typ = "claim"  # a Content ID claim: no strike, nothing to answer
    claimant = ""
    for rx in _CLAIMANT:
        m = rx.search(body)
        if m:
            claimant = re.sub(r"\s+", " ", m.group(1)).strip(" .,:;\"'“”")[:120]
            if claimant:
                break
    me = ""
    try:
        from . import gmail

        me = (gmail.connected_email() or "").lower()
    except Exception:  # noqa: BLE001
        pass
    mails = [x.strip(".").lower() for x in _EMAIL.findall(body)]
    mails = [x for x in mails if x != me and not x.rsplit("@", 1)[-1].endswith(_OWN_DOMAINS)
             and not x.endswith((".png", ".jpg", ".gif"))]
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
            "claimant": claimant, "claimant_email": mails[0] if mails else "",
            "about_video": bool(_HARD.search(subject)) and not _NOT_A_VIDEO.search(subject),
            "hard_subject": bool(_STRONG.search(subject)) and not _NOT_A_VIDEO.search(subject), "at": at}


def _norm(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().lower()


def _link(s, n: dict):
    """The uploaded clip a notice is about: (clip row or None, how it was matched)."""
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
    low = _norm(text)
    named = {_norm(t) for t in n["titles"] if len(_norm(t)) >= 4}
    cands = [u for u in ups if u.title and (_norm(u.title) in named or (len(_norm(u.title)) >= 10 and _norm(u.title) in low))]
    if not cands:
        return None, ""
    rank = lambda u: (len(_norm(u.title)), (u.yt_state or "ok") != "ok", u.attempted_at or datetime.min)  # noqa: E731
    return max(cands, key=rank), "video title"


def scan_mail() -> dict:
    """Read YouTube's recent notice mails (Gmail connected) and open / update cases. Returns {"new": [case ids], ...}."""
    from . import gmail

    out = {"new": [], "read": 0, "notices": 0}
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
            if n and n["type"] != "claim":
                used, cid = _apply_notice(n)
                out["notices"] += 1 if used else 0
                if cid:
                    out["new"].append(cid)
        except Exception as e:  # noqa: BLE001  (a layout nobody expected must never stop the others)
            log.warning("strike mail parse: %s", str(e)[:200])
            _note("Understanding a notice from YouTube", e)
        seen.append(mid)
    if out["read"]:
        set_setting(SEEN_KEY, json.dumps(seen[-600:]))
    return out


def _notice_row(n: dict, how: str = "") -> dict:
    return {"id": n["id"], "subject": n["subject"], "from": n["from"], "date": n["date"], "excerpt": n["excerpt"],
            "kind": n["kind"], "type": n["type"], "matched_by": how}


def _apply_notice(n: dict) -> tuple[bool, int | None]:
    """Attach a notice to its case (opening one when needed). Returns (was it used, the id of a NEW case or None)."""
    fresh = bool(n["at"] and n["at"] > utcnow() - FRESH)
    with SessionLocal() as s:
        clip_row, how = _link(s, n)
        if clip_row is None:
            if (n["type"] != "strike" or not n["about_video"]
                    or s.query(StrikeCase).filter(StrikeCase.notice_id == n["id"]).first()):
                return False, None  # good news / a newsletter / about a comment: nothing to keep
            d = {"title": n["subject"] or "A notice from YouTube", "notices": [_notice_row(n)], "claimant": n["claimant"],
                 "claimant_email": n["claimant_email"], "fresh": fresh, "unmatched": True}
            _log(d, "A notice from YouTube was found in your Gmail. It matched none of the videos uploaded from here.")
            case = StrikeCase(notice_id=n["id"], kind=n["kind"], state="mail", status="open", creator_key="",
                              mail_state="none", data=d, created_at=utcnow(), updated_at=utcnow())
            s.add(case)
            s.flush()
            if fresh and n["hard_subject"]:  # a strike nobody can place: stop uploading until the owner has looked
                names = []
                for dest in s.query(Destination).filter(Destination.paused.isnot(True)).all():
                    dest.paused, dest.paused_at = True, utcnow()
                    dest.paused_reason = (f"YouTube sent a notice (“{(n['subject'] or '')[:120]}”) that could not be matched to one of "
                                          "your videos. Uploads wait so nothing makes it worse. Open Alerts → Strikes, read it, "
                                          "check YouTube Studio, then press Resume uploads.")
                    names.append(dest.name or "")
                if names:
                    d2 = _data(case)
                    d2["paused_all"] = names
                    _log(d2, "Uploads were paused on: " + ", ".join(names) + ".")
                    _put(case, d2)
            s.commit()
            cid = case.id
            _tell(f"🚨 <b>A notice from YouTube</b> could not be matched to one of your videos:\n<i>{_e(n['subject'])}</i>\n"
                  + ("Uploads are paused until you have looked. " if fresh and n["hard_subject"] else "")
                  + "Open Alerts → Strikes and check YouTube Studio.")
            return True, cid
        case = s.query(StrikeCase).filter(StrikeCase.clip_id == clip_row.id).order_by(StrikeCase.id.desc()).first()
        if n["type"] == "resolved":
            if not case or case.status != "open":
                return False, None
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
            case = _new_case(s, c, n["kind"], c.yt_state if (c.yt_state or "") in KIND_OF_STATE else "mail", fresh)
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
            d["claimant_email"] = n["claimant_email"]
        if case.kind == "other" and n["kind"] != "other":
            case.kind = n["kind"]  # the mail says what the bare removal was about
        _log(d, f"YouTube's notice was found in your Gmail (“{n['subject'][:100]}”, matched by the {how}).")
        if got_address and case.mail_state in ("ready", "no_email"):
            case.mail_state = "none"  # written again, to the address YouTube gave
            d.pop("mail", None)
            d.pop("mail_checked", None)
        _put(case, d)
        s.commit()
        return True, new_id


# ---------------------------------------------------------------- protection
def _protect(case_id: int):
    """Once per case: never-use list, stop the creator's source(s), a mail-found copyright video is set to private."""
    self_private = None
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        if not case or not case.clip_id:
            return
        d = _data(case)
        did = d.setdefault("did", {})
        if did.get("at"):
            return
        key = case.creator_key or ""
        clip = s.get(Clip, case.clip_id)
        p = s.get(Project, clip.project_id) if clip and clip.project_id else None
        if key:
            _block(key, d.get("source_channel") or "", d.get("source_channel_url") or "", case.id)
            did["blocked"] = True
            _log(d, f"{d.get('source_channel') or 'The creator'} was put on the never-use list: no source uses their videos again.")
            stopped = []
            for ch in s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind == "channel")).all():
                if ch.mode == "own":
                    continue
                if creator_key(ch.resolved_url) == key or creator_key(ch.query) == key or (p is not None and p.channel_ref == ch.id):
                    if ch.auto:
                        ch.auto = False
                    ch.last_status = ("stopped: a strike came from this creator, so this source will not run again "
                                      "(Alerts → Strikes)")
                    stopped.append({"id": ch.id, "name": ch.name or ch.query or f"Source #{ch.id}"})
            did["sources"] = stopped
            if stopped:
                _log(d, "Stopped the source" + ("s" if len(stopped) > 1 else "") + ": "
                     + ", ".join("“" + x["name"] + "”" for x in stopped) + ".")
        if case.state == "mail" and case.kind == "copyright" and clip is not None and clip.youtube_id and clip.status == "uploaded":
            self_private = (clip.id, clip.destination_id, clip.youtube_id)
        did["at"] = _iso(utcnow())
        _put(case, d)
        s.commit()
    if self_private:  # YouTube's mail came before the monitor saw anything: take the video out of public view
        cid, dest_id, yid = self_private
        state, why = "", ""
        try:
            from . import youtube

            youtube.keep_private(dest_id, yid)
            state = "private"
        except LookupError:
            state = "gone"
        except Exception as e:  # noqa: BLE001
            why = _why(e)
        with SessionLocal() as s:
            case = s.get(StrikeCase, case_id)
            d = _data(case)
            d["did"]["own_video"] = state or "failed"
            d["did"]["own_video_error"] = why
            if state == "private":
                s.execute(update(Clip).where(Clip.id == cid).values(kept_private=True, live_noted=True, error=PRIVATE_NOTE))
                _log(d, "The video itself was set to private on YouTube (not deleted).")
            elif not state:
                _log(d, f"The video itself could not be set to private: {why}.")
            _put(case, d)
            s.commit()


def _why(e: BaseException) -> str:
    """Why a change on YouTube did not work, in plain words."""
    name, m = type(e).__name__, str(e)
    if name == "NeedsReconnect":
        return ("this channel was connected before the system could change videos — connect it once more (My channels → "
                "Connect a YouTube channel → the same channel, tick every box)")
    if name in ("RefreshError", "TokenError") or "invalid_grant" in m:
        return "the channel's Google login expired — reconnect the channel (My channels)"
    if "quotaExceeded" in m or "dailyLimitExceeded" in m:
        return "today's YouTube allowance is used up — it is tried again after the daily reset"
    if name == "HttpError":
        try:
            from .comments import _reason

            status, why, msg = _reason(e)
            if why in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT") or "insufficient" in msg.lower():
                return ("this channel's login may not change videos — connect it once more (My channels → Connect a YouTube "
                        "channel → the same channel, tick every box)")
            return "YouTube said: " + msg[:160]
        except Exception:  # noqa: BLE001
            pass
    return (m[:160] or name)


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
    sched = [r for r in rows if r.status == "uploaded" and r.youtube_id]
    if sched:
        fails = _load(FAIL_KEY, {})
        changed = False
        for r in sched:
            f = fails.get(str(r.id)) or {}
            if (f.get("next") or "") > _iso(utcnow()):  # tried a moment ago: not again in the same breath
                out["private_failed"] += 1
                continue
            ok, why, counted = _make_private(r.id, r.destination_id, r.youtube_id)
            if ok:
                out["private"] += 1
                if fails.pop(str(r.id), None) is not None:
                    changed = True
                continue
            out["private_failed"] += 1
            tries = int(f.get("tries") or 0) + (1 if counted else 0)
            # it is tried until the video's publish time: every few minutes at first, then once an hour (no giving up —
            # the owner may reconnect the channel tomorrow, and the video must still not go public next week)
            wait = RETRY_SOON if tries <= QUICK_TRIES else RETRY_LATER
            f = {"tries": tries, "error": why, "at": _iso(utcnow()), "next": _iso(utcnow() + wait),
                 "title": r.title or f"Clip #{r.id}", "youtube_id": r.youtube_id, "publish_at": _iso(r.publish_at),
                 "told": bool(f.get("told"))}
            if not f["told"]:
                f["told"] = True
                _tell(f"🚨 <b>Could NOT keep a scheduled video private</b>: {_e(f['title'])}\nA strike came from its creator, and it "
                      f"goes public on {r.publish_at:%d %b %Y %H:%M} UTC.\nWhy: {_e(why)}.\nOpen YouTube Studio now and set it to "
                      f"Private yourself: https://studio.youtube.com/video/{r.youtube_id}/edit")
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
    """What the system already did, in plain sentences."""
    did, lines = d.get("did") or {}, []
    creator = d.get("source_channel") or "the creator"
    for x in did.get("sources") or []:
        lines.append(f"Stopped the source “{x.get('name')}”.")
    if did.get("blocked"):
        lines.append(f"Put {creator} on the never-use list — no source will use their videos again."
                     if not d.get("released") else f"{creator} was on the never-use list; you allowed them again.")
    if cnt["held"]:
        n = cnt["held"]
        lines.append(f"Held {n} waiting clip{'' if n == 1 else 's'} made from their videos ({'it' if n == 1 else 'they'} will not "
                     "upload; nothing was deleted).")
    if cnt["private"]:
        n = len(cnt["private"])
        lines.append(f"Kept {n} video{'' if n == 1 else 's'} of theirs private on YouTube ({'it' if n == 1 else 'they'} will not go public; "
                     "nothing was deleted).")
    if did.get("own_video") == "private":
        lines.append("Set the video itself to private on YouTube (not deleted).")
    if d.get("paused_all"):
        lines.append("Paused uploads on: " + ", ".join(d["paused_all"]) + ".")
    elif did.get("paused") and paused:
        lines.append(f"Paused uploads to {d.get('my_channel') or 'the channel'}.")
    return lines


def _pause(case_id: int):
    """The channel's uploads wait (as before), and the reason says plainly what was done and how to resume."""
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        if not case or not case.clip_id:
            return
        d = _data(case)
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
        return "YouTube sent a notice that could not be matched to one of the videos uploaded from here."
    if case.kind == "copyright":
        who = f" by {d['claimant']}" if d.get("claimant") else ""
        if (case.state or "").startswith("rejected"):
            return (f"YouTube removed this video after a copyright complaint{who}. This usually comes with a copyright strike "
                    "(three strikes close a channel).")
        return f"YouTube sent a copyright notice about this video{who}."
    if case.kind == "guidelines":
        if (case.state or "").startswith("rejected"):
            return ("YouTube removed this video for breaking its Community Guidelines. This can come with a Community "
                    "Guidelines strike.")
        return "YouTube sent a Community Guidelines notice about this video."
    if case.state == "rejected:trademark":
        return "YouTube removed this video after a trademark complaint."
    if case.state == "rejected:legal":
        return "YouTube removed this video for a legal reason."
    return "YouTube sent a notice about this video."


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
    own = (d.get("did") or {}).get("own_video")
    if (case.state or "").startswith("rejected") or own == "gone":
        action = "The clip is no longer on YouTube. "
    elif own == "private":
        action = "The clip is no longer public. "
    else:
        action = ""
    who = re.sub(r"\s+", " ", name or d.get("source_channel") or "").strip()[:80] or "there"
    fill = dict(name=who, orig=orig, orig_short=short, title=title, link=link, credit=credit, why=why, action=action,
                help=RETRACT_HELP, sign=sign)
    return subj.format(**fill)[:150], body.format(**fill)


def _recipient(case, d: dict) -> tuple[str, str]:
    """(address, where it came from) — YouTube's notice, then the permission inbox, then their channel description."""
    from . import gmail

    me = (gmail.connected_email() or "").lower()
    good = lambda a: bool(a) and bool(gmail.EMAIL_RE.match(a)) and a.lower() != me  # noqa: E731
    if good(d.get("claimant_email")):
        return d["claimant_email"], "YouTube's notice"
    key = case.creator_key or ""
    if key.startswith("UC"):
        try:
            from . import permissions

            c = permissions._load().get(key) or {}
            if good(c.get("email")):
                return c["email"], "your permission inbox"
            if not d.get("email_checked"):
                d["email_checked"] = True
                found = permissions.lookup_emails([key]).get(key)
                if good(found):
                    return found, "their channel description"
        except Exception as e:  # noqa: BLE001
            _note("Looking up a creator's e-mail after a strike", e)
    return "", ""


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
        to, where = _recipient(case, d)
        name = d.get("claimant") if where == "YouTube's notice" and d.get("claimant") else ""
        subject, body = compose(case, d, name)
        d["mail"] = {"to": to, "to_from": where, "subject": subject, "body": body, "written_at": _iso(utcnow())}
        _log(d, f"The request to {name or d.get('source_channel') or 'the creator'} was written"
             + (f" (to {to}, from {where})." if to else ". No e-mail address was found for them."))
        case.mail_state = "ready" if to else "no_email"
        _put(case, d)
        s.commit()


def _sent_today() -> int:
    try:
        return int(get_setting("strike_sent:" + utcnow().strftime("%Y-%m-%d")) or 0)
    except (TypeError, ValueError):
        return 0


def send_request(case_id: int, to: str | None = None, by: str = "hand") -> dict:
    """Send the request from the owner's Gmail. Once per struck video: the claim on mail_state is atomic, and a send
    that may or may not have gone out is never repeated by itself. Raises ValueError with words for the owner."""
    from . import acl, gmail

    if not acl.allowed("gmail.send"):
        raise ValueError(acl.message("gmail.send"))
    if not gmail.connected_email():
        raise ValueError("Gmail is not connected. Connect it in My channels → Gmail, then press Send again.")
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
        if to is not None and str(to).strip():
            to = str(to).strip()
            if not gmail.EMAIL_RE.match(to):
                raise ValueError("That email address does not look right.")
            mail["to"], mail["to_from"] = to, "typed by you"
        if not mail.get("to"):
            raise ValueError("No e-mail address is known for this creator. Find it on their channel's About page "
                             "(“View email address”), paste it into the box and press Send again.")
        cap = AUTO_PER_DAY if by == "auto" else HAND_PER_DAY
        if _sent_today() >= cap:
            raise ValueError(f"{cap} requests were already sent today — more would look like spam. The rest go tomorrow.")
        was = case.mail_state
        d["mail"] = mail
        _put(case, d)
        s.commit()
        res = s.execute(update(StrikeCase).where(StrikeCase.id == case_id, StrikeCase.mail_state == was)
                        .values(mail_state="sending", updated_at=utcnow()))
        s.commit()
        if res.rowcount != 1:
            raise ValueError("The request is being sent right now.")
    err, sent = None, None
    try:
        sent = gmail.send(mail["to"], mail["body"], mail.get("subject"), kind="strike")
    except ValueError as e:  # refused before anything left (a bad address): it can be sent again
        err, state = str(e), ("ready" if was in ("ready", "interrupted") else was)
    except Exception as e:  # noqa: BLE001  (it may or may not have gone out: never again by itself)
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
            set_setting("strike_sent:" + utcnow().strftime("%Y-%m-%d"), str(_sent_today() + 1))
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
    """The automatic round: every ready request goes out, at most 3 a day — only when the account switched it on."""
    from . import acl, gmail

    if not settings()["auto"] or not acl.feature("gmail.send") or not gmail.connected_email():
        return 0
    with SessionLocal() as s:
        ids = [i for (i,) in s.query(StrikeCase.id).filter(StrikeCase.kind == "copyright", StrikeCase.status == "open",
                                                           StrikeCase.mail_state == "ready").order_by(StrikeCase.id)]
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
    if (d.get("did") or {}).get("own_video") == "failed":
        left.append("Set the video itself to Private in YouTube Studio — the system could not ("
                    + ((d.get("did") or {}).get("own_video_error") or "it did not work") + ").")
    if case.status == "retracted":
        left.append("It reads like the complaint is taken back. Check in YouTube Studio that the strike is gone, then press "
                    "Resume uploads." if paused else
                    "It reads like the complaint is taken back. Check in YouTube Studio that the strike is gone.")
    elif case.kind == "copyright" and not d.get("unmatched"):
        if case.mail_state == "ready":
            left.append("Read the request to the creator below and press “Send the request now” — or switch on automatic sending.")
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
    if d.get("paused_all"):
        paused = s.query(Destination).filter(Destination.paused.is_(True)).count() > 0
    yt = youtube_text(case, d)
    mail, reply = d.get("mail") or {}, d.get("reply") or {}
    key = case.creator_key or ""
    out = {
        "id": case.id, "kind": case.kind, "kind_label": KIND_LABEL.get(case.kind, "Notice"), "status": case.status,
        "state": case.state, "at": _iso(case.created_at), "updated_at": _iso(case.updated_at),
        "title": d.get("title") or "", "clip_id": case.clip_id, "youtube_id": case.youtube_id,
        "my_channel": d.get("my_channel") or "", "dest_id": dest.id if dest else None, "paused": paused,
        "creator": d.get("source_channel") or "", "creator_url": d.get("source_channel_url") or "",
        "source_title": d.get("source_title") or "", "source_url": d.get("source_url") or "",
        "claimant": d.get("claimant") or "", "unmatched": bool(d.get("unmatched")),
        "what": _what(case, d), "done": _done_lines(case, d, cnt, paused),
        "held": cnt["held"], "private": cnt["private"], "private_failed": cnt["failed"], "public_others": cnt["public"][:30],
        "blocked": bool(key) and key in blocked(), "released": bool(d.get("released")),
        "notices": [{k: n.get(k) for k in ("subject", "date", "excerpt", "matched_by")} for n in d.get("notices") or []],
        "mail": None, "reply": None,
        "youtube": {**yt, "studio_url": (f"https://studio.youtube.com/video/{case.youtube_id}/copyright"
                                         if case.youtube_id and case.kind == "copyright" else STUDIO),
                    "violations_url": STUDIO},
        "log": (d.get("log") or [])[-12:],
    }
    if case.kind == "copyright" and mail.get("body"):
        out["mail"] = {"state": case.mail_state, "to": mail.get("to") or "", "to_from": mail.get("to_from") or "",
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
        level = "bad" if c["status"] == "open" else "warn" if c["status"] == "retracted" else "info"
        out.append({"id": f"strike:{c['id']}:{c['status']}", "tab": "strikes", "level": level, "kind": "strike_case",
                    "label": {"open": "Strike", "retracted": "Taken back?", "solved": "Solved"}.get(c["status"], "Strike"),
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
    with SessionLocal() as s:
        case = _case(s, case_id)
        rows = _counts(s, case)["public"][:limit]
        dests = {cid: did for cid, did in s.query(Clip.id, Clip.destination_id).filter(Clip.id.in_([r["clip_id"] for r in rows]))} \
            if rows else {}
    done, failed, why = 0, 0, ""
    for r in rows:
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
    """Tell the owner once what happened, what was done, and what is left (with the ready mail when it waits)."""
    with SessionLocal() as s:
        case = s.get(StrikeCase, case_id)
        if not case or not case.clip_id:
            return
        d = _data(case)
        if d.get("told"):
            return
        c = public(s, case)
        d["told"] = True
        _put(case, d)
        s.commit()
    auto = settings()["auto"]
    text = (f"🚨 <b>{_e(c['kind_label'])}</b>: {_e(c['title'])}" + (f" ({_e(c['my_channel'])})" if c["my_channel"] else "")
            + f"\n{_e(c['what'])}\n\n<b>What the system did:</b>\n" + "\n".join("• " + _e(x) for x in (c["done"] or ["Recorded the case."]))
            + "\n\n<b>What is left for you:</b>\n" + "\n".join("• " + _e(x) for x in c["left"]))
    m = c.get("mail")
    if m and m["state"] in ("ready", "no_email"):
        if m["state"] == "ready" and auto:
            text += f"\n\nThe humble request goes to {_e(m['to'])} by itself within a few minutes."
        else:
            text += ("\n\n<b>The request to the creator" + (f" ({_e(m['to'])})" if m["to"] else " — no e-mail found, send it yourself")
                     + ":</b>\n" + _e(m["body"])[:2200])
    _tell(text)


def _first_response(case_id: int):
    for step, fn in (("protecting the channel after a strike", _protect), ("holding clips after a strike", lambda _i: enforce()),
                     ("pausing uploads after a strike", _pause), ("writing the request after a strike", prepare_mail),
                     ("telling the owner about a strike", _announce)):
        try:
            fn(case_id)
        except Exception as e:  # noqa: BLE001  (one step failing must not stop the next: protection first)
            log.exception("strike %s: %s", case_id, step)
            _note("Strike responder: " + step, e)


def _unfinished() -> list[int]:
    """Cases whose first response did not finish (a restart in the middle), or whose mail has to be written again."""
    with SessionLocal() as s:
        rows = s.query(StrikeCase).filter(StrikeCase.status == "open", StrikeCase.clip_id.isnot(None)).all()
        return [c.id for c in rows if not (c.data or {}).get("told")
                or (c.kind == "copyright" and c.mail_state in (None, "", "none") and not (c.data or {}).get("mail_checked"))]


def _lock() -> threading.Lock:
    return _locks.setdefault(tenant.current(), threading.Lock())


def watch() -> dict:
    """The round that needs no switch: new cases from the video monitor, and protection — at once and again and again."""
    lock = _lock()
    if not lock.acquire(blocking=False):
        return {"busy": True}
    try:
        new = scan_monitor()
        for cid in new:
            _first_response(cid)
        for cid in _unfinished():
            if cid not in new:
                _first_response(cid)
        _resync_blocked()
        res = enforce()
        return {"new": len(new), **res}
    finally:
        lock.release()


def mail_round() -> dict:
    """The Gmail round (only for accounts that may send creator mails): YouTube's notices, requests that are due,
    and the creators' answers."""
    lock = _lock()
    if not lock.acquire(timeout=120):
        return {"busy": True}
    try:
        got = scan_mail()
        for cid in got["new"]:
            _first_response(cid)
        if got["notices"]:
            for cid in _unfinished():  # a notice gave an address: the request is written again, to it
                if cid not in got["new"]:
                    _first_response(cid)
        with SessionLocal() as s:  # a send that was cut off by a restart is never repeated by itself
            s.query(StrikeCase).filter(StrikeCase.mail_state == "sending",
                                       StrikeCase.updated_at < utcnow() - timedelta(minutes=10)) \
                .update({"mail_state": "interrupted"}, synchronize_session=False)
            s.commit()
        sent = send_due()
        replies = check_replies()
        return {"new": len(got["new"]), "notices": got["notices"], "sent": sent, "replies": replies}
    finally:
        lock.release()

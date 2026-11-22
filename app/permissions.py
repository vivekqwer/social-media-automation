"""Permission inbox: creators of popular non-Creative-Commons videos, with a ready message asking to use their clips.

The owner sends the message (automatic messages to strangers are spam, and YouTube has no API for it).
When a creator says yes, one click (or a Telegram reply) turns them into a permission source that clips and
uploads their videos automatically, with credit.
"""
import json
import logging
import re

from .db import Channel, Destination, SessionLocal, get_setting, set_setting, utcnow, main_destination

log = logging.getLogger(__name__)
KEY = "creators"
PER_DAY = 5
# music labels, film studios and TV networks: they never give fan pages permission and their Content ID strikes
LABELS = re.compile(r"music|records|songs|films|movies|studios|pictures|t-series|zee|sony|star |colors|tips official|"
                    r"saregama|aditya|lahari|speed records|bhajan|bhakti|aarti|wave|yrf|dharma|netflix|prime video|news", re.I)

# Written like a person would write it: short, plain, a little informal, different for each creator,
# naming the video the owner actually liked. No emojis or polished marketing lines.
TEMPLATES = [  # always English (owner's choice); three versions so the mails are not identical
    ("Quick question about your videos",
     "Hey {name},\n\nI've been watching your videos for a while, the \"{video}\" one especially. I run a small channel "
     "where I post {about} Shorts.\n\nWould you be okay with me posting short clips from your videos there? I'd put your name and "
     "a link to the full video in every one.\n\nIf not, no worries at all. And if you ever want something taken down, just "
     "reply here and I'll do it straight away.\n\nThanks,\n{sign}"),
    ("About your video \"{video}\"",
     "Hi {name},\n\nI just watched \"{video}\" and really enjoyed it. I have a small YouTube channel where I post "
     "{about} Shorts.\n\nCould I use short clips (30-40 seconds) from your videos? Every clip would carry your name and a link "
     "to the original video, so people can find your channel.\n\nA simple yes or no is fine. If you say no, I won't use "
     "anything, and you can ask me to remove a clip at any time.\n\nThank you,\n{sign}"),
    ("A small request",
     "Hello {name},\n\nBig fan of your content. \"{video}\" was great. I run a small channel of {about} Shorts and "
     "I'd love to share a few short clips from your videos with full credit: your name and the link to the full video "
     "in each one.\n\nWould that be alright with you? Just reply yes or no. If it's a no, that's completely fine, and "
     "I'll take down anything you ask me to.\n\nThanks a lot,\n{sign}"),
]


def _clean_title(t: str) -> str:
    t = re.split(r"\s[|•/]\s|\s-\s", t or "")[0]  # "Title | Channel name" -> "Title"
    import unicodedata

    t = re.sub(r"#\S+", " ", t)  # no hashtags
    t = "".join(" " if unicodedata.category(ch)[0] == "S" or ch in "️|[]{}<>\"*_~^" or unicodedata.category(ch) in ("Co", "Cs")
                else ch for ch in t)  # no emojis / symbols (Hindi vowel signs are kept)
    t = re.sub(r"\s+", " ", t).strip(" -|,.")
    return (t[:60].rsplit(" ", 1)[0] if len(t) > 60 else t) or "aapki latest video"


def compose(name: str, video_title: str, cid: str) -> tuple[str, str]:
    """(subject, body) — the template is picked from the channel id, so each creator gets one version, always the same."""
    sign, about = mail_identity()
    subj, body = TEMPLATES[sum(map(ord, cid)) % len(TEMPLATES)]
    video = _clean_title(video_title)
    if video == "aapki latest video":  # nothing usable in the title
        video = "your latest video"
    return subj.format(video=video)[:150], body.format(name=(name or "there").strip(), video=video, about=about, sign=sign)


DEFAULT_SIGN, DEFAULT_ABOUT = "Funny Gunction", "funny"


def mail_identity() -> tuple[str, str]:
    """(name the mails are signed with, kind of channel) — set on the dashboard; the channel's own name is not used."""
    return get_setting("mail_sign") or DEFAULT_SIGN, get_setting("mail_about") or DEFAULT_ABOUT


MESSAGE = (
    "Namaste {name} ji! Main aapka bada fan hoon aur aapke videos ke chhote clips (Shorts) ek fan page par post karna chahta hoon — "
    "har clip me aapka naam, channel link aur original video ka link credit me rahega. Kya aap mujhe aapke videos ke clips "
    "use karne ki permission denge? Aap kabhi bhi mana kar sakte hain aur main turant hata dunga. Shukriya! 🙏\n\n"
    "Hi {name}! I'm a big fan and run a fan page. May I post short clips from your videos with full credit "
    "(your name, channel link and the original video link in every clip)? You can say no at any time and I'll remove them. Thank you!"
)


def _load() -> dict:
    raw = get_setting(KEY)
    return json.loads(raw) if raw else {}


def _save(d: dict):
    set_setting(KEY, json.dumps(d))


def refresh_inbox() -> list[dict]:
    """Add today's creators of popular non-CC videos (max 5 new a day) and tell the owner on Telegram."""
    from . import youtube
    from .competitors import overview, trending

    d = _load()
    today = utcnow().strftime("%Y-%m-%d")
    added = [c for c in d.values() if c.get("added") == today]
    candidates = []
    try:
        candidates += [v for v in trending("") if not v.get("cc")]
    except Exception as e:  # noqa: BLE001
        log.warning("permission inbox: trending: %s", str(e)[:150])
    candidates += [{**v, "channel_id": v["channel_url"].rsplit("/", 1)[-1]} for v in overview().get("outliers", [])]
    new = []
    for v in candidates:
        cid = v.get("channel_id") or ""
        if len(added) + len(new) >= PER_DAY:
            break
        if not cid or cid in d or youtube.RISKY.search(v.get("title", "")) or LABELS.search(v.get("channel", "")):
            continue
        d[cid] = {"channel_id": cid, "name": v.get("channel", ""), "url": f"https://www.youtube.com/channel/{cid}",
                  "video_id": v["video_id"], "video_title": v.get("title", ""), "views": v.get("views", 0),
                  "status": "new", "added": today}
        d[cid]["subject"], d[cid]["message"] = compose(v.get("channel", ""), v.get("title", ""), cid)
        new.append(d[cid])
    _save(d)
    if new:
        find_emails()
    if new:
        try:
            from .telegram import esc, notify

            for c in new:
                notify(f"🤝 <b>Ask for permission:</b> {esc(c['name'])}\nTheir video: {esc(c['video_title'][:80])}\n"
                       f"Channel: {c['url']}\n\nMessage to send (copy):\n<code>{esc(c['message'])}</code>",
                       [[{"text": "📧 Add email & send", "callback_data": f"pe:{c['channel_id']}"},
                         {"text": "✅ They said yes", "callback_data": f"py:{c['channel_id']}"}],
                        [{"text": "Asked another way", "callback_data": f"pa:{c['channel_id']}"},
                         {"text": "Skip", "callback_data": f"ps:{c['channel_id']}"}]])
        except Exception:  # noqa: BLE001
            pass
    return new


EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _deobfuscate(t: str) -> str:
    t = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", t, flags=re.I)
    return re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", t, flags=re.I)


def lookup_emails(channel_ids: list[str]) -> dict:
    """{channel_id: public email} from channel descriptions (1 quota unit per 50 channels)."""
    from . import quota, youtube

    out = {}
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i:i + 50]
        try:
            r = youtube.client().channels().list(part="snippet,brandingSettings", id=",".join(batch), maxResults=50).execute()
            quota.spend("read")
        except Exception as e:  # noqa: BLE001
            log.warning("email lookup: %s", str(e)[:150])
            return out
        for it in r.get("items", []):
            text = it.get("snippet", {}).get("description", "") + "\n" + it.get("brandingSettings", {}).get("channel", {}).get("description", "")
            m = [e.strip(".") for e in EMAIL_FIND.findall(_deobfuscate(text)) if not e.lower().endswith((".png", ".jpg", ".gif"))]
            if m:
                out[it["id"]] = m[0]
    return out


def add_creator(cid: str, name: str, video_id: str, video_title: str, views: int, email: str = "") -> dict:
    """Put a creator in the inbox (from the viral picks) with a ready mail about this exact video."""
    d = _load()
    c = d.get(cid)
    if not c:
        c = d[cid] = {"channel_id": cid, "name": name, "url": f"https://www.youtube.com/channel/{cid}", "video_id": video_id,
                      "video_title": video_title, "views": views, "status": "new", "added": utcnow().strftime("%Y-%m-%d"),
                      "email_checked": True}
        c["subject"], c["message"] = compose(name, video_title, cid)
    if email and not c.get("email"):
        c["email"], c["email_from"] = email, "their channel description"
    _save(d)
    return c


def find_emails(only: str | None = None) -> int:
    """Look for a public business email in each new creator's channel description (official API, 1 unit each)."""
    from . import quota, youtube

    d = _load()
    todo = [c for c in d.values() if c["status"] in ("new", "asked") and not c.get("email") and not c.get("mailed")
            and (only == c["channel_id"] or (only is None and not c.get("email_checked")))]
    found = 0
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        try:
            r = youtube.client().channels().list(part="snippet,brandingSettings", id=",".join(c["channel_id"] for c in batch),
                                                 maxResults=50).execute()
            quota.spend("read")
        except Exception as e:  # noqa: BLE001
            log.warning("permission inbox: email lookup: %s", str(e)[:150])
            return found
        texts = {it["id"]: (it.get("snippet", {}).get("description", "") + "\n"
                            + it.get("brandingSettings", {}).get("channel", {}).get("description", "")) for it in r.get("items", [])}
        for c in batch:
            m = [e.strip(".") for e in EMAIL_FIND.findall(_deobfuscate(texts.get(c["channel_id"], "")))
                 if not e.lower().endswith((".png", ".jpg", ".gif"))]
            c["email_checked"] = True
            if m:
                c["email"], c["email_from"] = m[0], "their channel description"
                found += 1
    _save(d)
    return found


def listing() -> list[dict]:
    return sorted(_load().values(), key=lambda c: ({"replied": 0, "new": 1, "asked": 2, "granted": 3, "skipped": 4, "declined": 5}
                                                   .get(c["status"], 6), c["name"]))


def set_status(cid: str, status: str) -> dict:
    d = _load()
    if cid not in d:
        raise KeyError(cid)
    d[cid]["status"] = status
    d[cid][status + "_at"] = utcnow().isoformat() + "Z"
    _save(d)
    return d[cid]


def set_email(cid: str, email: str) -> dict:
    from .gmail import EMAIL_RE

    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise ValueError("That email address does not look right.")
    d = _load()
    if cid not in d:
        raise KeyError(cid)
    d[cid]["email"] = email
    _save(d)
    return d[cid]


def rewrite_unsent() -> int:
    """Give creators who have not been mailed yet the new, natural message (keeps messages the owner edited)."""
    d = _load()
    n = 0
    for c in d.values():
        if not c.get("mailed") and not c.get("edited") and c["status"] in ("new", "asked"):
            c["subject"], c["message"] = compose(c.get("name", ""), c.get("video_title", ""), c["channel_id"])
            n += 1
    _save(d)
    return n


def set_message(cid: str, subject: str, message: str) -> dict:
    d = _load()
    if cid not in d:
        raise KeyError(cid)
    if d[cid].get("mailed"):
        raise ValueError("Already sent.")
    if len((message or "").strip()) < 30:
        raise ValueError("The message is too short.")
    d[cid].update(subject=(subject or "").strip()[:150] or d[cid].get("subject"), message=message.strip()[:4000], edited=True)
    _save(d)
    return d[cid]


def send_mail(cid: str) -> dict:
    """Send the ready message from the owner's Gmail (only when the owner presses Send)."""
    from . import acl, gmail

    if not acl.allowed("gmail.send"):  # every way of sending (dashboard, Viral page, Telegram) comes through here
        raise ValueError(acl.message("gmail.send"))
    d = _load()
    if cid not in d:
        raise KeyError(cid)
    c = d[cid]
    if not c.get("email"):
        raise ValueError("Add the creator's email first (channel About page → business email, or their Instagram bio).")
    if c.get("thread_id"):
        raise ValueError("Already sent to this creator.")
    res = gmail.send(c["email"], c["message"], c.get("subject"))
    c.update(status="asked", asked_at=utcnow().isoformat() + "Z", thread_id=res["thread_id"], mailed=True)
    _save(d)
    return c


YES_RE = re.compile(r"\b(yes|yeah|yep|yup|sure|of course|go ahead|permission granted|granted|you can|you may|allowed|ok|okay|"
                    r"haan|han ji|haa|ji haan|bilkul|theek hai|thik hai|kar sakte|kar sakta|kar lo|kr lo|use kar)\b", re.I)
# anything that is not a plain yes goes to the owner: refusals, conditions, money, legal words, questions
NOT_PLAIN_YES = re.compile(r"\b(no|not|don'?t|dont|do not|never|nahi|nahin|nhi|mat|nope|sorry|can'?t|cannot|won'?t|wont|only|but|"
                           r"however|condition|if|pay|paid|price|charge|fee|money|paise|rupees|rs|inr|license|licence|contract|"
                           r"agreement|remove|delete|strike|copyright|claim|ask|later|wait|think|maybe|shayad|sochke)\b|\?", re.I)


def _own_words(snippet: str) -> str:
    """The creator's new text only (drop the quoted original mail)."""
    t = re.split(r"\bOn .{0,80}wrote:|\bNamaste\b|\bHi .{0,40}I'm a big fan|^>", snippet or "", maxsplit=1, flags=re.I | re.M)[0]
    return re.sub(r"\s+", " ", t).strip()


def plain_yes(c: dict, r: dict) -> bool:
    """A short, unconditional yes from the address we wrote to. Everything else waits for the owner."""
    text = _own_words(r.get("snippet", ""))
    sender_ok = bool(c.get("email")) and c["email"].lower() in (r.get("from") or "").lower()
    return sender_ok and 0 < len(text) <= 300 and bool(YES_RE.search(text)) and not NOT_PLAIN_YES.search(text)


def revoke(cid: str) -> dict:
    """Undo an automatic yes: stop the source; the reply goes back to the owner to decide."""
    d = _load()
    c = d.get(cid)
    if not c:
        raise KeyError(cid)
    if c.get("source_id"):
        with SessionLocal() as s:
            ch = s.get(Channel, c["source_id"])
            if ch:
                ch.auto, ch.mode = False, "cc"  # paused; nothing from this creator is uploaded any more
                s.commit()
    c.update(status="replied" if c.get("reply") else "asked", auto_granted=False, proof=None)
    _save(d)
    return c


def check_replies() -> int:
    """Look for creators' replies in Gmail. A plain yes starts their source by itself (with Undo); the rest go to the owner."""
    from . import gmail

    if not gmail.connected_email():
        return 0
    d = _load()
    found, auto, ask = 0, [], []
    for c in d.values():
        if c.get("status") != "asked" or not c.get("thread_id") or c.get("reply_id"):
            continue
        try:
            r = gmail.reply_in_thread(c["thread_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("gmail reply check: %s", str(e)[:150])
            continue
        if not r:
            continue
        c.update(reply_id=r["id"], reply_from=r["from"], reply_date=r["date"], reply=r["snippet"], status="replied")
        found += 1
        (auto if plain_yes(c, r) else ask).append(c["channel_id"])
    if not found:
        return 0
    _save(d)
    from .telegram import esc, notify

    for cid in auto:  # clear, unconditional yes from the address we wrote to
        try:
            g = grant_from_reply(cid)
            d = _load()
            d[cid]["auto_granted"] = True
            _save(d)
            notify(f"✅ <b>{esc(g['name'])}</b> said yes:\n<i>{esc(_own_words(g['reply'])[:300])}</i>\n\n"
                   "Their new videos are now clipped and uploaded automatically, with credit. Wrong? Press Undo.",
                   [[{"text": "↩️ Undo", "callback_data": f"pu:{cid}"}]])
        except Exception as e:  # noqa: BLE001
            log.warning("auto-grant failed for %s: %s", cid, str(e)[:150])
            ask.append(cid)
    d = _load()
    for cid in ask:
        c = d[cid]
        try:
            notify(f"📩 <b>{esc(c['name'])}</b> replied:\n<i>{esc(c['reply'][:400])}</i>",
                   [[{"text": "✅ Permission given", "callback_data": f"pg:{cid}"},
                     {"text": "❌ They said no", "callback_data": f"pn:{cid}"}]])
        except Exception:  # noqa: BLE001
            pass
    return found


def grant_from_reply(cid: str) -> dict:
    d = _load()
    c = d.get(cid)
    if not c or not c.get("reply"):
        raise KeyError(cid)
    proof = f"Email reply from {c.get('reply_from', '')} on {c.get('reply_date', '')}: “{c['reply'][:300]}”"
    return grant(cid, proof)


def grant(cid: str, proof: str) -> dict:
    """The creator said yes: save the proof and start a permission source for their channel (automatic uploads)."""
    proof = (proof or "").strip()
    if len(proof) < 20:
        raise ValueError("Write how they said yes — who, how and when (e.g. 'Reply on Instagram from @name on 22 Nov').")
    d = _load()
    if cid not in d:
        raise KeyError(cid)
    c = d[cid]
    from . import acl

    if not acl.allowed("source.channel"):  # a yes becomes a channel source: that kind must be allowed for the account
        raise ValueError(acl.message("source.channel"))
    with SessionLocal() as s:
        dest = main_destination(s)
        if not dest:
            raise ValueError("Connect your YouTube channel first.")
        existing = s.query(Channel).filter(Channel.resolved_url == c["url"]).first()
        cap = acl.limit("sources")
        if not existing and cap is not None and acl.source_count() >= cap:
            raise ValueError(f"Your account can have {cap} source(s) and they are all used. Delete one first, "
                             "or ask the person who gave you access for more.")
        if existing:
            existing.mode, existing.permission_note, existing.auto = "permission", proof, True
            src_id = existing.id
        else:
            ch = Channel(kind="channel", query=c["url"], resolved_url=c["url"], name=c["name"], mode="permission",
                         permission_note=proof, auto=True, videos_per_run=2, clips_per_video=1, clip_len=35, aspect="9:16",
                         gap_hours=12, destination_id=dest.id, review_first=False, captions=True, post_times="10:00, 18:00",
                         tz_offset=-330)
            s.add(ch)
            s.flush()
            src_id = ch.id
        s.commit()
    c.update(status="granted", proof=proof, granted_at=utcnow().isoformat() + "Z", source_id=src_id)
    _save(d)
    try:  # the viral video we asked about is clipped right away
        from .viral import on_granted

        on_granted(cid)
    except Exception as e:  # noqa: BLE001
        log.warning("viral on_granted: %s", str(e)[:150])
    return c

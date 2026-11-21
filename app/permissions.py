"""Permission inbox: creators of popular non-Creative-Commons videos, with a ready message asking to use their clips.

The owner sends the message (automatic messages to strangers are spam, and YouTube has no API for it).
When a creator says yes, one click (or a Telegram reply) turns them into a permission source that clips and
uploads their videos automatically, with credit.
"""
import json
import logging
import re

from .db import Channel, Destination, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
KEY = "creators"
PER_DAY = 5
# music labels, film studios and TV networks: they never give fan pages permission and their Content ID strikes
LABELS = re.compile(r"music|records|songs|films|movies|studios|pictures|t-series|zee|sony|star |colors|tips official|"
                    r"saregama|aditya|lahari|speed records|bhajan|bhakti|aarti|wave|yrf|dharma|netflix|prime video|news", re.I)

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
                  "status": "new", "added": today, "message": MESSAGE.format(name=v.get("channel", "") or "creator")}
        new.append(d[cid])
    _save(d)
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


def send_mail(cid: str) -> dict:
    """Send the ready message from the owner's Gmail (only when the owner presses Send)."""
    from . import gmail

    d = _load()
    if cid not in d:
        raise KeyError(cid)
    c = d[cid]
    if not c.get("email"):
        raise ValueError("Add the creator's email first (channel About page → business email, or their Instagram bio).")
    if c.get("thread_id"):
        raise ValueError("Already sent to this creator.")
    res = gmail.send(c["email"], c["message"])
    c.update(status="asked", asked_at=utcnow().isoformat() + "Z", thread_id=res["thread_id"], mailed=True)
    _save(d)
    return c


def check_replies() -> int:
    """Look for creators' replies in Gmail and send them to the owner to decide (yes / no) with one tap."""
    from . import gmail

    if not gmail.connected_email():
        return 0
    d = _load()
    found = 0
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
        try:
            from .telegram import esc, notify

            notify(f"📩 <b>{esc(c['name'])}</b> replied:\n<i>{esc(r['snippet'][:400])}</i>",
                   [[{"text": "✅ Permission given", "callback_data": f"pg:{c['channel_id']}"},
                     {"text": "❌ They said no", "callback_data": f"pn:{c['channel_id']}"}]])
        except Exception:  # noqa: BLE001
            pass
    if found:
        _save(d)
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
    with SessionLocal() as s:
        dest = s.query(Destination).order_by(Destination.id).first()
        if not dest:
            raise ValueError("Connect your YouTube channel first.")
        existing = s.query(Channel).filter(Channel.resolved_url == c["url"]).first()
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
    return c

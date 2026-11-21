"""Permission inbox: creators of popular non-Creative-Commons videos, with a ready message asking to use their clips.

The owner sends the message (automatic messages to strangers are spam, and YouTube has no API for it).
When a creator says yes, one click (or a Telegram reply) turns them into a permission source that clips and
uploads their videos automatically, with credit.
"""
import json
import logging

from .db import Channel, Destination, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
KEY = "creators"
PER_DAY = 5

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
        if not cid or cid in d or youtube.RISKY.search(v.get("title", "")):
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
                       [[{"text": "✅ They said yes", "callback_data": f"py:{c['channel_id']}"},
                         {"text": "Asked", "callback_data": f"pa:{c['channel_id']}"},
                         {"text": "Skip", "callback_data": f"ps:{c['channel_id']}"}]])
        except Exception:  # noqa: BLE001
            pass
    return new


def listing() -> list[dict]:
    return sorted(_load().values(), key=lambda c: ({"new": 0, "asked": 1, "granted": 2, "skipped": 3}.get(c["status"], 4), c["name"]))


def set_status(cid: str, status: str) -> dict:
    d = _load()
    if cid not in d:
        raise KeyError(cid)
    d[cid]["status"] = status
    d[cid][status + "_at"] = utcnow().isoformat() + "Z"
    _save(d)
    return d[cid]


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

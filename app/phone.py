"""Phone notifications without passwords: the free "ntfy" app.

The owner installs ntfy on the phone and scans a QR code from the dashboard. The QR holds a long random topic
name that only this account knows; every message the system sends to the owner (Telegram/Gmail alerts) is also
pushed there. On top of the existing alerts, `events()` (every 10 minutes, per account) sends:
  * a video went live on YouTube (also always by mail),
  * every 10 new clips scheduled,
  * view milestones of each video (100, 500, 1,000, ...),
  * new subscribers,
  * something on the Dashboard needs the owner (also by mail).
The first run only records where things stand, so nothing old is announced.
"""
import html
import json
import logging
import re
import secrets

import httpx

from .db import Clip, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
SERVER = "https://ntfy.sh"
VIEW_MARKS = [100, 500, 1000, 5000, 10000, 50000, 100000, 500000, 1000000, 5000000, 10000000]


def topic() -> str:
    return get_setting("ntfy_topic") or ""


def enabled() -> bool:
    from . import acl

    return bool(topic()) and get_setting("ntfy_on") != "0" and acl.feature("phone.use")  # the Super Admin's switch


def setup(new: bool = False) -> dict:
    """Create (or replace) the secret topic. Replacing it stops the old phone subscription."""
    if new or not topic():
        set_setting("ntfy_topic", "raji-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:22].lower())
        set_setting("ntfy_on", "1")
    return status()


def status() -> dict:
    t = topic()
    out = {"set": bool(t), "on": enabled(), "topic": t, "link": f"{SERVER}/{t}" if t else ""}
    if t:
        try:
            import segno

            out["qr"] = segno.make(f"{SERVER}/{t}", error="m").svg_data_uri(scale=6, border=2)
        except Exception:  # noqa: BLE001  (no QR library: the link and topic name still work)
            out["qr"] = ""
    return out


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def push(text: str, title: str = "", url: str = "", tags: str = "", priority: int = 3) -> bool:
    """Send one push to the owner's phone. Never raises."""
    if not enabled():
        return False
    body = _plain(text)
    first, _, rest = body.partition("\n")
    payload = {"topic": topic(), "title": (title or first)[:120], "message": (rest.strip() if not title else body)[:3500] or first,
               "priority": priority}
    if url:
        payload["click"] = url
    if tags:
        payload["tags"] = [t for t in tags.split(",") if t]
    try:
        r = httpx.post(SERVER, content=json.dumps(payload).encode(), timeout=15)
        return r.status_code < 300
    except Exception as e:  # noqa: BLE001
        log.warning("phone push: %s", str(e)[:150])
        return False


def _mail(text: str):
    try:
        from .gmail import notify_owner

        notify_owner(text)
    except Exception as e:  # noqa: BLE001
        log.warning("mail: %s", str(e)[:150])


def _say(text: str, title: str = "", url: str = "", tags: str = "", mail: bool = False):
    """To the phone (with title and link), to Telegram if set up, and by mail when `mail` or when nothing else is set up."""
    from . import telegram

    msg = html.escape(_plain(text))
    if enabled():
        push(text, title, url, tags)
        if telegram.enabled():
            telegram.notify(msg, push_phone=False)
        if mail:
            _mail(text)
    else:
        telegram.notify(msg, push_phone=False)  # Telegram, or the owner's Gmail when Telegram is not set up
        if mail and telegram.enabled():
            _mail(text)


def _get_json(key: str, default):
    try:
        v = json.loads(get_setting(key) or "null")
        return default if v is None else v
    except ValueError:
        return default


def events():
    """Run every 10 minutes for each account. Announces only what is new since the last run."""
    from .db import ChannelStat, Destination, VideoStat

    now = utcnow()
    first_run = get_setting("phone_baseline") is None
    # 1. videos that went live (scheduled ones go live at their publish time)
    say = []
    with SessionLocal() as s:
        live = s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.live_noted.isnot(True),
                                    Clip.removed_at.is_(None)).all()
        for c in live:
            if c.publish_at and c.publish_at > now:
                continue
            if not first_run and (c.yt_state or "ok") in ("ok", "", None):
                say.append((f"Live on YouTube now: {c.title or 'your video'}\nhttps://youtu.be/{c.youtube_id}", "Your video is live",
                            f"https://youtu.be/{c.youtube_id}", "tada", True))
            c.live_noted = True
        s.commit()
        top = (s.query(Clip.id).order_by(Clip.id.desc()).first() or (0,))[0]
    # 2. every 10 new clips scheduled
    base = int(get_setting("phone_clip_base") or 0)
    if first_run or not base:
        base = top
        set_setting("phone_clip_base", str(base))
    with SessionLocal() as s:
        made = s.query(Clip).filter(Clip.id > base, Clip.status.in_(["queued", "uploading", "uploaded"])).count()
        nxt = s.query(Clip.publish_at).filter(Clip.status == "queued", Clip.publish_at.isnot(None)).order_by(Clip.publish_at.desc()).first()
        videos = [(v.video_id, v.title, v.views or 0) for v in s.query(VideoStat).all()]
        stats = []
        for d in s.query(Destination).all():
            st = s.query(ChannelStat).filter(ChannelStat.destination_id == d.id).order_by(ChannelStat.at.desc()).first()
            if st and st.subscribers is not None and not st.subscribers_hidden:
                stats.append((d.id, d.name, st.subscribers))
    told = int(get_setting("phone_clip_told") or 0)
    if made // 10 > told // 10:
        last = f" Scheduled up to {nxt[0]:%d %b}." if nxt and nxt[0] else ""
        say.append((f"{made // 10 * 10} new clips scheduled so far.{last} They go up by themselves, about 5 a day.",
                    f"{made // 10 * 10} clips scheduled", "", "calendar", False))
    set_setting("phone_clip_told", str(made))
    # 3. view milestones per video
    noted = _get_json("phone_views", {})
    for vid, title, views in videos:
        mark = max([m for m in VIEW_MARKS if views >= m], default=0)
        if vid in noted and mark > int(noted[vid]) and not first_run:
            say.append((f"{title or 'Your video'} passed {mark:,} views.\nhttps://youtu.be/{vid}", f"{mark:,} views",
                        f"https://youtu.be/{vid}", "eyes", False))
        noted[vid] = max(mark, int(noted.get(vid, 0)))
    set_setting("phone_views", json.dumps(noted))
    # 4. subscribers
    subs = _get_json("phone_subs", {})
    for did, name, n in stats:
        before = subs.get(str(did))
        if before is not None and n > before and not first_run:
            gain = n - before
            say.append((f"+{gain} subscriber{'s' if gain > 1 else ''} on {name} (now {n:,}).", "New subscribers", "", "busts_in_silhouette", False))
        subs[str(did)] = n
    set_setting("phone_subs", json.dumps(subs))
    for text, title, url, tags, mail in say:
        _say(text, title=title, url=url, tags=tags, mail=mail)
    # 5. something needs the owner (Dashboard problems at "bad" level), once per problem
    try:
        from .main import health

        told_bad = set(_get_json("phone_bad", []))  # announced and still there
        seen_before = set(_get_json("phone_bad_prev", []))  # there at the previous check (10 minutes ago)
        bad = {(i.get("key") or i["text"][:60]): i["text"] for i in health()["items"] if i.get("level") == "bad"}
        for k, text in bad.items():
            # only a problem that is still there 10 minutes later: a short blip never wakes the owner
            if k not in told_bad and k in seen_before and not first_run:
                _say(f"Needs you: {text}\nOpen the dashboard: Setup guide → Problems right now.", title="Raji Studio needs you", tags="warning", mail=True)
                told_bad.add(k)
        set_setting("phone_bad", json.dumps(sorted(told_bad & set(bad))))
        set_setting("phone_bad_prev", json.dumps(sorted(bad)))
    except Exception as e:  # noqa: BLE001
        log.warning("phone needs-you: %s", str(e)[:150])
    if first_run:
        set_setting("phone_baseline", now.isoformat())

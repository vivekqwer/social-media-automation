"""Weekly report: every Monday at 9 AM India time each account gets one short report of its week —
views this week vs last week, new subscribers, the top 3 videos (title, views, link), videos published this week,
what is scheduled for the next 7 days and anything that needs the owner.

* Sent to the account's own Gmail (gmail.notify_owner, kind "weekly": its own small daily cap) and to its phone app
  (app/phone.py) when that is set up. Nothing new is fetched: it is read from what the app already stores
  (ChannelStat / VideoStat from app/stats.py, the clips).
* The account's own switch (My channels → Weekly report, default on: setting `weekly_report_on`) and the Super Admin's
  switch `report.weekly` (the scheduler's when_on) both have to be on.
* Once per week per account, also after restarts: the week is claimed in the setting `weekly_report_week` before
  anything is sent (one atomic change). A week with no data (no numbers, nothing published, nothing scheduled) sends
  nothing — so a new account's first Monday is quiet.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from .db import ChannelStat, Clip, Destination, SessionLocal, Setting, VideoStat, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
SEND_HOUR = 9  # Monday, India time
ON_KEY = "weekly_report_on"  # "0" = the account switched it off
WEEK_KEY = "weekly_report_week"  # the ISO week already handled, e.g. "2026-W48"
LAST_KEY = "weekly_report_last"  # {"week", "at", "mail", "phone", "skipped"} for the card
VIEWS_KEY = "weekly_report_views"  # {video id: views} at the last report: next week's top videos are by views gained
TOP = 3


def is_on() -> bool:
    return get_setting(ON_KEY) != "0"


def set_on(on: bool):
    set_setting(ON_KEY, "1" if on else "0")


def week_due(now: datetime | None = None) -> str | None:
    """The ISO week whose report is due now (Monday from 9 AM India time until the end of that Monday), else None."""
    t = (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(IST)
    if t.weekday() != 0 or t.hour < SEND_HOUR:
        return None
    y, w, _d = t.isocalendar()
    return f"{y}-W{w:02d}"


def _claim(week: str) -> bool:
    """Take this week for this account, once: True only for the one call that changed the setting."""
    with SessionLocal() as s:
        res = s.execute(update(Setting).where(Setting.key == WEEK_KEY, Setting.value != week).values(value=week))
        s.commit()
        if res.rowcount == 1:
            return True
        if s.get(Setting, WEEK_KEY) is not None:
            return False  # this week was already taken
        try:
            s.add(Setting(key=WEEK_KEY, value=week))
            s.commit()
            return True
        except IntegrityError:  # taken at the same moment by another run
            s.rollback()
            return False


def _snap(s, dest_id: int, t: datetime):
    return (s.query(ChannelStat).filter(ChannelStat.destination_id == dest_id, ChannelStat.at <= t)
            .order_by(ChannelStat.at.desc()).first())


def _prev_views() -> dict:
    try:
        v = json.loads(get_setting(VIEWS_KEY) or "null")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def build(now: datetime | None = None) -> dict | None:
    """The numbers of the week that ended at `now`. None when there is nothing to tell (no data at all)."""
    now = now or utcnow()
    w1, w2 = now - timedelta(days=7), now - timedelta(days=14)
    views = views_last = subs = None
    with SessionLocal() as s:
        for d in s.query(Destination).order_by(Destination.id):
            a, b, c = _snap(s, d.id, now), _snap(s, d.id, w1), _snap(s, d.id, w2)
            if a and b and a.views is not None and b.views is not None and a.at > b.at:
                views = (views or 0) + max(0, a.views - b.views)
                if c and c.views is not None and b.at > c.at:
                    views_last = (views_last or 0) + max(0, b.views - c.views)
            if a and b and a.subscribers is not None and b.subscribers is not None and not a.subscribers_hidden and a.at > b.at:
                subs = (subs or 0) + (a.subscribers - b.subscribers)
        vids = [(v.video_id, v.title or "", v.views or 0) for v in
                s.query(VideoStat).filter((VideoStat.privacy == "public") | (VideoStat.privacy.is_(None)))]
        when = Clip.publish_at
        pub = (s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.removed_at.is_(None),
                                    ((when.isnot(None)) & (when > w1) & (when <= now))
                                    | ((when.is_(None)) & (Clip.attempted_at > w1) & (Clip.attempted_at <= now)))
               .order_by(Clip.publish_at).all())
        published = [(c.title or "Untitled", c.youtube_id) for c in pub]
        nxt = (s.query(Clip).filter(Clip.status.in_(["queued", "uploading", "uploaded"]), Clip.removed_at.is_(None),
                                    when > now, when <= now + timedelta(days=7)).order_by(when).all())
        coming = [(c.publish_at, c.title or "Untitled") for c in nxt]
    prev = _prev_views()
    gained = bool(prev)
    if gained:  # views gained since the last report (a video new since then: all its views)
        ranked = sorted(((vid, t, n - int(prev.get(vid, 0))) for vid, t, n in vids), key=lambda x: -x[2])
    else:  # the first report: the most watched videos so far
        ranked = sorted(vids, key=lambda x: -x[2])
    top = [{"title": t, "views": n, "link": f"https://youtu.be/{vid}"} for vid, t, n in ranked[:TOP] if n > 0]
    if views is None and subs is None and not published and not coming:
        return None  # no data for this week: nothing is sent
    needs = []
    try:
        from .main import health

        needs = [i["text"] for i in health()["items"] if i.get("level") == "bad"][:5]
    except Exception as e:  # noqa: BLE001
        log.warning("weekly report needs-you: %s", str(e)[:150])
    return {"from": w1, "to": now, "views": views, "views_last": views_last, "subscribers": subs, "top": top,
            "top_gained": gained, "published": published, "coming": coming, "needs": needs,
            "snapshot": {vid: n for vid, _t, n in vids if n > 0}}


def _n(x) -> str:
    return f"{x:,}"


def text(r: dict) -> str:
    """The report as short plain text (the same words for the mail and the phone)."""
    f, t = r["from"].replace(tzinfo=timezone.utc).astimezone(IST), r["to"].replace(tzinfo=timezone.utc).astimezone(IST)
    out = [f"Your week on YouTube ({f:%d %b} – {t:%d %b})"]
    if r["views"] is not None:
        line = f"Views this week: {_n(r['views'])}"
        if r["views_last"] is not None:
            line += f" (last week {_n(r['views_last'])}"
            if r["views_last"]:
                pct = round((r["views"] - r["views_last"]) * 100 / r["views_last"])
                line += f", {pct:+d}%"
            line += ")"
        out.append(line)
    if r["subscribers"] is not None:
        out.append(f"New subscribers: {r['subscribers']:+,}")
    if r["top"]:
        out.append("Top videos" + (" this week:" if r["top_gained"] else ":"))
        for i, v in enumerate(r["top"], 1):
            out.append(f"{i}. {v['title'][:80]} — {_n(v['views'])} views{' this week' if r['top_gained'] else ''} — {v['link']}")
    out.append(f"Published this week: {len(r['published'])}")
    if r["coming"]:
        first = r["coming"][0][0].replace(tzinfo=timezone.utc).astimezone(IST)
        out.append(f"Next 7 days: {len(r['coming'])} scheduled (first: {first:%a %d %b, %I:%M %p})")
    else:
        out.append("Next 7 days: nothing scheduled — add sources or approve clips so your channel keeps going.")
    if r["needs"]:
        out.append("Needs you:")
        out += [f"- {x[:160]}" for x in r["needs"]]
    else:
        out.append("Nothing needs you.")
    return "\n".join(out)


def _send(body: str) -> dict:
    sent = {"mail": False, "phone": False}
    try:
        from .gmail import notify_owner

        sent["mail"] = bool(notify_owner(body, kind="weekly"))
    except Exception as e:  # noqa: BLE001
        log.warning("weekly report mail: %s", str(e)[:150])
    try:
        from . import phone

        if phone.enabled():
            first, _, rest = body.partition("\n")
            sent["phone"] = bool(phone.push(rest, title=first, tags="bar_chart"))
    except Exception as e:  # noqa: BLE001
        log.warning("weekly report phone: %s", str(e)[:150])
    return sent


def weekly_due(now: datetime | None = None) -> dict | None:
    """Timed, for each account (scheduler: when_on("report.weekly")), every 15 minutes: does something only on
    Monday from 9 AM India time, once per week."""
    now = now or utcnow()
    week = week_due(now)
    if not week or not is_on() or get_setting(WEEK_KEY) == week:
        return None
    if not _claim(week):
        return None  # claimed before a restart, or by a run at the same moment: never twice
    r = build(now)
    last = {"week": week, "at": now.isoformat() + "Z", "mail": False, "phone": False, "skipped": r is None}
    if r is not None:
        last.update(_send(text(r)))
        set_setting(VIEWS_KEY, json.dumps(r["snapshot"]))
    set_setting(LAST_KEY, json.dumps(last))
    return last


def status() -> dict:
    """For the My channels card: on/off, the last report, and what this week's would say now."""
    try:
        last = json.loads(get_setting(LAST_KEY) or "null")
    except ValueError:
        last = None
    try:
        r = build()
        preview = text(r) if r else ""
    except Exception as e:  # noqa: BLE001
        log.warning("weekly report preview: %s", str(e)[:150])
        preview = ""
    from . import phone
    from .gmail import connected_email

    return {"on": is_on(), "last": last, "preview": preview, "mail": bool(connected_email()), "phone": phone.enabled()}

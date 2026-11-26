"""Small automatic growth helpers (each one standalone; a failure never touches uploads):
- thumbnail A/B test: a video doing much worse than the channel's usual gets a second thumbnail for 48 h, and the
  better of the two stays (click-through rate from YouTube Analytics when there is enough, else views per day)
- weekly report: a one-page PDF summary every Sunday (Telegram + download)
- backup data for the helper PC
"""
import json
import logging
import os
import statistics
from datetime import timedelta
from pathlib import Path

from .config import DATA_DIR
from .db import Clip, Destination, Project, SessionLocal, VideoStat, utcnow

log = logging.getLogger(__name__)
from .alerts import note as _note  # a failure that is only logged also shows in Alerts -> System errors (never raises)


# ---------- thumbnail A/B test ----------
# A video doing much worse than the channel's usual (views a day under 40% of the channel's median, 2-10 days after the
# upload) gets a test, once:
#   A  48 h with the thumbnail it has (views counted at the start and at the end)
#   B  48 h with the second thumbnail (another frame, the other line in yellow)
#   then the better one stays: when YouTube Analytics gives thumbnail impressions for both windows (at least
#   MIN_IMPRESSIONS each), the higher click-through rate wins; otherwise views per day decide. B must do at least as
#   well as A; when A wins, A's thumbnail is put back. Each change is one thumbnail upload (50 units of the daily
#   allowance): at most SWAPS_PER_DAY a day per account, only with room for 2 uploads left, and on the owner's Google
#   project only with what the owner's own uploads leave (quota.shared_room). "Thumbnails" switch (scheduler).
WINDOW = timedelta(hours=48)
SWAPS_PER_DAY = int(os.getenv("THUMB_SWAPS_PER_DAY") or "2")
MIN_IMPRESSIONS = 100


def _views(s, c) -> int | None:
    v = s.get(VideoStat, c.youtube_id) if c.youtube_id else None
    return int(v.views or 0) if v is not None else None


def _swaps_left() -> int:
    from .db import get_setting

    return max(0, SWAPS_PER_DAY - int(get_setting("thumb_swaps:" + utcnow().strftime("%Y-%m-%d")) or 0))


def _count_swap():
    from .db import get_setting, set_setting

    key = "thumb_swaps:" + utcnow().strftime("%Y-%m-%d")
    set_setting(key, str(int(get_setting(key) or 0) + 1))


def _room() -> bool:
    from . import quota

    return (_swaps_left() > 0 and quota.DAILY_UNITS - quota.used() >= 2 * quota.UPLOAD_COST
            and quota.shared_room(quota.COST["thumbnail"]))  # on the owner's project: owner's uploads first


def _ctr(dest_id: int, video_id: str, start, end) -> tuple[int, float] | None:
    """(impressions, click-through %) of one video between two dates from YouTube Analytics, None when not there.
    Analytics has its own allowance (no units of the upload allowance)."""
    try:
        from . import youtube

        ya = youtube.analytics_client(dest_id)
        r = ya.reports().query(ids="channel==MINE", startDate=start.date().isoformat(), endDate=end.date().isoformat(),
                               metrics="videoThumbnailImpressions,videoThumbnailImpressionsClickRate",
                               filters=f"video=={video_id}").execute()
        rows = r.get("rows") or []
        if not rows or rows[0][0] is None:
            return None
        return int(rows[0][0] or 0), round(float(rows[0][1] or 0.0), 2)
    except Exception as e:  # noqa: BLE001  (no analytics permission, metric not available: views decide)
        log.info("thumb test: no analytics for %s (%s)", video_id, str(e)[:120])
        return None


def _push(s, c, variant: int) -> bool:
    from . import youtube
    from .project_thumbs import remake_thumbnail

    old = c.thumb_variant
    c.thumb_variant = variant
    if remake_thumbnail(s, c) and youtube.push_thumbnail(c):
        _count_swap()
        return True
    c.thumb_variant = old
    return False


def _iso(t) -> str:
    return t.isoformat() + "Z"


def _at(v):
    from datetime import datetime

    return datetime.fromisoformat(str(v).rstrip("Z"))


def thumb_tests() -> dict:
    """Move every running thumbnail test on: A measured -> B goes on; B measured -> the better one stays."""
    out = {"b_started": 0, "decided": 0}
    now = utcnow()
    with SessionLocal() as s:
        rows = [c for c in s.query(Clip).filter(Clip.thumb_test.isnot(None), Clip.status == "uploaded").all()
                if (c.thumb_test or {}).get("state") in ("a", "b")]
        for c in rows:
            t = dict(c.thumb_test)
            views = _views(s, c)
            try:
                if c.removed_at or views is None:
                    t.update(state="done", winner=None, note="The video is no longer on the channel; the test stopped.")
                elif t["state"] == "a" and now - _at(t["a_at"]) >= WINDOW:
                    if not _room():
                        continue  # tried again on the next run
                    if _push(s, c, 1):
                        t.update(state="b", b_at=_iso(now), b_views=views, a_end=views)
                        out["b_started"] += 1
                    else:
                        t.update(state="done", winner="A", note="The second thumbnail could not be made (no frame was kept); "
                                                                "the first one stays.")
                elif t["state"] == "b" and now - _at(t["b_at"]) >= WINDOW:
                    a_days = max(0.5, (_at(t["b_at"]) - _at(t["a_at"])).total_seconds() / 86400)
                    b_days = max(0.5, (now - _at(t["b_at"])).total_seconds() / 86400)
                    a_rate = max(0, int(t.get("a_end", t["b_views"])) - int(t["a_views"])) / a_days
                    b_rate = max(0, views - int(t["b_views"])) / b_days
                    a_ctr = _ctr(c.destination_id, c.youtube_id, _at(t["a_at"]), _at(t["b_at"]))
                    b_ctr = _ctr(c.destination_id, c.youtube_id, _at(t["b_at"]), now)
                    if a_ctr and b_ctr and min(a_ctr[0], b_ctr[0]) >= MIN_IMPRESSIONS:
                        by, b_wins = "ctr", b_ctr[1] >= a_ctr[1]
                        t.update(a_ctr=a_ctr[1], b_ctr=b_ctr[1], a_impr=a_ctr[0], b_impr=b_ctr[0])
                    else:
                        by, b_wins = "views", b_rate >= a_rate
                    t.update(a_rate=round(a_rate, 1), b_rate=round(b_rate, 1), by=by, end_views=views)
                    if b_wins:
                        t.update(state="done", winner="B", note="")
                    elif not _room():
                        continue  # A won but there is no room to switch back today: decided on the next run
                    elif _push(s, c, 0):
                        t.update(state="done", winner="A", note="The first thumbnail was put back.")
                    else:
                        t.update(state="done", winner="A", note="The first thumbnail could not be put back; the second one stays.")
                    out["decided"] += 1
                    try:
                        from .telegram import esc, notify

                        notify(f"\U0001f9ea Thumbnail test for <b>{esc(c.title)}</b>: {describe(t)}")
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    continue
                c.thumb_test = t
                s.commit()
            except Exception as e:  # noqa: BLE001
                s.rollback()
                log.warning("thumb test %s: %s", c.id, str(e)[:200])
                _note("Thumbnail test (second thumbnail)", e)
    return out


def describe(t: dict | None) -> str:
    """One plain line for the dashboard."""
    t = t or {}
    if t.get("state") == "a":
        return "Thumbnail test: measuring the first thumbnail (A) for 48 hours, then the second one (B) is tried."
    if t.get("state") == "b":
        return "Thumbnail test: trying the second thumbnail (B) for 48 hours; the better one stays."
    if not t.get("winner"):
        return t.get("note") or ""
    if t.get("by") == "ctr":
        why = f"{t.get('b_ctr')}% of viewers clicked with B against {t.get('a_ctr')}% with A"
    elif t.get("by") == "views":
        why = f"{t.get('b_rate')} views a day with B against {t.get('a_rate')} with A"
    else:
        why = ""
    return (f"Thumbnail test: {t['winner']} won" + (f" ({why})" if why else "") + "."
            + (" B stays." if t["winner"] == "B" else "") + (f" {t['note']}" if t.get("note") else ""))


def swap_weak_thumbnails(max_per_run: int = 2) -> list[int]:
    """Daily: running tests move on (thumb_tests), and clips uploaded 2-10 days ago with views per day under 40% of the
    channel's median start a test (once). Returns the clips whose test started."""
    tests = thumb_tests()
    done = []
    now = utcnow()
    with SessionLocal() as s:
        running = sum(1 for (t,) in s.query(Clip.thumb_test).filter(Clip.thumb_test.isnot(None)).all()
                      if (t or {}).get("state") in ("a", "b"))
        for d in s.query(Destination).all():
            rows = s.query(VideoStat).filter(VideoStat.destination_id == d.id, VideoStat.privacy == "public",
                                             VideoStat.published_at.isnot(None)).all()
            if len(rows) < 5:
                continue
            vpd = {v.video_id: v.views / max(0.5, (now - v.published_at).total_seconds() / 86400) for v in rows}
            views = {v.video_id: int(v.views or 0) for v in rows}
            median = statistics.median(vpd.values()) or 0
            if median <= 0:
                continue
            clips = s.query(Clip).filter(Clip.destination_id == d.id, Clip.status == "uploaded", Clip.youtube_id.isnot(None),
                                         Clip.thumb_custom.isnot(True), (Clip.thumb_variant.is_(None)) | (Clip.thumb_variant == 0),
                                         Clip.thumb_test.is_(None), Clip.removed_at.is_(None),
                                         Clip.attempted_at <= now - timedelta(days=2), Clip.attempted_at >= now - timedelta(days=10)).all()
            for c in clips:
                if len(done) >= max_per_run or running + len(done) >= SWAPS_PER_DAY * 2:
                    break  # never more tests at once than the daily changes can serve
                if c.youtube_id in vpd and vpd[c.youtube_id] < 0.4 * median:
                    c.thumb_test = {"state": "a", "a_at": _iso(now), "a_views": views[c.youtube_id]}  # nothing uploaded yet
                    s.commit()
                    done.append(c.id)
    if done or tests["b_started"] or tests["decided"]:
        log.info("thumb tests: started %s, B on %s, decided %s", len(done), tests["b_started"], tests["decided"])
    return done


def thumb_summary() -> dict:
    """For the dashboard: how many tests ran and which thumbnail won."""
    with SessionLocal() as s:
        rows = [(c.id, c.title, c.thumb_test) for c in s.query(Clip).filter(Clip.thumb_test.isnot(None)).order_by(Clip.id.desc())]
    tests = [(cid, title, t) for cid, title, t in rows if isinstance(t, dict)]
    won = [t.get("winner") for _c, _t, t in tests if t.get("state") == "done"]
    return {"running": sum(1 for _c, _t, t in tests if t.get("state") in ("a", "b")), "a_won": won.count("A"),
            "b_won": won.count("B"),
            "recent": [{"id": cid, "title": title, "state": t.get("state"), "winner": t.get("winner"), "line": describe(t)}
                       for cid, title, t in tests[:5]]}


def weekly_report() -> Path | None:
    """One-page PDF: channel numbers this week, top videos, uploads, problems."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        from .db import Alert
        from .stats import overview

        o = overview()
        now = utcnow()
        week = now - timedelta(days=7)
        with SessionLocal() as s:
            uploaded = s.query(Clip).filter(Clip.status == "uploaded", Clip.attempted_at >= week).count()
            problems = s.query(Alert).filter(Alert.created_at >= week, Alert.kind != "restored").count()
        st = getSampleStyleSheet()
        out = DATA_DIR / "reports" / f"weekly-{now:%Y-%m-%d}.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        story = [Paragraph(f"Raji Studio — weekly report ({(now - timedelta(days=7)):%d %b} – {now:%d %b %Y})", st["Title"]),
                 Paragraph(f"Uploads this week: <b>{uploaded}</b> · Problems on YouTube: <b>{problems}</b>", st["Normal"]), Spacer(1, 6)]
        for c in o["channels"]:
            w = c.get("watch") or {}
            h = c.get("health") or {}
            story += [Paragraph(c["name"], st["Heading2"]),
                      Table([["Subscribers", c["subscribers"], f"{c['subs_7d']:+d}" if c["subs_7d"] is not None else "—"],
                             ["Total views", c["views"], f"{c['views_7d']:+d}" if c["views_7d"] is not None else "—"],
                             ["Watch time (28 days)", f"{round((w.get('minutes_28d') or 0) / 60, 1)} h", ""],
                             ["Channel health", f"{h.get('score', '—')}/100", "; ".join(h.get("notes") or [])[:80]]],
                            colWidths=[50 * mm, 40 * mm, 80 * mm],
                            style=TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 9)]))]
        top = sorted(o["videos"], key=lambda v: -v["views"])[:8]
        if top:
            story += [Spacer(1, 8), Paragraph("Top videos", st["Heading2"]),
                      Table([["Video", "Views", "Likes"]] + [[v["title"][:70], v["views"], v["likes"]] for v in top],
                            colWidths=[120 * mm, 25 * mm, 25 * mm],
                            style=TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                                              ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ece9ff"))]))]
        SimpleDocTemplate(str(out), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm).build(story)
        try:
            from .telegram import send_document

            send_document(str(out), "Weekly report")
        except Exception:  # noqa: BLE001
            pass
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("weekly report: %s", str(e)[:200])
        _note("Making the weekly report", e)
        return None


def backup_data() -> dict:
    """Everything needed to rebuild the dashboard of the copy the running code works in (channel logins stay
    encrypted; videos are on YouTube). Same contents as Download backup now (app/backup.py)."""
    from .backup import make

    return make()

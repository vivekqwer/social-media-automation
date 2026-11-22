"""Small automatic growth helpers (each one standalone; a failure never touches uploads):
- thumbnail auto-swap: a video doing much worse than the channel's usual gets a second thumbnail once
- weekly report: a one-page PDF summary every Sunday (Telegram + download)
- backup data for the helper PC
"""
import json
import logging
import statistics
from datetime import timedelta
from pathlib import Path

from .config import DATA_DIR
from .db import Base, Clip, Destination, Project, SessionLocal, VideoStat, engine_for, utcnow

log = logging.getLogger(__name__)


def swap_weak_thumbnails(max_per_run: int = 2) -> list[int]:
    """Clips uploaded 2-10 days ago with views per day under 40% of the channel's median get a new thumbnail (once)."""
    from . import quota, youtube
    from .project_thumbs import remake_thumbnail

    done = []
    now = utcnow()
    with SessionLocal() as s:
        for d in s.query(Destination).all():
            rows = s.query(VideoStat).filter(VideoStat.destination_id == d.id, VideoStat.privacy == "public",
                                             VideoStat.published_at.isnot(None)).all()
            if len(rows) < 5:
                continue
            vpd = {v.video_id: v.views / max(0.5, (now - v.published_at).total_seconds() / 86400) for v in rows}
            median = statistics.median(vpd.values()) or 0
            if median <= 0:
                continue
            clips = s.query(Clip).filter(Clip.destination_id == d.id, Clip.status == "uploaded", Clip.youtube_id.isnot(None),
                                         Clip.thumb_custom.isnot(True), (Clip.thumb_variant.is_(None)) | (Clip.thumb_variant == 0),
                                         Clip.attempted_at <= now - timedelta(days=2), Clip.attempted_at >= now - timedelta(days=10)).all()
            for c in clips:
                if len(done) >= max_per_run or quota.DAILY_UNITS - quota.used() < 2 * quota.UPLOAD_COST:
                    break
                if c.youtube_id in vpd and vpd[c.youtube_id] < 0.4 * median:
                    try:
                        c.thumb_variant = 1
                        if remake_thumbnail(s, c) and youtube.push_thumbnail(c):
                            done.append(c.id)
                            try:
                                from .telegram import esc, notify

                                notify(f"🔁 New thumbnail tried for <b>{esc(c.title)}</b> (views were low).")
                            except Exception:  # noqa: BLE001
                                pass
                        s.commit()
                    except Exception as e:  # noqa: BLE001
                        log.warning("thumb swap %s: %s", c.id, str(e)[:200])
                        s.rollback()
    return done


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
        return None


def backup_data() -> dict:
    """Everything needed to rebuild the dashboard (channel logins stay encrypted; videos are on YouTube)."""
    data = {"made_at": utcnow().isoformat() + "Z", "tables": {}}
    with engine_for().connect() as conn:
        for t in Base.metadata.sorted_tables:
            if t.name in ("video_stats", "channel_stats"):
                continue  # can be read from YouTube again
            rows = conn.execute(t.select()).mappings().all()
            data["tables"][t.name] = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows]
    faces = DATA_DIR / "faces"
    if faces.exists():
        import base64

        data["faces"] = {f.name: base64.b64encode(f.read_bytes()).decode() for f in faces.glob("*.png") if f.stat().st_size < 3_000_000}
    return data

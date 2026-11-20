"""Watch my uploaded videos on YouTube and raise dashboard alerts when one is deleted, rejected or blocked.

The YouTube API does not expose strikes or who made a claim; it only shows the result on the video
(removed / rejected with a reason / blocked in some countries). Alerts link to YouTube Studio for the rest.
"""
import logging

from . import youtube
from .db import Alert, Clip, Destination, Project, SessionLocal, utcnow

log = logging.getLogger(__name__)

REASONS = {
    "copyright": "removed for copyright (a takedown by the rights holder — this usually comes with a copyright strike)",
    "claim": "rejected because of a Content ID claim",
    "duplicate": "rejected as a duplicate of another upload",
    "inappropriate": "removed for breaking the Community Guidelines",
    "termsOfUse": "removed for breaking YouTube's Terms of Service",
    "trademark": "removed for a trademark complaint",
    "legal": "removed for a legal reason",
    "length": "rejected because it is too long",
    "uploaderAccountClosed": "removed because the channel was closed",
    "uploaderAccountSuspended": "removed because the channel was suspended",
}


def _state(item: dict | None) -> tuple[str, str, str]:
    """-> (state key, alert kind, message). State 'ok' means nothing is wrong."""
    if item is None:
        return "removed", "removed", "This video is no longer on YouTube — it was deleted (by you in YouTube Studio, or by YouTube)."
    st = item.get("status") or {}
    up = st.get("uploadStatus")
    if up == "rejected":
        r = st.get("rejectionReason") or "unknown"
        return f"rejected:{r}", "rejected", "YouTube " + REASONS.get(r, f"rejected this video ({r})") + "."
    if up == "deleted":
        return "removed", "removed", "This video was deleted on YouTube."
    if up == "failed":
        r = st.get("failureReason") or "unknown"
        return f"failed:{r}", "failed", f"YouTube could not process this video ({r})."
    blocked = ((item.get("contentDetails") or {}).get("regionRestriction") or {}).get("blocked") or []
    if blocked:
        shown = ", ".join(blocked[:8]) + (f" and {len(blocked) - 8} more" if len(blocked) > 8 else "")
        return (f"blocked:{len(blocked)}", "blocked",
                f"Blocked in {len(blocked)} countries ({shown}). This is usually a copyright claim by the rights holder.")
    return "ok", "", ""


def check_uploads() -> dict:
    """Check every uploaded clip once. Only changes in state create alerts."""
    with SessionLocal() as s:
        rows = s.query(Clip.id, Clip.destination_id, Clip.youtube_id).filter(
            Clip.status == "uploaded", Clip.youtube_id.isnot(None)).all()
    by_dest: dict = {}
    for cid, did, yid in rows:
        by_dest.setdefault(did, []).append((cid, yid))
    checked = new = 0
    for did, items in by_dest.items():
        try:
            found = youtube.video_status(did, [y for _, y in items])
        except Exception as e:  # noqa: BLE001  (login expired, quota...) -> never mark videos as deleted on an error
            log.warning("monitor: channel %s not checked: %s", did, str(e)[:200])
            continue
        with SessionLocal() as s:
            dest = s.get(Destination, did)
            for cid, yid in items:
                c = s.get(Clip, cid)
                state, kind, msg = _state(found.get(yid))
                prev = c.yt_state or "ok"
                if state != prev:
                    p = s.get(Project, c.project_id)
                    if state == "ok":
                        kind, msg = "restored", "This video is available again (no restriction found any more)."
                    s.add(Alert(clip_id=cid, kind=kind, message=msg, details={
                        "title": c.title, "youtube_id": yid,
                        "my_channel": dest.name if dest else None,
                        "uploaded_at": (c.attempted_at.isoformat() + "Z") if c.attempted_at else None,
                        "publish_at": (c.publish_at.isoformat() + "Z") if c.publish_at else None,
                        "source_title": p.title if p else None, "source_channel": p.channel if p else None,
                        "source_url": p.url if p else None, "source_mode": p.mode if p else None,
                        "clip_range": [c.start, c.end],
                    }))
                    new += 1
                c.yt_state, c.yt_checked_at = state, utcnow()
                checked += 1
            s.commit()
    return {"checked": checked, "new_alerts": new}

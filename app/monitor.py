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


# reasons that can come with a strike: stop uploading until the owner resumes (duplicates, length, claims don't)
STRIKE_STATES = ("removed", "rejected:copyright", "rejected:inappropriate", "rejected:termsOfUse",
                 "rejected:trademark", "rejected:legal")


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


LOGIN_EXPIRED = "The channel's Google login expired. Press Connect a YouTube channel to log in again, then Resume."


def _not_checked(dest_id, exc: BaseException):
    """A channel's videos could not be checked: make that visible instead of only writing a log line.
    Login gone -> the channel shows "reconnect" (uploads wait, exactly as when an upload finds the same);
    anything else -> the System errors tab of the Alerts page."""
    try:
        from google.auth.exceptions import RefreshError

        from .crypto import TokenError

        gone = isinstance(exc, TokenError) or (isinstance(exc, RefreshError) and "invalid_grant" in str(exc))
        if gone:  # expired or revoked for good (not a passing network fault)
            with SessionLocal() as s:
                d = s.get(Destination, dest_id) if dest_id is not None else None
                if d and not d.paused:
                    d.paused, d.paused_at, d.paused_reason = True, utcnow(), LOGIN_EXPIRED
                    s.commit()
            return
        if "quotaExceeded" in str(exc):
            return  # the daily limit has its own message; the check runs again after the reset
        from .guide import record_error

        record_error("Checking my videos on YouTube", exc)
    except Exception:  # noqa: BLE001  (reporting a problem must never stop the check of the other channels)
        pass


def check_uploads() -> dict:
    """Check every uploaded clip once. Only changes in state create alerts."""
    with SessionLocal() as s:
        rows = s.query(Clip.id, Clip.destination_id, Clip.youtube_id).filter(
            Clip.status == "uploaded", Clip.youtube_id.isnot(None)).all()
    by_dest: dict = {}
    for cid, did, yid in rows:
        by_dest.setdefault(did, []).append((cid, yid))
    checked = new = 0
    changed = []  # clips whose state changed in this check (the strike responder acts only on a change)
    for did, items in by_dest.items():
        try:
            found = youtube.video_status(did, [y for _, y in items])
        except Exception as e:  # noqa: BLE001  (login expired, quota...) -> never mark videos as deleted on an error
            log.warning("monitor: channel %s not checked: %s", did, str(e)[:200])
            _not_checked(did, e)
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
                        "clip_range": [c.start, c.end], "state": state,  # the Alerts page sorts into tabs by this
                    }))
                    new += 1
                    changed.append(cid)
                    try:
                        from .telegram import esc, notify

                        notify(f"🚨 <b>{esc(c.title or yid)}</b> ({esc(dest.name if dest else '')})\n{esc(msg)}\nhttps://youtu.be/{yid}")
                    except Exception:  # noqa: BLE001
                        pass
                    if state in STRIKE_STATES and dest and not dest.paused:  # protect the channel: hold its uploads
                        dest.paused, dest.paused_at = True, utcnow()
                        dest.paused_reason = f"“{c.title or yid}”: {msg}"
                c.yt_state, c.yt_checked_at = state, utcnow()
                checked += 1
            s.commit()
    try:  # a strike-like state: protect the channel at once (only YouTube's own status acts; see strikes.py)
        from . import strikes

        strikes.watch(changed)
    except Exception as e:  # noqa: BLE001  (the check itself is done; this is written down and tried again by its own job)
        log.warning("monitor: strike responder: %s", str(e)[:200])
        try:
            from .guide import record_error

            record_error("Strike responder (after checking my videos)", e)
        except Exception:  # noqa: BLE001
            pass
    return {"checked": checked, "new_alerts": new}

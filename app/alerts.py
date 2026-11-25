"""The Alerts page: every problem in one place, sorted into tabs.

  deleted  - a video is gone from YouTube (deleted by the owner, or by YouTube without a copyright reason)
  strikes  - YouTube rejected / blocked a video (copyright, claim, guidelines...) and channels paused because of it;
             and the strike cases of strikes.py (what the system did, the request to the creator, the words for YouTube)
  broken   - things that need attention now: failed clips and uploads, the helper PC, sources, thumbnails, logins, keys
  system   - unexpected server / background-job errors (guide.record_error), newest first, with the repeat count

Three kinds of rows feed the tabs:
* Alert rows (written by monitor.py) are history: they stay, and "Dismiss" only marks them as seen.
* "Now" items are worked out fresh on every call from the current state (main.health(), failed clips, ...). They
  disappear by themselves when the problem is fixed. Which of them the owner has already seen is kept in the
  setting "alerts_seen" (a list of item ids), so the sidebar badge only counts new ones.
* System errors come from the setting "guide_errors".

Nothing here keeps per-customer state in memory: sessions and settings follow the current customer by themselves.
"""
import json
import re
import zlib
from datetime import timedelta

from .db import Alert, Clip, Project, SessionLocal, get_setting, set_setting, utcnow

TABS = ("deleted", "strikes", "broken", "system")
SERIOUS = ("removed", "rejected", "blocked", "failed")  # Alert kinds that count for the badge ("restored" is good news)
YT_ID = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
_HTTP = re.compile(r'returned "(.+?)"\. Details', re.S)
NO_REUPLOAD = ("rejected:copyright", "rejected:claim", "rejected:trademark", "rejected:legal")

LABEL = {"removed": "Deleted", "rejected": "Removed by YouTube", "blocked": "Blocked / claimed",
         "failed": "YouTube could not process it", "restored": "Available again"}
# main.health() item key -> (tab, short title)
NOW = {
    "no_channel": ("broken", "No YouTube channel is connected"),
    "reconnect": ("broken", "A channel needs reconnecting"),
    "paused": ("strikes", "Uploads are paused"),
    "paused_by_you": ("broken", "You paused uploads"),
    "keys": ("broken", "Google keys are missing"),
    "private_stuck": ("broken", "Videos stay Private"),
    "helper_offline": ("broken", "The helper PC is offline"),
    "no_helper": ("broken", "No helper PC yet"),
    "thumbs": ("broken", "Thumbnails were refused"),
    "no_source": ("broken", "No source yet"),
    "no_scripts": ("broken", "A reel source has no scripts left"),
    "gmail": ("broken", "Gmail is not connected"),
    "quota": ("broken", "Today's upload limit is used"),
    "source_failed": ("broken", "A source failed"),
    "source_stale": ("broken", "A source has not run"),
    "self_repair": ("broken", "Being fixed by itself"),
}
SKIP_NOW = {"failed", "errors"}  # shown one by one instead (failed clips; the System errors tab)
RANK = {"bad": 0, "warn": 1, "info": 2}


def _iso(dt):
    return (dt.isoformat() + "Z") if dt else None


def reason(err) -> str:
    """A stored error made readable: known causes in plain words, Google's own sentence instead of the raw reply."""
    from .ingest import friendly_error

    m = friendly_error(err or "").strip()
    g = _HTTP.search(m)
    if g:
        return "YouTube said: " + g.group(1).strip()[:300]
    return m[:400] or "No reason was saved."


def alert_tab(kind: str, details: dict | None, earlier: str | None = None) -> str:
    """Which tab an Alert row belongs to. Unknown or unclear kinds go to the more serious tab."""
    state = (details or {}).get("state") or ""
    if kind == "removed":
        return "deleted"
    if kind == "failed" or (kind == "rejected" and state == "rejected:length"):
        return "broken"  # YouTube could not process it / it is too long: nothing to do with rights
    if kind == "restored":
        return earlier or "strikes"
    return "strikes"  # rejected, blocked, and anything new


def _seen_keys() -> set:
    try:
        v = json.loads(get_setting("alerts_seen") or "[]")
        return {str(x) for x in v} if isinstance(v, list) else set()
    except (ValueError, TypeError):
        return set()


def _alert_items(s) -> list:
    rows = s.query(Alert).order_by(Alert.id.desc()).limit(200).all()
    ids = {a.clip_id for a in rows if a.clip_id}
    clips = {c.id: c for c in s.query(Clip).filter(Clip.id.in_(ids)).all()} if ids else {}
    out = []
    for i, a in enumerate(rows):
        d = a.details or {}
        kind = a.kind or ""
        earlier = None
        if kind == "restored":  # good news goes next to the problem it ends
            for b in rows[i + 1:]:
                if b.clip_id == a.clip_id and (b.kind or "") != "restored":
                    earlier = alert_tab(b.kind or "", b.details or {})
                    break
        c = clips.get(a.clip_id)
        yid = d.get("youtube_id") if YT_ID.match(str(d.get("youtube_id") or "")) else None
        acts = []
        if yid:
            acts.append({"do": "link", "href": f"https://studio.youtube.com/video/{yid}/copyright", "label": "Open in YouTube Studio"})
        done = ""
        if c is not None:
            state = c.yt_state or ""
            if c.reuploaded_as:  # the same answer as in the clip itself
                done = f"Uploaded again as clip #{c.reuploaded_as}."
                acts.append({"do": "open_clip", "id": c.reuploaded_as, "label": "Open the new copy"})
            elif c.removed_at:
                done = "Removed from the dashboard."
                acts.append({"do": "restore", "id": c.id, "label": "Show it again"})
            else:
                acts.append({"do": "open_clip", "id": c.id, "label": "Open clip"})
                if kind in ("removed", "failed") and c.status == "uploaded" and state not in NO_REUPLOAD and not state.startswith("blocked"):
                    acts.append({"do": "again", "id": c.id, "ys": kind, "label": "Upload again"})
                if kind in ("removed", "rejected") and c.status == "uploaded":
                    acts.append({"do": "remove", "id": c.id, "label": "Remove"})
        seen = bool(a.seen)
        if not seen:
            acts.append({"do": "dismiss", "label": "Dismiss"})
        out.append({
            "id": f"alert:{a.id}", "tab": alert_tab(kind, d, earlier), "level": "info" if kind == "restored" else "bad",
            "kind": kind, "label": LABEL.get(kind, kind or "Problem"), "at": _iso(a.created_at),
            "title": d.get("title") or f"Clip #{a.clip_id}", "text": (a.message or "") + (f" {done}" if done else ""),
            "seen": seen, "clip_id": a.clip_id, "done": done,
            "details": {k: d.get(k) for k in ("my_channel", "uploaded_at", "publish_at", "source_title", "source_channel",
                                              "source_url", "source_mode", "clip_range")} | {"youtube_id": yid},
            "actions": acts,
        })
    return out


def _now_items(health_items: list | None) -> list:
    """What main.health() found, as board items (its own wording and fix buttons are kept)."""
    if health_items is None:
        return [{"id": "now:check:", "tab": "broken", "level": "warn", "kind": "now", "label": "Check", "at": None,
                 "title": "The system could not check itself", "seen": False, "actions": [],
                 "text": "The list of problems could not be made just now. Press Refresh in a minute; if it stays, "
                         "look at the System errors tab."}]
    out = []
    for it in health_items:
        key = it.get("key") or ""
        if key in SKIP_NOW or key not in NOW:
            if key in SKIP_NOW:
                continue
            tab, title = "broken", "Needs a look"  # a new kind of problem nobody sorted yet: still shown
        else:
            tab, title = NOW[key]
        level = it.get("level") if it.get("level") in RANK else "warn"
        if key == "paused_by_you":
            level = "warn"  # the owner's own choice: a reminder, not an alarm
        acts = [{"do": "fix", "fix": it["fix"], "id": it.get("id")}] if it.get("fix") else []
        out.append({"id": f"now:{key}:{it.get('ref') or it.get('id') or ''}", "tab": tab, "level": level, "kind": "now",
                    "label": {"bad": "Fix now", "warn": "Check", "info": "Info"}[level], "at": None, "title": title,
                    "text": it.get("text") or "", "actions": acts})
    return out


def _clip_items(s) -> list:
    out = []
    names = {}
    # only the small columns: this runs with every refresh of the dashboard, and clips/projects carry big ones
    clips = s.query(Clip.id, Clip.title, Clip.project_id, Clip.error, Clip.output_path, Clip.attempted_at, Clip.created_at,
                    Clip.thumb_note, Clip.meta_result)

    def title(c):
        if c.title:
            return c.title
        if c.project_id not in names:
            names[c.project_id] = s.query(Project.title).filter(Project.id == c.project_id).scalar() or ""
        return names[c.project_id] or f"Clip #{c.id}"

    try:  # failed clips the self-repair tries again by itself (app/watchdog.py)
        from .watchdog import pending_retries

        again = pending_retries()
    except Exception:  # noqa: BLE001
        again = {}
    for c in clips.filter(Clip.status == "failed").order_by(Clip.id.desc()).limit(100).all():
        upload = bool(c.output_path)  # the video was made; it is the upload that stopped
        why = reason(c.error)
        if c.id in again:
            why += f" The system tries it again by itself ({_iso(again[c.id])[:16].replace('T', ' ')} UTC)."
        out.append({
            "id": f"clip:{c.id}:{zlib.crc32((c.error or '').encode('utf-8', 'replace')):08x}", "tab": "broken",
            "level": "bad" if upload else "warn", "kind": "upload_failed" if upload else "clip_failed",
            "label": "Upload failed" if upload else "Clip failed", "at": _iso((c.attempted_at if upload else None) or c.created_at),
            "title": title(c), "clip_id": c.id,
            "text": ("The video was made, but the upload to YouTube did not work: " if upload else "This clip could not be made: ") + why,
            "actions": [{"do": "open_clip", "id": c.id, "label": "Open clip"}, {"do": "retry_clip", "id": c.id, "label": "Retry"}],
        })
    # a clip that is not failed but carries a note (e.g. the helper PC did not make its voice-over in time)
    for c in clips.filter(Clip.status.in_(["draft", "rendered"]), Clip.error.isnot(None), Clip.error != "",
                          ~Clip.error.like("Parked%")) \
            .order_by(Clip.id.desc()).limit(30).all():  # clips parked on purpose (too many waiting) are not a problem
        out.append({"id": f"clip:{c.id}:{zlib.crc32(c.error.encode('utf-8', 'replace')):08x}", "tab": "broken", "level": "warn",
                    "kind": "clip_note", "label": "Check", "at": _iso(c.created_at), "title": title(c), "clip_id": c.id,
                    "text": reason(c.error), "actions": [{"do": "open_clip", "id": c.id, "label": "Open clip"}]})
    for c in clips.filter(Clip.status.in_(["rendered", "queued"]), Clip.thumb_note.like("Thumbnail could not be made%")) \
            .order_by(Clip.id.desc()).limit(20).all():
        out.append({"id": f"thumb:{c.id}", "tab": "broken", "level": "warn", "kind": "thumb_failed", "label": "Check",
                    "at": _iso(c.created_at), "title": title(c), "clip_id": c.id,
                    "text": "The video is fine, but its thumbnail could not be made. Open the clip → Thumbnail and press "
                            "“Make thumbnail”, or upload your own picture. (" + reason(c.thumb_note[28:]) + ")",
                    "actions": [{"do": "open_clip", "id": c.id, "label": "Open clip"}]})
    since = utcnow() - timedelta(days=14)
    for c in clips.filter(Clip.status == "uploaded", Clip.attempted_at >= since, Clip.meta_result.isnot(None)) \
            .order_by(Clip.id.desc()).limit(150).all():
        res = c.meta_result or {}  # PostgreSQL: an empty JSON column is JSON null
        errs = [f"{k[:-6].capitalize()}: {v}" for k, v in res.items() if k.endswith("_error") and v] if isinstance(res, dict) else []
        if errs:
            out.append({"id": f"meta:{c.id}", "tab": "broken", "level": "warn", "kind": "meta_failed", "label": "Check",
                        "at": _iso(c.attempted_at), "title": title(c), "clip_id": c.id,
                        "text": "The video is on YouTube, but posting it to Instagram / Facebook did not work. "
                                + reason("; ".join(errs)) + " Check the connection in My channels → Instagram + Facebook.",
                        "actions": [{"do": "open_clip", "id": c.id, "label": "Open clip"}, {"do": "go", "page": "settings", "label": "Open My channels"}]})
    # first comment under a video: refused for good (comments off, made for kids...), or the login is too old for it
    no_perm = 0
    for c in s.query(Clip.id, Clip.title, Clip.project_id, Clip.attempted_at, Clip.comment_state) \
            .filter(Clip.status == "uploaded", Clip.attempted_at >= since, Clip.removed_at.is_(None),
                    (Clip.comment_state.like("failed%")) | (Clip.comment_state == "no permission")) \
            .order_by(Clip.id.desc()).limit(60).all():
        if c.comment_state == "no permission":
            no_perm += 1
            continue
        out.append({"id": f"comment:{c.id}:{zlib.crc32((c.comment_state or '').encode('utf-8', 'replace')):08x}",
                    "tab": "broken", "level": "warn", "kind": "comment_failed", "label": "Check", "at": _iso(c.attempted_at),
                    "title": title(c), "clip_id": c.id,
                    "text": "The video is on YouTube, but its first comment could not be posted: " + c.comment_state[8:].strip()
                            + ". Nothing else is affected. You can write a comment yourself in the YouTube app.",
                    "actions": [{"do": "open_clip", "id": c.id, "label": "Open clip"}]})
    if no_perm:
        out.append({"id": f"comment:noperm:{no_perm}", "tab": "broken", "level": "warn", "kind": "comment_permission",
                    "label": "Check", "at": None, "title": "First comments are waiting for permission",
                    "text": f"{no_perm} new video{'' if no_perm == 1 else 's'} did not get the first comment because the channel "
                            "was connected before comments were added. Reconnect the channel once to allow comments: My channels "
                            "→ Connect a YouTube channel → choose the same channel and tick every box. The waiting comments "
                            "are then posted by themselves.",
                    "actions": [{"do": "go", "page": "settings", "label": "Open My channels"}]})
    week = utcnow() - timedelta(days=7)
    for p in s.query(Project.id, Project.title, Project.quick, Project.created_at) \
            .filter(Project.created_at >= week, Project.quick.isnot(None)).order_by(Project.id.desc()).limit(80).all():
        q = p.quick or {}  # PostgreSQL: an empty JSON column is JSON null and passes "is not null"
        if isinstance(q, dict) and q.get("state") == "failed":
            out.append({"id": f"quick:{p.id}", "tab": "broken", "level": "warn", "kind": "quick_failed", "label": "Check",
                        "at": _iso(p.created_at), "title": p.title or "Quick upload",
                        "text": "This Quick upload stopped: " + reason(q.get("step")) + " Start it again with the Quick upload button at the top.",
                        "actions": []})
    return out


def _where(w: str) -> str:
    w = str(w or "")
    if w.startswith("background job: "):
        return "A background job stopped: " + w[16:]
    if w.startswith("timed job: "):
        return "A timed job stopped: " + w[11:]
    if w[:4] in ("GET ", "PUT ") or w[:5] == "POST " or w[:7] == "DELETE ":
        return "A page action failed: " + w
    return "Something went wrong: " + w


def _error_items() -> list:
    from .guide import recent_errors

    try:  # errors the self-repair took care of (a passing cause): shown, but not as a new problem
        from .watchdog import err_key, handled_keys

        done = handled_keys()
    except Exception:  # noqa: BLE001
        done, err_key = set(), None
    out = []
    for e in recent_errors():
        try:
            n = int(e.get("count") or 1)
            if done and err_key(e) in done:
                out.append({
                    "id": f"err:{e.get('type')}:{e.get('line')}:{e.get('at')}", "tab": "system", "level": "info",
                    "kind": "error", "label": "Handled by itself", "at": e.get("at"), "title": _where(e.get("where")),
                    "count": n, "text": "A passing problem (the database was busy, or the network / Google had a hiccup). "
                                        "The system tries again by itself; you are told only if it lasts.",
                    "tech": f"{e.get('type')}: {e.get('text')}" + (f" ({e.get('line')})" if e.get("line") else ""),
                    "actions": []})
                continue
            out.append({
                "id": f"err:{e.get('type')}:{e.get('line')}:{e.get('at')}", "tab": "system", "level": "warn", "kind": "error",
                "label": "Server problem", "at": e.get("at"), "title": _where(e.get("where")), "count": n,
                "text": ("This happened " + (f"{n} times" if n > 1 else "once") + ". The system carries on by itself. If it keeps "
                         "coming back, open the Setup guide, press Copy details and send them to the person who looks after the system."),
                "tech": f"{e.get('type')}: {e.get('text')}" + (f" ({e.get('line')})" if e.get("line") else ""),
                "actions": [{"do": "go", "page": "guide", "label": "Open Setup guide"}],
            })
        except Exception:  # noqa: BLE001  (one odd row must not hide the rest)
            continue
    return out


def board(health_items: list | None) -> dict:
    """Everything the Alerts page shows, in one go. `health_items` = main.health()["items"] (None: it could not run)."""
    with SessionLocal() as s:
        alerts = _alert_items(s)
        try:
            clips = _clip_items(s)
        except Exception as e:  # noqa: BLE001  (never an empty page because one list failed)
            from .guide import record_error

            record_error("Alerts: reading clips", e)
            clips = []
        last = s.query(Clip.yt_checked_at).filter(Clip.yt_checked_at.isnot(None)).order_by(Clip.yt_checked_at.desc()).first()
        watching = s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None)).count()
    cases, strike_set = [], None
    try:  # strike cases (strikes.py): what happened, what the system did, what is left for the owner
        from . import strikes

        cases, strike_set = strikes.board_items(), strikes.settings_public()
    except Exception as e:  # noqa: BLE001  (never an empty page because one list failed)
        from .guide import record_error

        record_error("Alerts: reading strike cases", e)
    live = _now_items(health_items) + cases + clips + _error_items()
    seen = _seen_keys()
    current = {i["id"] for i in live}
    if health_items is not None and seen - current:  # a problem that went away and comes back later is new again
        seen &= current
        set_setting("alerts_seen", json.dumps(sorted(seen)))
    for i in live:
        i["seen"] = i["id"] in seen
        if not i["seen"]:
            i["actions"] = i["actions"] + [{"do": "dismiss", "label": "Dismiss"}]
    tabs = {t: [] for t in TABS}
    for i in live + alerts:
        tabs[i["tab"]].append(i)
    for t in TABS:  # what is wrong right now first (worst first), then everything with a time, newest first
        now = sorted((i for i in tabs[t] if i["at"] is None), key=lambda i: RANK.get(i["level"], 1))
        hist = sorted((i for i in tabs[t] if i["at"] is not None), key=lambda i: str(i["at"]), reverse=True)
        tabs[t] = now + hist
    new = lambda i: not i["seen"] and i["level"] != "info"  # noqa: E731
    counts = {t: sum(1 for i in tabs[t] if new(i)) for t in TABS}
    counts["all"] = sum(counts.values())
    totals = {t: len(tabs[t]) for t in TABS}
    totals["all"] = sum(totals.values())
    badge = sum(1 for t in ("deleted", "strikes", "broken") for i in tabs[t] if not i["seen"] and i["level"] == "bad")
    try:
        from .watchdog import public as self_repair

        repair = self_repair()
    except Exception:  # noqa: BLE001
        repair = None
    return {"tabs": tabs, "counts": counts, "totals": totals, "badge": badge, "watching": watching, "self_repair": repair,
            "strike_reply": strike_set, "last_check": _iso(last[0]) if last else None,
            "failed_clips": sum(1 for i in clips if i["kind"] in ("clip_failed", "upload_failed"))}


def mark_seen(b: dict, tab: str | None = None, item_id: str | None = None) -> int:
    """Dismiss one item, a whole tab, or everything (tab None/'all'). History is kept; only the 'new' mark goes."""
    items = [i for t in TABS if tab in (None, "", "all", t) for i in b["tabs"][t]]
    if item_id:
        items = [i for i in items if i["id"] == item_id]
    items = [i for i in items if not i["seen"]]
    ids = [int(i["id"][6:]) for i in items if i["id"].startswith("alert:")]
    keys = {i["id"] for i in items if not i["id"].startswith("alert:")}
    if ids:
        with SessionLocal() as s:
            s.query(Alert).filter(Alert.id.in_(ids)).update({"seen": True}, synchronize_session=False)
            s.commit()
    if keys:
        set_setting("alerts_seen", json.dumps(sorted(_seen_keys() | keys)))
    return len(items)


def note(where: str, exc: BaseException):
    """A background step failed and only wrote a log line: put it in the System errors tab too.
    Left out, because they have their own clear messages elsewhere and would only repeat them: an expired login
    (reconnect), a used-up daily limit, and things that are simply not set up yet (no channel, no Gmail)."""
    try:
        from .guide import record_error

        m = str(exc)
        if type(exc).__name__ in ("RefreshError", "TokenError", "NeedsReconnect") or any(x in m for x in (
                "quotaExceeded", "invalid_grant", "No YouTube channel connected", "Gmail is not connected")):
            return
        record_error(where, exc)
    except Exception:  # noqa: BLE001  (reporting a problem must never cause one)
        pass

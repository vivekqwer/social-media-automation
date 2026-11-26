"""Viral board + 5 daily picks.

Every 2 hours the YouTube India chart (videos) and today's most-viewed Shorts are saved under today's date (IST),
with how many views each gained since it was first seen. Each day up to 5 videos are picked:
  * Creative Commons, or a creator who already said yes  -> clipped at the most-replayed part and queued now
  * anyone else with a public email                      -> a ready permission mail in the inbox; the clip is made
                                                            the moment their "yes" arrives (see permissions.grant)
Music labels, studios, TV and risky titles are never picked. Nothing is uploaded without CC or permission.
"""
import json
import logging
import re
from datetime import datetime, timedelta

from . import acl, quota, youtube
from .db import Channel, Clip, Destination, Project, SessionLocal, get_setting, set_setting, utcnow, main_destination

log = logging.getLogger(__name__)
from .alerts import note as _note  # a failure that is only logged also shows in Alerts -> System errors (never raises)
PER_DAY = 5
KEEP_DAYS = 30
MIN_SECONDS = 90  # clips are cut from real videos; other people's Shorts are shown but never re-posted
IST = timedelta(hours=5, minutes=30)
LIVE_RE = re.compile(r"\blive\b|livestream|live stream|\bstream\b|\bpremiere\b", re.I)  # stream replays make poor clips


def today() -> str:
    return (utcnow() + IST).strftime("%Y-%m-%d")


def _get(key: str, default):
    raw = get_setting(key)
    return json.loads(raw) if raw else default


def _iso_seconds(d: str) -> int:
    return youtube._iso_seconds(d or "")


def _rows(items: list, short_flag: bool | None = None) -> list[dict]:
    out = []
    for it in items:
        sn, st = it.get("snippet", {}), it.get("statistics") or {}
        dur = _iso_seconds((it.get("contentDetails") or {}).get("duration", ""))
        out.append({"video_id": it["id"], "title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
                    "channel_id": sn.get("channelId", ""), "published": sn.get("publishedAt", ""),
                    "views": int(st.get("viewCount") or 0), "likes": int(st.get("likeCount") or 0),
                    "comments": int(st.get("commentCount") or 0), "duration": dur,
                    "cc": (it.get("status") or {}).get("license") == "creativeCommon",
                    "short": short_flag if short_flag is not None else 0 < dur <= 60})
    return out


def _chart() -> list[dict]:
    r = youtube.client().videos().list(part="snippet,statistics,status,contentDetails", chart="mostPopular",
                                       regionCode="IN", maxResults=50).execute()
    quota.spend("read")
    return _rows(r.get("items", []))


def _shorts() -> list[dict]:
    """Most-viewed Shorts of the last 24 h in India (one search a day: 100 units)."""
    since = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = youtube.client().search().list(part="id", type="video", videoDuration="short", order="viewCount", regionCode="IN",
                                       relevanceLanguage="hi", publishedAfter=since, maxResults=25).execute()
    quota.spend("search")
    quota.note_search()
    ids = [it["id"]["videoId"] for it in r.get("items", []) if it.get("id", {}).get("videoId")]
    if not ids:
        return []
    v = youtube.client().videos().list(part="snippet,statistics,status,contentDetails", id=",".join(ids)).execute()
    quota.spend("read")
    return _rows(v.get("items", []), short_flag=True)


def _may_search() -> bool:
    """One search for today's viral Shorts. The owner and accounts with their own Google keys: whenever the day's
    search budget allows (as before). A customer on the owner's Google project: only in its fair turn, within its
    searches a day, and only with what the owner's due uploads leave (quota.search_block)."""
    if not quota.on_shared_budget():
        return quota.can_search()
    if quota.search_block():
        quota.note_turn(False)
        return False
    return True


def refresh(force: bool = False) -> dict:
    """Save today's chart (with view growth) and fill today's picks."""
    day, now = today(), utcnow().isoformat() + "Z"
    board = _get("viral:" + day, {"videos": {}, "shorts": {}, "updated": None, "shorts_at": None})
    if not youtube.configured():
        return board
    if not (acl.allowed("page.viral") or acl.allowed("source.viral")):  # both switched off for this account: nothing to do
        return board
    try:
        rows = _chart()
    except Exception as e:  # noqa: BLE001
        log.warning("viral chart: %s", str(e)[:150])
        _note("Viral today: reading YouTube's trending list", e)
        rows = []
    shorts_due = not board.get("shorts_at") or (force and datetime.fromisoformat(board["shorts_at"][:-1]) < utcnow() - timedelta(hours=6))
    if shorts_due and _may_search():
        try:
            rows += _shorts()
            board["shorts_at"] = now
            if quota.on_shared_budget():
                quota.note_turn(True)
        except Exception as e:  # noqa: BLE001
            log.warning("viral shorts: %s", str(e)[:150])
            _note("Viral today: searching for viral Shorts", e)
    for rank, r in enumerate(rows, 1):
        bucket = board["shorts" if r["short"] else "videos"]
        old = bucket.get(r["video_id"])
        r["first_seen"] = old["first_seen"] if old else now
        r["first_views"] = old["first_views"] if old else r["views"]
        r["gain"] = r["views"] - r["first_views"]
        r["rank"] = rank
        bucket[r["video_id"]] = r
    board["updated"] = now
    set_setting("viral:" + day, json.dumps(board))
    days = sorted(set(_get("viral_days", []) + [day]))[-KEEP_DAYS:]
    for old_day in sorted(set(_get("viral_days", [])) - set(days)):
        set_setting("viral:" + old_day, "")
    set_setting("viral_days", json.dumps(days))
    pick_today()
    if not acl.source_block("viral"):  # picks that had to wait (sources stopped, a limit reached) start now
        from .jobs import executor

        for p in _picks().get(day, []):
            if p.get("held") and not p.get("project_id") and p["why"] in ("cc", "allowed"):
                executor.submit(clip_pick, day, p["video_id"])
    return board


# ---------- daily picks ----------
def _picks() -> dict:
    return _get("viral_picks", {})


def _save_picks(p: dict):
    keep = sorted(p)[-KEEP_DAYS:]
    set_setting("viral_picks", json.dumps({k: p[k] for k in keep}))


def _source(s) -> Channel:
    """The settings every viral clip uses (editable on the Sources page; it never runs on its own)."""
    ch = s.query(Channel).filter(Channel.kind == "viral").first()
    if not ch:
        dest = main_destination(s)
        ch = Channel(kind="viral", name="Viral picks (5 a day)", query="viral picks", mode="permission", auto=False,
                     videos_per_run=0, clips_per_video=1, clip_len=35, aspect="9:16", gap_hours=2,
                     destination_id=dest.id if dest else None, captions=True, review_first=False,
                     post_times="09:00, 12:00, 15:00, 18:00, 21:00", tz_offset=-330)
        s.add(ch)
        s.commit()
    return ch


def pick_today(extra: str | None = None) -> list:
    """Choose today's videos (or add the one the owner picked by hand) and start what is allowed already."""
    from . import permissions

    day = today()
    allp = _picks()
    picks = allp.get(day, [])
    creators0 = permissions._load()
    # drop today's automatic picks that newer rules exclude, as long as nothing was sent or made for them yet
    keep = [p for p in picks if p.get("by_hand") or p.get("project_id") or creators0.get(p["channel_id"], {}).get("mailed")
            or not LIVE_RE.search(p["title"])]
    if len(keep) != len(picks):
        for p in picks:
            c = creators0.get(p["channel_id"], {})
            if p not in keep and c.get("video_id") == p["video_id"] and c.get("status") == "new" and not c.get("mailed"):
                permissions.set_status(p["channel_id"], "skipped")  # their inbox mail was only for this pick
        allp[day] = picks = keep
        _save_picks(allp)
    if not acl.allowed("source.viral"):  # "Viral picks" is switched off for this account: the board is shown, nothing is picked
        return picks
    if len(picks) >= PER_DAY and not extra:
        return picks
    board = _get("viral:" + day, {}).get("videos", {})
    with SessionLocal() as s:
        used = {v for (v,) in s.query(Project.video_id).all()}
    used |= {p["video_id"] for ps in allp.values() for p in ps}
    creators = permissions._load()
    cands = sorted(board.values(), key=lambda r: (r.get("gain", 0), r["views"]), reverse=True)
    from . import strikes

    struck = strikes.blocked()  # creators on the never-use list after a strike: never picked, never asked again
    if extra and any(r["video_id"] == extra and r.get("channel_id") in struck for r in cands):
        raise ValueError(strikes.blocked_message())
    cands = [r for r in cands if r.get("channel_id") not in struck]
    if extra:
        cands = [r for r in cands if r["video_id"] == extra]
        if not cands:
            raise ValueError("That video is not on today's board.")
        if extra in used:
            raise ValueError("Already picked or clipped before.")
    else:
        cands = [r for r in cands if r["video_id"] not in used and r["duration"] >= MIN_SECONDS
                 and not youtube.RISKY.search(r["title"]) and not permissions.LABELS.search(r["channel"])
                 and not LIVE_RE.search(r["title"])
                 and creators.get(r["channel_id"], {}).get("status") not in ("declined", "skipped")]
    # creators who already said yes first, then Creative Commons, then everyone else
    cands.sort(key=lambda r: 0 if creators.get(r["channel_id"], {}).get("status") == "granted" else 1 if r["cc"] else 2)
    unknown = [r["channel_id"] for r in cands if not r["cc"] and r["channel_id"] not in creators][:50]
    emails = permissions.lookup_emails(unknown) if unknown else {}
    new = []
    for r in cands:
        if len(picks) + len(new) >= (PER_DAY + 1 if extra else PER_DAY) or any(x["video_id"] == r["video_id"] for x in new):
            break
        c = creators.get(r["channel_id"], {})
        if r["cc"]:
            why = "cc"
        elif c.get("status") == "granted":
            why = "allowed"
        elif c.get("email") or emails.get(r["channel_id"]) or extra:
            why = "ask"
            permissions.add_creator(r["channel_id"], r["channel"], r["video_id"], r["title"], r["views"],
                                    emails.get(r["channel_id"]) or c.get("email") or "")
        else:
            continue  # no public email: cannot ask, so not picked
        new.append({"video_id": r["video_id"], "title": r["title"], "channel": r["channel"], "channel_id": r["channel_id"],
                    "views": r["views"], "cc": r["cc"], "why": why, "picked_at": utcnow().isoformat() + "Z",
                    "by_hand": bool(extra), "project_id": None, "note": ""})
    if not new:
        return picks
    allp = _picks()
    allp.setdefault(day, []).extend(new)
    _save_picks(allp)
    from .jobs import executor

    for p in new:
        if p["why"] in ("cc", "allowed"):
            executor.submit(clip_pick, day, p["video_id"])
    try:
        from .telegram import esc, notify

        ask = [p for p in new if p["why"] == "ask"]
        notify(f"🔥 <b>Today's viral picks</b> ({len(new)})\n" + "\n".join(
            f"• {esc(p['title'][:60])} — {esc(p['channel'])}: "
            + ("clipping now" if p["why"] != "ask" else "permission mail ready") for p in new)
            + (f"\n\n{len(ask)} mail(s) are ready: Dashboard → Viral → “Send all ready mails”." if ask else ""))
    except Exception:  # noqa: BLE001
        pass
    return allp[day]


def clip_pick(day: str, video_id: str):
    """Cut the most-replayed part of a picked video and queue it (CC or permission given)."""
    from . import permissions
    from .autopilot import _process_video

    allp = _picks()
    pick = next((p for p in allp.get(day, []) if p["video_id"] == video_id), None)
    if not pick or pick.get("project_id"):
        return
    c = permissions._load().get(pick["channel_id"], {})
    mode = "cc" if pick["cc"] else "permission"
    if mode == "permission" and c.get("status") != "granted":
        return
    blocked = acl.source_block("viral")  # sources stopped / viral picks off / a limit reached: wait, try again later
    if blocked:
        pick["note"], pick["held"] = f"waiting: {blocked}", True
        _save_picks(allp)
        return
    out = []
    with SessionLocal() as s:
        ch = _source(s)
        try:
            _process_video(s, ch, video_id, out, mode=mode, note=c.get("proof") if mode == "permission" else None)
        except Exception as e:  # noqa: BLE001
            out.append(f"failed: {str(e)[:150]}")
        p = s.query(Project).filter(Project.video_id == video_id).order_by(Project.id.desc()).first()
        pid = p.id if p else None
    allp = _picks()
    for x in allp.get(day, []):
        if x["video_id"] == video_id:
            x["project_id"], x["note"] = pid, "; ".join(out)[-300:]
            x.pop("held", None)
    _save_picks(allp)


def on_granted(channel_id: str):
    """A creator said yes: clip every picked video of theirs that was waiting."""
    from .jobs import executor

    for day, ps in _picks().items():
        for p in ps:
            if p["channel_id"] == channel_id and not p.get("project_id"):
                p["why"] = "allowed"
                executor.submit(clip_pick, day, p["video_id"])
    allp = _picks()
    for ps in allp.values():
        for p in ps:
            if p["channel_id"] == channel_id and not p.get("project_id"):
                p["why"] = "allowed"
    _save_picks(allp)


def _status(s, p: dict, creators: dict) -> dict:
    """Where this pick is right now, in plain words, plus the next step for the owner (if any)."""
    from . import permissions

    if p.get("project_id"):
        pr = s.get(Project, p["project_id"])
        clips = s.query(Clip).filter(Clip.project_id == p["project_id"]).all()
        if not clips:
            if pr and pr.analysis_pending:
                return {"step": "clip", "text": "Finding the most-replayed part (helper PC)"}
            return {"step": "clip", "text": p.get("note") or "Making the clip"}
        c = clips[0]
        if c.status == "uploaded":
            return {"step": "done", "text": "On YouTube", "youtube_id": c.youtube_id, "clip_id": c.id}
        if c.status == "queued":
            return {"step": "queued", "text": "Goes up " + ((c.publish_at.isoformat() + "Z") if c.publish_at else "next free slot"),
                    "at": (c.publish_at.isoformat() + "Z") if c.publish_at else None, "clip_id": c.id}
        if c.status == "failed":
            return {"step": "failed", "text": "Failed: " + (c.error or "")[:150], "clip_id": c.id}
        if c.status == "rendered":
            return {"step": "review", "text": "Ready — waiting for your approval", "clip_id": c.id}
        return {"step": "clip", "text": "Downloading on the helper PC" if c.status == "fetching" else "Editing the clip",
                "clip_id": c.id}
    if p["why"] in ("cc", "allowed"):
        return {"step": "clip", "text": (p.get("note") or "Waiting") if p.get("held") else "Starting the clip"}
    c = creators.get(p["channel_id"], {})
    st = c.get("status")
    if st == "granted":
        return {"step": "clip", "text": "They said yes — starting the clip"}
    if st == "replied":
        return {"step": "reply", "text": "They replied — read it and decide", "action": "decide"}
    if st == "declined":
        return {"step": "no", "text": "They said no — not used"}
    if st == "skipped":
        return {"step": "no", "text": "Skipped"}
    if c.get("mailed"):
        sent = ""
        try:
            sent = (datetime.fromisoformat(c["asked_at"].rstrip("Z")) + IST).strftime("%d %b, %I:%M %p")
        except Exception:  # noqa: BLE001
            pass
        return {"step": "wait", "text": f"Mail sent {sent} — waiting for their reply (checked every 15 min)"}
    if c.get("email"):
        if permissions.auto_on() and not permissions.auto_block(c, creators):
            return {"step": "mail", "text": "Mail ready — goes out by itself (or press Send)", "action": "send",
                    "email": c["email"]}
        return {"step": "mail", "text": "Mail ready — press Send", "action": "send", "email": c["email"]}
    if (c.get("email_search") or {}).get("tries", 0) >= permissions.SEARCH_TRIES:
        return {"step": "mail", "text": "No public e-mail found — add it by hand", "action": "email"}
    return {"step": "mail", "text": "Looking for the creator's e-mail", "action": "email"}


def view(day: str | None = None) -> dict:
    from . import permissions

    day = day or today()
    board = _get("viral:" + day, {"videos": {}, "shorts": {}})
    creators = permissions._load()
    picks = _picks().get(day, [])
    with SessionLocal() as s:
        picks = [{**p, "status": _status(s, p, creators)} for p in picks]
    picked = {p["video_id"] for p in picks}

    def rows(bucket):
        out = sorted(board.get(bucket, {}).values(), key=lambda r: r["views"], reverse=True)
        for r in out:
            r["picked"] = r["video_id"] in picked
            r["creator"] = (creators.get(r["channel_id"]) or {}).get("status")
        return out

    return {"day": day, "today": today(), "days": sorted(_get("viral_days", []), reverse=True), "updated": board.get("updated"),
            "videos": rows("videos"), "shorts": rows("shorts"), "picks": picks, "per_day": PER_DAY,
            "ready_mails": sum(1 for p in picks if p["status"].get("action") == "send")}


def send_ready_mails() -> dict:
    """The owner pressed “Send all ready mails”: send each picked creator's ready mail (Gmail limit 5 a day)."""
    from . import permissions

    sent, errors = [], []
    creators = permissions._load()
    for ps in _picks().values():
        for p in ps:
            c = creators.get(p["channel_id"], {})
            if p["why"] == "ask" and c.get("email") and not c.get("mailed") and c.get("status") == "new" \
                    and p["channel_id"] not in sent:
                try:
                    permissions.send_mail(p["channel_id"])
                    sent.append(p["channel_id"])
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{c.get('name')}: {str(e)[:120]}")
    return {"sent": len(sent), "errors": errors}

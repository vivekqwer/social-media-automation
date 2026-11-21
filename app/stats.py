"""Live numbers for my channels: subscribers, views, videos, and every video's views/likes/comments.

Synced from the YouTube Data API with each channel's own login (read-only, ~2 quota units per channel per sync).
"""
import logging
from datetime import datetime, timedelta

from . import quota, youtube
from .db import ChannelStat, Destination, SessionLocal, VideoStat, utcnow

log = logging.getLogger(__name__)
MAX_VIDEOS = 200  # newest videos per channel to track


def _dt(iso: str | None):
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=None)


def sync_channel(dest_id: int) -> dict:
    yt = youtube.client(dest_id)
    ch = yt.channels().list(part="statistics,contentDetails", mine=True).execute().get("items") or []
    quota.spend("read")
    if not ch:
        raise RuntimeError("YouTube returned no channel for this login")
    st, uploads = ch[0]["statistics"], ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while len(ids) < MAX_VIDEOS:
        r = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token).execute()
        quota.spend("read")
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    vids = {}
    for i in range(0, len(ids), 50):
        r = yt.videos().list(part="statistics,snippet,status", id=",".join(ids[i:i + 50]), maxResults=50).execute()
        quota.spend("read")
        vids.update({it["id"]: it for it in r.get("items", [])})
    now = utcnow()
    with SessionLocal() as s:
        s.add(ChannelStat(destination_id=dest_id, at=now, subscribers=int(st.get("subscriberCount") or 0),
                          subscribers_hidden=bool(st.get("hiddenSubscriberCount")),
                          views=int(st.get("viewCount") or 0), videos=int(st.get("videoCount") or 0)))
        for vid, it in vids.items():
            vs = it.get("statistics") or {}
            row = s.get(VideoStat, vid) or VideoStat(video_id=vid)
            row.views_prev = row.views if row.updated_at else None
            row.destination_id, row.title = dest_id, (it.get("snippet") or {}).get("title", "")[:300]
            row.published_at = _dt((it.get("snippet") or {}).get("publishedAt"))
            row.privacy = (it.get("status") or {}).get("privacyStatus")
            row.views, row.likes, row.comments = (int(vs.get(k) or 0) for k in ("viewCount", "likeCount", "commentCount"))
            row.updated_at = now
            s.merge(row)
        # videos that disappeared from the channel are no longer counted
        s.query(VideoStat).filter(VideoStat.destination_id == dest_id, VideoStat.updated_at < now).delete()
        s.query(ChannelStat).filter(ChannelStat.at < now - timedelta(days=120)).delete()  # keep 4 months of history
        s.commit()
    return {"videos": len(vids)}


def sync_all() -> dict:
    out = {}
    with SessionLocal() as s:
        dests = [d.id for d in s.query(Destination).all()]
    for did in dests:
        try:
            out[did] = sync_channel(did)
        except Exception as e:  # noqa: BLE001  (login expired etc.: keep the last numbers, try again next time)
            log.warning("stats: channel %s not synced: %s", did, str(e)[:200])
            out[did] = {"error": str(e)[:200]}
    return out


def recently_synced(seconds: int) -> bool:
    with SessionLocal() as s:
        last = s.query(ChannelStat.at).order_by(ChannelStat.at.desc()).first()
    return bool(last and last[0] > utcnow() - timedelta(seconds=seconds))


def _at_or_before(s, dest_id: int, t: datetime):
    return (s.query(ChannelStat).filter(ChannelStat.destination_id == dest_id, ChannelStat.at <= t)
            .order_by(ChannelStat.at.desc()).first())


IST = timedelta(hours=5, minutes=30)


def best_hours(s, dest_id: int) -> dict:
    """Which publish hours (IST) got the most views per day so far (needs a few public videos)."""
    now = utcnow()
    rows = s.query(VideoStat).filter(VideoStat.destination_id == dest_id, VideoStat.privacy == "public",
                                     VideoStat.published_at.isnot(None)).all()
    by = {}
    for v in rows:
        age = max(0.5, (now - v.published_at).total_seconds() / 86400)
        h = (v.published_at + IST).hour
        by.setdefault(h, []).append(v.views / age)
    ranked = sorted(((h, sum(x) / len(x), len(x)) for h, x in by.items()), key=lambda t: -t[1])
    return {"videos": len(rows), "ranked": [{"hour": h, "views_per_day": round(a, 1), "videos": n} for h, a, n in ranked[:5]],
            "enough": len(rows) >= 8}


def health(s, dest_id: int, last, watch) -> dict:
    """0-100: problems on YouTube, paused uploads, failed uploads, how much of each video people watch, growth."""
    from .db import Alert, Clip, Destination

    score, notes = 100, []
    d = s.get(Destination, dest_id)
    since = utcnow() - timedelta(days=90)
    bad = s.query(Alert).join(Clip, Clip.id == Alert.clip_id).filter(Clip.destination_id == dest_id, Alert.created_at >= since,
                                                                     Alert.kind.in_(["removed", "rejected", "blocked"])).count()
    if bad:
        score -= min(45, 15 * bad)
        notes.append(f"{bad} video(s) removed/blocked in 90 days")
    if d and d.paused:
        score -= 15
        notes.append("uploads are paused")
    failed = s.query(Clip).filter(Clip.destination_id == dest_id, Clip.status == "failed").count()
    if failed:
        score -= min(10, 2 * failed)
        notes.append(f"{failed} failed clip(s)")
    vids = (watch or {}).get("videos") or {}
    if vids:
        avg = sum(v["avg_percent"] for v in vids.values()) / len(vids)
        if avg < 40:
            score -= 10
            notes.append(f"people watch {avg:.0f}% of a video on average — make the start stronger")
    if not notes:
        notes.append("no problems found")
    return {"score": max(0, score), "notes": notes}


def overview() -> dict:
    now = utcnow()
    with SessionLocal() as s:
        chans = []
        for d in s.query(Destination).order_by(Destination.id):
            last = (s.query(ChannelStat).filter(ChannelStat.destination_id == d.id)
                    .order_by(ChannelStat.at.desc()).first())
            day = _at_or_before(s, d.id, now - timedelta(hours=24))
            week = _at_or_before(s, d.id, now - timedelta(days=7))
            hist = (s.query(ChannelStat).filter(ChannelStat.destination_id == d.id, ChannelStat.at >= now - timedelta(days=30))
                    .order_by(ChannelStat.at).all())
            daily = {}
            for h in hist:  # last snapshot of each day, for a small chart
                daily[h.at.strftime("%Y-%m-%d")] = {"views": h.views, "subscribers": h.subscribers}
            diff = lambda a, b, k: (getattr(a, k) - getattr(b, k)) if a and b else None  # noqa: E731
            chans.append({
                "id": d.id, "name": d.name,
                "subscribers": last.subscribers if last else None,
                "subscribers_hidden": bool(last and last.subscribers_hidden),
                "views": last.views if last else None, "videos": last.videos if last else None,
                "views_24h": diff(last, day, "views"), "subs_24h": diff(last, day, "subscribers"),
                "views_7d": diff(last, week, "views"), "subs_7d": diff(last, week, "subscribers"),
                "synced_at": (last.at.isoformat() + "Z") if last else None,
                "daily": [{"day": k, **v} for k, v in sorted(daily.items())],
                "watch": watch_time(d.id), "watch_status": get_setting(f"watchtime-status:{d.id}"),
                "best_hours": best_hours(s, d.id),
            })
            chans[-1]["health"] = health(s, d.id, last, chans[-1]["watch"])
        watch_by_video = {}
        for c in chans:
            watch_by_video.update((c["watch"] or {}).get("videos") or {})
        vids = s.query(VideoStat).order_by(VideoStat.published_at.desc()).limit(MAX_VIDEOS).all()
        names = {c["id"]: c["name"] for c in chans}
        videos = [{"video_id": v.video_id, "channel": names.get(v.destination_id), "title": v.title,
                   "published_at": (v.published_at.isoformat() + "Z") if v.published_at else None,
                   "privacy": v.privacy, "views": v.views, "likes": v.likes, "comments": v.comments,
                   "new_views": (v.views - v.views_prev) if v.views_prev is not None else None,
                   "watch": watch_by_video.get(v.video_id)} for v in vids]
    return {"channels": chans, "videos": videos}


# ---------- watch time (YouTube Analytics API; YouTube publishes these numbers with a 1-3 day delay) ----------
import json  # noqa: E402

from .db import get_setting, set_setting  # noqa: E402

TRAFFIC = {"YT_SEARCH": "YouTube search", "SUBSCRIBER": "Subscribers", "RELATED_VIDEO": "Suggested videos",
           "SHORTS": "Shorts feed", "BROWSE": "Home / browse", "EXT_URL": "Other websites", "NO_LINK_OTHER": "Direct / unknown",
           "PLAYLIST": "Playlists", "YT_CHANNEL": "Channel page", "NOTIFICATION": "Notifications", "YT_OTHER_PAGE": "Other YouTube",
           "END_SCREEN": "End screens", "HASHTAGS": "Hashtags", "SOUND_PAGE": "Sound pages", "ANNOTATION": "Cards"}


def _rows(r: dict) -> list[dict]:
    cols = [c["name"] for c in r.get("columnHeaders", [])]
    return [dict(zip(cols, row)) for row in r.get("rows") or []]


def sync_watch_time(dest_id: int) -> dict:
    ya = youtube.analytics_client(dest_id)
    today = utcnow().date()
    start = (today - timedelta(days=28)).isoformat()
    q = lambda **k: _rows(ya.reports().query(ids="channel==MINE", endDate=today.isoformat(), **k).execute())  # noqa: E731
    daily = q(startDate=start, dimensions="day", sort="day",
              metrics="views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost")
    life = q(startDate="2005-02-14", metrics="views,estimatedMinutesWatched")
    videos = q(startDate=start, dimensions="video", sort="-estimatedMinutesWatched", maxResults=50,
               metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage")
    traffic = q(startDate=start, dimensions="insightTrafficSourceType", sort="-views", metrics="views,estimatedMinutesWatched")
    countries = q(startDate=start, dimensions="country", sort="-views", maxResults=10, metrics="views,estimatedMinutesWatched")
    data = {
        "fetched_at": utcnow().isoformat() + "Z",
        "lifetime_minutes": int((life[0] if life else {}).get("estimatedMinutesWatched") or 0),
        "minutes_28d": sum(int(d.get("estimatedMinutesWatched") or 0) for d in daily),
        "views_28d": sum(int(d.get("views") or 0) for d in daily),
        "subs_gained_28d": sum(int(d.get("subscribersGained") or 0) for d in daily),
        "subs_lost_28d": sum(int(d.get("subscribersLost") or 0) for d in daily),
        "daily": [{"day": d["day"], "minutes": int(d.get("estimatedMinutesWatched") or 0), "views": int(d.get("views") or 0)}
                  for d in daily],
        "videos": {v["video"]: {"minutes": int(v.get("estimatedMinutesWatched") or 0), "avg_seconds": int(v.get("averageViewDuration") or 0),
                                "avg_percent": round(float(v.get("averageViewPercentage") or 0), 1)} for v in videos},
        "traffic": [{"source": TRAFFIC.get(t["insightTrafficSourceType"], t["insightTrafficSourceType"].replace("_", " ").title()),
                     "views": int(t.get("views") or 0), "minutes": int(t.get("estimatedMinutesWatched") or 0)} for t in traffic],
        "countries": [{"country": c["country"], "views": int(c.get("views") or 0), "minutes": int(c.get("estimatedMinutesWatched") or 0)}
                      for c in countries],
    }
    set_setting(f"watchtime:{dest_id}", json.dumps(data))
    return data


def watch_time(dest_id: int) -> dict | None:
    raw = get_setting(f"watchtime:{dest_id}")
    return json.loads(raw) if raw else None


def sync_all_watch_time(max_age_minutes: int = 0) -> dict:
    out = {}
    with SessionLocal() as s:
        dests = [d.id for d in s.query(Destination).all()]
    for did in dests:
        old = watch_time(did)
        if old and max_age_minutes and datetime.fromisoformat(old["fetched_at"].rstrip("Z")) > utcnow() - timedelta(minutes=max_age_minutes):
            continue
        try:
            sync_watch_time(did)
            set_setting(f"watchtime-status:{did}", "ok")
        except youtube.NeedsReconnect:
            set_setting(f"watchtime-status:{did}", "reconnect")
        except Exception as e:  # noqa: BLE001
            m = str(e)
            set_setting(f"watchtime-status:{did}", "api_off" if "has not been used" in m or "is disabled" in m
                        else "reconnect" if "insufficient" in m.lower() or "403" in m else "error: " + m[:200])
            log.warning("watch time: channel %s: %s", did, m[:200])
    return out

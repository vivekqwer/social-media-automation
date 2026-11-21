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
            })
        vids = s.query(VideoStat).order_by(VideoStat.published_at.desc()).limit(MAX_VIDEOS).all()
        names = {c["id"]: c["name"] for c in chans}
        videos = [{"video_id": v.video_id, "channel": names.get(v.destination_id), "title": v.title,
                   "published_at": (v.published_at.isoformat() + "Z") if v.published_at else None,
                   "privacy": v.privacy, "views": v.views, "likes": v.likes, "comments": v.comments,
                   "new_views": (v.views - v.views_prev) if v.views_prev is not None else None} for v in vids]
    return {"channels": chans, "videos": videos}

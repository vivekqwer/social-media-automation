"""Competitor tracker: find videos on other channels that get far more views than that channel usually gets.

Standalone module: one daily read per tracked channel (about 3 quota units), never touches uploads.
"""
import json
import logging
import statistics
from datetime import datetime, timedelta

from . import quota, youtube
from .db import get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
from .alerts import note as _note  # a failure that is only logged also shows in Alerts -> System errors (never raises)
KEY = "competitors"


def _load() -> dict:
    raw = get_setting(KEY)
    return json.loads(raw) if raw else {"channels": [], "outliers": [], "checked_at": None}


def _save(d: dict):
    set_setting(KEY, json.dumps(d))


def add(query: str) -> dict:
    url, name = youtube.api_resolve_channel(query)
    d = _load()
    if not any(c["url"] == url for c in d["channels"]):
        d["channels"].append({"url": url, "name": name})
        _save(d)
    return d


def remove(url: str) -> dict:
    d = _load()
    d["channels"] = [c for c in d["channels"] if c["url"] != url]
    d["outliers"] = [o for o in d["outliers"] if o.get("channel_url") != url]
    _save(d)
    return d


def _scan_channel(c: dict) -> list[dict]:
    ids = youtube.api_list_video_ids(c["url"], 30)
    if not ids:
        return []
    yt = youtube.client()
    r = yt.videos().list(part="statistics,snippet,contentDetails", id=",".join(ids[:50]), maxResults=50).execute()
    quota.spend("read")
    now = utcnow()
    vids = []
    for it in r.get("items", []):
        st, sn = it.get("statistics") or {}, it.get("snippet") or {}
        pub = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")).replace(tzinfo=None)
        age_days = max(0.5, (now - pub).total_seconds() / 86400)
        views = int(st.get("viewCount") or 0)
        vids.append({"video_id": it["id"], "title": sn.get("title", ""), "views": views, "age_days": round(age_days, 1),
                     "views_per_day": views / age_days, "published_at": sn.get("publishedAt"),
                     "duration": youtube._iso_seconds((it.get("contentDetails") or {}).get("duration", ""))})
    if len(vids) < 4:
        return []
    typical = statistics.median(v["views_per_day"] for v in vids) or 1
    out = []
    for v in vids:
        ratio = v["views_per_day"] / typical
        if ratio >= 3 and v["views"] >= 1000 and v["age_days"] <= 30:
            out.append({**v, "ratio": round(ratio, 1), "channel": c["name"], "channel_url": c["url"]})
    return out


def scan() -> dict:
    d = _load()
    found = []
    for c in d["channels"]:
        if quota.DAILY_UNITS - quota.used() < 2 * quota.UPLOAD_COST or not quota.shared_room(5 * quota.COST["read"]):
            break  # never use the upload budget (nor, on the owner's project, the owner's uploads)
        try:
            found += _scan_channel(c)
        except Exception as e:  # noqa: BLE001
            log.warning("competitors: %s: %s", c.get("name"), str(e)[:200])
            _note("Ideas: checking a channel you follow", e)
    d["outliers"] = sorted(found, key=lambda v: -v["ratio"])[:60]
    d["checked_at"] = utcnow().isoformat() + "Z"
    _save(d)
    if found:
        try:
            from .telegram import esc, notify

            top = d["outliers"][:3]
            notify("💡 <b>Video ideas</b> (doing far better than usual):\n" + "\n".join(
                f"• {esc(v['title'][:70])} — {esc(v['channel'])}, {v['ratio']}× normal" for v in top))
        except Exception:  # noqa: BLE001
            pass
    return d


def scan_if_due():
    d = _load()
    if d["channels"] and (not d["checked_at"] or datetime.fromisoformat(d["checked_at"].rstrip("Z")) < utcnow() - timedelta(hours=20)):
        scan()


def overview() -> dict:
    return _load()


# ---------- trending in India (YouTube chart; 1 quota unit, cached 6 hours) ----------
def trending(category: str = "") -> list[dict]:
    key = f"trending:{category}"
    raw = get_setting(key)
    if raw:
        cached = json.loads(raw)
        fresh = datetime.fromisoformat(cached["at"]) > utcnow() - timedelta(hours=6)
        if fresh and all("channel_id" in it for it in cached["items"]):  # older caches lack the channel and licence
            return cached["items"]
    kw = {"part": "snippet,statistics,status", "chart": "mostPopular", "regionCode": "IN", "maxResults": 30}
    if category:
        kw["videoCategoryId"] = category
    r = youtube.client().videos().list(**kw).execute()
    quota.spend("read")
    items = [{"video_id": it["id"], "title": it["snippet"].get("title", ""), "channel": it["snippet"].get("channelTitle", ""),
              "views": int((it.get("statistics") or {}).get("viewCount") or 0), "tags": (it["snippet"].get("tags") or [])[:8],
              "channel_id": it["snippet"].get("channelId", ""), "cc": (it.get("status") or {}).get("license") == "creativeCommon"}
             for it in r.get("items", [])]
    set_setting(key, json.dumps({"at": utcnow().isoformat(), "items": items}))
    return items

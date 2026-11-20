"""YouTube Data API quota: one daily budget per Google Cloud project, shared by ALL connected channels.

Default 10,000 units/day, reset at midnight Pacific time. An upload costs 1,600 units, so only ~5 uploads
fit in a day across every channel together. Usage is counted here so uploads and schedules can be planned.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Setting, SessionLocal

PT = ZoneInfo("America/Los_Angeles")
DAILY_UNITS = int(os.getenv("YT_DAILY_QUOTA") or "10000")
COST = {"upload": 1600, "thumbnail": 50, "search": 100, "read": 1}
UPLOAD_COST = COST["upload"] + COST["thumbnail"]
RESERVE = 400  # kept free for the video monitor, lookups and topic searches


def _pt_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(PT).strftime("%Y-%m-%d")


def pt_day_of(naive_utc: datetime) -> str:
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(PT).strftime("%Y-%m-%d")


def next_reset() -> datetime:
    """Next midnight Pacific, as naive UTC."""
    pt = datetime.now(timezone.utc).astimezone(PT)
    nxt = (pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.astimezone(timezone.utc).replace(tzinfo=None)


def spend(kind: str, n: int = 1):
    key = "quota:" + _pt_day()
    with SessionLocal() as s:
        row = s.get(Setting, key)
        if row:
            row.value = str(int(row.value or 0) + COST[kind] * n)
        else:
            s.add(Setting(key=key, value=str(COST[kind] * n)))
        s.commit()


def exhaust():
    """YouTube said the quota is used up (e.g. other apps share the project): show it as full until reset."""
    spend("read", max(0, DAILY_UNITS - used()))


def used() -> int:
    with SessionLocal() as s:
        row = s.get(Setting, "quota:" + _pt_day())
        return int(row.value) if row and row.value else 0


def capacity() -> int:
    """Uploads that fit in one day (all channels together)."""
    return max(1, (DAILY_UNITS - RESERVE) // UPLOAD_COST)


def can_upload() -> bool:
    return DAILY_UNITS - used() - UPLOAD_COST >= RESERVE // 2


def can_search() -> bool:
    return DAILY_UNITS - used() - COST["search"] >= UPLOAD_COST  # never eat the budget of the next upload


def summary() -> dict:
    u = used()
    return {"used": u, "limit": DAILY_UNITS, "left": max(0, DAILY_UNITS - u),
            "uploads_left": max(0, (DAILY_UNITS - u - RESERVE // 2) // UPLOAD_COST), "capacity": capacity(),
            "resets_at": next_reset().isoformat() + "Z"}

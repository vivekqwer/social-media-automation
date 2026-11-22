"""YouTube Data API quota: one daily budget per Google Cloud project, shared by ALL connected channels.

Default 10,000 units/day, reset at midnight Pacific time. An upload costs 1,600 units, so only ~5 uploads
fit in a day across every channel together. Usage is counted here so uploads and schedules can be planned.
"""
import os
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import tenant
from .db import Setting, SessionLocal as _TenantSession, get_setting


class _BudgetSession:
    """The YouTube budget belongs to a Google project, not to a customer. A customer who uses the server's own
    project (no keys of their own yet) spends from the owner's budget; one with their own keys has their own."""

    def __call__(self):
        own = tenant.current() == tenant.OWNER or bool(get_setting("google_client_id"))
        return _Scoped(tenant.current() if own else tenant.OWNER)


class _Scoped:
    def __init__(self, tid: int):
        self.tid = tid

    def __enter__(self):
        self.cm = tenant.use(self.tid)
        self.cm.__enter__()
        self.s = _TenantSession()
        return self.s.__enter__()

    def __exit__(self, *exc):
        try:
            return self.s.__exit__(*exc)
        finally:
            self.cm.__exit__(*exc)


SessionLocal = _BudgetSession()

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


_lock = threading.Lock()


def spend(kind: str, n: int = 1, units: int | None = None):
    key, add = "quota:" + _pt_day(), units if units is not None else COST[kind] * n
    with _lock, SessionLocal() as s:  # one process: a lock makes read-modify-write safe across threads
        row = s.get(Setting, key)
        if row:
            row.value = str(int(row.value or 0) + add)
        else:
            s.add(Setting(key=key, value=str(add)))
        s.commit()


def spent_on_search() -> int:
    with SessionLocal() as s:
        row = s.get(Setting, "quota-search:" + _pt_day())
        return int(row.value) if row and row.value else 0


def exhaust():
    """YouTube said the quota is used up (e.g. other apps share the project): show it as full until reset."""
    spend("read", units=max(0, DAILY_UNITS - used()))


def used() -> int:
    with SessionLocal() as s:
        row = s.get(Setting, "quota:" + _pt_day())
        return int(row.value) if row and row.value else 0


def capacity() -> int:
    """Uploads that fit in one day (all channels together)."""
    return max(1, (DAILY_UNITS - RESERVE) // UPLOAD_COST)


def can_upload() -> bool:
    return DAILY_UNITS - used() - UPLOAD_COST >= RESERVE // 2


SEARCH_BUDGET = 400  # topic searches may use at most this much a day, so uploads keep their budget


def can_search() -> bool:
    return spent_on_search() + COST["search"] <= SEARCH_BUDGET and DAILY_UNITS - used() - COST["search"] >= UPLOAD_COST


def note_search():
    key = "quota-search:" + _pt_day()
    with _lock, SessionLocal() as s:
        row = s.get(Setting, key)
        if row:
            row.value = str(int(row.value or 0) + COST["search"])
        else:
            s.add(Setting(key=key, value=str(COST["search"])))
        s.commit()


def summary() -> dict:
    u = used()
    return {"used": u, "limit": DAILY_UNITS, "left": max(0, DAILY_UNITS - u),
            "uploads_left": max(0, (DAILY_UNITS - u - RESERVE // 2) // UPLOAD_COST), "capacity": capacity(),
            "resets_at": next_reset().isoformat() + "Z"}

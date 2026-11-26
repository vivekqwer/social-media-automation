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
COST = {"upload": 1600, "thumbnail": 50, "search": 100, "read": 1, "comment": 50, "update": 50}
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


LEAD_DAYS_RANGE = (2, 400)


def upload_lead_hours() -> int:
    """How long before its publish time a clip is sent to YouTube (YouTube keeps it private until that time).
    Usually 48 hours. The account setting "upload_lead_days" sends clips earlier: with a large number everything that
    is waiting goes up as fast as the daily budget allows, earliest publish time first, and YouTube publishes each one
    on its own day even when this server is not running."""
    from .config import UPLOAD_LEAD_HOURS

    try:
        days = int(get_setting("upload_lead_days") or 0)
    except (TypeError, ValueError):
        days = 0
    if not days:
        return UPLOAD_LEAD_HOURS
    return min(max(days, LEAD_DAYS_RANGE[0]), LEAD_DAYS_RANGE[1]) * 24


def capacity() -> int:
    """Uploads that fit in one day (all channels together)."""
    return max(1, (DAILY_UNITS - RESERVE) // UPLOAD_COST)


def can_upload() -> bool:
    return DAILY_UNITS - used() - UPLOAD_COST >= RESERVE // 2


def can_comment(uploads_waiting: int = 0) -> bool:
    """Uploads come first: a comment (50 units, plus a few reads) is only posted when what is left today still holds
    every upload that is waiting to go up before the reset (as many as fit at all) and the reserve."""
    left = DAILY_UNITS - used() - RESERVE // 2
    need = min(max(0, int(uploads_waiting)), max(0, left // UPLOAD_COST)) * UPLOAD_COST
    return left - need >= COST["comment"] + 5 * COST["read"]


def can_update(uploads_waiting: int = 0) -> bool:
    """One videos.update (keeping a video private after a strike: 50 units + a read) fits in what is left today after
    every upload that is waiting (as many as fit at all) and half the reserve — the same rule as can_comment."""
    left = DAILY_UNITS - used() - RESERVE // 2
    need = min(max(0, int(uploads_waiting)), max(0, left // UPLOAD_COST)) * UPLOAD_COST
    return left - need >= COST["update"] + COST["read"]


def can_read(units: int = 1, uploads_waiting: int = 0) -> bool:
    """A few read units (looking up a creator's public e-mail) fit in what is left today after every upload that is
    waiting (as many as fit at all) and half the reserve — the same rule as can_comment: uploads come first."""
    left = DAILY_UNITS - used() - RESERVE // 2
    need = min(max(0, int(uploads_waiting)), max(0, left // UPLOAD_COST)) * UPLOAD_COST
    return left - need >= max(1, int(units))


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


# ---------------------------------------------------------------- topic searches: a fair turn for every account
# Customers without Google keys of their own search on the owner's Google project, so their topic searches and the
# owner's share one search budget (SEARCH_BUDGET, after the uploads: can_search). Turns are spread over the accounts:
# * an account on someone else's project makes at most SEARCHES_PER_ACCOUNT topic searches a day;
# * an account that already searched more today waits while another account on the same project, with fewer searches
#   today, is waiting for one (asked within TURN_WAIT and was refused) — round-robin, fewest searches first.
# An account with its own keys (and the owner) has the per-day cap only from its own budget. Uploads always come first.
SEARCHES_PER_ACCOUNT = int(os.getenv("SEARCHES_PER_ACCOUNT") or "2")
TURN_WAIT = timedelta(hours=8)


def on_shared_budget() -> bool:
    """This account spends from the owner's Google project (a customer without keys of its own)."""
    return tenant.current() != tenant.OWNER and not get_setting("google_client_id")


def _turns_key() -> str:
    return "quota-turns:" + _pt_day()


def _turns_load(s) -> dict:
    import json

    row = s.get(Setting, _turns_key())
    try:
        d = json.loads(row.value) if row and row.value else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def _turns_save(s, d: dict):
    import json

    s.merge(Setting(key=_turns_key(), value=json.dumps(d)))
    s.commit()


def searches_today() -> int:
    """Topic searches this account made today (on the project it spends from)."""
    me = str(tenant.current())  # (read before the budget session: inside it the code may run as the owner's copy)
    with SessionLocal() as s:
        return int((_turns_load(s).get(me) or {}).get("n") or 0)


def search_block() -> str:
    """'' when this account may make one topic search now, else why not (plain words)."""
    if not can_search():
        return "today's YouTube search budget is used or kept for uploads (it resets at 1:30 PM IST)"
    me, shared = str(tenant.current()), on_shared_budget()
    if shared and not shared_room(COST["search"] + COST["read"]):
        return ("today's YouTube budget of the shared Google project is kept for the owner's uploads "
                "(more tomorrow, or save your own Google keys)")
    with SessionLocal() as s:
        d = _turns_load(s)
    n = int((d.get(me) or {}).get("n") or 0)
    if shared and n >= SEARCHES_PER_ACCOUNT:
        return (f"this account's {SEARCHES_PER_ACCOUNT} topic searches a day on the shared Google project are used "
                "(more tomorrow, or save your own Google keys)")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for tid, o in d.items():
        if tid == me or not o.get("want"):
            continue
        try:
            waiting = now - datetime.fromisoformat(o["want"]) < TURN_WAIT
        except ValueError:
            waiting = False
        on = int(o.get("n") or 0)
        if waiting and on < n and not (o.get("shared") and on >= SEARCHES_PER_ACCOUNT):
            return "another account on the same Google project is waiting for its turn to search"
    return ""


def note_turn(searched: bool):
    """searched=True: this account used a turn (n + 1, no longer waiting). False: it asked and had to wait."""
    me = str(tenant.current())
    shared = on_shared_budget()
    with _lock, SessionLocal() as s:
        if s.get(Setting, _turns_key()) is None:  # the first turn of a new day: earlier days' rows are dropped
            s.query(Setting).filter(Setting.key.like("quota-turns:%"), Setting.key != _turns_key()).delete(
                synchronize_session=False)
        d = _turns_load(s)
        o = d.get(me) or {"n": 0}
        if searched:
            o["n"], o["want"] = int(o.get("n") or 0) + 1, None
        elif not (shared and int(o.get("n") or 0) >= SEARCHES_PER_ACCOUNT):  # a used-up account does not hold others up
            o["want"] = o.get("want") or datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        o["shared"] = shared
        d[me] = o
        _turns_save(s, d)


# ---------------------------------------------------------------- the owner's uploads come first on the owner's project
# A customer without Google keys of their own (or one the Super Admin lent the server's project to) spends from the
# OWNER'S daily budget. Their uploads, searches, thumbnails and other paid calls may only use what is left after every
# upload the owner still has due before the reset (the same count the first comment uses: comments._uploads_due_here,
# run in the owner's copy). The owner, and customers with their own keys, are not limited by this.
def owner_uploads_due() -> int:
    """The owner's uploads that still want today's budget (0 when it cannot be read: never a reason to stop the owner)."""
    try:
        from .comments import _uploads_due_here

        with tenant.use(tenant.OWNER):
            return int(_uploads_due_here())
    except Exception:  # noqa: BLE001  (unreadable: count a full day for the owner, so the customer waits)
        return capacity()


def shared_room(units: int) -> bool:
    """May the running account spend `units` from today's budget now? Always yes for the owner and for an account with
    its own Google keys. A customer on the owner's project: only when, after it, every upload the owner has due before
    the reset (and half the reserve) still fits."""
    if not on_shared_budget():
        return True
    left = DAILY_UNITS - used() - RESERVE // 2
    need = min(owner_uploads_due(), max(0, left // UPLOAD_COST)) * UPLOAD_COST
    return left - need >= units


def summary() -> dict:
    u = used()
    return {"used": u, "limit": DAILY_UNITS, "left": max(0, DAILY_UNITS - u),
            "uploads_left": max(0, (DAILY_UNITS - u - RESERVE // 2) // UPLOAD_COST), "capacity": capacity(),
            "resets_at": next_reset().isoformat() + "Z"}

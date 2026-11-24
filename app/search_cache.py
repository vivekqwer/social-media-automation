"""Topic search results, kept 24 hours and shared by every account.

A Creative Commons search for a phrase costs 100 units of a Google project's day, and many accounts search the same
ready-made niche phrases. The first account's search is kept here and every account (and every later run) takes its
videos from it; only when an account has used every video in it is the next page asked for (appended here).

What is kept is the public result list only: video id and title of what YouTube's public search shows for the
phrase. Nothing of any account (seen videos, channels, keys) is ever stored here. It lives in the owner's copy
(tenant 1) because it belongs to nobody in particular.
"""
import hashlib
import json
import threading
from datetime import datetime, timedelta

from . import tenant
from .db import Setting, SessionLocal, get_setting, set_setting, utcnow

HOURS = 24
MAX_PAGES = 6  # 50 results a page: 300 videos for one phrase is plenty for a day
PREFIX = "ytcc:"
_lock = threading.Lock()


def norm(phrase: str) -> str:
    return " ".join(str(phrase or "").lower().split())[:200]


def _key(phrase: str) -> str:
    return PREFIX + hashlib.sha1(norm(phrase).encode("utf-8")).hexdigest()[:32]


def get(phrase: str, now: datetime | None = None) -> dict | None:
    """{"at", "q", "items": [[video_id, title], ...], "token", "pages"} while younger than 24 hours, else None."""
    now = now or utcnow()
    try:
        with tenant.use(tenant.OWNER):
            d = json.loads(get_setting(_key(phrase)) or "")
        if norm(d.get("q")) != norm(phrase) or now - datetime.fromisoformat(d["at"]) >= timedelta(hours=HOURS):
            return None
        return {"at": d["at"], "q": d["q"], "items": [list(x)[:2] for x in d.get("items") or []],
                "token": d.get("token"), "pages": int(d.get("pages") or 0)}
    except Exception:  # noqa: BLE001
        return None


def add_page(phrase: str, items: list, token: str | None) -> dict:
    """Append one page of results (public id + title only) and return the updated entry."""
    clean = [[str(i), str(t or "")[:200]] for i, t in items if i]
    with _lock:
        cur = get(phrase)
        if cur is None:
            cur = {"at": utcnow().isoformat(), "q": norm(phrase), "items": [], "token": None, "pages": 0}
        have = {i for i, _t in cur["items"]}
        cur["items"] = cur["items"] + [x for x in clean if x[0] not in have]
        cur["token"], cur["pages"] = token or None, cur["pages"] + 1
        try:
            with tenant.use(tenant.OWNER):
                set_setting(_key(phrase), json.dumps(cur, ensure_ascii=False))
                _purge_daily()
        except Exception:  # noqa: BLE001  (a cache that cannot be written only costs a search later)
            pass
    return cur


def exhausted(entry: dict | None) -> bool:
    """Every page of this phrase is already known (or as many as are kept): searching again finds nothing new today."""
    return bool(entry) and (entry["pages"] >= MAX_PAGES or (entry["pages"] > 0 and not entry["token"]))


def _purge_daily():
    day = utcnow().strftime("%Y-%m-%d")
    if get_setting("ytcc_purged") == day:
        return
    set_setting("ytcc_purged", day)
    purge()


def purge(now: datetime | None = None) -> int:
    """Forget results older than 24 hours (owner's copy). Called once a day by the first cache write."""
    cut, n = (now or utcnow()) - timedelta(hours=HOURS), 0
    with tenant.use(tenant.OWNER):
        with SessionLocal() as s:
            for row in s.query(Setting).filter(Setting.key.like(PREFIX + "%")).all():
                try:
                    old = datetime.fromisoformat(json.loads(row.value)["at"]) < cut
                except Exception:  # noqa: BLE001
                    old = True
                if old:
                    s.delete(row)
                    n += 1
            s.commit()
    return n

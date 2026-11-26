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


def project_key(client_id: str) -> str:
    """Which Google project asked (a hash of its client id): YouTube's page tokens are only reused by that project."""
    return hashlib.sha1(str(client_id or "").encode("utf-8")).hexdigest()[:12]


def get(phrase: str, now: datetime | None = None) -> dict | None:
    """{"at", "q", "items": [[video_id, title], ...], "pages", "done", "tokens": {project: {"token", "page"}}} while
    younger than 24 hours, else None. pages = the most pages any project has read; done = YouTube has no more."""
    now = now or utcnow()
    try:
        with tenant.use(tenant.OWNER):
            d = json.loads(get_setting(_key(phrase)) or "")
        if norm(d.get("q")) != norm(phrase) or now - datetime.fromisoformat(d["at"]) >= timedelta(hours=HOURS):
            return None
        pages = int(d.get("pages") or 0)
        tokens = d.get("tokens") if isinstance(d.get("tokens"), dict) else {}  # (older rows: one token, project unknown)
        done = bool(d["done"]) if "done" in d else (pages > 0 and not d.get("token"))
        return {"at": d["at"], "q": d["q"], "items": [list(x)[:2] for x in d.get("items") or []],
                "pages": pages, "done": done, "tokens": tokens}
    except Exception:  # noqa: BLE001
        return None


def next_token(entry: dict | None, project: str) -> str | None:
    """The page token this project should ask with (None = page 1)."""
    return ((entry or {}).get("tokens") or {}).get(project, {}).get("token")


def add_page(phrase: str, items: list, token: str | None, project: str) -> dict:
    """Append one page of results (public id + title only) read by `project`, with that project's next-page token."""
    clean = [[str(i), str(t or "")[:200]] for i, t in items if i]
    with _lock:
        cur = get(phrase)
        if cur is None:
            cur = {"at": utcnow().isoformat(), "q": norm(phrase), "items": [], "pages": 0, "done": False, "tokens": {}}
        have = {i for i, _t in cur["items"]}
        cur["items"] = cur["items"] + [x for x in clean if x[0] not in have]
        page = int((cur["tokens"].get(project) or {}).get("page") or 0) + 1
        cur["tokens"][project] = {"token": token or None, "page": page}
        cur["pages"] = max(cur["pages"], page)
        cur["done"] = cur["done"] or not token  # the result list ends here (the same for every project)
        try:
            with tenant.use(tenant.OWNER):
                set_setting(_key(phrase), json.dumps(cur, ensure_ascii=False))
                _purge_daily()
        except Exception:  # noqa: BLE001  (a cache that cannot be written only costs a search later)
            pass
    return cur


def exhausted(entry: dict | None) -> bool:
    """Every page of this phrase is already known (or as many as are kept): searching again finds nothing new today."""
    return bool(entry) and (entry["pages"] >= MAX_PAGES or entry["done"])


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

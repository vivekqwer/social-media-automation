"""Instagram Reels + Facebook Page cross-posting (Meta Graph API, free).

When a Short is PUBLIC on YouTube (at once for a video that goes live on upload, at its publish time for a scheduled
one), the same video is posted as an Instagram Reel (Business/Creator account) and on the linked Facebook Page with
the same title / credit. Only Creative Commons, own and original content is cross-posted (a creator's permission may
cover YouTube only). Standalone: Meta errors never touch YouTube uploads.

How it runs (nothing waits in the single render worker any more):
  1. crosspost(cid): asks Instagram to fetch the video from a signed, short-lived link (/media/...) and posts it to the
     Facebook Page. Instagram needs a few minutes: its "container" is remembered in Clip.meta_result.
  2. post_due() (every 10 minutes, switch "meta.use"): publishes finished Instagram containers, starts the posts of
     Shorts that became public, and — when the server's copy of the video was already moved to the helper PC — asks the
     PC for it with the existing get-back job (archive: restore_wanted) and posts it when it is back.
  The server keeps the video until the post is done (archive.crosspost_pending), at most CROSSPOST_WAIT after the video
  became public.

Connect: the account's owner pastes a user token from Graph API Explorer. With their Meta app's id + secret (saved in
the Instagram + Facebook card; the owner's copy may use env META_APP_ID / META_APP_SECRET instead) the token is first
exchanged for a long-lived one, so the Page token does not expire. Every Page of the account is remembered (tokens encrypted); the owner picks the Page.
"""
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta

import httpx

from .crypto import decrypt, encrypt
from .db import Clip, Project, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v21.0"
CROSSPOST_MODES = {"cc", "original", "own"}
IG_WAIT = timedelta(hours=2)  # an Instagram container that is not ready by then has failed
PER_RUN = 3  # new posts started per run of post_due (Instagram allows 50 a day)
APP_KEY = "meta_app"  # owner setting: {"id": app id, "secret": encrypted app secret}
PERMISSIONS = ("pages_show_list", "pages_read_engagement", "pages_manage_posts", "business_management",
               "instagram_basic", "instagram_content_publish")


def _dec(raw: str | None) -> str:
    try:
        return decrypt(raw) if raw else ""
    except Exception:  # noqa: BLE001
        return ""


def _token() -> str:
    return _dec(get_setting("meta_token"))


def _cfg() -> dict:
    try:
        return json.loads(get_setting("meta_cfg") or "{}") or {}
    except ValueError:
        return {}


def _pages() -> dict:
    """{page_id: {"name", "token", "ig_id", "ig_username"}} — every Page the connected account manages."""
    try:
        return json.loads(_dec(get_setting("meta_pages")) or "{}") or {}
    except ValueError:
        return {}


# ---------- the Meta app (owner-provided; never typed by this program) ----------
def _saved_app() -> dict:
    try:
        return json.loads(get_setting(APP_KEY) or "{}") or {}
    except ValueError:
        return {}


def app_creds() -> tuple[str, str]:
    """(app id, app secret) of the Meta app this account made: its own saved values; the owner's copy may also use
    META_APP_ID / META_APP_SECRET from the server's settings. Each account needs its own app: an app in Development
    mode only works for the people who have a role on it."""
    saved = _saved_app()
    aid, sec = str(saved.get("id") or ""), _dec(saved.get("secret"))
    if aid and sec:
        return aid, sec
    from . import tenant

    if tenant.current() == tenant.OWNER:
        eid, esec = (os.getenv("META_APP_ID") or "").strip(), (os.getenv("META_APP_SECRET") or "").strip()
        if eid and esec:
            return eid, esec
    return aid, sec


def save_app(app_id: str, secret: str = "", clear: bool = False) -> dict:
    """The owner of the account pastes them (never typed by this program); the secret is kept encrypted."""
    if clear:
        set_setting(APP_KEY, "")
        return status()
    app_id = (app_id or "").strip()
    if not app_id.isdigit() or not 5 <= len(app_id) <= 30:
        raise ValueError("The App ID is a number (developers.facebook.com → your app → App settings → Basic).")
    secret = (secret or "").strip()
    if secret and not (16 <= len(secret) <= 64 and secret.isalnum()):
        raise ValueError("That does not look like an App secret (32 letters and numbers).")
    old = _saved_app()
    if not secret and not old.get("secret"):
        raise ValueError("Paste the App secret too (App settings → Basic → App secret → Show).")
    set_setting(APP_KEY, json.dumps({"id": app_id, "secret": encrypt(secret) if secret else old.get("secret")}))
    return status()


# ---------- connect ----------
def status() -> dict:
    cfg = _cfg()
    from .config import BASE_URL

    with SessionLocal() as s:
        waiting = s.query(Clip.meta_result).filter(Clip.meta_done.isnot(True), Clip.meta_result.isnot(None)).all()
    return {"connected": bool(_token() and cfg.get("page_id")), "page": cfg.get("page_name"), "page_id": cfg.get("page_id"),
            "instagram": cfg.get("ig_username"), "on": get_setting("meta_on") != "0",
            "facebook_on": cfg.get("facebook_on", True), "instagram_on": cfg.get("instagram_on", True),
            "long_lived": bool(cfg.get("long_lived")), "app_ready": all(app_creds()), "app_id": app_creds()[0],
            "error": get_setting("meta_error") or "",
            "public_link": BASE_URL.startswith("https://"), "pending": sum(1 for (r,) in waiting if _ig_pending(r)),
            "permissions": list(PERMISSIONS),
            "pages": [{"id": pid, "name": p.get("name"), "instagram": p.get("ig_username")} for pid, p in _pages().items()]}


def _long_lived(user_token: str) -> tuple[str, bool]:
    """Exchange a short user token (1 hour) for a 60-day one when the app's id + secret are known; Page tokens made
    from a long-lived user token do not expire."""
    aid, sec = app_creds()
    if not (aid and sec):
        return user_token, False
    r = httpx.get(f"{GRAPH}/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": aid,
                                                          "client_secret": sec, "fb_exchange_token": user_token}, timeout=30).json()
    if "error" in r or not r.get("access_token"):
        raise ValueError("Meta did not accept the App ID / App secret with this token: "
                         + ((r.get("error") or {}).get("message") or "no token returned")[:150])
    return r["access_token"], True


def setup(user_token: str, page_id: str | None = None) -> dict:
    """The owner pastes a token from Meta's Graph API Explorer; every Page and its Instagram account is found."""
    tok, long_lived = _long_lived(user_token.strip())
    r = httpx.get(f"{GRAPH}/me/accounts", params={"access_token": tok,
                                                   "fields": "id,name,access_token,instagram_business_account{id,username}"}, timeout=30).json()
    if "error" in r:
        raise ValueError("Meta did not accept this token: " + r["error"].get("message", "")[:150])
    pages = r.get("data") or []
    if not pages:
        raise ValueError("No Facebook Page found for this account. Create a Page and link your Instagram professional account to it.")
    found = {}
    for p in pages:
        ig = p.get("instagram_business_account") or {}
        found[str(p["id"])] = {"name": p.get("name"), "token": p.get("access_token"), "ig_id": ig.get("id"),
                               "ig_username": ig.get("username")}
    set_setting("meta_pages", encrypt(json.dumps(found)))
    pick = str(page_id) if page_id and str(page_id) in found else \
        next((pid for pid, p in found.items() if p["ig_id"]), next(iter(found)))
    set_setting("meta_on", "1")
    set_setting("meta_error", "")
    return choose_page(pick, long_lived=long_lived)


def choose_page(page_id: str, long_lived: bool | None = None) -> dict:
    p = _pages().get(str(page_id))
    if not p or not p.get("token"):
        raise ValueError("This Page is not one of the connected account's Pages. Connect again with a new token.")
    old = _cfg()
    set_setting("meta_token", encrypt(p["token"]))  # the Page token (does not expire when the user token was long-lived)
    set_setting("meta_cfg", json.dumps({"page_id": str(page_id), "page_name": p.get("name"), "ig_id": p.get("ig_id"),
                                        "ig_username": p.get("ig_username"), "facebook_on": old.get("facebook_on", True),
                                        "instagram_on": bool(p.get("ig_id")) and old.get("instagram_on", True),
                                        "long_lived": old.get("long_lived", False) if long_lived is None else long_lived}))
    return status()


def set_targets(facebook: bool | None = None, instagram: bool | None = None) -> dict:
    cfg = _cfg()
    if facebook is not None:
        cfg["facebook_on"] = bool(facebook)
    if instagram is not None:
        cfg["instagram_on"] = bool(instagram) and bool(cfg.get("ig_id"))
    set_setting("meta_cfg", json.dumps(cfg))
    return status()


def disconnect():
    for k in ("meta_token", "meta_cfg", "meta_pages", "meta_error"):
        set_setting(k, "")


# ---------- a short-lived public link to the video (Instagram fetches the file from a URL) ----------
def _sig(cid: int, exp: int, tid: int = 1) -> str:
    from .auth import _secret

    who = "" if tid == 1 else f"t{tid}:"  # the customer is part of what is signed: a link opens only that copy's clip
    return hmac.new(_secret(), f"media:{who}{cid}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def public_url(cid: int, base: str, minutes: int = 120) -> str:
    from . import tenant

    exp, tid = int(time.time()) + minutes * 60, tenant.current()
    return f"{base}/media/{cid}/{exp}/{_sig(cid, exp, tid)}.mp4" + ("" if tid == 1 else f"?t={tid}")


def check_link(cid: int, exp: int, sig: str, tid: int = 1) -> bool:
    return exp > time.time() and hmac.compare_digest(sig, _sig(cid, exp, tid))


def _caption(c: Clip) -> str:
    desc = (c.description or "").split("--- Original description ---")[0].strip()
    return f"{c.title}\n\n{desc}"[:2100]


# ---------- posting ----------
def public_since(c: Clip):
    """When the video became (or becomes) public on YouTube."""
    start = c.attempted_at or c.created_at or utcnow()
    return max(start, c.publish_at) if c.publish_at else start


def is_public(c: Clip, now=None) -> bool:
    return c.status == "uploaded" and bool(c.youtube_id) and not c.removed_at and public_since(c) <= (now or utcnow())


def _ig_pending(res) -> bool:
    res = res or {}
    return bool(res.get("ig_container")) and not res.get("instagram") and not res.get("instagram_error")


def _when(iso):
    try:
        return datetime.fromisoformat(str(iso).rstrip("Z"))
    except ValueError:
        return utcnow()


def _tell(text: str):
    try:
        from .telegram import notify

        notify(text)
    except Exception:  # noqa: BLE001
        pass


def _fail_token(msg: str):
    low = msg.lower()
    if "access token" in low or "session has expired" in low or "oauth" in low or "error validating" in low:
        if not get_setting("meta_error"):
            set_setting("meta_error", "Meta refused the login (" + msg[:120] + "). Connect Instagram + Facebook again.")
            _tell("⚠️ Instagram / Facebook: the Meta login stopped working. Open My channels → Instagram + Facebook and "
                  "connect again.")


def _save(cid: int, out: dict):
    done = bool(out.get("instagram") or out.get("facebook"))
    finished = not _ig_pending(out)
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        c.meta_result = out
        c.meta_done = done and finished
        s.commit()
        title = c.title or ""
    if finished:
        try:
            from .telegram import esc

            posted = [k for k in ("instagram", "facebook") if out.get(k)]
            errs = [f"{k.split('_')[0]}: {v}" for k, v in out.items() if k.endswith("_error")]
            if posted or errs:
                _tell(("📲 Also posted to " + " + ".join(posted) + f": <b>{esc(title)}</b>" if posted else "")
                      + ("\n⚠️ " + esc("; ".join(errs)) if errs else ""))
        except Exception:  # noqa: BLE001
            pass


def _publish_ig(cfg: dict, tok: str, out: dict) -> None:
    """Check the Instagram container once; publish it when it is ready."""
    cont = out["ig_container"]
    try:
        stt = httpx.get(f"{GRAPH}/{cont}", params={"fields": "status_code", "access_token": tok}, timeout=30).json()
        if "error" in stt:
            raise RuntimeError(stt["error"].get("message", "")[:200])
        code = stt.get("status_code")
        if code == "FINISHED":
            pub = httpx.post(f"{GRAPH}/{cfg['ig_id']}/media_publish", data={"creation_id": cont, "access_token": tok}, timeout=60).json()
            if "error" in pub:
                raise RuntimeError(pub["error"].get("message", "")[:200])
            out["instagram"] = pub["id"]
        elif code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video ({code})")
        elif utcnow() - _when(out.get("ig_at")) > IG_WAIT:
            raise RuntimeError("Instagram did not finish the video within 2 hours")
    except Exception as e:  # noqa: BLE001
        out["instagram_error"] = str(e)[:200]
        _fail_token(str(e))


def crosspost(cid: int) -> dict:
    """Post an uploaded, public Short to Instagram Reels and the Facebook Page. Returns what happened. Never waits
    for Instagram: an unfinished container is published by post_due."""
    from .config import BASE_URL

    st = status()
    if not (st["connected"] and st["on"]):
        return {"skipped": "not connected"}
    from . import acl, archive

    if not acl.allowed("meta.use"):  # "Instagram + Facebook" is switched off for this account
        return {"skipped": "switched off for this account"}
    cfg = _cfg()
    tok = _token()
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        p = s.get(Project, c.project_id) if c else None
        if not c or not p or c.aspect != "9:16" or p.mode not in CROSSPOST_MODES or c.meta_done or c.meta_result:
            return {"skipped": "not a Short / not allowed for this source / already done"}
        if not is_public(c):
            return {"skipped": "not public on YouTube yet"}
        if not archive.on_server(c):
            return {"skipped": "the video is on the PC only"}
        caption, url = _caption(c), public_url(cid, BASE_URL)
    out = {}
    if cfg.get("ig_id") and cfg.get("instagram_on", True):
        try:
            r = httpx.post(f"{GRAPH}/{cfg['ig_id']}/media", data={"media_type": "REELS", "video_url": url, "caption": caption,
                                                                   "share_to_feed": "true", "access_token": tok}, timeout=60).json()
            if "error" in r:
                raise RuntimeError(r["error"].get("message", "")[:200])
            out["ig_container"], out["ig_at"] = r["id"], utcnow().isoformat() + "Z"
            _publish_ig(cfg, tok, out)  # ready at once only in rare cases; else the 10-minute job publishes it
        except Exception as e:  # noqa: BLE001
            out["instagram_error"] = str(e)[:200]
            _fail_token(str(e))
    if cfg.get("page_id") and cfg.get("facebook_on", True):
        try:
            r = httpx.post(f"{GRAPH}/{cfg['page_id']}/videos", data={"file_url": url, "description": caption, "access_token": tok},
                           timeout=120).json()
            if "error" in r:
                raise RuntimeError(r["error"].get("message", "")[:200])
            out["facebook"] = r["id"]
        except Exception as e:  # noqa: BLE001
            out["facebook_error"] = str(e)[:200]
            _fail_token(str(e))
    if not out:
        out = {"skipped": "Facebook and Instagram are both switched off"}
    _save(cid, out)
    return out


def post_due() -> dict:
    """Every 10 minutes: finish Instagram posts, start the posts of Shorts that became public, get videos back from
    the PC when the server's copy is gone."""
    done = {"published": 0, "started": 0, "asked_pc": 0}
    st = status()
    if not (st["connected"] and st["on"]):
        return done
    from . import acl, archive

    if not acl.allowed("meta.use"):
        return done
    cfg, tok, now = _cfg(), _token(), utcnow()
    with SessionLocal() as s:
        pend = [(c.id, dict(c.meta_result or {})) for c in
                s.query(Clip).filter(Clip.meta_done.isnot(True), Clip.meta_result.isnot(None)).all() if _ig_pending(c.meta_result)]
    for cid, out in pend:
        _publish_ig(cfg, tok, out)
        if not _ig_pending(out):
            _save(cid, out)
            done["published"] += 1 if out.get("instagram") else 0
    with SessionLocal() as s:
        rows = s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.removed_at.is_(None),
                                    Clip.aspect == "9:16", Clip.meta_done.isnot(True), Clip.meta_result.is_(None)).all()
        ids = {c.project_id for c in rows}
        projects = {p.id: p for p in s.query(Project).filter(Project.id.in_(ids))} if ids else {}
        todo = []
        for c in rows:
            p = projects.get(c.project_id)
            if not p or p.mode not in CROSSPOST_MODES or not is_public(c, now) or public_since(c) < now - archive.CROSSPOST_WAIT:
                continue
            if archive.on_server(c):
                todo.append(c.id)
            elif c.archived_at and not c.restore_wanted:  # moved to the PC already: ask for it back (the get-back job)
                w, _why = archive.saver(s, c)
                if not (c.archived_by and w is None):  # the same rule as the Get back button (that helper still exists)
                    c.restore_wanted, c.archive_lease = now, None
                    c.archive_note = "Getting the video back from your PC for Instagram / Facebook."
                    done["asked_pc"] += 1
        s.commit()
    for cid in todo[:PER_RUN]:
        out = crosspost(cid)
        if not out.get("skipped"):
            done["started"] += 1
    return done

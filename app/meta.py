"""Instagram Reels + Facebook Page cross-posting (Meta Graph API, free).

After a Short goes up on YouTube, the same video is posted to the owner's Instagram (Business/Creator) and
Facebook Page with the same title/credit. Only Creative Commons, own and original content is cross-posted
(a creator's permission may cover YouTube only). Standalone: Meta errors never touch YouTube uploads.
"""
import hashlib
import hmac
import json
import logging
import time

import httpx

from .crypto import decrypt, encrypt
from .db import Clip, Project, SessionLocal, get_setting, set_setting

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v21.0"
CROSSPOST_MODES = {"cc", "original", "own"}


def _token() -> str:
    raw = get_setting("meta_token")
    try:
        return decrypt(raw) if raw else ""
    except Exception:  # noqa: BLE001
        return ""


def status() -> dict:
    cfg = json.loads(get_setting("meta_cfg") or "{}")
    return {"connected": bool(_token() and cfg.get("page_id")), "page": cfg.get("page_name"), "instagram": cfg.get("ig_username"),
            "on": get_setting("meta_on") != "0", "facebook_on": cfg.get("facebook_on", True), "instagram_on": cfg.get("instagram_on", True)}


def setup(user_token: str) -> dict:
    """Owner pastes a token from Meta's Graph API Explorer; we find their Page and its Instagram account."""
    r = httpx.get(f"{GRAPH}/me/accounts", params={"access_token": user_token.strip(),
                                                   "fields": "id,name,access_token,instagram_business_account{id,username}"}, timeout=30).json()
    if "error" in r:
        raise ValueError("Meta did not accept this token: " + r["error"].get("message", "")[:150])
    pages = r.get("data") or []
    if not pages:
        raise ValueError("No Facebook Page found for this account. Create a Page and link your Instagram professional account to it.")
    page = next((p for p in pages if p.get("instagram_business_account")), pages[0])
    ig = page.get("instagram_business_account") or {}
    set_setting("meta_token", encrypt(page["access_token"]))  # the Page token (long-lived when the user token was)
    set_setting("meta_cfg", json.dumps({"page_id": page["id"], "page_name": page.get("name"), "ig_id": ig.get("id"),
                                        "ig_username": ig.get("username"), "facebook_on": True, "instagram_on": bool(ig)}))
    set_setting("meta_on", "1")
    return status()


def disconnect():
    for k in ("meta_token", "meta_cfg"):
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


def crosspost(cid: int) -> dict:
    """Post an uploaded Short to Instagram Reels and the Facebook Page. Returns what happened."""
    from .config import BASE_URL

    st = status()
    if not (st["connected"] and st["on"]):
        return {"skipped": "not connected"}
    cfg = json.loads(get_setting("meta_cfg") or "{}")
    tok = _token()
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        p = s.get(Project, c.project_id)
        if c.aspect != "9:16" or p.mode not in CROSSPOST_MODES or c.meta_done:
            return {"skipped": "not a Short / not allowed for this source"}
        caption, url = _caption(c), public_url(cid, BASE_URL)
    out = {}
    if cfg.get("ig_id") and cfg.get("instagram_on", True):
        try:
            r = httpx.post(f"{GRAPH}/{cfg['ig_id']}/media", data={"media_type": "REELS", "video_url": url, "caption": caption,
                                                                   "share_to_feed": "true", "access_token": tok}, timeout=60).json()
            if "error" in r:
                raise RuntimeError(r["error"].get("message", "")[:200])
            cont = r["id"]
            for _ in range(40):  # Instagram downloads and processes the video
                time.sleep(15)
                stt = httpx.get(f"{GRAPH}/{cont}", params={"fields": "status_code", "access_token": tok}, timeout=30).json()
                if stt.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
                    break
            if stt.get("status_code") != "FINISHED":
                raise RuntimeError(f"Instagram could not process the video ({stt.get('status_code')})")
            pub = httpx.post(f"{GRAPH}/{cfg['ig_id']}/media_publish", data={"creation_id": cont, "access_token": tok}, timeout=60).json()
            if "error" in pub:
                raise RuntimeError(pub["error"].get("message", "")[:200])
            out["instagram"] = pub["id"]
        except Exception as e:  # noqa: BLE001
            out["instagram_error"] = str(e)[:200]
    if cfg.get("page_id") and cfg.get("facebook_on", True):
        try:
            r = httpx.post(f"{GRAPH}/{cfg['page_id']}/videos", data={"file_url": url, "description": caption, "access_token": tok},
                           timeout=120).json()
            if "error" in r:
                raise RuntimeError(r["error"].get("message", "")[:200])
            out["facebook"] = r["id"]
        except Exception as e:  # noqa: BLE001
            out["facebook_error"] = str(e)[:200]
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        c.meta_done = bool(out.get("instagram") or out.get("facebook"))
        c.meta_result = out
        s.commit()
    try:
        from .telegram import esc, notify

        done = [k for k in ("instagram", "facebook") if out.get(k)]
        errs = [f"{k.split('_')[0]}: {v}" for k, v in out.items() if k.endswith("_error")]
        if done or errs:
            notify(("📲 Also posted to " + " + ".join(done) + f": <b>{esc(caption.splitlines()[0])}</b>" if done else "")
                   + ("\n⚠️ " + esc("; ".join(errs)) if errs else ""))
    except Exception:  # noqa: BLE001
        pass
    return out

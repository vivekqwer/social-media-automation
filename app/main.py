import base64
import json
import os
import re
import secrets
import shutil
from urllib.parse import quote, urlparse
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from datetime import datetime
from pathlib import Path

from typing import Literal

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, update

from . import accounts, acl, auth, quota, scheduler, tenant, workers, youtube
from .autopilot import _next_slot, resume_after_analysis, run_channel
from .captions import fetch_cues
from .crypto import TokenError, decrypt, encrypt, encrypt_existing
from .config import APP_PASSWORD, APP_USER, BASE_URL, COOKIES_PATH, DATA_DIR, IS_LOCAL
from .monitor import check_uploads
from .db import (Alert, Base, Channel, Clip, Destination, Project, SessionLocal, Worker, engine, get_setting, migrate,
                 Setting, adopt_orphans, main_destination, set_setting, to_dict, utcnow)
from .hot import fetch_comments, fetch_heatmap, parse_timestamps, rank_moments
from .ingest import fetch_info, friendly_error, is_cc, video_id_from_url
from .jobs import executor
from .keywords import suggest
from .metadata import auto_metadata
from .metadata import build as build_meta
from .metadata import mark_hand_edit, propose as propose_meta
from .policy import check_edit
from .quick import quick_run
from .render import apply_short_cover, run_render
from .thumbs import make_thumbnail
from . import faces
from .tts import VOICES, synth

MODES = {"cc", "permission", "commentary"}
# public pages Google (OAuth consent / API audit) must be able to open without the dashboard password
PUBLIC_PATHS = {
    "/healthz", "/privacy", "/terms", "/about",
    "/login", "/forgot", "/reset", "/api/login", "/api/logout", "/api/reset-password", "/register", "/api/register",
    "/auth/reset/start", "/auth/callback",
}
SERVER_VERSION = os.getenv("RAILWAY_GIT_COMMIT_SHA") or ""  # the helper PC updates itself to match
_STATIC = Path(__file__).resolve().parent.parent / "static"
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
VOICE_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".aac"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
MAX_VIDEO_BYTES = 300 * 1024 * 1024
MAX_SHORT_SECONDS = 180
MAX_VOICE_BYTES = 25 * 1024 * 1024


def _check_yt_url(url: str):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in YT_HOSTS:
        raise HTTPException(400, "Only youtube.com / youtu.be URLs are allowed.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    migrate()
    accounts.init()
    encrypt_existing()
    sch = scheduler.start()
    yield
    sch.shutdown(wait=False)


app = FastAPI(title="Raji Studio", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    from .guide import record_error

    who = getattr(request.state, "who", None) or {}
    with tenant.use(who.get("tenant") or tenant.OWNER):
        record_error(f"{request.method} {request.url.path}", exc)
    return JSONResponse({"detail": "Something went wrong on the server. Try again in a minute. If it keeps happening, open "
                                   f"Setup guide → “If something goes wrong” and press Copy details. ({type(exc).__name__})"},
                        status_code=500)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # default handler echoes the raw input, which crashes on NaN/inf; return only location + message
    errs = [{"loc": [str(x) for x in e["loc"]], "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": errs}, status_code=422)


class LoginIn(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class ResetIn(BaseModel):
    password: str = Field(max_length=200)


class ChangePwIn(BaseModel):
    old_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):  # CSRF: only same-origin pages may send changes
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return PlainTextResponse("Cross-origin request blocked", status_code=403)
    if ".." in path:
        return PlainTextResponse("Not found", status_code=404)
    if path.startswith("/media/"):  # signed, short-lived video links for Instagram/Facebook (checked in the route)
        return _secure(await call_next(request))
    try:
        size = int(request.headers.get("content-length") or 0)
    except ValueError:
        size = 0
    if path.startswith("/api/worker/"):
        cap = 900 * 1024 * 1024 if path.startswith("/api/worker/source/") else 30 * 1024 * 1024 \
            if path.startswith(("/api/worker/voice/", "/api/worker/analysis/")) else 64 * 1024
        if size > cap:
            return JSONResponse({"detail": "Too large"}, status_code=413)
        tok = request.headers.get("x-worker-token", "")
        if path == "/api/worker/register":
            claim = request.headers.get("x-helper-claim", "")
            tid = workers.tenant_from_claim(claim) if claim else tenant.OWNER
            if tid is not None and tid != tenant.OWNER and tid not in accounts.active_tenants():
                tid = None  # the account is blocked or gone
            if tid is None:
                return JSONResponse({"detail": "This helper code is not valid. Copy it again from My channels → Helper PC."}, status_code=403)
        else:
            tid = workers.tenant_of(tok)
            if tid != tenant.OWNER and tid not in accounts.active_tenants():
                return JSONResponse({"detail": "Unknown helper"}, status_code=401)
        with tenant.use(tid):  # the helper sees and serves only its own customer's copy
            if path != "/api/worker/register":  # only approved helpers may send anything (checked before the body is read)
                found = workers.authenticate(tok)
                if not found:
                    return JSONResponse({"detail": "Unknown helper"}, status_code=401)
                if not found[1]:
                    return JSONResponse({"detail": "Waiting for approval in the dashboard"}, status_code=403)
            return _secure(await call_next(request))
    if path == "/api/upload-video" and request.method == "POST":
        if size > MAX_VIDEO_BYTES + 1024 * 1024:
            return JSONResponse({"detail": "Video too large (max 300 MB)"}, status_code=413)
    elif size > 50 * 1024 * 1024:  # the largest other dashboard upload is a 25 MB voice file
        return JSONResponse({"detail": "File too large"}, status_code=413)
    if path in PUBLIC_PATHS:
        who = auth.request_identity(request)
        if who:  # e.g. Google sends a signed-in person back to /auth/callback: it must run in THEIR copy
            request.state.who = who
            with tenant.use(who["tenant"]):
                if who["role"] == "super":
                    with acl.as_super():
                        return _secure(await call_next(request))
                return _secure(await call_next(request))
        return _secure(await call_next(request))
    if not auth.auth_configured():
        if not IS_LOCAL:
            return PlainTextResponse("Set the APP_PASSWORD env var before using this app.", status_code=503)
        return await call_next(request)
    who = auth.request_identity(request)
    if who:
        if path.startswith("/api/admin/") and who["role"] != "super":
            return JSONResponse({"detail": "Only the Super Admin can do this."}, status_code=403)
        request.state.who = who
        shown = request.headers.get("x-copy")
        if shown and shown != str(who["tenant"]) and path.startswith("/api/") and path != "/api/me":
            # e.g. "View as" ended or was started in another tab: this page still shows the other copy's rows,
            # and row numbers repeat between copies — never act on the wrong one
            return JSONResponse({"detail": "This page was showing another account's dashboard. It is being reloaded.",
                                 "reload": True}, status_code=409)
        real = request.scope.get("path") or path  # exactly what the router matches
        denied = acl.check_request(request.method, real, who)  # what the Super Admin switched off for this account
        if denied:
            return JSONResponse({"detail": denied}, status_code=403)
        with tenant.use(who["tenant"]):  # database and files of this person's private copy, for the whole request
            if who["role"] == "super":  # the Super Admin is never restricted, also while viewing a customer
                with acl.as_super():
                    resp = await call_next(request)
            else:
                resp = await call_next(request)
            acl.log_request(who, request.method, real, resp.status_code)
            if request.method != "GET":  # something changed: the Alerts number is counted afresh on the next poll
                _badge_cache.pop(who["tenant"], None)
            return _secure(resp)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not logged in"}, status_code=401)
    return RedirectResponse("/login?next=" + quote(path), status_code=302)


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob: https://i.ytimg.com; "
       "media-src 'self' blob:; frame-src https://www.youtube.com https://www.youtube-nocookie.com; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://accounts.google.com")


def _secure(resp):
    h = resp.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("X-Frame-Options", "DENY")
    if BASE_URL.startswith("https://"):
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


def _client_ip(request: Request) -> str:
    """The address Railway's proxy saw (the last X-Forwarded-For hop); earlier hops are set by the client."""
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "?")


def _set_cookie(resp, name: str, value: str, max_age: int):
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax", path="/",
                    secure=BASE_URL.startswith("https://"))


@app.get("/login")
def page_login(request: Request):
    if (not auth.auth_configured() and IS_LOCAL) or auth.request_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(_STATIC / "login.html")


@app.get("/forgot")
def page_forgot():
    return FileResponse(_STATIC / "forgot.html")


@app.get("/reset")
def page_reset(request: Request):
    if not auth.verify_reset_token(request.cookies.get(auth.RESET_COOKIE)):
        return RedirectResponse("/forgot?err=" + quote("Please verify with Google first."), status_code=302)
    return FileResponse(_STATIC / "reset.html")


@app.post("/api/login")
def api_login(body: LoginIn, request: Request):
    ip = _client_ip(request)
    customer = "@" in (body.username or "") and (body.username or "").strip() != APP_USER
    if auth.rate_limited(ip, "customer" if customer else "owner"):
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    if not auth.auth_configured():
        raise HTTPException(503, "No password is set on the server yet.")
    if customer:
        acc, problem = accounts.check_login(body.username, body.password)
        if not acc:
            if "Wrong" in problem:  # waiting / blocked are not guesses
                auth.note_fail(ip, "customer")
            raise HTTPException(403 if "approval" in problem or "blocked" in problem else 401, problem)
        resp = JSONResponse({"ok": True})
        _set_cookie(resp, auth.SESSION_COOKIE, accounts.make_session(acc), auth.SESSION_TTL)
        resp.delete_cookie(auth.AS_COOKIE, path="/")
        acl.record(acc.id, "user", "account", "Signed in", acc.tenant_id)
        return resp
    if not auth.check_login(body.username, body.password):
        auth.note_fail(ip)
        raise HTTPException(401, "Wrong username or password.")
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, auth.SESSION_COOKIE, auth.make_session(APP_USER), auth.SESSION_TTL)
    resp.delete_cookie(auth.AS_COOKIE, path="/")
    return resp


class RegisterIn(BaseModel):
    name: str = Field(max_length=120)
    brand: str = Field(max_length=60)
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)
    accept_terms: bool = False


@app.get("/register")
def page_register(request: Request):
    if auth.request_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(_STATIC / "register.html")


@app.post("/api/register")
def api_register(body: RegisterIn, request: Request):
    ip = _client_ip(request)
    if auth.rate_limited(ip, "register"):  # its own counter: registrations never touch anybody's sign-in attempts
        raise HTTPException(429, "Too many new accounts from this connection. Try again in an hour.")
    try:
        acc = accounts.register(body.email, body.name, body.brand, body.password, body.accept_terms)
    except ValueError as e:
        raise HTTPException(400, str(e))
    auth.note_fail(ip, "register")  # at most 3 accounts per connection in 10 minutes, 40 an hour in total
    from .telegram import esc

    _tell_owner(f"🆕 <b>New account waiting for approval</b>\n{esc(acc.name)} — {esc(acc.email)}\nSystem name: {esc(acc.brand)}\n"
                "Open Users in the dashboard to approve it.")
    return {"ok": True, "status": "pending"}


@app.get("/api/me")
def api_me(request: Request):
    """Who is signed in and what their plan allows (the dashboard shows the name and hides what is not theirs)."""
    who = dict(getattr(request.state, "who", None) or {"role": "super", "tenant": tenant.OWNER, "name": APP_USER})
    with SessionLocal() as s:
        who["channels"] = s.query(Destination).count()
    if who["role"] == "user":
        acc = accounts.get(who["account_id"])
        who["upgrade"] = acc.upgrade if acc else None
        who["access"] = acl.for_me(who)  # the dashboard hides what is switched off (the server refuses it anyway)
    who["pay"] = _pay_info() if who["role"] == "user" or who.get("viewing") else None
    if who["tenant"] != tenant.OWNER:
        from . import onboarding

        st = onboarding.state()
        who["onboarding"] = {"active": st["active"], "done": st["done"], "total": st["total"], "current": st["current"]}
    return who


@app.post("/api/logout")
def api_logout(request: Request):
    who = auth.request_identity(request)
    if who and who["role"] == "user":
        accounts.end_sessions(who["account_id"])
    elif who:
        auth.end_all_sessions()  # a copied cookie stops working too
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    resp.delete_cookie(auth.AS_COOKIE, path="/")
    return resp


@app.get("/auth/reset/start")
def reset_start():
    with tenant.use(tenant.OWNER):
        if not youtube.configured():
            return RedirectResponse("/forgot?err=" + quote("Google sign-in is not set up on this server."), status_code=302)
        if not auth.allowed_reset_emails():
            return RedirectResponse("/forgot?err=" + quote("No recovery account is registered yet. The hosting owner must set ADMIN_EMAIL."), status_code=302)
        return RedirectResponse(youtube.reset_url())


@app.post("/api/reset-password")
def api_reset_password(body: ResetIn, request: Request):
    if not auth.verify_reset_token(request.cookies.get(auth.RESET_COOKIE)):
        raise HTTPException(401, "Verification expired. Verify with Google again.")
    err = auth.validate_new_password(body.password)
    if err:
        raise HTTPException(400, err)
    auth.set_password(body.password)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.RESET_COOKIE, path="/")
    _set_cookie(resp, auth.SESSION_COOKIE, auth.make_session(APP_USER), auth.SESSION_TTL)
    return resp


@app.post("/api/change-password")
def api_change_password(body: ChangePwIn, request: Request):
    who = getattr(request.state, "who", None) or {}
    if who.get("viewing"):
        raise HTTPException(400, "You are viewing a customer's dashboard. Leave “View as” to change your own password.")
    if who.get("role") == "user":
        acc = accounts.get(who["account_id"])
        if not acc or not auth._verify_hash(body.old_password, acc.pw_hash):
            raise HTTPException(400, "Current password is wrong.")
        err = auth.validate_new_password(body.new_password)
        if err:
            raise HTTPException(400, err)
        accounts.set_password(acc.id, body.new_password)
        resp = JSONResponse({"ok": True})
        _set_cookie(resp, auth.SESSION_COOKIE, accounts.make_session(accounts.get(acc.id)), auth.SESSION_TTL)
        return resp
    if not auth.check_login(APP_USER, body.old_password):
        raise HTTPException(400, "Current password is wrong.")
    err = auth.validate_new_password(body.new_password)
    if err:
        raise HTTPException(400, err)
    auth.set_password(body.new_password)
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, auth.SESSION_COOKIE, auth.make_session(APP_USER), auth.SESSION_TTL)
    return resp


# ---------- accounts: what a customer can do about their own plan ----------
def _owner_setting(key: str) -> str | None:
    with tenant.use(tenant.OWNER):
        return get_setting(key)


def _pay_info() -> dict:
    """How to pay for more channels (set by the Super Admin, shown to customers)."""
    try:
        return json.loads(_owner_setting("pay_info") or "{}")
    except ValueError:
        return {}


def _tell_owner(text: str):
    try:
        from .telegram import notify

        with tenant.use(tenant.OWNER):
            notify(text)
    except Exception:  # noqa: BLE001
        pass


class UpgradeIn(BaseModel):
    channels: int = Field(ge=2, le=50)
    message: str = Field("", max_length=500)


class BrandIn(BaseModel):
    brand: str = Field(min_length=2, max_length=60)


@app.post("/api/account/upgrade")
def account_upgrade(body: UpgradeIn, request: Request):
    """A customer asks for more channels (after paying the way the Super Admin describes)."""
    who = getattr(request.state, "who", None) or {}
    if who.get("role") != "user":
        raise HTTPException(400, "Only customer accounts have a plan.")
    if body.channels <= (who.get("channel_limit") or 1):
        raise HTTPException(400, f"Your plan already allows {who.get('channel_limit') or 1} channel(s).")
    req = {"channels": body.channels, "message": accounts.clean_brand(body.message)[:500], "at": utcnow().isoformat() + "Z"}
    acc = accounts.update(who["account_id"], upgrade=req)
    from .telegram import esc

    _tell_owner(f"💳 <b>{esc(acc['name'])}</b> ({esc(acc['email'])}) asks for {body.channels} channels.\n"
                f"{esc(req['message'])}\nOpen Users in the dashboard to allow it.")
    return {"ok": True, "upgrade": req}


@app.post("/api/account/brand")
def account_brand(body: BrandIn, request: Request):
    """A customer renames their system (the name in their dashboard)."""
    who = getattr(request.state, "who", None) or {}
    if who.get("role") != "user":
        raise HTTPException(400, "The Super Admin's system name is set in the project.")
    try:
        return accounts.update(who["account_id"], brand=body.brand)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/account/helper-code")
def account_helper_code():
    """The code that attaches a new helper PC to this account's copy (the owner's helpers need none)."""
    tid = tenant.current()
    return {"code": None if tid == tenant.OWNER else workers.claim_code(tid)}


# ---------- Super Admin: users, limits, payments ----------
def _usage(tid: int) -> dict:
    from .db import tenant_exists

    if not tenant_exists(tid):  # deleted (or not made yet): looking at it must not create an empty copy again
        return {"error": "No data (deleted or not set up)."}
    try:
        with tenant.use(tid):
            with SessionLocal() as s:
                from . import onboarding

                ob = onboarding.state()
                return {"setup": f"{ob['done']}/{ob['total']}" if ob["total"] else "", "setup_done": not ob["active"],
                        "own_google": bool(get_setting("google_client_id")), "shared_google": get_setting("shared_google") == "1",
                        "channels": s.query(Destination).count(), "sources": s.query(Channel).count(),
                        "clips": s.query(Clip).count(), "uploaded": s.query(Clip).filter(Clip.status == "uploaded").count(),
                        "helper": _helper_state(s)["online"]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:100]}


class UserEdit(BaseModel):
    status: Literal["active", "blocked"] | None = None
    channel_limit: int | None = Field(None, ge=1, le=50)
    plan: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=2000)
    brand: str | None = Field(None, max_length=60)
    close_request: bool = False


class PayIn(BaseModel):
    upi: str = Field("", max_length=100)
    price: str = Field("", max_length=100)
    text: str = Field("", max_length=1000)


@app.get("/api/admin/users")
def admin_users():
    rows = accounts.listing()
    for r in rows:
        r["usage"] = _usage(r["tenant_id"]) if r["status"] != "pending" and r["tenant_id"] else None
        eff = acl.effective(r["id"])  # one line about the account's access; the whole panel is /api/admin/users/{id}/access
        r["acl"] = {"plan": eff["plan"], "controls": eff["controls"], "off": sum(1 for v in eff["perms"].values() if not v)} if eff else None
    return {"users": rows, "pay": _pay_info(), "owner": _usage(tenant.OWNER)}


@app.post("/api/admin/users/{aid}/approve")
def admin_approve(aid: int):
    try:
        out = accounts.approve(aid)
    except KeyError:
        raise HTTPException(404, "Account not found")
    acl.record(aid, "super", "admin.account", "Approved the account", out.get("tenant_id"))
    return out


@app.post("/api/admin/users/{aid}")
def admin_edit(aid: int, body: UserEdit):
    data = {k: v for k, v in body.model_dump(exclude={"close_request"}).items() if v is not None}
    acc = accounts.get(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if data.get("status") == "active" and not acc.tenant_id:
        raise HTTPException(400, "Approve the account first.")
    if body.close_request or (data.get("channel_limit") and acc.upgrade and data["channel_limit"] >= int(acc.upgrade.get("channels") or 0)):
        data["upgrade"] = None  # the request is answered
    try:
        out = accounts.update(aid, **data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if data.get("status") and data["status"] != acc.status:
        acl.record(aid, "super", "admin.account", "Blocked the account (signed out at once)" if data["status"] == "blocked"
                   else "Unblocked the account", acc.tenant_id)
    if data.get("channel_limit") and data["channel_limit"] != (acc.channel_limit or 1):
        acl.record(aid, "super", "admin.switch", f"YouTube channels: {acc.channel_limit or 1} → {data['channel_limit']}", acc.tenant_id)
    if body.close_request:
        acl.record(aid, "super", "admin.account", "Closed the request for more channels", acc.tenant_id)
    return out


@app.post("/api/admin/users/{aid}/reset-password")
def admin_reset_password(aid: int):
    acc = accounts.get(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    acl.record(aid, "super", "admin.account", "Gave a temporary password (old sign-ins ended)", acc.tenant_id)
    return {"password": accounts.reset_password(aid)}  # shown once; the customer changes it after signing in


@app.delete("/api/admin/users/{aid}")
def admin_delete(aid: int):
    acc = accounts.get(aid)
    try:
        accounts.delete(aid)
    except KeyError:
        raise HTTPException(404, "Account not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    acl.record(aid, "super", "admin.account", f"Removed the login of {acc.email if acc else aid}", acc.tenant_id if acc else None)
    return {"deleted": True}


@app.post("/api/admin/users/{aid}/view")
def admin_view_as(aid: int):
    """Open this customer's dashboard (to help them). Nothing they own is changed by looking."""
    acc = accounts.get(aid)
    if not acc or not acc.tenant_id:
        raise HTTPException(400, "This account has no dashboard yet (approve it first).")
    resp = JSONResponse({"ok": True, "viewing": acc.name})
    _set_cookie(resp, auth.AS_COOKIE, auth.make_view_as(aid), auth.AS_TTL)
    acl.record(aid, "super", "admin.view", "Opened the customer's dashboard (View as)", acc.tenant_id)
    return resp


@app.post("/api/admin/view/exit")
def admin_view_exit():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.AS_COOKIE, path="/")
    return resp


class SharedGoogleIn(BaseModel):
    on: bool


@app.post("/api/admin/users/{aid}/shared-google")
def admin_shared_google(aid: int, body: SharedGoogleIn):
    """Let this customer use the server's own Google project (they then share the owner's daily upload allowance)."""
    acc = accounts.get(aid)
    if not acc or not acc.tenant_id:
        raise HTTPException(400, "Approve the account first.")
    with tenant.use(acc.tenant_id):
        set_setting("shared_google", "1" if body.on else "")
    return {"shared_google": body.on}


@app.get("/api/admin/pay")
def admin_pay_get():
    return _pay_info()


@app.post("/api/admin/pay")
def admin_pay_set(body: PayIn):
    with tenant.use(tenant.OWNER):
        set_setting("pay_info", json.dumps({"upi": body.upi.strip(), "price": body.price.strip(), "text": body.text.strip()}))
    return _pay_info()


@app.get("/api/admin/selftest")
def admin_selftest():
    """Check on the live database that customers' copies are really separate."""
    with tenant.use(tenant.OWNER):
        return accounts.self_test()


# ---------- Super Admin: access control (switches, limits, plans, direct controls, activity log) ----------
# The rules themselves are in app/acl.py; these are only the buttons of the Users page.
class AccessIn(BaseModel):
    perms: dict[str, bool | None] = Field(default_factory=dict)  # None = follow the plan again
    limits: dict[str, int | None] = Field(default_factory=dict)  # None or 0 = no limit, -1 = follow the plan again


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    perms: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, int | None] = Field(default_factory=dict)
    apply: bool = True  # accounts on this plan follow the change (False: they keep what they have now)


class DefaultIn(BaseModel):
    perms: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, int | None] = Field(default_factory=dict)
    new_plan: str | None = Field(None, max_length=40)  # the plan a NEW account starts on ("" = none)
    apply: bool = True


class UserPlanIn(BaseModel):
    plan: str = Field("", max_length=40)


class ControlIn(BaseModel):
    action: Literal["pause_uploads", "resume_uploads", "stop_sources", "start_sources", "sign_out"]


class DeleteDataIn(BaseModel):
    email: str = Field(max_length=200)


def _acl_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except KeyError:
        raise HTTPException(404, "Not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/admin/acl")
def admin_acl():
    """Every switch and limit there is, the plans, and what accounts without a plan get."""
    return acl.overview()


@app.post("/api/admin/acl/default")
def admin_acl_default(body: DefaultIn):
    return {**_acl_call(acl.save_default, body.perms, body.limits, body.new_plan, body.apply), **acl.overview()}


@app.post("/api/admin/plans")
def admin_plan_save(body: PlanIn):
    return {**_acl_call(acl.save_plan, body.name, body.perms, body.limits, body.apply), **acl.overview()}


@app.delete("/api/admin/plans/{name}")
def admin_plan_delete(name: str):
    return {**_acl_call(acl.delete_plan, name), **acl.overview()}


@app.get("/api/admin/activity")
def admin_activity(account_id: int | None = None, actor: str = Query("", max_length=10), q: str = Query("", max_length=60),
                   limit: int = Query(100, ge=1, le=500), before: int | None = None):
    """What customers did and what was changed for them, newest first."""
    return {"rows": acl.activity(account_id, actor, q, limit, before)}


@app.get("/api/admin/users/{aid}/access")
def admin_access_get(aid: int, fresh: int = 0):
    acc = accounts.get(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if fresh and acc.tenant_id:
        acl.storage_bytes(acc.tenant_id, fresh=True)  # "Measure again"
    return acl.public_access(aid, with_usage=True)


@app.post("/api/admin/users/{aid}/access")
def admin_access_set(aid: int, body: AccessIn):
    """Switches and limits of one account. They apply to the very next thing the customer does."""
    changed = _acl_call(acl.set_access, aid, body.perms, body.limits)
    return {**acl.public_access(aid), "changed": changed}


@app.post("/api/admin/users/{aid}/plan")
def admin_access_plan(aid: int, body: UserPlanIn):
    return _acl_call(acl.apply_plan, aid, body.plan)


@app.post("/api/admin/users/{aid}/control")
def admin_access_control(aid: int, body: ControlIn):
    """Pause / resume all uploads, stop / start all sources, or sign the customer out — at once."""
    acc = accounts.get(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if body.action == "sign_out":
        accounts.end_sessions(aid)
        acl.record(aid, "super", "admin.control", "Signed the customer out everywhere", acc.tenant_id)
        return {"ok": True, "controls": acl.public_access(aid)["controls"]}
    name, on = {"pause_uploads": ("uploads_paused", True), "resume_uploads": ("uploads_paused", False),
                "stop_sources": ("sources_stopped", True), "start_sources": ("sources_stopped", False)}[body.action]
    return {"ok": True, "controls": _acl_call(acl.set_control, aid, name, on)}


@app.delete("/api/admin/users/{aid}/channels/{did}")
def admin_disconnect_channel(aid: int, did: int):
    """Disconnect one of a customer's YouTube channels. Their sources and waiting clips are kept."""
    acc = accounts.get(aid)
    if not acc or not acc.tenant_id:
        raise HTTPException(404, "Account not found")
    with tenant.use(acc.tenant_id):
        with SessionLocal() as s:
            d = s.get(Destination, did)
            if not d:
                raise HTTPException(404, "This channel is not connected any more.")
            name = d.name
        out = delete_destination(did, None, True)
    acl.record(aid, "super", "admin.control", f"Disconnected the YouTube channel “{name}”", acc.tenant_id)
    return out


@app.post("/api/admin/users/{aid}/delete-data")
def admin_delete_data(aid: int, body: DeleteDataIn):
    """Delete a customer's whole private copy (database part + files). Needs the account's e-mail typed out.
    The account is blocked and signed out first. The owner's own copy can never be deleted here."""
    return _acl_call(acl.delete_account_data, aid, body.email)


@app.get("/privacy")
def page_privacy():
    return FileResponse(_STATIC / "privacy.html")


@app.get("/terms")
def page_terms():
    return FileResponse(_STATIC / "terms.html")


@app.get("/about")
def page_about():
    return FileResponse(_STATIC / "about.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- schemas ----------
class ProjectIn(BaseModel):
    url: str
    mode: str = "cc"
    permission_note: str = ""


class ClipIn(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)


class Overlay(BaseModel):
    text: str = Field("", max_length=500)
    start: float = Field(0, ge=0, le=36000, allow_inf_nan=False)
    end: float = Field(3, ge=0, le=36000, allow_inf_nan=False)
    pos: Literal["top", "center", "bottom"] = "bottom"
    anim: Literal["fade", "slide_up", "none"] = "fade"
    size: int = Field(56, ge=16, le=200)
    color: str = Field("#ffffff", max_length=20)


class Cue(BaseModel):
    s: float = Field(ge=0, le=36000, allow_inf_nan=False)
    e: float = Field(ge=0, le=36000, allow_inf_nan=False)
    t: str = Field("", max_length=200)


class CaptionStyle(BaseModel):
    on: bool = False
    lang: str = Field("auto", max_length=12)
    pos: Literal["top", "center", "bottom"] = "bottom"
    size: Literal["S", "M", "L"] = "M"
    color: str = Field("#ffffff", max_length=20)


class CaptionsIn(BaseModel):
    lang: str = Field("auto", max_length=12)


class ClipUpdate(BaseModel):
    start: float | None = Field(None, ge=0, allow_inf_nan=False)
    end: float | None = Field(None, gt=0, allow_inf_nan=False)
    aspect: Literal["16:9", "9:16", "1:1"] | None = None
    overlays: list[Overlay] | None = Field(None, max_length=30)
    effects: list[Literal["fade", "punch_in", "shake", "bw"]] | None = None
    voice_text: str | None = Field(None, max_length=5000)
    voice_offset: float | None = Field(None, ge=0, le=600, allow_inf_nan=False)
    orig_volume: float | None = Field(None, ge=0, le=3, allow_inf_nan=False)
    title: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=5000)
    tags: list[str] | None = Field(None, max_length=60)
    captions: list[Cue] | None = Field(None, max_length=400)
    caption_style: CaptionStyle | None = None
    credit_on: bool | None = None
    short_cover: bool | None = None
    reframe: bool | None = None
    fit: Literal["full", "wide", "fill"] | None = None
    voice_fit: Literal["", "speed"] | None = None
    frame_title: bool | None = None
    cta_on: bool | None = None


class TtsIn(BaseModel):
    text: str
    voice: str = "hi-IN-MadhurNeural"


class MetaIn(BaseModel):
    extra_keywords: list[str] = []


class ScheduleIn(BaseModel):
    publish_at: str | None = None  # ISO datetime; null = publish now
    destination_id: int | None = None  # which of my channels to upload to


class ChannelIn(BaseModel):
    kind: Literal["channel", "topic", "reel", "trending", "viral"] = "channel"
    interval_hours: int | None = Field(None, ge=1, le=48)
    auto_scripts: bool = False  # reel source: scripts written by the helper PC's local AI on trending topics
    language: Literal["hi", "en"] = "hi"
    scripts: list[str] = Field(default_factory=list, max_length=500)
    voice: str = Field("", max_length=60)
    query: str = Field(min_length=1, max_length=300)
    mode: str = "cc"
    permission_note: str = Field("", max_length=2000)
    auto: bool = True
    videos_per_run: int = Field(3, ge=1, le=20)
    clips_per_video: int = Field(2, ge=1, le=10)
    clip_len: int = Field(30, ge=10, le=120)
    aspect: Literal["16:9", "9:16", "1:1"] = "9:16"
    gap_hours: int = Field(12, ge=1, le=720)
    destination_id: int | None = None
    review_first: bool = False
    captions: bool = False
    post_times: str = Field("", max_length=100)
    tz_offset: int = Field(0, ge=-900, le=900)


TIMES_RE = re.compile(r"^\s*([01]?\d|2[0-3]):[0-5]\d(\s*,\s*([01]?\d|2[0-3]):[0-5]\d){0,11}\s*$")


class ChannelEdit(BaseModel):
    """What can be changed on a source after it was added (only the fields that are sent change)."""
    name: str | None = Field(None, min_length=1, max_length=300)
    videos_per_run: int | None = Field(None, ge=1, le=20)
    clips_per_video: int | None = Field(None, ge=1, le=10)
    clip_len: int | None = Field(None, ge=10, le=120)
    aspect: Literal["16:9", "9:16", "1:1"] | None = None
    post_times: str | None = Field(None, max_length=100)
    tz_offset: int | None = Field(None, ge=-900, le=900)
    interval_hours: int | None = Field(None, ge=1, le=48)
    captions: bool | None = None
    review_first: bool | None = None
    destination_id: int | None = None
    voice: str | None = Field(None, max_length=60)


class CookiesIn(BaseModel):
    text: str = Field(max_length=400_000)


class ApproveIn(BaseModel):
    publish_at: str | None = None
    destination_id: int | None = None


def _get(s, model, id_):
    row = s.get(model, id_)
    if not row:
        raise HTTPException(404, f"{model.__name__} not found")
    return row


# ---------- projects ----------
def _check_permission_note(note: str):
    """Permission mode needs real proof (who allowed it, how, when) — not just the word 'permission'."""
    n = (note or "").strip()
    if len(n) < 20 or n.lower() in {"permission", "i have permission", "yes", "allowed", "ok"}:
        raise HTTPException(400, "Write how you got the creator's permission — who, how and when "
                                 "(e.g. 'Email from @channel on 20 Nov 2026 allowing reuse of this video').")


@app.post("/api/projects")
def create_project(body: ProjectIn):
    if body.mode not in MODES:
        raise HTTPException(400, "mode must be cc, permission or commentary")
    if body.mode == "permission":
        _check_permission_note(body.permission_note)
    _check_yt_url(body.url)
    try:
        info = fetch_info(body.url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read video: {friendly_error(e)}")
    if body.mode == "cc" and not is_cc(info["license"]):
        raise HTTPException(409, {"code": "not_cc", "license": info["license"] or "Standard YouTube License",
                                  "title": info["title"], "channel": info["channel"]})
    with SessionLocal() as s:
        p = Project(url=body.url, mode=body.mode, permission_note=body.permission_note, **info)
        s.add(p)
        s.commit()
        return to_dict(p)


@app.get("/api/projects")
def list_projects():
    with SessionLocal() as s:
        return [to_dict(p) for p in s.query(Project).order_by(Project.id.desc()).limit(50)]


@app.get("/api/projects/{pid}")
def get_project(pid: int):
    with SessionLocal() as s:
        p = to_dict(_get(s, Project, pid))
        p["clips"] = [to_dict(c) for c in s.query(Clip).filter(Clip.project_id == pid).order_by(Clip.id)]
        return p


@app.post("/api/projects/{pid}/analyze")
def analyze(pid: int, clip_len: int = Query(30, ge=5, le=600)):
    with SessionLocal() as s:
        p = _get(s, Project, pid)
        if not p.heatmap:
            p.heatmap = fetch_heatmap(p.video_id)
        secs = parse_timestamps(fetch_comments(p.video_id), p.duration)
        p.moments = rank_moments(p.duration, p.heatmap, secs, clip_len)
        s.commit()
        return {"moments": p.moments, "comment_timestamps": len(secs), "has_heatmap": bool(p.heatmap)}


@app.post("/api/projects/{pid}/clips")
def create_clip(pid: int, body: ClipIn):
    acl.need_clip_room()  # the account's clips-a-day and storage limits
    with SessionLocal() as s:
        p = _get(s, Project, pid)
        if body.end <= body.start or body.end > (p.duration or 10**9) + 1:
            raise HTTPException(400, "Invalid start/end")
        c = Clip(project_id=pid, start=body.start, end=body.end, aspect="9:16", effects=["fade"], overlays=[])
        s.add(c)
        s.commit()
        cid = c.id
    try:  # credit, original tags/description and fresh keywords, filled in straight away
        auto_metadata(cid)
    except Exception:  # noqa: BLE001  (keyword lookup offline) -> user can press Auto-fill later
        pass
    with SessionLocal() as s:
        return to_dict(s.get(Clip, cid))


# ---------- clips ----------
@app.get("/api/clips/{cid}")
def get_clip(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        return _clip_card(c, {c.project_id: s.get(Project, c.project_id)}, full=True)


def _render_sig(c: Clip) -> tuple:
    """What the rendered video actually depends on, with defaults filled in (None == off == default)."""
    r2 = lambda x, d=0.0: round(float(x if x is not None else d), 2)  # noqa: E731
    ovs = tuple((str(o.get("text", "")).strip(), r2(o.get("start")), r2(o.get("end"), 3), o.get("pos") or "bottom",
                 o.get("anim") or "fade", int(o.get("size") or 56), str(o.get("color") or "#ffffff").lower())
                for o in (c.overlays or []) if str(o.get("text", "")).strip())
    st = c.caption_style or {}
    caps = (tuple((r2(x.get("s")), r2(x.get("e")), str(x.get("t", "")).strip()) for x in (c.captions or [])),
            st.get("pos") or "bottom", st.get("size") or "M", str(st.get("color") or "#ffffff").lower()) if st.get("on") else None
    return (r2(c.start), r2(c.end), c.aspect, ovs, tuple(sorted(c.effects or [])), r2(c.voice_offset),
            r2(c.orig_volume, 1.0), caps, c.credit_on is not False, c.aspect == "9:16" and c.short_cover is not False,
            c.reframe is not False, c.fit or "full", c.voice_fit or "", c.aspect != "16:9" and c.frame_title is not False,
            c.cta_on is not False)


@app.put("/api/clips/{cid}")
def update_clip(cid: int, body: ClipUpdate):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("rendering", "fetching", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}; wait or create a new clip.")
        before = _render_sig(c)
        for k, v in body.model_dump(exclude_unset=True).items():
            if getattr(c, k) != v:
                mark_hand_edit(c, k, v)  # title/description/tags changed by hand: the keyword helper leaves them alone
                setattr(c, k, v)
        if (c.end or 0) <= (c.start or 0):
            raise HTTPException(400, "end must be greater than start")
        changed = _render_sig(c) != before  # only real changes to the video need a new render
        if changed and c.status in ("rendered", "queued", "failed"):
            c.status = "draft"
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/tts")
def make_tts(cid: int, body: TtsIn):
    if not body.text.strip():
        raise HTTPException(400, "Text is empty")
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        folder = DATA_DIR / "clips" / str(cid)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "voice.mp3"
        try:
            synth(body.text, body.voice, str(path))
        except Exception as e:  # noqa: BLE001
            if workers.any_recent():  # voice service refuses the server's IP: let the helper PC make it
                c.voice_text, c.tts_pending = body.text, json.dumps({"text": body.text, "voice": body.voice})
                s.commit()
                return to_dict(c)
            raise HTTPException(502, "The voice service refused this server. Start the Helper PC (My channels) "
                                     f"or upload an audio file. ({str(e)[:80]})")
        c.voice_path, c.voice_text, c.tts_pending = str(path), body.text, None
        c.status = "draft" if c.status in ("rendered", "queued", "failed") else c.status
        s.commit()
        return to_dict(c)


# ---------- sound sync: the voice-over and the clip end together ----------
class SyncIn(BaseModel):
    mode: Literal["clip", "speed", "none"]


def _sync_info(c: Clip, p: Project) -> dict:
    from .ingest import stream_seconds
    from .render import TEMPO_MAX, TEMPO_MIN, voice_tempo

    has = bool(c.voice_path) and Path(c.voice_path).exists()
    voice = round(stream_seconds(Path(c.voice_path), "a:0"), 2) if has else 0.0
    clip_len, off = round((c.end or 0) - (c.start or 0), 2), float(c.voice_offset or 0)
    if p.mode == "original":
        from .render import own_source

        src = own_source(c.id)
        longest = round(stream_seconds(src, "v:0") - float(c.start or 0), 2) if src else clip_len
    else:
        longest = float(p.duration or 0) - float(c.start or 0) if p.duration else clip_len  # length unknown: keep the clip
    longest = max(longest, 2.0)
    if c.aspect == "9:16":
        longest = min(longest, MAX_SHORT_SECONDS)
    gap = round(clip_len - off - voice, 2)  # > 0: the clip goes on after the voice ends; < 0: the voice is cut off
    need = voice / max(1.0, clip_len - off) if has else 1.0
    return {"has_voice": has, "voice": voice, "clip": clip_len, "offset": off, "gap": gap if has else 0.0,
            "in_sync": has and (abs(gap) <= 0.4 or (c.voice_fit == "speed" and TEMPO_MIN <= need <= TEMPO_MAX)),
            "voice_fit": c.voice_fit or "", "speed": round(voice_tempo(voice, clip_len, off), 3) if has else 1.0,
            "speed_ok": TEMPO_MIN <= need <= TEMPO_MAX, "longest_clip": round(max(0.0, longest), 2),
            "can_match_clip": has and off + voice + 0.25 <= longest + 0.5}


@app.get("/api/clips/{cid}/sync")
def clip_sync(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        return _sync_info(c, _get(s, Project, c.project_id))


@app.post("/api/clips/{cid}/sync")
def clip_sync_set(cid: int, body: SyncIn):
    """One press: make the clip exactly as long as the voice, or fit the voice's speed to the clip."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        p = _get(s, Project, c.project_id)
        if c.status in ("rendering", "fetching", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}; wait or create a new clip.")
        info = _sync_info(c, p)
        if body.mode != "none" and not info["has_voice"]:
            raise HTTPException(400, "Add a voice-over first (generate it or upload an audio file).")
        before = _render_sig(c)
        if body.mode == "clip":
            c.end = round((c.start or 0) + min(info["offset"] + info["voice"] + 0.25, info["longest_clip"]), 2)
            c.voice_fit = ""
        elif body.mode == "speed":
            if not info["speed_ok"]:
                raise HTTPException(400, "The voice and the clip are too different in length to fix with speed "
                                         "(it would sound wrong). Make the clip as long as the voice instead.")
            c.voice_fit = "speed"
        else:
            c.voice_fit = ""
        if _render_sig(c) != before and c.status in ("rendered", "queued", "failed"):
            c.status = "draft"
        s.commit()
        return {"sync": _sync_info(c, p), "clip": to_dict(c)}


# ---------- my own video file (no YouTube link): becomes a clip with every editing tool ----------
@app.post("/api/upload-video")
def upload_video(file: UploadFile = File(...), title: str = Form("")):
    from .ingest import stream_seconds, video_size

    ext = Path(file.filename or "video.mp4").suffix.lower()
    if ext not in VIDEO_EXT:
        raise HTTPException(400, "Upload an MP4 or MOV video file.")
    acl.need_clip_room()
    name = re.sub(r"\s+", " ", (title or Path(file.filename or "My video").stem).replace("_", " ")).strip()[:150] or "My video"
    with SessionLocal() as s:
        dest = main_destination(s)
        p = Project(url="", video_id=None, title=name, channel=dest.name if dest else "", channel_url="",
                    license="Original content (my own video)", description="", tags=[], duration=0, heatmap=[], moments=[],
                    mode="original")
        s.add(p)
        s.commit()
        c = Clip(project_id=p.id, start=0, end=2, aspect="9:16", effects=[], overlays=[], credit_on=False,
                 destination_id=dest.id if dest else None, status="draft")
        s.add(c)
        s.commit()
        src = DATA_DIR / "clips" / str(c.id) / "src" / "upload.mp4"
        try:
            _save_upload(file, src, MAX_VIDEO_BYTES)
            size, dur = video_size(src), stream_seconds(src, "v:0")
            if not size or dur < 2:
                raise HTTPException(400, "This file has no usable video (it must be at least 2 seconds long).")
        except Exception as e:  # noqa: BLE001  (bad file, full disk, ...): leave no half-made clip behind
            shutil.rmtree(src.parent.parent, ignore_errors=True)
            s.delete(c)
            s.delete(p)
            s.commit()
            if isinstance(e, HTTPException):
                raise
            raise HTTPException(400, "The video could not be saved or read. Check that it plays on your computer and try again.")
        wide = size[0] > size[1] * 1.15
        # a wide or long video is uploaded as a normal video (whole picture); a tall, short one as a Short
        c.aspect = "16:9" if wide or dur > MAX_SHORT_SECONDS else "9:16"
        c.end, p.duration = round(dur, 2), int(dur)
        s.commit()
        cid = c.id
    executor.submit(run_render, cid)
    return {"clip_id": cid, "seconds": round(dur, 1), "aspect": c.aspect, "width": size[0], "height": size[1]}


@app.post("/api/clips/{cid}/captions")
def make_captions(cid: int, body: CaptionsIn):
    """Pull the video's own YouTube captions for this clip's time range (editable afterwards)."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("rendering", "fetching", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}.")
        p = _get(s, Project, c.project_id)
        if not p.video_id:  # my own video / reel: there are no YouTube subtitles to fetch
            raise HTTPException(400, "This is your own video, so YouTube has no subtitles for it. "
                                     "Type the captions with “Add line”, or leave captions off.")
        try:
            cues = fetch_cues(p.video_id, c.start, c.end, body.lang)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)[:300])
        style = dict(c.caption_style or {"pos": "bottom", "size": "M", "color": "#ffffff"})
        style.update({"on": True, "lang": body.lang})
        c.captions, c.caption_style = cues, style
        if c.status in ("rendered", "queued", "failed"):
            c.status = "draft"
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/voice-upload")
def voice_upload(cid: int, file: UploadFile = File(...)):
    acl.need_storage()
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        folder = DATA_DIR / "clips" / str(cid)
        folder.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename or "v.mp3").suffix.lower() or ".mp3"
        if ext not in VOICE_EXT:
            raise HTTPException(400, f"Voice file must be one of {sorted(VOICE_EXT)}")
        path = folder / ("voice" + ext)
        size = 0
        with open(path, "wb") as f:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_VOICE_BYTES:
                    f.close()
                    path.unlink(missing_ok=True)
                    raise HTTPException(413, "Voice file too large (max 25MB)")
                f.write(chunk)
        c.voice_path = str(path)
        c.status = "draft" if c.status in ("rendered", "queued", "failed") else c.status
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/render")
def render(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("rendering", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}.")
        p = _get(s, Project, c.project_id)
        errs = check_edit(p, c)
        if errs:
            raise HTTPException(400, " ".join(errs))
        _claim_for_render(s, cid)
    executor.submit(run_render, cid)
    return {"status": "rendering"}


def _claim_for_render(s, cid: int):
    """Atomically mark a clip as rendering (a fresh start also resets the helper's download attempts)."""
    res = s.execute(update(Clip).where(Clip.id == cid, Clip.status.notin_(["rendering", "uploading", "uploaded"]))
                    .values(status="rendering", error=None, fetch_tries=0, job_lease=None, fetch_since=None))
    s.commit()
    if res.rowcount != 1:
        raise HTTPException(409, "The clip changed meanwhile (it may be uploading). Reload and try again.")


@app.post("/api/clips/{cid}/refetch")
def refetch_source(cid: int):
    """Throw away the downloaded source section and fetch it again (e.g. a bad or low-quality download)."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("rendering", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}.")
        if (DATA_DIR / "clips" / str(cid) / "src" / "upload.mp4").exists():
            raise HTTPException(400, "This is your own uploaded video — there is nothing to download again. "
                                     "To replace it, upload the new file with “Upload my video”.")
        shutil.rmtree(DATA_DIR / "clips" / str(cid) / "src", ignore_errors=True)
        _claim_for_render(s, cid)
    executor.submit(run_render, cid)
    return {"status": "rendering"}


@app.get("/api/clips/{cid}/video")
def clip_video(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if not c.output_path or not Path(c.output_path).exists():
            raise HTTPException(404, "Not rendered yet")
        return FileResponse(c.output_path, media_type="video/mp4")


# ---------- keyword helper: the most-typed search phrases, put into title, description and tags ----------
class ClipKeywordsIn(BaseModel):
    extra_keywords: list[str] = Field(default_factory=list, max_length=20)  # the owner's own picks: they always lead
    topic: str | None = Field(None, max_length=120)  # the owner's own topic words (None = keep the saved ones)


def _keyword_clip(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("uploading", "uploaded"):
            raise HTTPException(409, "This video is already on YouTube, so its title, description and tags "
                                     "can no longer be changed from here.")


@app.post("/api/clips/{cid}/metadata")
def clip_metadata(cid: int, body: ClipKeywordsIn):
    """'Use these': research the keywords, write title, description and tags from them and save."""
    _keyword_clip(cid)
    return auto_metadata(cid, [k.strip()[:100] for k in body.extra_keywords if k.strip()], hint=body.topic)


@app.post("/api/keywords/clip/{cid}")
def clip_keywords_preview(cid: int, body: ClipKeywordsIn):
    """'Find better keywords': research now and show what would be written. Nothing is saved."""
    _keyword_clip(cid)
    return propose_meta(cid, [k.strip()[:100] for k in body.extra_keywords if k.strip()], hint=body.topic, fresh=True)


_kw_calls: dict[int, list[float]] = {}  # customer -> times of their recent research requests


def _kw_rate_limited(tid: int, per_hour: int = 40) -> bool:
    import time as _t

    now = _t.time()
    recent = [x for x in _kw_calls.get(tid, []) if now - x < 3600]
    if tid != tenant.OWNER and len(recent) >= per_hour:
        _kw_calls[tid] = recent
        return True
    _kw_calls[tid] = recent + [now]
    return False


@app.get("/api/keywords")
def keywords(q: str = Query("", max_length=200)):
    """Keyword research for a topic: the phrases people type into YouTube search, most typed first, in groups.
    Free (YouTube's own search suggestions, saved for a day); no search-volume numbers exist, so none are shown."""
    from .keywords import research

    q = " ".join(q.split())
    if len(q) < 2:
        raise HTTPException(400, "Type a topic first, for example: street food india")
    if _kw_rate_limited(tenant.current()):
        raise HTTPException(429, "That is a lot of research in a short time. Please wait a few minutes and try again.")
    r = research(q)
    if r["net"] == "offline":
        raise HTTPException(502, "YouTube's search suggestions could not be reached just now. Please try again in a few minutes.")
    r["note"] = ("YouTube could not be reached just now — these are the results saved earlier." if r["net"] == "stale"
                 else "" if r["keywords"] else "Nobody seems to type this. Try fewer or simpler words.")
    return r


@app.get("/api/voices")
def voices():
    return VOICES


@app.post("/api/clips/{cid}/schedule")
def schedule(cid: int, body: ScheduleIn):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        p = _get(s, Project, c.project_id)
        if c.status not in ("rendered", "queued"):
            raise HTTPException(409, f"Clip is '{c.status}': render it first (edits invalidate the render).")
        if not c.output_path or not Path(c.output_path).exists():
            raise HTTPException(400, "Render the clip first")
        errs = check_edit(p, c)
        if errs:
            raise HTTPException(400, " ".join(errs))
        if not c.title or not c.description:  # same automatic credit/tags/keywords as Approve
            meta = build_meta(p, c, [])
            c.title, c.description, c.tags = meta["title"], meta["description"], meta["tags"]
        if body.destination_id:
            _get(s, Destination, body.destination_id)
            c.destination_id = body.destination_id
        if not c.destination_id or not s.get(Destination, c.destination_id):
            first = main_destination(s)
            if not first:
                raise HTTPException(400, "Connect your YouTube channel first (dashboard).")
            c.destination_id = first.id
        if body.publish_at:
            c.publish_at = _future_time(body.publish_at)
        else:
            c.publish_at = None
        c.status, c.error, c.autoqueue = "queued", None, True
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/unschedule")
def unschedule(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status == "queued":
            c.status, c.autoqueue = "rendered", False
            s.commit()
        return to_dict(c)


@app.get("/api/queue")
def queue():
    with SessionLocal() as s:
        rows = s.query(Clip).filter(Clip.status.in_(["queued", "uploading", "uploaded", "failed"])).order_by(Clip.id.desc()).limit(50)
        return [to_dict(c) for c in rows]


def _dest_dict(d: Destination) -> dict:
    out = to_dict(d)
    try:
        out["needs_reconnect"] = not youtube.token_client_ok(json.loads(decrypt(d.token)))  # e.g. keys changed / sharing ended
    except ValueError:
        out["needs_reconnect"] = False
    except TokenError:
        out["needs_reconnect"] = True  # stored login unreadable (e.g. key changed): press Connect again
    out["face"] = "own" if faces.face_path(d.id).exists() else ("default" if faces.DEFAULT_FACE.exists() else None)
    return out


def _dests_main_first(s) -> list:
    main = main_destination(s)
    rows = [{**_dest_dict(d), "is_main": bool(main and main.id == d.id)} for d in s.query(Destination).order_by(Destination.id)]
    return sorted(rows, key=lambda d: not d["is_main"])


def _dest_full(s, d: Destination) -> dict:
    """A channel with what depends on it (for My channels)."""
    out = _dest_dict(d)
    main = main_destination(s)
    out["is_main"] = bool(main and main.id == d.id)
    out["sources"] = s.query(Channel).filter(Channel.destination_id == d.id).count()
    out["pending"] = s.query(Clip).filter(Clip.destination_id == d.id, Clip.status.notin_(["uploaded", "uploading"])).count()
    out["uploaded"] = s.query(Clip).filter(Clip.destination_id == d.id, Clip.status == "uploaded").count()
    return out


def _move_work(s, from_ids: list[int], to_id: int) -> dict:
    """Sources and not-yet-uploaded clips of some channels go to another channel (uploaded videos stay where they are)."""
    n_src = s.query(Channel).filter(Channel.destination_id.in_(from_ids)).update({"destination_id": to_id}, synchronize_session=False)
    n_clip = s.query(Clip).filter(Clip.destination_id.in_(from_ids), Clip.status.notin_(["uploaded", "uploading"])) \
        .update({"destination_id": to_id}, synchronize_session=False)
    s.commit()
    return {"sources": n_src, "clips": n_clip}


def _clip_card(c: Clip, projects: dict, full: bool = False) -> dict:
    p = projects.get(c.project_id)
    d = to_dict(c)
    if not full:
        d.pop("description", None)
        d.pop("kw", None)  # the chosen keywords are only needed in the editor
    d["video_id"] = p.video_id if p else None
    d["source_title"] = p.title if p else None
    d["source_channel"] = p.channel if p else None
    d["source_channel_url"] = p.channel_url if p else None
    d["mode"] = p.mode if p else None
    d["license"] = p.license if p else None
    d["duration"] = p.duration if p else None
    d["source_id"] = p.channel_ref if p else None
    d["own_upload"] = bool(p and p.mode == "original" and (p.license or "").endswith("(my own video)"))
    return d


def _cards(s, rows) -> list[dict]:
    ids = {c.project_id for c in rows}
    projects = {p.id: p for p in s.query(Project).filter(Project.id.in_(ids)).all()} if ids else {}
    return [_clip_card(c, projects) for c in rows]


@app.get("/api/clips")
def list_clips(status: str | None = None, limit: int = Query(60, ge=1, le=200)):
    with SessionLocal() as s:
        q = s.query(Clip).filter(Clip.removed_at.is_(None))
        if status:
            q = q.filter(Clip.status.in_(status.split(",")))
        return _cards(s, q.order_by(Clip.id.desc()).limit(limit).all())


@app.get("/api/overview")
def overview():
    badge = _alerts_badge()  # before the session below is opened: it reads (and may tidy) on its own
    with SessionLocal() as s:
        statuses = ("draft", "fetching", "rendering", "rendered", "queued", "uploading", "uploaded", "failed")
        counts = {k: s.query(Clip).filter(Clip.status == k, Clip.removed_at.is_(None)).count() for k in statuses}
        upcoming = s.query(Clip).filter(Clip.status.in_(["queued", "uploading"])).order_by(Clip.publish_at).limit(8).all()
        recent = s.query(Clip).filter(Clip.status == "uploaded", Clip.removed_at.is_(None)).order_by(Clip.id.desc()).limit(6).all()
        failed = s.query(Clip).filter(Clip.status == "failed").order_by(Clip.id.desc()).limit(5).all()
        review = s.query(Clip).filter(Clip.status == "rendered").order_by(Clip.id.desc()).limit(6).all()
        return {
            "counts": counts,
            "disk": _disk_info(),
            "sources": s.query(Channel).count(),
            "videos_analyzed": s.query(Project).count(),
            "destinations": _dests_main_first(s),
            "ai_sources": [c.id for c in s.query(Channel.id).filter(Channel.kind == "reel", Channel.auto_scripts.is_(True))],
            "upcoming": _cards(s, upcoming),
            "recent": _cards(s, recent),
            "failed": _cards(s, failed),
            "review": _cards(s, review),
            "youtube_configured": youtube.configured(),
            "alerts_unseen": (n_alerts := s.query(Alert).filter(Alert.seen.isnot(True), Alert.kind != "restored").count()),
            "alerts_badge": n_alerts if badge is None else badge,  # sidebar: new serious items (deleted + strikes + not working)
            "quota": quota.summary(),
            "uploads_today": _uploads_today(s),
            "helper": _helper_state(s),
            "thumbs_pending": s.query(Clip).filter(Clip.thumb_pending.is_(True), Clip.status == "uploaded").count(),
        }


def _disk_info() -> dict | None:
    """Free space on the data volume (shared by all copies), so a big batch of clips never fills it up."""
    try:
        from .config import BASE_DATA_DIR

        u = shutil.disk_usage(BASE_DATA_DIR)
        return {"total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1), "used_pct": round(100 * u.used / u.total)}
    except OSError:
        return None


_badge_cache: dict[int, tuple[float, int | None]] = {}  # customer -> (when counted, number)


def _alerts_badge() -> int | None:
    import time as _t

    tid = tenant.current()
    hit = _badge_cache.get(tid)
    if hit and _t.time() - hit[0] < 60:  # the dashboard asks every 30 s; the full count is heavy
        return hit[1]
    try:
        n = _alerts_board(record=False)["badge"]  # the Alerts page itself writes problems down
    except Exception:  # noqa: BLE001  (the dashboard must open even if the alert list cannot be made)
        n = None
    _badge_cache[tid] = (_t.time(), n)
    return n


def _uploads_today(s) -> dict:
    since = scheduler._pacific_midnight(utcnow())
    return {str(did): n for did, n in s.query(Clip.destination_id, func.count()).filter(Clip.attempted_at >= since)
            .group_by(Clip.destination_id)}


def _helper_state(s) -> dict:
    """Is a helper PC online, and is work waiting for it?"""
    rows = s.query(Worker).filter(Worker.approved.is_(True)).all()
    ws = [workers.as_dict(w) for w in rows]
    waiting = (s.query(Clip).filter(Clip.status == "fetching").count()
               + s.query(Clip).filter(Clip.tts_pending.isnot(None), Clip.tts_pending != "").count()
               + s.query(Project).filter(Project.analysis_pending.is_(True)).count())
    online = sum(1 for w in ws if w["online"])
    if not online:
        # a helper in the middle of a long job (slow download, captions on the PC) does not call in until it is done:
        # it took a job a short while ago and was seen within the time a job may take -> it is working, not offline
        now = utcnow()
        busy = s.query(Clip).filter(Clip.job_lease.isnot(None), Clip.job_lease > now - JOB_LEASE).count()
        if busy and any(w.last_seen and w.last_seen > now - JOB_LEASE for w in rows):
            online = 1
    return {"approved": len(ws), "online": online, "waiting": waiting,
            "last_seen": max((w["last_seen"] for w in ws if w["last_seen"]), default=None)}


@app.post("/api/destinations/{did}/pause")
def pause_destination(did: int):
    with SessionLocal() as s:
        d = _get(s, Destination, did)
        d.paused, d.paused_at, d.paused_reason = True, utcnow(), "Paused by you"
        s.commit()
        return _dest_dict(d)


@app.post("/api/destinations/{did}/resume")
def resume_destination(did: int):
    with SessionLocal() as s:
        d = _get(s, Destination, did)
        d.paused, d.paused_reason = False, None
        s.commit()
        return _dest_dict(d)


# ---------- alerts: my uploaded videos deleted / rejected / blocked on YouTube ----------
@app.get("/api/alerts")
def list_alerts():
    with SessionLocal() as s:
        rows = s.query(Alert).order_by(Alert.id.desc()).limit(200).all()
        last = s.query(Clip.yt_checked_at).filter(Clip.yt_checked_at.isnot(None)).order_by(Clip.yt_checked_at.desc()).first()
        watching = s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None)).count()
        return {"alerts": [to_dict(a) for a in rows], "watching": watching,
                "last_check": (last[0].isoformat() + "Z") if last else None}


@app.post("/api/alerts/seen")
def alerts_seen():
    with SessionLocal() as s:
        s.query(Alert).filter(Alert.seen.is_(False)).update({"seen": True})
        s.commit()
    return {"ok": True}


@app.post("/api/alerts/check")
def alerts_check():
    return check_uploads()


def _alerts_board(record: bool = True) -> dict:
    """Every problem, sorted into the tabs of the Alerts page (see alerts.py). Never fails: a part that cannot be
    read is left out and written down under System errors."""
    from . import alerts, guide

    try:
        items = health()["items"]
    except Exception as e:  # noqa: BLE001
        if record:
            guide.record_error("Alerts: checking the system", e)
        items = None
    return alerts.board(items)


@app.get("/api/alerts/board")
def alerts_board():
    return _alerts_board()


class AlertSeenIn(BaseModel):
    tab: Literal["all", "deleted", "strikes", "broken", "system"] = "all"
    id: str | None = Field(None, max_length=200)  # one item; empty = everything in the tab


@app.post("/api/alerts/board/seen")
def alerts_board_seen(body: AlertSeenIn):
    """Dismiss one item or mark a whole tab as seen. Nothing is deleted: the history stays, only the 'new' mark goes."""
    from . import alerts

    n = alerts.mark_seen(_alerts_board(), body.tab, body.id)
    return {"ok": True, "marked": n, "badge": _alerts_board()["badge"]}


def _future_time(iso: str) -> datetime:
    """A publish time from the dashboard: must be at least 15 minutes ahead (else YouTube publishes at once)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, OverflowError):
        raise HTTPException(400, "Pick a valid date and time")
    if dt < utcnow() + timedelta(minutes=15):
        raise HTTPException(400, "That time is in the past or less than 15 minutes away — the video would go public "
                                 "immediately. Pick a later time, or clear the field to use the next free slot.")
    return dt


class ReuploadIn(BaseModel):
    confirm: bool = False


NO_REUPLOAD = ("rejected:copyright", "rejected:claim", "rejected:trademark", "rejected:legal")


GONE_STATES = ("removed", "deleted", "rejected", "private")  # "blocked:<n>" = blocked in some countries, still live


def _gone(c: Clip) -> bool:
    """The video is no longer (publicly) on YouTube — only then may it be uploaded again or removed here."""
    st = (c.yt_state or "").split(":", 1)[0]
    return c.status == "uploaded" and st in GONE_STATES


def _answer_alerts(s, clip_id: int):
    """The owner dealt with this clip (uploaded again / removed): its alerts are marked seen, the same in every view."""
    s.query(Alert).filter(Alert.clip_id == clip_id, Alert.seen.isnot(True)).update({"seen": True}, synchronize_session=False)


@app.post("/api/clips/{cid}/remove")
def remove_gone_clip(cid: int):
    """A video that was deleted from YouTube: take it off the dashboard lists. Its history stays in Alerts."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if not _gone(c):
            raise HTTPException(409, "Only a video that is no longer on YouTube can be removed here. To take down a live video, delete it in YouTube Studio first.")
        c.removed_at = utcnow()
        _answer_alerts(s, cid)
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/restore")
def restore_removed_clip(cid: int):
    """Undo "Remove": the clip shows in the lists again."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        c.removed_at = None
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/reupload")
def reupload_clip(cid: int, body: ReuploadIn):
    """Copy an uploaded clip (all edits, voice, captions, title, thumbnail) into a new clip that can be uploaded again."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status != "uploaded":
            raise HTTPException(409, "Only uploaded clips can be uploaded again — this one can still be edited.")
        if c.reuploaded_as and s.get(Clip, c.reuploaded_as):
            raise HTTPException(409, f"This video was already uploaded again as clip #{c.reuploaded_as}. Open that one instead.")
        if (c.yt_state or "") in NO_REUPLOAD or (c.yt_state or "").startswith("blocked"):
            raise HTTPException(409, "YouTube removed or blocked this video for copyright. Uploading the same content again "
                                     "can bring another strike (3 strikes close the channel), so it is blocked.")
        if not body.confirm:
            raise HTTPException(400, "Confirm first")
        acl.need_clip_room()
        new = Clip(**{k: getattr(c, k) for k in (
            "project_id", "start", "end", "aspect", "overlays", "effects", "voice_text", "voice_offset", "orig_volume",
            "title", "description", "tags", "destination_id", "captions", "caption_style", "thumb_text", "thumb_face",
            "credit_on", "fit", "voice_fit", "reframe", "short_cover", "frame_title", "cta_on")}, status="draft")
        s.add(new)
        s.commit()
        c.reuploaded_as = new.id
        _answer_alerts(s, c.id)
        s.commit()
        old_dir, new_dir = DATA_DIR / "clips" / str(c.id), DATA_DIR / "clips" / str(new.id)
        new_dir.mkdir(parents=True, exist_ok=True)
        if (old_dir / "src").exists():  # reuse the downloaded source: no new download needed
            shutil.copytree(old_dir / "src", new_dir / "src", dirs_exist_ok=True)
        if c.voice_path and Path(c.voice_path).exists():
            new.voice_path = str(shutil.copy(c.voice_path, new_dir / Path(c.voice_path).name))
        if c.thumb_custom and c.thumb_path and Path(c.thumb_path).exists():
            new.thumb_path, new.thumb_custom = str(shutil.copy(c.thumb_path, new_dir / Path(c.thumb_path).name)), True
        s.commit()
        nid = new.id
    executor.submit(run_render, nid)
    return {"id": nid}


@app.post("/api/clips/{cid}/approve")
def approve_clip(cid: int, body: ApproveIn):
    """Review-first flow: queue a rendered clip for upload (next free slot unless a time is given)."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        p = _get(s, Project, c.project_id)
        if c.status not in ("rendered", "queued", "failed"):
            raise HTTPException(409, f"Clip is '{c.status}'; render it first.")
        errs = check_edit(p, c)
        if errs:
            raise HTTPException(400, " ".join(errs))
        if not c.output_path or not Path(c.output_path).exists():
            raise HTTPException(400, "Render the clip first")
        if not c.title or not c.description:
            meta = build_meta(p, c, [])
            c.title, c.description, c.tags = meta["title"], meta["description"], meta["tags"]
        if body.destination_id:
            _get(s, Destination, body.destination_id)
            c.destination_id = body.destination_id
        if not c.destination_id or not s.get(Destination, c.destination_id):
            first = main_destination(s)
            if not first:
                raise HTTPException(400, "Connect your YouTube channel first.")
            c.destination_id = first.id
        if body.publish_at:
            c.publish_at = _future_time(body.publish_at)
        elif c.status == "queued" and c.publish_at and c.publish_at > utcnow():
            pass  # Re-queue without a new time keeps the clip's existing time
        else:
            ch = s.get(Channel, p.channel_ref) if p.channel_ref else None
            c.publish_at = _next_slot(s, ch.gap_hours if ch else 12, c.destination_id,
                                      ch.post_times if ch else None, (ch.tz_offset or 0) if ch else 0)
        c.autoqueue, c.status, c.error = True, "queued", None
        s.commit()
        return to_dict(c)


@app.delete("/api/clips/{cid}")
def discard_clip(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("uploading", "uploaded"):
            raise HTTPException(409, "Already uploaded; delete it on YouTube instead.")
        shutil.rmtree(DATA_DIR / "clips" / str(cid), ignore_errors=True)
        s.delete(c)
        s.commit()
        return {"deleted": True}


# ---------- autopilot channels ----------
@app.post("/api/channels")
def add_channel(body: ChannelIn):
    _need_step("source")
    acl.need(acl.SOURCE_KIND.get(body.kind, "source.channel"))  # each kind of source has its own switch
    if body.kind == "reel" and body.auto_scripts:
        acl.need("ai.reels")
    if body.kind != "viral":
        acl.need_source_room()  # the account's limit on sources
    if body.kind in ("topic", "reel", "trending"):  # topics are always CC; reels are my own content
        body.mode, body.permission_note = "cc", ""
    if body.mode not in ("cc", "permission", "own"):
        raise HTTPException(400, "Autopilot supports cc, permission or own-channel mode (commentary needs manual edit).")
    if body.mode == "permission":
        _check_permission_note(body.permission_note)
    body.query = body.query.strip()
    if not body.query:
        raise HTTPException(400, "Channel name/URL is empty")
    if body.kind == "reel" and body.auto_scripts:
        body.scripts = body.scripts or []
    elif body.kind == "trending":  # query = YouTube category id ("" = all, "26" = Howto & Style ...)
        if not re.fullmatch(r"\d{0,3}|all", body.query):
            raise HTTPException(400, "Pick a trending category")
    elif body.kind == "reel":  # my own scripts -> original reels (no copyright risk)
        if not any(t.strip() for t in body.scripts) and not body.auto_scripts:
            raise HTTPException(400, "Add at least one script.")
    elif body.kind == "topic":
        body.mode, body.permission_note = "cc", ""  # topics only ever use Creative Commons videos
        if body.query.startswith("http"):
            raise HTTPException(400, "A topic is words like 'street food india', not a link")
    elif body.query.startswith("http"):
        _check_yt_url(body.query)
        if video_id_from_url(body.query):
            raise HTTPException(400, "That is a single video link, not a channel. Use Quick upload or New clip for one "
                                     "video; add a channel link (youtube.com/@name) or a topic as a source.")
    with SessionLocal() as s:
        if body.destination_id:
            _get(s, Destination, body.destination_id)
        else:
            first = main_destination(s)
            if not first:
                raise HTTPException(400, "Connect your own YouTube channel first (dashboard), then add sources.")
            body.destination_id = first.id
        data = body.model_dump()
        language = data.pop("language")
        scripts = [{"text": t.strip()[:5000], "used": False} for t in data.pop("scripts") if t.strip()]
        ch = Channel(**data)
        if body.kind == "trending":
            ch.query = "" if body.query == "all" else body.query
            ch.name, ch.interval_hours = "Trending (CC clips): " + (TREND_CATS.get(ch.query) or "all"), body.interval_hours or 3
        if body.kind == "reel":
            ch.scripts, ch.mode, ch.permission_note, ch.resolved_url = scripts, "original", "", "reel"
            ch.name, ch.aspect, ch.voice = "Original reels: " + body.query[:60], "9:16", body.voice or None
            if body.auto_scripts:
                ch.name, ch.interval_hours, ch.permission_note = "Trending reels (local AI): " + body.query[:40], 4, language
        s.add(ch)
        s.commit()
        return to_dict(ch)


TREND_CATS = {"": "all", "26": "food & how-to", "22": "people & blogs", "24": "entertainment", "23": "comedy",
              "20": "gaming", "17": "sports", "28": "science & tech"}


class ScriptsIn(BaseModel):
    scripts: list[str] = Field(max_length=500)


@app.post("/api/channels/{chid}/scripts")
def add_scripts(chid: int, body: ScriptsIn):
    with SessionLocal() as s:
        ch = _get(s, Channel, chid)
        if ch.kind != "reel":
            raise HTTPException(400, "Scripts can only be added to an Original reels source.")
        new = [{"text": t.strip()[:5000], "used": False} for t in body.scripts if t.strip()]
        ch.scripts = list(ch.scripts or []) + new
        s.commit()
        return to_dict(ch)


@app.get("/api/channels")
def list_channels():
    with SessionLocal() as s:
        return [to_dict(c) for c in s.query(Channel).order_by(Channel.id.desc())]


@app.post("/api/channels/{chid}/run")
def run_channel_now(chid: int):
    with SessionLocal() as s:
        _get(s, Channel, chid)
    executor.submit(run_channel, chid)
    return {"started": True}


@app.post("/api/channels/{chid}/toggle")
def toggle_channel(chid: int):
    with SessionLocal() as s:
        ch = _get(s, Channel, chid)
        ch.auto = not ch.auto
        s.commit()
        return to_dict(ch)


class ReviewIn(BaseModel):
    review_first: bool


# ---------- Ideas: competitor outliers + trending (optional module) ----------
class CompIn(BaseModel):
    query: str = Field(min_length=2, max_length=300)


@app.get("/api/ideas")
def ideas(category: str = ""):
    from . import competitors

    if not re.fullmatch(r"\d{0,3}", category):
        raise HTTPException(400, "bad category")
    out = {**competitors.overview(), "trending": [], "trending_error": None}
    try:
        out["trending"] = competitors.trending(category)
    except Exception as e:  # noqa: BLE001
        out["trending_error"] = str(e)[:200]
    return out


@app.post("/api/ideas/competitors")
def add_competitor(body: CompIn):
    from . import competitors

    try:
        return competitors.add(body.query.strip())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not find that channel: {str(e)[:150]}")


@app.delete("/api/ideas/competitors")
def del_competitor(url: str):
    from . import competitors

    return competitors.remove(url)


@app.post("/api/ideas/scan")
def scan_competitors():
    from . import competitors

    return competitors.scan()


# ---------- weekly report + backup ----------
@app.post("/api/reports/weekly")
def make_weekly():
    from .growth import weekly_report

    p = weekly_report()
    if not p:
        raise HTTPException(500, "The report could not be made right now.")
    return {"file": p.name}


@app.get("/api/reports")
def list_reports():
    d = DATA_DIR / "reports"
    return sorted((f.name for f in d.glob("weekly-*.pdf")), reverse=True)[:20] if d.exists() else []


@app.get("/api/reports/{name}")
def get_report(name: str):
    if not re.fullmatch(r"weekly-\d{4}-\d{2}-\d{2}\.pdf", name):
        raise HTTPException(404, "Not found")
    p = DATA_DIR / "reports" / name
    if not p.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(p, media_type="application/pdf", filename=name)


@app.get("/api/worker/backup")
def worker_backup(request: Request):
    """The helper PC keeps a daily copy of the dashboard's data on your own computer."""
    _worker_or_401(request)
    from .growth import backup_data

    return backup_data()


# ---------- small settings: affiliate rules, Pexels key ----------
def _secret_setting(key: str) -> str:
    raw = get_setting(key)
    try:
        return decrypt(raw) if raw else ""
    except Exception:  # noqa: BLE001
        return ""


class AffRule(BaseModel):
    keywords: str = Field(max_length=300)
    text: str = Field(max_length=500)


class AffIn(BaseModel):
    rules: list[AffRule] = Field(max_length=50)


@app.get("/api/settings/affiliate")
def get_affiliate():
    return {"rules": json.loads(get_setting("affiliate_rules") or "[]")}


@app.post("/api/settings/affiliate")
def set_affiliate(body: AffIn):
    rules = [r.model_dump() for r in body.rules if r.keywords.strip() and r.text.strip()]
    for r in rules:
        for u in re.findall(r"https?://\S+", r["text"]):
            if not u.startswith("https://"):
                raise HTTPException(400, "Use https:// links only.")
    set_setting("affiliate_rules", json.dumps(rules))
    return {"rules": rules}


class KeyIn(BaseModel):
    key: str = Field("", max_length=200)


class FaceSizeIn(BaseModel):
    size: Literal["S", "M", "L"]


@app.get("/api/settings/face-size")
def face_size_get():
    return {"size": get_setting("thumb_face_size") or "S"}


@app.post("/api/settings/face-size")
def face_size_set(body: FaceSizeIn):
    """How big the face is on thumbnails. Thumbnails of clips that are not uploaded yet are made again."""
    set_setting("thumb_face_size", body.size)
    executor.submit(_rethumb, None)
    return {"size": body.size}


class ReminderIn(BaseModel):
    on: bool = True
    preset: str = Field("en", max_length=20)
    custom: str = Field("", max_length=200)


@app.get("/api/settings/reminder")
def reminder_get():
    """The small "like and subscribe" line near the end of every video: on/off and the wording."""
    from . import reminder

    return reminder.settings()


@app.post("/api/settings/reminder")
def reminder_set(body: ReminderIn):
    from . import reminder

    if body.preset != "custom" and body.preset not in dict(reminder.PRESETS):
        raise HTTPException(400, "Choose one of the wordings in the list.")
    custom = reminder.clean(body.custom)
    if body.preset == "custom":
        if len(body.custom.strip()) > reminder.MAX_LEN:
            raise HTTPException(400, f"Keep it short: at most {reminder.MAX_LEN} letters.")
        if len(custom) < 3:
            raise HTTPException(400, "Type a few plain words (letters only — emoji and symbols cannot be shown on the video).")
    return reminder.save(body.on, body.preset, custom)


class PhoneIn(BaseModel):
    action: Literal["setup", "new", "on", "off"]


@app.get("/api/phone")
def phone_get():
    from . import phone

    return phone.status()


@app.post("/api/phone")
def phone_set(body: PhoneIn):
    from . import phone

    if body.action in ("setup", "new"):
        return phone.setup(new=body.action == "new")
    set_setting("ntfy_on", "1" if body.action == "on" else "0")
    return phone.status()


@app.post("/api/phone/test")
def phone_test():
    from . import phone

    if not phone.enabled():
        raise HTTPException(400, "Set up phone notifications first.")
    if not phone.push("If you can read this on your phone, notifications work.", title="Raji Studio test", tags="white_check_mark"):
        raise HTTPException(502, "The notification service could not be reached. Try again in a minute.")
    return {"ok": True}


@app.get("/api/settings/pexels")
def get_pexels():
    return {"set": bool(_secret_setting("pexels_key"))}


@app.post("/api/settings/pexels")
def set_pexels(body: KeyIn):
    k = body.key.strip()
    if k:
        r = httpx.get("https://api.pexels.com/v1/search", params={"query": "food", "per_page": 1}, headers={"Authorization": k}, timeout=20)
        if r.status_code != 200:
            raise HTTPException(400, "Pexels did not accept this key.")
    set_setting("pexels_key", encrypt(k) if k else "")
    return {"set": bool(k)}


# ---------- Instagram + Facebook cross-post (optional module) ----------
class MetaIn(BaseModel):
    token: str = Field(min_length=20, max_length=1000)


@app.get("/media/{cid}/{exp}/{sig}.mp4")
def public_media(cid: int, exp: int, sig: str, t: int = Query(1, ge=1, le=10**9)):
    from .meta import check_link

    if not check_link(cid, exp, sig, t):  # the signature covers the customer number: no other copy's clip can be opened
        raise HTTPException(404, "Not found")
    with tenant.use(t):
        with SessionLocal() as s:
            c = s.get(Clip, cid)
            if not c or not c.output_path or not Path(c.output_path).exists():
                raise HTTPException(404, "Not found")
            return FileResponse(c.output_path, media_type="video/mp4")


@app.get("/api/meta")
def meta_status():
    from . import meta

    return meta.status()


@app.post("/api/meta/setup")
def meta_setup(body: MetaIn):
    from . import meta

    try:
        return meta.setup(body.token)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not reach Meta: {str(e)[:150]}")


@app.post("/api/meta/toggle")
def meta_toggle():
    from . import meta

    set_setting("meta_on", "0" if get_setting("meta_on") != "0" else "1")
    return meta.status()


@app.delete("/api/meta")
def meta_disconnect():
    from . import meta

    meta.disconnect()
    return meta.status()


@app.post("/api/clips/{cid}/crosspost")
def clip_crosspost(cid: int):
    from . import meta

    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status != "uploaded":
            raise HTTPException(409, "Upload it to YouTube first")
    executor.submit(meta.crosspost, cid)
    return {"started": True}


# ---------- permission inbox ----------
class GrantIn(BaseModel):
    proof: str = Field(max_length=500)


@app.get("/api/permissions")
def perm_list():
    from . import permissions

    return permissions.listing()


@app.post("/api/permissions/refresh")
def perm_refresh():
    from . import permissions

    return permissions.refresh_inbox()


@app.post("/api/permissions/{cid}/mark/{status}")
def perm_status(cid: str, status: Literal["asked", "skipped", "new", "declined"]):
    from . import permissions

    try:
        return permissions.set_status(cid, status)
    except KeyError:
        raise HTTPException(404, "Not in the list")


class EmailIn(BaseModel):
    email: str = Field(max_length=200)


class MailTextIn(BaseModel):
    subject: str = Field(default="", max_length=150)
    message: str = Field(max_length=4000)


@app.post("/api/permissions/{cid}/message")
def perm_message(cid: str, body: MailTextIn):
    from . import permissions

    try:
        return permissions.set_message(cid, body.subject, body.message)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "Not in the list")


class MailIdIn(BaseModel):
    sign: str = Field(default="", max_length=60)
    about: str = Field(default="", max_length=40)


@app.get("/api/settings/mail")
def mail_id_get():
    from . import permissions

    sign, about = permissions.mail_identity()
    return {"sign": sign, "about": about}


@app.post("/api/settings/mail")
def mail_id_set(body: MailIdIn):
    from . import permissions

    set_setting("mail_sign", body.sign.strip())
    set_setting("mail_about", body.about.strip())
    n = permissions.rewrite_unsent()
    sign, about = permissions.mail_identity()
    return {"sign": sign, "about": about, "rewritten": n}


@app.post("/api/permissions/rewrite")
def perm_rewrite():
    from . import permissions

    return {"rewritten": permissions.rewrite_unsent()}


@app.post("/api/permissions/{cid}/revoke")
def perm_revoke(cid: str):
    from . import permissions

    try:
        return permissions.revoke(cid)
    except KeyError:
        raise HTTPException(404, "Not in the list")


# ---------- viral board + 5 daily picks ----------
class ViralPickIn(BaseModel):
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")


@app.get("/api/viral")
def viral_view(day: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    from . import viral

    return viral.view(day)


@app.post("/api/viral/refresh")
def viral_refresh():
    from . import viral

    viral.refresh(force=True)
    return viral.view()


@app.post("/api/viral/pick")
def viral_pick(body: ViralPickIn):
    from . import viral

    try:
        viral.pick_today(extra=body.video_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return viral.view()


@app.post("/api/viral/send")
def viral_send():
    from . import viral

    return viral.send_ready_mails()


@app.post("/api/permissions/find-emails")
def perm_find_emails():
    from . import permissions

    return {"found": permissions.find_emails()}


@app.post("/api/permissions/{cid}/find-email")
def perm_find_email(cid: str):
    from . import permissions

    return {"found": permissions.find_emails(only=cid)}


@app.post("/api/ai/setup")
def ai_setup():
    set_setting("ai_wanted", "1")
    with SessionLocal() as s:
        w = s.query(Worker).filter(Worker.approved.is_(True)).order_by(Worker.last_seen.desc()).first()
        return {"status": (w.ai_status if w else None) or "The helper PC starts installing within a minute."}


@app.post("/api/permissions/{cid}/email")
def perm_email(cid: str, body: EmailIn):
    from . import permissions

    try:
        return permissions.set_email(cid, body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "Not in the list")


@app.post("/api/permissions/{cid}/send")
def perm_send(cid: str):
    from . import permissions

    try:
        return permissions.send_mail(cid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "Not in the list")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Gmail could not send it: {str(e)[:150]}")


@app.post("/api/permissions/{cid}/grant-reply")
def perm_grant_reply(cid: str):
    from . import permissions

    try:
        return permissions.grant_from_reply(cid)
    except KeyError:
        raise HTTPException(404, "No reply saved for this creator")


@app.get("/api/gmail")
def gmail_status():
    from . import gmail

    return {"email": gmail.connected_email()}


@app.get("/auth/gmail")
def gmail_login():
    from . import gmail, onboarding

    if not acl.allowed("gmail.connect"):
        return RedirectResponse("/?gmail_error=" + quote(acl.message("gmail.connect")), status_code=302)
    if not onboarding.allows("gmail"):
        return RedirectResponse("/?gmail_error=" + quote("Finish the earlier setup steps first."), status_code=302)
    if not youtube.configured():
        msg = ("Google sign-in is not set up on this server (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)." if tenant.current() == tenant.OWNER
               else "Save your Google project's keys first (My channels → Google project keys).")
        return RedirectResponse("/?gmail_error=" + quote(msg), status_code=302)
    return RedirectResponse(gmail.login_url(), status_code=302)


@app.delete("/api/gmail")
def gmail_disconnect():
    from . import gmail

    gmail.disconnect()
    return {"email": None}


@app.post("/api/permissions/{cid}/grant")
def perm_grant(cid: str, body: GrantIn):
    from . import permissions

    try:
        return permissions.grant(cid, body.proof)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "Not in the list")


# ---------- Telegram remote control (optional module) ----------
class TgIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


@app.get("/api/telegram")
def tg_status():
    from . import telegram

    return telegram.status()


@app.post("/api/telegram/setup")
def tg_setup(body: TgIn):
    from . import telegram

    try:
        return telegram.setup(body.token)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/telegram/toggle")
def tg_toggle():
    from .db import get_setting as gs, set_setting as ss

    ss("tg_on", "0" if gs("tg_on") != "0" else "1")
    from . import telegram

    return telegram.status()


@app.post("/api/telegram/test")
def tg_test():
    from . import telegram

    if not telegram.enabled():
        raise HTTPException(400, "Telegram is not linked yet.")
    telegram.notify("👋 Test message from Raji Studio. Send /help to see the commands.")
    return {"ok": True}


@app.delete("/api/telegram")
def tg_disconnect():
    from . import telegram

    telegram.disconnect()
    return telegram.status()


# ---------- analytics: live subscribers / views for my channels and videos ----------
@app.get("/api/analytics")
def analytics():
    from .stats import overview

    return overview()


@app.post("/api/analytics/sync")
def analytics_sync(live: int = 0):
    from .stats import overview, recently_synced, sync_all, sync_all_watch_time

    if live and (recently_synced(seconds=100) or quota.DAILY_UNITS - quota.used() < 2 * quota.UPLOAD_COST):
        return {**overview(), "errors": []}  # live mode: at most one YouTube read per ~2 minutes, never the upload budget
    res = sync_all()
    sync_all_watch_time(max_age_minutes=60)  # watch time changes slowly (YouTube adds it with a 1-3 day delay)
    errors = [v["error"] for v in res.values() if isinstance(v, dict) and v.get("error")]
    return {**overview(), "errors": errors}


# ---------- setup checklist: everything the owner still has to do, in one place ----------
@app.get("/api/setup")
def setup_checklist():
    from . import faces, gmail, meta, telegram

    items = []

    def add(key, title, done, why, action=None, optional=False, blocked=""):
        items.append({"key": key, "title": title, "done": bool(done), "why": why, "action": action, "optional": optional,
                      "blocked": blocked})

    with SessionLocal() as s:
        adopt_orphans(s)
        dests = s.query(Destination).all()
        main = main_destination(s)
        main_name = main.name if main else ""
        n_sources = s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "viral")).count()
        thumbs_wait = s.query(Clip).filter(Clip.thumb_pending.is_(True), Clip.status == "uploaded").count()
        thumbs_ok = s.query(Clip).filter(Clip.status == "uploaded", Clip.thumb_path.isnot(None),
                                         Clip.thumb_pending.isnot(True)).count()
        hp = _helper_state(s)
        llm = s.query(Worker).filter(Worker.approved.is_(True), Worker.llm.is_(True)).count() > 0
        ai_st = next((w.ai_status for w in s.query(Worker).filter(Worker.approved.is_(True)).order_by(Worker.last_seen.desc())
                      if w.ai_status), "")
        upcoming = [{"id": c.id, "title": c.title, "at": (c.publish_at.isoformat() + "Z") if c.publish_at else None,
                     "status": c.status}
                    for c in s.query(Clip).filter(Clip.status.in_(["queued", "uploading"])).order_by(Clip.publish_at).limit(6)]
        review = s.query(Clip).filter(Clip.status == "rendered").count()
        failed = s.query(Clip).filter(Clip.status == "failed").count()
        ai_src = s.query(Channel).filter(Channel.kind == "reel", Channel.auto_scripts.is_(True)).count()
    add("youtube", "YouTube channel connected", dests and not any(_dest_dict(d)["needs_reconnect"] for d in dests),
        "Uploads go to your channel.", {"href": "/auth/login", "label": "Connect / reconnect"})
    if len(dests) > 1:
        add("main", "Main channel chosen", True, f"{len(dests)} channels connected. Main: {main_name} — new sources, quick uploads, "
            "your own videos and viral picks go there.", {"go": "settings", "label": "My channels"})
    add("verify", "Channel phone-verified (custom thumbnails)", thumbs_wait == 0 and thumbs_ok > 0,
        "YouTube needs a verified phone number before it accepts custom thumbnails. Do it once now; this line turns "
        "green when YouTube has accepted the first thumbnail." if not thumbs_ok else "YouTube accepts your thumbnails.",
        {"url": "https://www.youtube.com/verify", "label": "Open youtube.com/verify"},
        blocked=f"{thumbs_wait} thumbnail(s) were refused by YouTube — verify the channel's phone number; they are then sent again by themselves"
        if thumbs_wait else "")
    add("face", "Your face photo", faces.DEFAULT_FACE.exists(), "Put on every thumbnail.", {"go": "settings", "label": "Upload photo"})
    add("helper", "Helper PC online", hp["online"], "Downloads videos, makes reels and captions from your home internet.",
        {"help": "helper", "label": "How to start it"}, blocked="" if hp["online"] else f"{hp['waiting']} job(s) are waiting for it")
    add("gmail", "Gmail connected", gmail.connected_email(), "Alerts to your inbox and permission mails to creators.",
        {"href": "/auth/gmail", "label": "Connect Gmail"})
    add("source", "At least one source", n_sources > 0, "A source is where clips come from; it runs by itself.",
        {"open": "add_source", "label": "Add source"}, blocked="" if n_sources else "No source yet — nothing will be made until you add one")
    perms = []
    try:
        from .permissions import listing

        perms = listing()
    except Exception:  # noqa: BLE001
        pass
    need_mail = [c for c in perms if c["status"] == "new" and not c.get("email")]
    ready = [c for c in perms if c["status"] == "new" and c.get("email") and not c.get("mailed")]
    replied = [c for c in perms if c["status"] == "replied"]
    add("perm", "Permission inbox", not need_mail and not replied and not ready,
        "Creators who may let you use their videos.", {"go": "ideas", "label": "Open inbox"},
        blocked=(f"{len(replied)} creator(s) replied — decide yes/no. " if replied else "")
        + (f"{len(ready)} mail(s) ready — check and press Send. " if ready else "")
        + (f"{len(need_mail)} creator(s) have no public email — open their About page (View email address) and paste it, or press Skip."
           if need_mail else ""))
    try:
        from . import viral

        vv = viral.view()
        ready = vv["ready_mails"]
        add("viral", "Today's viral picks", ready == 0, f"{len(vv['picks'])} of {vv['per_day']} picked today — clipped and uploaded by themselves.",
            {"go": "viral", "label": "Open Viral"}, blocked=f"{ready} permission mail(s) ready — press “Send all ready mails”" if ready else "")
    except Exception:  # noqa: BLE001
        pass
    add("review", "Clips waiting for your approval", review == 0, "Only sources with “Review first” wait for you.",
        {"go": "clips", "label": "Review clips"}, blocked=f"{review} clip(s) ready to review" if review else "")
    add("failed", "No failed clips", failed == 0, "", {"fix": "retry_failed", "label": "Retry all failed"},
        blocked=f"{failed} clip(s) failed" if failed else "")
    add("ai", "Local AI on the helper PC (Ollama)", llm, "Writes reel scripts on trending topics by itself.",
        {"fix": "setup_ai", "label": "Install now"}, optional=True,
        blocked="" if llm else (ai_st or ("Installs by itself on the helper PC." if ai_src else "")))
    add("telegram", "Telegram (phone buttons)", telegram.enabled(), "Approve and reply from your phone. Without it, alerts come by Gmail.",
        {"go": "settings", "label": "Set up"}, optional=True)
    add("pexels", "Pexels key (real photos in reels)", bool(_secret_setting("pexels_key")), "Reels use stock photos instead of colours.",
        {"go": "settings", "label": "Add key"}, optional=True)
    add("meta", "Instagram + Facebook", meta.status()["connected"], "Every Short is also posted as a Reel on Instagram and Facebook.",
        {"go": "settings", "label": "Connect"}, optional=True)
    from .guide import HOW

    for i in items:
        i["how"] = HOW.get(i["key"], [])
    must = [i for i in items if not i["optional"]]
    return {"items": items, "done": sum(i["done"] for i in must), "total": len(must), "upcoming": upcoming}


@app.get("/api/guide")
def guide_page():
    """Everything a new admin needs on one page: the steps (with how-to), today's problems, and what to do when something goes wrong."""
    from . import guide

    def safe(fn, name, empty):  # this page must open even when a part of the system is broken
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            guide.record_error("Setup guide: " + name, e)
            return empty

    setup = safe(setup_checklist, "steps", {"items": [], "done": 0, "total": 0, "upcoming": []})
    problems = safe(health, "problems", {"ok": False, "items": [{
        "level": "bad", "text": "The system could not check itself just now. Try “Check again” in a minute; "
                                "if it stays, press Copy details below.", "fix": None}]})
    return {"setup": setup, "problems": problems, "help": [{"q": q, "auto": a, "do": d} for q, a, d in guide.HELP],
            "errors": guide.recent_errors(), "version": SERVER_VERSION[:7]}


@app.post("/api/guide/errors/clear")
def guide_errors_clear():
    from . import guide

    guide.clear_errors()
    return {"ok": True}


# ---------- health: every problem in plain words, with a one-click fix ----------
@app.get("/api/health")
def health():
    items, now = [], utcnow()
    items += acl.health_items()  # what the administrator paused, switched off or limited for this account
    with SessionLocal() as s:
        dests = s.query(Destination).order_by(Destination.id).all()
        if not dests:
            items.append({"level": "bad", "text": "No YouTube channel is connected.", "fix": "connect", "key": "no_channel"})
        for d in dests:
            dd = _dest_dict(d)
            if dd["needs_reconnect"] or (d.paused and "login expired" in (d.paused_reason or "")):
                items.append({"level": "bad", "text": f"{d.name}: the Google login must be renewed.", "fix": "connect", "key": "reconnect", "ref": d.id})
            elif d.paused:
                items.append({"level": "bad", "text": f"{d.name}: uploads are paused — {d.paused_reason or ''}",
                              "fix": "resume", "id": d.id,
                              "key": "paused_by_you" if (d.paused_reason or "").startswith("Paused ") else "paused"})
        if tenant.current() != tenant.OWNER and not youtube.configured():
            items.append({"level": "bad", "text": "Your Google project keys are missing (or sharing was switched off). Uploads and sign-ins "
                                                 "with Google cannot work until you save your own keys.", "fix": "keys", "key": "keys"})
        stuck = s.query(Clip).filter(Clip.status == "uploaded", Clip.yt_state == "private", Clip.publish_at.isnot(None),
                                     Clip.publish_at < now - timedelta(hours=6)).count()
        if stuck:
            items.append({"level": "warn", "text": f"{stuck} uploaded video(s) are still Private hours after their publish time. YouTube locks "
                                                  "uploads from a Google project that has not passed its “API compliance audit”. Fill in "
                                                  "the “YouTube API Services - Audit and Quota Extension Form” for your Google project; "
                                                  "until then, set each video to Public by hand in YouTube Studio.", "fix": None, "key": "private_stuck"})
        hp = _helper_state(s)
        if hp["waiting"] and not hp["online"]:
            items.append({"level": "bad", "text": f"Helper PC is offline and {hp['waiting']} job(s) are waiting for it.",
                          "fix": "helper", "key": "helper_offline"})
        elif not hp["approved"]:
            items.append({"level": "warn", "text": "No helper PC is set up yet — YouTube blocks downloads from the server.",
                          "fix": "helper", "key": "no_helper"})
        failed = s.query(Clip).filter(Clip.status == "failed").count()
        if failed:
            items.append({"level": "warn", "text": f"{failed} clip(s) failed.", "fix": "retry_failed", "key": "failed"})
        thumbs_wait = s.query(Clip).filter(Clip.thumb_pending.is_(True), Clip.status == "uploaded").count()
        if thumbs_wait:
            items.append({"level": "warn", "text": f"{thumbs_wait} thumbnail(s) were refused by YouTube. Verify the channel's phone "
                                                  "number at youtube.com/verify — they are then sent again by themselves.",
                          "fix": "verify", "key": "thumbs"})
        if dests and not s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "viral")).count():
            items.append({"level": "info", "text": "No source yet — add one and the system starts making clips.", "fix": "add_source", "key": "no_source"})
        for ch in s.query(Channel).filter(Channel.kind == "reel", Channel.auto.is_(True)).all():
            if not ch.auto_scripts and not any(not x.get("used") for x in (ch.scripts or [])):
                items.append({"level": "info", "text": f"Reel source “{ch.name}” has no scripts left — add scripts (Sources → Scripts).",
                              "fix": "sources", "key": "no_scripts", "ref": ch.id})
        try:
            from . import gmail
            from .permissions import listing

            if not gmail.connected_email() and any(c["status"] in ("new", "asked") for c in listing()):
                items.append({"level": "warn", "text": "Gmail is not connected, so permission mails cannot be sent and replies are not read.",
                              "fix": "gmail", "key": "gmail"})
        except Exception:  # noqa: BLE001
            pass
        from .guide import recent_errors

        fresh = [e for e in recent_errors() if e["at"] > (now - timedelta(hours=6)).isoformat()]
        n_err = sum(int(e.get("count") or 1) for e in fresh)
        if n_err >= 3:
            items.append({"level": "warn", "text": f"{n_err} unexpected server problems in the last 6 hours "
                                                  f"(latest: {fresh[0]['type']} in {fresh[0]['where']}).", "fix": "guide", "key": "errors"})
        if quota.summary()["uploads_left"] == 0 and s.query(Clip).filter(Clip.status == "queued").count():
            items.append({"level": "info", "text": "Today's upload budget is used; queued clips go up after the daily "
                                                  "reset (1:30 PM IST). Nothing to do.", "fix": None, "key": "quota"})
        due = now - timedelta(hours=8)
        for ch in s.query(Channel).filter(Channel.auto.is_(True)).all():
            if ch.last_status and ch.last_status.startswith("error"):
                items.append({"level": "warn", "text": f"Source “{ch.name or ch.query}” failed last time: "
                                                      f"{friendly_error(ch.last_status[7:])[:160]}", "fix": "run_source", "id": ch.id, "key": "source_failed"})
            elif ch.last_run and ch.last_run < due:
                items.append({"level": "warn", "text": f"Source “{ch.name or ch.query}” has not run for a while.",
                              "fix": "run_source", "id": ch.id, "key": "source_stale"})
    return {"ok": not any(i["level"] in ("bad", "warn") for i in items), "items": items}


@app.post("/api/fix/retry-failed")
def retry_failed():
    """Render every failed clip again (rules are checked again; clips that break a rule stay failed)."""
    n = 0
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.status == "failed").all():
            p = s.get(Project, c.project_id)
            if p is None or check_edit(p, c):
                continue
            res = s.execute(update(Clip).where(Clip.id == c.id, Clip.status == "failed")
                            .values(status="rendering", error=None, fetch_tries=0, job_lease=None, fetch_since=None))
            if res.rowcount:
                n += 1
                executor.submit(run_render, c.id)
        s.commit()
    return {"retried": n}


@app.put("/api/channels/{chid}")
def edit_channel(chid: int, body: ChannelEdit):
    with SessionLocal() as s:
        ch = _get(s, Channel, chid)
        data = body.model_dump(exclude_unset=True)
        if data.get("post_times") is not None:
            pt = data["post_times"].strip()
            if pt and not TIMES_RE.match(pt):
                raise HTTPException(400, "Upload times must look like 10:00, 18:30 (24-hour clock, separated by commas).")
            data["post_times"] = ", ".join(x.strip().zfill(5) for x in pt.split(",")) if pt else ""
        if "destination_id" in data:
            if data["destination_id"] is None:
                data.pop("destination_id")
            else:
                _get(s, Destination, data["destination_id"])
        if ch.kind == "reel":  # reels are always vertical originals
            data.pop("aspect", None)
            data.pop("clips_per_video", None)
            data.pop("clip_len", None)
        else:
            data.pop("voice", None)
        for k, v in data.items():
            setattr(ch, k, v)
        s.commit()
        return to_dict(ch)


# (3) a reel the local AI wrote badly: throw it away and have a new script written on another topic
@app.post("/api/clips/{cid}/rewrite")
def clip_rewrite(cid: int):
    from .autopilot import trending_phrase

    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        p = _get(s, Project, c.project_id)
        ch = s.get(Channel, p.channel_ref) if p.channel_ref else None
        if not ch or ch.kind != "reel" or not ch.auto_scripts:
            raise HTTPException(400, "Only reels written by the local AI can be written again.")
        if c.status in ("uploading", "uploaded"):
            raise HTTPException(409, "Already on YouTube; delete it there instead.")
        if not _helper_state(s)["online"]:
            raise HTTPException(409, "The helper PC is offline — it writes the scripts. Start it and try again.")
        topic = (ch.script_wanted or {}).get("topic") or trending_phrase(ch)
        if not topic:
            raise HTTPException(409, "No new topic is available right now — try again later.")
        if not ch.script_wanted:
            ch.script_wanted = {"topic": topic, "since": utcnow().isoformat()}
        shutil.rmtree(DATA_DIR / "clips" / str(cid), ignore_errors=True)
        s.delete(c)
        s.commit()
        return {"discarded": True, "topic": topic}


# (2) make every waiting clip again (e.g. after changing how Shorts look); upload times are kept
class RerenderIn(BaseModel):
    fit: Literal["full", "wide", "fill"] | None = None  # also set the picture option on Shorts/square clips


@app.post("/api/fix/rerender-waiting")
def rerender_waiting(body: RerenderIn):
    n, skipped = 0, 0
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.status.in_(["rendered", "queued"])).order_by(Clip.publish_at, Clip.id).all():
            p = s.get(Project, c.project_id)
            if p is None or check_edit(p, c):
                skipped += 1
                continue
            keep_queue = c.status == "queued"
            res = s.execute(update(Clip).where(Clip.id == c.id, Clip.status.in_(["rendered", "queued"]))
                            .values(status="rendering", error=None, autoqueue=keep_queue,
                                    **({"fit": body.fit} if body.fit and c.aspect != "16:9" else {})))
            if res.rowcount:
                n += 1
                executor.submit(run_render, c.id)
        s.commit()
    return {"started": n, "skipped": skipped}


class ReelLangIn(BaseModel):
    language: Literal["hi", "en"]


@app.post("/api/channels/{chid}/language")
def channel_language(chid: int, body: ReelLangIn):
    """Language of the local AI's reel scripts (and a matching Indian voice)."""
    with SessionLocal() as s:
        ch = _get(s, Channel, chid)
        if ch.kind != "reel" or not ch.auto_scripts:
            raise HTTPException(400, "Only for AI-script reel sources")
        ch.permission_note = body.language
        ch.voice = "en-IN-PrabhatNeural" if body.language == "en" else "hi-IN-MadhurNeural"
        s.commit()
        return to_dict(ch)


@app.post("/api/channels/{chid}/review")
def set_review(chid: int, body: ReviewIn):
    """Switch a source between 'auto upload' and 'review first'."""
    with SessionLocal() as s:
        ch = _get(s, Channel, chid)
        ch.review_first = body.review_first
        s.commit()
        return to_dict(ch)


@app.delete("/api/channels/{chid}")
def delete_channel(chid: int):
    with SessionLocal() as s:
        s.delete(_get(s, Channel, chid))
        s.commit()
        return {"deleted": True}


# ---------- youtube oauth ----------
def _log_me(request: Request, action: str, text: str):
    """One line in the activity log for something a route did itself (customers, and the Super Admin inside their dashboard)."""
    who = getattr(request.state, "who", None) or {}
    if who.get("role") == "user":
        acl.record(who.get("account_id"), "user", action, text, who.get("tenant"))
    elif who.get("viewing"):
        acl.record(who["viewing"].get("account_id"), "super", action, text, who.get("tenant"))


def _reconnect_needed() -> bool:
    with SessionLocal() as s:
        return any(_dest_dict(d)["needs_reconnect"] or (d.paused and "login expired" in (d.paused_reason or ""))
                   for d in s.query(Destination).all())


@app.get("/auth/login")
def auth_login():
    if not acl.allowed("channels.connect") and not _reconnect_needed():  # renewing an expired login stays possible
        return RedirectResponse("/?connect_error=" + quote(acl.message("channels.connect")), status_code=302)
    if not youtube.configured():
        if tenant.current() != tenant.OWNER:
            return RedirectResponse("/?connect_error=" + quote("Save your Google project's keys first (Get started → step 1, or My channels → Google project keys)."), status_code=302)
        raise HTTPException(400, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.")
    return RedirectResponse(youtube.login_url())


# ---------- locked step-by-step onboarding (customers) ----------
class GoogleKeysIn(BaseModel):
    client_id: str = Field(max_length=200)
    client_secret: str = Field(max_length=200)


class AckIn(BaseModel):
    key: Literal["verify", "face", "gmail"]


@app.get("/api/onboarding")
def onboarding_state():
    from . import onboarding

    st = onboarding.state()
    st["helper_code"] = None if tenant.current() == tenant.OWNER else workers.claim_code(tenant.current())
    return st


@app.post("/api/onboarding/google")
def onboarding_google(body: GoogleKeysIn):
    """The copy's own Google project keys (pasted by the customer, or by the Super Admin while viewing as them)."""
    from . import onboarding

    if tenant.current() == tenant.OWNER:
        raise HTTPException(400, "The owner's keys are set on the server (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
    try:
        onboarding.save_google_keys(body.client_id, body.client_secret)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return onboarding.state()


@app.post("/api/onboarding/ack")
def onboarding_ack(body: AckIn):
    from . import onboarding

    if not onboarding.allows(body.key):
        raise HTTPException(409, "Finish the steps before this one first.")
    onboarding.acknowledge(body.key)
    return onboarding.state()


def _need_step(step: str):
    """Server-side lock: a customer who is still being set up cannot jump ahead of the current step."""
    from . import onboarding

    if not onboarding.allows(step):
        raise HTTPException(409, "This opens after the earlier setup steps are done. Open “Get started” and follow the steps in order.")


HELPER_APP_FILES = ("config.py", "tenant.py", "crypto.py", "ingest.py", "tts.py", "captions.py", "hot.py")
HELPER_WORKER_FILES = ("clipstudio_worker.py", "transcribe.py", "run_worker.bat", "run_hidden.bat", "install_autostart.bat",
                       "uninstall_autostart.bat")

def _helper_zip(tid: int, with_code: bool = True) -> bytes:
    """The helper program for one copy: only the files it needs (not the server), its code and the server address."""
    import io
    import zipfile

    root, buf = _STATIC.parent, io.BytesIO()
    crlf = lambda data: data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")  # noqa: E731  (cmd needs Windows line ends)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in HELPER_APP_FILES:
            z.write(root / "app" / f, f"app/{f}")
        for f in HELPER_WORKER_FILES:
            data = (root / "worker" / f).read_bytes()
            z.writestr(f"worker/{f}", crlf(data) if f.endswith(".bat") else data)
        z.writestr("worker/requirements.txt", (root / "worker" / "helper_requirements.txt").read_bytes())
        z.writestr("worker/server.txt", BASE_URL)
        z.writestr("Start helper.bat", crlf((root / "worker" / "start_helper.bat").read_bytes()))
        z.writestr("helper_version.txt", SERVER_VERSION or "dev")  # written last on the PC: marks a complete update
        if with_code and tid != tenant.OWNER:
            z.writestr("worker/helper_code.txt", workers.claim_code(tid))
    return buf.getvalue()


@app.get("/api/helper/download")
def helper_download():
    """The helper as one ZIP, already tied to this account (customers); double-click "Start helper" inside."""
    _need_step("helper")
    data = _helper_zip(tenant.current())
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="helper.zip"', "Cache-Control": "no-store"})


@app.get("/api/worker/update")
def worker_update(request: Request):
    """A helper without Git (a customer's) fetches the current helper files from its own server."""
    _worker_or_401(request)
    return Response(_helper_zip(tenant.current(), with_code=False), media_type="application/zip", headers={"Cache-Control": "no-store"})


@app.get("/auth/callback")
def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if state and state.startswith("reset:"):  # password recovery, verified through Google
        if error or not code:
            return RedirectResponse("/forgot?err=" + quote("Google sign-in was cancelled."), status_code=302)
        try:
            with tenant.use(tenant.OWNER):
                email = youtube.handle_reset_callback(code, state)
        except Exception as e:  # noqa: BLE001
            return RedirectResponse("/forgot?err=" + quote(str(e)[:150]), status_code=302)
        if email.lower() not in auth.allowed_reset_emails():
            return RedirectResponse("/forgot?err=" + quote("That Google account is not allowed to reset this password."), status_code=302)
        resp = RedirectResponse("/reset", status_code=302)
        _set_cookie(resp, auth.RESET_COOKIE, auth.make_reset_token(email), auth.RESET_TTL)
        return resp
    # connecting a YouTube channel or Gmail: only for a logged-in dashboard user
    if auth.auth_configured() and not auth.request_authenticated(request):
        return RedirectResponse("/login?next=/", status_code=302)
    if state and state.startswith("gmail:"):
        from . import gmail

        if error or not code:
            return RedirectResponse("/#settings", status_code=302)
        if not acl.allowed("gmail.connect"):
            return RedirectResponse("/?gmail_error=" + quote(acl.message("gmail.connect")), status_code=302)
        try:
            gmail.handle_callback(code, state)
        except Exception as e:  # noqa: BLE001
            return RedirectResponse("/?gmail_error=" + quote(str(e)[:200]), status_code=302)
        _log_me(request, "gmail.connect", "Connected Gmail")
        return RedirectResponse("/?gmail=ok", status_code=302)
    if error or not code or not state:
        return RedirectResponse("/", status_code=302)
    with SessionLocal() as s:
        before = {d.id for d in s.query(Destination.id)}
    who = auth.request_identity(request) or {}
    # a NEW channel needs the switch "Connect a YouTube channel"; renewing the login of a connected one is always allowed
    new_ok = acl.allowed("channels.connect")
    try:
        with tenant.use(who.get("tenant") or tenant.OWNER):
            did = youtube.handle_callback(code, state, who.get("channel_limit") if new_ok else 0)
    except youtube.ChannelLimit as e:
        if not new_ok:
            return RedirectResponse("/?connect_error=" + quote(acl.message("channels.connect")), status_code=302)
        return RedirectResponse("/?limit=" + quote(str(e)[:300]), status_code=302)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse("/?connect_error=" + quote(str(e)[:200]), status_code=302)
    with SessionLocal() as s:
        d = s.get(Destination, did)
        if d and d.paused and "login expired" in (d.paused_reason or ""):
            d.paused, d.paused_reason = False, None
            s.commit()
        adopt_orphans(s)
        if d:
            _log_me(request, "channels.connect",
                    ("Connected the YouTube channel “" if did not in before else "Renewed the login of “") + (d.name or "") + "”")
    return RedirectResponse(f"/?connected={did}&new={0 if did in before else 1}", status_code=302)


def _cookie_status() -> dict:
    if not COOKIES_PATH.exists():
        return {"saved": False}
    from .ingest import read_cookies

    txt = read_cookies()
    n = sum(1 for ln in txt.splitlines() if ln.strip() and not ln.startswith("#"))
    return {"saved": True, "cookies": n, "updated_at": COOKIES_PATH.stat().st_mtime}


@app.get("/api/settings/cookies")
def cookies_status():
    return _cookie_status()


@app.post("/api/settings/cookies")
def cookies_save(body: CookiesIn):
    """Netscape-format cookies.txt so the server can read YouTube without the 'confirm you're not a bot' block."""
    lines = []
    for ln in body.text.replace("\r\n", "\n").split("\n"):
        ln = ln.rstrip()
        if not ln or (ln.startswith("#") and not ln.startswith("#HttpOnly_")):
            continue
        parts = ln.split("\t")
        if len(parts) != 7:
            raise HTTPException(400, "Not a cookies.txt export: each line needs 7 tab-separated fields. Export again with a 'cookies.txt' browser extension.")
        lines.append(ln)
    if not any(("youtube.com" in ln or "google.com" in ln) for ln in lines):
        raise HTTPException(400, "No youtube.com / google.com cookies found in that file.")
    COOKIES_PATH.write_text(encrypt("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n"), encoding="utf-8")  # encrypted at rest
    try:
        COOKIES_PATH.chmod(0o600)
    except OSError:
        pass
    return _cookie_status()


@app.delete("/api/settings/cookies")
def cookies_delete():
    COOKIES_PATH.unlink(missing_ok=True)
    return {"saved": False}


class WorkerRegIn(BaseModel):
    name: str = Field("helper", max_length=120)


class WorkerFailIn(BaseModel):
    kind: str = Field("source", max_length=20)
    error: str = Field("", max_length=1000)


def _worker_or_401(request: Request) -> int:
    found = workers.authenticate(request.headers.get("x-worker-token", ""))
    if not found:
        raise HTTPException(401, "Unknown helper")
    wid, approved = found
    if not approved:
        raise HTTPException(403, "Waiting for approval in the dashboard")
    return wid


class PairIn(BaseModel):
    code: str = Field("", max_length=20)


@app.post("/api/worker/register")
def worker_register(body: WorkerRegIn, request: Request):
    ip = _client_ip(request)
    if auth.rate_limited(ip, "helper"):
        raise HTTPException(429, "Too many attempts")
    auth.note_fail(ip, "helper")  # helper registrations have their own counter
    with SessionLocal() as s:
        s.query(Worker).filter(Worker.approved.is_(False), Worker.created_at < utcnow() - timedelta(minutes=30)).delete()
        s.commit()
        if s.query(Worker).filter(Worker.approved.is_(False)).count() >= 5:
            raise HTTPException(429, "Too many helpers waiting for approval")
        tok = secrets.token_urlsafe(32)
        if tenant.current() != tenant.OWNER:
            tok = f"t{tenant.current()}.{tok}"  # tells the server which customer this helper belongs to
        w = Worker(name=(body.name.strip() or "helper")[:60], token_hash=workers.token_hash(tok), approved=False,
                   last_seen=utcnow(), ip=ip[:60], pair_hash=workers.token_hash(workers.pair_code(tok)))
        s.add(w)
        s.commit()
        return {"worker_id": w.id, "token": tok}


@app.get("/api/worker/jobs")
def worker_jobs(request: Request):
    _worker_or_401(request)
    jobs, now = [], utcnow()
    free = lambda lease: not lease or lease < now - JOB_LEASE  # noqa: E731
    with SessionLocal() as s:  # remember whether this helper has a local AI (for the setup checklist)
        w = s.query(Worker).filter(Worker.token_hash == workers.token_hash(request.headers.get("x-worker-token", ""))).first()
        if w is not None:
            w.llm = request.headers.get("x-worker-llm") == "1"
            if "x-worker-ai" in request.headers:
                w.ai_status = request.headers["x-worker-ai"][:200]
            s.commit()
    want_ai = False
    ai_on, pexels_on = acl.feature("ai.reels"), acl.feature("pexels.use")  # switched off for this account: no such jobs
    if ai_on and "x-worker-ai" in request.headers and request.headers.get("x-worker-llm") != "1":  # only helpers that know the job
        with SessionLocal() as s:
            want_ai = get_setting("ai_wanted") == "1" or s.query(Channel).filter(
                Channel.kind == "reel", Channel.auto_scripts.is_(True)).count() > 0
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.status == "fetching").order_by(Clip.id).limit(20):
            if len(jobs) >= 2 or not free(c.job_lease):
                continue
            if (c.fetch_tries or 0) >= MAX_JOB_TRIES:
                c.status, c.job_lease = "failed", None
                c.error = ("The helper PC could not get this video after 3 tries. Check the helper window/log, "
                           "then press Re-download video.")
                continue
            c.job_lease, c.fetch_tries = now, (c.fetch_tries or 0) + 1
            p = s.get(Project, c.project_id)
            if p.mode == "original" and (p.license or "").endswith("(my own video)"):
                c.status, c.job_lease = "failed", None  # an uploaded file cannot be made again by the helper
                c.error = "The uploaded video file is no longer on the server. Upload the video again (Clips → Upload my video)."
                continue
            if p.mode == "original":  # ReelGen makes the whole reel from my script on the helper PC
                ch = s.get(Channel, p.channel_ref) if p.channel_ref else None
                job = {"type": "reel", "clip_id": c.id, "script": p.description or "", "voice": (ch.voice if ch else "") or ""}
                pk = _secret_setting("pexels_key") if pexels_on else ""
                if pk:  # real stock photos instead of colour backgrounds
                    job["pexels_key"], job["photo_query"] = pk, (ch.query if ch and ch.query else "") or p.title
                jobs.append(job)
                continue
            job = {"type": "source", "clip_id": c.id, "video_id": p.video_id, "start": c.start, "end": c.end}
            if not c.captions and _wants_captions(s, p, c):  # no subtitles on YouTube: the helper writes them
                job["transcribe"], job["hint"] = True, f"{p.title} {' '.join(p.tags or [])} {(p.description or '')[:300]}"
            jobs.append(job)
        for c in s.query(Clip).filter(Clip.tts_pending.isnot(None), Clip.tts_pending != "").order_by(Clip.id).limit(5):
            if c.status == "fetching" or not free(c.job_lease):
                continue
            c.job_lease = now
            t = json.loads(c.tts_pending)
            jobs.append({"type": "tts", "clip_id": c.id, "text": t.get("text", ""), "voice": t.get("voice", "")})
        for p in s.query(Project).filter(Project.analysis_pending.is_(True)).order_by(Project.id).limit(5):
            if not free(p.analysis_lease):
                continue
            if (p.analysis_tries or 0) >= MAX_JOB_TRIES:
                p.analysis_pending = False  # carry on without the helper's data
                if p.quick:  # only Quick uploads keep this flag; autopilot videos have no quick state
                    p.quick = {**p.quick, "helper_analysed": True}
                executor.submit(resume_after_analysis, p.id)
                continue
            p.analysis_lease, p.analysis_tries = now, (p.analysis_tries or 0) + 1
            jobs.append({"type": "analyze", "project_id": p.id, "video_id": p.video_id,
                         "captions": bool((p.quick or {}).get("captions", True))})
        if ai_on and request.headers.get("x-worker-llm") == "1":
            for ch in s.query(Channel).filter(Channel.kind == "reel", Channel.auto_scripts.is_(True), Channel.script_wanted.isnot(None)):
                w = dict(ch.script_wanted or {})  # Postgres keeps an empty JSON value as 'null', which passes IS NOT NULL
                if not w.get("topic"):
                    continue
                if re.search(r"[^\x00-\x7f]", w["topic"]) and not re.search(r"\w{3}", w["topic"]):
                    ch.script_wanted = None  # a topic broken out of a Hindi title: ask again on the next run
                    continue
                if w.get("lease") and datetime.fromisoformat(w["lease"]) > now - JOB_LEASE:
                    continue
                w["lease"] = now.isoformat()
                ch.script_wanted = w
                jobs.append({"type": "script", "source_id": ch.id, "topic": w["topic"], "lang": ch.permission_note or "hi"})
        s.commit()
    if want_ai:  # the helper installs Ollama + the model by itself (it ignores this while already busy with it)
        jobs.append({"type": "setup_ai"})
    return {"jobs": jobs, "version": SERVER_VERSION}


class ScriptIn(BaseModel):
    topic: str = Field(max_length=200)
    script: str = Field(min_length=20, max_length=5000)


@app.post("/api/worker/script/{chid}")
def worker_script(chid: int, body: ScriptIn, request: Request):
    """The helper's local AI wrote a reel script: add it and make the reel right away."""
    _worker_or_401(request)
    with SessionLocal() as s:
        ch = _get(s, Channel, chid)
        if not ch.script_wanted:
            raise HTTPException(409, "No script was asked for")
        from .autopilot import script_problem

        why = script_problem(body.script, body.topic)
        if why:  # the small local model sometimes writes nonsense: ask again (3 tries), then move to the next topic
            w = dict(ch.script_wanted)
            w["tries"] = int(w.get("tries") or 0) + 1
            w.pop("lease", None)
            ch.script_wanted = w if w["tries"] < 3 else None
            ch.last_status = f"AI script about “{body.topic[:50]}” was {why} — " + ("writing it again" if w["tries"] < 3 else "skipped")
            s.commit()
            return {"ok": False, "why": why}
        ch.scripts = list(ch.scripts or []) + [{"text": body.script.strip(), "used": False, "topic": body.topic}]
        ch.script_wanted = None
        ch.last_run = None  # run now
        s.commit()
    executor.submit(run_channel, chid)
    return {"ok": True}


def _wants_captions(s, p: Project, c: Clip) -> bool:
    if (c.caption_style or {}).get("off"):
        return False
    if p.quick:
        return bool(p.quick.get("captions", True))
    ch = s.get(Channel, p.channel_ref) if p.channel_ref else None
    return bool(ch and ch.captions)


class CaptionsIn(BaseModel):
    cues: list[dict] = Field(max_length=2000)
    lang: str = Field("", max_length=10)


@app.post("/api/worker/captions/{cid}")
def worker_captions(cid: int, body: CaptionsIn, request: Request):
    _worker_or_401(request)
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.captions:
            return {"ok": True, "kept": "existing"}
        cues = [{"s": float(x.get("s", 0)), "e": float(x.get("e", 0)), "t": str(x.get("t", ""))[:200]}
                for x in body.cues if str(x.get("t", "")).strip()][:2000]
        if cues:
            c.captions = cues
            c.caption_style = {"on": True, "lang": body.lang or "auto", "pos": "bottom", "size": "M", "color": "#ffffff",
                               "source": "whisper"}
            s.commit()
    return {"ok": True, "lines": len(cues)}


JOB_LEASE = timedelta(minutes=40)  # a helper that took a job gets this long before another may take it
MAX_JOB_TRIES = 3


def _save_upload(file: UploadFile, dest: Path, limit: int):
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(dest, "wb") as f:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "File too large")
            f.write(chunk)


@app.post("/api/worker/source/{cid}")
def worker_source(cid: int, request: Request, file: UploadFile = File(...)):
    _worker_or_401(request)
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status != "fetching":
            raise HTTPException(409, f"Clip is '{c.status}', not waiting for a video")
        original = s.get(Project, c.project_id).mode == "original"
        dest = DATA_DIR / "clips" / str(cid) / "src" / ("reel.mp4" if original else f"src_{int(c.start)}_{int(c.end) + 1}.mp4")
        tmp = dest.with_name(f"{dest.stem}.{secrets.token_hex(4)}.part")
        _save_upload(file, tmp, 900 * 1024 * 1024)
        res = s.execute(update(Clip).where(Clip.id == cid, Clip.status == "fetching")
                        .values(status="rendering", job_lease=None, fetch_since=None))
        s.commit()
        if res.rowcount != 1:  # another helper delivered first
            tmp.unlink(missing_ok=True)
            raise HTTPException(409, "Already delivered")
        tmp.replace(dest)
        if original:  # the reel's own length is the clip
            from .ingest import stream_seconds

            secs = stream_seconds(dest, "v:0")
            if secs > 1:
                s.query(Clip).filter(Clip.id == cid).update({"end": round(secs, 2)})
                s.commit()
    executor.submit(run_render, cid)
    return {"ok": True}


@app.post("/api/worker/voice/{cid}")
def worker_voice(cid: int, request: Request, file: UploadFile = File(...)):
    _worker_or_401(request)
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if not c.tts_pending:
            raise HTTPException(409, "No voice-over is waiting for this clip")
        dest = DATA_DIR / "clips" / str(cid) / "voice.mp3"
        _save_upload(file, dest, MAX_VOICE_BYTES)
        c.voice_path, c.tts_pending, c.job_lease = str(dest), None, None
        if c.status in ("rendered", "queued", "failed"):
            c.status = "draft"
        s.commit()
    return {"ok": True}


@app.post("/api/worker/fail/{cid}")
def worker_fail(cid: int, body: WorkerFailIn, request: Request):
    _worker_or_401(request)
    if body.kind == "analyze":  # cid is a project id here: continue with a fallback slice
        with SessionLocal() as s:
            p = _get(s, Project, cid)
            p.analysis_pending = False
            if p.quick:  # only Quick uploads keep this flag; autopilot videos have no quick state
                p.quick = {**p.quick, "helper_analysed": True}
            s.commit()
        executor.submit(resume_after_analysis, cid)
        return {"ok": True}
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if body.kind == "tts":
            c.tts_pending = None
            c.error = "Voice-over failed on the helper PC: " + body.error[:300]
        elif body.kind == "script":
            pass
        elif c.status == "fetching":
            c.job_lease = None  # let it be tried again (up to 3 times in all)
            if (c.fetch_tries or 0) >= MAX_JOB_TRIES or "private" in body.error.lower() or "removed" in body.error.lower():
                c.status = "failed"
                c.error = (f"The helper PC could not get this video ({c.fetch_tries or 1} tries): "
                           + (body.error[:500] or "download failed") + " — press Re-download video to try again.")
        s.commit()
    return {"ok": True}


@app.get("/api/workers")
def list_workers():
    with SessionLocal() as s:
        return [workers.as_dict(w) for w in s.query(Worker).order_by(Worker.id)]


@app.post("/api/workers/{wid}/approve")
def approve_worker(wid: int, body: PairIn):
    _need_step("helper")
    with SessionLocal() as s:
        w = _get(s, Worker, wid)
        code = re.sub(r"\D", "", body.code or "")
        if w.pair_hash and not secrets.compare_digest(workers.token_hash(code), w.pair_hash):
            raise HTTPException(400, "Wrong code. Type the 6-digit pairing code shown in the helper's window or log.")
        w.approved = True
        s.commit()
        return workers.as_dict(w)


@app.delete("/api/workers/{wid}")
def delete_worker(wid: int):
    with SessionLocal() as s:
        s.delete(_get(s, Worker, wid))
        s.commit()
        return {"deleted": True}


class AnalysisIn(BaseModel):
    heatmap: list[dict] = Field(default_factory=list, max_length=2000)
    comments: list[str] = Field(default_factory=list, max_length=3000)
    caption_track: dict | None = None


@app.post("/api/worker/analysis/{pid}")
def worker_analysis(pid: int, body: AnalysisIn, request: Request):
    _worker_or_401(request)
    with SessionLocal() as s:
        p = _get(s, Project, pid)
        if not p.analysis_pending:
            raise HTTPException(409, "No analysis is waiting for this video")
        ch = s.get(Channel, p.channel_ref) if p.channel_ref and not p.quick else None
        if ch:  # autopilot source: its own clip length and clips per video
            want, n = min(ch.clip_len, p.duration or ch.clip_len), ch.clips_per_video
        else:
            fmt = (p.quick or {}).get("format", "shorts")
            want, n = min({"shorts": 35, "video": 60}.get(fmt, 35), p.duration or 35), 3
        if body.heatmap:
            p.heatmap = body.heatmap
        secs = parse_timestamps([str(t)[:2000] for t in body.comments], p.duration)
        p.moments = rank_moments(p.duration, p.heatmap or [], secs, want, n)
        if body.caption_track:
            p.caption_track = body.caption_track
        p.analysis_pending = False
        if p.quick:  # only Quick uploads keep this flag; autopilot videos have no quick state
            p.quick = {**p.quick, "helper_analysed": True}
        s.commit()
    executor.submit(resume_after_analysis, pid)
    return {"ok": True, "moments": len(p.moments or [])}


# ---------- quick upload ----------
class QuickIn(BaseModel):
    url: str = Field(max_length=500)
    format: Literal["shorts", "video"] = "shorts"
    length: Literal["auto", "whole"] = "auto"
    publish_at: str | None = None
    captions: bool = True
    mode: Literal["cc", "permission"] = "cc"
    permission_note: str = Field("", max_length=2000)
    destination_id: int | None = None  # which of my channels to upload to
    count: int = Field(1, ge=1, le=3)  # clips from this video (different best moments)


@app.post("/api/quick")
def quick_start(body: QuickIn):
    _check_yt_url(body.url)
    if body.length == "whole" and body.format == "shorts":
        raise HTTPException(400, "Shorts can be at most 3 minutes. Choose Video to upload the whole video.")
    if body.publish_at:
        try:
            datetime.fromisoformat(body.publish_at.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            raise HTTPException(400, "Pick a valid date and time")
    with SessionLocal() as s:
        if not s.query(Destination).count():
            raise HTTPException(400, "Connect your YouTube channel first (My channels).")
        if body.destination_id:
            _get(s, Destination, body.destination_id)
    acl.need_clip_room(body.count)
    try:
        info = fetch_info(body.url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read video: {friendly_error(e)}")
    if body.mode == "cc" and not is_cc(info["license"]):
        raise HTTPException(409, {"code": "not_cc", "license": info["license"] or "Standard YouTube License",
                                  "title": info["title"], "channel": info["channel"]})
    if body.mode == "permission":
        _check_permission_note(body.permission_note)
    if body.length == "whole" and info["duration"] > 90 * 60:
        raise HTTPException(400, "Whole-video upload works for videos up to 90 minutes. Choose Best moment instead.")
    with SessionLocal() as s:
        p = Project(url=body.url, mode=body.mode, permission_note=body.permission_note,
                    quick={"format": body.format, "length": body.length, "publish_at": body.publish_at,
                           "captions": body.captions, "destination_id": body.destination_id, "count": body.count,
                           "state": "starting", "step": "Reading the video"}, **info)
        s.add(p)
        s.commit()
        pid = p.id
    executor.submit(quick_run, pid)
    return {"project_id": pid}


def _quick_dict(s, p: Project) -> dict:
    q = p.quick or {}
    out = {"project_id": p.id, "state": q.get("state"), "step": q.get("step"), "format": q.get("format"),
           "length": q.get("length"), "title": p.title, "channel": p.channel, "video_id": p.video_id, "clip": None,
           "clip_count": len(q.get("clip_ids") or ([q["clip_id"]] if q.get("clip_id") else [])),
           "dest_name": None}
    if q.get("clip_id"):
        c = s.get(Clip, q["clip_id"])
        if c:
            d = s.get(Destination, c.destination_id) if c.destination_id else None
            out["dest_name"] = d.name if d else None
            out["clip"] = {"id": c.id, "status": c.status, "error": c.error, "youtube_id": c.youtube_id,
                           "publish_at": (c.publish_at.isoformat() + "Z") if c.publish_at else None,
                           "thumb": bool(c.thumb_path), "thumb_note": c.thumb_note, "start": c.start, "end": c.end}
    return out


@app.get("/api/quick")
def quick_list():
    with SessionLocal() as s:
        rows = s.query(Project).filter(Project.quick.isnot(None)).order_by(Project.id.desc()).limit(8).all()
        return [_quick_dict(s, p) for p in rows if p.quick]


@app.get("/api/quick/{pid}")
def quick_status(pid: int):
    with SessionLocal() as s:
        return _quick_dict(s, _get(s, Project, pid))


# ---------- thumbnails ----------
class ThumbIn(BaseModel):
    text: str = Field("", max_length=80)
    face: bool | None = None


def _rethumb(dest_id: int | None):
    """A face was added/changed: remake the automatic thumbnails of clips that are not on YouTube yet."""
    with SessionLocal() as s:
        q = s.query(Clip).filter(Clip.status.in_(["rendered", "queued"]), Clip.thumb_custom.isnot(True))
        if dest_id:
            q = q.filter(Clip.destination_id == dest_id)
        for c in q.all():
            if c.output_path and Path(c.output_path).exists():
                try:
                    c.thumb_path, c.thumb_note = str(make_thumbnail(c, s.get(Project, c.project_id))), None
                except Exception as e:  # noqa: BLE001
                    c.thumb_note = f"Thumbnail could not be made: {str(e)[:150]}"
        s.commit()


@app.post("/api/faces/{did}")
def face_upload(did: int, file: UploadFile = File(...)):
    """did = channel id, or 0 for the default face used by every channel without its own."""
    if did:
        with SessionLocal() as s:
            _get(s, Destination, did)
    acl.need_storage()
    data = file.file.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "Photo is larger than 8 MB")
    try:
        faces.save_face(did or None, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read this photo: {str(e)[:150]}")
    executor.submit(_rethumb, did or None)
    return {"ok": True}


@app.get("/api/faces/{did}")
def face_get(did: int):
    p = faces.face_path(did) if did else faces.DEFAULT_FACE
    if not p.exists():
        raise HTTPException(404, "No face uploaded")
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.delete("/api/faces/{did}")
def face_delete(did: int):
    (faces.face_path(did) if did else faces.DEFAULT_FACE).unlink(missing_ok=True)
    executor.submit(_rethumb, did or None)
    return {"ok": True}


@app.post("/api/clips/{cid}/thumbnail")
def thumb_generate(cid: int, body: ThumbIn):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        p = _get(s, Project, c.project_id)
        if not c.output_path or not Path(c.output_path).exists():
            raise HTTPException(400, "Render the clip first")
        c.thumb_text = body.text.strip() or None
        if body.face is not None:
            c.thumb_face = body.face
        try:
            c.thumb_path, c.thumb_custom, c.thumb_note = str(make_thumbnail(c, p)), False, None
            if c.status in ("rendered", "queued", "failed", "draft"):
                apply_short_cover(c)  # the Short's opening frame shows the new thumbnail
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e)[:200])
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/thumbnail-upload")
def thumb_upload(cid: int, file: UploadFile = File(...)):
    ext = Path(file.filename or "t.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        raise HTTPException(400, "Use a JPG or PNG image")
    acl.need_storage()
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        dest = DATA_DIR / "clips" / str(cid) / ("thumb_custom" + (".png" if ext == ".png" else ".jpg"))
        _save_upload(file, dest, 2 * 1024 * 1024)
        c.thumb_path, c.thumb_custom, c.thumb_note = str(dest), True, None
        if c.status in ("rendered", "queued", "failed", "draft"):
            apply_short_cover(c)
        s.commit()
        return to_dict(c)


@app.get("/api/clips/{cid}/thumbnail")
def thumb_get(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if not c.thumb_path or not Path(c.thumb_path).exists():
            raise HTTPException(404, "No thumbnail yet")
        return FileResponse(c.thumb_path)


@app.post("/api/clips/{cid}/thumbnail/push")
def thumb_push(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if not c.youtube_id or not c.thumb_path or not Path(c.thumb_path).exists():
            raise HTTPException(400, "Upload the video first and make a thumbnail")
        youtube.push_thumbnail(c)
        s.commit()
        return to_dict(c)


@app.get("/api/youtube/status")
def yt_status():
    return {"configured": youtube.configured()}


@app.get("/api/destinations")
def list_destinations():
    with SessionLocal() as s:
        adopt_orphans(s)
        return [_dest_full(s, d) for d in s.query(Destination).order_by(Destination.id)]


@app.post("/api/destinations/{did}/main")
def destination_main(did: int):
    """New sources, quick uploads, viral picks and my own videos go to this channel from now on."""
    with SessionLocal() as s:
        d = _get(s, Destination, did)
        set_setting("main_dest", str(d.id))
        s.query(Channel).filter(Channel.kind == "viral").update({"destination_id": d.id}, synchronize_session=False)
        s.commit()  # viral picks follow the main channel
        return _dest_full(s, d)


class TakeOverIn(BaseModel):
    from_id: int | None = None  # empty = from every other channel


@app.post("/api/destinations/{did}/take-over")
def destination_take_over(did: int, body: TakeOverIn):
    """Switch channels in one press: every source and every clip that is not uploaded yet moves here; it becomes the main channel."""
    with SessionLocal() as s:
        d = _get(s, Destination, did)
        if body.from_id is not None:
            _get(s, Destination, body.from_id)
        others = [x.id for x in s.query(Destination.id) if x.id != did and (body.from_id is None or x.id == body.from_id)]
        moved = _move_work(s, others, did) if others else {"sources": 0, "clips": 0}
        set_setting("main_dest", str(d.id))
        return {**moved, "channel": _dest_full(s, d)}


@app.delete("/api/destinations/{did}")
def delete_destination(did: int, move_to: int | None = None, force: bool = False):
    """Disconnect a channel. Its sources and waiting clips move to `move_to`; with `force` (no other channel) they are
    kept and wait until a channel is connected again. Videos already on YouTube are never touched."""
    with SessionLocal() as s:
        d = _get(s, Destination, did)
        if s.query(Clip).filter(Clip.destination_id == did, Clip.status == "uploading").count():
            raise HTTPException(409, "A video is being uploaded to this channel right now — try again in a few minutes.")
        n_src = s.query(Channel).filter(Channel.destination_id == did).count()
        n_clip = s.query(Clip).filter(Clip.destination_id == did, Clip.status.notin_(["uploaded", "uploading"])).count()
        moved = {"sources": 0, "clips": 0}
        if move_to is not None:
            if move_to == did:
                raise HTTPException(400, "Choose a different channel to move the work to.")
            _get(s, Destination, move_to)
            moved = _move_work(s, [did], move_to)
        elif (n_src or n_clip) and not force:
            others = s.query(Destination).filter(Destination.id != did).count()
            raise HTTPException(409, f"{n_src} source(s) and {n_clip} clip(s) use this channel. "
                                + ("Choose the channel they should move to." if others else
                                   "Connect the new channel first, or disconnect anyway — they then wait for a new channel."))
        else:  # nothing else to move to: keep the work, it is adopted by the next channel that is connected
            s.query(Channel).filter(Channel.destination_id == did).update({"destination_id": None}, synchronize_session=False)
            s.query(Clip).filter(Clip.destination_id == did, Clip.status.notin_(["uploaded", "uploading"])) \
                .update({"destination_id": None}, synchronize_session=False)
        main = s.get(Setting, "main_dest")
        if main and main.value == str(did):
            main.value = str(move_to) if move_to is not None else ""
        s.delete(d)
        s.commit()
        adopt_orphans(s)
        return {"deleted": True, **moved, "waiting": move_to is None and bool(n_src or n_clip)}


class SourceDestIn(BaseModel):
    destination_id: int


@app.post("/api/channels/{chid}/destination")
def channel_destination(chid: int, body: SourceDestIn):
    """Which of my channels this source uploads to (its clips that are not uploaded yet move too)."""
    with SessionLocal() as s:
        ch = _get(s, Channel, chid)
        _get(s, Destination, body.destination_id)
        ch.destination_id = body.destination_id
        pids = [p.id for p in s.query(Project.id).filter(Project.channel_ref == chid)]
        if pids:
            s.query(Clip).filter(Clip.project_id.in_(pids), Clip.status.notin_(["uploaded", "uploading"])) \
                .update({"destination_id": body.destination_id}, synchronize_session=False)
        s.commit()
        return to_dict(ch)


@app.get("/api/stats")
def stats():
    with SessionLocal() as s:
        by = {k: s.query(Clip).filter(Clip.status == k).count() for k in ("draft", "rendered", "queued", "uploaded", "failed")}
        return {
            "sources": s.query(Channel).count(),
            "destinations": s.query(Destination).count(),
            "videos_analyzed": s.query(Project).count(),
            "clips": by,
        }


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static", html=True), name="static")

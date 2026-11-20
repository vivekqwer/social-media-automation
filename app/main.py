import base64
import json
import secrets
import shutil
from urllib.parse import quote, urlparse
from contextlib import asynccontextmanager
from datetime import timezone
from datetime import datetime
from pathlib import Path

from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, scheduler, workers, youtube
from .autopilot import _next_slot, run_channel
from .captions import fetch_cues
from .crypto import TokenError, decrypt, encrypt_existing
from .config import APP_PASSWORD, APP_USER, BASE_URL, COOKIES_PATH, DATA_DIR, IS_LOCAL
from .monitor import check_uploads
from .db import Alert, Base, Channel, Clip, Destination, Project, SessionLocal, Worker, engine, migrate, to_dict, utcnow
from .hot import fetch_comments, fetch_heatmap, parse_timestamps, rank_moments
from .ingest import fetch_info, friendly_error, is_cc
from .jobs import executor
from .keywords import suggest
from .metadata import auto_metadata
from .metadata import build as build_meta
from .policy import check_edit
from .quick import quick_run
from .render import run_render
from .thumbs import make_thumbnail
from .tts import VOICES, synth

MODES = {"cc", "permission", "commentary"}
# public pages Google (OAuth consent / API audit) must be able to open without the dashboard password
PUBLIC_PATHS = {
    "/healthz", "/privacy", "/terms", "/about",
    "/login", "/forgot", "/reset", "/api/login", "/api/logout", "/api/reset-password",
    "/auth/reset/start", "/auth/callback",
}
_STATIC = Path(__file__).resolve().parent.parent / "static"
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
VOICE_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".aac"}
MAX_VOICE_BYTES = 25 * 1024 * 1024


def _check_yt_url(url: str):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in YT_HOSTS:
        raise HTTPException(400, "Only youtube.com / youtu.be URLs are allowed.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    migrate()
    encrypt_existing()
    sch = scheduler.start()
    yield
    sch.shutdown(wait=False)


app = FastAPI(title="Clip Studio", lifespan=lifespan)


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
    if path in PUBLIC_PATHS or path.startswith("/api/worker/"):
        return await call_next(request)
    if not auth.auth_configured():
        if not IS_LOCAL:
            return PlainTextResponse("Set the APP_PASSWORD env var before using this app.", status_code=503)
        return await call_next(request)
    if auth.request_authenticated(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not logged in"}, status_code=401)
    return RedirectResponse("/login?next=" + quote(path), status_code=302)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


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
    if auth.rate_limited(ip):
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    if not auth.auth_configured():
        raise HTTPException(503, "No password is set on the server yet.")
    if not auth.check_login(body.username, body.password):
        auth.note_fail(ip)
        raise HTTPException(401, "Wrong username or password.")
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, auth.SESSION_COOKIE, auth.make_session(APP_USER), auth.SESSION_TTL)
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/auth/reset/start")
def reset_start():
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
def api_change_password(body: ChangePwIn):
    if not auth.check_login(APP_USER, body.old_password):
        raise HTTPException(400, "Current password is wrong.")
    err = auth.validate_new_password(body.new_password)
    if err:
        raise HTTPException(400, err)
    auth.set_password(body.new_password)
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, auth.SESSION_COOKIE, auth.make_session(APP_USER), auth.SESSION_TTL)
    return resp


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


class TtsIn(BaseModel):
    text: str
    voice: str = "hi-IN-MadhurNeural"


class MetaIn(BaseModel):
    extra_keywords: list[str] = []


class ScheduleIn(BaseModel):
    publish_at: str | None = None  # ISO datetime; null = publish now
    destination_id: int | None = None  # which of my channels to upload to


class ChannelIn(BaseModel):
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
@app.post("/api/projects")
def create_project(body: ProjectIn):
    if body.mode not in MODES:
        raise HTTPException(400, "mode must be cc, permission or commentary")
    if body.mode == "permission" and not body.permission_note.strip():
        raise HTTPException(400, "Permission mode needs a note/proof of the creator's permission.")
    _check_yt_url(body.url)
    try:
        info = fetch_info(body.url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read video: {friendly_error(e)}")
    if body.mode == "cc" and not is_cc(info["license"]):
        raise HTTPException(400, f"Not a Creative Commons video (license: {info['license'] or 'Standard YouTube'}). "
                                 "Use permission or commentary mode.")
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


@app.put("/api/clips/{cid}")
def update_clip(cid: int, body: ClipUpdate):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("rendering", "fetching", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}; wait or create a new clip.")
        render_keys = {"start", "end", "aspect", "overlays", "effects", "voice_offset", "orig_volume", "captions", "caption_style", "credit_on"}
        changed = False
        for k, v in body.model_dump(exclude_unset=True).items():
            if getattr(c, k) != v:
                changed = changed or k in render_keys
                setattr(c, k, v)
        if (c.end or 0) <= (c.start or 0):
            raise HTTPException(400, "end must be greater than start")
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
            if workers.any_approved():  # voice service refuses the server's IP: let the helper PC make it
                c.voice_text, c.tts_pending = body.text, json.dumps({"text": body.text, "voice": body.voice})
                s.commit()
                return to_dict(c)
            raise HTTPException(502, "The voice service refused this server. Start the Helper PC (My channels) "
                                     f"or upload an audio file. ({str(e)[:80]})")
        c.voice_path, c.voice_text, c.tts_pending = str(path), body.text, None
        c.status = "draft" if c.status in ("rendered", "queued", "failed") else c.status
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/captions")
def make_captions(cid: int, body: CaptionsIn):
    """Pull the video's own YouTube captions for this clip's time range (editable afterwards)."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("rendering", "fetching", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}.")
        p = _get(s, Project, c.project_id)
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
        if c.status in ("rendering", "fetching", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}.")
        p = _get(s, Project, c.project_id)
        errs = check_edit(p, c)
        if errs:
            raise HTTPException(400, " ".join(errs))
        c.status, c.error = "rendering", None
        s.commit()
    executor.submit(run_render, cid)
    return {"status": "rendering"}


@app.post("/api/clips/{cid}/refetch")
def refetch_source(cid: int):
    """Throw away the downloaded source section and fetch it again (e.g. a bad or low-quality download)."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("rendering", "fetching", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}.")
        shutil.rmtree(DATA_DIR / "clips" / str(cid) / "src", ignore_errors=True)
        c.status, c.error = "rendering", None
        s.commit()
    executor.submit(run_render, cid)
    return {"status": "rendering"}


@app.get("/api/clips/{cid}/video")
def clip_video(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if not c.output_path or not Path(c.output_path).exists():
            raise HTTPException(404, "Not rendered yet")
        return FileResponse(c.output_path, media_type="video/mp4")


@app.post("/api/clips/{cid}/metadata")
def clip_metadata(cid: int, body: MetaIn):
    with SessionLocal() as s:
        _get(s, Clip, cid)
    return auto_metadata(cid, body.extra_keywords)


@app.get("/api/keywords")
def keywords(q: str):
    return {"keywords": suggest(q)}


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
        if not c.title or not c.description:
            raise HTTPException(400, "Generate/fill title and description first")
        errs = check_edit(p, c)
        if errs:
            raise HTTPException(400, " ".join(errs))
        if body.destination_id:
            _get(s, Destination, body.destination_id)
            c.destination_id = body.destination_id
        if not c.destination_id:
            first = s.query(Destination).order_by(Destination.id).first()
            if not first:
                raise HTTPException(400, "Connect your YouTube channel first (dashboard).")
            c.destination_id = first.id
        if body.publish_at:
            try:
                dt = datetime.fromisoformat(body.publish_at.replace("Z", "+00:00"))
                c.publish_at = dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
            except (ValueError, OverflowError):
                raise HTTPException(400, "publish_at must be a valid ISO datetime")
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
        decrypt(d.token)
        out["needs_reconnect"] = False
    except TokenError:
        out["needs_reconnect"] = True  # stored login unreadable (e.g. key changed): press Connect again
    return out


def _clip_card(c: Clip, projects: dict, full: bool = False) -> dict:
    p = projects.get(c.project_id)
    d = to_dict(c)
    if not full:
        d.pop("description", None)
    d["video_id"] = p.video_id if p else None
    d["source_title"] = p.title if p else None
    d["source_channel"] = p.channel if p else None
    d["source_channel_url"] = p.channel_url if p else None
    d["mode"] = p.mode if p else None
    d["license"] = p.license if p else None
    d["duration"] = p.duration if p else None
    return d


def _cards(s, rows) -> list[dict]:
    ids = {c.project_id for c in rows}
    projects = {p.id: p for p in s.query(Project).filter(Project.id.in_(ids)).all()} if ids else {}
    return [_clip_card(c, projects) for c in rows]


@app.get("/api/clips")
def list_clips(status: str | None = None, limit: int = Query(60, ge=1, le=200)):
    with SessionLocal() as s:
        q = s.query(Clip)
        if status:
            q = q.filter(Clip.status.in_(status.split(",")))
        return _cards(s, q.order_by(Clip.id.desc()).limit(limit).all())


@app.get("/api/overview")
def overview():
    with SessionLocal() as s:
        statuses = ("draft", "fetching", "rendering", "rendered", "queued", "uploading", "uploaded", "failed")
        counts = {k: s.query(Clip).filter(Clip.status == k).count() for k in statuses}
        upcoming = s.query(Clip).filter(Clip.status.in_(["queued", "uploading"])).order_by(Clip.publish_at).limit(8).all()
        recent = s.query(Clip).filter(Clip.status == "uploaded").order_by(Clip.id.desc()).limit(6).all()
        failed = s.query(Clip).filter(Clip.status == "failed").order_by(Clip.id.desc()).limit(5).all()
        review = s.query(Clip).filter(Clip.status == "rendered").order_by(Clip.id.desc()).limit(6).all()
        return {
            "counts": counts,
            "sources": s.query(Channel).count(),
            "videos_analyzed": s.query(Project).count(),
            "destinations": [_dest_dict(d) for d in s.query(Destination).order_by(Destination.id)],
            "upcoming": _cards(s, upcoming),
            "recent": _cards(s, recent),
            "failed": _cards(s, failed),
            "review": _cards(s, review),
            "youtube_configured": youtube.configured(),
            "alerts_unseen": s.query(Alert).filter(Alert.seen.is_(False)).count(),
        }


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
        if not c.destination_id:
            first = s.query(Destination).order_by(Destination.id).first()
            if not first:
                raise HTTPException(400, "Connect your YouTube channel first.")
            c.destination_id = first.id
        if body.publish_at:
            try:
                dt = datetime.fromisoformat(body.publish_at.replace("Z", "+00:00"))
                c.publish_at = dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
            except (ValueError, OverflowError):
                raise HTTPException(400, "publish_at must be a valid ISO datetime")
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
    if body.mode not in ("cc", "permission"):
        raise HTTPException(400, "Autopilot supports cc or permission mode only (commentary needs manual edit).")
    if body.mode == "permission" and not body.permission_note.strip():
        raise HTTPException(400, "Permission mode needs a note/proof of the creator's permission.")
    body.query = body.query.strip()
    if not body.query:
        raise HTTPException(400, "Channel name/URL is empty")
    if body.query.startswith("http"):
        _check_yt_url(body.query)
    with SessionLocal() as s:
        if body.destination_id:
            _get(s, Destination, body.destination_id)
        else:
            first = s.query(Destination).order_by(Destination.id).first()
            if not first:
                raise HTTPException(400, "Connect your own YouTube channel first (dashboard), then add sources.")
            body.destination_id = first.id
        ch = Channel(**body.model_dump())
        s.add(ch)
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
@app.get("/auth/login")
def auth_login():
    if not youtube.configured():
        raise HTTPException(400, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.")
    return RedirectResponse(youtube.login_url())


@app.get("/auth/callback")
def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if state and state.startswith("reset:"):  # password recovery, verified through Google
        if error or not code:
            return RedirectResponse("/forgot?err=" + quote("Google sign-in was cancelled."), status_code=302)
        try:
            email = youtube.handle_reset_callback(code, state)
        except Exception as e:  # noqa: BLE001
            return RedirectResponse("/forgot?err=" + quote(str(e)[:150]), status_code=302)
        if email.lower() not in auth.allowed_reset_emails():
            return RedirectResponse("/forgot?err=" + quote("That Google account is not allowed to reset this password."), status_code=302)
        resp = RedirectResponse("/reset", status_code=302)
        _set_cookie(resp, auth.RESET_COOKIE, auth.make_reset_token(email), auth.RESET_TTL)
        return resp
    # connecting a YouTube channel: only for a logged-in dashboard user
    if auth.auth_configured() and not auth.request_authenticated(request):
        return RedirectResponse("/login?next=/", status_code=302)
    if error or not code or not state:
        return RedirectResponse("/", status_code=302)
    try:
        youtube.handle_callback(code, state)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"OAuth failed: {str(e)[:200]}")
    return RedirectResponse("/")


def _cookie_status() -> dict:
    if not COOKIES_PATH.exists():
        return {"saved": False}
    txt = COOKIES_PATH.read_text(encoding="utf-8", errors="ignore")
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
    COOKIES_PATH.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n", encoding="utf-8")
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


@app.post("/api/worker/register")
def worker_register(body: WorkerRegIn, request: Request):
    ip = _client_ip(request)
    if auth.rate_limited(ip):
        raise HTTPException(429, "Too many attempts")
    auth.note_fail(ip)  # registrations count towards the same limiter
    with SessionLocal() as s:
        if s.query(Worker).filter(Worker.approved.is_(False)).count() >= 5:
            raise HTTPException(429, "Too many helpers waiting for approval")
        tok = secrets.token_urlsafe(32)
        w = Worker(name=body.name.strip() or "helper", token_hash=workers.token_hash(tok), approved=False, last_seen=utcnow())
        s.add(w)
        s.commit()
        return {"worker_id": w.id, "token": tok}


@app.get("/api/worker/jobs")
def worker_jobs(request: Request):
    _worker_or_401(request)
    jobs = []
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.status == "fetching").order_by(Clip.id).limit(3):
            p = s.get(Project, c.project_id)
            jobs.append({"type": "source", "clip_id": c.id, "video_id": p.video_id, "start": c.start, "end": c.end})
        for c in s.query(Clip).filter(Clip.tts_pending.isnot(None), Clip.tts_pending != "").order_by(Clip.id).limit(5):
            t = json.loads(c.tts_pending)
            jobs.append({"type": "tts", "clip_id": c.id, "text": t.get("text", ""), "voice": t.get("voice", "")})
        for p in s.query(Project).filter(Project.analysis_pending.is_(True)).order_by(Project.id).limit(2):
            jobs.append({"type": "analyze", "project_id": p.id, "video_id": p.video_id,
                         "captions": bool((p.quick or {}).get("captions", True))})
    return {"jobs": jobs}


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
        dest = DATA_DIR / "clips" / str(cid) / "src" / f"src_{int(c.start)}_{int(c.end) + 1}.mp4"
        _save_upload(file, dest, 800 * 1024 * 1024)
        c.status = "rendering"
        s.commit()
    executor.submit(run_render, cid)
    return {"ok": True}


@app.post("/api/worker/voice/{cid}")
def worker_voice(cid: int, request: Request, file: UploadFile = File(...)):
    _worker_or_401(request)
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        dest = DATA_DIR / "clips" / str(cid) / "voice.mp3"
        _save_upload(file, dest, MAX_VOICE_BYTES)
        c.voice_path, c.tts_pending = str(dest), None
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
            q = dict(p.quick or {})
            q["helper_analysed"] = True
            p.quick = q
            s.commit()
        executor.submit(quick_run, cid)
        return {"ok": True}
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if body.kind == "tts":
            c.tts_pending = None
            c.error = "Voice-over failed on the helper PC: " + body.error[:300]
        elif c.status == "fetching":
            c.status, c.error = "failed", body.error[:600] or "The helper PC could not download this video."
        s.commit()
    return {"ok": True}


@app.get("/api/workers")
def list_workers():
    with SessionLocal() as s:
        return [workers.as_dict(w) for w in s.query(Worker).order_by(Worker.id)]


@app.post("/api/workers/{wid}/approve")
def approve_worker(wid: int):
    with SessionLocal() as s:
        w = _get(s, Worker, wid)
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
        fmt = (p.quick or {}).get("format", "shorts")
        want = min({"shorts": 35, "video": 60}[fmt], p.duration or 35)
        if body.heatmap:
            p.heatmap = body.heatmap
        secs = parse_timestamps([str(t)[:2000] for t in body.comments], p.duration)
        p.moments = rank_moments(p.duration, p.heatmap or [], secs, want, 3)
        if body.caption_track:
            p.caption_track = body.caption_track
        p.analysis_pending = False
        q = dict(p.quick or {})
        q["helper_analysed"] = True
        p.quick = q
        s.commit()
        is_quick = bool(p.quick)
    if is_quick:
        executor.submit(quick_run, pid)
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
    try:
        info = fetch_info(body.url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read video: {friendly_error(e)}")
    if body.mode == "cc" and not is_cc(info["license"]):
        raise HTTPException(409, {"code": "not_cc", "license": info["license"] or "Standard YouTube License",
                                  "title": info["title"], "channel": info["channel"]})
    if body.mode == "permission" and not body.permission_note.strip():
        raise HTTPException(400, "Write how you got the creator's permission.")
    if body.length == "whole" and info["duration"] > 4 * 3600:
        raise HTTPException(400, "That video is longer than 4 hours.")
    with SessionLocal() as s:
        p = Project(url=body.url, mode=body.mode, permission_note=body.permission_note,
                    quick={"format": body.format, "length": body.length, "publish_at": body.publish_at,
                           "captions": body.captions, "destination_id": body.destination_id,
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


@app.post("/api/clips/{cid}/thumbnail")
def thumb_generate(cid: int, body: ThumbIn):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        p = _get(s, Project, c.project_id)
        if not c.output_path or not Path(c.output_path).exists():
            raise HTTPException(400, "Render the clip first")
        c.thumb_text = body.text.strip() or None
        try:
            c.thumb_path, c.thumb_custom, c.thumb_note = str(make_thumbnail(c, p)), False, None
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e)[:200])
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/thumbnail-upload")
def thumb_upload(cid: int, file: UploadFile = File(...)):
    ext = Path(file.filename or "t.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        raise HTTPException(400, "Use a JPG or PNG image")
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        dest = DATA_DIR / "clips" / str(cid) / ("thumb_custom" + (".png" if ext == ".png" else ".jpg"))
        _save_upload(file, dest, 2 * 1024 * 1024)
        c.thumb_path, c.thumb_custom, c.thumb_note = str(dest), True, None
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
        try:
            youtube.set_thumbnail(c.destination_id, c.youtube_id, c.thumb_path)
            c.thumb_note = "Thumbnail set on YouTube"
        except Exception as e:  # noqa: BLE001
            c.thumb_note = youtube.thumb_error_text(e)
        s.commit()
        return to_dict(c)


@app.get("/api/youtube/status")
def yt_status():
    return {"configured": youtube.configured()}


@app.get("/api/destinations")
def list_destinations():
    with SessionLocal() as s:
        return [_dest_dict(d) for d in s.query(Destination).order_by(Destination.id)]


@app.delete("/api/destinations/{did}")
def delete_destination(did: int):
    with SessionLocal() as s:
        if s.query(Channel).filter(Channel.destination_id == did).count():
            raise HTTPException(409, "Some source channels still upload to this channel; remove/reassign them first.")
        if s.query(Clip).filter(Clip.destination_id == did, Clip.status.in_(["queued", "uploading"])).count():
            raise HTTPException(409, "Clips are still queued for this channel; wait or cancel them first.")
        s.delete(_get(s, Destination, did))
        s.commit()
        return {"deleted": True}


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

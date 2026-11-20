import base64
import secrets
import shutil
from urllib.parse import urlparse
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

from . import scheduler, youtube
from .autopilot import _next_slot, run_channel
from .captions import fetch_cues
from .config import APP_PASSWORD, APP_USER, DATA_DIR, IS_LOCAL
from .db import Base, Channel, Clip, Destination, Project, SessionLocal, engine, migrate, to_dict, utcnow
from .hot import fetch_comments, parse_timestamps, rank_moments
from .ingest import fetch_info, is_cc
from .jobs import executor
from .keywords import suggest
from .metadata import auto_metadata
from .metadata import build as build_meta
from .policy import check_edit
from .render import run_render
from .tts import VOICES, synth

MODES = {"cc", "permission", "commentary"}
# public pages Google (OAuth consent / API audit) must be able to open without the dashboard password
PUBLIC_PATHS = {"/healthz", "/privacy", "/terms", "/about"}
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
    sch = scheduler.start()
    yield
    sch.shutdown(wait=False)


app = FastAPI(title="Clip Studio", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # default handler echoes the raw input, which crashes on NaN/inf; return only location + message
    errs = [{"loc": [str(x) for x in e["loc"]], "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": errs}, status_code=422)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):  # CSRF: browsers re-send cached Basic auth cross-site
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return PlainTextResponse("Cross-origin request blocked", status_code=403)
    if not APP_PASSWORD:
        if not IS_LOCAL:
            return PlainTextResponse("Set the APP_PASSWORD env var before using this app.", status_code=503)
        return await call_next(request)
    ok = False
    hdr = request.headers.get("authorization", "")
    if hdr.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
            ok = secrets.compare_digest(user.encode(), APP_USER.encode()) and secrets.compare_digest(
                pw.encode(), APP_PASSWORD.encode()
            )
        except Exception:  # noqa: BLE001
            ok = False
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="clipstudio"'})
    return await call_next(request)


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


class ApproveIn(BaseModel):
    publish_at: str | None = None


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
        raise HTTPException(400, f"Could not read video: {str(e)[:300]}")
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
        c = Clip(project_id=pid, start=body.start, end=body.end, effects=["fade"], overlays=[])
        s.add(c)
        s.commit()
        return to_dict(c)


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
        if c.status in ("rendering", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}; wait or create a new clip.")
        render_keys = {"start", "end", "aspect", "overlays", "effects", "voice_offset", "orig_volume", "captions", "caption_style"}
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
            raise HTTPException(502, f"TTS failed: {str(e)[:200]}")
        c.voice_path, c.voice_text = str(path), body.text
        c.status = "draft" if c.status in ("rendered", "queued", "failed") else c.status
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/captions")
def make_captions(cid: int, body: CaptionsIn):
    """Pull the video's own YouTube captions for this clip's time range (editable afterwards)."""
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("rendering", "uploading", "uploaded"):
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
        if c.status in ("rendering", "uploading", "uploaded"):
            raise HTTPException(409, f"Clip is {c.status}.")
        p = _get(s, Project, c.project_id)
        errs = check_edit(p, c)
        if errs:
            raise HTTPException(400, " ".join(errs))
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
        statuses = ("draft", "rendering", "rendered", "queued", "uploading", "uploaded", "failed")
        counts = {k: s.query(Clip).filter(Clip.status == k).count() for k in statuses}
        upcoming = s.query(Clip).filter(Clip.status.in_(["queued", "uploading"])).order_by(Clip.publish_at).limit(8).all()
        recent = s.query(Clip).filter(Clip.status == "uploaded").order_by(Clip.id.desc()).limit(6).all()
        failed = s.query(Clip).filter(Clip.status == "failed").order_by(Clip.id.desc()).limit(5).all()
        review = s.query(Clip).filter(Clip.status == "rendered").order_by(Clip.id.desc()).limit(6).all()
        return {
            "counts": counts,
            "sources": s.query(Channel).count(),
            "videos_analyzed": s.query(Project).count(),
            "destinations": [to_dict(d) for d in s.query(Destination).order_by(Destination.id)],
            "upcoming": _cards(s, upcoming),
            "recent": _cards(s, recent),
            "failed": _cards(s, failed),
            "review": _cards(s, review),
            "youtube_configured": youtube.configured(),
        }


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
def auth_callback(code: str, state: str):
    try:
        youtube.handle_callback(code, state)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"OAuth failed: {str(e)[:200]}")
    return RedirectResponse("/")


@app.get("/api/youtube/status")
def yt_status():
    return {"configured": youtube.configured()}


@app.get("/api/destinations")
def list_destinations():
    with SessionLocal() as s:
        return [to_dict(d) for d in s.query(Destination).order_by(Destination.id)]


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

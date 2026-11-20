import base64
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import timezone
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import scheduler, youtube
from .autopilot import run_channel
from .config import APP_PASSWORD, APP_USER, DATA_DIR, IS_LOCAL
from .db import Base, Channel, Clip, Destination, Project, SessionLocal, engine, to_dict, utcnow
from .hot import fetch_comments, parse_timestamps, rank_moments
from .ingest import fetch_info, is_cc
from .jobs import executor
from .keywords import suggest
from .metadata import auto_metadata
from .policy import check_edit
from .render import run_render
from .tts import VOICES, synth

MODES = {"cc", "permission", "commentary"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    sch = scheduler.start()
    yield
    sch.shutdown(wait=False)


app = FastAPI(title="Clip Studio", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
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


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- schemas ----------
class ProjectIn(BaseModel):
    url: str
    mode: str = "cc"
    permission_note: str = ""


class ClipIn(BaseModel):
    start: float
    end: float


class ClipUpdate(BaseModel):
    start: float | None = None
    end: float | None = None
    aspect: str | None = None
    overlays: list[dict] | None = None
    effects: list[str] | None = None
    voice_text: str | None = None
    voice_offset: float | None = None
    orig_volume: float | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class TtsIn(BaseModel):
    text: str
    voice: str = "hi-IN-MadhurNeural"


class MetaIn(BaseModel):
    extra_keywords: list[str] = []


class ScheduleIn(BaseModel):
    publish_at: str | None = None  # ISO datetime; null = publish now
    destination_id: int | None = None  # which of my channels to upload to


class ChannelIn(BaseModel):
    query: str
    mode: str = "cc"
    permission_note: str = ""
    auto: bool = True
    videos_per_run: int = 3
    clips_per_video: int = 2
    clip_len: int = 30
    aspect: str = "9:16"
    gap_hours: int = 12
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
def analyze(pid: int, clip_len: int = 30):
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
        return to_dict(_get(s, Clip, cid))


@app.put("/api/clips/{cid}")
def update_clip(cid: int, body: ClipUpdate):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status in ("uploading", "uploaded"):
            raise HTTPException(409, "Clip already uploaded")
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(c, k, v)
        if any(k in body.model_fields_set for k in ("start", "end", "aspect", "overlays", "effects", "voice_offset", "orig_volume")):
            c.status = "draft" if c.status in ("rendered", "queued", "failed") else c.status
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


@app.post("/api/clips/{cid}/voice-upload")
def voice_upload(cid: int, file: UploadFile = File(...)):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        folder = DATA_DIR / "clips" / str(cid)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("voice" + (Path(file.filename or "v.mp3").suffix or ".mp3"))
        with open(path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        c.voice_path = str(path)
        c.status = "draft" if c.status in ("rendered", "queued", "failed") else c.status
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/render")
def render(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
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
            dt = datetime.fromisoformat(body.publish_at.replace("Z", "+00:00"))
            c.publish_at = dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
        else:
            c.publish_at = None
        c.status, c.error = "queued", None
        s.commit()
        return to_dict(c)


@app.post("/api/clips/{cid}/unschedule")
def unschedule(cid: int):
    with SessionLocal() as s:
        c = _get(s, Clip, cid)
        if c.status == "queued":
            c.status = "rendered"
            s.commit()
        return to_dict(c)


@app.get("/api/queue")
def queue():
    with SessionLocal() as s:
        rows = s.query(Clip).filter(Clip.status.in_(["queued", "uploading", "uploaded", "failed"])).order_by(Clip.id.desc()).limit(50)
        return [to_dict(c) for c in rows]


# ---------- autopilot channels ----------
@app.post("/api/channels")
def add_channel(body: ChannelIn):
    if body.mode not in ("cc", "permission"):
        raise HTTPException(400, "Autopilot supports cc or permission mode only (commentary needs manual edit).")
    if body.mode == "permission" and not body.permission_note.strip():
        raise HTTPException(400, "Permission mode needs a note/proof of the creator's permission.")
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

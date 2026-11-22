from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from . import tenant
from .config import BASE_DATA_DIR, DATABASE_URL


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


IS_SQLITE = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if IS_SQLITE else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)  # tenant 1 + the shared tables
Base = declarative_base()  # the tables every customer has a private copy of
_engines: dict[int, object] = {tenant.OWNER: engine}
_makers: dict[int, object] = {}
_ready: set[int] = set()
_ensure_lock = __import__("threading").Lock()


def engine_for(tenant_id: int | None = None):
    """The database of one customer. PostgreSQL: the same server, the customer's own schema. SQLite: own file."""
    tid = tenant.current() if tenant_id is None else int(tenant_id)
    eng = _engines.get(tid)
    if eng is None:
        if IS_SQLITE:
            path = tenant.root(tid) / "app.db"
            eng = create_engine(f"sqlite:///{path.as_posix()}", connect_args=connect_args, pool_pre_ping=True)
        else:  # every table name without a schema is looked up in this customer's schema
            eng = engine.execution_options(schema_translate_map={None: tenant.schema(tid)})
        _engines[tid] = eng
    return eng


def tenant_exists(tenant_id: int) -> bool:
    """Is there a private copy for this customer at all? (Never creates one.)"""
    tid = int(tenant_id)
    if tid == tenant.OWNER or tid in _ready:
        return True
    if IS_SQLITE:
        return (tenant.root(tid) / "app.db").exists() if (tenant._base_dir / "tenants" / str(tid)).exists() else False
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
                            {"s": tenant.schema(tid)}).first() is not None


def ensure_tenant(tenant_id: int):
    """Create (or bring up to date) a customer's private tables. Safe to call again and again."""
    tid = int(tenant_id)
    if tid in _ready:
        return
    with _ensure_lock:
        if tid in _ready:
            return
        if tid != tenant.OWNER and not IS_SQLITE:
            from sqlalchemy import text

            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{tenant.schema(tid)}"'))
        Base.metadata.create_all(engine_for(tid))
        migrate(tid)
        _ready.add(tid)


class _Sessions:
    """`SessionLocal()` — a database session for whichever customer the running code is working for."""

    def __call__(self, **kw):
        tid = tenant.current()
        mk = _makers.get(tid)
        if mk is None:
            if tid != tenant.OWNER:
                ensure_tenant(tid)
            mk = _makers[tid] = sessionmaker(bind=engine_for(tid), expire_on_commit=False)
        return mk(**kw)


SessionLocal = _Sessions()


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    url = Column(String(500))
    video_id = Column(String(32), index=True)
    title = Column(String(500))
    channel = Column(String(300))
    channel_url = Column(String(500))
    license = Column(String(200))
    description = Column(Text)
    tags = Column(JSON, default=list)
    duration = Column(Integer, default=0)
    heatmap = Column(JSON, default=list)
    moments = Column(JSON, default=list)
    mode = Column(String(20))  # cc | permission | commentary | original (my own ReelGen reel)
    permission_note = Column(Text)
    channel_ref = Column(Integer, index=True)  # set when created by autopilot
    quick = Column(JSON)  # Quick upload settings + progress {format, length, publish_at, captions, state, step, clip_id}
    caption_track = Column(JSON)  # whole json3 caption track (from the helper PC), sliced per clip
    analysis_pending = Column(Boolean, default=False)  # waiting for the helper PC to read replays/comments
    analysis_since = Column(DateTime)
    analysis_lease = Column(DateTime)
    analysis_tries = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)


class Clip(Base):
    __tablename__ = "clips"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), index=True)
    start = Column(Float)
    end = Column(Float)
    aspect = Column(String(8), default="16:9")
    overlays = Column(JSON, default=list)
    effects = Column(JSON, default=list)
    voice_text = Column(Text)
    voice_path = Column(String(500))
    voice_offset = Column(Float, default=0.0)
    orig_volume = Column(Float, default=1.0)
    title = Column(String(200))
    description = Column(Text)
    tags = Column(JSON, default=list)
    kw = Column(JSON)  # keyword helper: what was chosen, from where and when {at, lead, net, note, items: [{k, src, hot, used}]}
    meta_edited = Column(Boolean)  # the owner changed title/description/tags by hand: the keyword helper leaves them alone
    publish_at = Column(DateTime)  # UTC, naive
    destination_id = Column(Integer)
    attempted_at = Column(DateTime)  # last upload attempt (quota accounting)
    autoqueue = Column(Boolean, default=False)  # re-queue automatically after a re-render
    captions = Column(JSON, default=list)  # [{s, e, t}] relative to clip start
    caption_style = Column(JSON)  # {on, lang, pos, size, color}
    tts_pending = Column(Text)  # JSON {text, voice} waiting for the helper PC
    thumb_text = Column(String(120))  # big text on the auto thumbnail (empty = from title)
    thumb_path = Column(String(500))
    thumb_custom = Column(Boolean, default=False)  # user uploaded their own image
    thumb_note = Column(Text)  # result of setting the thumbnail on YouTube
    thumb_pending = Column(Boolean, default=False)  # YouTube refused it; retried automatically until it works
    short_cover = Column(Boolean, default=True)  # Shorts: show the thumbnail in the first moment of the video
    reframe = Column(Boolean, default=True)  # Shorts: crop follows the person instead of the centre
    fit = Column(String(8))  # Shorts/square from a wide video: full (whole picture, blurred background; default) | wide | fill
    voice_fit = Column(String(8))  # speed = the voice-over is sped up/slowed down to end exactly with the clip
    frame_title = Column(Boolean)  # Shorts: title + small face at the top of every frame (None/True = on)
    cta_on = Column(Boolean)  # small "like and subscribe" reminder near the end of the video (None/True = on)
    reuploaded_as = Column(Integer)  # this video was deleted from YouTube and uploaded again as that clip
    removed_at = Column(DateTime)  # deleted from YouTube and removed from the dashboard by the owner (kept as history)
    live_noted = Column(Boolean)  # the owner was told this video went live
    meta_done = Column(Boolean, default=False)  # also posted to Instagram / Facebook
    meta_result = Column(JSON)
    thumb_variant = Column(Integer, default=0)  # 1 = second thumbnail tried automatically because views were low
    fetch_since = Column(DateTime)  # waiting for the helper PC since
    fetch_tries = Column(Integer, default=0)  # times a helper took the download job
    job_lease = Column(DateTime)  # a helper took this clip's job at; others wait until it expires
    credit_on = Column(Boolean, default=True)  # small 'Credit: channel' text on the video
    thumb_face = Column(Boolean, default=True)  # put my face (channel's or default) on the thumbnail
    yt_state = Column(String(60))  # last state seen on YouTube: ok | removed | rejected:<reason> | blocked:<n> ...
    yt_checked_at = Column(DateTime)
    # draft | rendering | rendered | queued | uploading | uploaded | failed
    status = Column(String(20), default="draft")
    output_path = Column(String(500))
    youtube_id = Column(String(32))
    error = Column(Text)
    created_at = Column(DateTime, default=utcnow)


class Channel(Base):
    """Autopilot source: give a channel name/URL, the system pulls + clips + schedules by itself."""

    __tablename__ = "channels"
    id = Column(Integer, primary_key=True)
    kind = Column(String(10), default="channel")  # channel | topic (CC search) | reel (ReelGen) | trending (CC on trending topics)
    interval_hours = Column(Integer)  # how often this source runs (empty = AUTOPILOT_INTERVAL_HOURS)
    scripts = Column(JSON, default=list)  # reel sources: [{"text": ..., "used": bool}]
    voice = Column(String(60))  # reel sources: edge-tts voice (empty = from the script's language)
    query = Column(String(300))  # name, @handle or URL as typed by user; keywords for a topic
    resolved_url = Column(String(500))
    name = Column(String(300))
    mode = Column(String(20))  # cc | permission  (commentary needs manual edit -> drafts only)
    permission_note = Column(Text)
    auto = Column(Boolean, default=True)
    videos_per_run = Column(Integer, default=3)
    clips_per_video = Column(Integer, default=2)
    clip_len = Column(Integer, default=30)
    aspect = Column(String(8), default="9:16")
    gap_hours = Column(Integer, default=12)  # spacing between scheduled uploads
    destination_id = Column(Integer)  # which of my channels receives the uploads
    captions = Column(Boolean, default=False)  # add YouTube captions to every clip
    post_times = Column(String(100))  # daily upload times, e.g. '10:00,18:30' (user's local time)
    tz_offset = Column(Integer, default=0)  # minutes, JS getTimezoneOffset(): utc = local + offset
    review_first = Column(Boolean, default=False)  # True: clips wait for my approval instead of auto-queueing
    seen = Column(JSON, default=list)
    seen_topics = Column(JSON, default=list)  # trending sources: topics already used
    auto_scripts = Column(Boolean, default=False)  # reel source: the helper's local AI writes scripts on trending topics
    script_wanted = Column(JSON)  # {"topic":..., "since":...} waiting for the helper's local AI
    last_run = Column(DateTime)
    last_status = Column(Text)
    created_at = Column(DateTime, default=utcnow)


class Destination(Base):
    """One of the user's own YouTube channels (OAuth-connected upload target)."""

    __tablename__ = "destinations"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String(64), unique=True)
    name = Column(String(300))
    token = Column(Text)
    paused = Column(Boolean, default=False)  # uploads held (set automatically when YouTube removes/blocks a video)
    paused_reason = Column(Text)
    paused_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)


class Worker(Base):
    """A helper PC that downloads video sections / makes voice-overs where YouTube doesn't block."""

    __tablename__ = "workers"
    id = Column(Integer, primary_key=True)
    name = Column(String(120))
    token_hash = Column(String(64), unique=True)
    approved = Column(Boolean, default=False)
    pair_hash = Column(String(64))  # hash of the pairing code the helper shows; needed to approve it
    llm = Column(Boolean, default=False)  # a local AI (Ollama) is running on this helper PC
    ai_status = Column(String(200))  # what the helper is doing to set the local AI up (shown on the dashboard)
    ip = Column(String(64))
    last_seen = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)


class Alert(Base):
    """Something happened to an uploaded video on YouTube (deleted, rejected for copyright, blocked...)."""

    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    clip_id = Column(Integer, index=True)
    kind = Column(String(30))  # removed | rejected | failed | blocked | restored
    message = Column(Text)
    details = Column(JSON)  # video title/link, my channel, source creator, upload time
    seen = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)


class ChannelStat(Base):
    """Snapshot of one of my channels (synced from YouTube every 15 minutes; kept for growth charts)."""

    __tablename__ = "channel_stats"
    id = Column(Integer, primary_key=True)
    destination_id = Column(Integer, index=True)
    at = Column(DateTime, default=utcnow, index=True)
    subscribers = Column(Integer)
    subscribers_hidden = Column(Boolean, default=False)
    views = Column(Integer)
    videos = Column(Integer)


class VideoStat(Base):
    """Latest numbers for each video on my channels (all videos, not only the ones this app uploaded)."""

    __tablename__ = "video_stats"
    video_id = Column(String(32), primary_key=True)
    destination_id = Column(Integer, index=True)
    title = Column(String(300))
    published_at = Column(DateTime)
    privacy = Column(String(20))
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    views_prev = Column(Integer)  # views at the previous sync (for "new views" right now)
    updated_at = Column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text)


def to_dict(obj) -> dict:
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        if c.name == "token":
            continue  # never expose OAuth tokens
        out[c.name] = (v.isoformat() + "Z") if isinstance(v, datetime) else v
    return out


def main_destination(s):
    """The channel new work goes to unless a source says otherwise (chosen in My channels; else the first connected)."""
    row = s.get(Setting, "main_dest")
    d = s.get(Destination, int(row.value)) if row and (row.value or "").isdigit() else None
    return d or s.query(Destination).order_by(Destination.id).first()


def adopt_orphans(s) -> dict:
    """Sources and not-yet-uploaded clips whose channel is missing (never set, or disconnected) go to the main channel.
    Called after a channel is connected/removed and before uploads, so nothing stays stuck."""
    main = main_destination(s)
    if not main:
        return {"sources": 0, "clips": 0}
    ids = [d.id for d in s.query(Destination.id)]
    src = s.query(Channel).filter((Channel.destination_id.is_(None)) | (Channel.destination_id.notin_(ids))).all()
    clips = s.query(Clip).filter(Clip.status.notin_(["uploaded", "uploading"]),
                                 (Clip.destination_id.is_(None)) | (Clip.destination_id.notin_(ids))).all()
    for x in src + clips:
        x.destination_id = main.id
    if src or clips:
        s.commit()
    return {"sources": len(src), "clips": len(clips)}


def get_setting(key: str):
    with SessionLocal() as s:
        row = s.get(Setting, key)
        return row.value if row else None


def set_setting(key: str, value: str):
    with SessionLocal() as s:
        s.merge(Setting(key=key, value=value))
        s.commit()


def migrate(tenant_id: int = tenant.OWNER, metadata=None):
    """Tiny auto-migration: add columns that exist in the models but not in the database yet."""
    from sqlalchemy import inspect, text

    tid = int(tenant_id)
    eng = engine_for(tid)
    sch = None if IS_SQLITE else tenant.schema(tid)  # raw SQL is not translated: name the schema ourselves
    insp = inspect(engine if not IS_SQLITE else eng)
    for table in (metadata or Base.metadata).sorted_tables:
        if not insp.has_table(table.name, schema=sch):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name, schema=sch)}
        for col in table.columns:
            if col.name not in existing:
                ddl = col.type.compile(engine.dialect)
                full = f'"{sch}".{table.name}' if sch else table.name
                with (engine if not IS_SQLITE else eng).begin() as conn:
                    conn.execute(text(f"ALTER TABLE {full} ADD COLUMN {col.name} {ddl}"))

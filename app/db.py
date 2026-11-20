from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


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
    mode = Column(String(20))  # cc | permission | commentary
    permission_note = Column(Text)
    channel_ref = Column(Integer, index=True)  # set when created by autopilot
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
    publish_at = Column(DateTime)  # UTC, naive
    destination_id = Column(Integer)
    attempted_at = Column(DateTime)  # last upload attempt (quota accounting)
    autoqueue = Column(Boolean, default=False)  # re-queue automatically after a re-render
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
    query = Column(String(300))  # name, @handle or URL as typed by user
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
    review_first = Column(Boolean, default=False)  # True: clips wait for my approval instead of auto-queueing
    seen = Column(JSON, default=list)
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
    created_at = Column(DateTime, default=utcnow)


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


def get_setting(key: str):
    with SessionLocal() as s:
        row = s.get(Setting, key)
        return row.value if row else None


def set_setting(key: str, value: str):
    with SessionLocal() as s:
        s.merge(Setting(key=key, value=value))
        s.commit()


def migrate():
    """Tiny auto-migration: add columns that exist in the models but not in the database yet."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                ddl = col.type.compile(engine.dialect)
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}"))

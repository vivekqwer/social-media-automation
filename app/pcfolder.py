"""Videos in a folder on the owner's PC (default D:\\youtube) -> the "From my PC" list -> YouTube, without filling the
server's small disk.

1. The helper watches its folder (a setting per helper, see `folder_for`). Every video in it — one per sub-folder
   (`<NNN>_<title>\\video.mp4` with thumbnail.jpg/png, description.txt and info.json) or a loose .mp4/.mov/.mkv — is
   reported once, keyed by its sha256, when its size has not changed for 60 seconds. Only the details and a small
   picture come to the server, never the video. A moved or renamed file is the same item; a deleted one is marked
   "missing on the PC".
2. Each item waits in Clips -> From my PC for Approve (or Skip). Approve gives it the next free daily slot (one a day,
   18:00 India time, after the last scheduled day — or the info.json date when that is later and free) and makes it
   a clip in status "queued" with title, description, tags and thumbnail from info.json / a topic / the file name.
   The account switch "Videos I add to the PC folder go up by themselves" (pc_auto, ON unless switched off) approves
   new items by itself; an info.json with "approve": "manual" always waits.
3. The file comes to the server only shortly before its turn (`due`): about a day before the upload rules would send
   it, at most MAX_FILES of them at once, never while the disk has less than MIN_FREE_GB free. The helper sends it
   (sha256 checked); the server uploads it with the slot as publishAt and deletes its copy. It was on the PC all along,
   so the clip counts as saved on the PC (no archive job). If the PC is off, it keeps asking; once the publish time has
   passed the clip moves to the next free day and says so (clip + Problems list) — never a late upload without a time.

Rules that never change:
* A helper reports and serves only its own account's folder: every helper request runs in the copy its token belongs
  to, and a job ticket (the central helper doing another account's download) never opens these addresses.
* A creator on the never-use list after a strike (app/strikes.py) is never approved.
* Loose files are the owner's own video ("Original content (my own video)"); items whose info.json names a licence or
  source keep their credit lines.
"""
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func

from . import quota, tenant
from .config import DATA_DIR
from .db import Clip, Destination, PcItem, Project, SessionLocal, Worker, get_setting, main_destination, set_setting, utcnow

log = logging.getLogger(__name__)

DEFAULT_FOLDER = r"D:\youtube"  # the owner's PC; customers choose their own folder in My channels -> Helper PC
MAX_FILES = int(os.getenv("PC_MAX_FILES") or "5")  # PC videos on the server at the same time (waiting for their upload)
MIN_FREE_GB = 1.5  # a PC video is never asked for while the server's disk has less free space than this
MAX_BYTES = 900 * 1024 * 1024  # larger files cannot come to the server (its disk is small)
THUMB_MAX = 2 * 1024 * 1024  # YouTube's limit for a thumbnail
LEASE = timedelta(minutes=60)  # a helper that took "send the file" gets this long before it is asked again
LATE = timedelta(minutes=30)  # a publish time closer than this cannot be kept: the clip moves to a new day
AHEAD = timedelta(days=1)  # ask for the file about a day before the upload rules would send it
REMIND = timedelta(days=30)  # "approve the waiting PC videos" this long before the last scheduled day
SLOT_TIME, TZ = "18:00", -330  # one a day at 18:00 India time (minutes, as JavaScript's getTimezoneOffset)
IST = timedelta(hours=5, minutes=30)
OWN_LICENSE = "Original content (my own video)"
NOTICE = ("Put only your own videos, or videos you have the rights to use, in this folder — anything in it can be "
          "uploaded to your channel.")
NO_RENDER = ("This video came from the folder on your PC and goes up exactly as it is — it is not made again on the "
             "server. Change the file on your PC instead (it then shows up as a new video).")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
CAMERA = re.compile(r"(?i)(vid|img|mov|pxl|dsc|dscn|dji|gopr|gh|mvi|video|whatsapp video|screen recording|recording)?"
                    r"[\W_\d]*(at[\W_\d]*)?")  # a camera / phone name: only a prefix, digits and separators


def slot_lock():
    """One lock for everything that hands out publish slots (reports, the timed job, Approve all, un-parking), so two
    of them never give the same day twice."""
    from .autopilot import SLOT_LOCK

    return SLOT_LOCK


# ---------------------------------------------------------------- settings
def folder_for(w: Worker | None) -> str:
    """The folder this helper watches. None = the default (D:\\youtube for the owner; off for customers), "" = off."""
    if w is not None and w.pc_folder is not None:
        return w.pc_folder
    return DEFAULT_FOLDER if tenant.current() == tenant.OWNER else ""


def auto_on() -> bool:
    """"Videos I add to the PC folder go up by themselves": ON unless the account switched it off."""
    return get_setting("pc_auto") != "0"


def set_auto(on: bool):
    set_setting("pc_auto", "1" if on else "0")


# ---------------------------------------------------------------- info.json
def _text(v, n: int) -> str:
    s = v if isinstance(v, str) else ("" if v is None else str(v))
    return s.replace("\x00", "").strip()[:n]


def parse_date(v) -> datetime | None:
    """A suggested upload date: "2027-07-01" (= 18:00 India time that day), an ISO time with a zone, or one without a
    zone (= India time). Naive UTC, or None."""
    s = _text(v, 40)
    if not s:
        return None
    try:  # a nonsense date (year 1, year 9999, "2027-13-45") is simply no date — never an error for the whole report
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            d = datetime.fromisoformat(s)
            return d.replace(hour=18) - IST
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            return d - IST
        return d.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError, TypeError):
        return None


def _first(raw: dict, *keys):
    for k in keys:
        if raw.get(k) not in (None, "", []):
            return raw[k]
    return None


def clean_info(raw, description_txt: str = "") -> dict | None:
    """info.json (+ description.txt) as the server keeps it. None when there was no info.json."""
    if not isinstance(raw, dict):
        return None
    tags = _first(raw, "tags", "keywords") or []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\n]", tags)]
    tags = [_text(t, 100).lstrip("#") for t in tags if isinstance(t, (str, int, float))][:60] if isinstance(tags, list) else []
    when = _first(raw, "suggested_upload_utc", "suggested_upload_ist", "suggested_upload_date", "suggested_date",
                  "upload_date", "publish_at", "date")
    approve = str(_first(raw, "approve", "approval") or "").strip().lower()
    url = lambda v: isinstance(v, str) and bool(re.match(r"(?i)^(https?://|www\.|youtube\.com/|UC[\w-]{22}$)", v.strip()))  # noqa: E731
    chan = _first(raw, "source_channel_link", "source_channel_url", "channel_url", "channel_link", "channel_id")
    name = _first(raw, "source_channel_name", "channel_name", "creator", "author")
    for k in ("source_channel", "channel"):  # usually the creator's NAME (the real files), sometimes an address
        v = raw.get(k)
        if v and url(v) and not chan:
            chan = v
        elif v and not url(v) and not name:
            name = v
    clip = _first(raw, "clip_id", "clip")
    credit = _first(raw, "credit", "credits", "attribution")
    if isinstance(credit, list):
        credit = "\n".join(_text(x, 300) for x in credit)
    out = {
        "title": _text(_first(raw, "title", "name"), 200),
        "description": _text(description_txt, 5000) or _text(_first(raw, "description"), 5000),
        "tags": [t for t in tags if t],
        "source": _text(_first(raw, "source_link", "source_url", "source", "url", "link"), 500),
        "channel": _text(chan, 300),  # the creator's channel ADDRESS: the never-use list and the strike hold match it
        "channel_name": _text(name, 200),
        "licence": _text(_first(raw, "licence", "license"), 200),
        "licence_line": _text(raw.get("licence_line") or raw.get("license_line"), 500),
        "credit": _text(credit, 1000),
        "clip_id": _text(clip, 40),
        "publish_at": None,
        # only "approve": "auto" lets a folder's video go by itself; empty, missing or anything else = wait for a person
        "auto": approve == "auto" and raw.get("manual") is not True,
        "manual": approve != "auto" or raw.get("manual") is True,
    }
    d = parse_date(when)
    out["publish_at"] = (d.isoformat() + "Z") if d else None
    return out


def topic_from_name(name: str) -> str:
    """"012_street food delhi" / "goa trip.mp4" -> the words; a camera name (VID_20241124_1030.mp4) -> ""."""
    stem = Path(name or "").stem if Path(name or "").suffix.lower() in (".mp4", ".mov", ".mkv") else (name or "")
    if CAMERA.fullmatch(stem.strip() or "x"):
        return ""
    words = re.sub(r"^[\d\s._-]+", "", stem)
    words = " ".join(re.sub(r"[_\-.]+", " ", words).split())
    return words[:120] if re.search(r"[^\W\d_]{2}", words) else ""


def _clip_ref(info: dict | None) -> int | None:
    """The clip of this account that info.json names ("123", 123 or "t1-c123" of this same account)."""
    v = (info or {}).get("clip_id") or ""
    m = re.fullmatch(r"(?:t(\d+)-c)?(\d{1,12})", str(v).strip())
    if not m or (m.group(1) and int(m.group(1)) != tenant.current()):
        return None
    return int(m.group(2))


# ---------------------------------------------------------------- what the helper reports
def _strict(info: dict | None) -> int:
    """How strongly an info.json holds a video back: 2 = waits for a person, 1 = may go by itself, 0 = none."""
    if info is None:
        return 0
    return 1 if info.get("auto") and not info.get("manual") else 2


def _pick(items: list) -> list:
    """The same file (sha256) twice in one report: one entry, the copy whose info.json holds it back most (so
    "approve": "manual" always wins over a copy without info.json or with "auto")."""
    best: dict = {}
    for it in items:
        sha = str(it.get("sha") or "").lower()
        info = it.get("info")
        rank = 2 if isinstance(info, dict) and str(info.get("approve", "")).strip().lower() != "auto" else 1 if isinstance(info, dict) else 0
        if sha not in best or rank > best[sha][0]:
            best[sha] = (rank, it)
    return [it for _r, it in best.values()]


def _merge_info(row: PcItem, info: dict | None, rel: str, fresh: bool) -> dict | None:
    """What the item keeps: what its own place says now; from ANOTHER copy of the same file only something stricter."""
    if fresh or row.rel == rel:
        return info
    return info if _strict(info) > _strict(row.info) else row.info


def _one(s, wid: int, it: dict, now, ids: dict, thumbs: list, new: list):
    sha = str(it.get("sha") or "").lower()
    if not SHA_RE.match(sha):
        raise ValueError("not a sha256")
    row = s.query(PcItem).filter(PcItem.sha == sha).first()
    raw = it.get("info")
    info = clean_info(raw, it.get("description_txt") or "") if raw is not None else None
    fresh = row is None
    if fresh:
        row = PcItem(sha=sha, state="new", tries=0, moves=0, created_at=now)
        s.add(row)
    rel = _text(it.get("rel"), 500)
    if row.state in ("new", "blocked", "skipped") or fresh:
        row.info = _merge_info(row, info, rel, fresh)  # details may still change while it waits
    row.worker_id, row.seen_at, row.missing_at = wid, now, None
    row.rel, row.name = rel, _text(it.get("name"), 255)
    row.folder = _text(it.get("folder"), 255)
    row.size = max(0, int(it.get("size") or 0))
    dur = float(it.get("duration") or 0)
    row.duration = dur if 0 < dur < 1e6 else 0.0
    row.width, row.height = max(0, min(100000, int(it.get("width") or 0))), max(0, min(100000, int(it.get("height") or 0)))
    s.flush()
    if it.get("thumb") and (not row.thumb or _thumb_path(row) is None):
        thumbs.append(sha)
    ids[sha] = row.id
    if row.state == "new":
        why = blocked_reason(s, row)  # its creator is on the never-use list: shown at once, never approved
        if why:
            row.state, row.note = "blocked", why[:400]
    if fresh:
        new.append(row)


def report(s, wid: int, body: dict) -> dict:
    """The helper's folder: new items, renamed / moved ones, and (with `present`) which are no longer there.
    Item by item: one bad item never stops the others.
    Answer: {"ids": {sha: item id}, "thumbs": [sha that still need their picture], "errors": {sha: why}}."""
    now = utcnow()
    w = s.get(Worker, wid)
    if w is not None:
        w.pc_state = {"folder": _text(body.get("folder"), 300), "exists": bool(body.get("exists", True)),
                      "videos": len(body.get("present") or []) if body.get("present") is not None else
                      ((w.pc_state or {}).get("videos") or 0), "at": now.isoformat() + "Z"}
    ids, thumbs, new, errors = {}, [], [], {}
    for it in _pick([x for x in body.get("items") or [] if isinstance(x, dict)]):
        sp = s.begin_nested()
        try:
            _one(s, wid, it, now, ids, thumbs, new)
            sp.commit()
        except Exception as e:  # noqa: BLE001
            sp.rollback()
            errors[str(it.get("sha") or "?")[:64]] = str(e)[:200] or type(e).__name__
            log.warning("PC folder report: item %s refused: %s", str(it.get("sha"))[:12], str(e)[:200])
    present = body.get("present")
    if present is not None and body.get("exists", True):
        have: dict = {}
        for p in present:
            if isinstance(p, dict):
                have.setdefault(str(p.get("sha") or "").lower(), []).append(p)
        for row in s.query(PcItem).filter(PcItem.worker_id == wid, PcItem.state != "done"):
            seen = have.get(row.sha)
            if not seen:
                if not row.missing_at:
                    row.missing_at = now
                    row.note = "Missing on the PC: the file is no longer in the folder."
                continue
            if row.missing_at:
                row.missing_at, row.note = None, "Found again in the folder on the PC."
            if row.rel not in {q.get("rel") for q in seen} and seen[0].get("rel"):  # moved / renamed
                q = seen[0]
                row.rel = _text(q["rel"], 500)
                row.folder = _text(q.get("folder"), 255) if q.get("folder") is not None else row.folder
                row.name = _text(q.get("name"), 255) or row.name
    s.flush()
    with slot_lock():
        for row in new:  # the switch: new videos go up by themselves (only loose files, or "approve": "auto")
            if auto_ready(row, now):
                approve(s, row, auto=True)
    return {"ids": ids, "thumbs": thumbs, "errors": errors}


def auto_ready(row: PcItem, now) -> bool:
    """May this video go up without a person pressing Approve?"""
    if row.state != "new" or row.missing_at:
        return False
    if not (row.duration or 0) > 0 or not (row.width or 0) > 0:
        return False  # not a readable video (ffmpeg found no picture or no length)
    if row.folder:  # a sub-folder: only when its info.json says "approve": "auto", and its file is video.*
        if _strict(row.info) != 1 or Path(row.name or "").stem.lower() != "video":
            return False
    elif row.info is not None or not topic_from_name(row.name):
        return False  # a camera name (VID_2024…) needs a few words about the video first
    from . import acl

    return auto_on() and acl.feature("uploads.auto") and not acl.clip_block(1)  # (the clips-a-day limit of the account)


def claimed_clips(s) -> set:
    """Parked drafts that a waiting / approved PC video names in its info.json (it was made on the PC): the daily
    un-parking leaves them alone. A skipped, missing or done item gives its draft back."""
    out = set()
    for row in s.query(PcItem).filter(PcItem.state.in_(["new", "approved", "blocked"]), PcItem.missing_at.is_(None)):
        cid = _clip_ref(row.info)
        if cid:
            out.add(cid)
    return out


# ---------------------------------------------------------------- the picture
def thumb_dir(iid: int) -> Path:
    return Path(os.fspath(DATA_DIR / "pc" / str(int(iid))))


def _thumb_path(row: PcItem) -> Path | None:
    for ext in ("jpg", "png"):
        p = thumb_dir(row.id) / f"thumb.{ext}"
        if p.exists():
            return p
    return None


def save_thumb(row: PcItem, data: bytes, kind: str) -> str:
    """'' when saved, else why not. JPEG or PNG only, at most 2 MB."""
    if not data or len(data) > THUMB_MAX:
        return "The picture must be a JPEG or PNG of at most 2 MB."
    ext = "jpg" if data[:3] == b"\xff\xd8\xff" else "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ""
    if not ext:
        return "The picture must be a JPEG or PNG of at most 2 MB."
    d = thumb_dir(row.id)
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("thumb.*"):
        old.unlink(missing_ok=True)
    tmp = d / f"thumb.{ext}.part"
    tmp.write_bytes(data)
    os.replace(tmp, d / f"thumb.{ext}")
    row.thumb = "file" if kind == "file" else "frame"
    return ""


# ---------------------------------------------------------------- slots
def _ist_day(t: datetime):
    return (t + IST).date()


def day_free(s, dest_id, t: datetime, skip_id: int | None = None) -> bool:
    """No other clip of this channel holds a publish time on that India-time day."""
    from .autopilot import HOLDS_SLOT

    start = datetime.combine(_ist_day(t), datetime.min.time()) - IST
    q = s.query(Clip.id).filter(Clip.destination_id == dest_id, Clip.status.in_(HOLDS_SLOT + ["held"]),
                                Clip.publish_at >= start, Clip.publish_at < start + timedelta(days=1))
    if skip_id:
        q = q.filter(Clip.id != skip_id)
    return q.first() is None


def _times() -> str:
    """The daily public times: the Super Admin's posts-per-day choice (e.g. 13:00, 19:00), else 18:00."""
    try:
        import json as _j

        from .db import get_setting

        return (_j.loads(get_setting("posts_per_day") or "{}") or {}).get("times") or SLOT_TIME
    except Exception:  # noqa: BLE001
        return SLOT_TIME


def slot_for(s, dest_id, want: datetime | None = None, skip_id: int | None = None) -> datetime:
    """The next free daily slot after the last scheduled day; the suggested date instead when it is later (the first
    free day from it when that day is taken)."""
    from .autopilot import _next_slot, fit_capacity

    base = _next_slot(s, 24, dest_id, _times(), TZ)
    if want is None or want <= base:
        return base
    t = want
    for _ in range(400):
        if day_free(s, dest_id, t, skip_id):
            return fit_capacity(s, t, _times(), TZ, skip_id=skip_id)
        t += timedelta(days=1)
    return base


def first_free_day(s, dest_id, after: datetime, skip_id: int | None = None) -> datetime:
    """A clip that missed its time: the earliest free 18:00 India-time day after `after` (a gap counts)."""
    from .autopilot import _slot_from_times, fit_capacity

    t = _slot_from_times(after, SLOT_TIME, TZ) or after + timedelta(days=1)
    for _ in range(800):
        if day_free(s, dest_id, t, skip_id):
            return fit_capacity(s, t, SLOT_TIME, TZ, skip_id=skip_id)
        t += timedelta(days=1)
    return t


# ---------------------------------------------------------------- approve / skip
def blocked_reason(s, row: PcItem, p: Project | None = None) -> str:
    """'' or why this item must not be used: its creator is on the never-use list after a strike."""
    from . import strikes

    info = row.info or {}
    refs = [info.get("channel"), p.channel_url if p else None]
    from .ingest import video_id_from_url

    vid = video_id_from_url(info.get("source") or "")
    if vid:
        old = s.query(Project).filter(Project.video_id == vid).first()
        if old is not None:
            refs.append(old.channel_url)
    for ref in refs:
        if ref and strikes.is_blocked(ref):
            return strikes.blocked_message(info.get("channel_name") or (p.channel if p else "") or "")
    return ""


def _fit(info: dict | None, row: PcItem, topic: str):
    from .metadata import _clean, _cut, _fit_tags

    info = info or {}
    name = topic or topic_from_name(row.folder or row.name) or topic_from_name(row.name) or Path(row.name or "My video").stem
    title = " ".join(_clean(info.get("title") or name).split())[:100] or "My video"
    desc = info.get("description") or ""
    credit = (info.get("credit") or "").strip()
    if credit and credit not in desc:
        desc = (desc + "\n\n" + credit).strip()
    from .ingest import video_id_from_url

    src = info.get("source") or ""
    vid = video_id_from_url(src)
    has_src = bool(src) and ((vid and vid in desc) or src in desc)  # compare the video id, not the address
    line = info.get("licence_line") or (f"Licence: {info['licence']}" if info.get("licence") else "")
    if src and not has_src:
        desc = (desc + f"\n\nSource: {src}" + (f"\n{line}" if line else "")).strip()
    elif info.get("licence_line") and _norm(line) not in _norm(desc):
        desc = (desc + "\n" + line).strip()
    return title, _cut(_clean(desc), 4900), _fit_tags(info.get("tags") or [], [])


def _norm(x: str) -> str:
    return " ".join(str(x or "").lower().split())


def approve(s, row: PcItem, topic: str = "", auto: bool = False) -> tuple[Clip | None, str]:
    """Make the item a queued clip on its slot. (clip, '') or (None, why not). Commits. One at a time (slot_lock)."""
    with slot_lock():
        return _approve(s, row, topic, auto)


def _approve(s, row: PcItem, topic: str = "", auto: bool = False) -> tuple[Clip | None, str]:
    topic = " ".join(str(topic or "").split())[:120]
    if row.state not in ("new", "skipped"):
        return None, {"approved": "It is already approved.", "done": "It is already on YouTube.",
                      "blocked": row.note or "Its creator is on the never-use list."}.get(row.state, "It cannot be approved.")
    if row.missing_at:
        return None, "The file is no longer in the folder on your PC."
    if (row.size or 0) > MAX_BYTES:
        return None, "This file is larger than 900 MB — the server cannot take it. Make a smaller copy on your PC."
    dest = main_destination(s)
    if dest is None:
        return None, "Connect a YouTube channel first (My channels)."
    info = row.info
    target = s.get(Clip, _clip_ref(info)) if _clip_ref(info) else None
    p = s.get(Project, target.project_id) if target is not None else None
    if target is not None:
        if target.status not in ("draft", "failed") or target.youtube_id or target.pc_item_id not in (None, row.id):
            return None, (f"Clip #{target.id} named in its info.json is already {target.status} — approving this "
                          "would upload the same clip twice.")
        from .autopilot import same_moment

        other = same_moment(s, target) if p is not None else None
        if other is not None:
            return None, f"It repeats the moment of clip #{other.id} (same video, same part) — it would be a duplicate."
    why = blocked_reason(s, row, p)
    if why:
        row.state, row.note = "blocked", why[:400]
        s.commit()
        return None, why
    title, desc, tags = _fit(info, row, topic)
    want = parse_date((info or {}).get("publish_at")) if info else None
    dest_id = (target.destination_id if target is not None and target.destination_id else None) or dest.id
    slot = slot_for(s, dest_id, want, skip_id=target.id if target is not None else None)
    tall = (row.height or 0) > (row.width or 0) * 1.15
    aspect = "9:16" if tall and (row.duration or 0) <= 180 else "16:9"
    if p is None:
        from .ingest import is_cc, video_id_from_url

        lic = (info or {}).get("licence") or ""
        src = (info or {}).get("source") or ""
        mode = "cc" if is_cc(lic) else ("permission" if src else "original")
        p = Project(url=src, video_id=video_id_from_url(src), title=title, channel=(info or {}).get("channel_name") or
                    (dest.name or ""), channel_url=(info or {}).get("channel") or "", license=lic or OWN_LICENSE,
                    description=desc, tags=tags, duration=int(row.duration or 0), heatmap=[], moments=[], mode=mode,
                    permission_note=(f"From the folder on my PC; rights as stated in its info.json: {lic or 'not stated'}"
                                     if mode == "permission" else None))
        s.add(p)
        s.flush()
        c = Clip(project_id=p.id, start=0, end=max(2.0, round(row.duration or 0, 2)), aspect=aspect, effects=[], overlays=[],
                 credit_on=False, destination_id=dest_id)
        s.add(c)
    else:
        c = target
        c.destination_id = dest_id
        if row.width and row.height:
            c.aspect = aspect
    c.status, c.autoqueue, c.error, c.publish_at = "queued", False, None, slot
    c.title, c.description, c.tags = title, desc, tags
    c.meta_edited = bool(info and info.get("title"))  # its own words: the keyword helper leaves them alone
    c.output_path = None  # the file is on the PC until shortly before its upload
    c.pc_item_id = row.id
    # it is on the PC already: counted as saved there (no archive job; the clean-up deletes the server copy after the upload)
    c.archived_at, c.archived_sha, c.archived_by, c.archived_size = utcnow(), row.sha, row.worker_id, row.size
    c.archived_src_sha, c.archive_missing, c.archive_note = None, None, None
    tp = _thumb_path(row)
    if tp is not None and row.thumb == "file":
        c.thumb_path, c.thumb_custom = str(tp), True
    s.flush()
    row.state, row.clip_id, row.topic = "approved", c.id, topic or row.topic
    row.note = (("Approved by itself" if auto else "Approved") + f" — goes live {(slot + IST):%d %b %Y} at "
                f"{(slot + IST):%H:%M} India time. The video stays on your PC until about a day before its upload.")[:400]
    s.commit()
    if topic or (not info and auto):
        words = topic or topic_from_name(row.folder or row.name)
        if words:  # title, description and tags from what people search now (a live search, within the day's budget)
            from .jobs import executor

            executor.submit(fill_topic, c.id, words)
    try:
        from . import acl

        acl.record_here("clips.approve", f"{'Approved by itself' if auto else 'Approved'} a video from the PC folder: "
                                         f"{title[:100]} (#{c.id})")
    except Exception:  # noqa: BLE001
        pass
    return c, ""


def fill_topic(clip_id: int, topic: str):
    """app/owntopic.py writes title, description and tags from the topic (the slot is kept)."""
    from .owntopic import fill

    fill(clip_id, topic)


_BULK: dict = {}  # account -> progress of its "Approve all" (runs in its own thread; one at a time per account)


def approve_all_start() -> dict:
    """Approve every waiting video, also the "approve": "manual" ones (this is the owner's own press), in the
    background; the dashboard shows the progress."""
    tid = tenant.current()
    st = _BULK.get(tid)
    if st and st.get("running"):
        return dict(st)
    with SessionLocal() as s:
        ids = [r.id for r in s.query(PcItem).filter(PcItem.state == "new", PcItem.missing_at.is_(None)).order_by(PcItem.id)]
    st = _BULK[tid] = {"running": True, "total": len(ids), "done": 0, "approved": 0, "not": [],
                       "at": utcnow().isoformat() + "Z"}
    th = threading.Thread(target=_bulk_run, args=(tid, ids, st), name=f"pc-approve-all-{tid}", daemon=True)
    th.start()
    st["_thread"] = th
    return {k: v for k, v in st.items() if not k.startswith("_")}


def _bulk_run(tid: int, ids: list, st: dict):
    from . import acl

    try:
        with tenant.use(tid):
            for iid in ids:
                with SessionLocal() as s:
                    row = s.get(PcItem, iid)
                    if row is not None and row.state == "new" and not row.missing_at:
                        stop = acl.clip_block(1)
                        if stop:
                            st["not"].append({"id": iid, "why": stop})
                            break
                        words = "" if row.info else (row.topic or topic_from_name(row.folder or row.name))
                        c, why = approve(s, row, words)
                        if c is None:
                            st["not"].append({"id": iid, "why": why})
                        else:
                            st["approved"] += 1
                st["done"] += 1
    except Exception as e:  # noqa: BLE001
        log.exception("PC folder: Approve all stopped")
        st["not"].append({"id": None, "why": f"stopped: {str(e)[:150]}"})
    finally:
        st["running"] = False


def bulk_state() -> dict:
    return {k: v for k, v in (_BULK.get(tenant.current()) or {}).items() if not k.startswith("_")}


def skip(s, row: PcItem) -> str:
    if row.state not in ("new", "blocked"):
        return "Only a video that waits can be skipped."
    row.state, row.note = "skipped", "Skipped — it stays on your PC and is not uploaded."
    s.commit()  # (a parked draft it named is free again for the daily un-parking: see claimed_clips)
    return ""


def unskip(s, row: PcItem) -> str:
    if row.state != "skipped":
        return "It is not skipped."
    row.state, row.note = "new", "Back in the list."
    s.commit()
    return ""


# ---------------------------------------------------------------- the file, shortly before its upload
def video_path(c: Clip) -> Path:
    return Path(os.fspath(DATA_DIR / "clips" / str(c.id) / "out.mp4"))


def on_server(c: Clip) -> bool:
    return bool(c.output_path) and Path(c.output_path).exists()


def ready(c: Clip, now) -> bool:
    """process_queue: a PC clip goes up only with its file here and a publish time it can still keep."""
    return on_server(c) and bool(c.publish_at) and c.publish_at > now + LATE


def _late_note(s, row: PcItem, c: Clip, now) -> str:
    w = s.get(Worker, row.worker_id) if row.worker_id else None
    online = bool(w and w.last_seen and w.last_seen > now - timedelta(minutes=10))
    if row.missing_at:
        return "the file is no longer in the folder on your PC"
    if not on_server(c):
        return "your PC was off (or its helper was not running) when the video was needed" if not online else \
            "the video did not arrive from your PC in time"
    return "it could not be uploaded in time (YouTube's daily limit, or uploads were paused)"


def due(now=None) -> dict:
    """Timed job (every 2 minutes, per account): clips whose time passed move to a new day, and the next files are
    asked from the PC."""
    from . import acl
    from .autopilot import disk_free_gb as free_gb

    now = now or utcnow()
    out = {"moved": 0, "asked": 0}
    with slot_lock(), SessionLocal() as s:
        rows = (s.query(Clip, PcItem).join(PcItem, PcItem.id == Clip.pc_item_id)
                .filter(Clip.status == "queued").order_by(Clip.publish_at, Clip.id).all())
        for c, row in rows:  # 1. never a late upload without a time: a missed slot moves to the next free day
            if c.publish_at and c.publish_at > now + LATE:
                continue
            why = _late_note(s, row, c, now)
            c.publish_at = first_free_day(s, c.destination_id, now + LATE, skip_id=c.id)
            when = c.publish_at + IST
            c.error = (f"Moved to {when:%d %b %Y} {when:%H:%M} India time: {why} before its publish time.")[:500]
            row.moved_at, row.moves, row.note = now, (row.moves or 0) + 1, c.error[:400]
            out["moved"] += 1
        s.commit()
        if acl.upload_block():
            return out
        paused = {d.id for d in s.query(Destination).filter(Destination.paused.is_(True))}
        here = sum(1 for c, _r in rows if on_server(c)) + sum(1 for _c, r in rows if r.wanted_at and not r.missing_at)
        # also a PC video that is being uploaded, or whose upload failed (its file is still on the server)
        here += sum(1 for c in s.query(Clip).filter(Clip.status.in_(["uploading", "failed"]), Clip.pc_item_id.isnot(None))
                    if on_server(c))
        free = free_gb()
        lead = timedelta(hours=quota.upload_lead_hours())
        cap = max(1, quota.capacity())
        asked_before = 0
        for c, row in rows:
            if on_server(c):
                asked_before += 1
                continue
            if c.destination_id in paused:
                continue
            if row.wanted_at:
                asked_before += 1
                continue
            if row.missing_at or here >= MAX_FILES:
                continue
            if not c.publish_at or c.publish_at - lead > now + AHEAD:
                break  # its turn under the upload lead is more than a day away (and so are the later ones)
            ahead = s.query(Clip).filter(Clip.status == "queued", Clip.pc_item_id.is_(None), Clip.publish_at < c.publish_at,
                                         Clip.publish_at <= now + lead).count() + asked_before
            if ahead >= cap:
                break  # about a day of other uploads come first: ask later
            gb = (row.size or 0) / 1e9
            if free is not None and free - gb < MIN_FREE_GB:
                log.info("PC video %s not asked for: the server's disk has %.1f GB free", row.id, free)
                break
            row.wanted_at, row.lease_at = now, None
            row.note = "Asked your PC for the video (its upload is close)."
            here += 1
            asked_before += 1
            out["asked"] += 1
            if free is not None:
                free -= gb
        s.commit()
    return out


def jobs(s, now, wid: int) -> list[dict]:
    """"Send this file" jobs for this helper (only the one whose folder has it)."""
    out = []
    rows = (s.query(PcItem).filter(PcItem.wanted_at.isnot(None), PcItem.worker_id == wid, PcItem.missing_at.is_(None))
            .order_by(PcItem.wanted_at, PcItem.id).limit(10).all())
    for row in rows:
        if len(out) >= 2:
            break
        if row.lease_at and row.lease_at > now - LEASE:
            continue
        c = s.get(Clip, row.clip_id) if row.clip_id else None
        if c is None or c.status != "queued" or on_server(c):
            row.wanted_at, row.lease_at = None, None
            continue
        row.lease_at, row.tries = now, (row.tries or 0) + 1
        out.append({"type": "pc_send", "item_id": row.id, "clip_id": c.id, "sha": row.sha, "rel": row.rel or "",
                    "size": int(row.size or 0)})
    return out


def receive_check(s, iid: int, wid: int) -> tuple[int, Path]:
    """Before a byte is saved: asked for, from this helper, still waiting. (size cap, temporary file) or ValueError."""
    row = s.get(PcItem, iid)
    if row is None:
        raise LookupError("Not found")
    if row.worker_id != wid:
        raise ValueError("Another helper PC has this video")
    c = s.get(Clip, row.clip_id) if row.clip_id else None
    if not row.wanted_at or c is None or c.status != "queued":
        raise ValueError("This video is not needed now")
    if on_server(c):
        raise ValueError("The video is already on the server")
    d = video_path(c).parent
    d.mkdir(parents=True, exist_ok=True)
    import secrets

    return int(row.size or 0), d / f"pc.{secrets.token_hex(4)}.part"


def receive_finish(s, iid: int, tmp: Path) -> tuple[bool, str]:
    from .archive import sha256

    row = s.get(PcItem, iid)
    c = s.get(Clip, row.clip_id) if row and row.clip_id else None
    if row is None or c is None or c.status != "queued" or not row.wanted_at:
        return False, "This video is not needed now"
    if sha256(tmp) != row.sha:
        row.note = "The file that came from the PC was not the same (sha256 differs); it is asked again."
        row.lease_at = None
        return False, "The file is not the one in the list (sha256 differs)."
    dest = video_path(c)
    os.replace(tmp, dest)
    c.output_path = str(dest)
    if (c.end or 0) <= 2.0 and not row.duration:
        try:
            from .ingest import stream_seconds

            secs = stream_seconds(dest, "v:0")
            if secs > 2:
                c.end = round(secs, 2)
        except Exception:  # noqa: BLE001
            pass
    row.wanted_at, row.lease_at, row.fetched_at = None, None, utcnow()
    row.note = "The video is on the server for its upload; it is deleted there right after it."
    return True, ""


def failed(s, iid: int, wid: int, error: str):
    row = s.get(PcItem, iid)
    if row is None or row.worker_id != wid:
        return
    row.note = ("Your PC could not send the video: " + error)[:400]
    if "not in the folder" in error or "no longer" in error:
        row.missing_at = row.missing_at or utcnow()  # it is asked again once the helper finds it in its folder


def after_upload(s, c: Clip):
    """The upload worked: the item is done; the server's copy goes now (unless an Instagram/Facebook post still needs
    it — then the usual clean-up deletes it, it is saved on the PC)."""
    row = s.get(PcItem, c.pc_item_id) if c.pc_item_id else None
    if row is not None:
        row.state, row.uploaded_at = "done", utcnow()
        row.note = "On YouTube; the server's copy was deleted (the video stays on your PC)."
    from . import archive

    try:
        from .meta import status as meta_status

        st = meta_status()
        meta_on = bool(st["connected"] and st["on"])
    except Exception:  # noqa: BLE001
        meta_on = True
    p = s.get(Project, c.project_id)
    if archive.crosspost_pending(c, p, meta_on, utcnow()):
        return
    try:
        if on_server(c) and archive.sha256(c.output_path) == c.archived_sha:
            Path(c.output_path).unlink(missing_ok=True)
    except OSError as e:
        log.warning("PC video of clip %s: could not delete the server copy: %s", c.id, str(e)[:150])


# ---------------------------------------------------------------- the dashboard
def as_dict(row: PcItem, c: Clip | None = None) -> dict:
    info = row.info or {}
    return {"id": row.id, "name": row.name, "rel": row.rel, "folder": row.folder, "size": row.size,
            "duration": row.duration, "width": row.width, "height": row.height, "state": row.state, "note": row.note,
            "has_info": row.info is not None, "manual": bool(row.folder) and _strict(row.info) != 1, "title": info.get("title") or "",
            "suggested": info.get("publish_at"), "licence": info.get("licence") or ("" if row.info else OWN_LICENSE),
            "source": info.get("source") or "", "topic": row.topic or topic_from_name(row.folder or row.name),
            "thumb": bool(row.thumb), "missing": bool(row.missing_at), "clip_id": row.clip_id,
            "publish_at": (c.publish_at.isoformat() + "Z") if c is not None and c.publish_at else None,
            "clip_status": c.status if c is not None else None, "on_server": bool(c is not None and on_server(c)),
            "wanted": bool(row.wanted_at), "moved": bool(row.moved_at),
            "needs_topic": not row.folder and not row.info and not topic_from_name(row.name),
            "created_at": (row.created_at.isoformat() + "Z") if row.created_at else None}


def listing(s) -> dict:
    rows = s.query(PcItem).order_by(PcItem.id.desc()).all()
    clips = {c.id: c for c in s.query(Clip).filter(Clip.pc_item_id.isnot(None))}
    waiting = [as_dict(r, clips.get(r.clip_id)) for r in rows if r.state in ("new", "blocked")]
    waiting.sort(key=lambda x: x["id"])
    other = [as_dict(r, clips.get(r.clip_id)) for r in rows if r.state not in ("new", "blocked")][:60]
    helpers = []
    for w in s.query(Worker).filter(Worker.approved.is_(True)).order_by(Worker.id):
        if w.can and w.can != "all" and "pc" not in (w.can or "").split(","):
            continue  # the phone has no folder
        st = w.pc_state or {}
        helpers.append({"id": w.id, "name": w.name, "folder": folder_for(w), "default": w.pc_folder is None,
                        "seen_folder": st.get("folder"), "exists": st.get("exists"), "videos": st.get("videos"),
                        "reported_at": st.get("at"), "knows": bool(st)})
    counts = {k: s.query(PcItem).filter(PcItem.state == k).count() for k in ("new", "approved", "skipped", "blocked", "done")}
    return {"waiting": waiting, "recent": other, "counts": counts, "auto": auto_on(), "notice": NOTICE, "helpers": helpers,
            "max_files": MAX_FILES, "bulk": bulk_state()}


def problems(s, now=None) -> list[dict]:
    """Lines for the Problems list (app/main.health)."""
    now = now or utcnow()
    items = []
    moved = s.query(PcItem).filter(PcItem.moved_at.isnot(None), PcItem.moved_at > now - timedelta(days=3),
                                   PcItem.state == "approved").count()
    if moved:
        items.append({"level": "warn", "key": "pc_moved", "fix": "clips",
                      "text": f"{moved} video(s) from your PC missed their publish time (the PC was off, or the file was not "
                              "there) and were moved to a new day. Keep the helper PC on — nothing went up late."})
    gone = s.query(PcItem).filter(PcItem.missing_at.isnot(None), PcItem.state == "approved").count()
    if gone:
        items.append({"level": "warn", "key": "pc_missing", "fix": "clips",
                      "text": f"{gone} approved video(s) are missing from the folder on your PC, so they cannot go up. "
                              "Put them back (moving or renaming inside the folder is fine)."})
    wanted = s.query(PcItem).filter(PcItem.wanted_at.isnot(None), PcItem.wanted_at < now - timedelta(hours=2)).count()
    if wanted:
        items.append({"level": "warn", "key": "pc_waiting", "fix": "helper",
                      "text": f"{wanted} video(s) from your PC are needed for their upload, but the helper PC has not sent "
                              "them. Turn the PC on and keep the helper running."})
    waiting = s.query(PcItem).filter(PcItem.state == "new", PcItem.missing_at.is_(None)).all()
    topic = [r for r in waiting if not r.folder and not r.info and not topic_from_name(r.name)]  # (JSON None = 'null')
    held = [r for r in waiting if r not in topic]  # wait for the owner's Approve (manual / no info.json / switch off)
    if held:
        from .autopilot import HOLDS_SLOT

        last = s.query(func.max(Clip.publish_at)).filter(Clip.status.in_(HOLDS_SLOT), Clip.publish_at > now).scalar()
        if last is not None and last - now <= REMIND:
            when = last + IST
            items.append({"level": "warn", "key": "pc_approve_soon", "fix": "clips",
                          "text": f"{len(held)} video(s) from your PC wait for your Approve, and the last scheduled video goes "
                                  f"live on {when:%d %b %Y}. Approve them before then (Clips → From my PC) so the "
                                  "channel keeps getting one a day."})
    if topic and auto_on():
        items.append({"level": "info", "key": "pc_topic", "fix": "clips",
                      "text": f"{len(topic)} video(s) in your PC folder need a few words about what they show (their file "
                              "name says nothing) — Clips → From my PC."})
    return items

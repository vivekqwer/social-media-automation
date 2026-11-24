"""Finished videos go to the owner's PC after they are uploaded; then the server's copy is deleted.

The server disk is small. Once a clip is on YouTube, the helper PC downloads its finished video (and, for my own
uploaded file, the original file too) into the folder `archive` next to the helper, checks the sha256 the server
gave, and confirms. Only then — and when no Instagram/Facebook post still needs the file — does the clean-up delete
the server's copy. Every database field and the thumbnail stay. "Get the video back from the PC" asks the helper to
send the file back (checked against the same sha256), after which "Upload again" works as before.

Rules that never change:
* A file is never deleted unless the PC confirmed a copy with the same sha256, and the file still has that sha256.
* A helper only ever sees its own customer's clips (the middleware runs every helper request in its own copy).
"""
import hashlib
import os
import shutil
from datetime import timedelta
from pathlib import Path

from . import tenant
from .config import DATA_DIR
from .db import Clip, Project, get_setting, set_setting, utcnow

ARCHIVE_PER_POLL = 2  # archive jobs handed to a helper in one go (video downloads always come first)
LATE_DAYS = 14  # no PC copy this long after the upload: one line in the Problems list
RESTORED_GRACE = timedelta(days=3)  # a video brought back from the PC stays this long for "Upload again"
CROSSPOST_WAIT = timedelta(days=2)  # an Instagram/Facebook post that has not happened by then never will
OWN_LICENSE_END = "(my own video)"
NAMES = ("video", "source")


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def folder(c: Clip) -> Path:
    return Path(os.fspath(DATA_DIR / "clips" / str(c.id)))


def video_path(c: Clip) -> Path:
    return Path(c.output_path) if c.output_path else folder(c) / "out.mp4"


def source_path(c: Clip) -> Path:
    return folder(c) / "src" / "upload.mp4"  # my own uploaded file: nothing on the internet has it


def key(cid: int) -> str:
    """The clip's name on the PC: the customer number is part of it, so two accounts' files never mix."""
    return f"t{tenant.current()}-c{int(cid)}"


def on_server(c: Clip) -> bool:
    return video_path(c).exists()


def complete(c: Clip) -> bool:
    """The PC confirmed every file this clip still has on the server (by sha256 at the time)."""
    if not (c.archived_at and c.archived_sha):
        return False
    return not source_path(c).exists() or bool(c.archived_src_sha)


def since() -> object:
    """When this account first had the PC copies (old uploads are given the same 14 days from then)."""
    v = get_setting("archive_since")
    if v:
        try:
            from datetime import datetime

            return datetime.fromisoformat(v)
        except ValueError:
            pass
    now = utcnow()
    set_setting("archive_since", now.isoformat())
    return now


# ---------------------------------------------------------------- jobs for the helper
def _info(s, c: Clip) -> dict:
    p = s.get(Project, c.project_id)
    return {"title": c.title or "", "youtube_id": c.youtube_id, "youtube_url": f"https://youtu.be/{c.youtube_id}" if c.youtube_id else "",
            "publish_at": (c.publish_at.isoformat() + "Z") if c.publish_at else None,
            "uploaded_at": (c.attempted_at.isoformat() + "Z") if c.attempted_at else None,
            "source_url": (p.url or "") if p else "", "source_title": (p.title or "") if p else "",
            "own_upload": bool(p and p.mode == "original" and (p.license or "").endswith(OWN_LICENSE_END)),
            "clip": key(c.id)}


def jobs(s, now, lease) -> list[dict]:
    """Restore jobs first (the owner is waiting for them), then a few archive jobs."""
    free = lambda t: not t or t < now - lease  # noqa: E731
    out = []
    for c in s.query(Clip).filter(Clip.restore_wanted.isnot(None)).order_by(Clip.id).limit(5):
        if not free(c.archive_lease):
            continue
        files = [{"name": "video", "sha": c.archived_sha}] if not on_server(c) else []
        if c.archived_src_sha and not source_path(c).exists():
            files.append({"name": "source", "sha": c.archived_src_sha})
        if not files:  # it is back already
            c.restore_wanted = None
            continue
        c.archive_lease = now
        out.append({"type": "restore", "clip_id": c.id, "key": key(c.id), "files": files})
    n = 0
    rows = (s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.archived_at.is_(None))
            .order_by(Clip.id))  # all of them: old clips whose file is gone must not hide the ones that still have one
    for c in rows:
        if n >= ARCHIVE_PER_POLL:
            break
        if not free(c.archive_lease) or not on_server(c):
            continue
        files = [{"name": "video", "sha": sha256(video_path(c)), "size": video_path(c).stat().st_size}]
        if source_path(c).exists():
            files.append({"name": "source", "sha": sha256(source_path(c)), "size": source_path(c).stat().st_size})
        c.archive_lease = now
        out.append({"type": "archive", "clip_id": c.id, "key": key(c.id), "files": files, "info": _info(s, c)})
        n += 1
    return out


def file_for(c: Clip, name: str) -> Path | None:
    if c.status != "uploaded" or name not in NAMES:
        return None
    p = video_path(c) if name == "video" else source_path(c)
    return p if p.exists() else None


def confirm(c: Clip, shas: dict) -> tuple[bool, str]:
    """The helper saved the files: record it only when every sha256 matches the server's file now."""
    if c.status != "uploaded" or not on_server(c):
        return False, "This clip has no finished video on the server to save."
    want = {"video": sha256(video_path(c))}
    if source_path(c).exists():
        want["source"] = sha256(source_path(c))
    for name, sha in want.items():
        if str(shas.get(name) or "").lower() != sha:
            c.archive_note = f"The copy on the PC did not match ({name}); it will be saved again."
            return False, f"The {name} file on the PC does not match the server's file."
    c.archived_at, c.archived_sha, c.archived_src_sha = utcnow(), want["video"], want.get("source")
    c.archive_note, c.archive_lease = None, None
    return True, ""


def put_back(c: Clip, uploads: dict) -> tuple[bool, str]:
    """uploads: name -> a temporary file the helper sent. Checked against the recorded sha256, then moved in place."""
    expect = {"video": c.archived_sha, "source": c.archived_src_sha}
    for name, tmp in uploads.items():
        if name not in NAMES or not expect.get(name):
            return False, f"Nothing called {name} was saved for this clip."
        if sha256(tmp) != expect[name]:
            return False, f"The {name} file from the PC is not the one that was saved (sha256 differs)."
    for name, tmp in uploads.items():
        dest = video_path(c) if name == "video" else source_path(c)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
    if "video" in uploads and not c.output_path:
        c.output_path = str(video_path(c))
    (folder(c) / "restored").write_text(utcnow().isoformat(), encoding="utf-8")  # kept a few days for "Upload again"
    c.restore_wanted, c.archive_lease = None, None
    c.archive_note = "The video is back on the server from your PC. Press Upload again within 3 days."
    return True, ""


# ---------------------------------------------------------------- clean-up
def crosspost_pending(c: Clip, p: Project | None, meta_on: bool, now) -> bool:
    """Instagram/Facebook fetch the file from the server: keep it until that post is done (or never will be)."""
    from .meta import CROSSPOST_MODES

    if not meta_on or c.meta_done or c.meta_result:
        return False
    if c.aspect != "9:16" or not p or p.mode not in CROSSPOST_MODES:
        return False
    return not (c.attempted_at and c.attempted_at < now - CROSSPOST_WAIT)


def _keep_frame(c: Clip, p: Project | None):
    """The second-thumbnail try (low views) needs a frame of the video: save it before the video goes."""
    if c.thumb_custom or (c.thumb_variant or 0) or not p:
        return
    try:
        from .thumbs import save_frame

        save_frame(c, p, 1)
    except Exception:  # noqa: BLE001  (a thumbnail frame is not worth keeping a whole video for)
        pass


def free_uploaded(s, c: Clip, d: Path, meta_on: bool, now) -> bool:
    """Delete the server's video files of an uploaded clip once the PC has them. True when something was deleted."""
    if not complete(c):
        return False
    if not (video_path(c).exists() or (d / "body.mp4").exists() or (d / "src").exists()):
        return False  # nothing left to free (done in an earlier round)
    marker = d / "restored"
    if marker.exists():
        import time

        if time.time() - marker.stat().st_mtime < RESTORED_GRACE.total_seconds():
            return False
    p = s.get(Project, c.project_id)
    if crosspost_pending(c, p, meta_on, now):
        return False
    vid, src = video_path(c), source_path(c)
    if vid.exists() and sha256(vid) != c.archived_sha:  # changed after it was saved: save it again first
        c.archived_at = None
        return False
    if src.exists() and sha256(src) != c.archived_src_sha:
        c.archived_at = None
        return False
    _keep_frame(c, p)
    vid.unlink(missing_ok=True)
    (d / "body.mp4").unlink(missing_ok=True)
    shutil.rmtree(d / "src", ignore_errors=True)
    marker.unlink(missing_ok=True)
    return True


def late(s) -> int:
    """Uploaded clips still without a PC copy LATE_DAYS after their upload (their files stay on the server)."""
    now = utcnow()
    start = since()
    edge = now - timedelta(days=LATE_DAYS)
    if start > edge:
        return 0
    rows = s.query(Clip).filter(Clip.status == "uploaded", Clip.archived_at.is_(None), Clip.attempted_at < edge).all()
    return sum(1 for c in rows if on_server(c))

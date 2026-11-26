"""Finished videos go to the owner's PC after they are uploaded; then the server's copy is deleted.

The server disk is small. Once a clip is on YouTube, the helper PC downloads its finished video (and, for my own
uploaded file, the original file too) into its archive folder (`%USERPROFILE%\\RajiStudioArchive` unless set
otherwise), checks the sha256 the server gave, and confirms. Only then — and when no Instagram/Facebook post still
needs the file — does the clean-up delete the server's copy. Every database field and the thumbnail stay.
"Get the video back from the PC" asks the helper that saved it to send the file back (checked against the same
sha256), after which "Upload again" works as before. Once a day each helper lists what its archive folder still
holds; a clip whose PC copy is gone is marked (and saved again while the server still has it) — nothing is ever
deleted because of that list.

Rules that never change:
* A file is never deleted unless the PC confirmed a copy with the same sha256, and the file still has that sha256.
* A helper only ever sees its own customer's clips (the middleware runs every helper request in its own copy).
"""
import hashlib
import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path

from . import tenant
from .config import DATA_DIR
from .db import Clip, Project, Worker, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

ARCHIVE_PER_POLL = 2  # archive jobs handed to a helper in one go (video downloads always come first)
LATE_DAYS = 14  # no PC copy this long after the upload: one line in the Problems list
RESTORED_GRACE = timedelta(days=3)  # a video brought back from the PC stays this long for "Upload again"
RESTORE_GIVE_UP = timedelta(days=3)  # "Get the video back" with no answer from the PC for this long: the ask ends
OFFLINE_LONG = timedelta(days=2)  # a helper not seen for this long is "offline for a long time"
INVENTORY_EVERY = timedelta(hours=24)  # each helper lists its archive folder this often
CROSSPOST_WAIT = timedelta(days=2)  # an Instagram/Facebook post that has not happened by then never will
RESTORE_MAX = 500 * 1024 * 1024  # the largest file a helper may send back
OWN_LICENSE_END = "(my own video)"
NAMES = ("video", "source")
OLD_HELPER = ("Update the helper PC so it can save uploaded videos — until then they stay on the server. "
              "(It updates itself when it is running; otherwise start worker\\run_worker.bat once.)")


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


def saved_names(cid: int, name: str, sha: str) -> set:
    """The file names a helper may have used for this file (it never overwrites, so a second one carries the sha)."""
    tail = ".source.mp4" if name == "source" else ".mp4"
    return {f"{key(cid)}{tail}", f"{key(cid)}-{(sha or '')[:12]}{tail}"}


def on_server(c: Clip) -> bool:
    return video_path(c).exists()


def own_upload(p: Project | None) -> bool:
    return bool(p and p.mode == "original" and (p.license or "").endswith(OWN_LICENSE_END))


def complete(c: Clip) -> bool:
    """The PC confirmed every file this clip still has on the server (by sha256 at the time)."""
    if not (c.archived_at and c.archived_sha):
        return False
    return not source_path(c).exists() or bool(c.archived_src_sha)


def all_back(c: Clip) -> bool:
    """Every file that was saved on the PC is on the server (again)."""
    return on_server(c) and (not c.archived_src_sha or source_path(c).exists())


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


def helpers_can_save(s) -> bool:
    """At least one approved helper is new enough to save videos (it tells the server its archive folder)."""
    return s.query(Worker).filter(Worker.approved.is_(True), Worker.archive_dir.isnot(None)).count() > 0


def saver(s, c: Clip) -> tuple[Worker | None, str]:
    """The helper that holds this clip's PC copy, and a plain sentence when it cannot send it back."""
    if not c.archived_by:
        return None, ""  # saved before this was recorded: any of the account's helpers may have it
    w = s.get(Worker, c.archived_by)
    if not w:
        return None, ("The helper PC that saved this video was removed from My channels → Helper PC. The video is still "
                      "in that PC's archive folder: start the helper there and approve it again, then press this again.")
    if not w.last_seen or w.last_seen < utcnow() - OFFLINE_LONG:
        when = w.last_seen.strftime("%d %b") if w.last_seen else "never"
        return w, (f"The helper PC that saved this video ({w.name}) has not been online since {when}. "
                   "Turn that PC on — the video comes back as soon as its helper runs.")
    return w, ""


# ---------------------------------------------------------------- jobs for the helper
def _info(s, c: Clip) -> dict:
    p = s.get(Project, c.project_id)
    return {"title": c.title or "", "youtube_id": c.youtube_id, "youtube_url": f"https://youtu.be/{c.youtube_id}" if c.youtube_id else "",
            "publish_at": (c.publish_at.isoformat() + "Z") if c.publish_at else None,
            "uploaded_at": (c.attempted_at.isoformat() + "Z") if c.attempted_at else None,
            "source_url": (p.url or "") if p else "", "source_title": (p.title or "") if p else "",
            "own_upload": own_upload(p), "clip": key(c.id)}


def restore_needs(c: Clip) -> list[dict]:
    """The files of this clip that are on the PC only: [{name, sha}]."""
    files = [{"name": "video", "sha": c.archived_sha}] if c.archived_sha and not on_server(c) else []
    if c.archived_src_sha and not source_path(c).exists():
        files.append({"name": "source", "sha": c.archived_src_sha})
    return files


def jobs(s, now, lease, wid: int | None = None) -> list[dict]:
    """The daily folder list, restore jobs (the owner is waiting for them), then a few archive jobs."""
    free = lambda t: not t or t < now - lease  # noqa: E731
    out = []
    w = s.get(Worker, wid) if wid else None
    if w and (not w.inventory_at or w.inventory_at < now - INVENTORY_EVERY):
        w.inventory_at = now
        out.append({"type": "inventory"})
    for c in s.query(Clip).filter(Clip.restore_wanted.isnot(None)).order_by(Clip.id).limit(20):
        if not free(c.archive_lease) or (c.archived_by and wid and c.archived_by != wid):
            continue  # only the helper that saved it has the file
        files = restore_needs(c)
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
        try:  # one unreadable file never stops the other jobs (downloads come in the same answer)
            files = [{"name": "video", "sha": sha256(video_path(c)), "size": video_path(c).stat().st_size}]
            if source_path(c).exists():
                files.append({"name": "source", "sha": sha256(source_path(c)), "size": source_path(c).stat().st_size})
        except OSError as e:
            log.warning("archive: clip %s skipped, its file cannot be read: %s", c.id, str(e)[:150])
            continue
        c.archive_lease = now
        out.append({"type": "archive", "clip_id": c.id, "key": key(c.id), "files": files, "info": _info(s, c)})
        n += 1
    return out


def file_for(c: Clip, name: str) -> Path | None:
    if c.status != "uploaded" or name not in NAMES:
        return None
    p = video_path(c) if name == "video" else source_path(c)
    return p if p.exists() else None


def confirm(c: Clip, shas: dict, wid: int | None = None) -> tuple[bool, str]:
    """The helper saved the files: record it only when every sha256 matches the server's file now."""
    if c.status != "uploaded" or not on_server(c):
        return False, "This clip has no finished video on the server to save."
    want = {"video": sha256(video_path(c))}
    if source_path(c).exists():
        want["source"] = sha256(source_path(c))
    for name, sha in want.items():
        if str(shas.get(name) or "").lower() != sha:
            if not c.archived_at:
                c.archive_note = f"The copy on the PC did not match ({name}); it will be saved again."
            return False, f"The {name} file on the PC does not match the server's file."
    if c.archived_at and c.archived_by and c.archived_by != wid and c.archived_sha == want["video"]:
        return True, ""  # a second helper finished the same job late: the first one's record stays
    c.archived_at, c.archived_sha, c.archived_src_sha = utcnow(), want["video"], want.get("source")
    c.archived_size = video_path(c).stat().st_size
    c.archived_src_size = source_path(c).stat().st_size if "source" in want else None
    c.archived_by, c.archive_missing = wid, None
    c.archive_note, c.archive_lease = None, None
    return True, ""


def restore_cap(c: Clip, name: str) -> int:
    """The most bytes a helper may send back for this file: its saved size + 10%, at most RESTORE_MAX."""
    size = c.archived_size if name == "video" else c.archived_src_size
    return min(RESTORE_MAX, int(size * 1.1) + 1) if size else RESTORE_MAX


def put_back(c: Clip, uploads: dict) -> tuple[bool, str]:
    """uploads: name -> a temporary file the helper sent. Checked against the recorded sha256, then moved in place."""
    expect = {"video": c.archived_sha, "source": c.archived_src_sha}
    for name, tmp in uploads.items():
        if name not in NAMES or not expect.get(name):
            return False, f"Nothing called {name} was saved for this clip."
        if sha256(tmp) != expect[name]:
            return False, f"The {name} file from the PC is not the one that was saved (sha256 differs)."
    # the marker first: the clean-up never sees a brought-back file without it (it would delete it again at once)
    (folder(c) / "restored").write_text(utcnow().isoformat(), encoding="utf-8")  # kept a few days for "Upload again"
    for name, tmp in uploads.items():
        dest = video_path(c) if name == "video" else source_path(c)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
    if "video" in uploads and not c.output_path:
        c.output_path = str(video_path(c))
    if not restore_needs(c):
        c.restore_wanted, c.archive_lease = None, None
        c.archive_note = "The video is back on the server from your PC. Press Upload again within 3 days."
    return True, ""


def expire_restores(s, now) -> int:
    """An ask to get a video back that no helper answered in RESTORE_GIVE_UP ends, with a plain note."""
    n = 0
    for c in s.query(Clip).filter(Clip.restore_wanted.isnot(None), Clip.restore_wanted < now - RESTORE_GIVE_UP):
        _w, why = saver(s, c)
        c.restore_wanted, c.archive_lease = None, None
        c.archive_note = ("No helper PC sent the video back within 3 days, so the request was stopped. "
                          + (why or "Make sure the helper PC that saved it is on, then press “Get the video back” again."))[:300]
        n += 1
    return n


def inventory(s, wid: int, names) -> dict:
    """The helper listed the files in its archive folder(s). Clips it saved whose file is not listed: still on the
    server -> saved again; on the PC only -> marked missing (a Problems line). Nothing is deleted because of this."""
    have = {str(x) for x in names}
    now, gone, resave, found = utcnow(), 0, 0, 0
    for c in s.query(Clip).filter(Clip.archived_at.isnot(None), Clip.archived_by == wid, Clip.pc_item_id.is_(None)):
        # (a video from the PC folder is not in the archive folder: the folder scan says when it is missing)
        need = [("video", c.archived_sha)] + ([("source", c.archived_src_sha)] if c.archived_src_sha else [])
        missing = [n for n, sha in need if not (saved_names(c.id, n, sha) & have)]
        if not missing:
            if c.archive_missing:
                c.archive_missing, c.archive_note = None, "The copy on your PC was found again."
                found += 1
            continue
        if all_back(c):  # the server still has everything: simply save it again
            c.archived_at, c.archived_by, c.archive_missing = None, None, None
            c.archive_note = "The copy on your PC was missing; it is being saved again."
            resave += 1
        elif not c.archive_missing:
            c.archive_missing = now
            c.archive_note = (f"The copy on your PC is missing from the archive folder ({', '.join(missing)}). "
                              "Nothing was deleted — if you moved the folder, put it back.")
            gone += 1
    return {"missing": gone, "saved_again": resave, "found_again": found}


# ---------------------------------------------------------------- clean-up
def crosspost_pending(c: Clip, p: Project | None, meta_on: bool, now) -> bool:
    """Instagram/Facebook fetch the file from the server: keep it until that post is done (or never will be)."""
    from .meta import CROSSPOST_MODES, _ig_pending, public_since

    if not meta_on or c.meta_done:
        return False
    if c.meta_result:  # tried: kept only while Instagram has not fetched the file yet (its container is not ready)
        return _ig_pending(c.meta_result) and public_since(c) >= now - CROSSPOST_WAIT
    if c.aspect != "9:16" or not p or p.mode not in CROSSPOST_MODES:
        return False
    return public_since(c) >= now - CROSSPOST_WAIT  # a scheduled video counts from when it goes public


def _keep_frame(c: Clip, p: Project | None):
    """Thumbnails made later (new text, the second try on low views) need a frame: save them before the video goes."""
    if c.thumb_custom or not p:
        return
    from .thumbs import save_frame

    for v in sorted({int(c.thumb_variant or 0), 1}):
        try:
            save_frame(c, p, v)
        except Exception:  # noqa: BLE001  (a thumbnail frame is not worth keeping a whole video for)
            pass


def free_uploaded(s, c: Clip, d: Path, meta_on: bool, now) -> bool:
    """Delete the server's video files of an uploaded clip once the PC has them. True when something was deleted."""
    if not complete(c) or c.restore_wanted or c.archive_missing:
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


def missing(s) -> int:
    return s.query(Clip).filter(Clip.archive_missing.isnot(None)).count()

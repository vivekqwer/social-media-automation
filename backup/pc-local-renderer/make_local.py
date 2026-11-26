"""Make the owner's parked clips into finished videos on this PC (D:\\youtube), for uploading later.

Nothing is uploaded and the server is never contacted. The clips come from a daily backup file of the dashboard;
the app code is used read-only from D:\\fix-main (download_section, render_clip, make_thumbnail, apply_short_cover),
so the videos get the same look as on the server.

Run (one command, from anywhere):
    "D:\\social medi engin\\.venv\\Scripts\\python.exe" D:\\youtube\\tool\\make_local.py

Options:
    --backup FILE   backup to read (default: the newest D:\\social medi engin\\backups\\clipstudio-*.json.gz)
    --ids FILE      clip ids, one per line (default: D:\\youtube\\tool\\ids.txt if it exists, else every clip whose
                    note starts with "Parked:")
    --limit N       make at most N videos in this run (the rest next time)
    --first DATE    first suggested upload day (default 2027-07-01); one a day at 18:00 IST, in clip-id order
    --dry-run       only list what would be made (no download, no render)
    --reload        load the backup into the local database again even if it was loaded before

Resumable: a clip whose folder already has video.mp4 is skipped. A clip that fails gets error.txt in its folder and
the run goes on. Everything is logged to D:\\youtube\\tool\\make_local.log.
"""
import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TOOL = Path(__file__).resolve().parent
OUT = TOOL.parent  # D:\youtube
DATA = TOOL / "data"
TMP = TOOL / "tmp"
APP = Path(os.getenv("RAJI_APP_DIR") or r"D:\fix-main")  # production code, used read-only
BACKUPS = Path(r"D:\social medi engin\backups")
FFMPEG_SRC = Path(r"D:\social medi engin\.venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v9.0.2.exe")
LOG = TOOL / "make_local.log"
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------- environment, BEFORE the app is imported
for d in (DATA, TMP, TOOL / "bin"):
    d.mkdir(parents=True, exist_ok=True)
ffmpeg = TOOL / "bin" / "ffmpeg.exe"  # yt-dlp only recognises a binary literally named ffmpeg
if not ffmpeg.exists():
    shutil.copy2(FFMPEG_SRC, ffmpeg)
for k in ("TMP", "TEMP", "TMPDIR"):
    os.environ[k] = str(TMP)
os.environ["DATA_DIR"] = str(DATA)
os.environ["DATABASE_URL"] = f"sqlite:///{(DATA / 'app.db').as_posix()}"
os.environ["FFMPEG_BIN"] = str(ffmpeg)
os.environ["BASE_URL"] = "http://localhost:8000"
for k in ("YT_COOKIES_FILE", "YT_PROXY", "RAILWAY_ENVIRONMENT", "RAILWAY_GIT_COMMIT_SHA", "TOKEN_ENCRYPTION_KEY", "FONTS_DIR",
          "FONT_PATH"):
    os.environ.pop(k, None)  # nothing from another setup may leak in
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True  # never write __pycache__ into the read-only app checkout
import tempfile  # noqa: E402

tempfile.tempdir = str(TMP)
os.chdir(TOOL)
sys.path.insert(0, str(APP))

log = logging.getLogger("make_local")


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # titles with emoji on a Windows console
    except (AttributeError, ValueError):
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


# ---------------------------------------------------------------- 1. backup -> fresh local SQLite
def newest_backup() -> Path:
    files = sorted(BACKUPS.glob("clipstudio-*.json.gz"))
    if not files:
        sys.exit(f"No backup file in {BACKUPS}")
    return files[-1]


def load_backup(path: Path, force: bool):
    """The app's own restore steps (check, row conversion, link check, one-transaction replace, faces), without the
    server-only parts (safety backup, job locks, restarting the scheduler)."""
    from app import backup, tenant
    from app.db import Base, engine, migrate

    blob = path.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    marker = DATA / "loaded.json"
    if not force and marker.exists():
        try:
            if json.loads(marker.read_text(encoding="utf-8")).get("sha256") == sha:
                log.info("local database already holds %s", path.name)
                return json.loads(marker.read_text(encoding="utf-8"))
        except ValueError:
            pass
    data = backup.unpack(blob, is_super=True)
    Base.metadata.create_all(engine)
    migrate(tenant.OWNER)
    prepared = {t.name: backup._rows(t, data["tables"].get(t.name) or []) for t in backup.tables()}
    faces = backup._faces(data.get("faces") or {})
    backup._check_links(prepared)
    settings = next(t for t in backup.tables() if t.name == "settings")
    backup._replace(tenant.OWNER, data, prepared, settings, True)
    backup._put_faces(faces)
    info = {"backup": str(path), "sha256": sha, "made_at": data.get("made_at"), "app_commit": data.get("app_commit"),
            "loaded_at": datetime.now(timezone.utc).isoformat(), "counts": {k: len(v) for k, v in prepared.items()},
            "faces": sorted(faces)}
    marker.write_text(json.dumps(info, indent=2), encoding="utf-8")
    log.info("loaded %s (made %s): %s, faces %s", path.name, info["made_at"], info["counts"], info["faces"])
    return info


# ---------------------------------------------------------------- 2./3. which clips
def read_ids(path: Path) -> list[int]:
    ids = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip().lstrip("#")
        if line.isdigit():
            ids.append(int(line))
    return sorted(set(ids))


def select(ids_file: Path | None) -> tuple[list[tuple[int, bool]], list[str], list[int]]:
    """The clips to make, in clip-id order, after the same duplicate rule the server uses when it un-parks a clip
    (autopilot.same_moment). Works on the local copy only. -> ([(clip id, in this backup)], skipped lines, waiting ids)
    An id that is not in the backup yet (newer than it) keeps its place in the order, so numbers and dates stay the
    same when a later backup brings it."""
    from app.autopilot import same_moment
    from app.db import Clip, SessionLocal

    skipped, waiting = [], []
    with SessionLocal() as s:
        if ids_file:
            want = read_ids(ids_file)
            rows = {c.id: c for c in s.query(Clip).filter(Clip.id.in_(want)).all()} if want else {}
            for i in want:
                if i not in rows:
                    waiting.append(i)
            chosen = [rows[i] for i in want if i in rows]
        else:
            chosen = s.query(Clip).filter(Clip.error.like("Parked:%")).order_by(Clip.id).all()
        out = []
        for c in chosen:
            if c.status in ("uploaded", "uploading") or c.youtube_id:
                skipped.append(f"clip {c.id}: already on YouTube ({c.youtube_id or c.status})")
                continue
            c.status = "draft"  # as parked on the server: a parked clip is not itself "made or waiting"
        s.flush()
        for c in chosen:
            if c.status != "draft":
                continue
            other = same_moment(s, c)
            if other is not None:
                c.error = f"Parked, not used: it repeats the moment of clip #{other.id} (same video, same part)."
                skipped.append(f"clip {c.id}: repeats the moment of clip #{other.id} ({other.status}), same video, "
                               f"{other.start:.0f}-{other.end:.0f}s vs {c.start:.0f}-{c.end:.0f}s")
                continue
            c.status = "rendered"  # made here: a later parked clip of the same moment is a repeat of this one
            out.append(c.id)
        s.rollback()  # the local copy stays exactly as the backup: a later run with another list starts clean
    order = sorted([(i, True) for i in out] + [(i, False) for i in waiting])
    return order, skipped, waiting


# ---------------------------------------------------------------- 4. one clip
def safe_title(t: str) -> str:
    t = re.sub(r"[^\w\- ]+", " ", t or "", flags=re.UNICODE)
    t = re.sub(r"[\s_]+", "-", t).strip("-. ")
    return t[:60].rstrip("-. ") or "clip"


def folder_for(num: int, clip_id: int, title: str) -> Path:
    want = OUT / f"{num:03d}_{safe_title(title)}"
    for info in OUT.glob("*/info.json"):  # a folder made for this clip earlier (the list may have changed since)
        try:
            if json.loads(info.read_text(encoding="utf-8")).get("clip_id") == clip_id:
                have = info.parent
                if have != want and not want.exists():
                    have.rename(want)  # its number changed: keep the folder, fix its name
                    return want
                return have
        except (ValueError, OSError):
            continue
    if want.exists():  # taken by another clip (the list changed): never mix two clips in one folder
        want = OUT / f"{num:03d}_{safe_title(title)}-clip{clip_id}"
    return want


def suggested(first: date, num: int) -> datetime:
    d = first + timedelta(days=num - 1)
    return datetime(d.year, d.month, d.day, 18, 0, tzinfo=IST)


def meta_for(project, clip) -> dict:
    from app import metadata

    built = metadata.build(project, clip, [])
    title = (clip.title or built["title"] or "").strip()[:100]
    desc = (clip.description or "").strip() or built["description"]
    if project.mode not in ("original", "own") and "Credit:" not in desc:
        desc = metadata._cut(desc + "\n\n" + metadata._credit_block(project, clip), 4900)
    tags = list(clip.tags or []) or built["tags"]
    return {"title": title, "description": desc, "tags": tags}


def probe(path: Path) -> dict:
    from app.ingest import stream_seconds, video_size

    return {"video_seconds": round(stream_seconds(path, "v:0"), 2), "audio_seconds": round(stream_seconds(path, "a:0"), 2),
            "size": list(video_size(path) or []), "bytes": path.stat().st_size}


def make_one(num: int, clip_id: int, first: date, backup_info: dict, dry: bool) -> str:
    import app.render as render
    from app.db import Clip, Destination, Project, SessionLocal
    from app.metadata import LICENSE_LINE
    from app.policy import check_edit
    from app.thumbs import make_thumbnail

    with SessionLocal() as s:
        clip = s.get(Clip, clip_id)
        project = s.get(Project, clip.project_id)
        dest = s.get(Destination, clip.destination_id) if clip.destination_id else None
        s.expunge_all()  # nothing done to these objects below goes back to the database
    meta = meta_for(project, clip)
    folder = folder_for(num, clip_id, meta["title"])
    video = folder / "video.mp4"
    when = suggested(first, num)
    if video.exists() and video.stat().st_size > 0:
        try:  # its place in the order changed (the list changed): the number and date follow
            old = json.loads((folder / "info.json").read_text(encoding="utf-8"))
            if old.get("number") != num or old.get("suggested_upload_ist") != when.isoformat():
                old.update(number=num, approve="manual", suggested_upload_ist=when.isoformat(),
                           suggested_upload_utc=when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                (folder / "info.json").write_text(json.dumps(old, indent=2, ensure_ascii=False), encoding="utf-8")
        except (ValueError, OSError):
            pass
        log.info("#%03d clip %s: already made (%s)", num, clip_id, folder.name)
        return "done"
    info = {
        "approve": "auto",  # owner (2026-11-25): everything should go by itself, no Approve press
        "number": num, "clip_id": clip_id, "project_id": project.id,
        "title": meta["title"], "description": meta["description"], "tags": meta["tags"],
        "source_link": f"https://www.youtube.com/watch?v={project.video_id}&t={int(clip.start or 0)}s",
        "source_video_title": project.title, "source_channel": project.channel, "source_channel_link": project.channel_url,
        "licence": project.license, "licence_line": LICENSE_LINE.get(project.mode, ""), "mode": project.mode,
        "start_seconds": clip.start, "end_seconds": clip.end, "length_seconds": round((clip.end or 0) - (clip.start or 0), 2),
        "format": clip.aspect, "made_for_channel": dest.name if dest else None,
        "suggested_upload_ist": when.isoformat(), "suggested_upload_utc": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "video_file": "video.mp4", "thumbnail_file": "thumbnail.jpg",
        "from_backup": Path(backup_info.get("backup") or "").name, "backup_made_at": backup_info.get("made_at"),
    }
    if dry:
        log.info("#%03d clip %s -> %s (%s, %.0fs, %s)", num, clip_id, folder.name, project.mode, info["length_seconds"],
                 info["suggested_upload_ist"])
        return "dry"
    folder.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    real_note = render._note
    render._note = lambda where, exc: notes.append(f"{where}: {str(exc)[:300]}")  # the server shows these as alerts
    t0 = time.time()
    try:
        errs = check_edit(project, clip)
        if errs:
            raise RuntimeError("Not allowed by the app's rules: " + "; ".join(errs))
        if clip.voice_path:
            notes.append("This clip had a voice-over on the server; the voice file is not in the backup, so it is left out.")
        if clip.thumb_custom:
            notes.append("This clip had its own uploaded thumbnail on the server; an automatic one was made instead.")
        out = render.render_clip(project, clip)
        clip.output_path = str(out)
        t_render = time.time() - t0
        clip.thumb_path = str(make_thumbnail(clip, project))
        render.apply_short_cover(clip)  # Shorts: the thumbnail as the first 0.7 s (as on the server)
        p = probe(out)
        if p["video_seconds"] < max(1.0, info["length_seconds"] * 0.9) or not p["size"]:
            raise RuntimeError(f"The finished video looks wrong (only {p['video_seconds']} s of picture).")
        shutil.copyfile(clip.thumb_path, folder / "thumbnail.jpg")
        info.update(made_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), video=p,
                    seconds_to_make=round(time.time() - t0, 1), seconds_download_and_render=round(t_render, 1), notes=notes)
        (folder / "info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        (folder / "description.txt").write_text(meta["description"] + "\n", encoding="utf-8")
        os.replace(out, folder / "video.tmp.mp4")  # moved, not copied: no second copy on the disk
        os.replace(folder / "video.tmp.mp4", video)  # video.mp4 appears only when it is complete
        if (folder / "error.txt").exists():
            os.replace(folder / "error.txt", folder / "error-earlier-run.txt")
        for n in notes:
            log.warning("#%03d clip %s: %s", num, clip_id, n)
        log.info("#%03d clip %s: made %s in %.0fs (%s s, %sx%s, %.1f MB)", num, clip_id, folder.name, time.time() - t0,
                 p["video_seconds"], *(p["size"] or ["?", "?"]), p["bytes"] / 1e6)
        return "made"
    except Exception as e:  # noqa: BLE001  (one clip failing must not stop the others)
        from app.ingest import friendly_error

        reason = friendly_error(e) if not str(e).startswith("Not allowed") else str(e)
        info.update(failed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), error=reason, notes=notes)
        (folder / "info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        (folder / "description.txt").write_text(meta["description"] + "\n", encoding="utf-8")
        (folder / "error.txt").write_text(
            f"Clip {clip_id} could not be made ({datetime.now().strftime('%Y-%m-%d %H:%M')}).\n\n{reason}\n\n"
            f"Run make_local.py again to retry it.\n", encoding="utf-8")
        log.error("#%03d clip %s: FAILED after %.0fs: %s", num, clip_id, time.time() - t0, reason[:400])
        return "failed"
    finally:
        render._note = real_note


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup", type=Path)
    ap.add_argument("--ids", type=Path)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--first", default="2027-07-01")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()
    setup_logging()
    first = date.fromisoformat(a.first)
    ids_file = a.ids or (TOOL / "ids.txt" if (TOOL / "ids.txt").exists() else None)
    bk = a.backup or newest_backup()
    log.info("---- start: backup %s, ids %s, app %s", bk, ids_file or "(clips noted 'Parked:')", APP)
    binfo = load_backup(bk, a.reload)
    ids, skipped, waiting = select(ids_file)
    for line in skipped:
        log.info("skipped %s", line)
    (TOOL / "skipped.txt").write_text("\n".join(skipped) + ("\n" if skipped else ""), encoding="utf-8")
    (TOOL / "waiting.txt").write_text(
        "".join(f"clip {i}: waiting for tomorrow's backup (not in {bk.name})\n" for i in waiting), encoding="utf-8")
    log.info("%d clips in the list: %d in this backup, %d waiting for tomorrow's backup, %d skipped",
             len(ids), len(ids) - len(waiting), len(waiting), len(skipped))
    count = {"made": 0, "done": 0, "failed": 0, "dry": 0, "waiting": 0}
    for num, (cid, here) in enumerate(ids, 1):
        if a.limit and count["made"] + count["failed"] >= a.limit:
            log.info("limit of %d reached; run again for the rest", a.limit)
            break
        if not here:
            log.info("#%03d clip %s: waiting for tomorrow's backup (it is newer than %s)", num, cid, bk.name)
            count["waiting"] += 1
            continue
        try:
            count[make_one(num, cid, first, binfo, a.dry_run)] += 1
        except Exception:  # noqa: BLE001  (e.g. the folder could not be written): log it, go on with the next
            log.exception("#%03d clip %s: unexpected problem", num, cid)
            count["failed"] += 1
    log.info("---- end: %s", count)


if __name__ == "__main__":
    main()

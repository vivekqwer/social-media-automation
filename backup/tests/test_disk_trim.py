"""A big queue fits on the server disk: a ready, queued clip keeps only its finished video and thumbnail."""
import os, sys, tempfile, time
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"
sys.path.insert(0, r"D:\integ-final")
from app import scheduler
from app.db import Base, engine, migrate, SessionLocal, Clip, Project
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
Base.metadata.create_all(engine); migrate()


def folder(cid, extra=()):
    w = Path(d) / "clips" / str(cid); (w / "src").mkdir(parents=True)
    for n in ("out.mp4", "body.mp4", "thumb.jpg", "src/src_0_31.mp4", *extra):
        (w / n).write_bytes(b"x" * 1000)
    old = time.time() - 3 * 3600
    os.utime(w, (old, old))
    return w


with SessionLocal() as s:
    p = Project(url="u", video_id="abcdefghijk", title="t", mode="cc", duration=60); s.add(p); s.commit()
    rows = [("queued", ()), ("rendered", ()), ("queued", ("src/upload.mp4",)), ("queued", ("src/reel.mp4",))]
    ids = []
    for st, extra in rows:
        c = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status=st); s.add(c); s.commit()
        w = folder(c.id, extra)
        c.output_path, c.thumb_path = str(w / "out.mp4"), str(w / "thumb.jpg"); s.commit()
        os.utime(w, (time.time() - 3 * 3600,) * 2)
        ids.append(c.id)
scheduler.cleanup_disk()
W = lambda i, n: (Path(d) / "clips" / str(i) / n).exists()
ok(W(ids[0], "out.mp4") and W(ids[0], "thumb.jpg") and not W(ids[0], "body.mp4") and not W(ids[0], "src/src_0_31.mp4"), "queued + ready: only the finished video and thumbnail stay")
ok(W(ids[1], "body.mp4") and W(ids[1], "src/src_0_31.mp4"), "a clip waiting for review keeps everything (it may still be edited)")
ok(W(ids[2], "src/upload.mp4") and W(ids[2], "body.mp4"), "an own uploaded video is never trimmed (the only copy)")
ok(W(ids[3], "src/reel.mp4"), "a helper-made reel keeps its source")

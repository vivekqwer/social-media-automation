"""A helper that is busy with a long job is not reported as offline."""
import os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "helper-busy-test-71"
sys.path.insert(0, r"D:/integ-final")
from app import main, workers
from app.db import Base, engine, migrate, SessionLocal, Clip, Project, Worker, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
Base.metadata.create_all(engine); migrate()
with SessionLocal() as s:
    p = Project(url="u", video_id="abcdefghijk", title="t", mode="cc", duration=60); s.add(p); s.commit()
    w = Worker(name="pc", token_hash=workers.token_hash("tok"), approved=True, last_seen=utcnow() - timedelta(minutes=15)); s.add(w)
    c = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="fetching", job_lease=utcnow() - timedelta(minutes=14)); s.add(c); s.commit()
    ok(main._helper_state(s)["online"] == 1, "seen 15 min ago, job taken 14 min ago: working, not offline")
    c.job_lease = None; s.commit()
    ok(main._helper_state(s)["online"] == 0, "seen 15 min ago and no job in hand: offline")
    c.job_lease = utcnow() - timedelta(minutes=50); w.last_seen = utcnow() - timedelta(minutes=50); s.commit()
    ok(main._helper_state(s)["online"] == 0, "job taken 50 min ago and silent since: offline")
    w.last_seen = utcnow(); s.commit()
    ok(main._helper_state(s)["online"] == 1, "just called in: online")

"""The helper runs video downloads side by side and everything else one after another; the server hands out 3."""
import os, sys, tempfile, threading, time
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
ROOT = r"D:/integ-final"
sys.path.insert(0, ROOT); sys.path.insert(0, ROOT + "/worker")
sys.argv = ["x", "http://localhost:9"]
import importlib.util
spec = importlib.util.spec_from_file_location("cw", ROOT + "/worker/clipstudio_worker.py"); cw = importlib.util.module_from_spec(spec); spec.loader.exec_module(cw)
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
cw.log = lambda m: None
active, peak, order, lock = [0], [0], [], threading.Lock()


def fake(job, headers):
    with lock:
        active[0] += 1; peak[0] = max(peak[0], active[0]); order.append(("start", job["type"], job.get("clip_id")))
    if job.get("boom"):
        with lock: active[0] -= 1
        raise RuntimeError("boom")
    time.sleep(0.3)
    with lock:
        active[0] -= 1; order.append(("end", job["type"], job.get("clip_id")))


cw.handle = fake
t0 = time.time()
cw.handle_all([{"type": "source", "clip_id": 1}, {"type": "source", "clip_id": 2}, {"type": "source", "clip_id": 3}], {})
ok(peak[0] == 3 and time.time() - t0 < 0.8, f"three downloads run at the same time (peak {peak[0]}, {time.time() - t0:.1f} s)")
peak[0] = 0; order.clear()
cw.handle_all([{"type": "source", "clip_id": 1}, {"type": "reel", "clip_id": 9}, {"type": "analyze"}, {"type": "source", "clip_id": 2}], {})
kinds = [k for a, k, _ in order if a == "start"]
ok(kinds[:2] == ["source", "source"] and kinds[2:] == ["reel", "analyze"] and peak[0] == 2, f"downloads together first, then the other jobs one by one ({kinds}, peak {peak[0]})")
peak[0] = 0; order.clear()
cw.handle_all([{"type": "source", "clip_id": 1, "boom": True}, {"type": "source", "clip_id": 2}, {"type": "tts", "clip_id": 3}], {})
ok(("end", "source", 2) in order and ("end", "tts", 3) in order, "one failing job does not stop the others")
peak[0] = 0
cw.handle_all([{"type": "source", "clip_id": 1}], {}); cw.handle_all([], {})
ok(peak[0] == 1, "a single job and an empty list work as before")
ok(cw.PARALLEL == 3 and hasattr(cw._HEAVY, "acquire"), "captions (Whisper) are written one at a time")
# the server hands out up to 3 downloads in one go
from app import main, workers
from app.db import Base, engine, migrate, SessionLocal, Clip, Project, Worker, utcnow
from fastapi.testclient import TestClient
with TestClient(main.app, raise_server_exceptions=False) as c:
    with SessionLocal() as s:
        p = Project(url="u", video_id="abcdefghijk", title="t", mode="cc", duration=600); s.add(p); s.commit()
        s.add(Worker(name="pc", token_hash=workers.token_hash("tok-" + "a" * 40), approved=True, last_seen=utcnow()))
        for i in range(5):
            s.add(Clip(project_id=p.id, start=i * 40, end=i * 40 + 30, aspect="9:16", status="fetching", captions=[{"s": 0, "e": 1, "t": "x"}]))
        s.commit()
    r = c.get("/api/worker/jobs", headers={"x-worker-token": "tok-" + "a" * 40})
    jobs = [j for j in r.json().get("jobs", []) if j["type"] == "source"]
    ok(r.status_code == 200 and len(jobs) == 3, f"3 downloads in one go ({r.status_code}, {len(jobs)})")
    r = c.get("/api/worker/jobs", headers={"x-worker-token": "tok-" + "a" * 40})
    jobs2 = [j for j in r.json().get("jobs", []) if j["type"] == "source"]
    ok(len(jobs2) == 2 and not {j["clip_id"] for j in jobs} & {j["clip_id"] for j in jobs2}, f"the next call gets the other 2, never the same clip twice ({len(jobs2)})")

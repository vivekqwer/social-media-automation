import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "multi-test-pass-51"
sys.path.insert(0, r"D:\integ-final")
import datetime as dt
from fastapi.testclient import TestClient
from app import main, quick
from app.db import SessionLocal, Destination, Clip
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
CC = "Creative Commons Attribution license (reuse allowed)"
main.fetch_info = lambda url: {"video_id": "abcdefghijk", "title": "Street food", "channel": "Cook", "channel_url": "", "license": CC,
                               "description": "", "tags": [], "duration": 900, "heatmap": []}
quick.fetch_heatmap = lambda v: [{"start_time": i * 10, "end_time": i * 10 + 10, "value": {20: 1.0, 50: 0.9, 80: 0.8}.get(i, 0.1)} for i in range(90)]
quick.fetch_comments = lambda v: []
quick.fetch_cues = lambda *a: []
quick.auto_metadata = lambda cid: None
quick.run_render = lambda cid: None
main.executor = type("E", (), {"submit": staticmethod(lambda fn, *a: fn(*a))})()
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "multi-test-pass-51"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
    at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    r = tc.post("/api/quick", json={"url": "https://www.youtube.com/watch?v=abcdefghijk", "format": "shorts", "publish_at": at, "count": 3})
    ok(r.status_code == 200, "quick started with 3 clips")
    with SessionLocal() as s:
        cl = s.query(Clip).order_by(Clip.id).all()
    starts = sorted(c.start for c in cl)
    ok(len(cl) == 3, f"3 clips made ({len(cl)})")
    ok(all(b - a >= 30 for a, b in zip(starts, starts[1:])), f"three different moments {starts}")
    times = [c.publish_at for c in cl]
    ok(len(set(times)) == 3 and times[0] < times[1] < times[2], f"each clip has its own time slot {times}")
    q = tc.get(f"/api/quick/{r.json()['project_id']}").json()
    ok(q["clip_count"] == 3, "progress shows 3 clips")

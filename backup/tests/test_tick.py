import os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "tick-test-pass-55"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, autopilot
from app.db import SessionLocal, Channel, Destination, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "tick-test-pass-55"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
    r = tc.post("/api/channels", json={"query": "https://www.youtube.com/shorts/Q46S5BfashM"})
    ok(r.status_code == 400 and "single video" in r.json()["detail"], "video link refused as a source")
    with SessionLocal() as s:
        s.add_all([Channel(kind="topic", query="a", auto=True, last_run=utcnow() - timedelta(hours=7), destination_id=1),
                   Channel(kind="topic", query="b", auto=True, last_run=utcnow() - timedelta(hours=1), destination_id=1),
                   Channel(kind="topic", query="c", auto=True, last_run=None, destination_id=1)]); s.commit()
    ran = []
    autopilot.run_channel = lambda i: ran.append(i)
    autopilot.autopilot_tick()
    ok(sorted(ran) == [1, 3], f"only due sources run ({ran})")

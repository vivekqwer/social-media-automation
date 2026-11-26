import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "health-test-pass-31"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main
from app.db import SessionLocal, Destination, Project, Clip
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
main.run_render = lambda cid: None
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "health-test-pass-31"})
    h = tc.get("/api/health").json(); ok(any(i["fix"] == "connect" for i in h["items"]), "no channel -> connect")
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x", paused=True, paused_reason="strike")); s.commit()
        p = Project(url="u", video_id="abcdefghijk", title="T", channel="C", mode="cc", license="Creative Commons Attribution license (reuse allowed)", duration=100); s.add(p); s.commit()
        s.add(Clip(project_id=p.id, start=0, end=30, status="failed", error="x")); s.commit()
    fixes = {i["fix"] for i in tc.get("/api/health").json()["items"]}
    ok({"resume", "retry_failed", "helper"} <= fixes, f"paused/failed/helper listed {fixes}")
    ok(tc.post("/api/fix/retry-failed").json()["retried"] == 1, "retry all failed")

"""How many clips may wait before the sources pause: usually 30, the Super Admin can raise it; a nearly full disk pauses too."""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, r"D:/integ-final")
from fastapi.testclient import TestClient
from app import main, autopilot, auth, jobs
from app.db import SessionLocal, Channel, Clip, Destination, Project, set_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
with TestClient(main.app, raise_server_exceptions=False) as c, TestClient(main.app, raise_server_exceptions=False) as anon, TestClient(main.app, raise_server_exceptions=False) as cu:
    c.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    r = c.get("/api/settings/queue-cap").json()
    ok(r == {"cap": 30, "usual": 30, "range": [10, 300]} and autopilot.queue_cap() == 30, f"nothing chosen: the usual 30 ({r})")
    ok(c.post("/api/admin/queue-cap", json={"cap": 5}).status_code == 422 and c.post("/api/admin/queue-cap", json={"cap": 5000}).status_code == 422, "numbers outside 10-300 are refused")
    r = c.post("/api/admin/queue-cap", json={"cap": 200})
    ok(r.status_code == 200 and r.json() == {"cap": 200} and autopilot.queue_cap() == 200, "the Super Admin raises it to 200")
    set_setting("queue_cap", "abc")
    ok(autopilot.queue_cap() == 30, "a damaged setting falls back to 30")
    set_setting("queue_cap", "99999")
    ok(autopilot.queue_cap() == 300, "never more than 300")
    ok(anon.post("/api/admin/queue-cap", json={"cap": 200}).status_code in (401, 403), "not signed in: refused")
    anon.post("/api/register", json={"name": "Asha", "brand": "Asha Studio", "email": "asha@example.com", "password": "customer-pass-12345", "accept_terms": True})
    aid = c.get("/api/admin/users").json()["users"][0]["id"]
    c.post(f"/api/admin/users/{aid}/approve")
    cu.post("/api/login", json={"username": "asha@example.com", "password": "customer-pass-12345"})
    ok(cu.post("/api/admin/queue-cap", json={"cap": 200}).status_code == 403, "a customer cannot raise it")
    # the source really pauses at the chosen number, and on a nearly full disk
    with SessionLocal() as s:
        dst = Destination(channel_id="UCx", name="Main", token="x"); s.add(dst); s.commit()
        p = Project(url="u", video_id="abcdefghijk", title="t", mode="cc", duration=60); s.add(p); s.commit()
        ch = Channel(kind="channel", query="q", name="Src", mode="cc", auto=True, destination_id=dst.id, resolved_url="https://x", videos_per_run=1, clips_per_video=1); s.add(ch); s.commit()
        for i in range(35):
            s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="queued", destination_id=dst.id))
        s.commit(); chid = ch.id
    calls = []
    autopilot.list_videos = lambda *a, **k: calls.append(1) or []
    for name in ("channel_videos", "list_channel_videos", "latest_videos", "search_videos"):
        if hasattr(autopilot, name):
            setattr(autopilot, name, lambda *a, **k: calls.append(1) or [])
    set_setting("queue_cap", "")
    try:
        autopilot.run_channel(chid)
    except Exception as e:  # noqa: BLE001
        print("NOTE run:", str(e)[:100])
    with SessionLocal() as s:
        st30 = s.get(Channel, chid).last_status or ""
    ok(st30.startswith("paused: 35 clips already queued"), f"35 waiting, usual cap 30: paused ({st30[:60]})")
    set_setting("queue_cap", "200")
    autopilot.disk_free_gb = lambda: 0.3
    try:
        autopilot.run_channel(chid)
    except Exception as e:  # noqa: BLE001
        print("NOTE run:", str(e)[:100])
    with SessionLocal() as s:
        std = s.get(Channel, chid).last_status or ""
    ok("disk is almost full" in std, f"cap 200 but only 0.3 GB free: waits for space ({std[:70]})")
    autopilot.disk_free_gb = lambda: 3.0
    try:
        autopilot.run_channel(chid)
    except Exception as e:  # noqa: BLE001
        print("NOTE run:", str(e)[:100])
    with SessionLocal() as s:
        st200 = s.get(Channel, chid).last_status or ""
    ok(not st200.startswith("paused"), f"cap 200, 35 waiting, disk fine: the source runs again ({st200[:70]})")

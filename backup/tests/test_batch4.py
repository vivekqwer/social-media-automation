import os, sys, tempfile, json
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "b4-test-pass-41"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, autopilot, metadata, stats, workers
from app.db import SessionLocal, Destination, Project, Clip, Channel, VideoStat, utcnow
from app.crypto import encrypt
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "b4-test-pass-41"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UCmine123456789012345678", name="Foody", token="x")); s.commit()
    # affiliate
    r = tc.post("/api/settings/affiliate", json={"rules": [{"keywords": "tawa, pressure cooker", "text": "My tawa: https://amzn.to/abc"}]})
    ok(r.status_code == 200, "affiliate rules saved")
    ok(tc.post("/api/settings/affiliate", json={"rules": [{"keywords": "x", "text": "bad http://evil"}]}).status_code == 400, "non-https link refused")
    p = Project(url="u", video_id="abcdefghijk", title="Crispy dosa on a tawa", channel="C", channel_url="", mode="cc", tags=["dosa"], description="d")
    c = Clip(start=0, end=30, aspect="9:16")
    m = metadata.build(p, c, ["dosa recipe"])
    ok("https://amzn.to/abc" in m["description"] and "affiliate" in m["description"], "matching affiliate line added")
    m2 = metadata.build(Project(url="u", video_id="x", title="Paneer tikka", channel="C", channel_url="", mode="cc", tags=[], description=""), c, [])
    ok("amzn" not in m2["description"], "no line when keywords don't match")
    # own channel
    p3 = Project(url="u", video_id="OWNVIDEO001", title="My long vlog", channel="Foody", channel_url="", mode="own", tags=[], description="full vlog")
    m3 = metadata.build(p3, c, [])
    ok("Full video: https://youtu.be/OWNVIDEO001" in m3["description"] and "Credit:" not in m3["description"], "own video: link back, no credit")
    r = tc.post("/api/channels", json={"kind": "channel", "query": "@foody", "mode": "own", "destination_id": 1})
    ok(r.status_code == 200 and r.json()["mode"] == "own", "own-channel source allowed")
    autopilot.fetch_info = lambda url: {"video_id": url[-11:], "title": "t", "channel": "Other", "channel_url": "https://www.youtube.com/channel/UCsomeoneelse", "license": None, "description": "", "tags": [], "duration": 600, "heatmap": []}
    log = []
    with SessionLocal() as s:
        ch = s.get(Channel, r.json()["id"])
        res = autopilot._process_video(s, ch, "OTHERVID001", log)
    ok(res is False and "not on one of your connected channels" in log[0], "someone else's video refused in own mode")
    # pexels key reaches the reel job
    main.set_setting("pexels_key", encrypt("px-test-key"))
    reg = tc.post("/api/worker/register", json={"name": "pc"}).json()
    tc.post(f"/api/workers/{reg['worker_id']}/approve", json={"code": workers.pair_code(reg["token"])})
    with SessionLocal() as s:
        rc = Channel(kind="reel", query="Food facts", mode="cc", destination_id=1, scripts=[]); s.add(rc); s.commit()
        pr = Project(url="", video_id=None, title="Samosa facts", channel="Foody", mode="original", description="Samosa script", channel_ref=rc.id); s.add(pr); s.commit()
        s.add(Clip(project_id=pr.id, start=0, end=60, status="fetching", fetch_since=utcnow())); s.commit()
    jobs = tc.get("/api/worker/jobs", headers={"x-worker-token": reg["token"]}).json()["jobs"]
    rj = [j for j in jobs if j["type"] == "reel"][0]
    ok(rj["pexels_key"] == "px-test-key" and rj["photo_query"] == "Food facts", "Pexels key + photo topic sent with the reel job")
    # best time + health
    with SessionLocal() as s:
        now = utcnow()
        for i in range(9):
            h = 4 if i % 2 else 12  # 04:30 UTC = 10:00 IST
            s.add(VideoStat(video_id=f"VS{i:09d}", destination_id=1, title="v", privacy="public", views=1000 if h == 4 else 100,
                            published_at=(now - timedelta(days=3)).replace(hour=h, minute=30), updated_at=now))
        s.commit()
    o = stats.overview()["channels"][0]
    ok(o["best_hours"]["enough"] and o["best_hours"]["ranked"][0]["hour"] == 10, f"best upload time learned: {o['best_hours']['ranked'][:2]}")
    ok(o["health"]["score"] == 100 and o["health"]["notes"] == ["no problems found"], f"health score {o['health']}")

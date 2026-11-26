import os, sys, tempfile
from datetime import datetime, timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "gaps-test-pass-12"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, workers, competitors
from app.db import SessionLocal, Destination, Channel, Clip, Project, Worker, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
started = []
main.executor.submit = lambda fn, *a, **k: started.append((getattr(fn, "__name__", "?"), a))
competitors.trending = lambda cat="": []
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "gaps-test-pass-12"})
    with SessionLocal() as s:
        a = Destination(channel_id="UCa", name="Foody", token="x"); b = Destination(channel_id="UCb", name="Funny", token="x"); s.add_all([a, b]); s.commit(); A, B = a.id, b.id
        t = Channel(kind="topic", query="street food", name="Street food", mode="cc", destination_id=A, auto=True, videos_per_run=2, clips_per_video=1, clip_len=35, aspect="9:16", post_times="10:00, 18:00", review_first=False)
        r = Channel(kind="reel", query="Food trends", name="AI reels", mode="original", destination_id=A, auto=True, auto_scripts=True, permission_note="en", scripts=[], videos_per_run=1, review_first=True)
        s.add_all([t, r]); s.commit(); T, R = t.id, r.id
    # (1) edit a source
    e = tc.put(f"/api/channels/{T}", json={"name": "Indian street food", "videos_per_run": 1, "clips_per_video": 2, "clip_len": 45, "aspect": "16:9",
                                           "post_times": "9:00,21:30", "review_first": True, "captions": True, "destination_id": B, "interval_hours": 6})
    j = e.json()
    ok(e.status_code == 200 and (j["name"], j["videos_per_run"], j["clips_per_video"], j["clip_len"], j["aspect"], j["post_times"], j["review_first"], j["captions"], j["destination_id"], j["interval_hours"])
       == ("Indian street food", 1, 2, 45, "16:9", "09:00, 21:30", True, True, B, 6), f"source edited: {j['post_times']}, {j['aspect']}, channel {j['destination_id']}")
    ok(tc.put(f"/api/channels/{T}", json={"post_times": "25:00"}).status_code == 400 and tc.put(f"/api/channels/{T}", json={"post_times": "morning"}).status_code == 400, "wrong upload times are refused with a message")
    ok(tc.put(f"/api/channels/{T}", json={"clip_len": 500}).status_code == 422 and tc.put(f"/api/channels/{T}", json={"destination_id": 999}).status_code == 404, "out-of-range values / unknown channel refused")
    j = tc.put(f"/api/channels/{T}", json={"captions": False}).json()
    ok(j["captions"] is False and j["clip_len"] == 45 and j["name"] == "Indian street food", "only the sent field changes")
    j = tc.put(f"/api/channels/{R}", json={"aspect": "16:9", "clip_len": 99, "videos_per_run": 2, "post_times": ""}).json()
    ok(j["aspect"] != "16:9" and j["videos_per_run"] == 2 and j["post_times"] == "", "reel source: stays vertical; other fields change")
    ok(tc.put("/api/channels/999", json={"name": "x"}).status_code == 404, "unknown source -> 404")
    # (2) make all waiting clips again
    with SessionLocal() as s:
        p = Project(url="u", video_id="abcdefghijk", title="T", channel="C", mode="cc", license="Creative Commons Attribution license (reuse allowed)", duration=600); s.add(p); s.commit()
        when = utcnow() + timedelta(days=2)
        s.add_all([Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="queued", autoqueue=True, publish_at=when, destination_id=A, fit="fill", title="a", description="d"),
                   Clip(project_id=p.id, start=40, end=70, aspect="9:16", status="rendered", autoqueue=True, destination_id=A, title="b", description="d"),
                   Clip(project_id=p.id, start=80, end=110, aspect="16:9", status="queued", autoqueue=True, publish_at=when, destination_id=A, fit=None, title="c", description="d"),
                   Clip(project_id=p.id, start=0, end=20, aspect="9:16", status="uploaded", youtube_id="Y", destination_id=A),
                   Clip(project_id=p.id, start=0, end=20, aspect="9:16", status="failed", destination_id=A),
                   Clip(project_id=p.id, start=5, end=6, aspect="9:16", status="queued", autoqueue=True, destination_id=A)])  # 1 s: breaks a rule
        s.commit()
    started.clear()
    r = tc.post("/api/fix/rerender-waiting", json={"fit": "full"}).json()
    with SessionLocal() as s:
        cs = {c.id: c for c in s.query(Clip).all()}
    ok(r["started"] == 3 and r["skipped"] == 1 and len(started) == 3, f"3 waiting clips re-rendered, the rule-breaking one skipped: {r}")
    ok(cs[1].status == "rendering" and cs[1].fit == "full" and cs[1].autoqueue and cs[1].publish_at is not None, "queued clip: new picture option, stays in the queue with its time")
    ok(cs[2].status == "rendering" and cs[2].autoqueue is False, "review clip comes back to review, not to the queue")
    ok(cs[3].fit is None and cs[4].status == "uploaded" and cs[5].status == "failed", "16:9 keeps its setting; uploaded and failed clips untouched")
    # (3) reject an AI reel and write a new one
    with SessionLocal() as s:
        pr = Project(url="", title="Hey there!", channel="Foody", mode="original", license="Original content (my own reel)", description="bad script", duration=0, channel_ref=R); s.add(pr)
        pt = Project(url="u", video_id="bbbbbbbbbbb", title="T", channel="C", mode="cc", duration=60, channel_ref=T); s.add(pt); s.commit()
        k1 = Clip(project_id=pr.id, start=0, end=60, aspect="9:16", status="rendered", destination_id=A); k2 = Clip(project_id=pt.id, start=0, end=30, aspect="9:16", status="rendered", destination_id=A)
        s.add_all([k1, k2]); s.commit(); K1, K2 = k1.id, k2.id
    ov = tc.get("/api/overview").json()
    card = tc.get(f"/api/clips/{K1}").json()
    ok(ov["ai_sources"] == [R] and card["source_id"] == R, "the dashboard knows which clips the AI wrote")
    ok(tc.post(f"/api/clips/{K2}/rewrite").status_code == 400, "a normal clip cannot be 'written again'")
    r = tc.post(f"/api/clips/{K1}/rewrite")
    ok(r.status_code == 409 and "helper PC is offline" in r.json()["detail"], "helper offline: explained, the reel is NOT thrown away")
    with SessionLocal() as s:
        s.add(Worker(name="pc", token_hash=workers.token_hash("tok"), approved=True, last_seen=utcnow(), llm=True)); s.commit()
        ok(s.get(Clip, K1) is not None, "reel still there")
    r = tc.post(f"/api/clips/{K1}/rewrite")
    with SessionLocal() as s:
        ch = s.get(Channel, R)
        ok(r.status_code == 200 and s.get(Clip, K1) is None and ch.script_wanted and "food" in ch.script_wanted["topic"].lower(), f"reel discarded, new script asked: {r.json().get('topic')}")
    jobs = tc.get("/api/worker/jobs", headers={"x-worker-token": "tok", "x-worker-llm": "1", "x-worker-ai": "ready"}).json()["jobs"]
    ok(any(j["type"] == "script" and j["source_id"] == R and j["lang"] == "en" for j in jobs), "the helper gets the script job at once")

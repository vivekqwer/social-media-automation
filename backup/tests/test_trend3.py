import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "t3-test-pass-71"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, autopilot, youtube, competitors, permissions, workers, telegram
from app.db import SessionLocal, Destination, Channel, Project, Clip
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
TREND = [{"video_id": "TRSONG00001", "title": "Pyaar Hua Official Music Video", "channel": "T-Series", "views": 9e6, "tags": [], "channel_id": "UCsong", "cc": False},
         {"video_id": "TRFOOD00001", "title": "Kolkata Street Food Tour | Puchka", "channel": "Foodie Raj", "views": 5e5, "tags": ["kolkata street food", "puchka"], "channel_id": "UCfoodieraj", "cc": False},
         {"video_id": "TRGAME00001", "title": "Minecraft Hardcore Day 100", "channel": "GamerX", "views": 3e5, "tags": [], "channel_id": "UCgamerx", "cc": False}]
competitors.trending = lambda category="": TREND
searched = []
youtube.api_search_cc = lambda q, skip, n, pages=3: searched.append(q) or []
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "t3-test-pass-71"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
    # 1. trending source
    r = tc.post("/api/channels", json={"kind": "trending", "query": "26", "destination_id": 1, "interval_hours": 3, "videos_per_run": 1})
    ok(r.status_code == 200 and r.json()["interval_hours"] == 3 and r.json()["mode"] == "cc", "trending source (CC, every 3 h)")
    tid = r.json()["id"]
    autopilot.run_channel(tid)
    with SessionLocal() as s:
        ch = s.get(Channel, tid)
    ok(searched == ["kolkata street food"], f"songs skipped, topic from tags searched: {searched}")
    ok(ch.query == "26" and "kolkata street food" in (ch.seen_topics or []), "category kept, topic remembered")
    autopilot.run_channel(tid)
    ok(searched[-1] == "minecraft hardcore", f"next run uses the next trend: {searched[-1]}")
    # 2. permission inbox
    sent = []
    telegram.notify = lambda text, buttons=None: sent.append((text, buttons))
    permissions.notify = None
    new = permissions.refresh_inbox()
    ok([c["name"] for c in new] == ["Foodie Raj", "GamerX"], f"creators of non-CC, non-music videos: {[c['name'] for c in new]}")
    ok("Foodie Raj" in new[0]["message"] and "credit" in new[0]["message"], "ready message with their name")
    ok(tc.post("/api/permissions/UCfoodieraj/grant", json={"proof": "yes"}).status_code == 400, "proof required")
    r = tc.post("/api/permissions/UCfoodieraj/grant", json={"proof": "Instagram reply from @foodieraj on 22 Nov 2026"})
    with SessionLocal() as s:
        src = s.get(Channel, r.json()["source_id"])
    ok(r.status_code == 200 and src.mode == "permission" and src.auto and not src.review_first and "Instagram" in src.permission_note,
       "yes -> automatic permission source with the proof")
    ok(tc.post("/api/permissions/UCgamerx/mark/skipped").json()["status"] == "skipped", "skip works")
    # telegram flow: button + reply
    from app.db import set_setting
    msg = telegram._handle_callback("py:UCgamerx")
    ok("reply" in msg.lower(), "Telegram 'They said yes' asks for the proof")
    # 3. AI scripts reel source
    r = tc.post("/api/channels", json={"kind": "reel", "query": "Trends", "scripts": [], "auto_scripts": True, "language": "hi", "destination_id": 1})
    ok(r.status_code == 200 and r.json()["auto_scripts"] and r.json()["interval_hours"] == 4, "AI-scripts reel source")
    rid = r.json()["id"]
    autopilot.run_channel(rid)
    with SessionLocal() as s:
        w = s.get(Channel, rid).script_wanted
    ok(w and w["topic"], f"asked the local AI for a script: {w}")
    reg = tc.post("/api/worker/register", json={"name": "pc"}).json()
    tc.post(f"/api/workers/{reg['worker_id']}/approve", json={"code": workers.pair_code(reg["token"])})
    H = {"x-worker-token": reg["token"]}
    ok(not [j for j in tc.get("/api/worker/jobs", headers={**H, "x-worker-llm": "0"}).json()["jobs"] if j["type"] == "script"], "no script job for a helper without AI")
    jobs = [j for j in tc.get("/api/worker/jobs", headers={**H, "x-worker-llm": "1"}).json()["jobs"] if j["type"] == "script"]
    ok(len(jobs) == 1 and jobs[0]["lang"] == "hi", "helper with local AI gets the script job")
    main.executor = type("E", (), {"submit": staticmethod(lambda fn, *a: fn(*a))})()
    r = tc.post(f"/api/worker/script/{rid}", headers=H, json={"topic": w["topic"], "script": "Kya aap jaante ho? Puchka Kolkata ka sabse pasand snack hai. Comment karo aur follow karo!"})
    with SessionLocal() as s:
        ch = s.get(Channel, rid)
        reels = s.query(Project).filter(Project.channel_ref == rid).all()
    ok(r.status_code == 200 and ch.script_wanted is None and len(reels) == 1 and reels[0].mode == "original", "script -> reel job created automatically")

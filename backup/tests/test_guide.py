import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "guide-test-pass-7"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, guide, jobs
from app.db import SessionLocal, Destination, Channel, Clip, Project
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
with TestClient(main.app, raise_server_exceptions=False) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "guide-test-pass-7"})
    g = tc.get("/api/guide").json()
    keys = [i["key"] for i in g["setup"]["items"]]
    ok(keys[0] == "youtube" and "source" in keys and "verify" in keys and "main" not in keys, f"fresh system: steps {keys}")
    ok(all(i["how"] for i in g["setup"]["items"]), "every step has how-to instructions: " + ", ".join(k for k in keys if not guide.HOW.get(k)))
    yt = [i for i in g["setup"]["items"] if i["key"] == "youtube"][0]
    ok(not yt["done"] and yt["action"]["href"] == "/auth/login" and any("Advanced" in h for h in yt["how"]), "step 1: connect YouTube, with Google's screens explained")
    ok(any(p.get("fix") == "connect" for p in g["problems"]["items"]), "problem list: no channel -> connect")
    ok(len(g["help"]) >= 15 and all(h["q"] and h["auto"] and h["do"] for h in g["help"]), f"{len(g['help'])} what-if answers")
    with SessionLocal() as s:
        a = Destination(channel_id="UCa", name="Foody", token="x"); b = Destination(channel_id="UCb", name="Funny", token="x"); s.add_all([a, b]); s.commit()
        p = Project(url="u", video_id="abcdefghijk", title="T", channel="C", mode="cc", duration=60); s.add(p); s.commit()
        s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="Y", thumb_pending=True, destination_id=a.id)); s.commit()
    g = tc.get("/api/guide").json()
    items = {i["key"]: i for i in g["setup"]["items"]}
    ok("main" in items and "Main: Foody" in items["main"]["why"], "two channels: a 'Main channel' step appears")
    ok(not items["verify"]["done"] and "refused" in items["verify"]["blocked"] and items["verify"]["action"]["url"].endswith("/verify"), "refused thumbnail -> phone verification step with link")
    ok(not items["source"]["done"] and items["source"]["action"]["open"] == "add_source", "no source -> 'Add source' step")
    fixes = [p.get("fix") for p in g["problems"]["items"]]
    ok("verify" in fixes and "add_source" in fixes, f"problem list has them too: {fixes}")
    # unexpected server error: plain message, saved for Copy details
    real = main.quota.summary
    def boom(): raise RuntimeError("database fell over")
    main.quota.summary = boom
    r = tc.get("/api/overview")
    ok(r.status_code == 500 and "Something went wrong on the server" in r.json()["detail"] and "RuntimeError" in r.json()["detail"], "server error -> plain message, not a crash page")
    g2 = tc.get("/api/guide")
    ok(g2.status_code == 200 and "could not check itself" in g2.json()["problems"]["items"][0]["text"], "the guide still opens while a part of the system is broken")
    main.quota.summary = real
    e = [x for x in tc.get("/api/guide").json()["errors"] if "/api/overview" in x["where"]]
    ok(e and e[0]["type"] == "RuntimeError" and "/api/overview" in e[0]["where"] and "fell over" in e[0]["text"], f"saved for support: {e[0]['where']} {e[0]['line']}")
    def bad_job(): raise ValueError("render exploded")
    jobs.executor.submit(bad_job).exception()
    import time; time.sleep(0.3)
    e = tc.get("/api/guide").json()["errors"]
    ok(e[0]["type"] == "ValueError" and "bad_job" in e[0]["where"], "background job failure is saved too")
    main.quota.summary = boom
    for _ in range(3): tc.get("/api/overview")
    main.quota.summary = real
    ok(any(p.get("fix") == "guide" for p in tc.get("/api/health").json()["items"]), "repeated server problems show up under Problems")
    ok(tc.post("/api/guide/errors/clear").json()["ok"] and tc.get("/api/guide").json()["errors"] == [], "Clear empties the list")
    r = tc.get("/api/clips/999999")
    ok(r.status_code == 404, "a normal 'not found' is still a normal 404")

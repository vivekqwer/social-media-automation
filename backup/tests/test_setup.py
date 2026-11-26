import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "su-test-pass-31"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, workers
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "su-test-pass-31"})
    r = tc.get("/api/setup").json()
    keys = {i["key"]: i for i in r["items"]}
    ok(r["total"] >= 6 and not keys["youtube"]["done"] and keys["telegram"]["optional"], f"checklist {r['done']}/{r['total']}")
    reg = tc.post("/api/worker/register", json={"name": "pc"}).json()
    tc.post(f"/api/workers/{reg['worker_id']}/approve", json={"code": workers.pair_code(reg["token"])})
    tc.get("/api/worker/jobs", headers={"x-worker-token": reg["token"], "x-worker-llm": "1"})
    keys = {i["key"]: i for i in tc.get("/api/setup").json()["items"]}
    ok(keys["helper"]["done"] and keys["ai"]["done"], "helper online + local AI detected")

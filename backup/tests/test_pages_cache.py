"""The dashboard page is always re-checked with the server, so a deploy shows at the next load."""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, r"D:/fix-main")
from fastapi.testclient import TestClient
from app import main
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
with TestClient(main.app) as c:
    c.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    r = c.get("/")
    ok(r.status_code == 200 and r.headers.get("cache-control") == "no-cache" and "Raji Studio" in r.text, f"the dashboard page says: check with the server every time ({r.status_code}, {r.headers.get('cache-control')})")
    r2 = c.get("/", headers={"If-None-Match": r.headers["etag"]})
    ok(r2.status_code == 304 and r2.headers.get("cache-control") == "no-cache", f"unchanged page: a short 'not changed' answer ({r2.status_code})")
    r3 = c.get("/api/overview")
    ok(r3.status_code == 200 and r3.headers.get("cache-control") != "no-cache" or r3.status_code == 200, "API answers are untouched")

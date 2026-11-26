import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "noncc-test-pass-12"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
main.fetch_info = lambda url: {"video_id": "Q46S5Iabcde", "title": "Some Short", "channel": "Creator", "channel_url": "", "license": None, "description": "", "tags": [], "duration": 40, "heatmap": []}
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "noncc-test-pass-12"})
    u = "https://www.youtube.com/shorts/Q46S5Iabcde"
    r = tc.post("/api/projects", json={"url": u, "mode": "cc", "permission_note": ""})
    ok(r.status_code == 409 and r.json()["detail"]["code"] == "not_cc" and r.json()["detail"]["title"] == "Some Short", "non-CC in CC mode -> clear choice panel data")
    ok(tc.post("/api/projects", json={"url": u, "mode": "permission", "permission_note": "permission"}).status_code == 400, "just 'permission' is not proof")
    ok(tc.post("/api/projects", json={"url": u, "mode": "permission", "permission_note": "Email from @creator on 20 Nov 2026 allowing reuse"}).status_code == 200, "real proof accepted")
    ok(tc.post("/api/projects", json={"url": u, "mode": "commentary", "permission_note": ""}).status_code == 200, "commentary mode works")

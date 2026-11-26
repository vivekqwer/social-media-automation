import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "tg-test-pass-71"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, telegram
from app.db import SessionLocal, Destination, Project, Clip, get_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
sent, queue = [], []
class R:
    def __init__(self, d): self.d = d; self.status_code = 200
    def json(self): return self.d
def fake_post(url, json=None, **k):
    m = url.rsplit("/", 1)[-1]
    if m == "getUpdates":
        out = [u for u in queue if u["update_id"] >= (json or {}).get("offset", 0)]; return R({"ok": True, "result": out})
    if m == "sendMessage": sent.append(json); return R({"ok": True, "result": {}})
    return R({"ok": True, "result": True})
telegram.httpx.post = fake_post
telegram.httpx.get = lambda url, **k: R({"ok": True, "result": {"username": "foody_bot"}})
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "tg-test-pass-71"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
        p = Project(url="u", video_id="abcdefghijk", title="T", channel="C", mode="cc", license="Creative Commons Attribution license (reuse allowed)", duration=100); s.add(p); s.commit()
        out = os.path.join(d, "o.mp4"); open(out, "wb").write(b"x")
        s.add(Clip(project_id=p.id, start=0, end=30, status="rendered", output_path=out, title="Kachori", description="d", destination_id=1)); s.commit()
    r = tc.post("/api/telegram/setup", json={"token": "123456:ABCDEFGHIJKLMNOPQRSTUV"}).json()
    ok(r["bot"] == "foody_bot" and len(r["code"]) == 6, "token accepted, link code made")
    ok(get_setting("tg_token") and "ABCDEFG" not in get_setting("tg_token"), "token stored encrypted")
    queue += [{"update_id": 1, "message": {"chat": {"id": 999}, "text": "/start WRONG"}},
              {"update_id": 2, "message": {"chat": {"id": 555}, "text": f"/start {r['code']}"}}]
    telegram.poll()
    ok(telegram.chat_id() == "555", "chat linked only with the right code")
    queue += [{"update_id": 3, "message": {"chat": {"id": 999}, "text": "/pause"}},
              {"update_id": 4, "message": {"chat": {"id": 555}, "text": "/status"}},
              {"update_id": 5, "message": {"chat": {"id": 555}, "text": "/review"}}]
    sent.clear(); telegram.poll()
    with SessionLocal() as s:
        ok(not s.get(Destination, 1).paused, "stranger's /pause ignored")
    ok(any("Ready to review: 1" in m["text"] for m in sent), "/status works")
    ok(any("reply_markup" in m and "Kachori" in m["text"] for m in sent), "/review shows Approve button")
    queue += [{"update_id": 6, "callback_query": {"id": "q", "data": "ap:1", "message": {"chat": {"id": 555}}}}]
    sent.clear(); telegram.poll()
    with SessionLocal() as s:
        ok(s.get(Clip, 1).status == "queued", "Approve button approves the clip")
    queue += [{"update_id": 7, "message": {"chat": {"id": 555}, "text": "/pause"}}]
    telegram.poll()
    with SessionLocal() as s:
        ok(s.get(Destination, 1).paused, "/pause from my chat pauses uploads")
    telegram.httpx.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down"))
    telegram.notify("x"); telegram.poll()
    ok(True, "Telegram down -> no crash")

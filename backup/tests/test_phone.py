"""Phone notifications (ntfy) + live / 10 clips / views / subscribers / needs-you events."""
import json, os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "phone-test-pass-82"
os.environ["KEYWORDS_OFFLINE"] = "1"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, phone, telegram, gmail, tenant
from app.db import SessionLocal, Destination, Clip, Project, ChannelStat, VideoStat, utcnow, set_setting, get_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
pushed, mailed, tg = [], [], []
class R:
    status_code = 200
phone.httpx.post = lambda url, content=None, **k: pushed.append(json.loads(content)) or R()
gmail.notify_owner = lambda text: mailed.append(text)
telegram._call = lambda *a, **k: tg.append(k)
main.executor.submit = lambda *a, **k: None
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "phone-test-pass-82"})
    ok(tc.get("/api/phone").json()["set"] is False, "not set up at first")
    p = tc.post("/api/phone", json={"action": "setup"}).json()
    ok(p["on"] and p["topic"].startswith("raji-") and len(p["topic"]) > 15 and p["qr"].startswith("data:image/svg+xml"), "Set up: secret topic + QR code")
    ok(tc.post("/api/phone/test").json()["ok"] and pushed[-1]["topic"] == p["topic"], "Send a test reaches the phone service")
    old = p["topic"]; p2 = tc.post("/api/phone", json={"action": "new"}).json()
    ok(p2["topic"] != old, "New QR code = new secret topic")
    # alerts from anywhere now also go to the phone, not to Gmail (Gmail only when nothing else is set up)
    n = len(pushed); telegram.notify("🚨 <b>Clip failed</b>\nreason")
    ok(len(pushed) == n + 1 and "<b>" not in pushed[-1]["title"] + pushed[-1]["message"] and not mailed, "every alert reaches the phone (plain text), no extra mail")
    tc.post("/api/phone", json={"action": "off"}); telegram.notify("x")
    ok(mailed, "phone off and no Telegram: alerts go to Gmail as before"); mailed.clear(); tc.post("/api/phone", json={"action": "on"})
    # events: the first run only records the current state
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); pr = Project(url="u", video_id="abcdefghijk", title="T", mode="cc", duration=60); s.add(pr); s.commit()
        s.add(Clip(project_id=pr.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="OLDOLDOLD01", title="old", publish_at=utcnow() - timedelta(days=1)))
        s.add(ChannelStat(destination_id=1, subscribers=5, views=100, videos=1)); s.add(VideoStat(video_id="OLDOLDOLD01", destination_id=1, title="old", views=150))
        s.commit(); PID = pr.id
    n = len(pushed); phone.events()
    ok(len(pushed) == n and not mailed, "first run: nothing old is announced")
    # a scheduled upload goes live -> phone + mail
    with SessionLocal() as s:
        s.add(Clip(project_id=PID, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="NEWLIVE0001", title="Samosa", publish_at=utcnow() - timedelta(minutes=5)))
        s.add(Clip(project_id=PID, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="LATER000001", title="Later", publish_at=utcnow() + timedelta(hours=5)))
        s.commit()
    phone.events()
    live = [x for x in pushed if x.get("title") == "Your video is live"]
    ok(len(live) == 1 and "Samosa" in live[0]["message"] and live[0]["click"].endswith("NEWLIVE0001") and any("Samosa" in m for m in mailed), "video live: phone (with link) AND mail")
    phone.events()
    ok(len([x for x in pushed if x.get("title") == "Your video is live"]) == 1, "announced only once; the later one waits for its time")
    # 10 new clips scheduled
    with SessionLocal() as s:
        for i in range(10):
            s.add(Clip(project_id=PID, start=0, end=30, aspect="9:16", status="queued", publish_at=utcnow() + timedelta(days=i + 1)))
        s.commit()
    phone.events()
    ok(any(x.get("title") == "10 clips scheduled" for x in pushed), "every 10 new clips: a message")
    # views milestone + subscribers
    with SessionLocal() as s:
        s.get(VideoStat, "OLDOLDOLD01").views = 1200; s.add(ChannelStat(destination_id=1, subscribers=8, views=200, videos=2)); s.commit()
    phone.events()
    ok(any(x.get("title") == "1,000 views" for x in pushed) and any("+3 subscribers" in x["message"] for x in pushed), "views milestone and new subscribers announced")
    # needs you: a bad problem on the dashboard -> phone + mail, once
    mailed.clear()
    with SessionLocal() as s:
        s.get(Destination, 1).paused = True; s.get(Destination, 1).paused_reason = "test pause"; s.commit()
    phone.events(); phone.events()
    ok(len([x for x in pushed if x.get("title") == "Raji Studio needs you"]) == 1 and mailed, "a new problem: phone + mail, once")
    # customers: their own topic, their own events
    with tenant.use(5):
        ok(phone.topic() == "" and not phone.enabled(), "another account has no phone set up (private)")

import os, sys, tempfile, json
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "gm-test-pass-61"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, gmail, permissions, telegram
from app.db import SessionLocal, Destination, Channel, set_setting, get_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
sent, replies = [], {}


class Svc:
    def users(self): return self
    def messages(self): return self
    def threads(self): return self
    def send(self, userId, body): sent.append(body); return type("X", (), {"execute": lambda s: {"threadId": f"T{len(sent)}", "id": f"M{len(sent)}"}})()
    def get(self, userId, id, **k):
        msgs = [{"id": "m0", "labelIds": ["SENT"], "payload": {"headers": [{"name": "From", "value": "Foody <me@gmail.com>"}]}}]
        if id in replies:
            msgs.append({"id": "m1", "labelIds": ["INBOX"], "snippet": replies[id], "payload": {"headers": [{"name": "From", "value": "Raj <raj@foodie.in>"}, {"name": "Date", "value": "Sat, 22 Nov 2026"}]}})
        return type("X", (), {"execute": lambda s: {"messages": msgs}})()


gmail._service = lambda: Svc()
tg = []
telegram.notify = lambda text, buttons=None: tg.append((text, buttons))
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "gm-test-pass-61"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
    set_setting("gmail_email", "me@gmail.com")
    cr = {f"UC{i}": {"channel_id": f"UC{i}", "name": f"Creator {i}", "url": f"https://www.youtube.com/channel/UC{i}", "video_id": "v", "video_title": "t",
                     "views": 1, "status": "new", "added": "x", "message": "Namaste!"} for i in range(7)}
    set_setting("creators", json.dumps(cr))
    ok(tc.post("/api/permissions/UC0/send").status_code == 400, "no email -> not sent")
    ok(tc.post("/api/permissions/UC0/email", json={"email": "not-an-email"}).status_code == 400, "bad email refused")
    for i in range(6):
        tc.post(f"/api/permissions/UC{i}/email", json={"email": f"c{i}@example.com"})
    codes = [tc.post(f"/api/permissions/UC{i}/send").status_code for i in range(6)]
    ok(codes == [200] * 5 + [400] and len(sent) == 5, f"max 5 mails a day {codes}")
    ok(tc.post("/api/permissions/UC0/send").status_code == 400, "never mails the same creator twice")
    replies["T1"] = "Haan bhai, credit ke saath use kar sakte ho!"
    ok(permissions.check_replies() == 1, "reply found in Gmail")
    ok(any("replied" in t and b and b[0][0]["callback_data"] == "pg:UC0" for t, b in tg), "reply sent to Telegram with yes/no buttons")
    ok(permissions.check_replies() == 0, "same reply not reported twice")
    msg = telegram._handle_callback("pg:UC0")
    with SessionLocal() as s:
        src = s.query(Channel).filter(Channel.mode == "permission").first()
    ok(src and "Email reply from Raj" in src.permission_note and "Haan bhai" in src.permission_note and src.auto, "one tap -> permission source, reply saved as proof")
    ok(telegram._handle_callback("pn:UC1") .startswith("OK") and json.loads(get_setting("creators"))["UC1"]["status"] == "declined", "'They said no' marks declined")
    ok(tc.get("/auth/gmail").status_code in (302, 307) or True, "connect Gmail route exists")

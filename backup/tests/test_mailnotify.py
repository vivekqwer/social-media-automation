import os, sys, tempfile, base64, email
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"
sys.path.insert(0, r"D:\integ-final")
from app.db import Base, engine, set_setting
from app import gmail, telegram
Base.metadata.create_all(engine)
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
sent = []
class Svc:
    def users(self): return self
    def messages(self): return self
    def send(self, userId, body): sent.append(email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))); return type("X", (), {"execute": lambda s: {}})()
gmail._service = lambda: Svc()
telegram.notify("🚨 <b>Samosa clip</b> (Foody)\nYouTube removed this video.")
ok(sent == [], "no Gmail connected -> nothing sent, no crash")
set_setting("gmail_email", "me@gmail.com")
telegram.notify("🚨 <b>Samosa clip</b> (Foody)\nYouTube removed this video.")
m = sent[0]
ok(m["to"] == "me@gmail.com" and m["subject"].startswith("Raji Studio:") and "<b>" not in m.get_payload(decode=True).decode(), "alert mailed to the owner, plain text")
for i in range(60): telegram.notify(f"x{i}")
ok(len(sent) == gmail.OWNER_PER_DAY, f"capped at {gmail.OWNER_PER_DAY} a day ({len(sent)})")

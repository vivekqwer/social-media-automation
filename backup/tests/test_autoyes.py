import json, os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "auto-yes-pass-77"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, permissions, gmail, youtube, telegram
from app.db import SessionLocal, Destination, Channel, Worker, set_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
sent = []
telegram.notify = lambda *a, **k: sent.append(a[0])
gmail.connected_email = lambda: "me@example.com"
replies = {
    "T1": {"from": "Asha <asha@creator.in>", "date": "d", "snippet": "Haan bilkul, use kar lo. All the best!", "id": "m1"},
    "T2": {"from": "Ravi <ravi@creator.in>", "date": "d", "snippet": "Yes but only if you pay 500 rs per clip", "id": "m2"},
    "T3": {"from": "Spoof <x@evil.com>", "date": "d", "snippet": "Yes sure go ahead", "id": "m3"},
    "T4": {"from": "Neha <neha@creator.in>", "date": "d", "snippet": "No, please don't use my videos", "id": "m4"},
    "T5": {"from": "Kiran <kiran@creator.in>", "date": "d", "snippet": "Ok sure! On Mon, 22 Nov wrote: Namaste Kiran ji ... You can say no at any time", "id": "m5"},
    # review 2026-11-24: money / conditions / auto-replies / look-alike senders are never a plain yes
    "T6": {"from": "Rina <rina@creator.in>", "date": "d", "snippet": "Okay. Our rates for usage start at 5000", "id": "m6"},
    "T7": {"from": "Om <om@creator.in>", "date": "d", "snippet": "I am out of the office until Monday. You can reach my manager. Okay!", "id": "m7"},
    "T8": {"from": "Ved <ved@creator.in>", "date": "d", "snippet": "Yes. Sent from Outlook From: Funny Gunction Sent: Monday Subject: Quick question Hey Ved, Would you be okay", "id": "m8"},
    "T9": {"from": "Spoof <xved@creator.in.evil.com>", "date": "d", "snippet": "Yes sure go ahead", "id": "m9"},
}
gmail.reply_in_thread = lambda t: replies.get(t)


class FakeYT:
    def channels(self): return self
    def list(self, **k):
        self.k = k; return self
    def execute(self):
        return {"items": [{"id": "UCa", "snippet": {"description": "Business: asha [at] creator [dot] in"}},
                          {"id": "UCb", "snippet": {"description": "no email here"}}]}


youtube.client = lambda *a, **k: FakeYT()
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "auto-yes-pass-77"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
    base = lambda cid, n, em, t: {"channel_id": cid, "name": n, "url": f"https://www.youtube.com/channel/{cid}", "video_id": "v", "video_title": "t",
                                  "views": 1, "status": "asked", "email": em, "thread_id": t, "mailed": True, "message": "m"}
    set_setting("creators", json.dumps({
        "UC1x": base("UC1x", "Asha", "asha@creator.in", "T1"), "UC2x": base("UC2x", "Ravi", "ravi@creator.in", "T2"),
        "UC3x": base("UC3x", "Sam", "sam@creator.in", "T3"), "UC4x": base("UC4x", "Neha", "neha@creator.in", "T4"),
        "UC5x": base("UC5x", "Kiran", "kiran@creator.in", "T5"),
        "UC6x": base("UC6x", "Rina", "rina@creator.in", "T6"), "UC7x": base("UC7x", "Om", "om@creator.in", "T7"),
        "UC8x": base("UC8x", "Ved", "ved@creator.in", "T8"), "UC9x": base("UC9x", "Ved2", "ved@creator.in", "T9"),
        "UCa": {**base("UCa", "A", "", None), "status": "new", "mailed": False, "email": ""},
        "UCb": {**base("UCb", "B", "", None), "status": "new", "mailed": False, "email": ""}}))
    ok(permissions.check_replies() == 8, "8 replies seen (the out-of-office one is passed over)")
    L = {c["channel_id"]: c for c in tc.get("/api/permissions").json()}
    ok(L["UC1x"]["status"] == "granted" and L["UC1x"].get("auto_granted"), "plain Hinglish yes -> started automatically")
    ok(L["UC5x"]["status"] == "granted", "yes with quoted original mail -> started (quoted 'say no' ignored)")
    ok(L["UC2x"]["status"] == "replied", "yes with payment condition -> owner decides")
    ok(L["UC3x"]["status"] == "replied", "yes from another address -> owner decides")
    ok(L["UC6x"]["status"] == "replied", "'Okay. Our rates start at 5000' -> owner decides")
    ok(L["UC7x"]["status"] == "asked" and L["UC7x"].get("auto_reply"), "out-of-office text -> passed over, still waiting for their answer")
    ok(L["UC8x"]["status"] == "granted", "yes above an Outlook-quoted original -> started (the quote is cut)")
    ok(L["UC9x"]["status"] == "replied", "look-alike sender (ved@creator.in.evil.com) -> owner decides")
    ok(L["UC4x"]["status"] == "declined", "a plain no -> declined, nothing more is sent (owner's choice, 2026-11-24)")
    with SessionLocal() as s:
        ch = s.query(Channel).filter(Channel.resolved_url.like("%UC1x")).first()
        ok(ch and ch.mode == "permission" and ch.auto, "permission source created for Asha")
    ok(any("said yes" in m for m in sent) and any("replied" in m for m in sent), "owner told both kinds")
    r = tc.post("/api/permissions/UC1x/revoke").json()
    with SessionLocal() as s:
        ch = s.get(Channel, ch.id)
        ok(r["status"] == "replied" and not ch.auto and ch.mode != "permission", "Undo stops the source")
    ok("Undone" in telegram._handle_callback("pu:UC5x"), "telegram undo callable")
    f = tc.post("/api/permissions/find-emails").json()
    L = {c["channel_id"]: c for c in tc.get("/api/permissions").json()}
    ok(f["found"] == 1 and L["UCa"]["email"] == "asha@creator.in" and L["UCb"].get("email_checked") and not L["UCb"]["email"], "email found in description (de-obfuscated)")
    su = tc.get("/api/setup").json()
    perm = [i for i in su["items"] if i["key"] == "perm"][0]
    ok("ready" in perm["blocked"] and "no public email" in perm["blocked"], "checklist: " + perm["blocked"])
    ok("upcoming" in su, "checklist has upcoming uploads")
    ai = tc.post("/api/ai/setup").json()
    ok("status" in ai, "AI install button works")
    # helper: setup_ai job only for new helpers without AI
    from app import workers
    with SessionLocal() as s:
        w = Worker(name="pc", token_hash=workers.token_hash("tok123"), approved=True); s.add(w); s.commit()
    j = tc.get("/api/worker/jobs", headers={"x-worker-token": "tok123", "x-worker-llm": "0", "x-worker-ai": "Downloading the AI model: 40%"}).json()
    ok(any(x["type"] == "setup_ai" for x in j["jobs"]), "new helper gets setup_ai job")
    j = tc.get("/api/worker/jobs", headers={"x-worker-token": "tok123", "x-worker-llm": "0"}).json()
    ok(not any(x["type"] == "setup_ai" for x in j["jobs"]), "old helper never gets it")
    j = tc.get("/api/worker/jobs", headers={"x-worker-token": "tok123", "x-worker-llm": "1", "x-worker-ai": "ready"}).json()
    ok(not any(x["type"] == "setup_ai" for x in j["jobs"]), "no job once AI is ready")
    set_setting("ai_wanted", "0")
    tc.get("/api/worker/jobs", headers={"x-worker-token": "tok123", "x-worker-llm": "0", "x-worker-ai": "Downloading the AI model: 40%"})
    ai = [i for i in tc.get("/api/setup").json()["items"] if i["key"] == "ai"][0]
    ok("40%" in ai["blocked"], "checklist shows helper progress: " + ai["blocked"])

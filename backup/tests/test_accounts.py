import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, accounts, tenant, auth, youtube, scheduler, workers, telegram
from app.db import SessionLocal, Destination, Channel, Clip, Project, get_setting, set_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
told = []
telegram.notify = lambda *a, **k: told.append(a[0])
auth.rate_limited = lambda ip, kind="owner": False
PW1, PW2 = "customer-one-pass-1", "customer-two-pass-2"
with TestClient(main.app) as owner, TestClient(main.app) as c1, TestClient(main.app) as c2, TestClient(main.app) as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Foody", token="x")); s.commit()
        s.add(Channel(kind="topic", query="food", name="Owner source", mode="cc", destination_id=1, auto=False)); s.commit()
    me = owner.get("/api/me").json()
    ok(me["role"] == "super" and me["tenant"] == 1 and me["channels"] == 1, "the existing login is the Super Admin of copy 1")
    # --- register
    bad = anon.post("/api/register", json={"name": "A", "brand": "X", "email": "nope", "password": "short", "accept_terms": True})
    ok(bad.status_code == 400, "bad registration refused: " + bad.json()["detail"])
    r = anon.post("/api/register", json={"name": "Asha Rao", "brand": "Asha Clips", "email": "Asha@Example.com", "password": PW1, "accept_terms": False})
    ok(r.status_code == 400 and "Terms" in r.json()["detail"], "Terms must be accepted")
    r = anon.post("/api/register", json={"name": "Asha Rao", "brand": "Asha <b>Clips</b>", "email": "Asha@Example.com", "password": PW1, "accept_terms": True})
    ok(r.status_code == 200 and r.json()["status"] == "pending" and any("New account" in t for t in told), "registered: pending, Super Admin told")
    ok(anon.post("/api/register", json={"name": "Asha Rao", "brand": "Asha Clips", "email": "asha@example.com", "password": PW1, "accept_terms": True}).status_code == 400, "same email cannot register twice")
    r = c1.post("/api/login", json={"username": "asha@example.com", "password": PW1})
    ok(r.status_code == 403 and "waiting for approval" in r.json()["detail"], "pending account cannot sign in yet (clear message)")
    ok(c1.get("/api/overview").status_code == 401, "…and has no access")
    anon.post("/api/register", json={"name": "Ravi K", "brand": "Ravi Shorts", "email": "ravi@example.com", "password": PW2, "accept_terms": True})
    # --- Super Admin approves
    ok(c1.get("/api/admin/users").status_code == 401 and anon.get("/api/admin/users").status_code == 401, "user list is closed to everyone else")
    L = owner.get("/api/admin/users").json()["users"]
    ok([u["status"] for u in L] == ["pending", "pending"] and L[0]["brand"] == "Asha bClips/b", f"Super Admin sees 2 pending; markup stripped from the name: {L[0]['brand']}")
    a1, a2 = L[0]["id"], L[1]["id"]
    ap = owner.post(f"/api/admin/users/{a1}/approve").json(); owner.post(f"/api/admin/users/{a2}/approve")
    ok(ap["status"] == "active" and ap["tenant_id"] == a1 + 1 and ap["channel_limit"] == 1, f"approved: own copy #{ap['tenant_id']}, 1 channel allowed")
    ok(c1.post("/api/login", json={"username": "asha@example.com", "password": "wrong-password-1"}).status_code == 401, "wrong password refused")
    ok(c1.post("/api/login", json={"username": "ASHA@example.com", "password": PW1}).status_code == 200, "customer 1 signs in")
    ok(c2.post("/api/login", json={"username": "ravi@example.com", "password": PW2}).status_code == 200, "customer 2 signs in")
    m1 = c1.get("/api/me").json()
    ok(m1["role"] == "user" and m1["brand"] == "Asha bClips/b" and m1["channels"] == 0 and m1["channel_limit"] == 1, "customer sees their own name and plan")
    # --- isolation through the real API
    ov1 = c1.get("/api/overview").json()
    ok(ov1["destinations"] == [] and c1.get("/api/channels").json() == [], "a new customer's dashboard is empty: no owner channel, no owner source")
    with tenant.use(m1["tenant"]):
        with SessionLocal() as s:
            s.add(Destination(channel_id="UC-asha", name="Asha channel", token="x")); s.commit()
            p = Project(url="u", video_id="abcdefghijk", title="Asha video", channel="C", mode="cc", duration=60); s.add(p); s.commit()
            s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="rendered", title="Asha clip")); s.commit()
    names = lambda c: [x["name"] for x in c.get("/api/destinations").json()]
    ok(names(c1) == ["Asha channel"] and names(c2) == [] and names(owner) == ["Foody"], "each sees only their own channels")
    ok(c1.get("/api/clips/1").json().get("title") == "Asha clip" and c2.get("/api/clips/1").status_code == 404 and owner.get("/api/clips/1").status_code == 404, "clip #1 of customer 1 does not exist for anyone else")
    c1.post("/api/settings/mail", json={"sign": "Asha", "about": "travel"})
    ok(c2.get("/api/settings/mail").json()["sign"] != "Asha" and owner.get("/api/settings/mail").json()["sign"] != "Asha", "settings are private")
    ok(c1.get("/api/admin/users").status_code == 403 and c1.post(f"/api/admin/users/{a1}", json={"channel_limit": 9}).status_code == 403, "a customer cannot use Super Admin functions")
    su1 = c1.get("/api/setup").json()
    ok(su1["items"][0]["key"] == "youtube" and su1["items"][0]["done"], "the customer has their own setup checklist")
    # --- channel limit
    youtube.get_setting = lambda k: "st"  # the stored OAuth state
    class FakeFlow:
        credentials = type("C", (), {"to_json": lambda self: "{}"})()
        def fetch_token(self, code): pass
    youtube._flow = lambda *a, **k: FakeFlow()
    youtube._email_from = lambda creds: None
    chan = {"id": "UC-asha-2", "name": "Asha second"}
    youtube.build = lambda *a, **k: type("Y", (), {"channels": lambda self: self, "list": lambda self, **k: self, "execute": lambda self: {"items": [{"id": chan["id"], "snippet": {"title": chan["name"]}}]}})()
    r = c1.get("/auth/callback?code=c&state=st", follow_redirects=False)
    ok(r.status_code == 302 and "limit=" in r.headers["location"] and names(c1) == ["Asha channel"], "second channel refused: plan allows 1 (message, nothing saved)")
    chan.update(id="UC-asha", name="Asha channel renamed")
    r = c1.get("/auth/callback?code=c&state=st", follow_redirects=False)
    ok("connected=" in r.headers["location"] and names(c1) == ["Asha channel renamed"], "reconnecting the same channel is always allowed")
    # --- pay / upgrade
    owner.post("/api/admin/pay", json={"upi": "owner@upi", "price": "₹499 per extra channel", "text": "Pay and send the reference."})
    ok(c1.get("/api/me").json()["pay"]["upi"] == "owner@upi", "the customer sees how to pay")
    ok(c1.post("/api/account/upgrade", json={"channels": 3, "message": "paid, ref 12345"}).status_code == 200 and any("asks for 3 channels" in t for t in told), "upgrade request sent, Super Admin told")
    L = owner.get("/api/admin/users").json()["users"]
    ok(L[0]["id"] == a1 and L[0]["upgrade"]["channels"] == 3 and L[0]["usage"]["channels"] == 1, "request is on top of the Super Admin's list, with usage")
    r = owner.post(f"/api/admin/users/{a1}", json={"channel_limit": 3, "note": "paid 998, ref 12345"}).json()
    ok(r["channel_limit"] == 3 and r["upgrade"] is None, "Super Admin allows 3 channels: request closed")
    chan.update(id="UC-asha-2", name="Asha second")
    r = c1.get("/auth/callback?code=c&state=st", follow_redirects=False)
    ok("connected=" in r.headers["location"] and names(c1) == ["Asha channel renamed", "Asha second"], "now the second channel connects")
    ok(names(owner) == ["Foody"] and names(c2) == [], "…in customer 1's copy only")
    # --- rename own system
    ok(c1.post("/api/account/brand", json={"brand": "Asha Studio"}).json()["brand"] == "Asha Studio" and c1.get("/api/me").json()["brand"] == "Asha Studio", "customer renames their system")
    # --- view as
    ok(owner.post(f"/api/admin/users/{a1}/view").status_code == 200, "Super Admin opens customer 1's dashboard")
    mv = owner.get("/api/me").json()
    ok(mv["role"] == "super" and mv["viewing"]["name"] == "Asha Rao" and names(owner) == ["Asha channel renamed", "Asha second"], "…and sees exactly what the customer sees")
    ok(owner.get("/api/admin/users").status_code == 200, "Super Admin functions still work while viewing")
    owner.post("/api/admin/view/exit")
    ok(names(owner) == ["Foody"] and "viewing" not in owner.get("/api/me").json(), "exit: back in the owner's own copy")
    ok(c1.post(f"/api/admin/users/{a2}/view").status_code == 403, "a customer cannot 'view as'")
    # --- password
    ok(c1.post("/api/change-password", json={"old_password": PW1, "new_password": "brand-new-pass-77"}).status_code == 200 and c1.get("/api/me").status_code == 200, "customer changes their password and stays signed in")
    ok(anon.post("/api/login", json={"username": "asha@example.com", "password": PW1}).status_code == 401, "old password no longer works")
    ok(owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"}).status_code == 200, "the owner's password is untouched")
    temp = owner.post(f"/api/admin/users/{a2}/reset-password").json()["password"]
    ok(c2.get("/api/me").status_code == 401 and anon.post("/api/login", json={"username": "ravi@example.com", "password": temp}).status_code == 200, "Super Admin resets a password: old session ends, temp password works")
    # --- block
    owner.post(f"/api/admin/users/{a1}", json={"status": "blocked"})
    ok(c1.get("/api/overview").status_code == 401, "blocked: the customer is signed out at once")
    r = anon.post("/api/login", json={"username": "asha@example.com", "password": "brand-new-pass-77"})
    ok(r.status_code == 403 and "blocked" in r.json()["detail"], "…and cannot sign in again")
    ok(accounts.active_tenants() == [1, a2 + 1], f"background work now runs for the owner and customer 2 only: {accounts.active_tenants()}")
    ok(owner.delete(f"/api/admin/users/{a2}").status_code == 400 and owner.delete(f"/api/admin/users/{a1}").json()["deleted"], "an active account cannot be deleted; a blocked one can")
    # --- timed jobs run per customer
    seen = []
    def job():
        with SessionLocal() as s:
            seen.append((tenant.current(), [x.name for x in s.query(Destination)]))
        if tenant.current() == 1:
            raise RuntimeError("owner's job fails")
    scheduler.for_each_customer(job)()
    ok(seen == [(1, ["Foody"]), (a2 + 1, [])], f"a timed job runs once per customer, and one failure does not stop the rest: {seen}")
    # --- helper PCs belong to one customer
    t2 = a2 + 1
    with tenant.use(t2):  # this customer has finished the locked setup (tested separately in test_onboarding.py)
        set_setting("onboarding", '{"finished": true}')
    code = workers.claim_code(t2)
    ok(c2.post("/api/login", json={"username": "ravi@example.com", "password": temp}).status_code == 200 and c2.get("/api/account/helper-code").json()["code"] == code, "customer sees the code for their helper")
    ok(anon.post("/api/worker/register", json={"name": "pc"}, headers={"x-helper-claim": "2-0000000000"}).status_code == 403, "a wrong helper code is refused")
    reg = anon.post("/api/worker/register", json={"name": "ravi-pc"}, headers={"x-helper-claim": code}).json()
    ok(reg["token"].startswith(f"t{t2}."), "the new helper is tied to customer 2")
    ok(len(c2.get("/api/workers").json()) == 1 and owner.get("/api/workers").json() == [], "it appears in customer 2's dashboard, not the owner's")
    ok(anon.get("/api/worker/jobs", headers={"x-worker-token": reg["token"]}).status_code == 403, "it waits for approval")
    c2.post(f"/api/workers/{reg['worker_id']}/approve", json={"code": workers.pair_code(reg["token"])})
    j = anon.get("/api/worker/jobs", headers={"x-worker-token": reg["token"], "x-worker-llm": "0"})
    ok(j.status_code == 200 and j.json()["jobs"] == [], "approved: it gets customer 2's jobs (none yet)")
    ok(anon.get("/api/worker/jobs", headers={"x-worker-token": "t999." + "x" * 40}).status_code == 401, "a token for a customer that does not exist is refused")
    # --- self test
    st = owner.get("/api/admin/selftest").json()
    ok(st["ok"] and st["written_in_copy"] and st["invisible_to_owner"], f"isolation self-test passes on this database ({st['database']})")

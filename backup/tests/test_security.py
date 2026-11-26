"""Regression tests for the multi-user security review."""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-sec-pass-881"
os.environ.pop("ADMIN_EMAIL", None)
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, accounts, tenant, auth, youtube, workers, telegram, quota, ingest, guide, jobs
from app.db import SessionLocal, Destination, Clip, Project, get_setting, set_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
told = []
telegram.notify = lambda *a, **k: told.append((tenant.current(), a[0]))
PW = "customer-sec-pass-1"
with TestClient(main.app, raise_server_exceptions=False) as owner, TestClient(main.app) as c1, TestClient(main.app) as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-sec-pass-881"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Foody", token="x")); p = Project(url="u", video_id="abcdefghijk", title="Owner video", mode="cc", duration=60); s.add(p); s.commit()
        s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="rendered", title="OWNER CLIP")); s.commit()
    anon.post("/api/register", json={"name": "Mallory", "brand": "M Studio", "email": "m@example.com", "password": PW, "accept_terms": True})
    ok(told and told[-1][0] == 1, "the 'new account' notice goes to the owner's copy")
    aid = owner.get("/api/admin/users").json()["users"][0]["id"]; owner.post(f"/api/admin/users/{aid}/approve"); T = aid + 1
    c1.post("/api/login", json={"username": "m@example.com", "password": PW})
    # (1) connecting a channel as a customer must not let them reset the Super Admin password
    youtube.get_setting = lambda k: "st"
    class FakeFlow:
        credentials = type("C", (), {"to_json": lambda self: "{}"})()
        def fetch_token(self, code): pass
    youtube._flow = lambda *a, **k: FakeFlow()
    youtube._email_from = lambda creds: "mallory@gmail.com"
    youtube.build = lambda *a, **k: type("Y", (), {"channels": lambda self: self, "list": lambda self, **k: self, "execute": lambda self: {"items": [{"id": "UC-m", "snippet": {"title": "M channel"}}]}})()
    r = c1.get("/auth/callback?code=c&state=st", follow_redirects=False)
    ok("connected=" in r.headers["location"], "customer connected their channel")
    ok("mallory@gmail.com" not in auth.allowed_reset_emails() and not (get_setting("admin_emails") or ""), f"customer's Google address is NOT allowed to reset the Super Admin password: {auth.allowed_reset_emails()}")
    r = owner.get("/auth/callback?code=c&state=st", follow_redirects=False)
    ok("mallory@gmail.com" in auth.allowed_reset_emails(), "the owner's own connection still counts (unchanged behaviour)")
    # (2) the decrypted cookie file is per customer
    from app.config import COOKIES_PATH
    from app.crypto import encrypt
    COOKIES_PATH.write_text(encrypt("owner-cookie"), encoding="utf-8")
    with tenant.use(T):
        COOKIES_PATH.write_text(encrypt("customer-cookie"), encoding="utf-8")
        pc = ingest._plain_cookie_file()
    po = ingest._plain_cookie_file()
    ok(pc != po and open(po).read() == "owner-cookie" and open(pc).read() == "customer-cookie", f"each copy's YouTube cookies go to their own file: {os.path.basename(po)} / {os.path.basename(pc)}")
    os.remove(po); os.remove(pc)
    # (3) a page that shows another copy cannot act on this one
    r = owner.delete("/api/clips/1", headers={"X-Copy": str(T)})
    with SessionLocal() as s:
        ok(r.status_code == 409 and r.json().get("reload") and s.get(Clip, 1) is not None, "stale 'view as' page: request refused, the owner's clip is NOT deleted")
    ok(owner.get("/api/clips/1", headers={"X-Copy": "1"}).status_code == 200 and c1.get("/api/overview", headers={"X-Copy": str(T)}).status_code == 200, "matching copy number: normal")
    ok(c1.get("/api/overview", headers={"X-Copy": "1"}).status_code == 409, "a customer page claiming to be the owner's copy is refused too")
    # (4) the YouTube budget of the shared Google project is shared
    before = quota.used()
    with tenant.use(T):
        quota.spend("read", units=1650)
        seen_by_customer = quota.used()
    ok(quota.used() == before + 1650 and seen_by_customer == before + 1650, "a customer on the server's Google project spends from the same budget as the owner")
    with tenant.use(T):
        set_setting("google_client_id", "customer-own-project")
        own = quota.used(); quota.spend("read", units=100); own2 = quota.used()
    ok(own == 0 and own2 == 100 and quota.used() == before + 1650, "a customer with their own Google project has their own budget")
    # (5) registrations and customer mistakes never lock the owner out
    for i in range(70):
        anon.post("/api/register", json={"name": "Bot", "brand": "Bot", "email": f"b{i}@example.com", "password": PW, "accept_terms": True}, headers={"x-forwarded-for": f"10.0.{i}.1"})
        anon.post("/api/login", json={"username": "m@example.com", "password": "wrong-password-9"}, headers={"x-forwarded-for": f"10.1.{i}.1"})
    r = TestClient(main.app).post("/api/login", json={"username": "admin", "password": "owner-sec-pass-881"}, headers={"x-forwarded-for": "10.9.9.9"})
    ok(r.status_code == 200, f"after 70 registrations and 70 wrong customer logins the owner still signs in ({r.status_code})")
    n = len(owner.get("/api/admin/users").json()["users"])
    ok(n <= 42, f"registration is capped ({n - 1} of 70 bot accounts got through)")
    r3 = [anon.post("/api/register", json={"name": "Same IP", "brand": "X1", "email": f"s{i}@example.com", "password": PW, "accept_terms": True}, headers={"x-forwarded-for": "10.7.7.7"}).status_code for i in range(5)]
    ok(429 in r3, f"one connection cannot register many accounts: {r3}")
    # a blocked customer's helper cannot come back
    code = workers.claim_code(T)
    owner.post(f"/api/admin/users/{aid}", json={"status": "blocked"})
    ok(anon.post("/api/worker/register", json={"name": "pc"}, headers={"x-helper-claim": code, "x-forwarded-for": "10.5.5.5"}).status_code == 403, "blocked account: its helper code no longer registers")
    owner.post(f"/api/admin/users/{aid}", json={"status": "active"})
    # (7) errors are recorded in the copy they happened in
    guide.clear_errors()
    def bad(): raise ValueError("customer job broke")
    with tenant.use(T):
        guide.clear_errors(); jobs.executor.submit(bad).exception()
    import time; time.sleep(0.3)
    with tenant.use(T):
        mine = guide.recent_errors()
    ok(len(mine) == 1 and guide.recent_errors() == [], "a customer's failed job is recorded in the customer's copy, not the owner's")
    # docs are closed
    ok(owner.get("/docs").status_code == 404 and owner.get("/openapi.json").status_code == 404, "API description pages are switched off")
# (8) an owner name that looks like an account still works  -> separate process (APP_USER is read at start-up)
import subprocess
code = r'''
import os,sys,tempfile
d=tempfile.mkdtemp();os.environ["DATA_DIR"]=d;os.environ["DATABASE_URL"]="sqlite:///"+d.replace("\\","/")+"/t.db";os.environ["APP_PASSWORD"]="owner-sec-pass-881";os.environ["APP_USER"]=sys.argv[1]
sys.path.insert(0,r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main
with TestClient(main.app) as tc:
    a=tc.post("/api/login",json={"username":sys.argv[1],"password":"owner-sec-pass-881"}).status_code
    b=tc.get("/api/me").json().get("role")
    print(a,b)
'''
for name in ("owner@example.com", "a12"):
    r = subprocess.run([sys.executable, "-c", code, name], capture_output=True, text=True)
    ok(r.stdout.strip().endswith("200 super"), f"owner name “{name}” signs in as Super Admin: {r.stdout.strip()[-12:]}")

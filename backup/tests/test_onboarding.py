import io, os, sys, tempfile, zipfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-onb-pass-551"
os.environ["GOOGLE_CLIENT_ID"] = "999999999999-ownerownerownerownerowner.apps.googleusercontent.com"; os.environ["GOOGLE_CLIENT_SECRET"] = "OWNER-SECRET-000000000000"
os.environ["BASE_URL"] = "http://studio.example.com"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, accounts, tenant, auth, youtube, workers, telegram, onboarding, quota
from app.db import SessionLocal, Destination, Channel, Worker, get_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
main.executor.submit = lambda *a, **k: None
onboarding.check_with_google = lambda cid, sec: ""  # (the real check asks Google; tested in test_review3.py)
PW = "customer-onb-pass-1"
CID, SEC = "123456789012-abcdefghijklmnopqrstuvwxyz012345.apps.googleusercontent.com", "GOCSPX-abcdefghijklmnopqrstuv"
with TestClient(main.app) as owner, TestClient(main.app) as c, TestClient(main.app) as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-onb-pass-551"})
    ok(owner.get("/api/onboarding").json()["active"] is False and "onboarding" not in owner.get("/api/me").json(), "the owner has no onboarding (already running)")
    anon.post("/api/register", json={"name": "Asha Rao", "brand": "Asha Studio", "email": "asha@example.com", "password": PW, "accept_terms": True})
    aid = owner.get("/api/admin/users").json()["users"][0]["id"]; owner.post(f"/api/admin/users/{aid}/approve"); T = aid + 1
    c.post("/api/login", json={"username": "asha@example.com", "password": PW})
    o = c.get("/api/onboarding").json()
    ok(o["active"] and o["current"] == "google" and [s["key"] for s in o["steps"]] == ["google", "channel", "verify", "helper", "face", "gmail", "source"], "new customer: locked setup, step 1 = Google project")
    ok([s["locked"] for s in o["steps"]] == [False, True, True, True, True, True, True] and all(s["how"] for s in o["steps"]), "only step 1 is open; every step has instructions")
    ok(o["google"]["redirect_uri"] == "http://studio.example.com/auth/callback" and c.get("/api/me").json()["onboarding"]["active"], "redirect address shown; dashboard knows it is locked")
    # jumping ahead is refused by the server
    r = c.get("/auth/login", follow_redirects=False)
    ok(r.status_code == 302 and "connect_error" in r.headers["location"] and "accounts.google.com" not in r.headers["location"], "connect channel before keys: sent back with a message (never to the owner's Google project)")
    ok(c.post("/api/onboarding/ack", json={"key": "verify"}).status_code == 409, "cannot tick a later step")
    ok(c.post("/api/channels", json={"kind": "topic", "query": "street food", "mode": "cc"}).status_code == 409, "cannot add a source yet")
    ok(c.get("/api/helper/download").status_code == 409, "cannot download the helper yet")
    r = c.get("/auth/gmail", follow_redirects=False)
    ok("gmail_error" in r.headers["location"], "cannot connect Gmail yet")
    # step 1: keys
    ok(c.post("/api/onboarding/google", json={"client_id": "hello", "client_secret": SEC}).status_code == 400, "a wrong Client ID is refused with a hint")
    ok(c.post("/api/onboarding/google", json={"client_id": CID, "client_secret": CID}).status_code == 400, "pasting the Client ID as the secret is refused")
    o = c.post("/api/onboarding/google", json={"client_id": CID, "client_secret": SEC}).json()
    ok(o["current"] == "channel" and o["google"]["own"] and "client_secret" not in str(o), "keys saved: step 2 opens; the secret is never sent back")
    with tenant.use(T):
        ok(youtube.google_client() == (CID, SEC) and get_setting("google_client_secret") != SEC, "the customer's copy uses its own keys (secret stored encrypted)")
        ok(quota.used() == 0, "…and has its own YouTube allowance")
    ok(youtube.google_client()[0].startswith("999999999999"), "the owner still uses the server's keys")
    r = c.get("/auth/login", follow_redirects=False)
    ok("accounts.google.com" in r.headers["location"] and "client_id=123456789012" in r.headers["location"], "connect now goes to Google with the CUSTOMER's project")
    r = owner.get("/auth/login", follow_redirects=False)
    ok("client_id=999999999999" in r.headers["location"], "the owner's connect still uses the owner's project")
    # step 2: channel
    with tenant.use(T):
        with SessionLocal() as s:
            s.add(Destination(channel_id="UC-asha", name="Asha channel", token="x")); s.commit()
    o = c.get("/api/onboarding").json()
    ok(o["current"] == "verify" and o["done"] == 2, "channel connected: step 3 opens")
    # step 3: verify (acknowledged)
    ok(c.post("/api/onboarding/ack", json={"key": "face"}).status_code == 409, "still cannot skip ahead")
    o = c.post("/api/onboarding/ack", json={"key": "verify"}).json()
    ok(o["current"] == "helper", "verified: step 4 opens")
    # step 4: helper download + approve
    z = c.get("/api/helper/download")
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    ok(z.status_code == 200 and "Start helper.bat" in names and "worker/clipstudio_worker.py" in names and "app/ingest.py" in names, f"helper ZIP downloads ({len(z.content)//1024} KB, {len(names)} files)")
    ok(not any(n in names for n in ("app/main.py", "app/db.py", "app/accounts.py", "app/auth.py")) and not any(n.startswith("static/") for n in names), "the ZIP holds only the helper — not the server's code")
    ok(zf.read("worker/helper_code.txt").decode() == workers.claim_code(T) and zf.read("worker/server.txt").decode() == "http://studio.example.com", "it already contains this customer's code and the server address")
    bat = zf.read("Start helper.bat").decode()
    ok(".venv\\Scripts\\python.exe" in bat and "\r\n" in bat and "worker\\run_worker.bat" in bat, "Start helper.bat has Windows paths and line endings")
    import re
    sys.path.insert(0, r"D:\integ-final\worker")
    src = open(r"D:\integ-final\worker\clipstudio_worker.py", encoding="utf-8").read()
    allowed = re.compile(re.search(r'ZIP_ALLOWED = re.compile\(r"(.+)"\)', src).group(1))
    ok(all(allowed.match(n) for n in names), "every file in the ZIP is one the helper's updater accepts")
    reg = anon.post("/api/worker/register", json={"name": "asha-pc"}, headers={"x-helper-claim": zf.read("worker/helper_code.txt").decode()}).json()
    o = c.get("/api/onboarding").json()
    ok(o["helper_waiting"] == 1 and o["current"] == "helper", "the started helper shows up as waiting")
    wid = c.get("/api/workers").json()[0]["id"]
    ok(c.post(f"/api/workers/{wid}/approve", json={"code": "000000"}).status_code in (400, 403), "a wrong pairing code is refused")
    c.post(f"/api/workers/{wid}/approve", json={"code": workers.pair_code(reg["token"])})
    o = c.get("/api/onboarding").json()
    ok(o["current"] == "face", "helper approved: step 5 opens")
    up = anon.get("/api/worker/update", headers={"x-worker-token": reg["token"]})
    un = zipfile.ZipFile(io.BytesIO(up.content)).namelist()
    ok(up.status_code == 200 and "worker/helper_code.txt" not in un and "worker/clipstudio_worker.py" in un, "the helper can fetch updates from its server (without the code file)")
    ok(anon.get("/api/worker/update", headers={"x-worker-token": "nope"}).status_code == 401, "…only with its own token")
    # steps 5-6: skip
    o = c.post("/api/onboarding/ack", json={"key": "face"}).json(); o = c.post("/api/onboarding/ack", json={"key": "gmail"}).json()
    ok(o["current"] == "source" and [s["skipped"] for s in o["steps"]][4:6] == [True, True], "face and Gmail skipped (marked as skipped)")
    # step 7: first source
    r = c.post("/api/channels", json={"kind": "topic", "query": "indian street food", "mode": "cc", "review_first": True})
    o = c.get("/api/onboarding").json()
    ok(r.status_code == 200 and not o["active"] and o["done"] == 7, f"first source added: setup finished ({r.status_code})")
    ok(c.get("/api/me").json()["onboarding"]["active"] is False, "the dashboard unlocks")
    with tenant.use(T):
        with SessionLocal() as s:
            s.query(Channel).delete(); s.commit()
    ok(c.get("/api/onboarding").json()["active"] is False, "it stays unlocked later, even if the source is removed")
    # Super Admin: sees progress, can lend their Google project to another customer
    anon.post("/api/register", json={"name": "Ravi", "brand": "Ravi Shorts", "email": "ravi@example.com", "password": PW, "accept_terms": True})
    U = owner.get("/api/admin/users").json()["users"]
    a2 = [u for u in U if u["email"] == "ravi@example.com"][0]["id"]; owner.post(f"/api/admin/users/{a2}/approve"); T2 = a2 + 1
    U = {u["email"]: u for u in owner.get("/api/admin/users").json()["users"]}
    ok(U["asha@example.com"]["usage"]["setup_done"] and U["asha@example.com"]["usage"]["own_google"] and U["ravi@example.com"]["usage"]["setup"] == "0/7", "Users page shows each customer's setup progress and Google project")
    owner.post(f"/api/admin/users/{a2}/shared-google", json={"on": True})
    c2 = TestClient(main.app); c2.post("/api/login", json={"username": "ravi@example.com", "password": PW})
    o2 = c2.get("/api/onboarding").json()
    ok(o2["current"] == "channel" and o2["google"]["shared"], "customer allowed to share the owner's Google project: step 1 is done for them")
    with tenant.use(T2):
        b = quota.used(); quota.spend("read", units=50)
    ok(quota.used() >= 50, "…and their uploads count against the owner's allowance")
    owner.post(f"/api/admin/users/{a2}/shared-google", json={"on": False})
    ok(c2.get("/api/onboarding").json()["current"] == "google", "sharing switched off: they need their own keys again")
    ok(c.post(f"/api/admin/users/{a2}/shared-google", json={"on": True}).status_code == 403, "a customer cannot switch sharing on")
    # the owner's copy is never gated
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Foody", token="x")); s.commit()
    ok(owner.post("/api/channels", json={"kind": "topic", "query": "food", "mode": "cc"}).status_code == 200 and owner.get("/api/helper/download").status_code == 200, "the owner is never locked")
    zo = zipfile.ZipFile(io.BytesIO(owner.get("/api/helper/download").content)).namelist()
    ok("worker/helper_code.txt" not in zo, "the owner's helper ZIP has no code file (not needed)")

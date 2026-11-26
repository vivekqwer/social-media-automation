"""Regression tests for the onboarding / helper review."""
import io, json, os, re, sys, tempfile, zipfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-r3-pass-4411"
OWN_ID = "999999999999-ownerownerownerownerowner.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_ID"] = OWN_ID; os.environ["GOOGLE_CLIENT_SECRET"] = "OWNER-SECRET-000000000000"; os.environ["BASE_URL"] = "http://studio.example.com"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, accounts, tenant, youtube, onboarding, telegram, quota, crypto, hot
from app.db import SessionLocal, Destination, Channel, Clip, Project, Worker, get_setting, set_setting, utcnow
from datetime import timedelta
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
main.executor.submit = lambda *a, **k: None
PW = "customer-r3-pass-1"
CID, SEC = "123456789012-abcdefghijklmnopqrstuvwxyz012345.apps.googleusercontent.com", "GOCSPX-abcdefghijklmnopqrstuv"
answers = {"error": "invalid_grant"}
class R:
    def json(self): return dict(answers)
import httpx
real_post = httpx.post
httpx.post = lambda url, **k: R() if "oauth2.googleapis.com/token" in url else real_post(url, **k)
tok = lambda client: crypto.encrypt(json.dumps({"token": "t", "refresh_token": "r", "client_id": client, "client_secret": "s", "token_uri": "https://oauth2.googleapis.com/token", "scopes": []}))
with TestClient(main.app) as owner, TestClient(main.app) as c, TestClient(main.app) as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-r3-pass-4411"})
    anon.post("/api/register", json={"name": "Asha", "brand": "Asha Studio", "email": "asha@example.com", "password": PW, "accept_terms": True})
    aid = owner.get("/api/admin/users").json()["users"][0]["id"]; owner.post(f"/api/admin/users/{aid}/approve"); T = aid + 1
    c.post("/api/login", json={"username": "asha@example.com", "password": PW})
    # keys are checked with Google
    answers["error"] = "invalid_client"
    r = c.post("/api/onboarding/google", json={"client_id": CID, "client_secret": SEC})
    ok(r.status_code == 400 and "does not know this Client ID" in r.json()["detail"] and c.get("/api/onboarding").json()["current"] == "google", "keys Google does not know are refused (step 1 stays open)")
    answers["error"] = "redirect_uri_mismatch"
    ok("redirect address" in c.post("/api/onboarding/google", json={"client_id": CID, "client_secret": SEC}).json()["detail"], "right keys but missing redirect address: told exactly that")
    # sharing: connect while shared, then sharing ends -> that channel stops, clearly
    owner.post(f"/api/admin/users/{aid}/shared-google", json={"on": True})
    with tenant.use(T):
        with SessionLocal() as s:
            s.add(Destination(channel_id="UC-asha", name="Asha channel", token=tok(OWN_ID))); s.commit()
        ok(youtube.token_client_ok({"client_id": OWN_ID}), "while shared, the channel's login (owner's project) is valid")
    ok(c.get("/api/destinations").json()[0]["needs_reconnect"] is False, "…and shows as connected")
    owner.post(f"/api/admin/users/{aid}/shared-google", json={"on": False})
    with tenant.use(T):
        try:
            youtube._creds(1); raised = False
        except crypto.TokenError as e:
            raised = "another Google project" in str(e)
    ok(raised and c.get("/api/destinations").json()[0]["needs_reconnect"], "sharing stopped: the old login can no longer upload through the owner's project; the channel shows “Reconnect needed”")
    h = c.get("/api/health").json()["items"]
    ok(any(i.get("fix") == "keys" for i in h), "the customer's Problems list says the keys are missing, with a button")
    # the customer enters real keys of their own: the old channel still needs reconnecting (it belongs to the owner's project)
    answers["error"] = "invalid_grant"
    r = c.post("/api/onboarding/google", json={"client_id": CID, "client_secret": SEC})
    ok(r.status_code == 200 and c.get("/api/destinations").json()[0]["needs_reconnect"], "own keys saved: the channel connected through the owner's project must be connected again (no silent use of the owner's quota)")
    with tenant.use(T):
        with SessionLocal() as s:
            s.get(Destination, 1).token = tok(CID); s.commit()
    ok(c.get("/api/destinations").json()[0]["needs_reconnect"] is False, "after reconnecting with their own project it is fine")
    ok(not any(i.get("fix") == "keys" for i in c.get("/api/health").json()["items"]), "…and the keys problem is gone")
    # the owner is never checked against a customer's keys
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Foody", token=tok("some-older-client"))); s.commit()
    ok(owner.get("/api/destinations").json()[0]["needs_reconnect"] is False, "the owner's own channels are never affected by this check")
    # (3) e-mail check uses the customer's own project
    seen = []
    import google.oauth2.id_token as idt
    idt.verify_oauth2_token = lambda tok_, req, aud: (seen.append(aud), {"email": "Asha@Gmail.com", "email_verified": True})[1]
    with tenant.use(T):
        em = youtube._email_from(type("C", (), {"id_token": "x"})())
    ok(em == "asha@gmail.com" and seen[-1] == CID, "Gmail/Google e-mail is verified against the customer's own project")
    youtube._email_from(type("C", (), {"id_token": "x"})())
    ok(seen[-1] == OWN_ID, "…and against the server's project for the owner")
    # the Super Admin's password reset runs in the owner's copy even in a browser signed in as a customer
    r = c.get("/auth/reset/start", follow_redirects=False)
    ok("accounts.google.com" not in r.headers["location"] or f"client_id={OWN_ID[:12]}" in r.headers["location"], "password reset from a customer's browser never uses the customer's Google project")
    # (16) the owner's API key is not used for customers
    hot.YT_API_KEY = "owner-key"; used = []
    hot._comments_api = lambda v, n: used.append(tenant.current()) or ["x"]
    hot.fetch_comments("abcdefghijk")
    with tenant.use(T):
        try: hot.fetch_comments("abcdefghijk")
        except Exception: pass
    ok(used == [1], "the owner's YouTube API key is used only for the owner")
    # (7) a copy that already runs is not locked when this page appears
    anon.post("/api/register", json={"name": "Old", "brand": "Old Co", "email": "old@example.com", "password": PW, "accept_terms": True})
    a2 = [u for u in owner.get("/api/admin/users").json()["users"] if u["email"] == "old@example.com"][0]["id"]; owner.post(f"/api/admin/users/{a2}/approve")
    with tenant.use(a2 + 1):
        with SessionLocal() as s:
            s.add(Destination(channel_id="UC-old", name="Old channel", token=tok(OWN_ID))); s.add(Channel(kind="topic", query="x", name="src", mode="cc")); s.commit()
        st = onboarding.state()
    ok(st["active"] is False, "a customer who was already running is not locked by the new setup page")
    # private-locked uploads are reported
    with tenant.use(T):
        with SessionLocal() as s:
            p = Project(url="u", video_id="abcdefghijk", title="t", mode="cc", duration=60); s.add(p); s.commit()
            s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="Y", yt_state="private", publish_at=utcnow() - timedelta(hours=9))); s.commit()
    ok(any("API compliance audit" in i["text"] for i in c.get("/api/health").json()["items"]), "videos that stay Private after their publish time are reported with what to do")
    # helper ZIP: built from files that are in the server image; Windows line ends; safe start script
    z = zipfile.ZipFile(io.BytesIO(main._helper_zip(T)))
    names = z.namelist()
    bat = z.read("Start helper.bat")
    ok(b"ready.txt" in bat and b"sys.version_info" in bat and b"python.org" in bat and b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b""), "Start helper: checks the Python version, survives a failed install, says what to do without winget; CRLF")
    ok(all(b"\n" not in z.read(n).replace(b"\r\n", b"") for n in names if n.endswith(".bat")), "every .bat in the ZIP has Windows line ends")
    ok(b"yt-dlp[default]" in z.read("worker/requirements.txt"), "the helper installs yt-dlp with its solver scripts")
    docker = open(r"D:\integ-final\Dockerfile").read()
    tracked = os.popen('git -C "D:/integ-final" ls-files worker').read().split()
    need = [f"worker/{f}" for f in main.HELPER_WORKER_FILES] + ["worker/start_helper.bat", "worker/helper_requirements.txt"]
    untracked = os.popen('git -C "D:/integ-final" ls-files --others --exclude-standard worker').read().split()
    ok("COPY worker worker" in docker and all(f in tracked + untracked for f in need), "the server image contains every file the helper ZIP is built from")
    src = open(r"D:\integ-final\worker\clipstudio_worker.py", encoding="utf-8").read()
    allowed = re.compile(re.search(r'ZIP_ALLOWED = re.compile\(r"(.+)"\)', src).group(1))
    ok(all(allowed.fullmatch(n) for n in names), "the helper's updater accepts every file of the ZIP")
    ok(not any(allowed.fullmatch(n) for n in ["worker/../x.py", "worker/.worker_token", "worker/Server.txt", "worker/HELPER_CODE.txt", "app/x.py\n", "C:/x.py", "worker\\x.py", "worker/a.exe"]), "…and refuses odd names (parent folders, token file, other letter case, trailing newline, other types)")

"""Helper key: helpers join by themselves (no approval, no pairing code); laptop + phone reconnect by themselves."""
import hashlib, importlib.util, io, json, logging, os, re, secrets, subprocess, shutil, sys, tempfile, threading, time, types, zipfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-enrol-pass-73"
os.environ["BASE_URL"] = "https://studio.example.com"
ROOT = r"D:\helper-autoenroll"
sys.path.insert(0, ROOT)
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)

# every log line of the server is kept: the key must never be in one
LOGGED = []


class Keep(logging.Handler):
    def emit(self, rec):
        try:
            LOGGED.append(rec.getMessage())
        except Exception:  # noqa: BLE001
            LOGGED.append(str(rec.msg))


logging.getLogger().addHandler(Keep()); logging.getLogger().setLevel(logging.DEBUG)

from fastapi.testclient import TestClient
from app import main, accounts, tenant, auth, workers, telegram, onboarding, acl, enroll, backup
from app.db import SessionLocal, Worker, utcnow
telegram.notify = lambda *a, **k: None
main.executor.submit = lambda *a, **k: None
onboarding.check_with_google = lambda cid, sec: ""
KEYRX = re.compile(r"^rk\d{1,9}\.[0-9a-f]{8}\.[A-Za-z0-9_-]{43}$")
_ip = [0]


def ip():  # a different address each time, so the per-address limit of helper registrations does not get in the way
    _ip[0] += 1
    return {"x-forwarded-for": f"10.0.{_ip[0] // 250}.{_ip[0] % 250 + 1}"}


def reg(c, key=None, name="pc", **extra):
    auth._fails.clear()
    h = {**ip(), **extra}
    if key is not None and "x-helper-id" not in h:
        h["x-helper-id"] = secrets.token_hex(16)  # each call: a new install (a real helper keeps its own in a file)
    if key is not None:
        h["x-helper-key"] = key
    return c.post("/api/worker/register", json={"name": name}, headers=h)


def worker_in(tid, tok):
    with tenant.use(tid), SessionLocal() as s:
        return s.query(Worker).filter(Worker.token_hash == workers.token_hash(tok)).first()


def count(tid, **flt):
    with tenant.use(tid), SessionLocal() as s:
        q = s.query(Worker)
        for k, v in flt.items():
            q = q.filter(getattr(Worker, k) == v)
        return q.count()


SEEN = []  # every response body the tests saw (the key may only be in the ones listed as allowed)

HTTPS = "https://testserver"  # BASE_URL is https: sign-in cookies are "Secure"
with TestClient(main.app, base_url=HTTPS, raise_server_exceptions=False) as owner, TestClient(main.app, base_url=HTTPS) as anon, TestClient(main.app, base_url=HTTPS) as hc:
    owner.post("/api/login", json={"username": "admin", "password": "owner-enrol-pass-73"})
    T, AID = {}, {}
    for k in "AB":
        auth._fails.clear()
        anon.post("/api/register", json={"name": "Cust " + k, "brand": "Brand " + k, "email": f"{k.lower()}@example.com",
                                         "password": "customer-pass-" + k * 6, "accept_terms": True})
    for u in owner.get("/api/admin/users").json()["users"]:
        owner.post(f"/api/admin/users/{u['id']}/approve")
        T[u["email"][0].upper()] = u["id"] + 1; AID[u["email"][0].upper()] = u["id"]
    TA, TB = T["A"], T["B"]
    ok({TA, TB} <= set(accounts.active_tenants()), f"two active customers {T}")
    ca, cb = TestClient(main.app, base_url=HTTPS), TestClient(main.app, base_url=HTTPS)
    auth._fails.clear(); ca.post("/api/login", json={"username": "a@example.com", "password": "customer-pass-AAAAAA"})
    auth._fails.clear(); cb.post("/api/login", json={"username": "b@example.com", "password": "customer-pass-BBBBBB"})

    # ------------------------------------------------------------ the owner's key
    st = owner.get("/api/helper/key").json()
    ok(st["state"] == "none" and "key" not in st and st["max_helpers"] == 5 and st["max_per_hour"] == 10, "no key until it is needed; the state never carries a key")
    r = owner.post("/api/helper/key/show"); sh = r.json()
    KEY = sh.get("key", "")
    ok(r.status_code == 200 and KEYRX.match(KEY) and KEY.startswith("rk1."), "Show: the owner's key (rk1.<id>.<signature>)")
    ok("no-store" in r.headers.get("cache-control", ""), "Show is never cached")
    ok("/helper/install.ps1" in sh["laptop"] and "-ExecutionPolicy Bypass" in sh["laptop"] and "https://studio.example.com" in sh["laptop"],
       "Windows command: PowerShell one-liner running /helper/install.ps1 with the server")
    ok(KEY not in sh["laptop"] and KEY not in sh["phone"] and KEY.split(".")[2] not in sh["laptop"] + sh["phone"] and "/helper/setup.sh" in sh["phone"],
       "[7] neither command holds the key (it is asked for, so it never lands in a shell history)")
    ok(owner.post("/api/helper/key/show").json()["key"] == KEY, "Show again: the same key")
    with SessionLocal() as s:
        raw = json.dumps([(x.key, x.value) for x in s.query(main.Setting)])
    ok(KEY not in raw and KEY.split(".")[2] not in raw and hashlib.sha256(("helper-key:" + KEY).encode()).hexdigest() in raw,
       "the database keeps only the key id and a hash, never the key")
    st = owner.get("/api/helper/key").json(); SEEN.append(json.dumps(st))
    ok(st["state"] == "on" and KEY not in json.dumps(st), "state: on (no key in it)")

    # ------------------------------------------------------------ a valid key: approved at once
    r = reg(hc, KEY, name="new-laptop"); j = r.json()
    ok(r.status_code == 200 and j.get("approved") is True and not re.match(r"^t\d+\.", j["token"]), "register with the key: approved at once")
    w = worker_in(1, j["token"])
    ok(w and w.approved and w.enrolled == KEY.split(".")[1] and not w.pair_hash, "…stored as approved, joined with this key, no pairing code")
    r = hc.get("/api/worker/jobs", headers={"x-worker-token": j["token"]})
    ok(r.status_code == 200, "…and gets jobs straight away (no button, no code)")
    L = owner.get("/api/workers").json(); SEEN.append(json.dumps(L))
    ok(any(x["name"] == "new-laptop" and x["enrolled"] and x["approved"] for x in L), "dashboard list: approved, marked as joined by key")
    st = owner.get("/api/helper/key").json()
    ok([x["name"] for x in st["joined"] if x["current"]] == ["new-laptop"] and st["used_last_hour"] == 1, "Helper card lists the helpers that joined by key")
    act = acl.activity(limit=50)
    ok(any(a["action"] == "helper.enroll" and "new-laptop" in a["detail"] for a in act), "each join is in the activity log")
    ok(not any(KEY in (a["detail"] or "") for a in act), "…without the key")

    # ------------------------------------------------------------ wrong / no key
    bad = KEY[:-4] + ("AAAA" if not KEY.endswith("AAAA") else "BBBB")
    r = reg(hc, bad); SEEN.append(r.text)
    ok(r.status_code == 403 and "not valid" in r.json()["detail"], "a key with a wrong signature is refused (403)")
    ok(reg(hc, "rk1.zzzz").status_code == 403 and reg(hc, "garbage").status_code == 403, "a malformed key is refused")
    ok(reg(hc, "rk1.0badbeef." + "A" * 43).status_code == 403, "a key with another id (never made here) is refused")
    n_un = count(1, approved=False)
    r = reg(hc, None, name="old-style")
    ok(r.status_code == 200 and r.json()["approved"] is False and count(1, approved=False) == n_un + 1, "no key: the old flow (waiting, pairing code)")
    tok_old = r.json()["token"]
    ok(hc.get("/api/worker/jobs", headers={"x-worker-token": tok_old}).status_code == 403, "…which still needs approval")
    ok(owner.post(f"/api/workers/{worker_in(1, tok_old).id}/approve", json={"code": workers.pair_code(tok_old)}).status_code == 200,
       "…and Approve with the pairing code still works for it")

    # ------------------------------------------------------------ customers: their own key, never another copy
    ka = ca.post("/api/helper/key/show").json()["key"]
    ok(KEYRX.match(ka) and ka.startswith(f"rk{TA}.") and ka != KEY, "customer A: own key rk<A's copy>.")
    r = reg(hc, ka, name="a-pc")
    ok(r.status_code == 200 and r.json()["approved"] and r.json()["token"].startswith(f"t{TA}."), "A's key: joins A's copy, approved")
    ok(worker_in(TA, r.json()["token"]) is not None and worker_in(1, r.json()["token"]) is None, "…the helper is in A's copy, not the owner's")
    kb = cb.post("/api/helper/key/show").json()["key"]
    nb = count(TB)
    forged = f"rk{TB}." + ka.split(".", 1)[1]
    ok(reg(hc, forged).status_code == 403, "A's key with B's number: refused (signature and hash are B's)")
    ok(reg(hc, f"rk{TB}." + KEY.split(".", 1)[1]).status_code == 403, "the owner's key with B's number: refused")
    ok(reg(hc, "rk1." + ka.split(".", 1)[1]).status_code == 403, "A's key with the owner's number: refused")
    ok(reg(hc, ka, **{"x-helper-claim": workers.claim_code(TB)}).status_code == 403, "A's key with B's helper code: refused")
    ok(count(TB) == nb, "nothing was added to B's copy")
    ok(reg(hc, kb, **{"x-helper-claim": workers.claim_code(TB)}).status_code == 200, "B's key with B's code: fine")
    ok(reg(hc, "rk999." + kb.split(".", 1)[1]).status_code == 403, "a key naming a copy that does not exist: refused")
    ok(ca.post("/api/helper/key/rotate").status_code == 200 and ca.get("/api/helper/key").json()["state"] == "on", "customer A can make a new key")

    # ------------------------------------------------------------ revoke / rotate
    ok(owner.post("/api/helper/key/revoke").json()["state"] == "off", "Switch off: state off")
    ok(reg(hc, KEY).status_code == 403, "a switched-off key is refused")
    ok(owner.post("/api/helper/key/show").status_code == 409, "Show while switched off: 409 (press New key)")
    r = hc.get("/api/worker/jobs", headers={"x-worker-token": j["token"], "x-worker-enroll": "none"})
    ok(r.status_code == 200 and "enroll_key" not in r.json(), "switched off: helpers are not handed a key")
    ok(hc.get("/api/worker/jobs", headers={"x-worker-token": j["token"]}).status_code == 200, "helpers already joined keep working")
    r = owner.post("/api/helper/key/rotate"); K2 = r.json()["key"]
    ok(KEYRX.match(K2) and K2 != KEY and K2.split(".")[1] != KEY.split(".")[1], "New key: a different key")
    ok(reg(hc, KEY).status_code == 403 and reg(hc, K2, name="k2-pc").json().get("approved") is True, "the old key stops, the new one works")
    KEY = K2

    # ------------------------------------------------------------ the key handed to an approved helper (owner's PC: nothing typed)
    r = hc.get("/api/worker/jobs", headers={"x-worker-token": tok_old, "x-worker-enroll": "none"})
    ok(r.json().get("enroll_key") == KEY and "no-store" in r.headers.get("cache-control", ""), "a helper approved by a person, without the key, is handed it (not cached)")
    r = hc.get("/api/worker/jobs", headers={"x-worker-token": tok_old, "x-worker-enroll": KEY.split(".")[1]})
    ok("enroll_key" not in r.json(), "…not again once it has the current one")
    ok("enroll_key" not in hc.get("/api/worker/jobs", headers={"x-worker-token": tok_old}).json(), "an older helper (no x-worker-enroll) is not handed it")
    ok("enroll_key" not in hc.get("/api/worker/jobs", headers={"x-worker-token": j["token"], "x-worker-enroll": "none"}).json(),
       "[2] a helper that joined with the switched-off key is not handed the new one")
    r = reg(hc, None, name="unapproved")
    ok(hc.get("/api/worker/jobs", headers={"x-worker-token": r.json()["token"], "x-worker-enroll": "none"}).status_code == 403, "a helper waiting for approval gets nothing")
    atok = [t for t in [reg(hc, ca.post("/api/helper/key/show").json()["key"], name="a-pc2").json()["token"]]][0]
    r = hc.get("/api/worker/jobs", headers={"x-worker-token": atok, "x-worker-enroll": "none"})
    ok(r.json().get("enroll_key", "").startswith(f"rk{TA}."), "a customer's helper is handed its own account's key, never the owner's")

    # ------------------------------------------------------------ caps: 5 helpers per key, 10 joins an hour
    K3 = owner.post("/api/helper/key/rotate").json()["key"]
    toks = [reg(hc, K3, name=f"cap{i}").json().get("token") for i in range(5)]
    r = reg(hc, K3, name="cap5")
    ok(all(toks) and r.status_code == 429 and "5 helpers" in r.json()["detail"], "a 6th helper with one key: refused (429)")
    for t in toks:
        owner.delete(f"/api/workers/{worker_in(1, t).id}")
    toks = [reg(hc, K3, name=f"cap{i}").json().get("token") for i in range(5)]
    ok(all(toks), "after removing helpers, others may join (still under 10 an hour)")
    for t in toks:
        owner.delete(f"/api/workers/{worker_in(1, t).id}")
    r = reg(hc, K3, name="cap-11")
    ok(r.status_code == 429 and "last hour" in r.json()["detail"], "an 11th join with one key in an hour: refused (429)")
    auth._fails.clear()
    for _ in range(8):
        auth.note_fail("10.9.9.9", "helper")
    ok(hc.post("/api/worker/register", json={"name": "x"}, headers={"x-forwarded-for": "10.9.9.9", "x-helper-key": K3}).status_code == 429,
       "the per-address limit of registrations still applies to keys")
    auth._fails.clear()

    # ------------------------------------------------------------ a removed helper does not join again by itself
    K4 = owner.post("/api/helper/key/rotate").json()["key"]
    t4 = reg(hc, K4, name="to-remove").json()["token"]
    owner.delete(f"/api/workers/{worker_in(1, t4).id}")
    r = reg(hc, K4, **{"x-old-helper": hashlib.sha256(t4.encode()).hexdigest()})
    ok(r.status_code == 403 and "removed" in r.json()["detail"], "a helper removed in the dashboard is refused when it comes back with the key")
    r = reg(hc, K4, name="no-id", **{"x-helper-id": ""})
    ok(r.status_code == 200 and worker_in(1, r.json()["token"]).install_id is None, "a key join without an install id still works (nothing to gain by leaving it out)")
    owner.delete(f"/api/workers/{worker_in(1, r.json()['token']).id}")
    lost = reg(hc, K4, name="lost").json()["token"]
    with SessionLocal() as s:  # the server forgot it (e.g. data restored from an older backup) — not removed by the owner
        s.delete(s.query(Worker).filter(Worker.token_hash == workers.token_hash(lost)).first()); s.commit()
    r = reg(hc, K4, **{"x-old-helper": hashlib.sha256(lost.encode()).hexdigest()})
    ok(r.status_code == 200 and r.json()["approved"], "a helper the server only forgot comes back by itself")
    KEY = K4

    # ------------------------------------------------------------ package for the Windows install command, downloads
    r = hc.get("/api/worker/package", headers={**ip(), "x-helper-key": KEY})
    z = zipfile.ZipFile(io.BytesIO(r.content)) if r.status_code == 200 else None
    ok(z and "worker/clipstudio_worker.py" in z.namelist() and z.read("worker/server.txt") == b"https://studio.example.com"
       and z.read("worker/enroll_key.txt").decode() == KEY, "GET /api/worker/package with the key: the helper, server.txt and the key")
    auth._fails.clear()
    ok(hc.get("/api/worker/package", headers={**ip()}).status_code == 403 and hc.get("/api/worker/package", headers={**ip(), "x-helper-key": bad}).status_code == 403,
       "…without a key or with a wrong one: 403")
    auth._fails.clear()
    kA = ca.post("/api/helper/key/show").json()["key"]
    r = hc.get("/api/worker/package", headers={**ip(), "x-helper-key": kA})
    zA = zipfile.ZipFile(io.BytesIO(r.content))
    ok(zA.read("worker/helper_code.txt").decode() == workers.claim_code(TA) and zA.read("worker/enroll_key.txt").decode() == kA, "a customer's package: their code and their key")
    r = owner.get("/api/helper/download"); zo = zipfile.ZipFile(io.BytesIO(r.content))
    ok(zo.read("worker/enroll_key.txt").decode() == KEY, "Download my helper (dashboard): the ZIP carries the key — it joins by itself")
    wt = reg(hc, KEY, name="upd").json()["token"]
    r = hc.get("/api/worker/update", headers={"x-worker-token": wt}); zu = zipfile.ZipFile(io.BytesIO(r.content))
    ok("worker/enroll_key.txt" not in zu.namelist(), "the self-update ZIP never carries the key")
    r = anon.get("/helper/install.ps1")
    ok(r.status_code == 200 and b"param(" in r.content and b"rk1." not in r.content and b"x-helper-key" in r.content, "install.ps1 is public and holds no key")

    # ------------------------------------------------------------ who may see the key
    ok(anon.post("/api/helper/key/show").status_code == 401 and anon.get("/api/helper/key").status_code == 401, "not signed in: no key")
    owner.post(f"/api/admin/users/{AID['B']}/access", json={"perms": {"helper.manage": False}})
    ok(cb.post("/api/helper/key/show").status_code == 403 and cb.post("/api/helper/key/rotate").status_code == 403, "helper.manage switched off: no key, no rotate")
    owner.post(f"/api/admin/users/{AID['B']}/access", json={"perms": {"helper.manage": True}})
    kb2 = cb.post("/api/helper/key/show").json()["key"]
    ok(kb2.startswith(f"rk{TB}.") and kb2 != KEY, "each account sees only its own key")
    ok(cb.post(f"/api/admin/workers/1/central", json={"on": True}).status_code == 403, "central stays Super Admin only")

    # ------------------------------------------------------------ never in logs / other responses
    all_keys = [KEY, ka, kA, kb, kb2, K2, K3]
    ok(not any(k in m for k in all_keys for m in LOGGED), f"no key in any server log line ({len(LOGGED)} lines)")
    ok(not any(k in b for k in all_keys for b in SEEN), "no key in the state, list or error answers")
    ok(not any(k in (a["detail"] or "") for k in all_keys for a in acl.activity(limit=500)), "no key in the activity log")
    me = owner.get("/api/me").text + owner.get("/api/workers").text + owner.get("/api/onboarding").text
    ok(not any(k in me for k in all_keys), "no key in /api/me, /api/workers, /api/onboarding")

    # ------------------------------------------------------------ routes are classified
    R = {(m, p): rule for m, p, rule, _t in acl.ROUTES}
    ok(R.get(("GET", "/api/helper/key")) == "helper.manage" and R.get(("POST", "/api/helper/key/show")) == "helper.manage"
       and R.get(("POST", "/api/helper/key/rotate")) == "helper.manage" and R.get(("POST", "/api/helper/key/revoke")) == "helper.manage",
       "the key routes need helper.manage (account admin / Super Admin)")
    ok(R.get(("GET", "/api/worker/package")) == acl.HELPER and R.get(("GET", "/helper/install.ps1")) == acl.ANYONE and "/helper/install.ps1" in main.PUBLIC_PATHS,
       "package: helper; install.ps1: public")
    ok({"helper_enroll", "helper_removed"} <= backup.PROTECTED, "a restored backup never brings back an old key")

    # ================================================================== the laptop helper (worker/clipstudio_worker.py)
    tmp = tempfile.mkdtemp()
    sys.argv = ["x", "http://localhost:9"]
    spec = importlib.util.spec_from_file_location("cw", ROOT + "/worker/clipstudio_worker.py"); cw = importlib.util.module_from_spec(spec); spec.loader.exec_module(cw)
    cw.TOKEN_FILE = cw.pathlib.Path(tmp) / ".worker_token"; cw.ENROLL_FILE = cw.pathlib.Path(tmp) / "enroll_key.txt"
    cw.REFUSED_FILE = cw.pathlib.Path(tmp) / "enroll_key.refused"; cw.ID_FILE = cw.pathlib.Path(tmp) / "helper_id.txt"
    ok("worker/enroll_key.txt" in cw.ZIP_KEEP, "the self-update never overwrites enroll_key.txt")
    gi = open(ROOT + "/.gitignore", encoding="utf-8").read().split()
    ok("worker/enroll_key.txt" in gi, ".gitignore keeps the key out of git")
    CLOG, PAUSES, CALLS = [], [], []
    cw.log = lambda m: CLOG.append(m)

    class Stop(BaseException):  # ends the helper's endless loop (it catches every Exception)
        pass

    class Transport(Exception):
        pass

    class HSE(Exception):
        def __init__(self, m, request=None, response=None):
            super().__init__(m); self.response = response

    class Resp:
        def __init__(self, r):
            self.status_code, self._r, self.headers, self.content = r.status_code, r, r.headers, r.content
        def json(self): return self._r.json()
        def raise_for_status(self):
            if self.status_code >= 400:
                raise HSE("x", response=self)

    PLAN = []  # what the fake network does for each GET: "ok", "down", or a callable run first

    def fpost(url, json=None, headers=None, timeout=None, **kw):
        CALLS.append(("POST", url, dict(headers or {})))
        auth._fails.clear()
        return Resp(hc.post(url.replace(cw.SERVER, ""), json=json, headers={**(headers or {}), **ip()}))

    def fget(url, headers=None, timeout=None, **kw):
        if not PLAN:
            raise Stop()
        step = PLAN.pop(0)
        CALLS.append(("GET", url, dict(headers or {})))
        if callable(step):
            step()
        if step == "down":
            raise Transport("All connection attempts failed")
        return Resp(hc.get(url.replace(cw.SERVER, ""), headers=headers))

    cw.httpx = types.SimpleNamespace(post=fpost, get=fget, HTTPError=Exception, TransportError=Transport, HTTPStatusError=HSE)
    cw.single_instance = lambda: None
    cw.llm_ready = lambda: False
    cw.archive_header = lambda: ""
    cw.handle_all = lambda jobs, h: None
    cw.backup_daily = lambda h: None
    cw.maybe_update = lambda v: None
    cw.pause = lambda s, offline=False: PAUSES.append((s, offline))

    # the owner's laptop as it is today: an approved token, no key file
    ltok = "owner-laptop-token-" + "L" * 40
    with SessionLocal() as s:
        s.add(Worker(name="LAPTOP", token_hash=workers.token_hash(ltok), approved=True, last_seen=utcnow())); s.commit()
    cw.TOKEN_FILE.write_text(ltok, encoding="utf-8")

    def server_forgets():  # e.g. the data was restored from an older backup: the token is unknown now
        with SessionLocal() as s:
            s.query(Worker).filter(Worker.token_hash == workers.token_hash(ltok)).delete(); s.commit()

    PLAN[:] = ["ok", server_forgets, "ok"]
    try:
        cw.main()
    except Stop:
        pass
    ok(CALLS[0][0] == "GET" and CALLS[0][2].get("x-worker-enroll") == "none", "laptop polls with x-worker-enroll: none (it has no key yet)")
    ok(cw.ENROLL_FILE.exists() and cw.ENROLL_FILE.read_text().strip() == KEY, "…the server hands it the key, it saves worker/enroll_key.txt (nothing typed)")
    posts = [c for c in CALLS if c[0] == "POST"]
    ok(len(posts) == 1 and posts[0][2].get("x-helper-key") == KEY and posts[0][2].get("x-old-helper") == hashlib.sha256(ltok.encode()).hexdigest()
       and posts[0][2].get("x-helper-id") == cw.ID_FILE.read_text().strip(),
       "after a 401 it registers again with the key (and its old token's hash, never the token)")
    newtok = cw.TOKEN_FILE.read_text().strip()
    w = worker_in(1, newtok)
    ok(newtok != ltok and w and w.approved and w.enrolled, "…and is approved at once: no pairing code, no button")
    gets = [c for c in CALLS if c[0] == "GET"]
    ok(len(gets) == 3 and gets[-1][2]["x-worker-token"] == newtok and gets[-1][2]["x-worker-enroll"] == KEY.split(".")[1], "…and polls on with the new token")
    ok(any("nothing to approve" in m for m in CLOG) and not any("pairing code" in m for m in CLOG), "laptop log: 'nothing to approve', no pairing code")
    ok(not any(KEY in m for m in CLOG) and not any(KEY.split(".")[2] in m for m in CLOG), "the key is never in the helper's log")

    # removed by the owner: does not come back by itself; it waits for approval with a pairing code (old flow)
    CALLS.clear(); CLOG.clear()
    PLAN[:] = [lambda: owner.delete(f"/api/workers/{worker_in(1, newtok).id}"), "ok"]
    try:
        cw.main()
    except Stop:
        pass
    t5 = cw.TOKEN_FILE.read_text().strip()
    ok(worker_in(1, t5) is not None and not worker_in(1, t5).approved, "removed in the dashboard: it does not rejoin by itself (it waits for approval)")
    ok(any("not accepted" in m for m in CLOG) and any("pairing code: " + cw.pair_code(t5) in m for m in CLOG), "…its log says why, and shows the pairing code")
    ok(not cw.ENROLL_FILE.exists() and cw.REFUSED_FILE.read_text().strip() == KEY, "[1] …and the key is set aside (enroll_key.refused) until a person approves it")

    # offline: back-off, but it calls in again as soon as the network is back
    owner.post(f"/api/workers/{worker_in(1, t5).id}/approve", json={"code": cw.pair_code(t5)})
    CALLS.clear(); CLOG.clear(); PAUSES.clear()
    PLAN[:] = ["down", "down", "down", "ok"]
    try:
        cw.main()
    except Stop:
        pass
    ok([p[1] for p in PAUSES[:3]] == [True, True, True] and PAUSES[3] == (cw.POLL_SECONDS, False), f"no connection: waits marked 'offline' (look at the network), then normal again {PAUSES}")
    ok(any("Connected again" in m for m in CLOG), "…'Connected again' in the log")
    PAUSES.clear(); CLOG.clear()

    class HTTP500:
        status_code = 500
        def json(self): return {}
        def raise_for_status(self): raise HSE("500")

    cw.httpx.get = lambda *a, **k: (_ for _ in ()).throw(Stop()) if len(PAUSES) >= 2 else HTTP500()
    try:
        cw.main()
    except Stop:
        pass
    ok([p[1] for p in PAUSES] == [False, False], "a server error (an answer) is not 'offline': normal back-off")
    cw.httpx.get = fget

    # pause(): fake clock and network
    spec2 = importlib.util.spec_from_file_location("cw2", ROOT + "/worker/clipstudio_worker.py"); cw2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(cw2)
    clock = {"t": 1000.0, "probes": 0, "up_at": 3}
    cw2.time = types.SimpleNamespace(time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))

    def net():
        clock["probes"] += 1
        return clock["probes"] >= clock["up_at"]

    real_net = cw2.network_up
    cw2.network_up = net
    cw2.pause(300, offline=True)
    ok(clock["t"] - 1000 == 90 and clock["probes"] == 3, f"offline, 5-minute back-off: the network is looked at every 30 s and it calls in 90 s later, when it is back ({clock['t'] - 1000} s)")
    clock.update(t=1000.0, probes=0, up_at=99)
    cw2.time = types.SimpleNamespace(time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + (3600 if s == 30 else s)))
    cw2.pause(300, offline=True)
    ok(clock["probes"] == 1, "PC slept (the clock jumped an hour): the wait is over at once, no extra 5 minutes")
    clock.update(t=1000.0, probes=0, up_at=1)
    cw2.time = types.SimpleNamespace(time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    cw2.pause(8, offline=False)
    ok(clock["t"] - 1000 == 8 and clock["probes"] == 0, "online: a plain wait, no network checks")
    seen = []

    class Sock:
        def close(self): pass

    def cc(addr, timeout=None):
        seen.append((addr, timeout))
        if addr[0] == "down.example":
            raise OSError("no route")
        return Sock()

    cw2.network_up = real_net
    cw2.socket = types.SimpleNamespace(create_connection=cc, gethostname=lambda: "h")
    cw2.SERVER = "https://up.example"; up = cw2.network_up()
    cw2.SERVER = "https://down.example"; down = cw2.network_up()
    ok(up and not down and seen[0] == (("up.example", 443), 5), "network check: name lookup + connect to the server's port 443, 5 s timeout, nothing sent")

    # first start with no internet: waits instead of stopping
    cw3 = cw2
    cw3.TOKEN_FILE = cw.pathlib.Path(tempfile.mkdtemp()) / ".worker_token"; cw3.ENROLL_FILE = cw.pathlib.Path(tmp) / "enroll_key.txt"
    cw3.ID_FILE = cw.pathlib.Path(tempfile.mkdtemp()) / "helper_id.txt"; cw3.REFUSED_FILE = cw.pathlib.Path(tmp) / "x.refused"
    cw3.log = lambda m: None
    cw3.SERVER = cw.SERVER
    tries = []

    def post_down_then_up(url, json=None, headers=None, timeout=None, **kw):
        tries.append(1)
        if len(tries) < 3:
            raise Transport("offline")
        return fpost(url, json=json, headers=headers)

    cw3.httpx = types.SimpleNamespace(post=post_down_then_up, get=fget, HTTPError=Exception, TransportError=Transport, HTTPStatusError=HSE)
    P3 = []
    cw3.pause = lambda s, offline=False: P3.append(offline)
    cw3.time = __import__("time")
    tk = cw3.first_token()
    ok(len(tries) == 3 and P3 == [True, True] and worker_in(1, tk).approved, "first start without internet: waits (offline checks), then joins by itself with the key")

    # ================================================================== the phone helper (worker/phone/phone.py)
    home = tempfile.mkdtemp()
    os.environ["RAJI_HELPER_HOME"] = home; os.environ["RAJI_SERVER"] = "http://localhost:9"; os.environ.pop("RAJI_KEY", None)
    sys.argv = ["phone.py"]
    spec = importlib.util.spec_from_file_location("phone", ROOT + "/worker/phone/phone.py"); phone = importlib.util.module_from_spec(spec); spec.loader.exec_module(phone)
    PCALLS = []

    def ppost(url, json=None, headers=None, timeout=None, **kw):
        PCALLS.append(("POST", dict(headers or {})))
        auth._fails.clear()
        return Resp(hc.post(url.replace(phone.SERVER, ""), json=json, headers={**(headers or {}), **ip()}))

    PPLAN = []

    def pget(url, headers=None, timeout=None, **kw):
        if not PPLAN:
            raise Stop()
        step = PPLAN.pop(0)
        PCALLS.append(("GET", dict(headers or {})))
        if callable(step):
            step()
        if step == "down":
            raise Transport("Network is unreachable")
        return Resp(hc.get(url.replace(phone.SERVER, ""), headers=headers))

    phone.httpx = types.SimpleNamespace(post=ppost, get=pget, HTTPError=Exception, TransportError=Transport, HTTPStatusError=HSE)
    phone.single_instance = lambda: None
    phone.ffmpeg_bin = lambda: "ffmpeg"
    PSL = []
    phone._sleep_until = lambda s, running, offline=False: PSL.append(offline)
    # a phone set up with the dashboard's command: the key in enroll_key.txt, no token yet
    open(os.path.join(home, "enroll_key.txt"), "w").write(KEY + "\n")
    PPLAN[:] = ["ok", "down", "down", "ok"]
    try:
        phone.main()
    except Stop:
        pass
    ptok = open(os.path.join(home, ".worker_token")).read().strip()
    ok(PCALLS[0][0] == "POST" and PCALLS[0][1].get("x-helper-key") == KEY and worker_in(1, ptok).approved and worker_in(1, ptok).name == "Android phone",
       "phone with the key: joins by itself, approved, no pairing code")
    ok(PCALLS[1][1].get("x-worker-enroll") == KEY.split(".")[1] and PCALLS[1][1].get("x-worker-can") == "source,analyze", "phone polls with its key id and source,analyze")
    ok(PSL[:3] == [False, True, True] and PSL[3] is False, f"phone offline: waits that look at the network, then normal again {PSL}")
    plog = open(os.path.join(home, "helper.log"), encoding="utf-8").read()
    ok("nothing to approve" in plog and "pairing code" not in plog and "Connected again" in plog, "phone log: nothing to approve; connected again")
    ok(KEY not in plog, "phone log: never the key")
    # the server forgets the phone: it rejoins by itself
    PCALLS.clear()
    PPLAN[:] = [lambda: (lambda s: (s.query(Worker).filter(Worker.token_hash == workers.token_hash(ptok)).delete(), s.commit(), s.close()))(SessionLocal()), "ok"]
    try:
        phone.main()
    except Stop:
        pass
    p2 = open(os.path.join(home, ".worker_token")).read().strip()
    ok(p2 != ptok and worker_in(1, p2).approved, "phone: token unknown (401) -> rejoins by itself with the key")
    # a phone set up before keys existed: no key file; the server hands it over on its next poll
    open(os.path.join(home, "enroll_key.txt"), "w").write("")
    PPLAN[:] = ["ok"]
    try:
        phone.main()
    except Stop:
        pass
    ok(open(os.path.join(home, "enroll_key.txt")).read().strip() == KEY, "phone without a key: the server hands it over, it is saved (enroll_key.txt)")
    ok(oct(os.stat(os.path.join(home, "enroll_key.txt")).st_mode)[-3:] in ("600", "666"), "…file mode set (600 on the phone; Windows ignores it)")
    # _sleep_until with a fake clock
    spec = importlib.util.spec_from_file_location("phone2", ROOT + "/worker/phone/phone.py"); ph2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(ph2)
    clk = {"t": 0.0, "n": 0}
    ph2.time = types.SimpleNamespace(time=lambda: clk["t"], sleep=lambda s: clk.__setitem__("t", clk["t"] + s))

    def pnet():
        clk["n"] += 1
        return clk["n"] >= 2

    ph2.network_up = pnet
    ph2._sleep_until(300, set(), True)
    ok(60 <= clk["t"] <= 62 and clk["n"] == 2, f"phone offline with a 5-minute back-off: back after ~60 s when the network returns ({clk['t']} s)")
    clk.update(t=0.0, n=0)
    ph2._sleep_until(20, set(), False)
    ok(clk["t"] == 20 and clk["n"] == 0, "phone online: a plain 20 s wait")
    for h in list(phone.log.handlers):
        phone.log.removeHandler(h); h.close()

    # setup.sh: the key is asked for (hidden), checked exactly, kept in a private file
    sh = open(ROOT + "/worker/phone/setup.sh", encoding="utf-8").read()
    ok("read -r -s KEY </dev/tty" in sh and "RAJI_KEY" not in sh and "enroll_key.txt" in sh and "umask 077" in sh and "chmod 600" in sh,
       "[7] setup.sh: asks for the key at a hidden prompt (never on the command line), private file")
    ok('"${2' not in sh and "nothing to approve" in sh, "setup.sh: never a key/code argument; says 'nothing to approve'")
    page = open(ROOT + "/static/index.html", encoding="utf-8").read()
    ok("/api/helper/key/show" in page and "/api/helper/key/rotate" in page and "/api/helper/key/revoke" in page and "#wk-key" in page,
       "dashboard: Show / New key / Switch off; the key box hides with helper.manage")
    ok("type the pairing code it shows:" not in page and "A helper without the key is waiting" in page, "onboarding: no pairing code in the normal path (only for a helper without the key)")

    # ================================================================== review fixes (round 2)
    from datetime import timedelta
    sha = lambda x: hashlib.sha256(x.encode()).hexdigest()
    auth._fails.clear()

    def load_cw(name):
        sys.argv = ["x", "http://localhost:9"]
        sp = importlib.util.spec_from_file_location(name, ROOT + "/worker/clipstudio_worker.py"); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
        tdir = cw.pathlib.Path(tempfile.mkdtemp())
        m.TOKEN_FILE, m.ENROLL_FILE, m.REFUSED_FILE, m.ID_FILE = tdir / ".worker_token", tdir / "enroll_key.txt", tdir / "enroll_key.refused", tdir / "helper_id.txt"
        m.LOGS = []
        m.log = lambda s, _m=m: _m.LOGS.append(s)
        m.httpx = types.SimpleNamespace(post=fpost, get=fget, HTTPError=Exception, TransportError=Transport, HTTPStatusError=HSE)
        m.single_instance = lambda: None; m.llm_ready = lambda: False; m.archive_header = lambda: ""
        m.handle_all = lambda jobs, h: None; m.backup_daily = lambda h: None; m.maybe_update = lambda v: None
        m.pause = lambda s, offline=False: None
        return m

    # [1] MUST - the reviewer's chain, server side: removed -> pairing fallback -> row purged after 30 min -> key again
    K5 = owner.post("/api/helper/key/rotate").json()["key"]
    IID = secrets.token_hex(16)
    t1 = reg(hc, K5, name="laptop-x", **{"x-helper-id": IID}).json()["token"]
    owner.delete(f"/api/workers/{worker_in(1, t1).id}")
    r = reg(hc, K5, name="laptop-x", **{"x-helper-id": IID})
    ok(r.status_code == 403 and "removed" in r.json()["detail"], "[1] removed helper that leaves x-old-helper out: refused by its install id")
    r = reg(hc, None, name="laptop-x", **{"x-helper-id": IID}); t2 = r.json()["token"]
    ok(r.json()["approved"] is False, "[1] pairing fallback: waiting for a person")
    with SessionLocal() as s:
        w = s.query(Worker).filter(Worker.token_hash == workers.token_hash(t2)).first(); w.created_at = utcnow() - timedelta(minutes=31); s.commit()
    reg(hc, None, name="someone-else")
    ok(worker_in(1, t2) is None, "[1] the waiting row is purged after 30 minutes (as before)")
    r = reg(hc, K5, name="laptop-x", **{"x-helper-id": IID, "x-old-helper": sha(t2)})
    ok(r.status_code == 403 and "removed" in r.json()["detail"], "[1] ...and the helper is still refused with the key (remembered by install id, not by the row)")
    t3 = reg(hc, None, name="laptop-x", **{"x-helper-id": IID}).json()["token"]
    owner.post(f"/api/workers/{worker_in(1, t3).id}/approve", json={"code": workers.pair_code(t3)})
    ok(IID not in enroll.removed()["ids"], "[1] a person approves it with its pairing code: taken off the removed list")
    ok(hc.get("/api/worker/jobs", headers={"x-worker-token": t3, "x-worker-enroll": "none"}).json().get("enroll_key") == K5, "[1] ...and it is handed the key again")

    # [1] the same chain with the real laptop helper (key file, dashboard removal, purge, 401)
    c4 = load_cw("cw4")
    c4.ENROLL_FILE.write_text(K5)
    tok4 = c4.first_token()
    ok(worker_in(1, tok4).approved and worker_in(1, tok4).install_id == c4.ID_FILE.read_text().strip(), "[1] laptop joins with the key; its install id is stored")
    CALLS.clear()

    def purge_waiting():
        with SessionLocal() as s:
            for w in s.query(Worker).filter(Worker.approved.is_(False)).all():
                w.created_at = utcnow() - timedelta(minutes=31)
            s.commit()
        reg(hc, None, name="someone-else")

    PLAN[:] = [lambda: owner.delete(f"/api/workers/{worker_in(1, tok4).id}"), "ok", purge_waiting, "ok"]
    try:
        c4.main()
    except Stop:
        pass
    final = c4.TOKEN_FILE.read_text().strip()
    keyposts = [c for c in CALLS if c[0] == "POST" and "x-helper-key" in c[2]]
    ok(len(keyposts) == 1 and not c4.ENROLL_FILE.exists() and c4.REFUSED_FILE.exists(), "[1] laptop: refused once, key set aside, never sent again")
    ok(worker_in(1, final) is not None and not worker_in(1, final).approved, "[1] laptop: after the purge and the 401 it waits for a person (not approved by itself)")
    with SessionLocal() as s:
        mine = s.query(Worker).filter(Worker.install_id == c4.ID_FILE.read_text().strip(), Worker.approved.is_(True)).count()
    ok(mine == 0, "[1] no approved helper with that install id")

    # [2] New key: hand-over only to helpers approved by a person, or joined with the key being replaced
    K6 = owner.post("/api/helper/key/rotate").json()["key"]
    t_old = reg(hc, K6, name="joined-K6").json()["token"]
    K7 = owner.post("/api/helper/key/rotate").json()["key"]
    IDC = secrets.token_hex(16)
    t_cur = reg(hc, K7, name="joined-K7", **{"x-helper-id": IDC}).json()["token"]
    tp = "person-approved-" + "p" * 40
    with SessionLocal() as s:
        s.add(Worker(name="by-hand", token_hash=workers.token_hash(tp), approved=True, last_seen=utcnow())); s.commit()
    K8 = owner.post("/api/helper/key/rotate").json()["key"]
    give = lambda tk: hc.get("/api/worker/jobs", headers={"x-worker-token": tk, "x-worker-enroll": "none"}).json().get("enroll_key")
    ok(give(tp) == K8, "[2] approved by a person: handed the new key")
    ok(give(t_cur) == K8 and worker_in(1, t_cur).enrolled == K8.split(".")[1], "[2] joined with the key just replaced: handed the new key (now counts as joined with it)")
    ok(give(t_old) is None, "[2] joined with an older key: handed nothing")
    r = owner.post("/api/helper/key/rotate", json={"leaked": True}); K9 = r.json()["key"]
    ok(r.status_code == 200 and KEYRX.match(K9) and K9 != K8, "[2] Key leaked: a new key")
    ok(worker_in(1, t_cur) is None and worker_in(1, t_old) is None and worker_in(1, tp) is not None, "[2] ...every helper that joined by key is removed; the one approved by a person stays")
    ok(reg(hc, K9, name="joined-K7", **{"x-helper-id": IDC}).status_code == 403, "[2] ...and a removed one cannot join again with the new key")
    page = open(ROOT + "/static/index.html", encoding="utf-8").read()
    ok("Key leaked" in page and "does not remove helpers that already joined" in page, "[2] dashboard: New key says what it does not do; Key leaked does all three")

    # [3] limits and the 401 loop
    auth._fails.clear()
    hc.post("/api/worker/register", json={"name": "x"}, headers={"x-forwarded-for": "10.7.7.7", "x-helper-key": K9, "x-helper-id": secrets.token_hex(16)})
    ok(not auth._fails.get("helper|10.7.7.7"), "[3] a successful key join does not use up the shared registration limit")
    hc.post("/api/worker/register", json={"name": "x"}, headers={"x-forwarded-for": "10.7.7.8", "x-helper-key": bad, "x-helper-id": secrets.token_hex(16)})
    ok(len(auth._fails.get("helper|10.7.7.8", [])) == 1, "[3] a refused one does")
    auth._fails.clear()

    class R:
        def __init__(self, code, detail=""): self.status_code, self._d = code, {"detail": detail}
        def json(self): return self._d
        def raise_for_status(self):
            if self.status_code >= 400: raise HSE(str(self.status_code), response=self)

    c5 = load_cw("cw5")
    c5.ENROLL_FILE.write_text(K9); c5.TOKEN_FILE.write_text("t" * 50)
    P5 = []

    def p5(s, offline=False):
        P5.append(s)
        if len(P5) >= 9:
            raise Stop()

    c5.pause = p5
    c5.httpx = types.SimpleNamespace(post=lambda *a, **k: R(429, "Too many attempts"), get=lambda *a, **k: R(401), HTTPError=Exception, TransportError=Transport, HTTPStatusError=HSE)
    try:
        c5.main()
    except Stop:
        pass
    ok(P5[:8] == [30, 60, 120, 240, 480, 960, 1800, 1800], f"[3] 401 and registering keeps failing: 30 s doubling to 30 min ({P5})")
    ok(any("cannot be used right now" in m and "pairing code instead" in m for m in c5.LOGS) and c5.ENROLL_FILE.exists(), "[3] a 429 on the key join: plain log line, pairing flow, key kept for later")

    # [4] a customer's key is never used to fall back into the owner's copy
    ka_old = ka  # A rotated its key long ago: this one is refused
    c6 = load_cw("cw6")
    c6.ENROLL_FILE.write_text(ka_old)
    CALLS.clear(); code = None
    try:
        c6.get_token(force_new=True)
    except SystemExit as e:
        code = str(e.code)
    ok(code and "customer account" in code and len([c for c in CALLS if c[0] == "POST"]) == 1, "[4] laptop: refused customer key and no helper code -> stops, no register into the owner's copy")
    os.environ["RAJI_HELPER_HOME"] = tempfile.mkdtemp(); sys.argv = ["phone.py"]
    sp = importlib.util.spec_from_file_location("ph3", ROOT + "/worker/phone/phone.py"); ph3 = importlib.util.module_from_spec(sp); sp.loader.exec_module(ph3)
    ph3.httpx = phone.httpx
    open(os.path.join(os.environ["RAJI_HELPER_HOME"], "enroll_key.txt"), "w").write(ka_old)
    PCALLS.clear(); code = None
    try:
        ph3.get_token(force_new=True)
    except SystemExit as e:
        code = e.code
    ok(code == ph3.EXIT_STOP and len([c for c in PCALLS if c[0] == "POST"]) == 1, "[4] phone: the same - stops with a plain message (exit 6), never into the owner's copy")
    bash = shutil.which("bash")
    if bash:
        rx = [ln for ln in sh.splitlines() if ln.startswith("KEY_RX=")][0].split("#")[0].strip()
        good = "rk12.0123abcd." + "A" * 43
        cases = [good, "rk12.0123abcd.short", "rk12.0123ABCD." + "A" * 43, good + ";rm -rf ~", "x" + good, "rk1.0123abcd." + "A" * 44]
        script = rx + "\n" + "\n".join(f"[[ '{k}' =~ $KEY_RX ]] && echo Y || echo N" for k in cases)
        out = subprocess.run([bash, "-c", script], capture_output=True, text=True).stdout.split()
        ok(out == ["Y", "N", "N", "N", "N", "N"], f"[4] setup.sh checks the key's exact shape ({out})")

    # [5] the account's Helper PC switch
    kb3 = cb.post("/api/helper/key/show").json()["key"]
    tb = reg(hc, kb3, name="b-pc").json()["token"]
    owner.post(f"/api/admin/users/{AID['B']}/access", json={"perms": {"helper.manage": False}})
    r = reg(hc, kb3, name="b-pc2")
    ok(r.status_code == 403 and r.json()["detail"] == enroll.INVALID, "[5] Helper PC switched off: no key joins")
    ok("enroll_key" not in hc.get("/api/worker/jobs", headers={"x-worker-token": tb, "x-worker-enroll": "none"}).json(), "[5] ...no key hand-over")
    ok(hc.get("/api/worker/package", headers={**ip(), "x-helper-key": kb3}).status_code == 403, "[5] ...no package")
    owner.post(f"/api/admin/users/{AID['B']}/access", json={"perms": {"helper.manage": True}})
    ok(reg(hc, kb3, name="b-pc3").status_code == 200, "[5] switched on again: joins work")

    # [6] the key file keeps its access rights when the server hands over a new key (written in place)
    c6.ENROLL_FILE.write_text(K8)
    ino = os.stat(c6.ENROLL_FILE).st_ino
    c6.save_enroll_key(K9)
    ok(c6.ENROLL_FILE.read_text() == K9 and os.stat(c6.ENROLL_FILE).st_ino == ino, "[6] save_enroll_key writes in place (same file: its access rights stay)")
    ps1 = open(ROOT + "/worker/install.ps1", encoding="utf-8").read()
    ok("icacls $keyFile /inheritance:r" in ps1, "[6] install.ps1 locks the key file to this Windows user (on every run)")

    # [7] Windows: the key from the clipboard or a hidden prompt, history cleaned when it was on a command line
    ok("Get-Clipboard" in ps1 and "Read-Host -AsSecureString" in ps1 and "Set-Clipboard" in ps1 and "ConsoleHost_history.txt" in ps1
       and "Remove-Item Env:RAJI_KEY" in ps1 and "[Parameter(Mandatory = $true)][string]$Key" not in ps1, "[7] install.ps1: clipboard / hidden prompt; empties the clipboard; scrubs the history file")

    # [8] one key, even when many ask at once; a join counted under an old key never brings it back
    with tenant.use(TB):
        main.set_setting(enroll.SETTING, "null")
    got = []

    def ask():
        with tenant.use(TB):
            got.append(enroll.current_key())

    ths = [threading.Thread(target=ask) for _ in range(12)]
    [x.start() for x in ths]; [x.join() for x in ths]
    with tenant.use(TB):
        now_key = enroll.current_key()
        ok(len(set(got)) == 1 and got[0] == now_key, f"[8] 12 first calls at once: one key ({len(set(got))})")
        old_kid = enroll.kid_of(now_key)
        newer = enroll.rotate()
        enroll.note_use(old_kid)
        st8 = json.loads(main.get_setting(enroll.SETTING))
        ok(st8["kid"] == enroll.kid_of(newer) and st8["uses"] == [], "[8] a join counted after a rotation does not overwrite the new key")

    # [9] the same answer for a copy that does not exist and for a wrong key
    auth._fails.clear()
    r1 = anon.post("/api/worker/register", json={"name": "x"}, headers={"x-helper-key": "rk999.00000000." + "a" * 43, "x-helper-id": secrets.token_hex(16)})
    r2 = anon.post("/api/worker/register", json={"name": "x"}, headers={"x-helper-key": "rk%d.00000000." % TB + "a" * 43, "x-helper-id": secrets.token_hex(16)})
    ok(r1.status_code == r2.status_code == 403 and r1.json() == r2.json() == {"detail": enroll.INVALID}, "[9] same 403 text for an unknown account and a wrong key")
    auth._fails.clear()

    # [10] Hide hides everything; a waiting helper picks up a key written later
    ok("KEYCMD=null;loadKey();phoneCmd()" in page and "if(KEYCMD){el.textContent=KEYCMD.phone" not in page, "[10] Hide resets both commands (and no command carries the key anyway)")
    c7 = load_cw("cw7")
    t7 = c7.first_token()
    ok(not worker_in(1, t7).approved, "[10] a helper started without a key waits for approval")
    PLAN[:] = ["ok", lambda: c7.ENROLL_FILE.write_text(K9), "ok"]
    try:
        c7.main()
    except Stop:
        pass
    t7b = c7.TOKEN_FILE.read_text().strip()
    ok(t7b != t7 and worker_in(1, t7b).approved, "[10] ...and joins by itself once a key is written next to it (re-read on each 403)")

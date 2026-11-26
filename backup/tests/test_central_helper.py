"""Central helper: one helper of the Super Admin serves every account; each job carries a signed ticket."""
import os, sqlite3, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-central-pass-61"
os.environ["BASE_URL"] = "http://studio.example.com"
ROOT = r"D:\phone-helper"
sys.path.insert(0, ROOT)
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)

# --- an existing database from before this change: the workers table has no "central" / "can" columns yet
OLD_TOK = "old-laptop-token-" + "x" * 40
import hashlib
con = sqlite3.connect(d + "/t.db")
con.execute("CREATE TABLE workers (id INTEGER PRIMARY KEY, name VARCHAR(120), token_hash VARCHAR(64) UNIQUE, approved BOOLEAN, "
            "pair_hash VARCHAR(64), llm BOOLEAN, ai_status VARCHAR(200), ip VARCHAR(64), last_seen DATETIME, created_at DATETIME)")
con.execute("INSERT INTO workers (name, token_hash, approved, llm, last_seen, created_at) VALUES (?, ?, 1, 0, ?, ?)",
            ("laptop", hashlib.sha256(OLD_TOK.encode()).hexdigest(), "2026-11-24 10:00:00", "2026-01-01 00:00:00"))
con.commit(); con.close()

from fastapi.testclient import TestClient
from app import main, accounts, tenant, auth, workers, telegram, onboarding
from app.db import SessionLocal, Clip, Project, Worker, Destination, utcnow
telegram.notify = lambda *a, **k: None
main.executor.submit = lambda *a, **k: None  # no rendering in this test
onboarding.check_with_google = lambda cid, sec: ""
CID, SEC = "123456789012-abcdefghijklmnopqrstuvwxyz012345.apps.googleusercontent.com", "GOCSPX-abcdefghijklmnopqrstuv"

CT = "central-phone-token-" + "c" * 40  # the owner's phone (central)
CAN = {"x-worker-can": "source,analyze"}


def clip_in(tid, vid, n=1, **kw):
    with tenant.use(tid), SessionLocal() as s:
        p = Project(url="u", video_id=vid, title="t " + vid, mode="cc", duration=600); s.add(p); s.commit()
        ids = []
        for i in range(n):
            c = Clip(project_id=p.id, start=10 + i * 40, end=40 + i * 40, aspect="9:16", status="fetching",
                     captions=[{"s": 0, "e": 1, "t": "x"}], fetch_since=utcnow(), **kw)
            s.add(c); s.commit(); ids.append(c.id)
        return ids


def clip(tid, cid):
    with tenant.use(tid), SessionLocal() as s:
        return s.get(Clip, cid)


def src_file(tid, cid):
    return [p for p in (tenant.root(tid) / "clips" / str(cid) / "src").glob("*.mp4")] if (tenant.root(tid) / "clips" / str(cid) / "src").exists() else []


with TestClient(main.app, raise_server_exceptions=False) as owner, TestClient(main.app) as anon, TestClient(main.app) as hc:
    # migration of the old table
    with SessionLocal() as s:
        w = s.query(Worker).filter(Worker.name == "laptop").first()
        ok(w is not None and w.approved and not w.central and w.can is None, "existing database: workers table gets the new columns; the old helper stays approved, not central")
    owner.post("/api/login", json={"username": "admin", "password": "owner-central-pass-61"})
    T = {}
    for k in "ABCD":
        auth._fails.clear()
        anon.post("/api/register", json={"name": "Cust " + k, "brand": "Brand " + k, "email": f"{k.lower()}@example.com",
                                         "password": "customer-pass-" + k * 6, "accept_terms": True})
    for u in owner.get("/api/admin/users").json()["users"]:
        owner.post(f"/api/admin/users/{u['id']}/approve")
        T[u["email"][0].upper()] = u["id"] + 1
    ok(sorted(T) == list("ABCD") and set(T.values()) <= set(accounts.active_tenants()), f"four active customers {T}")
    TA, TB, TC, TD = T["A"], T["B"], T["C"], T["D"]

    # jobs everywhere; every copy numbers its clips from 1, so a mix-up would be visible
    o1 = clip_in(1, "vidOWNER001")
    a1 = clip_in(TA, "vidAAAAAAA1"); b1 = clip_in(TB, "vidBBBBBBB1"); c1 = clip_in(TC, "vidCCCCCCC1")
    ok(o1 == a1 == b1 == c1 == [1], "each copy has its own clip number 1")
    with SessionLocal() as s:
        s.add(Worker(name="Android phone", token_hash=workers.token_hash(CT), approved=True, last_seen=utcnow())); s.commit()
        cw = s.query(Worker).filter(Worker.token_hash == workers.token_hash(CT)).first().id
    CTOK = "t%d.%s" % (TC, "own-helper-of-c-" + "k" * 40)  # customer C's own helper, online
    with tenant.use(TC), SessionLocal() as s:
        s.add(Worker(name="C pc", token_hash=workers.token_hash(CTOK), approved=True, last_seen=utcnow())); s.commit()
    H = {"x-worker-token": CT, **CAN, "x-worker-slots": "3"}

    # not central yet: only the owner's own job
    j = hc.get("/api/worker/jobs", headers=H).json()["jobs"]
    ok([x["video_id"] for x in j] == ["vidOWNER001"] and "ticket" not in j[0], "before it is marked central the phone gets only the owner's jobs")
    with SessionLocal() as s:
        s.get(Clip, 1).job_lease = None; s.commit()

    # marking central: Super Admin only
    cust = TestClient(main.app)
    cust.post("/api/login", json={"username": "a@example.com", "password": "customer-pass-AAAAAA"})
    ok(cust.post(f"/api/admin/workers/{cw}/central", json={"on": True}).status_code == 403, "a customer cannot mark a helper central")
    with SessionLocal() as s:
        s.add(Worker(name="new", token_hash=workers.token_hash("unapproved-" + "u" * 40), approved=False, last_seen=utcnow())); s.commit()
        un = s.query(Worker).filter(Worker.name == "new").first().id
    ok(owner.post(f"/api/admin/workers/{un}/central", json={"on": True}).status_code == 400, "an unapproved helper cannot be central")
    with SessionLocal() as s:
        lap = s.query(Worker).filter(Worker.name == "laptop").first().id
    r = owner.post(f"/api/admin/workers/{lap}/central", json={"on": True})
    ok(r.status_code == 400 and "too old" in r.json()["detail"], "a helper that never said what it can do (old laptop) cannot be made central: " + r.json()["detail"][:60])
    r = owner.post(f"/api/admin/workers/{cw}/central", json={"on": True})
    ok(r.status_code == 200 and r.json()["central"] is True, "the Super Admin marks the phone central")
    ok(any(w["central"] for w in owner.get("/api/workers").json()), "the helper list shows it as central")

    # central: owner + A + B (C has its own helper online), each with the right ticket
    j = hc.get("/api/worker/jobs", headers=H).json()["jobs"]
    vids = sorted(x["video_id"] for x in j)
    ok(vids == ["vidAAAAAAA1", "vidBBBBBBB1", "vidOWNER001"], f"central helper gets jobs from several accounts, not C (own helper online): {vids}")
    tk = {x["video_id"]: x.get("ticket") for x in j}
    ok(tk["vidOWNER001"] is None and tk["vidAAAAAAA1"] and tk["vidBBBBBBB1"] and tk["vidAAAAAAA1"] != tk["vidBBBBBBB1"], "other accounts' jobs carry a ticket; the owner's own job needs none")
    ta, tb = workers.read_ticket(tk["vidAAAAAAA1"]), workers.read_ticket(tk["vidBBBBBBB1"])
    ok(ta["tid"] == TA and tb["tid"] == TB and ta["wid"] == cw and ta["kind"] == "source", "tickets name the account, the job and this helper")
    ok(clip(TA, 1).fetch_tries == 1 and clip(TA, 1).job_lease is not None and clip(TB, 1).fetch_tries == 1, "lease and tries are set in each account's own copy")
    j2 = hc.get("/api/worker/jobs", headers=H).json()["jobs"]
    ok(j2 == [], "the next call does not hand out the same jobs again")

    # forged / altered tickets
    def post_src(cid, ticket, tok=CT):
        h = {"x-worker-token": tok}
        if ticket is not None:
            h["x-job-ticket"] = ticket
        return hc.post(f"/api/worker/source/{cid}", headers=h, files={"file": ("source.mp4", b"video-bytes", "video/mp4")})
    import base64
    body, sig = tk["vidAAAAAAA1"].rsplit(".", 1)
    payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
    forged = base64.urlsafe_b64encode(payload.replace(f"{TA}|", f"{TB}|", 1).encode()).decode().rstrip("=") + "." + sig
    ok(post_src(1, forged).status_code == 403, "a ticket with the account changed is refused")
    ok(post_src(1, tk["vidAAAAAAA1"][:-2] + "00").status_code == 403, "a ticket with a changed signature is refused")
    ok(post_src(1, "rubbish").status_code == 403 and post_src(1, "").status_code == 403, "garbage is refused")
    ok(post_src(2, tk["vidAAAAAAA1"]).status_code == 403, "a ticket opens only its own clip")
    ok(hc.post("/api/worker/voice/1", headers={"x-worker-token": CT, "x-job-ticket": tk["vidAAAAAAA1"]},
               files={"file": ("v.mp3", b"x", "audio/mpeg")}).status_code == 403, "a download ticket does not open the voice-over address")
    ok(post_src(1, tk["vidAAAAAAA1"], tok=OLD_TOK).status_code == 403, "another (non-central) helper of the owner cannot use the ticket")
    ok(post_src(1, tk["vidAAAAAAA1"], tok=CTOK).status_code == 403, "a customer's helper cannot use a ticket")
    expired = workers.make_ticket(TA, "source", 1, cw, ttl=-5)
    ok(post_src(1, expired).status_code == 403, "an expired ticket is refused")
    r = hc.post("/api/worker/fail/1", headers={"x-worker-token": CT, "x-job-ticket": tk["vidAAAAAAA1"]}, json={"kind": "analyze", "error": "x"})
    ok(r.status_code == 403, "the fail address checks the job kind against the ticket")
    ok(clip(TA, 1).status == "fetching" and not src_file(TA, 1) and not src_file(TB, 1), "nothing was delivered by the refused calls")

    # delivery lands in the right account only
    r = post_src(1, tk["vidAAAAAAA1"])
    ok(r.status_code == 200, f"A's video delivered with A's ticket ({r.status_code} {r.text[:80]})")
    ok(clip(TA, 1).status == "rendering" and clip(TB, 1).status == "fetching" and clip(1, 1).status == "fetching" and clip(TC, 1).status == "fetching",
       "only A's clip 1 moved on; clip 1 of the owner, B and C is untouched")
    ok(len(src_file(TA, 1)) == 1 and not src_file(1, 1) and not src_file(TB, 1), "the file is saved in A's data folder only")
    with accounts.CoreSession() as s:
        acc = s.query(accounts.Account).filter(accounts.Account.tenant_id == TB).first(); acc.status = "blocked"; s.commit()
    ok(post_src(1, tk["vidBBBBBBB1"]).status_code == 410, "a ticket of an account blocked meanwhile is refused")
    with accounts.CoreSession() as s:
        acc = s.query(accounts.Account).filter(accounts.Account.tenant_id == TB).first(); acc.status = "active"; s.commit()
    r = post_src(1, tk["vidBBBBBBB1"])
    ok(r.status_code == 200 and clip(TB, 1).status == "rendering" and len(src_file(TB, 1)) == 1, "B's video lands in B")
    ok(post_src(1, None).status_code == 200 and clip(1, 1).status == "rendering", "the owner's own job is delivered without a ticket, as before")

    # leases, retries, MAX_JOB_TRIES per job
    a2 = clip_in(TA, "vidAAAAAAA2")[0]
    j = hc.get("/api/worker/jobs", headers=H).json()["jobs"]
    t2 = next(x["ticket"] for x in j if x["video_id"] == "vidAAAAAAA2")
    r = hc.post(f"/api/worker/fail/{a2}", headers={"x-worker-token": CT, "x-job-ticket": t2}, json={"kind": "source", "error": "network"})
    ok(r.status_code == 200 and clip(TA, a2).job_lease is None and clip(TA, a2).status == "fetching", "a failure with the ticket frees the job in A for another try")
    j = hc.get("/api/worker/jobs", headers=H).json()["jobs"]
    ok(any(x["video_id"] == "vidAAAAAAA2" for x in j) and clip(TA, a2).fetch_tries == 2, "it is handed out again (try 2)")
    with tenant.use(TA), SessionLocal() as s:
        c = s.get(Clip, a2); c.job_lease, c.fetch_tries = None, main.MAX_JOB_TRIES; s.commit()
    hc.get("/api/worker/jobs", headers=H)
    ok(clip(TA, a2).status == "failed", "after MAX_JOB_TRIES it fails in A, as with an own helper")

    # customer helpers keep their own world
    r = hc.get("/api/worker/jobs", headers={"x-worker-token": CTOK, **CAN})
    jc = r.json()["jobs"]
    ok(r.status_code == 200 and [x["video_id"] for x in jc] == ["vidCCCCCCC1"] and "ticket" not in jc[0], "C's own helper gets C's job only, without a ticket")
    clip_in(TA, "vidAAAAAAA3")
    with tenant.use(TC), SessionLocal() as s:
        s.query(Worker).first().central = True; s.commit()  # even if a customer's row said "central" somehow
        s.get(Clip, 1).job_lease = None; s.commit()
    jc = hc.get("/api/worker/jobs", headers={"x-worker-token": CTOK, **CAN, "x-worker-slots": "3"}).json()["jobs"]
    ok([x["video_id"] for x in jc] == ["vidCCCCCCC1"], "a customer's helper never gets another account's jobs (central flag in a customer copy means nothing)")
    with tenant.use(TC), SessionLocal() as s:
        s.get(Clip, 1).job_lease = None; s.commit()

    # C's own helper goes quiet -> the central helper takes C's job
    j = hc.get("/api/worker/jobs", headers=H).json()["jobs"]
    ok(not any(x["video_id"] == "vidCCCCCCC1" for x in j), "while C's own helper is online the central helper leaves C alone")
    from app.db import get_setting, set_setting
    with tenant.use(TC):
        ok(get_setting("shared_helper") == "1", "an account approved from now on starts with 'use the shared helper when my PC is off' ON")
        set_setting("shared_helper", "0")  # like an account that had its own helper before this change
    with tenant.use(TC), SessionLocal() as s:
        w = s.query(Worker).first(); w.last_seen = utcnow() - timedelta(minutes=30); s.commit()
        c = s.get(Clip, 1); c.job_lease = None; s.commit()
    j = hc.get("/api/worker/jobs", headers=H).json()["jobs"]
    ok(not any(x["video_id"] == "vidCCCCCCC1" for x in j), "C has its own helper and did not allow the shared one: its PC being off does not hand its jobs over")
    ccl = TestClient(main.app)
    ccl.post("/api/login", json={"username": "c@example.com", "password": "customer-pass-CCCCCC"})
    r = ccl.get("/api/account/helper-code").json()
    ok(r["code"] and r["shared"] is False and r["shared_chosen"] is False, "C's Helper card shows the switch off")
    r = ccl.post("/api/account/shared-helper", json={"on": True})
    ok(r.status_code == 200 and r.json()["shared"] is True, "C switches on 'Use the shared helper when my PC is off'")
    ok(owner.post("/api/account/shared-helper", json={"on": True}).status_code == 400, "the owner has no such switch (its helpers are the shared one)")
    j = hc.get("/api/worker/jobs", headers=H).json()["jobs"]
    ok(any(x["video_id"] == "vidCCCCCCC1" and workers.read_ticket(x["ticket"])["tid"] == TC for x in j), "C's own helper silent and C allowed it: the central helper takes C's job, with C's ticket")

    # slots: never more than the phone asks for
    for i in range(3):
        clip_in(TB, f"vidBBBBBBS{i}")
    j = hc.get("/api/worker/jobs", headers={**H, "x-worker-slots": "1"}).json()["jobs"]
    ok(len(j) == 1, f"x-worker-slots 1: one job ({len(j)})")
    j = hc.get("/api/worker/jobs", headers={**H, "x-worker-slots": "0"}).json()["jobs"]
    ok(j == [], "x-worker-slots 0: only says it is alive")

    # new customer D without a helper: setup and problems stop asking while the central helper is online
    dc = TestClient(main.app)
    dc.post("/api/login", json={"username": "d@example.com", "password": "customer-pass-DDDDDD"})
    dc.post("/api/onboarding/google", json={"client_id": CID, "client_secret": SEC})
    with tenant.use(TD), SessionLocal() as s:
        s.add(Destination(channel_id="UC-d", name="D channel", token="x")); s.commit()
    o = dc.post("/api/onboarding/ack", json={"key": "verify"}).json()
    ok(o["current"] == "face" and next(x for x in o["steps"] if x["key"] == "helper")["done"], f"D's setup: the helper step is done by the central helper (current: {o['current']})")
    keys = [i["key"] for i in dc.get("/api/health").json()["items"]]
    ok("no_helper" not in keys and "helper_offline" not in keys, f"D's problems list does not ask for a helper {keys}")
    hs = next(i for i in dc.get("/api/setup").json()["items"] if i["key"] == "helper")
    ok(hs["done"] and "shared helper" in hs["why"], "D's checklist: helper online (the shared one)")
    with tenant.use(TD):
        ok(workers.any_recent("source") and workers.any_recent("analyze") and not workers.any_recent("tts"), "D's downloads wait for the central phone; voice-overs do not (the phone makes none)")
    with SessionLocal() as s:
        s.get(Worker, cw).last_seen = utcnow() - timedelta(minutes=30); s.commit()
    keys = [i["key"] for i in dc.get("/api/health").json()["items"]]
    o = dc.get("/api/onboarding").json()
    ok("no_helper" in keys and o["current"] == "helper", "central helper offline: D is asked for a helper again")
    with SessionLocal() as s:
        s.get(Worker, cw).last_seen = utcnow(); s.commit()
    ok(owner.post(f"/api/admin/workers/{cw}/central", json={"on": False}).json()["central"] is False, "the Super Admin can switch central off")
    j = hc.get("/api/worker/jobs", headers=H).json()["jobs"]
    ok(all("ticket" not in x for x in j), "not central any more: no other account's jobs")
    with tenant.use(TD):
        ok(not workers.central_online(), "…and D no longer counts on it")
    old = workers.make_ticket(TB, "source", 2, cw)
    ok(post_src(2, old).status_code == 403, "tickets stop working once the helper is no longer central")

    # ---- review fixes
    ok(workers.TICKET_TTL == int(main.JOB_LEASE.total_seconds()), "a ticket lasts as long as the job lease")
    owner.post(f"/api/admin/workers/{cw}/central", json={"on": True})
    HA = {"x-worker-token": CT, "x-worker-can": "all", "x-worker-slots": "3"}  # even if the central helper claims everything
    from app.db import Channel
    with tenant.use(TA), SessionLocal() as s:
        for cl in s.query(Clip).filter(Clip.status == "fetching"): cl.job_lease = None
        ch = Channel(kind="reel", query="q", auto_scripts=True, script_wanted={"topic": "secret topic of A"}); s.add(ch); s.commit()
        rp = Project(url="u", video_id="", title="reel", mode="original", description="A's secret script " * 5, channel_ref=ch.id, duration=30); s.add(rp); s.commit()
        s.add(Clip(project_id=rp.id, start=0, end=30, aspect="9:16", status="fetching"))
        s.add(Clip(project_id=rp.id, start=0, end=30, aspect="9:16", status="draft", tts_pending='{"text": "A private voice text", "voice": "x"}'))
        s.add(Project(url="u", video_id="vidAAAAAANA", title="an", mode="cc", duration=600, analysis_pending=True)); s.commit()
    from app.db import set_setting as _ss
    with tenant.use(TA):
        _ss("pexels_key", "PEXELS-SECRET-OF-A")
    with tenant.use(TB), SessionLocal() as s:
        for cl in s.query(Clip).filter(Clip.status == "fetching"): cl.status = "failed"
        s.commit()
    with tenant.use(TC), SessionLocal() as s:
        for cl in s.query(Clip).filter(Clip.status == "fetching"): cl.status = "failed"
        s.commit()
    with SessionLocal() as s:
        for cl in s.query(Clip).filter(Clip.status == "fetching"): cl.status = "failed"
        s.commit()
    seen = []
    for _ in range(4):
        seen += hc.get("/api/worker/jobs", headers={**HA, "x-worker-llm": "1"}).json()["jobs"]
    foreign = [x for x in seen if x.get("ticket")]
    ok(foreign and {x["type"] for x in foreign} <= {"source", "analyze"}, f"another account's jobs are only downloads and analysis, whatever the helper claims ({sorted({x['type'] for x in foreign})})")
    blob = str(seen)
    ok("secret" not in blob.lower() and "PEXELS" not in blob and "private voice" not in blob, "no pexels key, voice-over text or script topic of another account is ever sent")
    import hmac as _h
    pl = f"{TA}|tts|2|{cw}|9999999999"
    fake = workers._b64(pl.encode()) + "." + workers._ticket_sig(pl)
    ok(workers.read_ticket(fake) is None, "even a correctly signed ticket of another kind (voice-over) is not accepted")
    try:
        workers.make_ticket(TA, "tts", 2, cw); made = True
    except ValueError:
        made = False
    ok(not made, "the server makes tickets only for downloads and analysis")
    ok(set(workers.TICKET_PATHS) == {"source", "analyze"} and all(set(v) <= {"source", "analysis", "fail"} for v in workers.TICKET_PATHS.values()),
       "a foreign ticket opens only the source / analysis / fail addresses")

    # reels of a customer are not 'waited for' by the phone; the helper-offline alert says so
    with tenant.use(TD), SessionLocal() as s:
        rp = Project(url="u", video_id="", title="reel", mode="original", description="x" * 50, duration=30); s.add(rp); s.commit()
        s.add(Clip(project_id=rp.id, start=0, end=30, aspect="9:16", status="fetching")); s.commit()
    with tenant.use(TD):
        ok(workers.any_recent("source") and not workers.any_recent("reel"), "D: downloads wait for the shared phone, a reel does not (no reel-capable helper)")
    items = dc.get("/api/health").json()["items"]
    bad = [i for i in items if i["key"] == "helper_offline"]
    ok(bad and "1 job" in bad[0]["text"], "D's problems: a reel waits and no helper that makes reels is online (alert not hidden by the phone)")
    hs = next(i for i in dc.get("/api/setup").json()["items"] if i["key"] == "helper")
    ok(not hs["done"] and "1 job" in hs["blocked"], "D's checklist shows the waiting reel")
    import app.render as rnd
    src = open(rnd.__file__, encoding="utf-8").read()
    ok('any_recent("reel" if isinstance(e, NeedsHelper) else "source")' in src, "render: a reel waits only for a helper that makes reels")

    # round robin: one place at a time goes to the accounts in turn
    clip_in(TA, "vidAAAARR01"); clip_in(TB, "vidBBBBRR01"); clip_in(TB, "vidBBBBRR02")
    with tenant.use(TA), SessionLocal() as s:
        for cl in s.query(Clip).filter(Clip.status == "fetching", Clip.project_id != None):
            if s.get(Project, cl.project_id).video_id != "vidAAAARR01": cl.status = "failed"
        s.query(Project).filter(Project.analysis_pending.is_(True)).update({"analysis_pending": False}); s.commit()
    got = []
    for _ in range(2):
        got += [workers.read_ticket(x["ticket"])["tid"] for x in hc.get("/api/worker/jobs", headers={**H, "x-worker-slots": "1"}).json()["jobs"] if x.get("ticket")]
    ok(sorted(got) == sorted([TA, TB]), f"with one free place, two calls serve two different accounts ({got})")

    # an error in a ticketed request is listed in that account's error list
    rc = TestClient(main.app, raise_server_exceptions=False)
    tb2 = clip_in(TB, "vidBBBBERR1")[0]
    t_err = workers.make_ticket(TB, "source", tb2, cw)
    real_save = main._save_upload
    main._save_upload = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk broke in test"))
    r = rc.post(f"/api/worker/source/{tb2}", headers={"x-worker-token": CT, "x-job-ticket": t_err}, files={"file": ("s.mp4", b"x", "video/mp4")})
    main._save_upload = real_save
    with tenant.use(TB):
        eb = get_setting("guide_errors") or ""
    with tenant.use(1):
        eo = get_setting("guide_errors") or ""
    ok(r.status_code == 500 and "disk broke" in eb and "disk broke" not in eo, "the error is in B's list, not the owner's")

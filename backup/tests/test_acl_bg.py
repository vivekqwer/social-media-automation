"""Access control in background work: uploads paused / switched off / limited, sources stopped, source types,
clips a day, automatic upload off, and the switchable features (mails, Instagram/Facebook, Telegram, viral picks, AI reels)."""
import json, os, sys, tempfile
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, acl, tenant, auth, youtube, scheduler, telegram, jobs, autopilot, quick, viral, permissions, meta, gmail
from app.db import SessionLocal, Destination, Channel, Clip, Project, set_setting, get_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
told = []
telegram.notify = lambda *a, **k: told.append(a[0])
auth.rate_limited = lambda ip, kind="owner": False
submitted = []
jobs.executor.submit = lambda fn, *a, **k: submitted.append((getattr(fn, "__name__", "?"), a))
CC = youtube.CC_LICENSE
uploaded = []
youtube.upload = lambda dest_id, path, title, desc, tags, publish_at=None: (uploaded.append((tenant.current(), title)), "yt%05d" % len(uploaded))[1]
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
with mk() as owner, mk() as c1, mk() as c2, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Owner channel", token="x")); s.commit()

    def customer(client, email, name):
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email, "password": PW, "accept_terms": True})
        aid = next(u["id"] for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email)
        tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        client.post("/api/login", json={"username": email, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
            with SessionLocal() as s:
                s.add(Destination(channel_id="UC-" + name, name=name + " channel", token="x")); s.commit()
        return aid, tid

    a1, t1 = customer(c1, "asha@example.com", "Asha")
    a2, t2 = customer(c2, "ravi@example.com", "Ravi")
    sw = lambda aid, key, on: owner.post(f"/api/admin/users/{aid}/access", json={"perms": {key: on}})
    lim = lambda aid, **limits: owner.post(f"/api/admin/users/{aid}/access", json={"limits": limits})
    ctl = lambda aid, action: owner.post(f"/api/admin/users/{aid}/control", json={"action": action})
    health = lambda c: [i["text"] for i in c.get("/api/health").json()["items"]]
    f = Path(d) / "video.mp4"; f.write_bytes(b"x")
    seq = [0]

    def queue(tid, n=1):
        with tenant.use(tid):
            with SessionLocal() as s:
                dest = s.query(Destination).first()
                seq[0] += 1
                p = Project(url="u", video_id="vid%08d" % seq[0], title="T", channel="C", mode="cc", license=CC, duration=600); s.add(p); s.commit()
                for i in range(n):
                    s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="queued", title=f"clip of copy {tid}", description="d",
                               output_path=str(f), destination_id=dest.id))
                s.commit()

    def statuses(tid):
        with tenant.use(tid):
            with SessionLocal() as s:
                return sorted(c.status for c in s.query(Clip).all())

    run_queue = scheduler.for_each_customer(scheduler.process_queue)
    # ---------- uploads paused by the Super Admin
    for t in (1, t1, t2):
        queue(t)
    ctl(a1, "pause_uploads")
    run_queue()
    ok(statuses(t1) == ["queued"] and statuses(t2) == ["uploaded"] and statuses(1) == ["uploaded"] and sorted(x[0] for x in uploaded) == [1, t2],
       f"uploads paused for customer 1: nothing of theirs goes up, everybody else's does ({uploaded})")
    ok(any("paused by the administrator" in x for x in health(c1)) and not any("administrator" in x for x in health(c2)),
       "customer 1's Problems list says: Uploads are paused by the administrator")
    run_queue(); run_queue()
    ok(statuses(t1) == ["queued"] and len(uploaded) == 2, "it stays queued quietly, round after round")
    with tenant.use(t1):
        errs = json.loads(get_setting("guide_errors") or "[]")
    ok(errs == [], "…without filling the error list")
    ctl(a1, "resume_uploads")
    run_queue()
    ok(statuses(t1) == ["uploaded"] and not any("administrator" in x for x in health(c1)), "resumed: the clip goes up in the next round, the notice is gone")
    rows = [r["detail"] for r in owner.get(f"/api/admin/activity?account_id={a1}").json()["rows"]]
    ok(any(x.startswith("Uploaded to YouTube: clip of copy") for x in rows), "the upload is in the customer's activity log")
    # ---------- "Upload to YouTube at all" off
    sw(a1, "uploads.youtube", False)
    queue(t1); n = len(uploaded)
    run_queue()
    ok(statuses(t1) == ["queued", "uploaded"] and len(uploaded) == n and any("switched off for your account" in x for x in health(c1)),
       "Upload to YouTube off: clips can be made and queued, none is uploaded, and the dashboard says so")
    sw(a1, "uploads.youtube", True)
    # ---------- uploads per day
    lim(a1, uploads_per_day=2)   # one was uploaded today already
    queue(t1, 2)
    run_queue()
    ok(statuses(t1).count("uploaded") == 2 and statuses(t1).count("queued") == 2, f"uploads a day = 2: only one more goes up today ({statuses(t1)})")
    ok(any("limit of 2 upload" in x for x in health(c1)), "…and the dashboard says the rest go up tomorrow")
    queue(t2, 2); run_queue()
    ok(statuses(t2) == ["uploaded"] * 3, "…another customer has no such limit")
    lim(a1, uploads_per_day=None)
    run_queue()
    ok(statuses(t1).count("uploaded") == 4, "limit removed: the waiting clips go up")

    # ---------- sources stopped / source types / clips a day
    searched = []
    autopilot.youtube.api_search_cc = lambda q, seen, n, pages=3: (searched.append((tenant.current(), q)), [])[1]

    def src(tid, kind="topic", **kw):
        with tenant.use(tid):
            with SessionLocal() as s:
                dest = s.query(Destination).first()
                ch = Channel(kind=kind, query=kw.pop("query", "street food"), mode="cc", auto=True, destination_id=dest.id, videos_per_run=1,
                             clips_per_video=2, clip_len=30, aspect="9:16", gap_hours=12, **kw)
                s.add(ch); s.commit()
                return ch.id

    def last(tid, chid):
        with tenant.use(tid):
            with SessionLocal() as s:
                return s.get(Channel, chid).last_status or ""

    def tick(tid):
        with tenant.use(tid):
            with SessionLocal() as s:
                for ch in s.query(Channel).all():
                    ch.last_run = None
                s.commit()
            autopilot.autopilot_tick()

    s1, s2 = src(t1), src(t2)
    ctl(a1, "stop_sources")
    tick(t1); tick(t2)
    ok(last(t1, s1) == "waiting: all sources are stopped by the administrator" and [x[0] for x in searched] == [t2],
       f"sources stopped for customer 1: their source waits (“{last(t1, s1)}”), the other customer's runs")
    ok(any("sources are stopped by the administrator" in x for x in health(c1)) and not any("has not run" in x or "failed last time" in x for x in health(c1)),
       "the dashboard says why, with no false alarms about the sources")
    with tenant.use(t1):
        autopilot.run_channel(s1)
    ok(len(searched) == 1, "…also “Run now” does nothing while stopped")
    ctl(a1, "start_sources")
    tick(t1)
    ok((t1, "street food") in searched and not last(t1, s1).startswith("waiting"), "started again: the source runs by itself")
    sw(a1, "source.topic", False); n = len(searched)
    tick(t1)
    ok(len(searched) == n and last(t1, s1) == "waiting: switched off for your account (Source type: topic (Creative Commons videos))", f"source type off: “{last(t1, s1)}”")
    sw(a1, "source.topic", True)
    r1 = src(t1, "reel", query="AI reels", auto_scripts=True, scripts=[])
    sw(a1, "ai.reels", False)
    tick(t1)
    ok("AI reels" in last(t1, r1) and last(t1, r1).startswith("waiting: switched off"), f"AI reels off: the AI source waits (“{last(t1, r1)}”)")
    from app import workers
    from app.db import Worker
    tok = f"t{t1}.helper-token-for-the-test"
    with tenant.use(t1):
        with SessionLocal() as s:
            s.get(Channel, r1).script_wanted = {"topic": "street food history"}
            s.add(Worker(name="pc", token_hash=workers.token_hash(tok), approved=True)); s.commit()
    kinds = lambda **h: sorted(j["type"] for j in anon.get("/api/worker/jobs", headers={"x-worker-token": tok, **h}).json()["jobs"])
    off_jobs = kinds(**{"x-worker-llm": "1"}) + kinds(**{"x-worker-ai": "idle"})
    sw(a1, "ai.reels", True)
    on_jobs = kinds(**{"x-worker-ai": "idle"}) + kinds(**{"x-worker-llm": "1"})
    ok(off_jobs == [] and on_jobs == ["setup_ai", "script"], f"…and the helper gets no script / AI-install jobs for this account (off: {off_jobs}, on: {on_jobs})")
    with tenant.use(t1):
        with SessionLocal() as s:
            s.delete(s.get(Channel, r1)); s.commit()

    # ---------- making clips: automatic upload off, clips a day
    def fake_render(cid):
        with SessionLocal() as s:
            c = s.get(Clip, cid); c.status, c.output_path = "rendered", str(f); s.commit()

    def fake_meta(cid, extra=None):
        with SessionLocal() as s:
            c = s.get(Clip, cid); c.title, c.description = "A title", "A description"; s.commit()

    autopilot.run_render, autopilot.auto_metadata = fake_render, fake_meta

    def make(tid, chid, moments=1):
        with tenant.use(tid):
            with SessionLocal() as s:
                seq[0] += 1
                p = Project(url="u", video_id="mk%09d" % seq[0], title="T", channel="C", mode="cc", license=CC, duration=600, channel_ref=chid,
                            moments=[{"start": 10 + 50 * i, "end": 40 + 50 * i} for i in range(moments)]); s.add(p); s.commit()
                log = []
                autopilot._make_clips(s, s.get(Channel, chid), p, log)
                return [(c.status, bool(c.autoqueue), c.publish_at is not None) for c in s.query(Clip).filter(Clip.project_id == p.id)], log

    got, _ = make(t1, s1)
    ok(got == [("queued", True, True)], f"by default an auto-upload source queues its clips: {got}")
    sw(a1, "uploads.auto", False)
    got, log = make(t1, s1)
    ok(got == [("rendered", False, False)] and "ready for review" in log[0], f"automatic upload off: the clip waits for approval although the source says auto: {got}")
    got2, _ = make(t2, s2)
    ok(got2 == [("queued", True, True)], "…another customer's clips still queue by themselves")
    ok(any("Automatic upload is switched off" in x for x in health(c1)), "…and the dashboard explains that every clip waits for approval")
    with tenant.use(t1):
        with SessionLocal() as s:
            dest = s.query(Destination).first()
            rch = Channel(kind="reel", query="My reels", mode="original", auto=True, destination_id=dest.id, videos_per_run=1, gap_hours=12,
                          review_first=False, scripts=[{"text": "Hello there. This is a script. It has sentences. Four of them.", "used": False}])
            s.add(rch); s.commit()
            log = []
            autopilot._run_reels(s, rch, log)
            rc = s.query(Clip).order_by(Clip.id.desc()).first()
            ok(rc.status == "fetching" and not rc.autoqueue and rc.publish_at is None, "…reels too: made, but not queued by themselves")
            pq = Project(url="u", video_id="quick000001", title="Q", channel="C", mode="cc", license=CC, duration=600,
                         moments=[{"start": 5, "end": 40}], quick={"format": "shorts", "length": "auto", "count": 1, "captions": False}); s.add(pq); s.commit()
            qid = pq.id
        quick.run_render, quick.auto_metadata = (lambda cid: None), (lambda cid, extra=None: None)
        quick.quick_run(qid)
        with SessionLocal() as s:
            qc = s.query(Clip).filter(Clip.project_id == qid).first()
            ok(qc is not None and not qc.autoqueue, "…and Quick upload clips wait for approval as well")
    sw(a1, "uploads.auto", True)
    with tenant.use(t1):
        made_today = acl.clips_today()
    lim(a1, clips_per_day=made_today + 1)
    got, log = make(t1, s1, moments=2)
    ok(len(got) == 1 and any("stopped: today's limit" in x for x in log), f"clips a day in background work: 1 of 2 moments is made, then it stops quietly ({log[-1]})")
    tick(t1)
    ok(last(t1, s1).startswith("waiting: today's limit of"), f"…and the source waits for tomorrow: “{last(t1, s1)}”")
    lim(a1, clips_per_day=None)

    # ---------- switchable features
    ran = []
    job = scheduler.for_each_customer(scheduler.when_on("gmail.send", lambda: ran.append(tenant.current())))
    sw(a1, "gmail.send", False)
    job()
    ok(ran == [1, t2], f"a timed job of a switched-off feature is skipped for that customer only: ran for {ran}")
    sent = []
    gmail.send = lambda to, body, subject=None: (sent.append(to), {"thread_id": "th1"})[1]
    with tenant.use(t1):
        permissions._save({"UCx": {"channel_id": "UCx", "name": "Creator", "url": "u", "status": "new", "email": "c@example.com", "message": "Hi there", "subject": "Hello"}})
        try:
            permissions.send_mail("UCx"); raised = ""
        except ValueError as e:
            raised = str(e)
        ok(raised == acl.message("gmail.send") and sent == [], "permission mails off: nothing is sent, whichever way Send is pressed")
        ok(telegram._handle_callback("pm:UCx") == acl.message("gmail.send"), "…Telegram's Send button says the same")
    sw(a1, "gmail.send", True)
    with tenant.use(t1):
        permissions.send_mail("UCx")
    ok(sent == ["c@example.com"], "switched on: the mail goes out")
    meta.status = lambda: {"connected": True, "on": True}
    sw(a1, "meta.use", False)
    with tenant.use(t1):
        ok(meta.crosspost(1) == {"skipped": "switched off for this account"}, "Instagram + Facebook off: cross-posting is skipped quietly")
    sw(a1, "meta.use", True)
    telegram._token = lambda: "token"
    for t in (t1, t2):
        with tenant.use(t):
            set_setting("tg_chat", "123")
    sw(a1, "telegram.use", False)
    with tenant.use(t1):
        off = telegram.enabled()
    with tenant.use(t2):
        on2 = telegram.enabled()
    polled = []
    scheduler.for_each_customer(scheduler.when_on("telegram.use", lambda: polled.append(tenant.current())))()
    ok(not off and on2 and polled == [1, t2], "Telegram off: no messages, no polling for that customer")
    sw(a1, "telegram.use", True)
    sw(a1, "clips.approve", False)
    with tenant.use(t1):
        ok(telegram._handle_callback("ap:1") == acl.message("clips.approve"), "a Telegram button needs the same switch as the dashboard button")
    sw(a1, "clips.approve", True)
    # viral picks
    row = {"video_id": "CCCCCCCCCC1", "title": "CC cooking", "channel": "CookCC", "channel_id": "UCcc", "views": 300000, "likes": 1, "comments": 1,
           "duration": 600, "cc": True, "short": False, "gain": 10}
    sw(a1, "source.viral", False)
    submitted.clear()
    with tenant.use(t1):
        set_setting("viral:" + viral.today(), json.dumps({"videos": {"CCCCCCCCCC1": row}, "shorts": {}}))
        picks_off = viral.pick_today()
    ok(picks_off == [] and not submitted, "Viral picks off: the board is kept, nothing is picked")
    sw(a1, "source.viral", True)
    with tenant.use(t1):
        picks_on = viral.pick_today()
    ok(len(picks_on) == 1 and submitted and submitted[0][0] == "clip_pick", "switched on: today's pick is made")
    ctl(a1, "stop_sources")
    done = []
    autopilot._process_video = lambda s, ch, v, log, mode=None, note=None: done.append(v) or True
    with tenant.use(t1):
        viral.clip_pick(viral.today(), "CCCCCCCCCC1")
        held = viral._picks()[viral.today()][0]
    ok(held.get("held") and held["note"].startswith("waiting: all sources are stopped") and not done, "sources stopped: a viral pick waits too")
    ctl(a1, "start_sources")
    with tenant.use(t1):
        viral.clip_pick(viral.today(), "CCCCCCCCCC1")
        after = viral._picks()[viral.today()][0]
    ok(done == ["CCCCCCCCCC1"] and not after.get("held"), "started again: the pick is clipped")
    # the owner's own copy is never restricted, even by a stray setting
    with tenant.use(1):
        ok(acl.state() is None and acl.upload_block() == "" and acl.source_block("topic") == "" and acl.allowed("gmail.send") and acl.health_items() == [],
           "the owner's own copy is never restricted")
print("DONE", flush=True)

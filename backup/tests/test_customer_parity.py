"""Customer parity: every feature the Super Admin has also works for a registered customer, inside the customer's own
copy, and every one of them has a Super Admin switch (on/off per user and per plan) that the server enforces, the page
hides and the timed jobs respect. Owner-only tools stay owner-only. A customer on the owner's Google project never
takes the owner's upload budget. Tenant isolation holds."""
import json, os, re, socket, sys, tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = os.environ.get("RS_WT", r"D:\customer-parity")
sys.path.insert(0, WT)

# no real network in this test: any socket that tries to leave the machine fails at once
_connect = socket.socket.connect
def _no_net(self, addr):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise OSError("no network in tests: " + str(host))
    return _connect(self, addr)
socket.socket.connect = _no_net

from fastapi.testclient import TestClient
from app import main, acl, tenant, auth, youtube, scheduler, telegram, jobs, quota, phone, reminder, learn, strikes, keywords, viral, owntopic, pcfolder
from app.db import SessionLocal, Destination, Channel, Clip, Project, Worker, PcItem, StrikeCase, set_setting, get_setting, utcnow
from app import workers as W
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
told = []
telegram.notify = lambda *a, **k: told.append(a[0] if a else "")
auth.rate_limited = lambda ip, kind="owner": False
submitted = []
jobs.executor.submit = lambda fn, *a, **k: submitted.append((getattr(fn, "__name__", "?"), tenant.current()))
uploaded = []
youtube.upload = lambda dest_id, path, title, desc, tags, publish_at=None: (uploaded.append((tenant.current(), title)), "yt%05d" % len(uploaded))[1]
youtube.push_thumbnail = lambda c: True
keywords._get = lambda q, hl, gl: [q, q + " recipe", q + " shorts"]  # YouTube's suggestions, without the network
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
NEW = ["phone.use", "keywords.use", "ideas.autotune", "clips.reminder", "clips.pc_folder", "backup.use", "strikes.reply"]

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
    f = Path(d) / "video.mp4"; f.write_bytes(b"x")
    seq = [0]

    def clip(tid, status="draft", n=1, publish_in_h=None, title=None):
        with tenant.use(tid):
            with SessionLocal() as s:
                dest = s.query(Destination).first()
                seq[0] += 1
                p = Project(url="https://youtu.be/vid%08d" % seq[0], video_id="vid%08d" % seq[0], title="Street food", channel="C",
                            mode="cc", license=youtube.CC_LICENSE, duration=600)
                s.add(p); s.commit()
                ids = []
                for i in range(n):
                    c = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status=status, title=title or f"clip of copy {tid}",
                             description="d", output_path=str(f), destination_id=dest.id,
                             publish_at=(utcnow() + timedelta(hours=publish_in_h)) if publish_in_h is not None else None)
                    s.add(c); s.commit(); ids.append(c.id)
                return ids[0] if n == 1 else ids

    def statuses(tid):
        with tenant.use(tid):
            with SessionLocal() as s:
                return sorted(c.status for c in s.query(Clip).all())

    # ================================================================ 1. the catalogue
    for k in NEW:
        ok(k in acl.DEFAULT and acl.DEFAULT[k] is True, f"switch {k} exists in acl.DEFAULT, on by default ({acl.LABEL.get(k)})")
    me = c1.get("/api/me").json()
    ok(all(me["access"]["perms"].get(k) is True for k in NEW), "a new customer has every new switch on (nothing they had is taken away)")
    cat = owner.get("/api/admin/acl").json()
    ok(all(any(p["key"] == k for p in cat["catalog"]["perms"]) for k in NEW), "the Access panel lists the new switches (per user and per plan)")
    used = {r.replace("inside:", "") for _m, _p, r, _t in acl.ROUTES}
    ok(all(k in used for k in NEW), "every new switch guards at least one address in acl.ROUTES")
    m = re.search(r"const PERM_HIDE=\{(.*?)\};", html, re.S)
    hide_keys = set(re.findall(r"'([a-z_]+\.[a-z_]+)':", m.group(1))) if m else set()
    ok(all(k in hide_keys for k in NEW), "the page hides each new feature when its switch is off (PERM_HIDE)")
    not_hidden = sorted(k for k in acl.DEFAULT if k not in hide_keys and not k.startswith("page.") and not k.startswith("source.")
                        and k not in ("uploads.youtube",))
    ok(not not_hidden, "every customer-facing switch (except pages, source types and 'upload at all', which have their own handling) "
                       f"hides something on the page — missing: {not_hidden}")

    # ================================================================ 2. each feature: ON works for the customer, OFF refused
    cid1, cid2 = clip(t1), clip(t2)
    up1 = clip(t1, "uploaded")
    with tenant.use(t1):
        with SessionLocal() as s:
            s.add(PcItem(sha="a" * 64, worker_id=1, rel="v.mp4", name="v.mp4", folder="", size=10, duration=30, width=1080, height=1920,
                         state="new")); s.commit()
            pid1 = s.query(PcItem).first().id
            s.add(StrikeCase(clip_id=up1, kind="copyright", status="open", mail_state="ready", data={"confirmed": True})); s.commit()
            sc1 = s.query(StrikeCase).first().id
    did1 = c1.get("/api/destinations").json()
    did1 = (did1.get("destinations") if isinstance(did1, dict) else did1)[0]["id"]
    # (feature row, switch, method, path, body, safe to call when ON)
    P = [
        ("sources: topic", "source.topic", "POST", "/api/channels", {"kind": "topic", "query": "street food india"}, False),
        ("sources: channel", "sources.add", "POST", "/api/channels", {"kind": "channel", "query": "@someone"}, False),
        ("sources: niches", "sources.add", "POST", "/api/niches", {"keys": ["food"]}, False),
        ("sources: run now", "sources.run", "POST", "/api/channels/1/run", None, False),
        ("sources: edit", "sources.edit", "PUT", "/api/channels/1", {}, False),
        ("sources: AI reels", "ai.reels", "POST", "/api/ai/setup", None, False),
        ("viral: picks", "source.viral", "POST", "/api/viral/pick", {"video_id": "abcdefghijk"}, False),
        ("viral page", "page.viral", "GET", "/api/viral", None, True),
        ("quick upload", "page.quick", "GET", "/api/quick", None, True),
        ("own video upload", "clips.own_video", "POST", "/api/upload-video", None, False),
        ("editor", "clips.edit", "PUT", f"/api/clips/{cid1}", {"title": "Better title"}, True),
        ("captions", "clips.captions", "POST", f"/api/clips/{cid1}/captions", {"lang": "auto"}, False),
        ("thumbnails", "clips.thumbnail", "POST", f"/api/clips/{cid1}/thumbnail", {}, False),
        ("face", "clips.face", "POST", "/api/settings/face-size", {"size": "M"}, True),
        ("reminder", "clips.reminder", "POST", "/api/settings/reminder", {"on": True, "preset": "share", "custom": ""}, True),
        ("first comment", "uploads.comment", "POST", "/api/settings/comment", {"on": True, "preset": "ask", "custom": ""}, True),
        ("keyword helper: research", "keywords.use", "GET", "/api/keywords?q=street+food", None, True),
        ("keyword helper: clip preview", "keywords.use", "POST", f"/api/keywords/clip/{cid1}", {"topic": "street food"}, True),
        ("keyword helper: use these", "keywords.use", "POST", f"/api/clips/{cid1}/metadata", {"topic": "street food"}, True),
        ("what works", "page.ideas", "GET", "/api/insights", None, True),
        ("auto-tune", "ideas.autotune", "POST", "/api/insights/autotune", {"on": True}, True),
        ("competitor tracker", "ideas.competitors", "POST", "/api/ideas/scan", None, False),
        ("permission inbox", "permissions.manage", "POST", "/api/permissions/refresh", None, False),
        ("permission mails", "gmail.send", "POST", "/api/permissions/UCabc/send", {}, False),
        ("gmail", "gmail.connect", "DELETE", "/api/gmail", None, True),
        ("telegram", "telegram.use", "POST", "/api/telegram/test", None, False),
        ("phone notifications", "phone.use", "POST", "/api/phone", {"action": "setup"}, True),
        ("instagram / facebook", "meta.use", "POST", "/api/meta/toggle", {"on": True}, False),
        ("strike responder: send", "strikes.reply", "POST", f"/api/strikes/{sc1}/send", {}, True),
        ("strike responder: automatic", "strikes.reply", "POST", "/api/settings/strike-reply", {"auto": True}, True),
        ("alerts board", "page.alerts", "GET", "/api/alerts/board", None, True),
        ("upload scheduling", "clips.upload_now", "POST", f"/api/clips/{cid1}/schedule", {"publish_at": None}, False),
        ("archive: get back", "clips.create", "POST", f"/api/clips/{up1}/get-back", None, True),
        ("PC folder: list", "clips.pc_folder", "GET", "/api/pc-items", None, True),
        ("PC folder: switch", "clips.pc_folder", "POST", "/api/pc-items/auto", {"on": False}, True),
        ("PC folder: skip", "clips.pc_folder", "POST", f"/api/pc-items/{pid1}/skip", None, True),
        ("helper: key", "helper.manage", "GET", "/api/helper/key", None, True),
        ("helper: download", "helper.manage", "GET", "/api/helper/download", None, False),
        ("backup: download", "backup.use", "GET", "/api/backup/download", None, True),
        ("backup: restore", "backup.use", "POST", "/api/backup/restore", {"ref": "upload", "confirm": "RESTORE"}, False),
        ("payments: ask for more", "account.upgrade", "POST", "/api/account/upgrade", {"channels": 2}, False),
        ("multiple channels: main", "channels.main", "POST", f"/api/destinations/{did1}/main", None, True),
        ("setup guide", "page.guide", "GET", "/api/guide", None, True),
        ("analytics", "page.analytics", "GET", "/api/analytics", None, True),
        ("pexels", "pexels.use", "POST", "/api/settings/pexels", {"key": ""}, False),
        ("affiliate", "affiliate.use", "POST", "/api/settings/affiliate", {"rules": []}, True),
    ]
    call = lambda c, mth, p, body: c.request(mth, p, json=body) if body is not None else c.request(mth, p)
    for row, key, mth, path, body, safe in P:
        if safe:
            r = call(c1, mth, path, body)
            ok(r.status_code != 403, f"[{row}] switch ON: the customer can use it ({mth} {path} -> {r.status_code} {str(r.json() if r.headers.get('content-type','').startswith('application/json') else '')[:90]})")
        sw(a1, key, False)
        r = call(c1, mth, path, body)
        det = (r.json() or {}).get("detail") if r.headers.get("content-type", "").startswith("application/json") else ""
        ok(r.status_code == 403 and "switched off" in str(det), f"[{row}] switch {key} OFF: refused with a plain message ({r.status_code} {str(det)[:70]})")
        if safe:
            r2 = call(c2, mth, path.replace(f"/{cid1}/", f"/{cid2}/").replace(f"/{cid1}", f"/{cid2}"), body)
            ok(r2.status_code != 403, f"[{row}] …another customer is not affected ({r2.status_code})")
        ok(c1.get("/api/me").json()["access"]["perms"][key] is False, f"[{row}] /api/me tells the page {key} is off (it hides it)")
        sw(a1, key, True)
    # redirect pages
    sw(a1, "channels.connect", False)
    r = c1.get("/auth/login", follow_redirects=False)
    ok(r.status_code == 302 and "connect_error" in r.headers.get("location", ""), "[channel connect] switched off: the Google login page says so (redirect)")
    sw(a1, "channels.connect", True)
    sw(a1, "gmail.connect", False)
    r = c1.get("/auth/gmail", follow_redirects=False)
    ok(r.status_code == 302 and "gmail_error" in r.headers.get("location", ""), "[gmail] switched off: the Gmail login says so (redirect)")
    sw(a1, "gmail.connect", True)
    # per plan, not only per user
    ok(owner.post("/api/admin/plans", json={"name": "Lite", "perms": {"keywords.use": False, "phone.use": False}, "limits": {}}).status_code == 200,
       "the Super Admin makes a plan with two of the new switches off")
    ok(owner.post(f"/api/admin/users/{a1}/plan", json={"plan": "Lite"}).status_code == 200, "…puts customer 1 on it")
    ok(c1.get("/api/keywords?q=street+food").status_code == 403 and c1.post("/api/phone", json={"action": "setup"}).status_code == 403
       and c2.get("/api/keywords?q=street+food").status_code == 200, "the plan's switches apply to customer 1 only")
    owner.post(f"/api/admin/users/{a1}/plan", json={"plan": ""})
    ok(c1.get("/api/keywords?q=street+food").status_code == 200, "off the plan: back on")
    # the Super Admin is never restricted, also while viewing the customer
    sw(a1, "backup.use", False)
    owner.post(f"/api/admin/users/{a1}/view")
    ok(owner.get("/api/backup/download").status_code == 200, "the Super Admin viewing customer 1 can still use a feature switched off for them")
    owner.post("/api/admin/view/exit")
    sw(a1, "backup.use", True)

    # ================================================================ 3. owner-only tools
    for mth, path, body in [("POST", "/api/admin/upload-lead", {"days": 10}), ("POST", "/api/admin/queue-cap", {"cap": 50}),
                            ("POST", "/api/admin/park-waiting", {"keep": 0}), ("POST", "/api/admin/unpark", {"per_day": 1}),
                            ("POST", "/api/admin/workers/1/central", {"on": True}), ("GET", "/api/admin/razorpay", None),
                            ("POST", "/api/admin/plans", {"name": "X", "perms": {}, "limits": {}}), ("GET", "/api/admin/users", None)]:
        r = call(c1, mth, path, body)
        ok(r.status_code == 403 and "Super Admin" in r.json()["detail"], f"owner-only: a customer is refused {mth} {path}")
    ok(c1.get("/api/settings/upload-lead").status_code == 200 and c1.get("/api/settings/queue-cap").status_code == 200,
       "a customer can read its own upload lead and queue cap")
    owner.post(f"/api/admin/users/{a1}/view")
    ok(owner.post("/api/admin/upload-lead", json={"days": 10}).json()["days"] == 10, "per-account version: the Super Admin sets customer 1's upload lead while viewing them")
    owner.post("/api/admin/view/exit")
    with tenant.use(t1):
        lead1 = quota.upload_lead_hours()
    with tenant.use(t2):
        lead2 = quota.upload_lead_hours()
    lead0 = quota.upload_lead_hours()
    ok(lead1 == 240 and lead2 == 48 and lead0 == 48, f"…only customer 1's copy changed ({lead1}/{lead2}/{lead0} hours)")
    with tenant.use(t1):
        set_setting("upload_lead_days", "")

    # ================================================================ 4. inside the work: the switches act, not only the addresses
    with tenant.use(t1):
        phone.setup(); n_on = phone.enabled()
        rc = Clip(start=0, end=30, aspect="9:16", overlays=[], captions=[])
        plan_on = reminder.plan(rc, 1080, 1920, 30.0)
        learn_on = learn.enabled()
    sw(a1, "phone.use", False); sw(a1, "clips.reminder", False); sw(a1, "ideas.autotune", False); sw(a1, "clips.captions", False)
    with tenant.use(t1):
        n_off = phone.enabled()
        plan_off = reminder.plan(rc, 1080, 1920, 30.0)
        learn_off = learn.enabled()
        with SessionLocal() as s:
            pr = s.query(Project).first(); cc = s.query(Clip).first()
            caps_off = main._wants_captions(s, SimpleNamespace(quick={"captions": True}, channel_ref=None), cc)
    with tenant.use(t2):
        phone.setup()
        n_other = phone.enabled()
        with SessionLocal() as s:
            caps_other = main._wants_captions(s, SimpleNamespace(quick={"captions": True}, channel_ref=None), s.query(Clip).first())
    ok(n_on and not n_off and n_other, "phone notifications: no push goes out for customer 1 while 'Phone notifications' is off; customer 2 unaffected")
    ok(plan_on is not None and plan_off is None, "reminder: drawn while on; not drawn on new videos while 'Like, share & subscribe reminder' is off")
    ok(learn_on and not learn_off, "auto-tune: off for the account while the Super Admin's 'Auto-tune' is off")
    ok(not caps_off and caps_other, "captions: a clip of customer 1 no longer waits for captions while 'Captions' is off")
    with tenant.use(t1):
        try:
            strikes.send_request(sc1); why = ""
        except ValueError as e:
            why = str(e)
    ok(why != acl.message("strikes.reply"), f"strike responder ON: the switches let it through (it stops later: {why[:60]})")
    sw(a1, "strikes.reply", False)
    with tenant.use(t1):
        try:
            strikes.send_request(sc1); why = ""
        except ValueError as e:
            why = str(e)
        st = strikes.settings_public()
    ok(why == acl.message("strikes.reply"), f"strike responder OFF: the request is never sent ({why[:60]})")
    ok(st["allowed"] is False and "Strike responder" in st["note"], "…and the Alerts → Strikes switch row says why")
    for k in ("phone.use", "clips.reminder", "ideas.autotune", "clips.captions", "strikes.reply"):
        sw(a1, k, True)
    # the helper is told to stop watching the PC folder while it is off
    CTOK = f"t{t1}." + "c" * 30
    with tenant.use(t1):
        with SessionLocal() as s:
            s.add(Worker(name="asha-pc", token_hash=W.token_hash(CTOK), approved=True, last_seen=utcnow(), can="all", pc_folder=r"D:\videos")); s.commit()
    HC = {"x-worker-token": CTOK, "x-worker-can": "all", "x-worker-pc": "1"}
    j_on = anon.get("/api/worker/jobs", headers=HC).json()
    sw(a1, "clips.pc_folder", False)
    j_off = anon.get("/api/worker/jobs", headers=HC).json()
    sw(a1, "clips.pc_folder", True)
    ok(j_on.get("pc_folder") == r"D:\videos" and j_off.get("pc_folder") == "", f"PC folder: the customer's helper watches its folder while on, none while off ({j_on.get('pc_folder')!r} / {j_off.get('pc_folder')!r})")

    # ================================================================ 5. the timed jobs: run for customers, respect the switches
    import app.telegram as m_tg, app.growth as m_gr, app.permissions as m_pm, app.viral as m_vi, app.competitors as m_co, app.stats as m_st
    import app.metadata as m_md, app.learn as m_le, app.payments as m_pa, app.backup as m_bk, app.phone as m_ph, app.comments as m_cm
    import app.pcfolder as m_pc, app.autopilot as m_ap, app.strikes as m_sk
    ran = []

    def rec(name):
        def fn(*a, **k):
            ran.append((name, tenant.current()))
        fn.__name__ = name
        return fn

    patches = [(scheduler, "process_queue"), (scheduler, "sweep_stale"), (scheduler, "check_helper"), (scheduler, "cleanup_disk"),
               (scheduler, "retry_thumbnails"), (scheduler, "check_uploads"), (scheduler, "autopilot_tick"), (scheduler, "recover"),
               (m_tg, "poll"), (m_tg, "daily_summary"), (m_gr, "swap_weak_thumbnails"), (m_gr, "weekly_report"), (m_pm, "check_replies"),
               (m_pm, "refresh_inbox"), (m_vi, "refresh"), (m_co, "scan_if_due"), (m_st, "sync_all"), (m_st, "sync_all_watch_time"),
               (m_md, "freshen_due"), (m_le, "daily"), (m_pa, "expire_due"), (m_bk, "daily"), (m_ph, "events"), (m_cm, "post_due"),
               (m_pc, "due"), (m_ap, "unpark_due"), (m_sk, "mail_round"), (m_sk, "watch")]
    names = {(m_le, "daily"): "learn_daily", (m_bk, "daily"): "backup_daily", (m_vi, "refresh"): "viral_refresh", (m_pc, "due"): "pc_due",
             (m_ph, "events"): "phone_events", (m_sk, "watch"): "strike_watch", (m_sk, "mail_round"): "strike_mails"}
    saved = {(mod, a): getattr(mod, a) for mod, a in patches}
    for mod, a in patches:
        setattr(mod, a, rec(names.get((mod, a), a)))
    real_submit = jobs.executor.submit
    jobs.executor.submit = lambda fn, *a, **k: fn(*a, **k)

    class FakeSch:
        def __init__(self, **k):
            self.jobs = []
        def add_job(self, fn, *a, **k):
            self.jobs.append(fn)
        def start(self):
            pass
    real_bs = scheduler.BackgroundScheduler
    scheduler.BackgroundScheduler = FakeSch
    try:
        sch = scheduler.start()
        KEYED = {"poll": "telegram.use", "swap_weak_thumbnails": "clips.thumbnail", "weekly_report": "page.analytics",
                 "check_replies": "gmail.send", "refresh_inbox": "permissions.manage", "scan_if_due": "ideas.competitors",
                 "freshen_due": "keywords.use", "learn_daily": "ideas.autotune", "phone_events": "phone.use",
                 "post_due": "uploads.comment", "pc_due": "clips.pc_folder", "strike_mails": "gmail.send"}
        ALWAYS = {"process_queue", "sweep_stale", "check_helper", "cleanup_disk", "retry_thumbnails", "check_uploads", "autopilot_tick",
                  "daily_summary", "viral_refresh", "sync_all", "sync_all_watch_time", "backup_daily", "unpark_due", "strike_watch"}
        for k in set(KEYED.values()):
            sw(a1, k, False)  # customer 1: every switchable background feature off; customer 2: all on
        ran.clear()
        for job in sch.jobs:
            job()
        by = {}
        for n, t in ran:
            by.setdefault(n, set()).add(t)
        ok(set(KEYED) | ALWAYS | {"expire_due"} <= set(by), f"every timed job ran in this round (missing: {sorted(set(KEYED) | ALWAYS | {'expire_due'} - set(by))})")
        for n, key in sorted(KEYED.items()):
            ok(by.get(n) == {tenant.OWNER, t2}, f"timed job {n}: runs for the owner and customer 2, skipped for customer 1 while {key} is off ({sorted(by.get(n, []))})")
        for n in sorted(ALWAYS):
            ok(by.get(n) == {tenant.OWNER, t1, t2}, f"timed job {n}: runs for every account ({sorted(by.get(n, []))})")
        ok(by.get("expire_due") == {tenant.OWNER}, "plans ending: once, in the owner's copy (all accounts at once)")
        ok(not (set(by) - set(KEYED) - ALWAYS - {"expire_due", "recover"}), f"no timed job without a decision here: {sorted(set(by) - set(KEYED) - ALWAYS - {'expire_due', 'recover'})}")
        for k in set(KEYED.values()):
            sw(a1, k, True)
        ran.clear()
        for job in sch.jobs:
            job()
        by = {}
        for n, t in ran:
            by.setdefault(n, set()).add(t)
        ok(all(by.get(n) == {tenant.OWNER, t1, t2} for n in KEYED), "switched on again: every per-customer job runs for customer 1 too")
    finally:
        scheduler.BackgroundScheduler = real_bs
        for (mod, a), fn in saved.items():
            setattr(mod, a, fn)
        jobs.executor.submit = real_submit

    # ================================================================ 6. the owner's upload budget
    for t in (1, t1, t2):  # clean start
        with tenant.use(t):
            with SessionLocal() as s:
                s.query(Clip).filter(Clip.status.in_(["queued"])).update({"status": "draft"}); s.commit()
    uploaded.clear()
    quota.spend("read", units=6400)  # the owner's project has 6,400 of 10,000 units used today: room for 2 more uploads, no more
    clip(1, "queued", n=2, publish_in_h=1, title="owner clip")
    clip(t1, "queued", publish_in_h=1, title="asha clip")  # customer 1: no Google keys of their own -> the owner's project
    with tenant.use(t2):
        set_setting("google_client_id", "123-own.apps.googleusercontent.com")  # customer 2: own Google project, own budget
    clip(t2, "queued", publish_in_h=1, title="ravi clip")
    with tenant.use(t1):
        shared1, due = quota.on_shared_budget(), quota.owner_uploads_due()
        room_before = quota.shared_room(quota.UPLOAD_COST)
        scheduler.process_queue()
        sb = quota.search_block()
        may = viral._may_search()
        real_conf = youtube.configured
        youtube.configured = lambda: True
        try:
            ot = owntopic.find("street food")
        finally:
            youtube.configured = real_conf
    hl1 = [i for i in c1.get("/api/health").json()["items"] if i.get("key") == "shared_budget"]
    hl2 = [i for i in c2.get("/api/health").json()["items"] if i.get("key") == "shared_budget"]
    ok(shared1 and due == 2 and not room_before, f"customer 1 is on the owner's project; the owner has {due} uploads due and the budget only holds those")
    ok(statuses(t1).count("queued") == 1 and not any(t == t1 for t, _ in uploaded), "customer 1's upload waits: it never takes the owner's upload budget")
    ok("kept for the owner's uploads" in sb and may is False, f"…nor a search (topic / viral Shorts): {sb[:80]}")
    ok(ot["source"] == "saved" and "owner" in ot["why_cached"], f"…own-video topic search falls back to the saved lists ({ot['why_cached'][:70]})")
    ok(len(hl1) == 1 and not hl2, "customer 1's Problems list says why its uploads wait (and how to get its own budget); customer 2 sees nothing")
    with tenant.use(t2):
        own2 = quota.on_shared_budget()
        scheduler.process_queue()
    ok(not own2 and ("ravi clip" in [x[1] for x in uploaded]), "customer 2 with its own Google keys uploads from its own budget at once")
    scheduler.process_queue()  # the owner's copy
    ok(sum(1 for t, _ in uploaded if t == 1) == 2, "the owner's two uploads go up")
    with tenant.use(t1):
        due_after = quota.owner_uploads_due()
        scheduler.process_queue()
    ok(due_after == 0 and any(t == t1 for t, _ in uploaded), "once the owner has nothing due, customer 1 uses what is left")
    with tenant.use(tenant.OWNER):
        owner_room = quota.shared_room(10 ** 6)
    ok(owner_room, "the owner is never limited by this rule")

    # ================================================================ 7. tenant isolation
    ok(not c2.get("/api/pc-items").json().get("waiting") and not owner.get("/api/pc-items").json().get("waiting"),
       "customer 1's PC-folder video is not listed for customer 2 or the owner")
    ok(c1.get("/api/pc-items").json().get("waiting") or c1.get("/api/pc-items").json().get("recent"), "…but is for customer 1")
    with tenant.use(t1):
        tp1 = phone.topic()
    with tenant.use(t2):
        tp2 = phone.topic()
    ok(tp1 and tp2 and tp1 != tp2 and not phone.topic(), "phone notification topics are per account (the owner's copy has none)")
    ok(c2.post(f"/api/strikes/{sc1}/send", json={}).status_code in (400, 404) and c2.get("/api/strikes").json()["cases"] == [],
       "customer 2 cannot see or act on customer 1's strike case")
    import gzip
    r = c1.get("/api/backup/download")
    bk = json.loads(gzip.decompress(r.content)) if r.status_code == 200 else {}
    ok(bk.get("tenant") == t1, f"customer 1's backup is of its own copy only (tenant {bk.get('tenant')})")
    sw(a1, "keywords.use", False)
    ok(c2.get("/api/me").json()["access"]["perms"]["keywords.use"] is True and c1.get("/api/me").json()["access"]["perms"]["keywords.use"] is False,
       "a switch of one account never changes another account")
    sw(a1, "keywords.use", True)
    ok(c1.get("/api/clips/%d" % cid2).status_code == 404 or c1.get("/api/clips/%d" % cid2).json().get("title") != "clip of copy %d" % t2,
       "customer 1 never reads customer 2's clip by its number")
    rows = [r["detail"] for r in owner.get(f"/api/admin/activity?account_id={a1}").json()["rows"]]
    ok(any("Strike responder" in x and "switched off" in x for x in rows), "the Super Admin's switch changes are in customer 1's activity log")

    for f_ in ("acl.py", "main.py", "scheduler.py", "quota.py", "phone.py", "reminder.py", "learn.py", "strikes.py", "viral.py", "owntopic.py"):
        ok(b"\x08" not in open(os.path.join(WT, "app", f_), "rb").read(), f"no backspace bytes in app/{f_}")
    ok(b"\x08" not in html.encode("utf-8"), "no backspace bytes in the page")

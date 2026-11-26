"""Weekly report: Monday 9 AM IST, one short report per account by its own Gmail and its phone app — views this
week vs last, new subscribers, top 3 videos, published this week, the next 7 days, what needs them. Once per week (also
after a restart), nothing for a week without data, the account's own switch and the Super Admin's switch."""
import base64, email, email.header, json, os, sys, tempfile
from datetime import datetime, timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = r"D:\integ-final"
sys.path.insert(0, WT)
from fastapi.testclient import TestClient
from app import main, acl, tenant, auth, telegram, jobs, gmail, phone, weekly, scheduler
from app.db import SessionLocal, Destination, ChannelStat, VideoStat, Clip, Project, set_setting, get_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
told = []
telegram.notify = lambda text, *a, **k: told.append((tenant.current(), text))
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
mails, pushes = [], []


class Svc:  # Gmail, faked: no network
    def users(self): return self
    def messages(self): return self
    def send(self, userId, body):
        mails.append((tenant.current(), email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))))
        return type("X", (), {"execute": lambda s: {}})()


class R:
    status_code = 200


gmail._service = lambda: Svc()
phone.httpx.post = lambda url, content=None, **k: pushes.append((tenant.current(), json.loads(content))) or R()  # ntfy, faked
MON = datetime(2026, 11, 30, 3, 45)  # Monday 30 Nov 2026, 9:15 AM IST (naive UTC, like the app)
NOW = [MON]
weekly.utcnow = gmail.utcnow = lambda: NOW[0]  # the report and the mail count both see the test's Monday
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
body_of = lambda m: m.get_payload(decode=True).decode()
subject = lambda m: str(email.header.make_header(email.header.decode_header(m["subject"])))

# ---------- the switch and its addresses
ok(len(acl.PERMS) == 61 and acl.DEFAULT["report.weekly"] is True, "one new Super Admin switch 'report.weekly', on by default")
routes = {(m, p): r for m, p, r, _t in acl.ROUTES}
ok(routes.get(("GET", "/api/settings/weekly-report")) == acl.SIGNED and routes.get(("POST", "/api/settings/weekly-report")) == "report.weekly",
   "its addresses are in acl.ROUTES (read: signed in; change: the switch)")
html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
ok("'report.weekly':'.card:has(#wr-on)'" in html and 'id="wr-on"' in html and "Weekly report" in html, "My channels has the Weekly report card, hidden when the switch is off")

# ---------- when it is due
ok(weekly.week_due(MON) == "2026-W49" and weekly.week_due(datetime(2026, 11, 30, 3, 29)) is None
   and weekly.week_due(datetime(2026, 11, 29, 6, 0)) is None and weekly.week_due(datetime(2026, 12, 1, 3, 45)) is None,
   "due on Monday from 9:00 AM IST only (not 8:59, not Sunday, not Tuesday)")

with mk() as owner, mk() as c1, mk() as c2, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})

    def customer(client, email_, name):
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email_, "password": PW, "accept_terms": True})
        aid = next(u for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email_)["id"]
        tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        client.post("/api/login", json={"username": email_, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
        return aid, tid

    a1, t1 = customer(c1, "asha@example.com", "Asha")
    a2, t2 = customer(c2, "ravi@example.com", "Ravi")
    with tenant.use(t1):  # Asha: two weeks of numbers, videos, clips; Gmail and the phone app set up
        set_setting("gmail_email", "asha@gmail.com")
        phone.setup()
        with SessionLocal() as s:
            dst = Destination(channel_id="UC-asha", name="Asha main", token="x"); s.add(dst); s.commit()
            for days, views, subs in ((15, 1000, 10), (7.1, 1500, 12), (0.2, 2300, 20)):
                s.add(ChannelStat(destination_id=dst.id, at=MON - timedelta(days=days), views=views, subscribers=subs, videos=4))
            for vid, title, views in (("vidA", "Samosa street", 900), ("vidB", "Chai stall", 400), ("vidC", "Jalebi", 700),
                                      ("vidD", "Lassi", 50), ("vidP", "Private one", 5000)):
                s.add(VideoStat(video_id=vid, destination_id=dst.id, title=title, views=views, privacy="private" if vid == "vidP" else "public"))
            p = Project(title="src"); s.add(p); s.commit()
            for i, (st, when) in enumerate((("uploaded", -2), ("uploaded", -5), ("uploaded", -9), ("queued", 1), ("queued", 3),
                                            ("uploaded", 6), ("queued", 9), ("queued", 16))):
                s.add(Clip(project_id=p.id, status=st, title=f"Clip {i}", youtube_id=f"yt{i}" if st == "uploaded" else None,
                           publish_at=MON + timedelta(days=when), destination_id=dst.id))
            s.commit()
        set_setting("gmail_owner:" + MON.strftime("%Y-%m-%d"), "40")  # today's alert mails are used up: the report has its own count

    # ---------- the account's own switch (My channels)
    r = c1.get("/api/settings/weekly-report").json()
    ok(r["on"] is True and r["allowed"] is True and r["mail"] is True and r["phone"] is True, f"default ON; Gmail and phone app found ({r['on']}, {r['mail']}, {r['phone']})")
    ok("Views this week: 800 (last week 500, +60%)" in r["preview"], f"the card shows what it would say now ({r['preview'][:80]!r})")
    ok(c1.post("/api/settings/weekly-report", json={"on": False}).json()["on"] is False, "switched off by the customer")
    with tenant.use(t1):
        ok(weekly.weekly_due(MON) is None and get_setting(weekly.WEEK_KEY) is None and not mails, "off: nothing sent, the week is not used up")
    c1.post("/api/settings/weekly-report", json={"on": True})

    # ---------- the report
    told.clear()
    real_health = main.health
    main.health = lambda: {"items": [{"level": "warn", "text": "small thing"}]}  # nothing at level "bad"
    with tenant.use(t1):
        out = weekly.weekly_due(MON)
    main.health = real_health
    ok(out and out["mail"] is True and out["phone"] is True and out["skipped"] is False, f"Monday 9:15 AM IST: sent by mail and phone ({out})")
    m = [x for t, x in mails if t == t1]
    txt = body_of(m[0]) if m else ""
    ok(len(m) == 1 and m[0]["to"] == "asha@gmail.com" and subject(m[0]).startswith("Raji Studio: Your week on YouTube"), "one mail, to the account's own Gmail")
    ok("Views this week: 800 (last week 500, +60%)" in txt and "New subscribers: +8" in txt, "views this week vs last week, new subscribers")
    ok("1. Samosa street — 900 views — https://youtu.be/vidA" in txt and "2. Jalebi — 700 views" in txt and "3. Chai stall — 400 views" in txt
       and "Lassi" not in txt and "Private one" not in txt, "top 3 videos with views and link (public only)")
    ok("Published this week: 2" in txt and "Next 7 days: 3 scheduled" in txt, "published this week (2) and the next 7 days (3)")
    ok("Nothing needs you." in txt and "Switch it off in My channels → Weekly report" in txt, "nothing needs them; the mail says how to switch it off")
    pp = [x for t, x in pushes if t == t1]
    ok(len(pp) == 1 and pp[0]["title"].startswith("Your week on YouTube") and "Views this week: 800" in pp[0]["message"], "one phone message (ntfy) with the same words")
    ok(not told, "nothing went to Telegram / the alert mails")

    # ---------- once per week, also after a restart
    with tenant.use(t1):
        again = weekly.weekly_due(MON + timedelta(minutes=15))
        restart = weekly.weekly_due(MON + timedelta(hours=3))
    ok(again is None and restart is None and len(mails) == 1 and len(pushes) == 1, "the same Monday again (and after a restart): nothing more")
    with tenant.use(t1):
        ok(json.loads(get_setting(weekly.LAST_KEY))["week"] == "2026-W49", "the last report is remembered for the card")
        ok(weekly._claim("2026-W49") is False and weekly._claim("2026-W50") is True and weekly._claim("2026-W50") is False, "the week is claimed once (atomic)")
        set_setting(weekly.WEEK_KEY, "2026-W49")

    # ---------- needs you, next week's top videos by views gained
    with tenant.use(t1):
        with SessionLocal() as s:
            s.get(VideoStat, "vidD").views = 650  # +600 this week
            s.get(VideoStat, "vidA").views = 950  # +50
            s.commit()
    main.health = lambda: {"items": [{"level": "bad", "text": "Your channel Asha main needs to be connected again."},
                                     {"level": "warn", "text": "small thing"}]}
    with tenant.use(t1):
        out = weekly.weekly_due(MON + timedelta(days=7))
    main.health = real_health
    txt = body_of(mails[-1][1])
    ok(out and len(mails) == 2 and "Top videos this week:" in txt and txt.index("Lassi — 600 views this week") < txt.index("Samosa street — 50 views this week"),
       "next Monday: one more report; top videos by views gained since the last report")
    ok("Needs you:\n- Your channel Asha main needs to be connected again." in txt and "small thing" not in txt, "what needs them (bad problems only)")

    # ---------- a week with no data: nothing sent
    with tenant.use(t2):
        set_setting("gmail_email", "ravi@gmail.com")
        out = weekly.weekly_due(MON)
    ok(out and out["skipped"] is True and not [1 for t, _ in mails if t == t2], "an account with no numbers, nothing published or scheduled: nothing sent")
    ok(c2.get("/api/settings/weekly-report").json()["preview"] == "", "…and the card says there is nothing yet")

    # ---------- the timed job: per account, the Super Admin's switch
    owner.post(f"/api/admin/users/{a1}/access", json={"perms": {"report.weekly": False}})
    r = c1.post("/api/settings/weekly-report", json={"on": True})
    ok(r.status_code == 403, f"switch off for Asha: her own switch cannot be changed ({r.status_code})")

    class FakeSch:
        def __init__(self, **k): self.jobs = {}
        def add_job(self, fn, *a, **k): self.jobs[k.get("id") or getattr(fn, "__name__", "")] = (fn, a, k)
        def get_job(self, key): return self.jobs.get(key)
        def start(self): pass

    real_bs = scheduler.BackgroundScheduler
    scheduler.BackgroundScheduler = FakeSch
    import app.watchdog as wd
    real_sg = wd.start_guard
    wd.start_guard = lambda sch: None
    try:
        sch = scheduler.start()
    finally:
        scheduler.BackgroundScheduler = real_bs
        wd.start_guard = real_sg
    job = sch.jobs.get("weekly_due")
    ok(job and job[1][:1] == ("interval",) and job[2].get("minutes") == 15, "the weekly job: checked every 15 minutes (acts on Monday from 9 AM IST)")
    NOW[0] = MON + timedelta(days=14)
    n_before = len(mails)
    with tenant.use(t1):
        set_setting("weekly_report_week", "2026-W50")
    job[0]()
    with tenant.use(t1):
        ok(get_setting(weekly.WEEK_KEY) == "2026-W50" and len([1 for t, _ in mails if t == t1]) == 2, "Super Admin switch off: skipped for that account")
    with tenant.use(t2):
        ok(get_setting(weekly.WEEK_KEY) == "2026-W51", "…while the other account's week is handled by the same round")
    owner.post(f"/api/admin/users/{a1}/access", json={"perms": {"report.weekly": None}})
    job[0]()
    ok(len([1 for t, _ in mails if t == t1]) == 3, "switched on again: the report goes out in the next round")
    pj = [k for k in sch.jobs if k == "payments_check"]
    ok(pj and sch.jobs["payments_check"][2].get("minutes") == 10, "the payments check runs every 10 minutes (owner's copy)")

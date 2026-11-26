"""Growth pack: the public home page and the free trial, Instagram Reels / Facebook cross-posting when a Short is
public, Hindi / Hinglish titles per channel, and the thumbnail A/B test. No real network: YouTube, Meta and the
keyword suggestions are fakes."""
import json, os, re, socket, sys, tempfile
from datetime import timedelta
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
for k in ("META_APP_ID", "META_APP_SECRET", "THUMB_SWAPS_PER_DAY"):
    os.environ.pop(k, None)
WT = os.environ.get("RS_WT", r"D:\growth-pack")
sys.path.insert(0, WT)

_connect = socket.socket.connect
def _no_net(self, addr):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise OSError("no network in tests: " + str(host))
    return _connect(self, addr)
socket.socket.connect = _no_net

from fastapi.testclient import TestClient
from app import main, acl, tenant, auth, accounts, payments, trial, meta, metadata, keywords, growth, archive, jobs, youtube, telegram, project_thumbs, backup
from app.db import SessionLocal, Destination, Project, Clip, VideoStat, Worker, set_setting, get_setting, utcnow
from app.crypto import encrypt
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
told = []
telegram.notify = lambda *a, **k: told.append(a[0] if a else "")
auth.rate_limited = lambda ip, kind="owner": False
submitted = []
jobs.executor.submit = lambda fn, *a, **k: submitted.append((getattr(fn, "__name__", "?"), a))
asked = []
def fake_suggest(q, hl, gl):
    asked.append((q, hl))
    base = [q, q + " recipe", q + " street style"]
    if hl == "hi":
        base += [q + " in hindi", q + " kaise banaye"]
    return base
keywords._get = fake_suggest
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
home = open(os.path.join(WT, "static", "home.html"), encoding="utf-8").read()
reg = open(os.path.join(WT, "static", "register.html"), encoding="utf-8").read()
now = utcnow()

with mk() as owner, mk() as anon, mk() as c1, mk() as c2, mk() as c3:
    # =================================================================== 1. home page + free trial
    r = anon.get("/", follow_redirects=False)
    ok(r.status_code == 200 and "Start free trial" in r.text and 'href="/login"' in r.text and "Raji Studio" in r.text,
       "a visitor who is not signed in gets the home page at / (not a redirect to the sign-in form)")
    ok(r.headers.get("content-security-policy") == main.CSP, "the home page keeps the app's CSP")
    ok(anon.get("/welcome").status_code == 200 and anon.get("/index.html", follow_redirects=False).status_code == 200,
       "/welcome and /index.html show the home page to visitors too")
    ok("<script src" not in home and not re.findall(r'(?:src|href)="https?://', home) and "fonts.googleapis" not in home,
       "the home page loads nothing from outside (no external script, style or font)")
    ok('name="viewport"' in home and "@media(max-width:420px)" in home and "auto-fit,minmax(240px" in home, "the home page is made for phones (375 px)")
    ok(all(w in home for w in ("Creative Commons", "credit", "own long video", "Automatic scheduling", "Strike protection", "Helper PC", "phone")),
       "the home page says what it does in plain words")
    ok(len(home.encode()) < 20000, f"the home page is light ({len(home.encode())} bytes)")
    p = anon.get("/api/public/plans").json()
    ok(p == {"plans": [], "trial_days": 0, "instant": False}, f"no plans yet: nothing for sale, no trial ({p})")

    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    ok('id="p-users"' in owner.get("/").text, "signed in, / is the dashboard")
    owner.post("/api/admin/plans", json={"name": "Free", "limits": {"channels": 1}, "perms": {}})
    owner.post("/api/admin/plans", json={"name": "Pro", "limits": {"channels": 3}, "perms": {}, "price": 499, "period": "monthly",
                                         "description": "For growing channels"})
    owner.post("/api/admin/acl/default", json={"perms": {}, "limits": {}, "new_plan": "Free", "apply": False})
    p = anon.get("/api/public/plans")
    pl = p.json()
    ok([x["name"] for x in pl["plans"]] == ["Pro"] and set(pl["plans"][0]) == {"name", "price", "period", "period_text", "description"}
       and pl["plans"][0]["price"] == 499, f"public plans: only plans for sale, prices only ({pl['plans']})")
    ok("public" in p.headers.get("cache-control", "") and "includes" not in p.text and "limits" not in p.text and "online" not in p.text, "nothing but prices is shown")
    sg = owner.get("/api/admin/signup").json()
    ok(sg["approve"] == "hand" and sg["trial_on"] is True and sg["trial_days"] == 7 and sg["plan"] == "Free" and not sg["active"] and sg["note"],
       "defaults: approve by hand (as before), 7 days, on the plan new accounts start on — no trial while that is the free plan anyway")
    ok(pl["trial_days"] == 0, "the home page offers no trial that would give nothing")
    sg = owner.post("/api/admin/signup", json={"trial_plan": "Pro"}).json()
    ok(sg["active"] and sg["plan"] == "Pro" and anon.get("/api/public/plans").json()["trial_days"] == 7, "trial on the plan Pro: 7 days shown on the home page")
    ok(owner.post("/api/admin/signup", json={"trial_plan": "Nope"}).status_code == 400 and owner.post("/api/admin/signup", json={"trial_days": 0}).status_code == 422
       and owner.post("/api/admin/signup", json={"approve": "maybe"}).status_code == 422, "bad trial settings refused")

    r = anon.post("/api/register", json={"name": "Asha", "brand": "Asha Studio", "email": "asha@example.com", "password": PW, "accept_terms": True}).json()
    ok(r == {"ok": True, "status": "pending"}, "approve by hand stays the default: a new account waits")
    ok(c1.post("/api/login", json={"username": "asha@example.com", "password": PW}).status_code == 403, "and cannot sign in yet")
    a1 = accounts.by_email("asha@example.com").id
    ok(accounts.get(a1).plan_expires is None and not accounts.get(a1).trial, "no trial clock before approval")
    owner.post(f"/api/admin/users/{a1}/approve")
    a = accounts.get(a1)
    ok(a.trial and a.paid_plan == "Pro" and acl.effective(a1)["plan"] == "Pro" and a.channel_limit == 3 and a.limit_before_pay == 1
       and timedelta(days=6, hours=23) < a.plan_expires - utcnow() <= timedelta(days=7), "approval starts the trial: plan Pro for 7 days, the paid-plan end date")
    c1.post("/api/login", json={"username": "asha@example.com", "password": PW})
    me = c1.get("/api/me").json()
    ok(me["trial"] and me["trial"]["days_left"] == 7 and me["trial"]["plan"] == "Pro", "the customer's dashboard knows the trial (banner)")
    ok(c1.get("/api/admin/signup").status_code == 403 and c1.post(f"/api/admin/users/{a1}/trial", json={"action": "end"}).status_code == 403,
       "customers cannot change trials")
    acc_panel = owner.get(f"/api/admin/users/{a1}/access").json()
    ok(acc_panel["trial"]["days_left"] == 7 and acc_panel["account"]["trial"] is True, "the Access panel shows the trial")
    before = accounts.get(a1).plan_expires
    r = owner.post(f"/api/admin/users/{a1}/trial", json={"action": "extend", "days": 3})
    ok(r.status_code == 200 and accounts.get(a1).plan_expires - before == timedelta(days=3), "Super Admin extends the trial by 3 days")
    ok(owner.post(f"/api/admin/users/{a1}/trial", json={"action": "extend", "days": 0}).status_code == 422, "0 days refused")

    def set_end(aid, delta):
        with accounts.CoreSession() as s:
            x = s.get(accounts.Account, aid); x.plan_expires, x.expiry_warned = utcnow() + delta, None; s.commit()
    set_end(a1, timedelta(days=2, hours=12))
    told.clear(); payments.expire_due()
    ok(not told and accounts.get(a1).expiry_warned is None, "2.5 days before the end: no reminder yet (a paid plan would be warned at 3)")
    set_end(a1, timedelta(days=1, hours=20))
    told.clear(); r = payments.expire_due()
    ok(r["warned"] == 1 and any("free trial ends on" in t for t in told), "2 days before the end: the reminder")
    told.clear(); payments.expire_due()
    ok(not told, "the reminder is sent once")
    set_end(a1, -timedelta(minutes=1))
    told.clear(); r = payments.expire_due()
    a = accounts.get(a1)
    ok(r["ended"] == 1 and acl.effective(a1)["plan"] == "Free" and not a.trial and a.plan_expires is None and a.channel_limit == 1,
       "the trial ends with the paid-plan machinery: back to the free plan, 1 channel")
    ok(any("free trial has ended" in t for t in told) and any("free trial" in t and "ended" in t for t in told), "customer and owner are told in plain words")

    # end now (Super Admin) + a payment ends a trial
    anon.post("/api/register", json={"name": "Ravi", "brand": "Ravi Studio", "email": "ravi@example.com", "password": PW, "accept_terms": True})
    a2 = accounts.by_email("ravi@example.com").id
    owner.post(f"/api/admin/users/{a2}/approve")
    ok(accounts.get(a2).trial, "second account on its trial")
    r = owner.post(f"/api/admin/users/{a2}/trial", json={"action": "end"})
    ok(r.status_code == 200 and acl.effective(a2)["plan"] == "Free" and not accounts.get(a2).trial, "End the trial now: back to the free plan at once")
    ok(owner.post(f"/api/admin/users/{a2}/trial", json={"action": "end"}).status_code == 400, "ending a trial that is not running: plain 400")

    sg = owner.post("/api/admin/signup", json={"approve": "auto"}).json()
    ok(sg["approve"] == "auto" and anon.get("/api/public/plans").json()["instant"] is True, "Super Admin switches to: start the trial at once")
    r = anon.post("/api/register", json={"name": "Meena", "brand": "Meena Studio", "email": "meena@example.com", "password": PW, "accept_terms": True}).json()
    a3 = accounts.by_email("meena@example.com").id
    ok(r["status"] == "active" and r["trial_days"] == 7 and accounts.get(a3).trial and accounts.get(a3).tenant_id,
       "no approval needed: the account is ready and the trial runs")
    ok(c3.post("/api/login", json={"username": "meena@example.com", "password": PW}).status_code == 200, "and can sign in at once (no card asked anywhere)")
    a = accounts.get(a3)
    out = payments._apply_paid(a, "Pro", "monthly", 49900, "", "by UPI")
    a = accounts.get(a3)
    ok(out["applied"] and not a.trial and a.paid_plan == "Pro" and timedelta(days=29) < a.plan_expires - utcnow() <= timedelta(days=30),
       "paying ends the trial; the paid month starts today (trial days are not added)")
    owner.post("/api/admin/signup", json={"trial_on": False})
    anon.post("/api/register", json={"name": "Kiran", "brand": "Kiran Studio", "email": "kiran@example.com", "password": PW, "accept_terms": True})
    a4 = accounts.by_email("kiran@example.com")
    ok(a4.status == "active" and not a4.trial and a4.plan_expires is None and acl.effective(a4.id)["plan"] == "Free", "trial switched off: a new account simply starts on the free plan")
    owner.post("/api/admin/signup", json={"trial_on": True, "approve": "hand"})
    ok("signup" in backup.PROTECTED, "the sign-up settings are never taken from a backup file")
    ok("j.status==='active'" in reg and "free trial has started (no card needed)" in reg, "the sign-up page says when the account is ready at once")
    ok('id="su-box"' in html and "Approve new accounts by hand" in html and "data-atrx" in html and "data-atre" in html and "trial-bar" in html,
       "dashboard: New accounts card, Extend / End in Access, the customer's trial banner")

    # =================================================================== 2. Instagram + Facebook
    calls, ig = [], {"status": "IN_PROGRESS", "fail": False}

    class R:
        def __init__(self, x): self.x = x
        def json(self): return self.x

    def fget(url, params=None, **k):
        calls.append(("GET", url, dict(params or {})))
        if url.endswith("/oauth/access_token"):
            return R({"access_token": "LONGUSERTOKEN"})
        if url.endswith("/me/accounts"):
            return R({"data": [{"id": "PAGE2", "name": "Second Page", "access_token": "PAGETOKEN222"},
                               {"id": "PAGE1", "name": "Foody Page", "access_token": "PAGETOKEN111", "instagram_business_account": {"id": "IG1", "username": "foody.fan"}}]})
        if "/CONT" in url:
            return R({"status_code": ig["status"]})
        return R({})

    def fpost(url, data=None, **k):
        calls.append(("POST", url, dict(data or {})))
        if url.endswith("/IG1/media"):
            if ig["fail"]:
                return R({"error": {"message": "Error validating access token: Session has expired"}})
            return R({"id": "CONT%d" % len(calls)})
        if url.endswith("/media_publish"):
            return R({"id": "IGPOST%d" % len(calls)})
        if url.endswith("/videos"):
            return R({"id": "FBVID%d" % len(calls)})
        return R({})

    meta.httpx.get, meta.httpx.post = fget, fpost
    slept, real_sleep = [], meta.time.sleep
    meta.time.sleep = lambda sec: slept.append(sec)  # (the time module is shared: put back after this part)
    r = owner.post("/api/meta/setup", json={"token": "USERTOKEN-abcdefghijklmnop"}).json()
    ok(r["connected"] and r["page"] == "Foody Page" and r["instagram"] == "foody.fan" and not r["long_lived"] and not r["app_ready"],
       "connect: the Page with Instagram is picked; without the Meta app the login is short (shown)")
    ok(len(r["pages"]) == 2 and "PAGETOKEN" not in json.dumps(r) and "PAGETOKEN" not in (get_setting("meta_pages") or "") + (get_setting("meta_token") or ""),
       "every Page is remembered, tokens only encrypted and never sent to the browser")
    ok(owner.post("/api/meta/app", json={"app_id": "abc", "app_secret": "x" * 32}).status_code == 400
       and owner.post("/api/meta/app", json={"app_id": "1234567"}).status_code == 400, "a wrong App ID / a missing secret is refused")
    SECRET = "s3cr3tAppSecretValue0123456789ab"
    r = owner.post("/api/meta/app", json={"app_id": "1234567", "app_secret": SECRET}).json()
    ok(r["app_ready"] and r["app_id"] == "1234567" and SECRET not in json.dumps(r) and SECRET not in (get_setting("meta_app") or ""),
       "the account's own Meta app is saved; the secret never comes back and is stored encrypted")
    r = owner.post("/api/meta/setup", json={"token": "USERTOKEN-abcdefghijklmnop"}).json()
    ex = [c for c in calls if c[1].endswith("/oauth/access_token")]
    ok(r["long_lived"] and ex and ex[-1][2]["client_id"] == "1234567" and ex[-1][2]["client_secret"] == SECRET
       and [c for c in calls if c[1].endswith("/me/accounts")][-1][2]["access_token"] == "LONGUSERTOKEN", "with the app: the token is made long-lived first")
    r = owner.post("/api/meta/page", json={"page_id": "PAGE2"}).json()
    ok(r["page"] == "Second Page" and r["instagram"] is None and r["instagram_on"] is False, "another Page can be chosen (no Instagram there: Reels off)")
    ok(owner.post("/api/meta/page", json={"page_id": "PAGE9"}).status_code == 400, "an unknown Page is refused")
    owner.post("/api/meta/page", json={"page_id": "PAGE1"})
    r = owner.post("/api/meta/targets", json={"instagram": True, "facebook": True}).json()
    ok(r["instagram_on"] and r["facebook_on"], "Instagram and Facebook both on")

    with SessionLocal() as s:
        dest = Destination(channel_id="UC-owner", name="Owner channel", token="x"); s.add(dest); s.commit(); did = dest.id
        pc = Project(url="u", video_id="abcdefghijk", title="Samosa Recipe Street Style", channel="Cook", channel_url="https://youtube.com/@cook",
                     mode="cc", duration=60, tags=["samosa", "street food"], description="Full recipe of samosa #samosa"); s.add(pc)
        pp = Project(url="u", video_id="bbbbbbbbbbb", title="Perm", channel="Cook", mode="permission", duration=60); s.add(pp); s.commit()
        vid = Path(d) / "o.mp4"; vid.write_bytes(b"video-bytes")
        cs = Clip(project_id=pc.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="YS1", title="Samosa", description="#shorts\n\nCredit: Cook",
                  output_path=str(vid), destination_id=did, attempted_at=now - timedelta(hours=1), publish_at=now + timedelta(hours=5))
        cperm = Clip(project_id=pp.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="YP1", title="Perm", output_path=str(vid),
                     destination_id=did, attempted_at=now - timedelta(hours=1))
        s.add_all([cs, cperm]); s.commit(); cid, cpid, pcid = cs.id, cperm.id, pc.id
    r = meta.crosspost(cid)
    ok(r.get("skipped") == "not public on YouTube yet" and not any("/IG1/media" in c[1] for c in calls), "a scheduled Short is not posted before it is public on YouTube")
    ok(owner.post(f"/api/clips/{cid}/crosspost").status_code == 409, "the button says so too")
    with SessionLocal() as s:
        ok(archive.crosspost_pending(s.get(Clip, cid), s.get(Project, pcid), True, utcnow() + timedelta(days=1)),
           "its video is kept on the server until it has been posted (counted from when it goes public)")
    r = meta.post_due()
    ok(r == {"published": 0, "started": 0, "asked_pc": 0}, "nothing to do while it is private")
    with SessionLocal() as s:
        s.get(Clip, cid).publish_at = utcnow() - timedelta(minutes=1); s.commit()
    r = meta.post_due()
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        res, done = dict(c.meta_result or {}), c.meta_done
        pending_keep = archive.crosspost_pending(c, s.get(Project, pcid), True, utcnow())
    ok(r["started"] == 1 and res.get("ig_container") and res.get("facebook") and not done, "when it is public: Instagram container made, Facebook posted, Reel still processing")
    ok(pending_keep, "the video stays on the server while Instagram has not fetched it yet")
    ok(meta.status()["pending"] == 1, "the card shows one Reel being processed")
    cap = [c for c in calls if c[1].endswith("/IG1/media")][-1][2]
    ok(cap["media_type"] == "REELS" and "Samosa" in cap["caption"] and "/media/%d/" % cid in cap["video_url"], "Reel with title + credit from the signed link")
    ok(not meta.post_due()["published"], "still processing: nothing published yet")
    ig["status"] = "FINISHED"
    r = meta.post_due()
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        ok(r["published"] == 1 and c.meta_done and c.meta_result.get("instagram"), "Instagram ready: the Reel is published by the 10-minute job")
        ok(not archive.crosspost_pending(c, s.get(Project, pcid), True, utcnow()), "then the server's copy may go to the PC as usual")
    ok(any("Also posted to instagram + facebook" in t for t in told), "the owner is told")
    ok(meta.crosspost(cid).get("skipped") and meta.crosspost(cpid).get("skipped"), "never twice; permission-mode clips are never cross-posted")

    url = cap["video_url"]
    ok(owner.get("/" + url.split("://", 1)[1].split("/", 1)[1]).content == b"video-bytes", "the signed link serves the video")
    with SessionLocal() as s:
        w = Worker(name="Laptop", token_hash="h1", approved=True, last_seen=utcnow()); s.add(w); s.commit()
        gone = Clip(project_id=pcid, start=30, end=60, aspect="9:16", status="uploaded", youtube_id="YS2", title="Samosa 2", output_path=str(Path(d) / "gone.mp4"),
                    destination_id=did, attempted_at=now - timedelta(hours=3), archived_at=now - timedelta(hours=1), archived_sha="abc", archived_by=w.id)
        s.add(gone); s.commit(); gid = gone.id
    n = len(calls)
    r = meta.post_due()
    with SessionLocal() as s:
        g = s.get(Clip, gid)
        ok(r["asked_pc"] == 1 and g.restore_wanted and "Instagram" in (g.archive_note or "") and len(calls) == n,
           "the video was already moved to the PC: it is asked back with the get-back job, nothing posted yet")
    Path(d, "gone.mp4").write_bytes(b"back")
    r = meta.post_due()
    ok(r["started"] == 1, "when it is back on the server it is posted")

    ig["fail"], ig["status"] = True, "IN_PROGRESS"
    with SessionLocal() as s:
        c3c = Clip(project_id=pcid, start=60, end=90, aspect="9:16", status="uploaded", youtube_id="YS3", title="Samosa 3", output_path=str(vid),
                   destination_id=did, attempted_at=now - timedelta(minutes=30)); s.add(c3c); s.commit()
    told.clear(); meta.post_due()
    st = meta.status()
    ok(st["error"] and "Connect Instagram + Facebook again" in st["error"] and any("login stopped working" in t for t in told), "an expired Meta login is shown and told once")
    ig["fail"] = False

    # a customer with "Instagram + Facebook" switched off: nothing runs in their copy
    t1 = accounts.get(a1).tenant_id
    owner.post(f"/api/admin/users/{a1}/access", json={"perms": {"meta.use": False}})
    with tenant.use(t1):
        set_setting("meta_token", encrypt("PAGETOKENX")); set_setting("meta_cfg", json.dumps({"page_id": "PX", "ig_id": "IGX"}))
        n = len(calls)
        ok(meta.post_due() == {"published": 0, "started": 0, "asked_pc": 0} and len(calls) == n, "switch off: the job does nothing for that account")
    ok(c1.post("/api/meta/page", json={"page_id": "PX"}).status_code == 403 and c1.post("/api/meta/app", json={"app_id": "1234567"}).status_code == 403,
       "and the new Meta addresses are refused for it")
    owner.post(f"/api/admin/users/{a1}/access", json={"perms": {"meta.use": None}})
    ok(not slept, "cross-posting never sleeps (it used to wait up to 10 minutes in the single render worker)")
    meta.time.sleep = real_sleep
    src = open(os.path.join(WT, "app", "scheduler.py"), encoding="utf-8").read()
    ok('when_on("meta.use", meta_due)' in src, "the 10-minute job is behind the switch")
    ok("How to connect" in html and "developers.facebook.com" in html and "App settings → Basic" in html and "Graph API Explorer" in html and 'id="mt-appsave"' in html,
       "the card says exactly what to make on developers.facebook.com")

    # =================================================================== 3. Hindi / Hinglish titles
    with SessionLocal() as s:
        p = s.get(Project, pcid)
        c = Clip(project_id=pcid, start=0, end=30, aspect="9:16", status="queued", destination_id=did); s.add(c); s.commit(); qid = c.id
        en = metadata.build(p, c, [])
        ok(en == metadata.build(p, c, [], "en") and en["title"] == "Samosa Recipe Street Style | Best Moment" and "#hindi" not in en["description"]
           and en["description"].startswith("#shorts"), "English (default): exactly as before")
        hg = metadata.build(p, c, [], "hinglish")
        hi = metadata.build(p, c, [], "hi")
    block = "\n".join(["Credit: Cook - https://youtube.com/@cook", "Source: https://youtu.be/abcdefghijk?t=0", metadata.LICENSE_LINE["cc"]])
    ok(hg["title"].endswith("| Best Moment Dekho") and hg["description"].splitlines()[0] == "Samosa Recipe Street Style — Cook ki video ka sabse best moment."
       and "#hindi" in hg["description"], "Hinglish: title hook, first line and #hindi")
    ok(hi["title"].endswith("| सबसे बढ़िया पल") and "की वीडियो का सबसे बढ़िया पल।" in hi["description"].splitlines()[0] and "#हिंदी" in hi["description"],
       "Hindi: title hook, first line and hashtag in Hindi")
    ok(all(block in x["description"] and "--- Original description ---" in x["description"] for x in (en, hg, hi)), "credit and licence lines stay unchanged, in English")
    ok(owner.post(f"/api/destinations/{did}/language", json={"language": "urdu"}).status_code == 422, "an unknown language is refused")
    submitted.clear()
    r = owner.post(f"/api/destinations/{did}/language", json={"language": "hinglish"})
    ok(r.status_code == 200 and r.json()["title_lang"] == "hinglish" and any(n == "_rewrite_lang" for n, _a in submitted),
       "My channels → Titles in Hinglish: saved, waiting clips are rewritten in the background")
    asked.clear()
    m = metadata.propose(qid)
    ok(any(hl == "hi" and q.endswith("in hindi") for q, hl in asked) and any(hl == "hi" for q, hl in asked), "the keyword helper asks YouTube's suggestions with hl=hi")
    ok(m["kw"].get("lang") == "hinglish" and "#hindi" in m["description"] and block in m["description"], f"propose writes Hinglish ({m['title']})")
    first = m["description"].splitlines()[0]
    ok("ki video ka sabse best moment" in first or "sabse best moment" in first, f"the hook line is Hinglish ({first})")
    ok(re.search(r"in hindi|kaise banaye|Dekho", m["title"], re.I), f"the title is Hinglish ({m['title']})")
    ok(metadata.propose(qid)["title"] == m["title"] and metadata.propose(qid)["description"] == m["description"], "the same answer every time (templates, no AI)")
    with SessionLocal() as s:
        c = s.get(Clip, qid); c.kw = {"v": 1, "at": utcnow().isoformat() + "Z", "net": "ok", "items": []}; s.commit()
        ok(metadata.needs_refresh(s.get(Clip, qid)), "keywords written in English before the change are refreshed for the new language")
        e = Clip(project_id=pcid, start=0, end=30, aspect="9:16", status="queued", destination_id=did, title="My own title", meta_edited=True); s.add(e); s.commit(); eid = e.id
    main._rewrite_lang(did)
    with SessionLocal() as s:
        ok("Dekho" in s.get(Clip, qid).title or "hindi" in s.get(Clip, qid).title.lower(), f"the waiting clip was rewritten ({s.get(Clip, qid).title})")
        ok(s.get(Clip, eid).title == "My own title", "a clip edited by hand is never touched")
    ok(len(acl.PERMS) == 61 and acl.DEFAULT["channels.language"] is True, "one new switch: Title language (on by default)")
    owner.post(f"/api/admin/users/{a1}/access", json={"perms": {"channels.language": False}})
    ok(c1.post("/api/destinations/1/language", json={"language": "hi"}).status_code == 403, "switched off: the customer cannot change it")
    owner.post(f"/api/admin/users/{a1}/access", json={"perms": {"channels.language": None}})
    ok("'channels.language':'[data-tlang]'" in html and "Titles in Hinglish" in html, "the choice is on the channel and hidden when switched off")

    # =================================================================== 4. thumbnail A/B
    pushed = []
    project_thumbs.remake_thumbnail = lambda s, c: True
    youtube.push_thumbnail = lambda c: (pushed.append((c.id, c.thumb_variant)), True)[1]
    ctr = {}
    growth._ctr = lambda dest, vid, a, b: None  # no Analytics numbers: views per day decide
    with SessionLocal() as s:
        for i in range(6):
            s.add(VideoStat(video_id=f"V{i}", destination_id=did, privacy="public", published_at=now - timedelta(days=5), views=5000))
        for vid_ in ("LOW1", "LOW2", "LOW3"):
            s.add(VideoStat(video_id=vid_, destination_id=did, privacy="public", published_at=now - timedelta(days=3), views=30))
        ids = []
        for vid_ in ("LOW1", "LOW2", "LOW3"):
            c = Clip(project_id=pcid, start=0, end=30, aspect="9:16", status="uploaded", youtube_id=vid_, title="Low " + vid_, destination_id=did,
                     attempted_at=now - timedelta(days=3), output_path=str(vid)); s.add(c); s.commit(); ids.append(c.id)
    started = growth.swap_weak_thumbnails()
    with SessionLocal() as s:
        st = {i: s.get(Clip, i).thumb_test for i in ids}
    ok(len(started) == 2 and all(st[i]["state"] == "a" for i in started) and not pushed, "weak videos start a test (A measured first, nothing uploaded), at most 2 a run")
    growth.swap_weak_thumbnails()
    ok(not pushed, "before 48 hours nothing changes")

    def age(cid_, key, hours=49):
        with SessionLocal() as s:
            c = s.get(Clip, cid_); t = dict(c.thumb_test); t[key] = (utcnow() - timedelta(hours=hours)).isoformat() + "Z"; c.thumb_test = t; s.commit()
    def views(v, n):
        with SessionLocal() as s:
            s.get(VideoStat, v).views = n; s.commit()
    i1, i2 = started
    for i in (i1, i2):
        age(i, "a_at")
    views("LOW1", 50); views("LOW2", 70)  # A windows: +20 and +40 views
    r = growth.thumb_tests()
    with SessionLocal() as s:
        ok(r["b_started"] == 2 and all(s.get(Clip, i).thumb_test["state"] == "b" and s.get(Clip, i).thumb_variant == 1 for i in (i1, i2))
           and sorted(pushed) == sorted([(i1, 1), (i2, 1)]), "after 48 h the second thumbnail (B) goes on")
    for i in (i1, i2):
        age(i, "b_at"); age(i, "a_at", 98)
    views("LOW1", 110); views("LOW2", 80)  # B windows: +60 (better) and +10 (worse)
    set_setting("thumb_swaps:" + utcnow().strftime("%Y-%m-%d"), "0")  # (a new day: the changes of the day before do not count)
    pushed.clear(); told.clear()
    r = growth.thumb_tests()
    with SessionLocal() as s:
        t1_, t2_ = s.get(Clip, i1).thumb_test, s.get(Clip, i2).thumb_test
        ok(t1_["winner"] == "B" and t1_["by"] == "views" and s.get(Clip, i1).thumb_variant == 1, f"B got more views a day: B stays ({growth.describe(t1_)})")
        ok(t2_["winner"] == "A" and s.get(Clip, i2).thumb_variant == 0 and pushed == [(i2, 0)], f"A was better: A's thumbnail is put back ({growth.describe(t2_)})")
    ok(any("Thumbnail test" in t for t in told), "the owner is told which one won")
    card = owner.get(f"/api/clips/{i1}").json()
    ok("B won" in (card.get("thumb_test_line") or "") and "views a day" in card["thumb_test_line"], f"the clip shows which one won: {card.get('thumb_test_line')}")
    summ = owner.get("/api/thumb-tests").json()
    ok(summ["b_won"] == 1 and summ["a_won"] == 1 and summ["recent"], f"Analytics: tests and winners ({summ['running']} running)")

    # the third: click-through decides when Analytics has enough impressions; the daily cap holds
    growth.swap_weak_thumbnails()
    with SessionLocal() as s:
        i3 = next(i for i in ids if i not in (i1, i2))
        ok(s.get(Clip, i3).thumb_test["state"] == "a", "the third weak video starts its test")
    age(i3, "a_at")
    set_setting("thumb_swaps:" + utcnow().strftime("%Y-%m-%d"), str(growth.SWAPS_PER_DAY))
    pushed.clear(); growth.thumb_tests()
    with SessionLocal() as s:
        ok(s.get(Clip, i3).thumb_test["state"] == "a" and not pushed, "at most N thumbnail changes a day: the next one waits")
    set_setting("thumb_swaps:" + utcnow().strftime("%Y-%m-%d"), "0")
    growth.thumb_tests()
    age(i3, "b_at"); age(i3, "a_at", 98)
    views("LOW3", 1000)  # B got far more views, but click-through says A
    growth._ctr = lambda dest, vid, a, b: (800, 6.5) if utcnow() - b > timedelta(hours=10) else (800, 3.1)  # A window ended 49 h ago
    pushed.clear(); growth.thumb_tests()
    with SessionLocal() as s:
        t3 = s.get(Clip, i3).thumb_test
    ok(t3["by"] == "ctr" and t3["winner"] == "A" and pushed == [(i3, 0)], f"click-through from YouTube Analytics decides when there is enough: {growth.describe(t3)}")
    import app.quota as quota
    real_room = quota.shared_room
    quota.shared_room = lambda units: False
    ok(not growth._room(), "on the owner's Google project a customer swaps only with what the owner's uploads leave")
    quota.shared_room = real_room
    src = open(os.path.join(WT, "app", "scheduler.py"), encoding="utf-8").read()
    ok('when_on("clips.thumbnail", lambda: executor.submit(swap_weak_thumbnails))' in src, "the test runs every day behind the Thumbnails switch (on by default)")
    ok('id="an-thumbs"' in html and "/api/thumb-tests" in html and "thumb_test_line" in html, "Analytics and the clip editor show the winner")

    # =================================================================== routes
    for m_, p_ in [("GET", "/welcome"), ("GET", "/api/public/plans"), ("POST", "/api/admin/users/{aid}/trial"), ("GET", "/api/admin/signup"),
                   ("POST", "/api/admin/signup"), ("POST", "/api/meta/page"), ("POST", "/api/meta/targets"), ("POST", "/api/meta/app"),
                   ("POST", "/api/destinations/{did}/language"), ("GET", "/api/thumb-tests")]:
        ok(any(x[0] == m_ and x[1] == p_ for x in acl.ROUTES), f"{m_} {p_} is in acl.ROUTES")
    ok({"/welcome", "/api/public/plans"} <= main.PUBLIC_PATHS, "the home page and its prices need no sign-in")

"""Access control: every switch blocks its addresses for the customer (and only for that customer), the Super Admin
is never blocked, limits, inline checks, /api/me, activity log, direct controls."""
import json, os, sys, tempfile
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, r"D:\integ-final")
import httpx
from fastapi.testclient import TestClient
from app import main, acl, accounts, tenant, auth, youtube, telegram, jobs, growth, competitors, permissions, viral, stats
from app.db import SessionLocal, Destination, Channel, Clip, Project, set_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
submitted = []
jobs.executor.submit = lambda fn, *a, **k: submitted.append((getattr(fn, "__name__", "?"), a))  # nothing heavy runs
CC = youtube.CC_LICENSE
main.fetch_info = lambda url: {"video_id": "abcdefghijk", "title": "Street food", "channel": "Cook", "channel_url": "", "license": CC,
                               "description": "", "tags": [], "duration": 600, "heatmap": []}
main.auto_metadata = lambda cid, extra=None: {}
main.check_uploads = lambda: {}
growth.weekly_report = lambda: None
competitors.trending = lambda category="": []
permissions.refresh_inbox = lambda: []
permissions.find_emails = lambda only=None: 0
viral.refresh = lambda force=False: {}
stats.sync_all = lambda: {}
stats.sync_all_watch_time = lambda max_age_minutes=0: {}


def no_net(*a, **k):
    raise RuntimeError("no network in tests")


httpx.get = httpx.post = no_net
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
    switch = lambda aid, **perms: owner.post(f"/api/admin/users/{aid}/access", json={"perms": {k.replace("__", "."): v for k, v in perms.items()}})
    sw = lambda aid, key, on: owner.post(f"/api/admin/users/{aid}/access", json={"perms": {key: on}})
    lim = lambda aid, **limits: owner.post(f"/api/admin/users/{aid}/access", json={"limits": limits})
    detail = lambda r: (r.json() or {}).get("detail") if r.headers.get("content-type", "").startswith("application/json") else None

    # ---------- defaults: nothing changes for anybody until the Super Admin switches something
    me = c1.get("/api/me").json()
    ok(me["access"] and all(me["access"]["perms"].values()) and len(me["access"]["perms"]) == len(acl.PERMS) == 51,
       f"a new customer has every one of the {len(acl.PERMS)} switches on (today's behaviour)")
    ok(all(v is None for k, v in me["access"]["limits"].items() if k != "channels") and me["access"]["limits"]["channels"] == 1
       and not any(me["access"]["controls"].values()), "…no limits except 1 channel, nothing paused")
    ok("access" not in owner.get("/api/me").json(), "the Super Admin has no switches at all")
    ok(c1.post("/api/channels", json={"kind": "topic", "query": "street food"}).status_code == 200, "by default a customer can add a source")
    ok(c1.get("/api/admin/acl").status_code == 403 and c1.post(f"/api/admin/users/{a1}/access", json={"perms": {}}).status_code == 403
       and c1.get("/api/admin/activity").status_code == 403, "a customer cannot open or change any of this")
    ok(owner.post(f"/api/admin/users/{a1}/access", json={"perms": {"no.such": False}}).status_code == 400
       and owner.post("/api/admin/users/999999/access", json={"perms": {}}).status_code == 404, "unknown switch / unknown account are refused")

    # ---------- every switch blocks every address it guards — for this customer only, at once, and never the Super Admin
    def fill(p):
        p = p.replace("{name}", "weekly-2026-01-04.pdf").replace("{status}", "asked").replace("{rest:path}", "x")
        import re
        return re.sub(r"\{[^}]+\}", "999999", p)

    def call(client, m, p):
        return client.request(m, p, json={}) if m in ("POST", "PUT") else client.request(m, p, follow_redirects=False)

    by = {}
    for m, p, rule, _t in acl.ROUTES:
        if rule in acl.DEFAULT:
            by.setdefault(rule, []).append(("POST" if m == "*" else m, fill(p)))
    ok(owner.post(f"/api/admin/users/{a1}/view").status_code == 200, "(the Super Admin opens customer 1's dashboard for the whole check)")
    bad_block, bad_other, bad_super, bad_on, n = [], [], [], [], 0
    for key, routes in by.items():
        sw(a1, key, False)
        for m, p in routes:
            n += 1
            r = call(c1, m, p)
            if r.status_code != 403 or detail(r) != acl.message(key):
                bad_block.append((key, m, p, r.status_code))
            if call(c2, m, p).status_code == 403:
                bad_other.append((key, m, p))
            if call(owner, m, p).status_code == 403:
                bad_super.append((key, m, p))
        sw(a1, key, True)
        for m, p in routes:
            if call(c1, m, p).status_code == 403:
                bad_on.append((key, m, p))
    ok(not bad_block, f"each of {len(by)} switches, when off, refuses each of its {n} addresses with 403 and the clear message" + (f" — NOT: {bad_block[:6]}" if bad_block else ""))
    ok(not bad_other, "…while another customer is not affected" + (f" — {bad_other[:6]}" if bad_other else ""))
    ok(not bad_super, "…and the Super Admin, working inside that customer's dashboard, is never refused" + (f" — {bad_super[:6]}" if bad_super else ""))
    ok(not bad_on, "switched on again: the same signed-in browser may use it again at once (no new sign-in)" + (f" — {bad_on[:6]}" if bad_on else ""))
    ok(acl.message("sources.add") == "This is switched off for your account: Add sources. Ask the person who gave you access.", "the message says what is off and whom to ask")

    # ---------- switches pressed quickly after each other are all kept
    from concurrent.futures import ThreadPoolExecutor
    keys = ["page.viral", "page.ideas", "clips.discard", "meta.use", "telegram.use", "pexels.use", "sources.run", "clips.voice", "ai.reels", "affiliate.use"]
    cookies = dict(owner.cookies)

    def press(k):
        with mk() as o:
            o.cookies.update(cookies)
            return o.post(f"/api/admin/users/{a2}/access", json={"perms": {k: False}}).status_code

    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(press, keys))
    own = owner.get(f"/api/admin/users/{a2}/access").json()["own_perms"]
    ok(codes == [200] * 10 and own == {k: False for k in keys}, f"10 switches changed at the same moment: all 10 are saved ({len(own)} saved)")
    owner.post(f"/api/admin/users/{a2}/access", json={"perms": {k: None for k in keys}})
    ok(owner.get(f"/api/admin/users/{a2}/access").json()["own_perms"] == {} and all(c2.get("/api/me").json()["access"]["perms"].values()),
       "a switch can be handed back to the plan / default (nothing of the customer's own left)")

    # ---------- things decided inside a route
    sw(a1, "source.reel", False)
    reel = {"kind": "reel", "query": "My reels", "scripts": ["Hello there. This is my first script for a reel."]}
    r = c1.post("/api/channels", json=reel)
    ok(r.status_code == 403 and detail(r) == acl.message("source.reel"), "source type off: a reel source cannot be added")
    ok(c1.post("/api/channels", json={"kind": "topic", "query": "cooking hacks"}).status_code == 200, "…other kinds still can")
    ok(owner.post("/api/channels", json=reel).status_code == 200, "…and the Super Admin (viewing) can add one for them")
    sw(a1, "source.reel", True)
    for kind, body in (("topic", {"kind": "topic", "query": "funny cats"}), ("channel", {"kind": "channel", "query": "@somechannel", "mode": "cc"}),
                       ("trending", {"kind": "trending", "query": "all"})):
        sw(a1, "source." + kind, False)
        r = c1.post("/api/channels", json=body)
        blocked = r.status_code == 403 and detail(r) == acl.message("source." + kind)
        sw(a1, "source." + kind, True)
        ok(blocked and c1.post("/api/channels", json=body).status_code == 200, f"source type “{kind}”: off refuses it, on allows it")
    sw(a1, "ai.reels", False)
    r = c1.post("/api/channels", json={"kind": "reel", "query": "AI reels", "auto_scripts": True})
    ok(r.status_code == 403 and detail(r) == acl.message("ai.reels") and c1.post("/api/channels", json=reel).status_code == 200,
       "AI reels off: no AI-script source, hand-written reels still fine")
    sw(a1, "ai.reels", True)
    owner.post("/api/admin/view/exit")

    # ---------- limits
    with tenant.use(t1):
        have = acl.source_count()
    lim(a1, sources=have)
    r = c1.post("/api/channels", json={"kind": "topic", "query": "one more"})
    ok(r.status_code == 403 and f"can have {have} sources" in detail(r), f"sources limit reached: {detail(r)}")
    lim(a1, sources=have + 1)
    ok(c1.post("/api/channels", json={"kind": "topic", "query": "one more"}).status_code == 200
       and c1.post("/api/channels", json={"kind": "topic", "query": "two more"}).status_code == 403, "limit raised by one: exactly one more fits")
    lim(a1, sources=None)
    ok(c1.post("/api/channels", json={"kind": "topic", "query": "two more"}).status_code == 200, "limit removed: no limit")
    pid = c1.post("/api/projects", json={"url": "https://www.youtube.com/watch?v=abcdefghijk", "mode": "cc"}).json()["id"]
    lim(a1, clips_per_day=1)
    r1 = c1.post(f"/api/projects/{pid}/clips", json={"start": 10, "end": 40})
    r2 = c1.post(f"/api/projects/{pid}/clips", json={"start": 50, "end": 80})
    ok(r1.status_code == 200 and r2.status_code == 403 and "1 clip a day" in detail(r2), f"clips a day: the second clip is refused — {detail(r2)}")
    r = c1.post("/api/quick", json={"url": "https://www.youtube.com/watch?v=abcdefghijk"})
    ok(r.status_code == 403 and "a day" in detail(r), "…also for Quick upload")
    ok(c2.post("/api/projects", json={"url": "https://www.youtube.com/watch?v=abcdefghijk", "mode": "cc"}).status_code == 200, "…another customer is not limited")
    h = c1.get("/api/health").json()["items"]
    ok(any("limit of 1 new clip" in i["text"] for i in h), "the customer's Problems list says the clip limit is reached")
    lim(a1, clips_per_day=0)
    ok(c1.post(f"/api/projects/{pid}/clips", json={"start": 50, "end": 80}).status_code == 200, "0 = no limit")
    # storage
    big = acl.tenant_folder(t1) / "clips" / "big.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"0" * (2 * 1024 * 1024))
    lim(a1, storage_mb=1)
    r = c1.post(f"/api/projects/{pid}/clips", json={"start": 90, "end": 120})
    ok(r.status_code == 403 and "storage is full" in detail(r) and "Nothing was deleted" in detail(r), f"storage full: new clips refused — {detail(r)}")
    r = c1.post("/api/upload-video", files={"file": ("a.mp4", b"x" * 10, "video/mp4")})
    ok(r.status_code == 403 and "storage is full" in detail(r), "…and file uploads")
    ok(big.exists() and any("storage is full" in i["text"] and i["level"] == "bad" for i in c1.get("/api/health").json()["items"]),
       "nothing is deleted automatically; the Problems list explains it")
    walks = []
    real_walk = acl._folder_bytes
    acl._folder_bytes = lambda p: (walks.append(1), real_walk(p))[1]
    for _ in range(5):
        c1.get("/api/health"); c1.post(f"/api/projects/{pid}/clips", json={"start": 90, "end": 120})
    ok(len(walks) == 0, "the folder size is remembered, not measured on every request")
    acc = owner.get(f"/api/admin/users/{a1}/access?fresh=1").json()
    ok(len(walks) == 1 and acc["usage"]["storage_mb"] >= 2 and acc["limits"]["storage_mb"] == 1, f"“Measure again” measures once: {acc['usage']['storage_mb']} MB used of 1")
    acl._folder_bytes = real_walk
    lim(a1, storage_mb=50)
    ok(c1.post(f"/api/projects/{pid}/clips", json={"start": 90, "end": 120}).status_code == 200, "more space given: clips can be made again")
    lim(a1, storage_mb=None)
    # channels limit lives in the same place
    r = owner.post(f"/api/admin/users/{a1}/access", json={"limits": {"channels": 3}}).json()
    ok(r["limits"]["channels"] == 3 and c1.get("/api/me").json()["channel_limit"] == 3, "the channel limit is set in the same panel")

    # ---------- connecting channels / Gmail (browser pages: a readable redirect, not raw JSON)
    sw(a1, "channels.connect", False)
    r = c1.get("/auth/login", follow_redirects=False)
    ok(r.status_code == 302 and "connect_error=" in r.headers["location"] and "switched%20off" in r.headers["location"], "Connect a channel off: sent back with the message")
    youtube.get_setting = lambda k: "st"
    class FakeFlow:
        credentials = type("C", (), {"to_json": lambda self: "{}"})()
        def fetch_token(self, code): pass
    youtube._flow = lambda *a, **k: FakeFlow()
    youtube._email_from = lambda creds: None
    chan = {"id": "UC-new", "name": "Second"}
    youtube.build = lambda *a, **k: type("Y", (), {"channels": lambda self: self, "list": lambda self, **k: self, "execute": lambda self: {"items": [{"id": chan["id"], "snippet": {"title": chan["name"]}}]}})()
    names = lambda c: [x["name"] for x in c.get("/api/destinations").json()]
    r = c1.get("/auth/callback?code=c&state=st", follow_redirects=False)
    ok("connect_error=" in r.headers["location"] and names(c1) == ["Asha channel"], "…a NEW channel cannot be connected even with a Google answer in hand")
    chan.update(id="UC-Asha", name="Asha channel (renewed)")
    r = c1.get("/auth/callback?code=c&state=st", follow_redirects=False)
    ok("connected=" in r.headers["location"] and names(c1) == ["Asha channel (renewed)"], "…renewing the login of a connected channel stays possible")
    with tenant.use(t1):
        with SessionLocal() as s:
            x = s.query(Destination).first(); x.paused, x.paused_reason = True, "The channel's Google login expired."; s.commit()
    r = c1.get("/auth/login", follow_redirects=False)
    ok("switched%20off" not in r.headers.get("location", ""), "…and the Reconnect button works when a login expired")
    with tenant.use(t1):
        with SessionLocal() as s:
            x = s.query(Destination).first(); x.paused, x.paused_reason = False, None; s.commit()
    sw(a1, "channels.connect", True)
    chan.update(id="UC-new", name="Second")
    r = c1.get("/auth/callback?code=c&state=st", follow_redirects=False)
    ok("connected=" in r.headers["location"] and len(names(c1)) == 2, "switch on (and 3 channels allowed): the second channel connects")
    sw(a1, "gmail.connect", False)
    r = c1.get("/auth/gmail", follow_redirects=False)
    ok(r.status_code == 302 and "gmail_error=" in r.headers["location"] and "switched%20off" in r.headers["location"], "Gmail connect off: sent back with the message")
    sw(a1, "gmail.connect", True)

    # ---------- what the dashboard is told
    switch(a1, page__viral=False, sources__add=False)
    owner.post(f"/api/admin/users/{a1}/control", json={"action": "pause_uploads"})
    acc = c1.get("/api/me").json()["access"]
    ok(acc["perms"]["page.viral"] is False and acc["perms"]["sources.add"] is False and acc["perms"]["page.clips"] is True
       and acc["controls"]["uploads_paused"] is True, "/api/me tells the dashboard what to hide (the same browser, no new sign-in)")
    ok(c2.get("/api/me").json()["access"]["perms"]["page.viral"] is True, "…per customer")
    switch(a1, page__viral=True, sources__add=True)
    owner.post(f"/api/admin/users/{a1}/control", json={"action": "resume_uploads"})

    # ---------- direct controls
    ok(c1.get("/api/overview").status_code == 200, "(customer 1 is signed in)")
    owner.post(f"/api/admin/users/{a1}/control", json={"action": "sign_out"})
    ok(c1.get("/api/overview").status_code == 401 and c2.get("/api/overview").status_code == 200, "“Sign out now”: that customer is out at once, nobody else")
    ok(c1.post("/api/login", json={"username": "asha@example.com", "password": PW}).status_code == 200, "…and can sign in again (not blocked)")
    did = next(x["id"] for x in c1.get("/api/destinations").json() if x["name"] == "Second")
    r = owner.delete(f"/api/admin/users/{a1}/channels/{did}")
    ok(r.status_code == 200 and names(c1) == ["Asha channel (renewed)"] and [x["name"] for x in owner.get("/api/destinations").json()] == ["Owner channel"],
       "the Super Admin disconnects one of the customer's channels (only that one, only theirs)")
    ok(owner.delete(f"/api/admin/users/{a1}/channels/{did}").status_code == 404, "…a channel that is gone gives a clear answer")
    ok(owner.post(f"/api/admin/users/{a1}/control", json={"action": "explode"}).status_code == 422, "unknown control refused")

    # ---------- the Access panel's data
    p = owner.get(f"/api/admin/users/{a1}/access").json()
    ok(set(p) >= {"perms", "own_perms", "limits", "controls", "plan", "usage", "activity", "account"} and p["usage"]["channels"] == 1
       and p["usage"]["channel_list"][0]["name"] == "Asha channel (renewed)" and p["usage"]["sources"] >= 5 and p["usage"]["clips_per_day"] >= 3,
       f"the panel shows switches, limits, usage and channels: {dict((k, v) for k, v in p['usage'].items() if k != 'channel_list')}")
    cat = owner.get("/api/admin/acl").json()["catalog"]
    ok([g["id"] for g in cat["groups"]] == ["pages", "channels", "sources", "clips", "uploads", "extras"] and len(cat["perms"]) == 51
       and [x["key"] for x in cat["limits"]] == ["channels", "sources", "clips_per_day", "uploads_per_day", "storage_mb"], "the catalogue: 6 groups, 51 switches, 5 limits")

    # ---------- activity log
    rows = owner.get(f"/api/admin/activity?account_id={a1}&limit=500").json()["rows"]
    text = [(r["actor"], r["detail"]) for r in rows]
    ok(("user", "Signed in") in text and any(a == "user" and d_.startswith("Added a source") for a, d_ in text), "the log has what the customer did (signed in, added a source)")
    ok(("super", "Add sources: switched off") in text and ("super", "Add sources: switched on") in text, "…which switch the Super Admin changed, on and off")
    ok(("super", "Sources: no limit → %d" % have) in text and ("super", "Clips made per day: no limit → 1") in text and ("super", "YouTube channels: 1 → 3") in text, "…limits with the old and the new value")
    ok(("super", "Paused all uploads") in text and ("super", "Resumed uploads") in text and ("super", "Signed the customer out everywhere") in text
       and ("super", "Disconnected the YouTube channel “Second”") in text, "…the direct controls")
    ok(any(a == "super" and d_.startswith("Added a source") for a, d_ in text) and ("super", "Opened the customer's dashboard (View as)") in text,
       "…and what the Super Admin did inside the customer's dashboard is marked as theirs")
    ok(any(d_ == "Connected the YouTube channel “Second”" for _a, d_ in text) and all(r["at"] and r["name"] == "Asha" for r in rows), "…with the time and whose account")
    only2 = owner.get(f"/api/admin/activity?account_id={a2}").json()["rows"]
    ok(only2 and all(r["account_id"] == a2 and r["name"] == "Ravi" for r in only2) and not any("Asha" in r["detail"] or "Second" in r["detail"] for r in only2),
       "the log can be read per customer")
    allrows = owner.get("/api/admin/activity?limit=500").json()["rows"]
    ok({r["account_id"] for r in allrows} >= {a1, a2} and allrows[0]["id"] > allrows[-1]["id"], "…and overall, newest first")
    f1 = owner.get("/api/admin/activity?actor=super&q=switched").json()["rows"]
    ok(f1 and all(r["actor"] == "super" and "switched" in r["detail"] for r in f1), "…filtered by who did it and by words")
    older = owner.get(f"/api/admin/activity?limit=5&before={allrows[4]['id']}").json()["rows"]
    ok(older and older[0]["id"] < allrows[4]["id"], "…page by page")
    ok(p["activity"] and p["activity"][0]["account_id"] == a1, "the Access panel carries the customer's recent activity")
    n0 = len(owner.get("/api/admin/activity?limit=500").json()["rows"])
    c1.get("/api/overview"); c1.get("/api/clips"); c1.put("/api/clips/1", json={"title": "x"})
    ok(len(owner.get("/api/admin/activity?limit=500").json()["rows"]) == n0, "reading pages and small edits do not fill the log")
    old_max = acl.MAX_ACTIVITY
    acl.MAX_ACTIVITY = 50
    for i in range(450):
        acl.record(a2, "system", "test", f"line {i}")
    with acl.CoreSession() as s:
        left = s.query(acl.Activity).count()
    acl.MAX_ACTIVITY = old_max
    ok(left <= 250, f"the log is kept to a sane size (old lines are dropped): {left} lines left")
print("DONE", flush=True)

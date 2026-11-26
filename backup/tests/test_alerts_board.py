import json, os, sys, tempfile, time
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "board-test-pass-9"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from app import main, guide, jobs, monitor, alerts, auth, tenant, telegram
from app.db import SessionLocal, Alert, Destination, Channel, Clip, Project, get_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
ids = lambda b, tab: [i["id"] for i in b["tabs"][tab]]
kinds = lambda b, tab: [i["kind"] for i in b["tabs"][tab]]
find = lambda b, tab, pred: next((i for i in b["tabs"][tab] if pred(i)), None)
does = lambda item: [a["do"] for a in item["actions"]]
with TestClient(main.app, raise_server_exceptions=False) as tc, TestClient(main.app) as c1, TestClient(main.app) as anon:
    tc.post("/api/login", json={"username": "admin", "password": "board-test-pass-9"})
    board = lambda c=tc: c.get("/api/alerts/board").json()
    # --- a fresh system
    b = board()
    ok(set(b["tabs"]) == {"deleted", "strikes", "broken", "system"} and set(b["counts"]) == {"all", "deleted", "strikes", "broken", "system"}, "one call returns the four tabs and the counts (All = the sum)")
    nc = find(b, "broken", lambda i: i["id"] == "now:no_channel:")
    ok(nc and nc["level"] == "bad" and nc["at"] is None and does(nc) == ["fix", "dismiss"] and nc["actions"][0]["fix"] == "connect", "no channel connected -> Not working, 'fix now', with the Connect button")
    ok(b["badge"] >= 1 and b["counts"]["deleted"] == 0 and b["counts"]["strikes"] == 0 and b["tabs"]["system"] == [], f"badge counts it ({b['badge']}); other tabs empty")
    ok(tc.get("/api/overview").json()["alerts_badge"] == b["badge"], "the sidebar number comes with the dashboard data")
    # --- videos on YouTube: deleted / copyright / blocked / processing failed / too long
    with SessionLocal() as s:
        s.add(Destination(channel_id="UCa", name="Foody", token="x")); s.commit()
        p = Project(url="https://youtu.be/abcdefghijk", video_id="abcdefghijk", title="Street food", channel="Creator <b>", mode="cc", duration=600); s.add(p); s.commit()
        for i, y in enumerate(["yt_del01", "yt_copy1", "yt_block", "yt_proc1", "yt_long1", "yt_fine1"]):
            s.add(Clip(project_id=p.id, start=i * 10, end=i * 10 + 30, aspect="9:16", status="uploaded", youtube_id=y, destination_id=1, title=f"Clip {y}", output_path="x.mp4"))
        s.commit()
    state = {"yt_copy1": {"status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}},
             "yt_block": {"status": {"uploadStatus": "processed"}, "contentDetails": {"regionRestriction": {"blocked": ["IN", "US"]}}},
             "yt_proc1": {"status": {"uploadStatus": "failed", "failureReason": "codec"}},
             "yt_long1": {"status": {"uploadStatus": "rejected", "rejectionReason": "length"}},
             "yt_fine1": {"status": {"uploadStatus": "processed"}}}
    monitor.youtube.video_status = lambda did, yids: {y: state[y] for y in yids if y in state}
    r = tc.post("/api/alerts/check").json()
    ok(r == {"checked": 6, "new_alerts": 5}, f"check: {r}")
    b = board()
    dele = b["tabs"]["deleted"]
    ok(len(dele) == 1 and dele[0]["kind"] == "removed" and dele[0]["title"] == "Clip yt_del01" and dele[0]["level"] == "bad" and not dele[0]["seen"], "deleted video -> Deleted tab")
    ok(does(dele[0]) == ["link", "open_clip", "again", "remove", "dismiss"] and dele[0]["actions"][0]["href"] == "https://studio.youtube.com/video/yt_del01/copyright", "…with Open in YouTube Studio, Open clip, Upload again, Dismiss")
    ok(dele[0]["details"]["source_channel"] == "Creator <b>" and dele[0]["details"]["clip_range"] == [0.0, 30.0], "…and the details of the video (escaped by the page, raw here)")
    st = b["tabs"]["strikes"]
    cp = find(b, "strikes", lambda i: i["title"] == "Clip yt_copy1"); bl = find(b, "strikes", lambda i: i["title"] == "Clip yt_block")
    ok(cp and cp["kind"] == "rejected" and "copyright" in cp["text"] and "again" not in does(cp), "copyright removal -> Strikes & copyright, no 'Upload again'")
    ok(bl and bl["kind"] == "blocked" and "again" not in does(bl), "blocked in countries -> Strikes & copyright")
    pz = find(b, "strikes", lambda i: i["id"] == "now:paused:1")
    ok(pz and pz["level"] == "bad" and pz["actions"][0] == {"do": "fix", "fix": "resume", "id": 1} and "Foody" in pz["text"], "the channel paused because of it is in the same tab, with Resume uploads")
    ok(st[0]["id"] == "now:paused:1", "what is wrong right now comes first")
    pr = find(b, "broken", lambda i: i["title"] == "Clip yt_proc1"); lg = find(b, "broken", lambda i: i["title"] == "Clip yt_long1")
    ok(pr and pr["kind"] == "failed" and "again" in does(pr), "YouTube could not process a video -> Not working, with Upload again")
    ok(lg and lg["kind"] == "rejected", "rejected only because it is too long -> Not working (not a strike)")
    ok(alerts.alert_tab("rejected", {}) == "strikes" and alerts.alert_tab("something_new", None) == "strikes" and alerts.alert_tab("rejected", None) == "strikes", "unclear kinds go to the more serious tab")
    nh = find(b, "broken", lambda i: i["id"] == "now:no_helper:")
    ok(nh and nh["level"] == "warn" and b["counts"] == {"deleted": 1, "strikes": 4, "broken": 3, "system": 0, "all": 8} and b["badge"] == 7,  # + the strike case (strikes.py)
       f"tab numbers count what is new ({b['counts']}); the sidebar only the serious ones ({b['badge']}: 'no helper PC yet' is a hint, not an alarm)")
    ok(b["watching"] == 6 and b["last_check"], "page header: how many videos are watched, last check")
    # --- old endpoints still work
    old = tc.get("/api/alerts").json()
    ok(len(old["alerts"]) == 5 and old["watching"] == 6 and tc.get("/api/overview").json()["alerts_unseen"] == 5, "the old /api/alerts list and the dashboard's alerts_unseen still work")
    # --- failed clips and uploads
    with SessionLocal() as s:
        s.add(Clip(project_id=1, start=0, end=30, aspect="9:16", status="failed", title="Bad render", error="ERROR: Sign in to confirm you're not a bot")); s.commit()
        s.add(Clip(project_id=1, start=0, end=30, aspect="9:16", status="failed", title="Bad upload", output_path="out.mp4", destination_id=1,
                   error='<HttpError 400 when requesting https://youtube.googleapis.com/upload returned "The user has exceeded the number of videos they may upload.". Details: "[{}]">')); s.commit()
        s.add(Clip(project_id=1, start=0, end=30, aspect="9:16", status="draft", title="Voice late", error="The helper PC did not make the voice-over in time. Start the helper and press Make voice again.")); s.commit()
        s.add(Clip(project_id=1, start=0, end=30, aspect="9:16", status="rendered", title="No thumb", thumb_note="Thumbnail could not be made: font missing")); s.commit()
        s.add(Clip(project_id=1, start=0, end=30, aspect="9:16", status="uploaded", title="Insta fail", youtube_id="yt_meta1", yt_state="ok", attempted_at=main.utcnow(),
                   meta_result={"instagram_error": "Session has expired", "facebook": "123"})); s.commit()
        s.add(Project(url="u", video_id="zzzzzzzzzzz", title="Quick one", channel="C", mode="cc", duration=60, quick={"state": "failed", "step": "Video unavailable"})); s.commit()
        rid, uid = [c.id for c in s.query(Clip).filter(Clip.status == "failed").order_by(Clip.id)]
    b = board()
    rf = find(b, "broken", lambda i: i["kind"] == "clip_failed"); uf = find(b, "broken", lambda i: i["kind"] == "upload_failed")
    ok(rf and rf["level"] == "warn" and "could not be made" in rf["text"] and "Helper PC" in rf["text"] and does(rf) == ["open_clip", "retry_clip", "dismiss"], "failed clip -> Not working, reason in plain words, Open clip + Retry")
    ok(uf and uf["level"] == "bad" and "YouTube said: The user has exceeded the number of videos they may upload." in uf["text"] and "HttpError" not in uf["text"], "failed upload -> 'fix now', YouTube's own sentence instead of the raw reply")
    ok(b["failed_clips"] == 2 and not any(i["id"].startswith("now:failed") for i in b["tabs"]["broken"]), "failed clips are listed one by one (no second summary line)")
    ok({"clip_note", "thumb_failed", "meta_failed", "quick_failed"} <= set(kinds(b, "broken")), f"also shown: late voice-over, thumbnail not made, Instagram/Facebook failed, Quick upload stopped")
    mf = find(b, "broken", lambda i: i["kind"] == "meta_failed")
    ok("Instagram: Session has expired" in mf["text"] and "facebook" not in mf["text"].lower().replace("instagram / facebook", "").replace("instagram + facebook", ""), "Instagram failure names what failed, not what worked")
    # --- system errors: background jobs, page errors, the video check itself
    def bad_job(): raise ValueError("render exploded")
    jobs.executor.submit(bad_job).exception(); time.sleep(0.3)
    jobs.executor.submit(bad_job).exception(); time.sleep(0.3)
    b = board()
    se = b["tabs"]["system"]
    ok(len(se) == 1 and se[0]["title"] == "A background job stopped: bad_job" and se[0]["count"] == 2 and "2 times" in se[0]["text"] and "ValueError: render exploded" in se[0]["tech"], "a background job that crashed twice -> System errors, once, with the repeat count")
    ok(does(se[0]) == ["go", "dismiss"] and se[0]["actions"][0]["page"] == "guide" and b["counts"]["system"] == 1, "…with Open Setup guide and Dismiss")
    badge_before = b["badge"]
    def boom(did, yids): raise RuntimeError("YouTube API fell over")
    monitor.youtube.video_status = boom
    r = tc.post("/api/alerts/check").json()
    b = board()
    ok(r["checked"] == 0 and b["tabs"]["system"][0]["title"] == "Something went wrong: Checking my videos on YouTube" and len(b["tabs"]["deleted"]) == 1, "the video check itself failing is no longer silent (and marks nothing as deleted)")
    ok(b["badge"] == badge_before, "system errors do not raise the sidebar number")
    real_health = main.quota.summary
    def nope(): raise RuntimeError("quota table broken")
    main.quota.summary = nope
    b = board()
    ok(find(b, "broken", lambda i: i["id"] == "now:check:") and len(b["tabs"]["deleted"]) == 1 and any("Alerts: checking the system" in i["title"] for i in b["tabs"]["system"]), "the page still opens when the self-check breaks; that problem is listed too")
    main.quota.summary = real_health
    # --- dismiss / mark as seen (history is kept)
    n_rows = len(tc.get("/api/alerts").json()["alerts"])
    b = board(); did = b["tabs"]["deleted"][0]["id"]; badge0 = b["badge"]
    r = tc.post("/api/alerts/board/seen", json={"tab": "all", "id": did}).json()
    b = board()
    ok(r["marked"] == 1 and r["badge"] == badge0 - 1 and b["tabs"]["deleted"][0]["seen"] and "dismiss" not in does(b["tabs"]["deleted"][0]) and b["counts"]["deleted"] == 0, "Dismiss on one item: seen, badge one lower")
    ok(len(tc.get("/api/alerts").json()["alerts"]) == n_rows, "…nothing is deleted: the history stays")
    r = tc.post("/api/alerts/board/seen", json={"tab": "strikes"}).json()
    b = board()
    ok(r["marked"] == 4 and b["counts"]["strikes"] == 0 and all(i["seen"] for i in b["tabs"]["strikes"]) and b["counts"]["broken"] > 0, "Mark all as seen works per tab (the other tabs keep their marks)")
    ok(sorted(json.loads(get_setting("alerts_seen"))) == ["now:paused:1", "strike:1:open"], "what was seen of the 'right now' items is remembered by item")
    tc.post("/api/alerts/board/seen", json={"tab": "all"})
    b = board()
    ok(b["badge"] == 0 and b["counts"]["all"] == 0 and b["totals"]["all"] > 8, f"Mark all as seen: nothing new, {b['totals']['all']} items still listed")
    ok(tc.get("/api/overview").json()["alerts_badge"] == 0, "sidebar number gone")
    with SessionLocal() as s:  # the same upload fails again for another reason: that is news
        c = s.get(Clip, uid); c.error = "Backend Error"; s.commit()
    b = board()
    uf = find(b, "broken", lambda i: i["kind"] == "upload_failed")
    ok(not uf["seen"] and b["badge"] == 1, "a problem that comes back with a new reason is new again")
    tc.post(f"/api/destinations/1/resume"); b = board()
    ok(not find(b, "strikes", lambda i: i["id"].startswith("now:paused")) and "now:paused:1" not in json.loads(get_setting("alerts_seen")), "a fixed problem leaves the list by itself (and is forgotten, so it would be new next time)")
    tc.post("/api/destinations/1/pause"); b = board()
    mine = find(b, "broken", lambda i: i["id"] == "now:paused_by_you:1")
    ok(mine and mine["level"] == "warn" and not find(b, "strikes", lambda i: i["kind"] == "now"), "uploads paused by the owner are a reminder under Not working, not a strike")
    tc.post("/api/destinations/1/resume")
    ok(tc.post("/api/alerts/board/seen", json={"tab": "nonsense"}).status_code == 422, "a wrong tab name is refused")
    # --- good news and expired logins
    state["yt_block"] = state["yt_meta1"] = {"status": {"uploadStatus": "processed"}}
    monitor.youtube.video_status = lambda did, yids: {y: state[y] for y in yids if y in state}
    tc.post("/api/alerts/check"); b = board()
    rs = find(b, "strikes", lambda i: i["kind"] == "restored")
    ok(rs and rs["level"] == "info" and rs["title"] == "Clip yt_block" and b["counts"]["strikes"] == 0, "'available again' is shown next to the problem it ends, and is not counted")
    def expired(did, yids): raise RefreshError("invalid_grant: Token has been expired or revoked.")
    monitor.youtube.video_status = expired
    tc.post("/api/alerts/check"); b = board()
    rc = find(b, "broken", lambda i: i["id"] == "now:reconnect:1")
    ok(rc and rc["level"] == "bad" and rc["actions"][0]["fix"] == "connect" and not any("RefreshError" in (i.get("tech") or "") for i in b["tabs"]["system"]), "expired Google login found by the video check -> 'reconnect' under Not working (not a system error)")
    with SessionLocal() as s:
        dd = s.get(Destination, 1); dd.paused, dd.paused_reason = False, None; s.commit()
    # --- every customer sees only their own alerts
    anon.post("/api/register", json={"name": "Asha Rao", "brand": "Asha Clips", "email": "asha@example.com", "password": "customer-one-pass-1", "accept_terms": True})
    uid1 = tc.get("/api/admin/users").json()["users"][0]["id"]
    tc.post(f"/api/admin/users/{uid1}/approve")
    c1.post("/api/login", json={"username": "asha@example.com", "password": "customer-one-pass-1"})
    t1 = c1.get("/api/me").json()["tenant"]
    b1 = board(c1)
    ok(b1["tabs"]["deleted"] == [] and b1["tabs"]["strikes"] == [] and b1["tabs"]["system"] == [] and not any(i.get("clip_id") for i in b1["tabs"]["broken"]), "a customer's Alerts page has none of the owner's alerts, failed clips or system errors")
    with tenant.use(t1):
        with SessionLocal() as s:
            s.add(Alert(clip_id=77, kind="removed", message="gone", details={"title": "Asha's video", "youtube_id": "yt_asha01"})); s.commit()
        guide.record_error("background job: asha_job", KeyError("asha only"))
    b1 = board(c1); bo = board()
    ok([i["title"] for i in b1["tabs"]["deleted"]] == ["Asha's video"] and [i["title"] for i in b1["tabs"]["system"]] == ["A background job stopped: asha_job"], "the customer sees their own")
    leak = [i["title"] for t in bo["tabs"].values() for i in t if "asha" in (i["title"] + i["text"] + str(i.get("tech"))).lower()]
    ok(not leak and len(bo["tabs"]["deleted"]) == 1, f"…and the owner never sees the customer's {leak} {len(bo['tabs']['deleted'])}")
    ok(does(b1["tabs"]["deleted"][0]) == ["link", "dismiss"], "a clip that no longer exists gets no 'Open clip' button")
    owner_new = bo["counts"]["all"]
    r = c1.post("/api/alerts/board/seen", json={"tab": "all"}).json()
    ok(r["marked"] >= 2 and board(c1)["counts"]["all"] == 0 and board()["counts"]["all"] == owner_new, "a customer's 'Mark all as seen' changes nothing for the owner")
    ok(anon.get("/api/alerts/board").status_code == 401 and anon.post("/api/alerts/board/seen", json={"tab": "all"}).status_code == 401, "closed without a login")
# --- the guide
qs = [q for q, _, _ in guide.HELP]
i = qs.index("The thumbnail is not on YouTube")
ok(qs[i + 1] == "Where do I see everything that went wrong?" and "Alerts" in guide.HELP[i + 1][2], "Setup guide: a help entry about the Alerts page, right after the thumbnail entry")
page = open(r"D:\integ-final\static\index.html", encoding="utf-8").read()
ok(all(x in page for x in ("Strikes & copyright", "Not working", "System errors", "id=\"al-tabs\"", "Mark all as seen", "/api/alerts/board")), "dashboard: the Alerts page has the tabs")

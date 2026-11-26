"""Self-repair (app/watchdog.py): each remedy finds its problem, fixes it at most N times per clip and per day, does
nothing the second time, stays inside its own account, can be switched off, never deletes a file that exists only on
the server and never uploads anything. Plus: its messages (e-mail always, phone, Telegram, WhatsApp) and the WhatsApp
card. No real network: httpx, Gmail and YouTube are faked."""
import io, json, logging, os, re, sys, tempfile, time
from datetime import timedelta
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
CODE = os.environ.get("RS_CODE", r"D:\watchdog")
sys.path.insert(0, CODE)
import httpx
from fastapi.testclient import TestClient
from app import main, acl, tenant, auth, youtube, scheduler, telegram, jobs, autopilot, archive, gmail, phone, guide, watchdog, whatsapp, workers
from app.db import SessionLocal, Destination, Clip, Project, Worker, ChannelStat, VideoStat, set_setting, get_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)

# ---------------------------------------------------------------- fakes: nothing leaves this computer
uploaded = []
youtube.upload = lambda *a, **k: (uploaded.append(a), "yt-never")[1]
submitted = []
jobs.executor.submit = lambda fn, *a, **k: submitted.append((getattr(fn, "__name__", "?"), a, tenant.current()))
auth.rate_limited = lambda ip, kind="owner": False
net = []  # every "network" call: (method, url, params/json)


class Resp:
    def __init__(self, code=200, text="Message queued", data=None):
        self.status_code, self.text, self._d = code, text, data if data is not None else {"ok": True, "result": {}}

    def json(self):
        return self._d


GET_FAIL = []


def fake_get(url, params=None, timeout=None, **k):
    net.append(("GET", url, dict(params or {})))
    if GET_FAIL:
        raise httpx.ConnectError(f"cannot reach {url}?phone={params.get('phone')}&apikey={params.get('apikey')}")
    return Resp()


def fake_post(url, json=None, content=None, data=None, timeout=None, **k):
    net.append(("POST", url, json if json is not None else (content.decode() if content else data)))
    return Resp()


httpx.get, httpx.post = fake_get, fake_post
mails = []


class _Exec:
    def __init__(self, body): self.body = body
    def execute(self): mails.append(self.body); return {"id": "m1"}


class _Msgs:
    def send(self, userId=None, body=None): return _Exec(body)


class _Users:
    def messages(self): return _Msgs()


class _Svc:
    def users(self): return _Users()


gmail._service = lambda: _Svc()
logbuf = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(logbuf))
logging.getLogger().setLevel(logging.INFO)
NOW = utcnow()
FILE = Path(d) / "video.mp4"; FILE.write_bytes(b"x" * 100)
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
_real_start = scheduler.start


def _paused_start():  # every TestClient starts the app (and its timed jobs): keep them all paused during the test
    sch = _real_start()
    sch.pause()
    return sch


scheduler.start = _paused_start


def only(*names, on=True):
    """Switch every remedy off except these (so one test does not see the others' work)."""
    return watchdog.save_config(on, [k for k in watchdog.REMEDIES if k not in names])


def reset():
    set_setting(watchdog.STATE_KEY, "")
    for k in list(scheduler._queue_runs):
        scheduler._queue_runs[k] = 0


def run(tid=tenant.OWNER, at=None):
    with tenant.use(tid):
        return watchdog.run(at or utcnow())


def state(tid=tenant.OWNER):
    with tenant.use(tid):
        return watchdog._load()


def told(tid=tenant.OWNER):
    return [f for f in state(tid)["fixed"] if f["kind"] == "alert"]


def fixed(tid=tenant.OWNER, r=None):
    return [f for f in state(tid)["fixed"] if f["kind"] == "fixed" and (r is None or f["r"] == r)]


seq = [0]


def clip(tid=tenant.OWNER, **kw):
    with tenant.use(tid), SessionLocal() as s:
        dest = s.query(Destination).first()
        seq[0] += 1
        p = Project(url="u", video_id="v%010d" % seq[0], title="T", channel="C", channel_url="https://www.youtube.com/channel/UCcreator",
                    mode="cc", license=youtube.CC_LICENSE, duration=600)
        s.add(p); s.commit()
        base = dict(project_id=p.id, start=0, end=30, aspect="9:16", status="queued", title=f"Clip title {seq[0]}",
                    description="d", destination_id=dest.id, autoqueue=True)
        base.update(kw)
        c = Clip(**base); s.add(c); s.commit()
        return c.id


def get(cid, tid=tenant.OWNER):
    with tenant.use(tid), SessionLocal() as s:
        return s.get(Clip, cid)


def setc(cid, tid=tenant.OWNER, **kw):
    with tenant.use(tid), SessionLocal() as s:
        c = s.get(Clip, cid)
        for k, v in kw.items():
            setattr(c, k, v)
        s.commit()


with mk() as owner, mk() as c1, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Owner channel", token="x")); s.commit()

    anon.post("/api/register", json={"name": "Asha", "brand": "Asha Studio", "email": "asha@example.com", "password": PW, "accept_terms": True})
    a1 = next(u["id"] for u in owner.get("/api/admin/users").json()["users"] if u["email"] == "asha@example.com")
    t1 = owner.post(f"/api/admin/users/{a1}/approve").json()["tenant_id"]
    c1.post("/api/login", json={"username": "asha@example.com", "password": PW})
    with tenant.use(t1):
        set_setting("onboarding", '{"finished": true}')
        with SessionLocal() as s:
            s.add(Destination(channel_id="UC-asha", name="Asha channel", token="x")); s.commit()
    sw = lambda aid, key, on: owner.post(f"/api/admin/users/{aid}/access", json={"perms": {key: on}})

    # ============================================================ switches
    ok(watchdog.config() == {"on": True, "off": []} and watchdog.enabled() and watchdog.enabled("retry"), "Self-repair is on by default, every remedy on")
    ok(len(acl.PERMS) == 60 and acl.DEFAULT["watchdog.repair"] is True and acl.DEFAULT["whatsapp.use"] is True, "two new Super Admin switches, both on by default")
    r = owner.post("/api/admin/watchdog", json={"on": False, "off": []})
    ok(r.status_code == 200 and not r.json()["on"] and run() == {"off": True}, "Super Admin switches Self-repair off: the round does nothing")
    ok(c1.post("/api/admin/watchdog", json={"on": True, "off": []}).status_code == 403, "a customer cannot change the self-repair switches")
    ok(owner.post("/api/admin/watchdog", json={"on": True, "off": ["nonsense"]}).status_code == 422, "unknown remedy names are refused")
    owner.post("/api/admin/watchdog", json={"on": True, "off": ["disk", "late"]})
    out = run()
    ok("disk" not in out and "late" not in out and "retry" in out, "a remedy can be switched off on its own")
    ok(owner.get("/api/watchdog").status_code == 200 and c1.get("/api/watchdog").status_code == 200, "the Fixed-by-itself list opens for the owner and a customer")

    # ============================================================ 1. stuck "rendering"
    only("stuck"); reset(); submitted.clear()
    r1 = clip(status="rendering")
    run(at=NOW)
    ok(get(r1).status == "rendering" and not submitted, "stuck render: seen for the first time, nothing done yet")
    run(at=NOW + timedelta(minutes=10))
    ok(get(r1).status == "rendering", "stuck render: not before 30 minutes")
    orig_q = jobs.queued
    jobs.queued = lambda name, tid: {r1} if name == "run_render" else set()
    run(at=NOW + timedelta(minutes=40))
    ok(get(r1).status == "rendering" and not submitted, "a clip that waits in line for its render is left alone")
    jobs.queued = orig_q
    orig_busy = jobs.busy
    jobs.busy = lambda tid: True
    run(at=NOW + timedelta(minutes=40))
    ok(get(r1).status == "rendering", "while a render job runs for the account (e.g. a Quick upload), nothing is touched")
    jobs.busy = orig_busy
    run(at=NOW + timedelta(minutes=40))
    ok(get(r1).status == "draft" and submitted == [("run_render", (r1,), 1)], "lost render: back to draft and made again (like after a restart)")
    ok(len(fixed(r="stuck")) == 1 and "being made again" in fixed(r="stuck")[0]["t"], "written to Fixed by itself, in plain words")
    submitted.clear(); run(at=NOW + timedelta(minutes=41))
    ok(not submitted and len(fixed(r="stuck")) == 1, "idempotent: the next round changes nothing")
    for i in range(2, 5):  # it keeps getting stuck
        setc(r1, status="rendering")
        run(at=NOW + timedelta(hours=i)); run(at=NOW + timedelta(hours=i, minutes=35))
    ok(len(fixed(r="stuck")) == 3 and get(r1).status == "rendering", "at most 3 fixes of the same clip")
    ok(len([t for t in told() if "keeps stopping" in t["t"]]) == 1, "then one message to the owner")
    run(at=NOW + timedelta(hours=6))
    ok(len([t for t in told() if "keeps stopping" in t["t"]]) == 1, "the message is not repeated")

    # ============================================================ 1b. stuck "uploading"
    reset(); submitted.clear()
    u_done = clip(status="uploading", youtube_id="ytDONE00001", publish_at=NOW + timedelta(days=2))
    u_lost = clip(status="uploading", output_path=str(FILE), publish_at=NOW + timedelta(days=3))
    u_twin = clip(status="uploading", output_path=str(FILE), publish_at=NOW + timedelta(days=4))
    scheduler._queue_runs[1] = 1
    run(at=NOW); run(at=NOW + timedelta(minutes=30))
    ok(get(u_done).status == "uploading", "while an upload round runs, an uploading clip is never touched")
    scheduler._queue_runs[1] = 0
    run(at=NOW + timedelta(minutes=31))
    ok(get(u_done).status == "uploaded" and get(u_done).youtube_id == "ytDONE00001", "on YouTube already (id saved): marked uploaded")
    ok(get(u_lost).status == "uploading" and get(u_twin).status == "uploading", "no id and the channel's video list not read since: waits (never a double upload)")
    with SessionLocal() as s:
        dest = s.query(Destination).first()
        s.add(ChannelStat(destination_id=dest.id, at=NOW + timedelta(minutes=40), subscribers=1, views=1, videos=1))
        s.add(VideoStat(video_id="ytTWIN00001", destination_id=dest.id, title=get(u_twin).title, views=0, updated_at=NOW))
        s.commit()
    run(at=NOW + timedelta(minutes=45))
    lost = get(u_lost)
    ok(lost.status == "queued" and abs((lost.publish_at - (NOW + timedelta(days=3))).total_seconds()) < 1,
       "not on the channel: back to queued with the same slot")
    ok(get(u_twin).status == "failed" and "not uploaded again" in get(u_twin).error and watchdog.classify(get(u_twin).error) is None
       and any("NOT uploaded again" in t["t"] for t in told()),
       "a video with the same title is on the channel: never uploaded again, the owner is asked to look")
    ok(not uploaded, "the watchdog itself never uploads")

    # ============================================================ 2. retry failed clips
    ok(watchdog.classify("No helper PC fetched this video for 24 hours. Start the helper") == "helper", "classify: no helper for 24 h")
    ok(all(watchdog.classify(e) == "transient" for e in ("(sqlite3.OperationalError) database is locked", "HttpError 503 backendError",
                                                          "ReadTimeout: timed out", "Connection reset by peer")), "classify: passing reasons")
    ok(all(watchdog.classify(e) is None for e in ("Video blocked by copyright", "Not a Creative Commons video", "This video is private, removed or members-only, so it can't be used.",
                                                   "quotaExceeded", "held after a strike 503", "Reconnect your YouTube channel (login expired)", "some new bug")),
       "classify: copyright / not CC / removed source / quota / strike / login / unknown stay failed")
    only("retry"); reset(); submitted.clear()
    f_lock = clip(status="failed", error="(sqlite3.OperationalError) database is locked")
    f_copy = clip(status="failed", error="Video blocked by copyright claim")
    f_help = clip(status="failed", error="No helper PC fetched this video for 24 hours. Start the helper on your PC")
    run(at=NOW)
    ok(get(f_lock).status == "failed" and not submitted, "a passing failure waits 30 minutes first")
    items = owner.get("/api/health").json()["items"]
    fail_item = next((i for i in items if i["key"] == "failed"), None)
    rep_item = next((i for i in items if i["key"] == "self_repair"), None)
    with SessionLocal() as s_:
        n_failed = s_.query(Clip).filter(Clip.status == "failed").count()
    ok(fail_item and fail_item["text"].startswith(f"{n_failed - 2} clip") and rep_item and rep_item["level"] == "info"
       and rep_item["text"].startswith("2 failed clip"),
       "Problems list: the clip that will be tried again by itself is not counted as a problem")
    run(at=NOW + timedelta(minutes=31))
    ok(get(f_lock).status == "rendering" and ("run_render", (f_lock,), 1) in submitted, "after 30 minutes: made again (same claim as the Retry button)")
    ok(get(f_copy).status == "failed" and get(f_copy).error.startswith("Video blocked"), "copyright: never retried")
    ok(get(f_help).status == "failed", "no helper online: the no-helper failure waits")
    with SessionLocal() as s:
        s.add(Worker(name="pc", token_hash="h1", approved=True, last_seen=utcnow())); s.commit()
    run(at=NOW + timedelta(minutes=32))
    ok(get(f_help).status == "rendering", "a helper is online again: the no-helper failure is tried again")
    n_sub = len(submitted)
    run(at=NOW + timedelta(minutes=33))
    ok(len(submitted) == n_sub, "idempotent: a clip being made is not touched again")
    setc(f_lock, status="failed", error="database is locked")
    run(at=NOW + timedelta(minutes=60))
    ok(get(f_lock).status == "failed", "failed again: waits 2 hours (back-off)")
    run(at=NOW + timedelta(hours=3))
    ok(get(f_lock).status == "rendering", "after the back-off: try 2")
    setc(f_lock, status="failed", error="database is locked")
    run(at=NOW + timedelta(hours=12))
    ok(get(f_lock).status == "rendering", "try 3 after 8 hours")
    setc(f_lock, status="failed", error="database is locked")
    run(at=NOW + timedelta(hours=40))
    ok(get(f_lock).status == "failed" and any("after 3 tries" in t["t"] for t in told()), "after 3 tries: stays failed, one message")
    run(at=NOW + timedelta(hours=41))
    ok(len([t for t in told() if "after 3 tries" in t["t"]]) == 1, "the give-up message is sent once")
    # a failed UPLOAD: queued again only when the channel's video list proves it is not there
    reset()
    up = clip(status="failed", output_path=str(FILE), error="HttpError 500 when requesting ... backendError",
              attempted_at=NOW + timedelta(minutes=30), publish_at=NOW - timedelta(hours=5))  # (after the list read at +40 min)
    run(at=NOW); run(at=NOW + timedelta(minutes=31))
    ok(get(up).status == "failed", "upload failure: the channel's list was not read after it -> waits")
    with SessionLocal() as s:
        s.add(ChannelStat(destination_id=dest.id, at=NOW + timedelta(minutes=50), subscribers=1, views=1, videos=1)); s.commit()
    run(at=NOW + timedelta(minutes=32))
    u = get(up)
    ok(u.status == "queued" and u.publish_at > utcnow() and u.publish_at.hour == 12 and u.publish_at.minute == 30,
       f"not on the channel: queued again on the next free daily slot, 18:00 India time ({u.publish_at})")
    up2 = clip(status="failed", output_path=str(FILE), error="Connection reset by peer", attempted_at=NOW, publish_at=NOW + timedelta(days=9))
    with SessionLocal() as s:
        s.add(VideoStat(video_id="ytSAME00001", destination_id=dest.id, title=get(up2).title, views=0, updated_at=NOW)); s.commit()
    run(at=NOW + timedelta(minutes=33)); run(at=NOW + timedelta(minutes=70))
    ok(get(up2).status == "failed" and any("NOT uploaded again" in t["t"] and "broke off" in t["t"] for t in told()),
       "same title already on the channel: never uploaded twice")
    # the daily cap
    with SessionLocal() as s:
        s.query(Clip).filter(Clip.status == "failed").update({"status": "draft"}); s.commit()
    reset(); submitted.clear()
    many = [clip(status="failed", error="ReadTimeout: timed out") for _ in range(25)]
    run(at=NOW); run(at=NOW + timedelta(minutes=31))
    ok(sum(1 for i in many if get(i).status == "rendering") == watchdog.CAP["retry"] == 20, "at most 20 retries a day per account")
    ok(not uploaded, "never uploads anything itself")

    # ============================================================ 3. waiting clip whose time passed
    only("late"); reset()
    with SessionLocal() as s:
        s.query(Clip).filter(Clip.status != "uploaded").update({"status": "draft", "publish_at": None})
        s.commit()
    later = clip(status="queued", publish_at=(NOW + timedelta(days=5)).replace(hour=12, minute=30, second=0, microsecond=0), output_path=str(FILE))
    late1 = clip(status="queued", publish_at=NOW - timedelta(hours=3), output_path=str(FILE))
    soon = clip(status="queued", publish_at=NOW - timedelta(minutes=20), output_path=str(FILE))
    pcc = clip(status="queued", publish_at=NOW - timedelta(hours=3), output_path=str(FILE), pc_item_id=99)
    run(at=NOW)
    l1 = get(late1)
    want = get(later).publish_at + timedelta(days=1)
    ok(l1.status == "queued" and l1.publish_at == want, f"time passed: moved to the next free day after the last scheduled one ({l1.publish_at} = {want})")
    ok("Moved to" in (l1.error or "") and "India time" in l1.error, "the clip gets a plain note")
    ok(get(soon).publish_at == NOW - timedelta(minutes=20) or abs((get(soon).publish_at - (NOW - timedelta(minutes=20))).total_seconds()) < 1,
       "a time passed by less than an hour is left to the upload round")
    ok(get(pcc).publish_at < NOW, "a PC-folder video is left to its own rule (pcfolder.due)")
    run(at=NOW + timedelta(minutes=1))
    ok(get(late1).publish_at == want and len(fixed(r="late")) == 1, "idempotent: moved once")
    for i in range(12):  # uploads stay stopped: it keeps missing its day
        setc(late1, publish_at=NOW - timedelta(hours=2))
        run(at=NOW + timedelta(minutes=2 + i))
    ok(len(fixed(r="late")) == 10 and any("missed its day" in t["t"] for t in told()), "at most 10 moves of one clip, then one message")
    ok(not uploaded, f"moving a time never uploads {uploaded[:2]}")

    # ============================================================ 5. disk
    only("disk"); reset()
    root = Path(d) / "clips"

    def folder(cid, names, old=True):
        w = root / str(cid); (w / "src").mkdir(parents=True, exist_ok=True)
        for n in names:
            (w / n).write_bytes(b"v" * 2000)
        if old:
            t = time.time() - 10 * 3600
            for f in [w, *w.rglob("*")]:
                os.utime(f, (t, t))
        return w

    saved = clip(status="uploaded", youtube_id="ytSAVED0001", attempted_at=NOW - timedelta(days=5))
    w_saved = folder(saved, ["out.mp4"])
    setc(saved, output_path=str(w_saved / "out.mp4"), archived_at=NOW, archived_sha=archive.sha256(w_saved / "out.mp4"))
    only_srv = clip(status="uploaded", youtube_id="ytONLY00001", attempted_at=NOW - timedelta(days=5))
    w_only = folder(only_srv, ["out.mp4", "src/upload.mp4"])
    setc(only_srv, output_path=str(w_only / "out.mp4"))
    waiting = clip(status="queued", publish_at=NOW + timedelta(days=20))
    w_wait = folder(waiting, ["out.mp4", "thumb.jpg"])
    setc(waiting, output_path=str(w_wait / "out.mp4"))
    (w_wait / "pc.ab12.part").write_bytes(b"p"); os.utime(w_wait / "pc.ab12.part", (time.time() - 8 * 3600,) * 2)
    (w_wait / "out.cd34.part").write_bytes(b"p"); os.utime(w_wait / "out.cd34.part", (time.time() - 8 * 3600,) * 2)
    (w_wait / "fresh.ef56.part").write_bytes(b"p")
    free = [0.5]
    autopilot.disk_free_gb = lambda: free[0]
    run(at=NOW)
    ok(not (w_saved / "out.mp4").exists(), "low disk: an uploaded video saved on the PC (same sha256) is removed")
    ok((w_only / "out.mp4").exists() and (w_only / "src" / "upload.mp4").exists(), "never: an uploaded video NOT saved on the PC, or my own file")
    ok((w_wait / "out.mp4").exists() and (w_wait / "thumb.jpg").exists(), "never: a waiting clip's video or thumbnail")
    ok(not (w_wait / "out.cd34.part").exists() and (w_wait / "fresh.ef56.part").exists(), "broken-off .part files older than 6 hours go, fresh ones stay")
    ok(len(fixed(r="disk")) == 1 and any("almost full" in t["t"] for t in told()), "still low after the clean-up: one message")
    run(at=NOW + timedelta(minutes=10))
    ok(len(fixed(r="disk")) == 1 and len([t for t in told() if "almost full" in t["t"]]) == 1, "idempotent: not again within the hour, the message once a day")
    for i in range(1, 8):
        run(at=NOW + timedelta(hours=i, minutes=5))
    ok(len(fixed(r="disk")) == watchdog.CAP["disk"], "at most 4 clean-ups a day")
    free[0] = 5.0; reset(); run(at=NOW)
    ok(not fixed(r="disk"), "enough room: nothing is done")

    # ============================================================ 7. logins
    only("login"); reset(); set_setting("gmail_email", "owner@example.com")
    with SessionLocal() as s:
        dd = s.query(Destination).first(); dd.paused, dd.paused_reason = True, "The channel's Google login expired. Press Connect"; s.commit()
    mails.clear(); run(at=NOW)
    ok(any("Connect a YouTube channel" in t["t"] for t in told()) and mails, "YouTube login expired: one message with the button to press, by e-mail")
    n = len(told()); run(at=NOW + timedelta(hours=3))
    ok(len(told()) == n, "not repeated within a day")
    run(at=NOW + timedelta(hours=25))
    ok(len(told()) == n + 1, "repeated once the next day")
    with SessionLocal() as s:
        dd = s.query(Destination).first(); dd.paused, dd.paused_reason = False, None; s.commit()
    reset(); set_setting("gmail_email", "owner@example.com"); set_setting("gmail_broken", NOW.isoformat())
    run(at=NOW)
    ok(any("Connect Gmail" in t["t"] for t in told()), "Gmail login expired: one message (phone / Telegram / WhatsApp; the mail cannot go)")
    set_setting("gmail_broken", "")

    # ============================================================ 8. helper offline
    only("helper"); reset()
    with SessionLocal() as s:
        s.query(Worker).update({"last_seen": utcnow() - timedelta(hours=3)}); s.commit()
    fw = clip(status="fetching", fetch_since=utcnow() - timedelta(hours=1))
    run(at=NOW)
    ok(not told(), "helper offline: no message at once")
    run(at=NOW + timedelta(minutes=31))
    ok(len([t for t in told() if "helper PC has been offline" in t["t"]]) == 1, "offline for 30 minutes while jobs wait: one message")
    run(at=NOW + timedelta(minutes=50)); run(at=NOW + timedelta(hours=5))
    ok(len([t for t in told() if "helper PC has been offline" in t["t"]]) == 1, "only one (at most once a day)")
    setc(fw, fetch_since=utcnow() - timedelta(hours=30))
    with SessionLocal() as s:
        s.query(Worker).update({"last_seen": utcnow() - timedelta(days=1)}); s.commit()
    scheduler.sweep_stale()
    ok(get(fw).status == "fetching", "waited 24 h but the helper was seen yesterday: keeps waiting (not failed)")
    with SessionLocal() as s:
        s.query(Worker).update({"last_seen": utcnow() - timedelta(days=3)}); s.commit()
    scheduler.sweep_stale()
    ok(get(fw).status == "failed" and "24 hours" in get(fw).error, "no helper for 2 days: fails as before, with the plain note")
    calls = []
    import app.telegram as tgm
    orig_once = tgm.notify_once
    tgm.notify_once = lambda *a, **k: calls.append(a)
    scheduler.check_helper()
    ok(not calls, "the old every-2-hours helper message is not sent while the watchdog handles it")
    only("retry")  # helper remedy off: the old rule is back
    setc(fw, status="fetching", fetch_since=utcnow() - timedelta(hours=30), error=None)
    with SessionLocal() as s:
        s.query(Worker).update({"last_seen": utcnow() - timedelta(days=1)}); s.commit()
    scheduler.sweep_stale()
    ok(get(fw).status == "failed", "helper remedy switched off: the 24-hour rule works as before")
    tgm.notify_once = orig_once

    # ============================================================ 9. repeating system errors
    only("errors"); reset(); guide.clear_errors()

    def boom(where, exc):
        try:
            raise exc
        except Exception as e:  # noqa: BLE001
            guide.record_error(where, e)

    for _ in range(2):
        boom("timed job: sync_all", RuntimeError("(sqlite3.OperationalError) database is locked"))
        boom("timed job: odd_job", ValueError("boom: unexpected value"))
    run(at=NOW)
    for _ in range(8):
        boom("timed job: sync_all", RuntimeError("(sqlite3.OperationalError) database is locked"))
        boom("timed job: odd_job", ValueError("boom: unexpected value"))
    run(at=NOW + timedelta(minutes=10))
    ok(any("sync_all" in f["t"] and "passing reason" in f["t"] for f in fixed(r="errors")), "a repeating passing error is grouped and marked handled")
    ok(len([t for t in told() if "odd_job" in t["t"] and "Copy details" in t["t"] and "boom" in t["t"]]) == 1,
       "an unknown repeating error: one message with the details to copy")
    b = owner.get("/api/alerts/board").json()
    sysi = b["tabs"]["system"]
    ok(any(i["label"] == "Handled by itself" and i["level"] == "info" for i in sysi), "Alerts -> System: the handled one is shown as handled, not as new")
    hitems = owner.get("/api/health").json()["items"]
    errs = next((i for i in hitems if i["key"] == "errors"), None)
    ok(errs is None or "odd_job" in errs["text"], "Problems list: the handled error is left out")
    for _ in range(8):
        boom("timed job: odd_job", ValueError("boom: unexpected value"))
    run(at=NOW + timedelta(minutes=20))
    ok(len([t for t in told() if "odd_job" in t["t"]]) == 1, "the same error is not reported again the same day")

    # ============================================================ 10. stalled timed jobs
    from apscheduler.schedulers.background import BackgroundScheduler
    only("scheduler"); reset()
    sch = BackgroundScheduler(timezone="UTC")
    ran = []
    scheduler.JOB_STATE["demo"] = {"started": 0.0, "done": 0.0, "running": 0}
    scheduler.JOB_STATE["fine"] = {"started": 0.0, "done": 0.0, "running": 0}
    sch.add_job(scheduler._tracked("demo", lambda: ran.append(1)), "interval", minutes=10, id="demo", name="demo", max_instances=1, coalesce=True)
    sch.add_job(scheduler._tracked("fine", lambda: None), "interval", minutes=10, id="fine", name="fine", max_instances=1, coalesce=True)
    sch.start(paused=True)
    t0 = time.time()
    scheduler.JOB_STATE["demo"] = {"started": t0 - 4 * 3600, "done": t0 - 5 * 3600, "running": 1}  # one round hangs
    scheduler.JOB_STATE["fine"] = {"started": t0 - 60, "done": t0 - 50, "running": 0}
    res = watchdog.check_jobs(sch, t0)
    ids = sorted(j.id for j in sch.get_jobs())
    ok(res["restarted"] == ["demo"] and ids == ["demo~1", "fine"], f"a stalled job is started again under a new id, a healthy one is left ({ids})")
    ok(any("“demo” had stalled" in f["t"] for f in fixed(r="scheduler")), "written to Fixed by itself")
    ok(watchdog.check_jobs(sch, t0 + 60)["restarted"] == [], "idempotent: not again right after")
    for k in range(2, 5):
        scheduler.JOB_STATE["demo"] = {"started": t0 - 4 * 3600, "done": t0 - 5 * 3600, "running": 1}
        watchdog.check_jobs(sch, t0 + k)
    ok(len(fixed(r="scheduler")) == 3 and any("Restart the server" in t["t"] for t in told()), "at most 3 restarts a day, then one message")
    only("retry")
    scheduler.JOB_STATE["fine"] = {"started": t0 - 9 * 3600, "done": t0 - 9 * 3600, "running": 0}
    ok(watchdog.check_jobs(sch, t0)["stalled"] == [], "switched off: the guard does nothing")
    sch.shutdown(wait=False)

    # ============================================================ tenant isolation
    only("retry", "late"); reset()
    with tenant.use(t1):
        watchdog.save_config(True, [k for k in watchdog.REMEDIES if k not in ("retry", "late")])
        set_setting(watchdog.STATE_KEY, "")
    own_f = clip(status="failed", error="database is locked")
    cust_f = clip(t1, status="failed", error="database is locked")
    cust_late = clip(t1, status="queued", publish_at=NOW - timedelta(hours=3), output_path=str(FILE))
    run(at=NOW); run(at=NOW + timedelta(minutes=31))
    ok(get(own_f).status == "rendering" and get(cust_f, t1).status == "failed" and get(cust_late, t1).publish_at < NOW,
       "a round in the owner's copy never touches a customer's clips")
    run(t1, at=NOW); run(t1, at=NOW + timedelta(minutes=31))
    ok(get(cust_f, t1).status == "rendering" and get(cust_late, t1).publish_at > NOW, "the customer's own round fixes the customer's clips")
    ok(fixed(t1, "retry") and all(f.get("clip") != own_f for f in fixed(t1)), "each account has its own Fixed-by-itself list")
    sw(a1, "watchdog.repair", False)
    with tenant.use(t1):
        ok(not watchdog.enabled() and watchdog.run() == {"off": True}, "Super Admin switch 'Self-repair' off for that account: nothing runs there")
    ok(c1.post("/api/watchdog/check").status_code == 403, "and the customer's Check now is refused")
    ok(watchdog.enabled(), "the owner's copy is not affected")
    sw(a1, "watchdog.repair", True)

    # ============================================================ messages: e-mail always, phone, Telegram, WhatsApp
    reset(); mails.clear(); net.clear()
    set_setting("gmail_email", "owner@example.com")
    set_setting("tg_token", __import__("app.crypto", fromlist=["encrypt"]).encrypt("123:abc")); set_setting("tg_chat", "42"); set_setting("tg_on", "1")
    phone.setup()
    owner.post("/api/whatsapp", json={"action": "save", "phone": "+91 70000 00123", "apikey": "987654"})
    ok(telegram.enabled() and phone.enabled() and whatsapp.enabled(), "(Telegram, phone app and WhatsApp are set up)")
    sent = watchdog.deliver("Test problem: press this button.")
    ok(sent.get("mail") and len(mails) == 1, "e-mail always, also when Telegram is set up")
    ok(any(m == "POST" and "ntfy" in u for m, u, _ in net) and any(m == "POST" and "telegram" in u for m, u, _ in net)
       and any(m == "GET" and "callmebot" in u for m, u, _ in net), "also to the phone (ntfy), Telegram and WhatsApp")
    day = utcnow().strftime("%Y-%m-%d")
    set_setting("gmail_owner:" + day, "40")
    mails.clear(); watchdog.deliver("x")
    ok(len(mails) == 1, "the usual 40-a-day mail limit used up: the watchdog's mail still goes (own count)")
    set_setting("gmail_owner_wd:" + day, str(gmail.WATCHDOG_PER_DAY))
    mails.clear(); watchdog.deliver("x")
    ok(not mails, "the watchdog's own daily mail cap holds")
    st0 = state(); st0["alerts_today"] = watchdog.ALERTS_PER_DAY
    with tenant.use(1):
        ok(watchdog._alert(st0, "k:new", "text", utcnow()) is False, "at most 12 watchdog messages a day per account")

    # ============================================================ WhatsApp card
    for k in ("wa_phone", "wa_key", "wa_on", "wa_sent:" + day):
        set_setting(k, "")
    net.clear(); logbuf.truncate(0)
    r = owner.post("/api/whatsapp", json={"action": "save", "phone": "12ab", "apikey": "k"})
    ok(r.status_code == 400 and "12ab" not in r.text, "a wrong number is refused, without echoing it")
    r = owner.post("/api/whatsapp", json={"action": "save", "phone": "+91 70000 00123", "apikey": "secretKEY77"})
    st = owner.get("/api/whatsapp").json()
    ok(r.status_code == 200 and st["set"] and st["on"] and st["phone"] == "•••123", "saved: only the last 3 digits are shown")
    blob = json.dumps([r.json(), st])
    ok("7000000123" not in blob and "secretKEY77" not in blob, "the number and the key are never in an answer")
    ok("7000000123" not in (get_setting("wa_phone") or "") and "secretKEY77" not in (get_setting("wa_key") or "")
       and (get_setting("wa_phone") or "").startswith("enc:"), "both are stored encrypted")
    ok(owner.post("/api/whatsapp/test").status_code == 200 and net and net[-1][2]["phone"] == "+917000000123" and net[-1][2]["apikey"] == "secretKEY77",
       "Test: one message through CallMeBot (faked here)")
    for _ in range(12):
        whatsapp.send("x")
    ok(whatsapp.sent_today() == 10 and owner.post("/api/whatsapp/test").status_code == 429, "at most 10 a day")
    set_setting("wa_sent:" + day, "0")
    GET_FAIL.append(1); whatsapp.send("x"); GET_FAIL.clear()
    with acl.CoreSession() as s:
        acts = " ".join(a.detail or "" for a in s.query(acl.Activity).all())
    logs = logbuf.getvalue()
    ok("7000000123" not in logs and "secretKEY77" not in logs and "whatsapp: not sent (ConnectError)" in logs,
       "a failed send logs only the kind of error, never the number, the key or the address")
    ok("7000000123" not in acts and "secretKEY77" not in acts, "never in the activity log")
    errs_txt = json.dumps(guide.recent_errors())
    ok("7000000123" not in errs_txt and "secretKEY77" not in errs_txt, "never in System errors")
    owner.post("/api/whatsapp", json={"action": "off"})
    net.clear(); whatsapp.send("x")
    ok(not net and not owner.get("/api/whatsapp").json()["on"], "turned off: nothing is sent")
    owner.post("/api/whatsapp", json={"action": "on"})
    # needs-you alerts go there too; nothing else does
    net.clear()
    telegram.notify("⬆️ Uploaded: something")
    ok(not any("callmebot" in u for _m, u, _p in net), "ordinary messages (uploads ...) never go to WhatsApp")
    set_setting("phone_baseline", NOW.isoformat())
    set_setting("phone_bad_prev", json.dumps(["no_channel_x"]))
    orig_health = main.health
    main.health = lambda: {"items": [{"key": "no_channel_x", "level": "bad", "text": "Something needs you"}]}
    net.clear(); phone.events()
    main.health = orig_health
    ok(any("callmebot" in u and "needs you" in p["text"] for _m, u, p in net if isinstance(p, dict) and "text" in p),
       "a 'needs you' alert also goes to WhatsApp")
    # per account, and the Super Admin's switch
    ok(c1.get("/api/whatsapp").json()["set"] is False, "a customer has their own (empty) WhatsApp card")
    sw(a1, "whatsapp.use", False)
    ok(c1.post("/api/whatsapp", json={"action": "save", "phone": "+91 70000 00999", "apikey": "abc123"}).status_code == 403,
       "switched off by the Super Admin: the customer cannot set it up")
    with tenant.use(t1):
        set_setting("wa_phone", whatsapp.encrypt("+917000000999")); set_setting("wa_key", whatsapp.encrypt("abc123"))
        net.clear()
        ok(not whatsapp.enabled() and whatsapp.send("x") is False and not net, "and nothing is sent for that account")
    sw(a1, "whatsapp.use", True)
    src = open(os.path.join(CODE, "app", "whatsapp.py"), encoding="utf-8").read() + open(os.path.join(CODE, "static", "index.html"), encoding="utf-8").read()
    ok(not re.search(r"\+\d[\d ]{9,}\d", src), "no phone number is written anywhere in the code or the page")

    # ============================================================ System errors clear themselves
    only("errors"); reset(); guide.clear_errors()
    iso = lambda dt: dt.isoformat() + "Z"
    rows = [
        {"at": "2026-11-24T17:20:00Z", "where": "GET /api/clips/56", "type": "TimeoutError", "line": "base.py:1",
         "text": "QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00", "count": 2},
        {"at": "2026-11-22T09:00:00Z", "where": "Reading watch time from YouTube", "type": "HttpError", "line": "stats.py:9",
         "text": '<HttpError 500 when requesting https://youtubeanalytics.googleapis.com/v2/reports returned "Internal error encountered.". Details: "backendError">', "count": 1},
        {"at": iso(utcnow() - timedelta(hours=1)), "where": "timed job: sync_all", "type": "OperationalError", "line": "x.py:1",
         "text": "(sqlite3.OperationalError) database is locked", "count": 1},
        {"at": iso(utcnow() - timedelta(days=4)), "where": "timed job: odd", "type": "KeyError", "line": "y.py:2", "text": "'zz'", "count": 3},
        {"at": iso(utcnow() - timedelta(days=1)), "where": "timed job: odd2", "type": "KeyError", "line": "y.py:3", "text": "'yy'", "count": 1},
    ]
    set_setting("guide_errors", json.dumps(rows))
    before = owner.get("/api/alerts/board").json()
    run()
    b = owner.get("/api/alerts/board").json()
    titles = [i["title"] for i in b["tabs"]["system"]]
    lines = [f["t"] for f in fixed(r="errors")]
    ok(not any("/api/clips/56" in t for t in titles) and not any("watch time" in t for t in titles),
       "the live 24 Nov pool timeout and the 22 Nov Google 500 are resolved on the first run")
    ok(any(l == "A page action failed: GET /api/clips/56: a short database rush on 24 Nov — it did not come back." for l in lines)
       and any("Reading watch time from YouTube: a passing hiccup at Google / YouTube on 22 Nov — it did not come back." in l for l in lines),
       f"each one is a plain line in Fixed by itself ({lines})")
    ok(any("sync_all" in t for t in titles), "a passing fault from an hour ago is still shown (6 hours not over)")
    ok(not any("odd" == t[-3:] for t in titles) and any(q["title"].endswith("odd") for q in b["self_repair"]["quiet"]),
       "any other error quiet for 3 days moves to 'Seen before, quiet now' (kept, not deleted)")
    ok(any(t.endswith("odd2") for t in titles), "an error from yesterday stays in the list")
    ok(b["counts"]["system"] < before["counts"]["system"] and b["totals"]["system"] == before["totals"]["system"] - 3,
       f"they no longer count ({before['counts']['system']} -> {b['counts']['system']})")
    ok(len(json.loads(get_setting("guide_errors"))) == 5, "nothing is deleted from the error log")
    run()
    ok(len(fixed(r="errors")) == len(lines), "idempotent: resolved once")
    back = json.loads(get_setting("guide_errors"))
    back[0]["at"], back[0]["count"] = iso(utcnow()), 3  # the pool timeout happens again
    set_setting("guide_errors", json.dumps(back))
    b = owner.get("/api/alerts/board").json()
    ok(any("/api/clips/56" in i["title"] for i in b["tabs"]["system"]), "a resolved error that comes back shows again normally")
    run()
    ok(any("/api/clips/56" in i["title"] for i in owner.get("/api/alerts/board").json()["tabs"]["system"]),
       "...and is not resolved again until it has been quiet for 6 hours")
    ok(watchdog.classify("QueuePool limit of size 5 overflow 10 reached, connection timed out") == "transient",
       "a clip that failed on a pool timeout is tried again")
    guide.clear_errors()

    # ============================================================ database pool + dashboard read gate
    from app import db as dbm
    ok(dbm.pool_options("sqlite:///x.db") == {"pool_pre_ping": True}, "SQLite keeps its own pool defaults")
    po = dbm.pool_options("postgresql://u:p@h/db")
    ok(po["pool_size"] == 10 and po["max_overflow"] == 20 and po["pool_timeout"] == 10 and po["pool_pre_ping"]
       and po["pool_size"] + po["max_overflow"] <= 90, f"PostgreSQL: 10 + 20 connections (inside Railway's 100), 10 s timeout ({po})")
    os.environ["DB_POOL_SIZE"] = "4"
    ok(dbm.pool_options("postgresql://u:p@h/db")["pool_size"] == 4, "the pool can be changed with DB_POOL_SIZE")
    del os.environ["DB_POOL_SIZE"]
    import threading
    gate_clip = clip(status="queued", publish_at=NOW + timedelta(days=30))
    orig_gate, orig_wait, orig_get = main._READ_GATE, main.READ_WAIT, main._get
    main._READ_GATE, main.READ_WAIT = threading.BoundedSemaphore(2), 0.4
    live, peak, lock = [0], [0], threading.Lock()

    def slow_get(s, model, i):
        with lock:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(1.0)
        with lock:
            live[0] -= 1
        return orig_get(s, model, i)

    main._get = slow_get
    codes = []
    th = [threading.Thread(target=lambda: codes.append(owner.get(f"/api/clips/{gate_clip}").status_code)) for _ in range(6)]
    [x.start() for x in th]; [x.join() for x in th]
    main._get = orig_get
    ok(peak[0] <= 2 and codes.count(200) >= 2 and 503 in codes, f"a burst of clip reads: at most 2 at once, the rest are told to try again ({sorted(codes)}, peak {peak[0]})")
    ok(owner.get(f"/api/clips/{gate_clip}").status_code == 200, "after the burst the slots are free again")
    ok(owner.get("/api/health").status_code == 200, "other pages are not held by the clip-read limit")
    main._READ_GATE, main.READ_WAIT = orig_gate, orig_wait

    # ============================================================ last checks
    ok(not uploaded, "nothing was ever uploaded by the watchdog")
    ok(not any(str(e.get("where", "")).startswith("watchdog:") for e in guide.recent_errors()), "no remedy raised an error")
    with SessionLocal() as s:
        ok(all(c.publish_at and c.publish_at > utcnow() for c in s.query(Clip).filter(Clip.status == "queued", Clip.pc_item_id.is_(None),
                                                                                     Clip.id.notin_([soon]))), "every queued clip has a future time (none unscheduled)")

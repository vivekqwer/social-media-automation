"""First comment after upload: posted once when public, scheduled waits, old login skipped, quota, permanent errors,
per-clip off, placeholders, notification, routes. The Google API is mocked: nothing is sent to Google."""
import json, os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"
os.environ["APP_PASSWORD"] = "comment-test-pass-71"; os.environ["KEYWORDS_OFFLINE"] = "1"; os.environ["MAX_UPLOADS_PER_DAY"] = "100"
WT = r"D:\integ-final"
sys.path.insert(0, WT)
import httplib2
from googleapiclient.errors import HttpError
from fastapi.testclient import TestClient
from app import main, comments, youtube, telegram, phone, gmail, quota, scheduler, acl, alerts
from app.crypto import encrypt
from app.db import SessionLocal, Destination, Clip, Project, utcnow, set_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)

said, mailed, pushed = [], [], []
telegram.enabled = lambda: True
telegram._call = lambda method, **k: said.append(k.get("text", ""))
gmail.notify_owner = lambda text: mailed.append(text)
phone.push = lambda *a, **k: pushed.append((a, k)) or True
phone.enabled = lambda: True
main.executor.submit = lambda *a, **k: None


class Exec:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class FakeThreads:
    def __init__(self, yt): self.yt = yt
    def list(self, **k):
        self.yt.calls.append(("list", k["videoId"]))
        return Exec(lambda: {"items": [x for x in self.yt.existing.get(k["videoId"], [])]})
    def insert(self, part, body):
        vid = body["snippet"]["videoId"]
        self.yt.calls.append(("insert", vid, body["snippet"]["topLevelComment"]["snippet"]["textOriginal"]))
        def run():
            if self.yt.fail.get(vid):
                raise self.yt.fail[vid]
            return {"id": "Ugx" + vid}
        return Exec(run)


class FakeYT:
    def __init__(self): self.calls, self.existing, self.fail = [], {}, {}
    def commentThreads(self): return FakeThreads(self)


YT = FakeYT()
STATUS = {}  # video id -> privacyStatus (missing = deleted)
status_calls = []
youtube.comment_client = lambda did: YT
def fake_status(did, ids):
    status_calls.append(list(ids))
    return {i: {"id": i, "status": {"privacyStatus": STATUS[i], "uploadStatus": "processed"}} for i in ids if i in STATUS}
youtube.video_status = fake_status
uploaded = []
def fake_upload(did, path, title, desc, tags, publish_at=None):
    vid = f"VID{len(uploaded):08d}"; uploaded.append((vid, publish_at)); return vid
youtube.upload = fake_upload


def http_err(status, reason, msg="refused"):
    return HttpError(httplib2.Response({"status": status}), json.dumps({"error": {"code": status, "message": msg,
                                                                               "errors": [{"reason": reason}]}}).encode())


def tok(scopes):
    info = {"token": "t", "refresh_token": "r", "client_id": "cid", "client_secret": "sec", "token_uri": "https://oauth2.googleapis.com/token"}
    if scopes is not None:
        info["scopes"] = scopes
    return encrypt(json.dumps(info))


NEW = youtube.SCOPES
OLD = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]

with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "comment-test-pass-71"})
    # ---------- scope change
    ok(youtube.COMMENT_SCOPE in youtube.SCOPES and "https://www.googleapis.com/auth/youtube.upload" in youtube.SCOPES
       and youtube.ANALYTICS_SCOPE in youtube.SCOPES, "new connections ask for youtube.force-ssl too (upload/analytics kept)")
    f = youtube._flow()
    ok(youtube.COMMENT_SCOPE in (f.oauth2session.scope or []), "the Google sign-in flow asks for the comment scope")
    with SessionLocal() as s:
        s.add(Destination(channel_id="UCnew", name="Foody", token=tok(NEW)))
        s.add(Destination(channel_id="UCold", name="Old Channel", token=tok(OLD)))
        s.add(Destination(channel_id="UCvery", name="Very old", token=tok(None)))
        pr = Project(url="u", video_id="abcdefghijk", title="Src", mode="original", duration=600); s.add(pr); s.commit()
        PID = pr.id
        NEWD, OLDD, VOLD = [x.id for x in s.query(Destination).order_by(Destination.id)]
    ok(youtube.can_comment(NEWD) and not youtube.can_comment(OLDD) and youtube.can_comment(VOLD),
       "can_comment: new login yes, older login (no force-ssl) no, login without stored scopes is tried")
    from google.oauth2.credentials import Credentials
    from app.crypto import decrypt
    with SessionLocal() as s:
        c_old = Credentials.from_authorized_user_info(json.loads(decrypt(s.get(Destination, OLDD).token)))
    ok(list(c_old.scopes) == OLD, "an existing login keeps (and refreshes with) the scopes it was made with")
    dl = {x["id"]: x for x in tc.get("/api/destinations").json()}
    ok(dl[NEWD]["can_comment"] is True and dl[OLDD]["can_comment"] is False, "My channels shows which channels can comment")

    # ---------- settings: default on, presets, placeholders, validation
    st = tc.get("/api/settings/comment").json()
    ok(st["on"] is True and st["preset"] == "ask" and len(st["presets"]) >= 5 and any("कमेंट" in p["text"] for p in st["presets"]),
       "default ON with presets incl. a Hindi one")
    ch = {c["name"]: c["can_comment"] for c in st["channels"]}
    ok(ch == {"Foody": True, "Old Channel": False, "Very old": True}, "settings list which channels need a reconnect")
    ok(tc.post("/api/settings/comment", json={"on": True, "preset": "nope"}).status_code == 400, "unknown preset refused")
    ok(tc.post("/api/settings/comment", json={"on": True, "preset": "custom", "custom": " "}).status_code == 400, "empty own comment refused")
    ok(tc.post("/api/settings/comment", json={"on": True, "preset": "custom", "custom": "x" * 600}).status_code == 400, "over 500 characters refused")
    r = tc.post("/api/settings/comment", json={"on": True, "preset": "custom", "custom": "Loved {title}? More on {channel} 🔥\r\n\r\n\r\n\r\n{hashtags}\x07"}).json()
    ok(r["preset"] == "custom" and r["text"] == "Loved {title}? More on {channel} 🔥\n\n{hashtags}", "own comment saved, cleaned (no control chars, no 3 empty lines)")

    class C: pass
    c = C(); c.title = "Best #samosa in Delhi"; c.description = "credit\n#streetfood #Delhi #samosa"; c.tags = ["x"]; c.comment_text = None
    ok(comments.text_for(c, "Foody") == "Loved Best #samosa in Delhi? More on Foody 🔥\n\n#samosa #streetfood #Delhi",
       "placeholders {title} {channel} {hashtags} filled (hashtags from title/description, no doubles)")
    c.description, c.title, c.tags = "", "Kachori", ["street food", "चाट"]
    ok(comments.hashtags(c) == "#streetfood #चाट", "no hashtags in the text: made from the tags (Hindi kept)")
    c.comment_text = "Own words for {title}"
    ok(comments.text_for(c, "Foody") == "Own words for Kachori", "a clip's own words win over the account's")

    # ---------- an upload through the real queue marks the clip "waiting"
    out = os.path.join(d, "o.mp4"); open(out, "wb").write(b"x")
    def add_clip(dest, title, publish_at=None, **kw):
        with SessionLocal() as s:
            x = Clip(project_id=PID, start=0, end=30, aspect="9:16", status="queued", output_path=out, title=title,
                     destination_id=dest, publish_at=publish_at, description="#food", **kw)
            s.add(x); s.commit(); return x.id
    A = add_clip(NEWD, "Live now video")
    B = add_clip(NEWD, "Scheduled video", publish_at=utcnow() + timedelta(hours=5))
    scheduler.process_queue()
    with SessionLocal() as s:
        a, b = s.get(Clip, A), s.get(Clip, B)
        ok(a.status == "uploaded" and a.comment_state == "waiting" and a.comment_next is None, "upload done -> comment waiting")
        ok(b.status == "uploaded" and b.comment_state == "waiting" and b.comment_next == b.publish_at, "scheduled upload -> waits for its publish time")
        AV, BV = a.youtube_id, b.youtube_id
    STATUS[AV] = "public"; STATUS[BV] = "private"
    said.clear()
    res = comments.post_due()
    ins = [x for x in YT.calls if x[0] == "insert"]
    ok(res["posted"] == 1 and len(ins) == 1 and ins[0][1] == AV, "the live video gets its comment; the scheduled one is not touched")
    ok(ins[0][2].startswith("Loved Live now video? More on Foody"), "posted with the account's words, placeholders filled")
    ok(all(BV not in ids for ids in status_calls), "a scheduled video is not even looked at before its time")
    with SessionLocal() as s:
        a = s.get(Clip, A)
        ok(a.comment_state == "posted" and a.comment_id == "Ugx" + AV, "comment id stored, state posted")
    ok(said and "Comment posted on" in said[-1] and f"https://youtu.be/{AV}" in said[-1] and "⋮ → Pin" in said[-1],
       "notification: 'Comment posted on <title> — pin it: ⋮ → Pin' with the youtu.be link")
    ok(pushed and pushed[-1][1].get("url") == f"https://youtu.be/{AV}", "the phone gets it with the link to tap")
    n = len(YT.calls); comments.post_due(); comments.post_due()
    ok(not [x for x in YT.calls[n:] if x[0] == "insert"], "never posted twice (later runs do nothing for it)")
    # scheduled: publish time passes
    with SessionLocal() as s:
        s.get(Clip, B).publish_at = utcnow() - timedelta(minutes=1); s.get(Clip, B).comment_next = utcnow() - timedelta(minutes=1); s.commit()
    comments.post_due()
    with SessionLocal() as s:
        b = s.get(Clip, B)
        ok(b.comment_state.startswith("waiting") and "not public" in b.comment_state and not b.comment_id
           and b.comment_next > utcnow(), "publish time passed but YouTube still shows it private -> waits, no comment")
        b.comment_next = utcnow() - timedelta(minutes=1); s.commit()
    STATUS[BV] = "public"
    comments.post_due()
    with SessionLocal() as s:
        ok(s.get(Clip, B).comment_state == "posted", "once public, the scheduled video gets its comment")

    # ---------- the channel already has a comment there (crash after posting): no second one
    C1 = add_clip(NEWD, "Crash video"); scheduler.process_queue()
    with SessionLocal() as s:
        CV = s.get(Clip, C1).youtube_id
    STATUS[CV] = "public"
    YT.existing[CV] = [{"id": "UgxEARLIER", "snippet": {"topLevelComment": {"snippet": {"authorChannelId": {"value": "UCnew"}}}}}]
    n = len(YT.calls); comments.post_due()
    with SessionLocal() as s:
        x = s.get(Clip, C1)
        ok(x.comment_id == "UgxEARLIER" and x.comment_state == "posted" and not [c for c in YT.calls[n:] if c[0] == "insert"],
           "a comment by the channel itself is already there -> taken, nothing new posted")
    with SessionLocal() as s:  # a run that stopped between claim and save
        x = s.get(Clip, C1); x.comment_state, x.comment_id = "posting", None; s.commit()
    comments.recover_stuck(); comments.post_due()
    with SessionLocal() as s:
        ok(s.get(Clip, C1).comment_id == "UgxEARLIER", "a run stopped half-way is picked up again without a double comment")

    # ---------- older login: no permission, no API call; after reconnect it goes out by itself
    D1 = add_clip(OLDD, "Old channel video"); scheduler.process_queue()
    with SessionLocal() as s:
        DV = s.get(Clip, D1).youtube_id
    STATUS[DV] = "public"
    n, ns = len(YT.calls), len(status_calls)
    res = comments.post_due()
    with SessionLocal() as s:
        ok(s.get(Clip, D1).comment_state == "no permission" and len(YT.calls) == n and len(status_calls) == ns,
           "login without force-ssl -> 'no permission', no call to Google at all")
    ok(any(i["kind"] == "comment_permission" and "Reconnect the channel once" in i["text"] for i in alerts._clip_items(SessionLocal())),
       "Alerts: one plain item 'reconnect the channel once to allow comments'")
    with SessionLocal() as s:
        s.get(Destination, OLDD).token = tok(NEW); s.commit()
    comments.post_due()
    with SessionLocal() as s:
        ok(s.get(Clip, D1).comment_state == "posted", "after the channel is connected again the waiting comment is posted by itself")

    # ---------- permanent error: recorded, shown, not retried
    E1 = add_clip(NEWD, "Kids video"); scheduler.process_queue()
    with SessionLocal() as s:
        EV = s.get(Clip, E1).youtube_id
    STATUS[EV] = "public"; YT.fail[EV] = http_err(403, "commentsDisabled", "The video has comments disabled.")
    comments.post_due()
    with SessionLocal() as s:
        st_e = s.get(Clip, E1).comment_state
    ok(st_e.startswith("failed:") and "turned off" in st_e and "made for kids" in st_e, "comments disabled -> 'failed: …' in plain words")
    n = len(YT.calls); comments.post_due(); comments.post_due()
    ok(len(YT.calls) == n, "a permanent error is not tried again")
    items = alerts._clip_items(SessionLocal())
    ok(any(i["kind"] == "comment_failed" and i["clip_id"] == E1 and "turned off" in i["text"] for i in items), "shown in Alerts (Not working) in plain words")
    card = tc.get(f"/api/clips/{E1}").json()
    ok(card["comment_state"].startswith("failed") and "comment_preview" in card, "the clip shows the state")

    # ---------- transient error: retried a few times with longer gaps, then given up
    F1 = add_clip(NEWD, "Busy video"); scheduler.process_queue()
    with SessionLocal() as s:
        FV = s.get(Clip, F1).youtube_id
    STATUS[FV] = "public"; YT.fail[FV] = http_err(503, "backendError", "Backend Error")
    for i in range(comments.MAX_TRIES):
        comments.post_due()
        with SessionLocal() as s:
            x = s.get(Clip, F1)
            if i < comments.MAX_TRIES - 1:
                good = x.comment_state.startswith("waiting") and x.comment_next > utcnow() and x.comment_tries == i + 1
                x.comment_next = utcnow() - timedelta(seconds=1); s.commit()
                if not good:
                    break
    ok(good, "YouTube busy -> waits and tries again later (counted)")
    with SessionLocal() as s:
        ok(s.get(Clip, F1).comment_state.startswith("failed: tried 5 times"), "after 5 tries: failed, not tried forever")

    # ---------- quota: uploads keep priority
    G1 = add_clip(NEWD, "Quota video"); scheduler.process_queue()
    with SessionLocal() as s:
        GV = s.get(Clip, G1).youtube_id
    STATUS[GV] = "public"
    with SessionLocal() as s:  # put them in the queue without uploading (channel paused for the upload loop is not wanted)
        for i in range(6):
            s.add(Clip(project_id=PID, start=0, end=30, aspect="9:16", status="queued", output_path=out, title=f"w{i}",
                       destination_id=NEWD, publish_at=utcnow() + timedelta(hours=3)))
        s.commit()
    used = quota.used()
    quota.spend("read", units=max(0, quota.DAILY_UNITS - used - quota.RESERVE // 2 - 5 * quota.UPLOAD_COST - 40))
    ok(not quota.can_comment(comments.uploads_waiting()) and quota.can_comment(0), "budget left = only the waiting uploads -> no comment")
    n = len(YT.calls); comments.post_due()
    with SessionLocal() as s:
        ok(s.get(Clip, G1).comment_state == "waiting" and len(YT.calls) == n, "comment waits, nothing spent on it")
    with SessionLocal() as s:
        s.query(Clip).filter(Clip.title.like("w%"), Clip.status == "queued").update({"status": "draft"}, synchronize_session=False); s.commit()
    comments.post_due()
    with SessionLocal() as s:
        ok(s.get(Clip, G1).comment_state == "posted", "when the uploads no longer need the budget, the comment goes out")
    YT.fail.clear()
    # quota spent per comment
    H1 = add_clip(NEWD, "Cost video")
    from app import quota as q2
    q2.spend("read", units=-(quota.used() - 0))  # back to an empty day for the rest
    scheduler.process_queue()
    with SessionLocal() as s:
        HV = s.get(Clip, H1).youtube_id
    STATUS[HV] = "public"; u0 = quota.used(); comments.post_due()
    ok(quota.used() - u0 == quota.COST["comment"] + 1, "a comment costs 50 units (+1 for the duplicate check)")
    q2.spend("read", units=quota.DAILY_UNITS)
    ok(not quota.can_comment(0), "a used-up day: no comments at all")
    q2.spend("read", units=-quota.used())

    # ---------- per-clip off / account off / ACL switch off
    I1 = add_clip(NEWD, "Off clip", comment_on=False); scheduler.process_queue()
    with SessionLocal() as s:
        ok(s.get(Clip, I1).comment_state == "off", "per-clip switch off -> no comment")
    n = len(YT.calls); comments.post_due()
    ok(len(YT.calls) == n, "an 'off' clip is never commented")
    r = tc.post(f"/api/clips/{I1}/comment", json={"on": True, "text": "Late one {title}"}).json()
    ok(r["comment_state"] == comments.BY_HAND and r["comment_text"] == "Late one {title}" and r["comment_preview"] == "Late one Off clip",
       "editor: switch it on after the upload -> waiting, with own words and preview")
    with SessionLocal() as s:
        STATUS[s.get(Clip, I1).youtube_id] = "public"
    comments.post_due()
    ok([x for x in YT.calls if x[0] == "insert"][-1][2] == "Late one Off clip", "...and it is posted with those words")
    ok(tc.post(f"/api/clips/{I1}/comment", json={"text": "again"}).status_code == 409, "a posted comment cannot be changed here")
    tc.post("/api/settings/comment", json={"on": False, "preset": "ask", "custom": ""})
    J1 = add_clip(NEWD, "Account off"); scheduler.process_queue()
    with SessionLocal() as s:
        ok(s.get(Clip, J1).comment_state == "off", "account setting off -> no comment")
    tc.post("/api/settings/comment", json={"on": True, "preset": "ask", "custom": ""})
    real_feature = acl.feature
    acl.feature = lambda k: k != "uploads.comment"
    K1 = add_clip(NEWD, "ACL off"); scheduler.process_queue()
    with SessionLocal() as s:
        ok(s.get(Clip, K1).comment_state == "off", "Super Admin switch 'First comment' off -> no comment")
    ok(comments.post_due() == {"posted": 0, "waiting": 0, "no_permission": 0, "failed": 0}, "...and the job does nothing")
    acl.feature = real_feature
    # PUT (editor Save) keeps the per-clip words for clips not uploaded yet
    L1 = add_clip(NEWD, "Draft");
    with SessionLocal() as s:
        s.get(Clip, L1).status = "draft"; s.commit()
    tc.put(f"/api/clips/{L1}", json={"comment_on": True, "comment_text": "  Hi {channel}\r\n"})
    card = tc.get(f"/api/clips/{L1}").json()
    ok(card["comment_text"] == "Hi {channel}" and card["comment_preview"] == "Hi Foody" and card["status"] == "draft",
       "editor Save stores the clip's comment (no re-render needed)")
    # old uploads from before the feature are never commented
    with SessionLocal() as s:
        s.add(Clip(project_id=PID, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="OLDVIDEO001", destination_id=NEWD, title="old"))
        s.commit()
    STATUS["OLDVIDEO001"] = "public"; n = len(YT.calls); comments.post_due()
    ok(not [x for x in YT.calls[n:] if x[1] == "OLDVIDEO001"], "videos uploaded before this feature get no comment")
    # deleted video
    M1 = add_clip(NEWD, "Deleted"); scheduler.process_queue()
    comments.post_due()
    with SessionLocal() as s:
        ok(s.get(Clip, M1).comment_state == "failed: the video is no longer on YouTube", "video gone -> failed, plainly")
    # route table
    ok(acl.rule_for("POST", "/api/settings/comment")[0] == "uploads.comment" and acl.rule_for("POST", "/api/clips/5/comment")[0] == "uploads.comment"
       and acl.rule_for("GET", "/api/settings/comment")[0] == acl.SIGNED, "acl ROUTES lines for the new routes")
    ok("uploads.comment" in acl.DEFAULT and acl.DEFAULT["uploads.comment"] is True, "a Super Admin switch 'First comment under new videos' (on by default)")

page = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
ok("YouTube does not let any app pin a comment" in page and "tap ⋮ next to your comment → Pin" in page, "the page says plainly that pinning is by hand")
ok("reconnect once to allow comments" in page and 'id="e-fc-box"' in page and "'uploads.comment':" in page, "reconnect hint, editor box, hidden when switched off")
for name in ("fc-on", "fc-preset", "fc-custom", "fc-prev", "fc-save", "fc-chs", "fc-badge", "e-fc-on", "e-fc-text", "e-fc-note", "e-fc-save"):
    if page.count(f'id="{name}"') != 1:
        ok(False, f"id {name} appears {page.count(chr(105) + 'd=' + name)} times")
for f in os.listdir(os.path.join(WT, "app")):
    if f.endswith(".py") and b"\x08" in open(os.path.join(WT, "app", f), "rb").read():
        ok(False, "backspace byte in " + f)

# ================= review fixes =================
from app import tenant, accounts
q2.spend("read", units=-quota.used())
def public_clip(title, **kw):
    cid = add_clip(NEWD, title, **kw); scheduler.process_queue()
    with SessionLocal() as s:
        v = s.get(Clip, cid).youtube_id
    STATUS[v] = "public"; return cid

# 1. shared budget: a customer on the owner's Google project counts too
accounts.active_tenants = lambda: [tenant.OWNER, 7, 8]
with tenant.use(7):
    with SessionLocal() as s:
        for i in range(6):
            s.add(Clip(project_id=1, start=0, end=30, aspect="9:16", status="queued", title=f"cust{i}", publish_at=utcnow() + timedelta(hours=2)))
        s.commit()
with tenant.use(8):
    set_setting("google_client_id", "own-keys.apps.googleusercontent.com")
    with SessionLocal() as s:
        for i in range(6):
            s.add(Clip(project_id=1, start=0, end=30, aspect="9:16", status="queued", title=f"own{i}", publish_at=utcnow() + timedelta(hours=2)))
        s.commit()
ok(comments.budget_tenants() == [1, 7], "owner's budget = owner + customers without own keys (not the one with own keys)")
with tenant.use(8):
    ok(comments.budget_tenants() == [8] and comments.uploads_waiting() == 6, "a customer with own keys counts only its own uploads")
with tenant.use(7):
    ok(comments.budget_tenants() == [1, 7], "a customer on the shared project counts the owner's uploads too")
ok(comments.uploads_waiting() == 6, "owner's view counts the shared customer's 6 waiting uploads (not the own-keys customer's)")
N1 = public_clip("Shared budget video")
quota.spend("read", units=quota.DAILY_UNITS - quota.used() - quota.RESERVE // 2 - 5 * quota.UPLOAD_COST - 40)
n = len(YT.calls); comments.post_due()
with SessionLocal() as s:
    ok(s.get(Clip, N1).comment_state == "waiting" and len(YT.calls) == n,
       "owner's comment waits: the budget left is needed by a customer's uploads on the same project")
with tenant.use(7):
    with SessionLocal() as s:
        s.query(Clip).update({"status": "draft"}); s.commit()
comments.post_due()
with SessionLocal() as s:
    ok(s.get(Clip, N1).comment_state == "posted", "customer's uploads gone -> the comment goes out")
q2.spend("read", units=-quota.used())
with tenant.use(7):  # uploads that will not go up today are not counted
    with SessionLocal() as s:
        s.query(Clip).update({"status": "queued"}); s.commit()
    real_cap, real_block = acl.uploads_cap, acl.upload_block
    acl.uploads_cap = lambda n: 2
    ok(comments._uploads_due_here() == 2, "no more than the account's uploads-a-day limit is counted")
    acl.uploads_cap = real_cap; acl.upload_block = lambda: "paused by the administrator"
    ok(comments._uploads_due_here() == 0, "uploads paused for the account -> none counted")
    acl.upload_block = real_block
    with SessionLocal() as s:
        s.query(Clip).update({"status": "draft"}); s.commit()

# 2. scheduled more than 14 days ahead: still gets its comment
P1 = add_clip(NEWD, "Far future", publish_at=utcnow() + timedelta(hours=1)); scheduler.process_queue()
with SessionLocal() as s:
    x = s.get(Clip, P1); PV = x.youtube_id
    ok(x.status == "uploaded" and x.comment_state == "waiting", "scheduled upload waiting for its comment")
    x.attempted_at = utcnow(); x.publish_at = x.comment_next = utcnow() + timedelta(days=20); s.commit()
comments.post_due()
with SessionLocal() as s:  # (an upload 20 days before its publish time, e.g. with a long upload lead)
    x = s.get(Clip, P1)
    ok(x.comment_state == "waiting", "publish time 20 days ahead: still waiting, not given up")
    x.attempted_at = utcnow() - timedelta(days=20); x.publish_at = utcnow() - timedelta(minutes=2); x.comment_next = x.publish_at; s.commit()
STATUS[PV] = "public"; comments.post_due()
with SessionLocal() as s:
    ok(s.get(Clip, P1).comment_state == "posted", "uploaded 20 days ago, public since minutes: comment posted (14 days count from publish)")
P2 = public_clip("Really old")
with SessionLocal() as s:
    x = s.get(Clip, P2); x.attempted_at = utcnow() - timedelta(days=30); x.publish_at = utcnow() - timedelta(days=15); s.commit()
comments.post_due()
with SessionLocal() as s:
    ok(s.get(Clip, P2).comment_state.startswith("failed: the video did not become public"), "more than 14 days after publish without a comment -> given up")

# 3. account switch off holds waiting comments; a clip's own ON does not override it
Q1 = public_clip("Held video")
with SessionLocal() as s:
    ok(s.get(Clip, Q1).comment_state == "waiting", "waiting before the switch is turned off")
comments.save(False, "ask", "")
with SessionLocal() as s:
    s.get(Clip, Q1).comment_on = True; s.commit()
n = len(YT.calls); res = comments.post_due()
with SessionLocal() as s:
    ok(s.get(Clip, Q1).comment_state == "waiting" and len(YT.calls) == n and res["waiting"] >= 1,
       "account switch off: nothing posted (not even with the clip's own ON), comment stays waiting")
ok(main.get_clip(Q1)["comment_account_on"] is False, "the editor knows the account switch is off (shows 'Held')")
Q2 = add_clip(NEWD, "Old held"); scheduler.process_queue()
with SessionLocal() as s:
    ok(s.get(Clip, Q2).comment_state == "off", "uploaded while off -> no comment for it")
    x = s.get(Clip, Q2); x.comment_state = "waiting"; x.attempted_at = utcnow() - timedelta(days=20); x.publish_at = None; s.commit()
comments.post_due()
with SessionLocal() as s:
    ok(s.get(Clip, Q2).comment_state == "off", "held past 14 days while off -> quietly 'off' (no Alerts noise)")
comments.save(True, "ask", "")
comments.post_due()
with SessionLocal() as s:
    ok(s.get(Clip, Q1).comment_state == "posted", "switched on again within 14 days -> the held comment is posted")
page = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
ok("not even for single clips" in page and "Held: first comments are switched off" in page, "the page explains what Off does")

# ================= UI check fixes =================
import subprocess
from app import guide
# old video (before the feature, no comment state), 30 days old: Save comment posts one now
R1 = public_clip("Before the feature")
with SessionLocal() as s:
    x = s.get(Clip, R1); x.comment_state = None; x.attempted_at = utcnow() - timedelta(days=30); x.publish_at = None; s.commit()
card = main.get_clip(R1)
ok(not card.get("comment_state") and card["status"] == "uploaded", "a pre-feature clip has no comment state")
main.clip_comment(R1, main.ClipCommentIn(on=True, text=None))
with SessionLocal() as s:
    ok(s.get(Clip, R1).comment_state == comments.BY_HAND, "Save comment on an old video -> waiting (asked by hand)")
comments.post_due()
with SessionLocal() as s:
    ok(s.get(Clip, R1).comment_state == "posted", "asked by hand on a 30-day-old video: posted, not given up")
R2 = public_clip("Before the feature, account off")
with SessionLocal() as s:
    x = s.get(Clip, R2); x.comment_state = None; x.attempted_at = utcnow() - timedelta(days=30); x.publish_at = None; s.commit()
main.clip_comment(R2, main.ClipCommentIn(on=True, text=None))
comments.save(False, "ask", "")
comments.post_due()
with SessionLocal() as s:
    ok(s.get(Clip, R2).comment_state == "off", "asked by hand but the account switch is off -> quietly 'off', not held forever")
comments.save(True, "ask", "")
# guide: entries of switched-off features are hidden
real_feature = acl.feature
acl.feature = lambda k: k != "uploads.comment"
qs = [h["q"] for h in guide.help_items()]
ok("The first comment is not pinned / was not posted" not in qs and len(qs) == len(guide.HELP) - 1, "First comment switched off -> its guide entry hidden")
acl.feature = real_feature
ok(len(guide.help_items()) == len(guide.HELP), "all switched on -> every guide entry shown")
ok(all(q in [h[0] for h in guide.HELP] for q in guide.HELP_FEATURE), "every hidden-entry key matches a real guide question")
# the preview renders exactly what the server posts
page = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
js = "\n".join(l for l in page.split("\n") if l.startswith("const fcClean=") or l.startswith("const fcFill=") or l.startswith("  .split('\\n')"))
samples = ["Line one\n\n\n\n\nLine two  \t end", "  {title}\r\n\r\n\r\n\r\n{channel}   {hashtags}\t", "a\x07b\tc\n\n\nd", "x" * 600 + "\n\n\n\ny"]
out = subprocess.run(["node", "-e", js + "\nconst S=JSON.parse(process.argv[1]);process.stdout.write(JSON.stringify(S.map(t=>fcFill(t,'Street food in Delhi','My channel','#streetfood #delhi'))))", json.dumps(samples)],
                     capture_output=True, text=True, encoding="utf-8")
got = json.loads(out.stdout or "null") if out.returncode == 0 else out.stderr
want = [comments.fill(comments.clean(t), "Street food in Delhi", "My channel", "#streetfood #delhi") for t in samples]
ok(got == want, f"dashboard preview == the text the server posts (blank lines, spaces, control chars, length) {got!r} {want!r}"[:300])
ok("no comments are posted while this is off" in page and "$('#fc-on').onchange=" in page, "Off toast matches the state; the switch saves by itself")
ok("This video was uploaded before first comments existed. Press Save comment to post one now." in page, "pre-feature clip says so plainly")
ok("its switch is off. Switch it on and press Save comment" in page and "as soon as the video is public:':'Posted after the upload, as soon as the video is public.'" in page,
   "clip switch off / no preview: a full sentence, never a dangling colon")

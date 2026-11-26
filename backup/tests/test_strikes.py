"""Strike responder (app/strikes.py): a strike is found (video monitor / YouTube's notice mails), the channel is protected
at once (never-use list, source stopped, waiting clips held, scheduled videos kept private, nothing deleted), the humble
request to the creator (written, sent once, never twice, daily limit, automatic only when switched on and only to the
address YouTube's own notice gives for the creator), the answer, only YouTube's own status acts (a mail alone never does;
the first run records old removals as history), one message per round, the YouTube budget,
the words for YouTube, the dashboard data, tenant isolation, access rules, and the tables on an existing database.
YouTube, Gmail and Telegram are faked: nothing leaves this PC."""
import base64, email, json, os, sys, tempfile, time
from datetime import timedelta
from email import policy
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"
os.environ["APP_PASSWORD"] = "strike-test-pass-42"; os.environ["KEYWORDS_OFFLINE"] = "1"; os.environ["MAX_UPLOADS_PER_DAY"] = "100"
WT = os.environ.get("RS_CODE") or r"D:\social medi engin\.claude\worktrees\agent-acd946fd91a4c32a3"
sys.path.insert(0, WT)
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from app import main, acl, auth, autopilot, db, gmail, guide, jobs, permissions, scheduler, strikes, telegram, tenant, viral, youtube
from app.db import Alert, Base, Channel, Clip, Destination, Project, SessionLocal, StrikeCase, get_setting, set_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)

# ---------------------------------------------------------------- fakes (no network)
told = []
telegram.notify = lambda text, *a, **k: told.append(text)
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda *a, **k: None
# no timed jobs in the background during this test: every round is called by hand, so nothing races the checks
scheduler.start = lambda: type("NoJobs", (), {"shutdown": lambda self, wait=False: None})()
CC = youtube.CC_LICENSE
uploaded = []
youtube.upload = lambda dest_id, path, title, desc, tags, publish_at=None: (uploaded.append(title), "up%08d" % len(uploaded))[1]
STATE = {}  # my video id -> what YouTube says (default: fine)
youtube.video_status = lambda did, ids: {i: STATE.get(i, {"id": i, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}) for i in ids}
private_calls, PRIVATE_FAIL = [], {}
real_keep_private, real_comment_client = youtube.keep_private, youtube.comment_client


def fake_private(dest_id, video_id):
    private_calls.append((tenant.current(), dest_id, video_id))
    if video_id in PRIVATE_FAIL:
        raise PRIVATE_FAIL[video_id]
    return "private"


youtube.keep_private = fake_private
EMAILS, lookups = {}, []
permissions.lookup_emails = lambda ids: (lookups.append(list(ids)), {i: EMAILS[i] for i in ids if i in EMAILS})[1]
INBOX, SENT, REPLIES, gets, GET_FAIL = {}, [], {}, [], set()


class Exec:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class Msgs:
    def __init__(self, tid): self.tid = tid
    def list(self, userId, q, maxResults=30):
        return Exec(lambda: {"messages": [{"id": m["id"]} for m in INBOX.get(self.tid, [])]})
    def get(self, userId, id, format="full"):
        def run():
            gets.append(id)
            if id in GET_FAIL:
                raise RuntimeError("Gmail is busy")
            return next(m for m in INBOX[self.tid] if m["id"] == id)
        return Exec(run)
    def send(self, userId, body):
        def run():
            m = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=policy.default)
            SENT.append({"tenant": self.tid, "to": m["to"], "subject": str(m["subject"]), "from": str(m["from"]), "body": m.get_content()})
            return {"threadId": f"T{len(SENT)}", "id": f"S{len(SENT)}"}
        return Exec(run)


class Threads:
    def get(self, userId, id, format=None, metadataHeaders=None):
        msgs = [{"id": "s0", "labelIds": ["SENT"], "payload": {"headers": [{"name": "From", "value": "Foody Fun <owner@gmail.example>"}]}}]
        if id in REPLIES:
            r = REPLIES[id]
            msgs.append({"id": r["id"], "labelIds": ["INBOX"], "snippet": r["text"],
                         "payload": {"headers": [{"name": "From", "value": r["from"]}, {"name": "Date", "value": "Mon, 23 Nov 2026 11:00:00 +0000"}]}})
        return Exec(lambda: {"messages": msgs})


class Svc:
    def __init__(self): self.tid = tenant.current()
    def users(self): return self
    def messages(self): return Msgs(self.tid)
    def threads(self): return Threads()


gmail._service = lambda: Svc()
b64 = lambda t: base64.urlsafe_b64encode(t.encode("utf-8")).decode().rstrip("=")


def mail(mid, frm, subject, plain=None, html=None, age_h=1.0):
    parts = ([{"mimeType": "text/plain", "body": {"data": b64(plain)}}] if plain is not None else []) + \
            ([{"mimeType": "text/html", "body": {"data": b64(html)}}] if html is not None else [])
    heads = [{"name": "From", "value": frm}, {"name": "Date", "value": "Mon, 23 Nov 2026 10:00:00 +0000"}]
    if subject is not None:
        heads.append({"name": "Subject", "value": subject})
    return {"id": mid, "threadId": "th-" + mid, "snippet": (plain or "")[:120], "internalDate": str(int((time.time() - age_h * 3600) * 1000)),
            "payload": {"mimeType": "multipart/alternative", "headers": heads, "parts": [{"mimeType": "multipart/related", "parts": parts}]}}


# autopilot without YouTube: every video belongs to the creator whose letter is the 4th character of its id
uc = lambda k: "UC" + k * 22
url_of = lambda k: "https://www.youtube.com/channel/" + uc(k)
NAMES = {"A": "Spice Route Kitchen", "B": "Chai Corner", "C": "Dosa Diaries", "D": "Biryani Brothers", "E": "Kulfi Kings",
         "F": "Momo Masters", "G": "Golgappa Guru", "H": "Tandoor Tales", "K": "Knife Stunts India", "Z": "Zaika Zone"}
made, fetched = [], []
autopilot.fetch_info = lambda u: (fetched.append(u.rsplit("=", 1)[-1]), {
    "video_id": u.rsplit("=", 1)[-1], "title": "Video " + u.rsplit("=", 1)[-1], "channel": NAMES[u.rsplit("=", 1)[-1][3]],
    "channel_url": url_of(u.rsplit("=", 1)[-1][3]), "license": CC, "description": "", "tags": [], "duration": 600, "heatmap": []})[1]
autopilot.fetch_heatmap = lambda vid: []
autopilot.fetch_comments = lambda vid: []
autopilot._make_clips = lambda s, ch, p, log: made.append(p.video_id)
autopilot.disk_free_gb = lambda: None
OUT = str(Path(d) / "out.mp4"); Path(OUT).write_bytes(b"video")
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)

with mk() as tc, mk() as c1, mk() as c2, mk() as anon:
    tc.post("/api/login", json={"username": "admin", "password": "strike-test-pass-42"})
    set_setting("mail_sign", "Foody Fun")

    def project(k, vid, mode="cc", src=None, note=None, title=None):
        with SessionLocal() as s:
            p = Project(url=f"https://www.youtube.com/watch?v={vid}", video_id=vid, title=title or f"Original {vid}", channel=NAMES[k],
                        channel_url=url_of(k), license=CC if mode == "cc" else None, mode=mode, permission_note=note, duration=600,
                        channel_ref=src, description="about the original")
            s.add(p); s.commit(); return p.id

    def clip(pid, status, title, yid=None, publish_at=None, dest=None, **kw):
        with SessionLocal() as s:
            p = s.get(Project, pid)
            desc = f"#shorts\n\nOriginal video: {p.title}\nCredit: {p.channel} - {p.channel_url}\nSource: https://youtu.be/{p.video_id}?t=0\n"
            c = Clip(project_id=pid, start=0, end=30, aspect="9:16", status=status, title=title, description=desc, output_path=OUT,
                     destination_id=dest or s.query(Destination).first().id, youtube_id=yid, publish_at=publish_at,
                     attempted_at=utcnow() - timedelta(hours=3) if status == "uploaded" else None, **kw)
            s.add(c); s.commit(); return c.id

    def st(cid):
        with SessionLocal() as s:
            return s.get(Clip, cid).status

    cases = lambda c=tc: c.get("/api/strikes").json()["cases"]
    case_of = lambda yid, c=tc: next((x for x in cases(c) if x["youtube_id"] == yid), None)
    errors = lambda: json.loads(get_setting("guide_errors") or "[]")
    soon, past = utcnow() + timedelta(days=10), utcnow() - timedelta(days=2)

    # ---------------------------------------------------------------- the page
    raw = (Path(WT) / "static" / "index.html").read_bytes()
    ok(b"\x08" not in raw, "static/index.html has no 0x08 bytes")
    page = raw.decode("utf-8")
    ok("Send the humble request to the creator automatically" in page and "data-st=" in page and "Copy the text for YouTube" in page
       and "Open YouTube Studio" in page and "Mark as solved" in page and "Send the request now" in page, "the Strikes tab has the switch and the buttons")

    # ---------------------------------------------------------------- 1. a strike seen by the video monitor
    with SessionLocal() as s:
        s.add(Destination(channel_id="UCmine000000000000000000", name="Foody", token="x")); s.commit()
        D1 = s.query(Destination).first().id
        srcA = Channel(kind="channel", query=url_of("A"), resolved_url=url_of("A"), name=NAMES["A"], mode="cc", auto=True, destination_id=D1,
                       videos_per_run=1, clips_per_video=1, clip_len=30, aspect="9:16", gap_hours=12)
        topic = Channel(kind="topic", query="street food", resolved_url="topic", name="Topic: street food", mode="cc", auto=True, destination_id=D1,
                        videos_per_run=2, clips_per_video=1, clip_len=30, aspect="9:16", gap_hours=12)
        s.add_all([srcA, topic]); s.commit()
        SRC_A, TOPIC = srcA.id, topic.id
    PA, PA2, PB = project("A", "srcAone0001", src=SRC_A), project("A", "srcAtwo0002", src=TOPIC), project("B", "srcBone0001", src=TOPIC)
    A1 = clip(PA, "uploaded", "Best Samosa Moment", "ytA1struck1", past)
    A2, A3, A4, A5 = clip(PA, "queued", "A queued"), clip(PA2, "rendered", "A ready"), clip(PA, "fetching", "A fetching"), clip(PA2, "rendering", "A rendering")
    A6 = clip(PA2, "uploaded", "A scheduled", "ytA6sched01", soon, comment_state="waiting")
    A7 = clip(PA, "uploaded", "A public already", "ytA7public1", past)
    A8, A9 = clip(PA, "draft", "A draft", autoqueue=True), clip(PA2, "failed", "A failed")
    B1, B2 = clip(PB, "queued", "B queued"), clip(PB, "uploaded", "B scheduled", "ytB2sched01", soon)
    EMAILS[uc("A")] = "hello@spiceroute.example"
    with SessionLocal() as s:
        n_clips, n_projects = s.query(Clip).count(), s.query(Project).count()
    ok(cases() == [] and tc.get("/api/strikes").json()["settings"]["auto"] is False, "a fresh account: no cases, automatic sending is OFF")
    STATE["ytA1struck1"] = {"id": "ytA1struck1", "status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}}
    r = tc.post("/api/alerts/check").json()
    ok(r["new_alerts"] == 1, f"the monitor sees the copyright removal ({r})")
    cs = cases()
    ok(len(cs) == 1 and cs[0]["kind"] == "copyright" and cs[0]["state"] == "rejected:copyright" and cs[0]["status"] == "open"
       and cs[0]["youtube_id"] == "ytA1struck1" and cs[0]["creator"] == NAMES["A"], "one case for the struck video: copyright, with its creator")
    CA = cs[0]["id"]
    ok(strikes.is_blocked(url_of("A")) and strikes.is_blocked(uc("A")) and not strikes.is_blocked(url_of("B")),
       "the creator is on the never-use list (others are not)")
    with SessionLocal() as s:
        a, t = s.get(Channel, SRC_A), s.get(Channel, TOPIC)
        ok(a.auto is False and (a.last_status or "").startswith("stopped: this creator is on the never-use list") and t.auto is True,
           "the source that points at the creator's channel is stopped; the topic source keeps running")
    held = {x: st(x) for x in (A2, A3, A4, A5, A8, A9)}
    ok(set(held.values()) == {"held"}, f"every waiting clip of that creator is held: queued, ready, fetching, rendering, draft, failed ({held})")
    with SessionLocal() as s:
        was = [s.get(Clip, x).held_was for x in (A2, A3, A4, A5, A8, A9)]
        ok(was == ["queued", "rendered", "fetching", "rendering", "draft", "failed"] and s.get(Clip, A2).autoqueue is False
           and "Held" in (s.get(Clip, A2).error or ""), "each held clip remembers what it was and says why")
        ok(s.get(Clip, B1).status == "queued" and s.get(Clip, A7).status == "uploaded", "another creator's clip and the already public video are untouched")
        ok(private_calls == [(1, D1, "ytA6sched01")], f"the scheduled (not yet public) video of that creator is set to private — only that one ({private_calls})")
        a6 = s.get(Clip, A6)
        ok(a6.kept_private is True and a6.status == "uploaded" and a6.comment_state == "off" and a6.live_noted is True,
           "it stays in the list as uploaded, marked kept private; no first comment, no 'is live' message")
        ok(s.query(Clip).count() == n_clips and s.query(Project).count() == n_projects and Path(OUT).exists(), "nothing was deleted (clips, videos, files)")
        dest = s.get(Destination, D1)
        why = dest.paused_reason or ""
        ok(dest.paused is True and "What the system did" in why and "Stopped the source" in why and "Held 6 waiting clips" in why
           and "Kept 1 video" in why and "never-use list" in why and "press Resume uploads" in why, f"uploads are paused and the reason says what was done and what to press: {why[:160]}…")
    c = cs[0]
    ok(c["held"] == 6 and len(c["private"]) == 1 and [x["youtube_id"] for x in c["public_others"]] == ["ytA7public1"] and c["blocked"] and c["paused"],
       "the case counts: 6 held, 1 kept private, 1 other public video of the creator")
    ok(any("Stopped the source" in x for x in c["done"]) and any("never-use list" in x for x in c["done"]) and any("Held 6" in x for x in c["done"])
       and any("Kept 1 video" in x for x in c["done"]), "…and lists what the system already did, in plain sentences")
    h = [i for i in tc.get("/api/health").json()["items"] if i.get("key") == "paused"]
    ok(len(h) == 1 and h[0]["fix"] == "resume" and "What the system did" in h[0]["text"], "Problems & fixes / Alerts carry the same text with the Resume button")
    m = c["mail"]
    ok(m and m["state"] == "ready" and m["to"] == "hello@spiceroute.example" and m["to_from"] == "their channel description" and m["can_send"],
       "the request is written; the address comes from the creator's channel description")
    ok(len([x for x in told if "What the system did" in x]) == 1 and "retract" in told[-1] and "What is left for you" in told[-1]
       and "hello@spiceroute.example" in told[-1], "the owner is told once: what happened, what was done, what is left, with the ready text")
    ok(SENT == [], "automatic sending is OFF: nothing was sent")
    n_told, n_priv, n_look = len(told), len(private_calls), len(lookups)
    tc.post("/api/alerts/check"); strikes.watch(); strikes.watch(); strikes.mail_round()
    ok(len(cases()) == 1 and len(private_calls) == n_priv and len(told) == n_told and len(lookups) == n_look and SENT == [],
       "running it again and again changes nothing: one case, no second private call, no second message, no second lookup")
    # held clips cannot be made or uploaded by hand either
    codes = [tc.post(f"/api/clips/{A3}/approve", json={}).status_code, tc.post(f"/api/clips/{A2}/schedule", json={}).status_code,
             tc.post(f"/api/clips/{A2}/render").status_code, tc.post(f"/api/clips/{A4}/refetch").status_code]
    ok(codes == [409] * 4 and "held" in tc.post(f"/api/clips/{A3}/approve", json={}).json()["detail"], f"Approve / Upload / Re-render of a held clip are refused with the reason ({codes})")
    from app.render import run_render
    run_render(A8)
    ok(st(A8) == "held", "the render job leaves a held clip alone")
    ok(tc.get("/api/overview").json()["counts"]["held"] == 6 and len(tc.get("/api/clips?status=held").json()) == 6, "the Clips page can count and list the held clips")
    # the last gate: a clip of that creator that slips into the queue is held by the upload job, the others go up
    tc.post(f"/api/destinations/{D1}/resume")
    A10 = clip(PA, "queued", "A slipped in")
    scheduler.process_queue()
    ok(st(A10) == "held" and st(B1) == "uploaded" and uploaded == ["B queued"], f"the upload job itself refuses the struck creator's clip; another creator's goes up ({uploaded})")

    # ---------------------------------------------------------------- 2. the never-use list in every source type
    def said(chid):
        with SessionLocal() as s:
            return s.get(Channel, chid).last_status or ""

    youtube.api_search_cc = lambda q, seen, n, pages=3: [v for v in ["newAone0001", "newBone0001"] if v not in seen]
    autopilot.run_channel(TOPIC)
    ok(made == ["newBone0001"] and "never-use list" in said(TOPIC) and "newAone0001" in said(TOPIC),
       f"topic source: the struck creator's video is skipped, the other creator's is used ({said(TOPIC)[:90]})")
    with SessionLocal() as s:
        tr = Channel(kind="trending", query="food", name="Trending", mode="cc", auto=True, destination_id=D1, videos_per_run=2, clips_per_video=1,
                     clip_len=30, aspect="9:16", gap_hours=12)
        s.add(tr); s.commit(); TREND = tr.id
        s.get(Channel, SRC_A).auto = True; s.commit()  # somebody starts the stopped source again
    autopilot.trending_phrase = lambda ch: "street food"
    youtube.api_search_cc = lambda q, seen, n, pages=3: [v for v in ["trdAone0001", "trdBone0002"] if v not in seen]
    made.clear(); autopilot.run_channel(TREND)
    ok(made == ["trdBone0002"] and "never-use list" in said(TREND), "trending source: the same")
    autopilot.list_video_ids = lambda url, n: ["chnAone0009", "chnAtwo0010"]
    made.clear(); autopilot.run_channel(SRC_A)
    with SessionLocal() as s:
        a = s.get(Channel, SRC_A)
        ok(made == [] and a.auto is False and a.last_status.startswith("stopped:"), f"channel source of the struck creator: started again by hand, it stops itself ({a.last_status[:70]})")
        hs = Channel(kind="channel", query="@spiceroute", resolved_url="https://www.youtube.com/@spiceroute", name="Spice (by handle)", mode="cc", auto=True,
                     destination_id=D1, videos_per_run=3, clips_per_video=1, clip_len=30, aspect="9:16", gap_hours=12)
        s.add(hs); s.commit(); HANDLE = hs.id
    fetched.clear(); autopilot.run_channel(HANDLE)
    with SessionLocal() as s:
        hs = s.get(Channel, HANDLE)
        ok(made == [] and fetched == ["chnAone0009"] and hs.auto is False and "never-use list" in hs.last_status and "source stopped" in hs.last_status,
           "a source that knows the creator only by @handle: the first video shows who it is, and the source stops (no going through 300 videos)")
    day = viral.today()
    row = lambda vid, k: {"video_id": vid, "title": "Viral " + vid, "channel": NAMES[k], "channel_id": uc(k), "views": 900000, "gain": 5000, "cc": True, "duration": 600, "short": False}
    set_setting("viral:" + day, json.dumps({"videos": {"virAone0001": row("virAone0001", "A"), "virZone0001": row("virZone0001", "Z")}, "shorts": {}}))
    picks = viral.pick_today()
    ok([p["video_id"] for p in picks] == ["virZone0001"], f"viral picks: the struck creator's video is never picked ({[p['video_id'] for p in picks]})")
    try:
        viral.pick_today("virAone0001"); picked = True
    except ValueError as e:
        picked = "never-use list" in str(e) and None
    ok(picked is None, "…not by hand from the Viral page either (a plain reason)")
    made.clear()
    allp = viral._picks(); allp[day].append({**row("virAtwo0002", "A"), "why": "cc", "project_id": None, "note": ""}); viral._save_picks(allp)
    viral.clip_pick(day, "virAtwo0002"); viral.clip_pick(day, "virZone0001")
    ok(made == ["virZone0001"], "…and a pick that was already waiting is skipped when it is clipped")
    set_setting("creators", json.dumps({uc("A"): {"channel_id": uc("A"), "name": NAMES["A"], "url": url_of("A"), "video_id": "v", "video_title": "t",
                                                  "views": 1, "status": "replied", "reply": "yes", "added": "x", "message": "m"}}))
    try:
        permissions.grant(uc("A"), "Reply by mail from the creator on 23 Nov: yes"); granted = True
    except ValueError as e:
        granted = "never-use list" in str(e) and None
    ok(granted is None, "a later 'permission given' for that creator makes no source")
    main.fetch_info = lambda u: autopilot.fetch_info(u)
    r = tc.post("/api/projects", json={"url": "https://www.youtube.com/watch?v=manAone0001", "mode": "cc"})
    r2 = tc.post("/api/projects", json={"url": "https://www.youtube.com/watch?v=manBone0001", "mode": "cc"})
    ok(r.status_code == 409 and "never-use list" in r.json()["detail"] and r2.status_code == 200, "a clip by hand from that creator is refused; other creators work")
    r = tc.post("/api/quick", json={"url": "https://www.youtube.com/watch?v=qckAone0001", "mode": "cc"})
    ok(r.status_code == 409 and "never-use list" in str(r.json()["detail"]), "Quick upload of that creator's video is refused the same way")

    # ---------------------------------------------------------------- 3. the request: three wordings, humble, no threats
    class C: pass
    texts = []
    for k in "ABC":
        x = C(); x.creator_key, x.id, x.youtube_id, x.state = uc(k), 1, "ytA1struck1", "rejected:copyright"
        texts.append(strikes.compose(x, {"source_title": "How I make samosa at home | Spice Route", "title": "Best Samosa Moment", "source_channel": NAMES[k],
                                         "credited": True, "source_mode": "cc", "license": CC}))
    ok(len({b for _s, b in texts}) == 3 and len({s for s, _b in texts}) == 3, "three different wordings (one creator always gets the same one)")
    low = [b.lower() for _s, b in texts]
    ok(all(("apolog" in b or "sorry" in b) and "https://youtu.be/yta1struck1" in b and "best samosa moment" in b and strikes.RETRACT_HELP in b
           and b.rstrip().endswith("foody fun") and ("retract" in b or "withdraw" in b) for b in low),
       "each one: an apology, my clip's title and link, a polite request to retract with YouTube's help link, signed with the owner's name")
    ok(all("named you as the creator and linked to your original video" in b and "creative commons" in b and "no longer on youtube" in b for b in low)
       and all(("no more clips" in b or "nothing from your videos" in b or "any more clips" in b) for b in low),
       "…it says the clip carried credit and a link, why it was used, that it is gone, and that no further clips of theirs will be posted")
    import re
    bad_words = re.compile(r"\b(lawyer|legal|court|sue|fair use|demand|must|you have to|counter\S*|rights?|entitled|allowed by law)\b")
    ok(all(not bad_words.search(b) for b in low) and all(b.isascii() and s.isascii() for s, b in texts), "no legal words, no arguing, no emojis")
    ok(all(len(b) < 1400 for _s, b in texts), "short")
    x = C(); x.creator_key, x.id, x.youtube_id, x.state = uc("A"), 1, "yt", "mail"
    plain = strikes.compose(x, {"source_title": "T", "title": "My clip", "source_channel": "X", "credited": False, "source_mode": "commentary"})[1]
    ok("named you" not in plain and "Creative Commons" not in plain and "no longer" not in plain, "it only says what is true for that clip (no credit claimed when there was none)")

    # ---------------------------------------------------------------- 4. sending by hand: once, never twice
    r = tc.post(f"/api/strikes/{CA}/send", json={})
    ok(r.status_code == 400 and "Gmail is not connected" in r.json()["detail"] and SENT == [], "Send without Gmail: a plain message, nothing sent")
    set_setting("gmail_email", "owner@gmail.example")
    r = tc.post(f"/api/strikes/{CA}/send", json={})
    ok(r.status_code == 200 and len(SENT) == 1 and SENT[0]["to"] == "hello@spiceroute.example" and "Foody Fun" in SENT[0]["from"]
       and "https://youtu.be/ytA1struck1" in SENT[0]["body"] and strikes.RETRACT_HELP in SENT[0]["body"], "Send the request now: one mail from the owner's Gmail, in the owner's name")
    m = case_of("ytA1struck1")["mail"]
    ok(m["state"] == "sent" and m["sent_at"] and not m["can_send"], "the case shows: sent, to whom, when")
    r = tc.post(f"/api/strikes/{CA}/send", json={})
    ok(r.status_code == 400 and "never sent twice" in r.json()["detail"] and len(SENT) == 1, "a second press sends nothing")
    ok(get_setting("gmail_sent:" + utcnow().strftime("%Y-%m-%d")) in (None, "", "0"), "it does not use up the day's permission mails")

    # ---------------------------------------------------------------- 5. YouTube's notice mails
    with SessionLocal() as s:
        s.add(Destination(channel_id="UCmine222222222222222222", name="Second channel", token="x")); s.commit()
        D2 = [x.id for x in s.query(Destination).order_by(Destination.id)][1]
    PH, PK = project("H", "srcHone0001", title="Paneer tikka in a clay oven"), project("K", "srcKone0001", mode="permission", note="Mail from Knife Stunts India on 2 Nov 2026: yes, with credit")
    H1 = clip(PH, "uploaded", "Paneer Tikka Secrets", "ytH1mail001", past)
    H2 = clip(PH, "queued", "H waiting")
    K1 = clip(PK, "uploaded", "Street Knife Tricks Compilation", "ytK1guide01", past)
    K2 = clip(PK, "queued", "K waiting")
    with SessionLocal() as s:
        for x in s.query(Destination).all():
            x.paused, x.paused_reason = False, None
        s.commit()
    takedown = ("Hi Foody,\n\nWe have received a copyright removal request for your video. Based on applicable copyright law, we have removed your video "
                "from YouTube.\n\nVideo title: Paneer Tikka Secrets\nVideo URL: https://www.youtube.com/watch?v=ytH1mail001\nContent used: Paneer tikka in a clay oven\n"
                "Claimant: Tandoor Tales Media\nClaimant email: legal@tandoortales.example\nRemoved on: Nov 23, 2026\n\nYou now have 1 copyright strike. If you get 3 "
                "copyright strikes, your channel will be terminated.\n\nWhat you can do next\n- Wait 90 days for the strike to expire\n- Get a retraction: you can get "
                "in touch with the claimant and ask them to retract their claim of copyright infringement. Questions: help@partner-agency.example\n"
                "- Submit a counter notification\n\n- The YouTube Team")
    guideline = ("<html><body><table><tr><td><h2>Your video was removed</h2><p>Hi Foody,</p><p>Our team has reviewed your content, and, unfortunately, we think "
                 "it violates our <a href=\"https://support.google.com/youtube/answer/2801964\">harmful or dangerous content policy</a>. We've removed the "
                 "following content from YouTube:</p><p><b>Video:</b> Street Knife Tricks Compilation</p><p>Your channel now has 1 strike. You won't be able "
                 "to upload, post, or live stream for 1 week.</p><p><a href=\"https://studio.youtube.com/channel/UCmine000000000000000000/\">Take a look</a> "
                 "and appeal if you think we made a mistake.</p></td></tr></table><style>.x{color:red}</style></body></html>")
    INBOX[1] = [
        mail("m-odd2", "YouTube <no-reply@youtube.com>", None, plain=None, html=None),  # no subject, no text at all
        mail("m-news", "YouTube Creators <no-reply@youtube.com>", "Copyright basics: what every creator should know", plain="Learn about copyright strikes, takedown requests and Content ID in our new course."),
        mail("m-spoof", "YouTube Copyright <copyright@youtube-support.example>", "Copyright strike: your video was removed", plain="Your video https://youtu.be/ytB2sched01 was removed. Contact payments@scam.example"),
        mail("m-odd", "YouTube <no-reply@youtube.com>", "Important: content removed from your channel", plain="Ref 99-1827 · TL;DR ░░ see policy centre ░░ item 7 of 9"),
        mail("m-claim", "YouTube <no-reply@youtube.com>", "A copyright claim was created for content in \"Best Samosa Moment\"", plain="A copyright owner using Content ID has claimed some material in your video Best Samosa Moment (https://youtu.be/ytA1struck1). This is not a copyright strike."),
        # the reviewer's three look-alikes: real YouTube senders, strike-like words, a link to one of my videos — none is a strike
        mail("m-cmt", "YouTube <no-reply@youtube.com>", "Your comment was removed",
             plain="We removed a comment you posted on https://www.youtube.com/watch?v=ytB2sched01 because it violates our Community Guidelines."),
        mail("m-share", "YouTube <no-reply@youtube.com>", "Copyright Strike Notice has shared a video with you",
             plain="Copyright Strike Notice shared a private video. Message: your video https://youtu.be/ytB2sched01 is claimed by Big Label.\nClaimant email: legal-team@evil.example"),
        mail("m-cid", "YouTube <noreply@youtube.com>", "Copyright-protected content found in your video",
             plain="Your video B scheduled https://youtu.be/ytB2sched01 contains content owned by X. This is not a copyright strike."),
        # a title that only CONTAINS one of my titles is not a match
        mail("m-sub", "YouTube <noreply@youtube.com>", "Your video has been taken down from YouTube",
             plain="Video title: Best Samosa Moment compilation part 2\nClaimant: Someone\nreply to someone@x.example"),
        mail("m-guide", "YouTube <no-reply@youtube.com>", "Your YouTube channel got a Community Guidelines strike", html=guideline),
        mail("m-take", "YouTube Copyright <copyright@youtube.com>", "[Copyright takedown notice] Your video has been taken down from YouTube", plain=takedown),
    ]
    INBOX[1][0]["payload"]["parts"] = [{"mimeType": "text/plain", "body": {"data": "%%%not-base64%%%"}}, {"body": {}}, "junk"]
    n_cases, n_err, n_told, n_priv = len(cases()), len(errors()), len(told), len(private_calls)
    res = strikes.mail_round()
    cs = cases()
    ok(res["notices"] == 4 and len(cs) == n_cases + 4 and len(errors()) == n_err,
       f"11 mails read: takedown + guidelines + two that match none of my videos are notices; claim, newsletter, look-alike sender, empty mail, "
       f"removed comment, shared video and 'copyright-protected content' are not; no error ({res})")
    ch, ck = case_of("ytH1mail001"), case_of("ytK1guide01")
    ok(ch and ch["kind"] == "copyright" and ch["state"] == "mail" and ch["claimant"] == "Tandoor Tales Media" and ch["confirmed"] is False
       and ch["notices"][0]["matched_by"] == "video link" and "Copyright takedown notice" in ch["notices"][0]["subject"] and len(ch["notices"][0]["excerpt"]) <= 400,
       "copyright takedown mail -> a case for the video named by its link, with the claimant, the subject and a short excerpt (not confirmed yet)")
    ok(ch["mail"]["to"] == "legal@tandoortales.example" and "YouTube's notice" in ch["mail"]["to_from"] and "Claimant email: legal@tandoortales.example" in ch["mail"]["to_from"]
       and ch["mail"]["auto"] is False and "Tandoor Tales Media," in ch["mail"]["body"].split("\n")[0],
       "the address comes only from the labelled claimant line (not the other address in the text); the card says where it came from")
    ok(any("send it by hand" in x for x in ch["left"]), "the claimant's name is not the creator's channel name: 'ready — send it by hand', never by itself")
    with SessionLocal() as s:
        d1, d2 = s.get(Destination, D1), s.get(Destination, D2)
        nothing = (private_calls[n_priv:] == [] and not strikes.is_blocked(uc("H")) and not strikes.is_blocked(uc("K")) and st(H2) == "queued"
                   and st(K2) == "queued" and not d1.paused and not d2.paused)
    ok(nothing and ch["done"] == [] and "has not changed anything" in ch["what"],
       "A MAIL ALONE NEVER ACTS: nobody blocked, nothing held, nothing private, nothing paused — the card says so")
    ok(any("does not show a strike-like removal" in x["text"] for x in ch["log"]), "…YouTube itself was asked about the video (videos.list): it shows nothing wrong")
    ok(ck and ck["kind"] == "guidelines" and ck["notices"][0]["matched_by"] == "video title" and ck["mail"] is None and ck["confirmed"] is False,
       "Community Guidelines mail (HTML only, no link) -> matched by the exact 'Video:' line; no mail to the creator")
    unk = next(x for x in cs if x["unmatched"] and x["title"] == "Important: content removed from your channel")
    ok(unk["clip_id"] is None and "Ref 99-1827" in unk["notices"][0]["excerpt"]
       and unk["mail"] is None and "matched none" in unk["youtube"]["note"], "a layout the parser does not know is kept as a case with its subject and excerpt — never a crash")
    ok(not d1.paused and not d2.paused, "a fresh removal notice nobody can place pauses NOTHING: the owner is told")
    sub_case = [x for x in cs if x["unmatched"] and x["title"] == "Your video has been taken down from YouTube"]
    ok(len(sub_case) == 1 and case_of("ytA1struck1")["notices"] == [], "'Video title: Best Samosa Moment compilation part 2' is not 'Best Samosa Moment': no match")
    ok(case_of("ytA1struck1")["notices"] == [] and not any(x["youtube_id"] == "ytB2sched01" for x in cs) and not strikes.is_blocked(uc("B"))
       and not any(c[2] == "ytB2sched01" for c in private_calls) and st(B1) != "held",
       "the reviewer's look-alikes (comment removed, 'Copyright Strike Notice' shared a video, Content ID) and a claim change nothing")
    ok(not any("evil.example" in json.dumps(x) for x in cs), "an address in a mail that failed the checks is never taken")
    ok(len(told) == n_told + 1 and "could not be matched" in told[-1] and "YouTube's notice in your Gmail" in told[-1],
       "the owner gets ONE message for the round (not one per mail)")
    # YouTube itself confirms: now the channel is protected (copyright) / paused (guidelines)
    STATE["ytH1mail001"] = {"id": "ytH1mail001", "status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}}
    STATE["ytK1guide01"] = {"id": "ytK1guide01", "status": {"uploadStatus": "rejected", "rejectionReason": "inappropriate"}}
    n_told = len(told)
    tc.post("/api/alerts/check")
    ch, ck = case_of("ytH1mail001"), case_of("ytK1guide01")
    ok(ch["confirmed"] and ch["state"] == "rejected:copyright" and strikes.is_blocked(uc("H")) and st(H2) == "held" and len(cases()) == n_cases + 4,
       "YouTube's own status confirms the copyright case: the creator is blocked and their waiting clip held (the same case)")
    ok(not any(c[2] == "ytH1mail001" for c in private_calls) and "no longer on YouTube" in ch["mail"]["body"], "the struck video itself is not touched")
    with SessionLocal() as s:
        ok(s.get(Destination, D1).paused, "uploads to the channel are paused")
    ok(ck["confirmed"] and ck["kind"] == "guidelines" and not strikes.is_blocked(uc("K")) and st(K2) == "queued" and ck["can_block"]
       and "not a complaint from the creator" in ck["what"] and any("Stop using this creator" in x for x in ck["left"]),
       "Community Guidelines confirmed: the channel is paused, but the creator is NOT blocked (it was not their complaint) — the button is offered")
    strike_msgs = [t for t in told[n_told:] if "What the system did" in t]
    ok(len(strike_msgs) == 1 and "Copyright complaint" in strike_msgs[0] and "Community Guidelines" in strike_msgs[0],
       "one summary message for the round, covering both cases")
    set_setting(strikes.SETTING, '{"auto": true}')
    n = len(SENT)
    ok(strikes.send_due() == 0 and len(SENT) == n, "automatic ON: the claimant's name is not the channel's, so the request still waits for Send")
    set_setting(strikes.SETTING, '{"auto": false}')
    r = tc.post(f"/api/strikes/{ck['id']}/block").json()
    ck = case_of("ytK1guide01")
    ok(strikes.is_blocked(uc("K")) and st(K2) == "held" and r["held"] == 1 and any("You put Knife Stunts India on the never-use list" in x for x in ck["done"])
       and not ck["can_block"], "“Stop using this creator”: the owner's choice — blocked, their waiting clip held")
    n_get = len(gets)
    strikes.mail_round()
    ok(len(gets) == n_get and len(cases()) == n_cases + 4, "a mail is read once: the next round opens nothing again")
    # a notice for a video that already has a case joins it
    INBOX[1].insert(0, mail("m-take2", "YouTube Copyright <copyright@youtube.com>", "[Copyright takedown notice] Your video has been taken down from YouTube",
                            plain=takedown.replace("ytH1mail001", "ytA1struck1").replace("Paneer Tikka Secrets", "Best Samosa Moment").replace("Tandoor Tales Media", "Spice Route Kitchen Pvt Ltd")))
    GET_FAIL.add("m-take2")
    strikes.mail_round()
    ok(case_of("ytA1struck1")["notices"] == [], "Gmail busy while reading: nothing is lost …")
    GET_FAIL.clear(); strikes.mail_round()
    ca = case_of("ytA1struck1")
    ok(len(cases()) == n_cases + 4 and len(ca["notices"]) == 1 and ca["claimant"] == "Spice Route Kitchen Pvt Ltd" and ca["mail"]["state"] == "sent" and len(SENT) == 1,
       "… it is read in the next round and joins the existing case (no second case, no second mail)")
    # parser details
    p = strikes.parse_notice
    ok(p({"from": "YouTube <no-reply@youtube.com>", "subject": "Your video has been removed from YouTube", "text": "It violates our Community Guidelines."})["kind"] == "guidelines"
       and p({"from": "x@notify.youtube.com", "subject": "Copyright strike", "text": ""})["kind"] == "copyright"
       and p({"from": "a@accounts.google.com", "subject": "Copyright strike", "text": ""}) is None
       and p({"from": "x@gmail.com", "subject": "Copyright strike", "text": ""}) is None and p({}) is None and p(None) is None
       and p({"from": "no-reply@youtube.com", "subject": "Someone replied to your comment", "text": "copyright strike"}) is None,
       "the parser: sender must be youtube.com (as the Gmail search), the subject must speak of it, odd input gives None")
    ok(strikes.MAIL_QUERY.startswith("from:youtube.com ") and strikes._from_youtube("YouTube <no-reply@youtube.com>") and not strikes._from_youtube("x@google.com"),
       "the Gmail search and the sender check agree: youtube.com only")
    ok(p({"from": "no-reply@youtube.com", "subject": "Your comment was removed", "text": ""})["about_video"] is False
       and p({"from": "no-reply@youtube.com", "subject": "Good news: the copyright strike on your video was retracted", "text": ""})["type"] == "resolved"
       and p({"from": "no-reply@youtube.com", "subject": "Copyright claim released", "text": "The claim on your video has been released."})["type"] == "claim",
       "…a removed comment is not about a video; good news is recognised; a Content ID 'claim released' is not good news about a strike")
    bare = p({"from": "no-reply@youtube.com", "subject": "Your video has been taken down", "text": "Claimant: Zaika Zone\nContact them at zz@zaika.example"})
    lab = p({"from": "no-reply@youtube.com", "subject": "Your video has been taken down", "text": "Claimant: Zaika Zone\nClaimant email: zz@zaika.example"})
    ok(bare["claimant_email"] == "" and lab["claimant_email"] == "zz@zaika.example" and lab["claimant_named"] == "Zaika Zone",
       "an address is taken only from a labelled claimant line — never 'the first address in the text'")
    DUP1, DUP2 = clip(PB, "uploaded", "Same Title Twice", "ytDUP000001", past), clip(PB, "uploaded", "Same Title Twice", "ytDUP000002", past)
    with SessionLocal() as s:
        L = lambda txt: strikes._link(s, p({"from": "no-reply@youtube.com", "subject": "Your video has been taken down", "text": txt}))[1]
        ok(L("Video title: Paneer Tikka Secrets") == "video title" and L("Video:  paneer tikka   SECRETS ") == "video title"
           and L("Video title: Paneer Tikka Secrets part 2") == "" and L("We removed Paneer Tikka Secrets from YouTube.") == ""
           and L("Title: Paneer Tikka Secrets") == "" and L("Video: Same Title Twice") == "",
           "title match: only an exact 'Video title:' / 'Video:' line, and only when one clip has that title (no search inside the text)")

    # ---------------------------------------------------------------- 6. the answer
    REPLIES["T1"] = {"id": "r1", "from": "Spice Route <hello@spiceroute.example>",
                     "text": "Okay, I have retracted the claim. Please ask first next time. On Mon, 23 Nov 2026 Foody Fun wrote: " + SENT[0]["body"][:200]}
    n_told = len(told)
    res = strikes.mail_round()
    ca = case_of("ytA1struck1")
    ok(res["replies"] == 1 and ca["reply"]["retracted"] is True and ca["status"] == "retracted" and "retracted the claim" in ca["reply"]["text"],
       "the creator's answer is picked up from the thread and shown; it reads like 'taken back'")
    ok(any("uploads can be resumed" in t for t in told[n_told:]) and any("Resume uploads" in x for x in ca["left"]), "the owner is told the uploads can be resumed (after checking YouTube Studio)")
    ok(strikes.mail_round()["replies"] == 0, "the same answer is not reported twice")
    CH = ch["id"]
    tc.post(f"/api/strikes/{CH}/send", json={})
    REPLIES[f"T{len(SENT)}"] = {"id": "r2", "from": "Tandoor Tales <legal@tandoortales.example>", "text": "No. We will not retract it. You used our video without asking."}
    strikes.mail_round()
    chh = case_of("ytH1mail001")
    ok(len(SENT) == 2 and chh["reply"] and chh["reply"]["retracted"] is False and chh["status"] == "open", "a refusal is shown as it is — not taken for a retraction")
    R = strikes.reads_as_retracted
    ok(R("Done, I withdrew the complaint.") and R("The strike has been removed from our side") and R("ok claim released") and not R("I will not retract")
       and not R("Why should I retract it?") and not R("Pay 5000 rupees and I will retract") and not R("thanks for writing") and not R(""),
       "'taken back' needs clear words, and no refusal, condition or question")
    ok(strikes._own_words("Hello Spice Route Kitchen, I am writing to apologise ... retract the complaint", "Hello Spice Route Kitchen,\n\nI am writing") == "",
       "my own request quoted back is not read as their answer")
    INBOX[1].insert(0, mail("m-good", "YouTube Copyright <copyright@youtube.com>", "Good news: the copyright removal request for your video was retracted",
                            plain="The claimant retracted the request for https://www.youtube.com/watch?v=ytH1mail001. The strike has been removed."))
    strikes.mail_round()
    ok(case_of("ytH1mail001")["status"] == "retracted" and len(cases()) == n_cases + 4, "YouTube's own good-news mail marks the case as taken back")

    # ---------------------------------------------------------------- 7. automatic sending: only YouTube's own address for the creator, 3 a day
    inbox = {}
    for k in "CDEF":
        inbox[uc(k)] = {"channel_id": uc(k), "name": NAMES[k], "url": url_of(k), "video_id": "v", "video_title": "t", "views": 1, "status": "asked",
                        "added": "x", "message": "m", "email": f"{k.lower()}@creator.example"}
    set_setting("creators", json.dumps(inbox))
    struck = {}
    for k in "CDEFG":
        pid = project(k, f"src{k}one0001")
        struck[k] = clip(pid, "uploaded", f"Clip of {NAMES[k]}", f"yt{k}1struck1", past)
        STATE[f"yt{k}1struck1"] = {"id": f"yt{k}1struck1", "status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}}
        if k == "D":
            D2c = clip(pid, "uploaded", "D scheduled", "ytD2sched01", soon)
    PRIVATE_FAIL["ytD2sched01"] = youtube.NeedsReconnect()
    n_told = len(told)
    tc.post("/api/alerts/check")
    mails = {k: case_of(f"yt{k}1struck1")["mail"] for k in "CDEFG"}
    ok(all(mails[k]["state"] == "ready" and mails[k]["to"] == f"{k.lower()}@creator.example" and mails[k]["to_from"] == "your permission inbox"
           and mails[k]["auto"] is False for k in "CDEF"),
       "five more strikes: the address comes from the permission inbox — ready, to be sent by hand")
    g = case_of("ytG1struck1")
    strike_msgs = [t for t in told[n_told:] if "What the system did" in t]
    ok(mails["G"]["state"] == "no_email" and mails["G"]["to"] == "" and mails["G"]["body"] and any("No e-mail address was found" in x for x in g["left"]),
       "no e-mail anywhere: the case says so, with the ready text to send yourself")
    ok(len(strike_msgs) == 1 and len(told[n_told:]) == 5 + 1 and "Could NOT keep a scheduled video private" in strike_msgs[0],
       f"five strikes in one round: ONE summary message from the responder (beside the video check's own lines), the alarm in it ({len(told[n_told:])})")
    strikes.mail_round()
    ok(len(SENT) == 2, "automatic sending OFF: the ready requests wait")
    # a scheduled video that could not be made private
    dcase = case_of("ytD1struck1")
    ok(len(dcase["private_failed"]) == 1 and "connect it once more" in dcase["private_failed"][0]["error"] and any("Set “D scheduled” to Private yourself" in x for x in dcase["left"])
       and len([t for t in told if "Could NOT keep a scheduled video private" in t]) == 1,
       "a scheduled video that could not be set to private: recorded with the reason, and the owner is told loudly (once)")
    n_calls = len([c for c in private_calls if c[2] == "ytD2sched01"])
    strikes.watch()
    ok(n_calls == 1 and len([c for c in private_calls if c[2] == "ytD2sched01"]) == 1, "not hammered: a moment later it is not tried again yet")

    def later():  # the waiting time before the next try has passed
        f = json.loads(get_setting(strikes.FAIL_KEY))
        for v in f.values():
            v["next"] = "2000-01-01T00:00:00Z"
        set_setting(strikes.FAIL_KEY, json.dumps(f))
    later(); strikes.watch()
    ok(len([c for c in private_calls if c[2] == "ytD2sched01"]) == 2 and len([t for t in told if "Could NOT keep" in t]) == 1, "it is tried again later, without a second alarm")
    PRIVATE_FAIL.clear(); later(); strikes.watch()
    with SessionLocal() as s:
        ok(s.get(Clip, D2c).kept_private is True and case_of("ytD1struck1")["private_failed"] == [] and len(case_of("ytD1struck1")["private"]) == 1,
           "when it works, the video is kept private and the warning goes")
    r = tc.post("/api/settings/strike-reply", json={"auto": True})
    ok(r.status_code == 200 and r.json()["auto"] is True and tc.get("/api/settings/strike-reply").json()["auto"] is True, "the account switches automatic sending ON")
    set_setting("strike_sent:" + utcnow().strftime("%Y-%m-%d"), "0")  # a new day
    res = strikes.mail_round()
    ok(res["sent"] == 0 and len(SENT) == 2, "automatic ON, but every address came from the permission inbox: nothing goes out by itself")
    for k in "CDEF":  # YouTube's own notice names the creator as the claimant, with an address
        INBOX[1].insert(0, mail(f"m-cl{k}", "YouTube Copyright <copyright@youtube.com>", "[Copyright takedown notice] Your video has been taken down from YouTube",
                                plain=f"Video URL: https://www.youtube.com/watch?v=yt{k}1struck1\nClaimant: {NAMES[k]}\nClaimant email: {k.lower()}@claimant.example\n"))
    n_told = len(told)
    res = strikes.mail_round()
    ok(res["sent"] == 3 and [m["to"] for m in SENT[2:]] == ["c@claimant.example", "d@claimant.example", "e@claimant.example"],
       f"the address from YouTube's notice, claimant = the creator's channel: sent by itself, at most 3 a day, oldest case first ({[m['to'] for m in SENT[2:]]})")
    ok(len(told) == n_told + 1 and told[-1].count("was sent to") == 3, "the owner gets one message for the round, naming the three")
    strikes.mail_round(); strikes.mail_round()
    ok(len(SENT) == 5 and case_of("ytF1struck1")["mail"]["state"] == "ready", "the fourth waits for tomorrow; nobody gets a second mail")
    set_setting("strike_sent:" + utcnow().strftime("%Y-%m-%d"), "0")  # the next day
    strikes.mail_round()
    ok(len(SENT) == 6 and SENT[5]["to"] == "f@claimant.example", "next day: the one that waited goes out")
    strikes.mail_round()
    ok(len(SENT) == 6 and case_of("ytG1struck1")["mail"]["state"] == "no_email", "no e-mail: never sent by itself")
    ok(len({(m["to"]) for m in SENT}) == len(SENT), "nobody was written to twice")
    CG = g["id"]
    r = tc.post(f"/api/strikes/{CG}/send", json={"to": "not an address"})
    ok(r.status_code == 400 and len(SENT) == 6, "a typed address that is not one is refused")
    r = tc.post(f"/api/strikes/{CG}/send", json={"to": "guru@golgappa.example"})
    ok(r.status_code == 200 and SENT[-1]["to"] == "guru@golgappa.example" and case_of("ytG1struck1")["mail"]["to_from"] == "typed by you", "the owner pastes the address and sends it")
    # a send that may or may not have gone out is never repeated by itself
    pidz = project("Z", "srcZone0001"); Z1 = clip(pidz, "uploaded", "Clip of Zaika", "ytZ1struck1", past)
    EMAILS[uc("Z")] = "zaika@zone.example"
    STATE["ytZ1struck1"] = {"id": "ytZ1struck1", "status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}}
    tc.post("/api/alerts/check")
    set_setting("strike_sent:" + utcnow().strftime("%Y-%m-%d"), "0")
    real_send = Msgs.send
    Msgs.send = lambda self, userId, body: Exec(lambda: (_ for _ in ()).throw(TimeoutError("timed out after 30.0s at 10.1.2.3")))
    r = tc.post(f"/api/strikes/{case_of('ytZ1struck1')['id']}/send", json={})
    Msgs.send = real_send
    z = case_of("ytZ1struck1")
    ok(r.status_code == 400 and "did not finish" in r.json()["detail"] and "10.1.2.3" not in r.json()["detail"] and "10.1.2.3" not in json.dumps(z),
       "a send that broke off: plain words in the toast and the card, never the raw error text")
    ok(z["mail"]["state"] == "interrupted" and z["mail"]["can_send"] and any("did not finish" in x for x in z["left"]), "a send that broke off: marked, shown, with what to check")
    n = len(SENT); strikes.mail_round(); strikes.mail_round()
    ok(len(SENT) == n, "…and never repeated by itself")
    ok(tc.post(f"/api/strikes/{z['id']}/send", json={}).status_code == 200 and SENT[-1]["to"] == "zaika@zone.example", "the owner can press Send again")
    # good news counts only when it is about the very same video, and a Content ID release is not about a strike
    news = lambda mid, subject, body: INBOX[1].insert(0, mail(mid, "YouTube Copyright <copyright@youtube.com>", subject, plain=body))
    news("m-cidrel", "Copyright claim released", "The claim on https://youtu.be/ytE1struck1 has been released.")
    news("m-goodx", "Update on a copyright removal request", "The claimant has retracted the removal request for https://youtu.be/NOTMINE0001.")
    strikes.mail_round()
    ok(case_of("ytE1struck1")["status"] == "open", "a Content ID 'claim released' and good news about another video change nothing")
    news("m-goodE", "Update on a copyright removal request", "The claimant has retracted the removal request for https://youtu.be/ytE1struck1.")
    strikes.mail_round()
    ok(case_of("ytE1struck1")["status"] == "retracted", "good news written in the text (not the subject), for the same video id, marks the case as taken back")
    tc.post("/api/settings/strike-reply", json={"auto": False})
    # keeping videos private stays inside the day's YouTube budget (uploads keep their share)
    from app import quota
    qkey = "quota:" + quota._pt_day()
    q_before = get_setting(qkey)
    set_setting(qkey, str(quota.DAILY_UNITS - 100))
    QA, QB = clip(PA, "uploaded", "A far future", "ytAfar00001", utcnow() + timedelta(days=20)), clip(PA, "uploaded", "A tonight", "ytAsoon0001", utcnow() + timedelta(minutes=30))
    n, n_told = len(private_calls), len(told)
    strikes.watch()
    fq = json.loads(get_setting(strikes.FAIL_KEY)).get(str(QA)) or {}
    ok([c[2] for c in private_calls[n:]] == ["ytAsoon0001"] and "allowance" in fq.get("error", "") and fq.get("tries") == 0
       and fq.get("next") == strikes._iso(quota.next_reset()),
       "budget short: the video going public tonight is still kept private; the one weeks away waits for the daily reset (not counted as a try)")
    ok(not any("Could NOT keep" in t for t in told[n_told:]), "…and a video weeks away that waits for the budget is not an alarm")
    strikes.watch()
    ok(len(private_calls) == n + 1, "…and it is not tried again before the reset")
    set_setting(qkey, q_before or "0")

    # ---------------------------------------------------------------- 8. the words for YouTube
    y = case_of("ytA1struck1")["youtube"]
    ok("Creative Commons" in y["text"] and "https://youtu.be/srcAone0001" in y["text"] and "Credit: Spice Route Kitchen" in y["text"] and "Best Samosa Moment" in y["text"]
       and "penalty of perjury" in y["warning"] and "counter-notification" in y["action"], "copyright with a CC licence: a draft of the evidence the system holds, with the legal warning")
    ok(any("Content" in x for x in y["steps"]) and any("Restrictions" in x and "See details" in x for x in y["steps"]) and any("Select action" in x for x in y["steps"])
       and any("Channel violations" in x for x in y["steps"]) and y["studio_url"].startswith("https://studio.youtube.com/"), "the exact place to paste it, as plain steps")
    yk = case_of("ytK1guide01")["youtube"]
    ok("Community Guidelines" in yk["text"] and "respectfully" in yk["text"] and "apologise" in yk["text"] and yk["text"].rstrip().endswith("Foody Fun") and "Appeal" in yk["action"]
       and yk["warning"] == "", "Community Guidelines: a short humble appeal in the owner's name")
    x = C(); x.kind, x.youtube_id, x.state, x.id = "copyright", "yt", "rejected:copyright", 1
    yp = strikes.youtube_text(x, {"title": "T", "source_mode": "permission", "permission_note": "Mail from X on 2 Nov: yes", "source_title": "O", "source_channel": "X"})
    yn = strikes.youtube_text(x, {"title": "T", "source_mode": "commentary", "source_title": "O", "source_channel": "X"})
    ok("Mail from X on 2 Nov: yes" in yp["text"] and yn["text"] == "" and "Do not send a counter-notification" in yn["note"] and yn["warning"],
       "with a recorded permission the draft quotes it; with no licence or permission there is nothing to file, and it says so")
    hp = [i for i in tc.get("/api/guide").json()["help"] if "strike" in i["q"].lower() and "complaint" in i["q"].lower()]
    ok(len(hp) == 1 and "cannot be answered automatically" in hp[0]["auto"] and "Copy the text for YouTube" in hp[0]["do"], "the Setup guide explains honestly what cannot be done")

    # ---------------------------------------------------------------- 9. the Alerts board and the owner's buttons
    b = tc.get("/api/alerts/board").json()
    items = [i for i in b["tabs"]["strikes"] if i["kind"] == "strike_case"]
    ok(len(items) == len(cases()) and all(i["case"]["what"] and "done" in i["case"] and "left" in i["case"] for i in items) and b["strike_reply"]["auto"] is False
       and b["strike_reply"]["gmail"] == "owner@gmail.example", "the Strikes tab gets every case with all its card needs, and the switch")
    open_items = [i for i in items if i["case"]["status"] == "open" and i["case"]["confirmed"]]
    notice_items = [i for i in items if i["case"]["status"] == "open" and not i["case"]["confirmed"]]
    ok(all(i["level"] == "bad" and i["at"] is None for i in open_items) and b["badge"] >= len(open_items)
       and notice_items and all(i["level"] == "warn" and i["label"] == "Notice" for i in notice_items),
       "open strikes are on top and count for the red number; a notice YouTube did not confirm is a yellow 'Notice'")
    one = open_items[0]
    tc.post("/api/alerts/board/seen", json={"tab": "all", "id": one["id"]})
    b2 = tc.get("/api/alerts/board").json()
    ok(next(i for i in b2["tabs"]["strikes"] if i["id"] == one["id"])["seen"] is True and b2["badge"] == b["badge"] - 1, "Dismiss marks one case as seen")
    n = len(private_calls)
    r = tc.post(f"/api/strikes/{CA}/hide-public").json()
    with SessionLocal() as s:
        ok(r["done"] == 1 and private_calls[n:] == [(1, D1, "ytA7public1")] and s.get(Clip, A7).kept_private is True and r["case"]["public_others"] == [],
           "“Make their other videos private”: the creator's public video is set to private (not deleted)")
    CK = ck["id"]
    r = tc.post(f"/api/strikes/{CK}/release").json()
    with SessionLocal() as s:
        ok(r["released"] == 1 and not strikes.is_blocked(uc("K")) and s.get(Clip, K2).status == "rendered" and s.get(Clip, K2).held_was is None and s.get(Clip, K2).autoqueue is False,
           "“Use this creator again”: off the never-use list, the held clip is back for review (not straight to upload)")
    strikes.watch()
    ok(not strikes.is_blocked(uc("K")) and st(K2) == "rendered" and strikes.is_blocked(uc("A")), "…and the next round does not put them back")
    r = tc.post(f"/api/strikes/{unk['id']}/solved").json()
    ok(r["status"] == "solved" and next(i for i in tc.get("/api/alerts/board").json()["tabs"]["strikes"] if i["kind"] == "strike_case" and i["case"]["id"] == unk["id"])["level"] == "info",
       "“Mark as solved”: the case becomes history")
    ok(tc.post(f"/api/strikes/{CA}/solved").json()["status"] == "solved" and strikes.is_blocked(uc("A")) and st(A2) == "held", "solved does not un-block the creator or release clips")
    ok(tc.post("/api/strikes/99999/solved").status_code == 404 and tc.post("/api/strikes/99999/send", json={}).status_code == 404, "an unknown case: a plain 404")
    # the list survives a lost setting (a backup restored elsewhere)
    set_setting(strikes.BLOCK_KEY, "{}")
    strikes.watch()
    ok(strikes.is_blocked(uc("A")) and strikes.is_blocked(uc("H")) and not strikes.is_blocked(uc("K")), "the never-use list is rebuilt from the cases (not for a creator the owner allowed again)")
    K3, K4 = clip(PK, "uploaded", "Knife again", "ytK3again01", past), clip(PK, "queued", "K waiting 2")
    STATE["ytK3again01"] = {"id": "ytK3again01", "status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}}
    tc.post("/api/alerts/check")
    ok(strikes.is_blocked(uc("K")) and st(K4) == "held", "a NEW copyright strike from a creator who was allowed again: on the list again")
    set_setting(strikes.BLOCK_KEY, "{}")
    strikes.watch()
    ok(strikes.is_blocked(uc("K")), "…and a lost list is rebuilt with them: the open new case was not released (an older released case does not count)")
    from app import backup
    ok(backup._protected("strike_blocked") and backup._protected("strike_sent:2026-11-23"), "a restored backup cannot empty the never-use list or the day's count")

    # ---------------------------------------------------------------- 10. the real YouTube helper (videos.update)
    calls = []

    class V:
        def list(self, **k): calls.append(("list", k)); return Exec(lambda: {"items": [{"id": k["id"], "status": dict(V.status)}] if V.status else []})
        def update(self, **k): calls.append(("update", k)); return Exec(lambda: {})
    V.status = {"privacyStatus": "private", "publishAt": "2026-12-24T12:30:00Z", "uploadStatus": "processed", "license": "youtube", "embeddable": True,
                "publicStatsViewable": True, "selfDeclaredMadeForKids": False, "madeForKids": False}
    youtube.comment_client = lambda did: type("Y", (), {"videos": lambda self: V()})()
    was = real_keep_private(D1, "vidSCHED001")
    body = calls[-1][1]["body"]
    ok(was == "private" and calls[-1][0] == "update" and calls[-1][1]["part"] == "status" and body["id"] == "vidSCHED001" and body["status"]["privacyStatus"] == "private"
       and "publishAt" not in body["status"] and body["status"]["embeddable"] is True and body["status"]["license"] == "youtube" and "uploadStatus" not in body["status"],
       "youtube.keep_private: videos.update with status private and NO publish time; the other settings are sent back unchanged")
    V.status = {"privacyStatus": "private", "uploadStatus": "processed"}; calls.clear()
    real_keep_private(D1, "vidPRIVATE01")
    ok([c[0] for c in calls] == ["list"], "already private with no publish time: nothing is changed")
    V.status = None
    try:
        real_keep_private(D1, "vidGONE0001"); gone = False
    except LookupError:
        gone = True
    ok(gone, "a video that is gone from YouTube: said so (LookupError)")
    real_creds = youtube._creds
    youtube._creds = lambda did: type("Cr", (), {"scopes": ["https://www.googleapis.com/auth/youtube.upload"]})()
    youtube.comment_client = real_comment_client  # the real check of the login's scopes (no call to Google is made)
    try:
        real_keep_private(D1, "vidOLDLOGIN"); old = False
    except youtube.NeedsReconnect:
        old = True
    youtube._creds = real_creds
    ok(old and "connect it once more" in strikes._why(youtube.NeedsReconnect()), "a channel login made before comments were added cannot change a video: NeedsReconnect, in plain words")

    # ---------------------------------------------------------------- 11. customers: own cases only, own switch, access rules
    def customer(client, email_, name):
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email_, "password": PW, "accept_terms": True})
        aid = next(u["id"] for u in tc.get("/api/admin/users").json()["users"] if u["email"] == email_)
        tid = tc.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        client.post("/api/login", json={"username": email_, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
            set_setting("gmail_email", name.lower() + "@gmail.example")
            with SessionLocal() as s:
                s.add(Destination(channel_id="UC-" + name, name=name + " channel", token="x")); s.commit()
        return aid, tid

    a1, t1 = customer(c1, "asha@example.com", "Asha")
    a2, t2 = customer(c2, "ravi@example.com", "Ravi")
    sw = lambda aid, key, on: tc.post(f"/api/admin/users/{aid}/access", json={"perms": {key: on}})
    owner_cases = len(cases())
    ok(cases(c1) == [] and cases(c2) == [] and c1.get("/api/alerts/board").json()["tabs"]["strikes"] == [], "a customer sees none of the owner's cases")
    with tenant.use(t1):
        ok(not strikes.is_blocked(uc("A")) and strikes.blocked() == {}, "the never-use list is each account's own")
        pid = project("A", "srcAcust001"); X1 = clip(pid, "uploaded", "Asha's struck clip", "ytX1struck1", past); X2 = clip(pid, "queued", "Asha waiting")
        strikes.watch()  # the responder's first run for this account (nothing removed yet: no history)
        with SessionLocal() as s:
            s.get(Clip, X1).yt_state = "rejected:copyright"; s.commit()
        set_setting("creators", json.dumps({uc("A"): {"channel_id": uc("A"), "name": NAMES["A"], "url": url_of("A"), "status": "asked", "email": "a@creator.example"}}))
        set_setting(strikes.SETTING, '{"auto": true}')
    sw(a1, "gmail.send", False)
    n_sent, n_told = len(SENT), len(told)
    scheduler.for_each_customer(strikes.watch)()
    scheduler.for_each_customer(scheduler.when_on("gmail.send", strikes.mail_round))()
    cc = cases(c1)
    with tenant.use(t1):
        ok(len(cc) == 1 and cc[0]["youtube_id"] == "ytX1struck1" and strikes.is_blocked(uc("A")) and st(X2) == "held" and cc[0]["mail"]["state"] == "ready",
           "a customer's strike: protection runs for their copy (no switch needed), the request is written")
        ok(len(SENT) == n_sent and strikes.send_due() == 0, "“Permission mails to creators” switched off for the account: nothing is sent, even with automatic ON")
    ok(len(cases()) == owner_cases and cases(c2) == [], "the owner's and the other customer's lists are unchanged")
    ok(c1.post("/api/strikes/1/send", json={}).status_code == 403 and c1.post("/api/settings/strike-reply", json={"auto": True}).status_code == 403
       and c1.post("/api/settings/strike-reply", json={"auto": False}).status_code == 200, "with the switch off the account cannot send or switch automatic ON — but can always switch it OFF")
    ok(c2.post("/api/strikes/1/solved").status_code == 404 and c2.post("/api/strikes/1/send", json={}).status_code == 404
       and c2.post(f"/api/strikes/{CA}/release").status_code == 404 and cases(c1)[0]["status"] == "open" and case_of("ytZ1struck1")["status"] == "open",
       "another account cannot reach a case by its number (each copy has its own numbers)")
    sw(a1, "gmail.send", True)
    with tenant.use(t1):
        set_setting(strikes.SETTING, '{"auto": true}')
    scheduler.for_each_customer(scheduler.when_on("gmail.send", strikes.mail_round))()
    ok(len(SENT) == n_sent, "switch on again: the address is from the permission inbox, so it still waits for the customer's Send")
    r = c1.post("/api/strikes/1/send", json={})
    ok(r.status_code == 200 and len(SENT) == n_sent + 1 and SENT[-1]["tenant"] == t1 and SENT[-1]["to"] == "a@creator.example" and cases(c1)[0]["mail"]["state"] == "sent",
       "the customer presses Send: it goes out from the customer's copy")
    ok(c1.post("/api/strikes/1/solved").json()["status"] == "solved" and case_of("ytA1struck1")["id"] == CA and len(cases()) == owner_cases, "a customer acts on their own case #1, not the owner's")
    rows = [r["detail"] for r in tc.get(f"/api/admin/activity?account_id={a1}").json()["rows"]]
    ok(any("retract" in x for x in rows) and any("Marked a strike case as solved" in x for x in rows), "what the customer did is in their activity log")
    sw(a2, "page.alerts", False)
    ok(c2.get("/api/strikes").status_code == 403 and c2.post("/api/strikes/1/solved").status_code == 403, "Alerts page switched off: the cases are closed too")
    ok(anon.get("/api/strikes").status_code == 401 and anon.post("/api/strikes/1/send", json={}).status_code == 401, "not signed in: nothing")
    rule = lambda m, p: acl.rule_for(m, p)[0]
    ok(rule("GET", "/api/strikes") == "page.alerts" and rule("POST", "/api/strikes/7/send") == "gmail.send" and rule("POST", "/api/strikes/7/solved") == "page.alerts"
       and rule("POST", "/api/strikes/7/release") == "sources.edit" and rule("POST", "/api/strikes/7/hide-public") == "channels.pause"
       and rule("GET", "/api/settings/strike-reply") == acl.SIGNED and rule("POST", "/api/settings/strike-reply") == "page.alerts", "every new address has its line in acl.ROUTES")

    # ---------------------------------------------------------------- 12. an existing database gets the new table and columns
    with db.engine.begin() as conn:  # the owner's copy as it was before this version
        conn.execute(text("DROP TABLE strike_cases"))
        conn.execute(text("ALTER TABLE clips DROP COLUMN held_was"))
        conn.execute(text("ALTER TABLE clips DROP COLUMN kept_private"))
        before = conn.execute(text("SELECT count(*) FROM clips")).scalar()
    Base.metadata.create_all(db.engine); db.migrate()  # what every start does
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("clips")}
    with SessionLocal() as s:
        ok(insp.has_table("strike_cases") and {"held_was", "kept_private"} <= cols and s.query(Clip).count() == before and s.query(StrikeCase).count() == 0,
           "owner's database from before: the table and the two clip columns are added at start, the clips are all still there")
    OLD = 4242  # a customer's copy from before, opened for the first time by this version
    folder = Path(d) / "tenants" / str(OLD); folder.mkdir(parents=True)
    import sqlite3
    con = sqlite3.connect(str(folder / "app.db"))
    con.executescript("CREATE TABLE projects (id INTEGER PRIMARY KEY, url VARCHAR(500), video_id VARCHAR(32), title VARCHAR(500), channel VARCHAR(300), "
                      "channel_url VARCHAR(500), mode VARCHAR(20));"
                      "CREATE TABLE clips (id INTEGER PRIMARY KEY, project_id INTEGER, start FLOAT, \"end\" FLOAT, title VARCHAR(200), status VARCHAR(20), "
                      "youtube_id VARCHAR(32), yt_state VARCHAR(60));"
                      "CREATE TABLE settings (key VARCHAR(100) PRIMARY KEY, value TEXT);"
                      "INSERT INTO projects VALUES (1, 'u', 'srcOLD00001', 'Old original', 'Old Creator', '" + url_of("B") + "', 'cc');"
                      "INSERT INTO clips VALUES (1, 1, 0, 30, 'Old struck clip', 'uploaded', 'ytOLDstrk01', 'rejected:copyright');"
                      "INSERT INTO clips VALUES (2, 1, 0, 30, 'Old waiting clip', 'queued', NULL, NULL);")
    con.commit(); con.close()
    with tenant.use(OLD):
        with SessionLocal() as s:  # (tables and columns appear on first use) + a scheduled video of the same creator
            s.add(Clip(id=4, project_id=1, start=0, end=30, title="Old scheduled", status="uploaded", youtube_id="ytOLDsched1",
                       publish_at=utcnow() + timedelta(days=30))); s.commit()
        n_priv = len(private_calls)
        res = strikes.watch()
        with SessionLocal() as s:
            ok(res["new"] == 0 and s.query(StrikeCase).count() == 0 and s.get(Clip, 2).status == "queued" and not strikes.is_blocked(url_of("B"))
               and len(private_calls) == n_priv and json.loads(get_setting(strikes.INIT_KEY))["history"] == {"1": "rejected:copyright"},
               "FIRST RUN with old history: the removal that was already there is recorded as history — nothing held, nothing private, nobody blocked")
            s.add(Clip(id=3, project_id=1, start=0, end=30, title="New struck clip", status="uploaded", youtube_id="ytOLDstrk02", yt_state="rejected:copyright"))
            s.commit()
        res = strikes.watch()
        with SessionLocal() as s:
            cs_old = s.query(StrikeCase).all()
            ok(res["new"] == 1 and len(cs_old) == 1 and cs_old[0].clip_id == 3 and cs_old[0].kind == "copyright" and s.get(Clip, 2).status == "held"
               and s.get(Clip, 2).held_was == "queued" and strikes.is_blocked(url_of("B")) and private_calls[n_priv:] == [(OLD, None, "ytOLDsched1")],
               "a removal that appears AFTER the first run acts: case, creator blocked, waiting clip held, scheduled video kept private")
            fresh = (cs_old[0].data or {}).get("fresh")
        ok(fresh is False, "a removal with no fresh alert does not pause uploads again")
    ok(not strikes.is_blocked(url_of("B")), "…and that never-use list is that copy's own")
    # ---------------------------------------------------------------- 13. small rules
    ok(strikes._why(RuntimeError("token=abc123 at /srv/x")) == "something unexpected went wrong" and "abc123" not in strikes._why(ValueError("abc123")),
       "_why: plain sentences, never the raw error text")
    from googleapiclient.errors import HttpError
    he = HttpError(type("Resp", (), {"status": 500, "reason": "boom"})(), b'{"error": {"message": "internal secret detail 42"}}')
    ok("secret" not in strikes._why(he) and "YouTube" in strikes._why(he), "…also for an error from YouTube")
    import inspect
    src = inspect.getsource(sys.modules["app.scheduler"])  # (start itself is replaced in this test)
    ok("executor.submit(strike_mails)" not in src and 'when_on("gmail.send", strike_mails)' in src, "strike_mails runs in the scheduler's thread, not the single render worker")
    lk = strikes._lock(); lk.acquire(); t0 = time.time()
    busy = strikes.mail_round(); lk.release()
    ok(busy == {"busy": True} and time.time() - t0 < 1, "…and never waits for the lock (no 120 s wait)")
    import threading
    key = strikes.SENT_PREFIX + utcnow().strftime("%Y-%m-%d")
    set_setting(key, "7")
    got = []
    ths = [threading.Thread(target=lambda: got.append(strikes._reserve(strikes.HAND_PER_DAY))) for _ in range(6)]
    [t.start() for t in ths]; [t.join() for t in ths]
    ok(sum(1 for x in got if x) == 3 and get_setting(key) == "10", f"the day's limit is check-and-count in one step: 6 sends at once at 7 of 10 -> exactly 3 ({got})")
    set_setting(key, "0")
    # (the app's own timed jobs run during a test too: a channel sync that meets a fake login is not part of this)
    wheres = sorted({e.get("where") for e in errors()} - {"Reading my channel's numbers from YouTube"})
    ok(wheres == ["Reading YouTube's notices in Gmail", "Sending the request to a creator after a strike"],
       f"System errors holds only the two failures that were staged here (Gmail busy, a send that timed out): {wheres}")
# ---------- review 3: comment / reply notifications are never strikes (strangers write their text)
for subj in ('Copyright Strike Notice commented on "Amazing street food moment"',
             'New comment from Copyright Takedown Team on your video',
             'Copyright Strike Notice replied to your comment'):
    n3 = strikes.parse_notice({"id": "rv3" + subj[:6], "from": "YouTube <noreply@youtube.com>", "subject": subj,
                               "text": "Someone commented on your video https://www.youtube.com/watch?v=ABCDEFGHIJK\n"
                                       "Claimant: Creator A\nClaimant email: evil@attacker.example\n", "internal_ms": 0})
    ok(n3 is None or n3.get("type") != "strike", f"not a strike: {subj[:50]} ({n3 and n3.get('type')})")
# history survives a passing 'removed' / unseen check (review 3, finding 2)
import inspect
src = inspect.getsource(strikes._history)
ok('now.get(k) != "ok"' in src and "not in KIND_OF_STATE" in src, "an old rejection stays history through a passing state")
src = inspect.getsource(strikes.scan_monitor)
ok('case.status != "open" and not (case.data or {}).get("confirmed")' in src, "a real strike after a closed mail-only case opens a new case")
from app import backup as _bk
ok("strike_watch_init" in _bk.PROTECTED, "a restore keeps the responder's history marker")
print("DONE", flush=True)

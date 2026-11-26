import json, os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "viral-test-pass-31"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, viral, youtube, permissions, autopilot, quota, telegram, jobs, gmail
from app.db import SessionLocal, Destination, Project, Clip, Channel
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
jobs.executor.submit = lambda fn, *a, **k: fn(*a, **k)  # run inline
youtube.configured = lambda: True


def vid(i, title, ch, chid, views, dur="PT10M", cc=False):
    return {"id": i, "snippet": {"title": title, "channelTitle": ch, "channelId": chid, "publishedAt": "2026-11-21T10:00:00Z"},
            "statistics": {"viewCount": str(views), "likeCount": "10"}, "contentDetails": {"duration": dur},
            "status": {"license": "creativeCommon" if cc else "youtube"}}


CHART = [vid("AAAAAAAAAA1", "Big vlog", "Vlogger", "UCv", 900000), vid("AAAAAAAAAA2", "Song", "T-Series", "UCt", 5000000),
         vid("AAAAAAAAAA3", "CC cooking", "CookCC", "UCcc", 300000, cc=True), vid("AAAAAAAAAA4", "Prank", "NoMail", "UCn", 800000),
         vid("AAAAAAAAAA5", "Gaming", "Gamer", "UCg", 700000), vid("AAAAAAAAAA6", "Tiny", "Tiny", "UCs", 100, dur="PT40S")]
SHORTS = [vid("SSSSSSSSSS1", "Short1", "S", "UCsh", 50000, dur="PT30S")]
calls = []


class Q:
    def __init__(self, kind): self.kind = kind
    def list(self, **k):
        self.k = k; calls.append((self.kind, k)); return self
    def execute(self):
        if self.kind == "videos":
            return {"items": CHART} if self.k.get("chart") else {"items": SHORTS}
        if self.kind == "search":
            return {"items": [{"id": {"videoId": "SSSSSSSSSS1"}}]}
        return {"items": [{"id": "UCv", "snippet": {"description": "biz: vlog@mail.in"}}, {"id": "UCg", "snippet": {"description": "gamer [at] mail [dot] com"}}]}


class YT:
    def videos(self): return Q("videos")
    def search(self): return Q("search")
    def channels(self): return Q("channels")


youtube.client = lambda *a, **k: YT()
processed = []


def fake_process(s, ch, v, log, mode=None, note=None):
    processed.append((v, mode, note, ch.kind))
    p = Project(url="u", video_id=v, title="t", channel="c", mode=mode, duration=600, channel_ref=ch.id); s.add(p); s.commit()
    s.add(Clip(project_id=p.id, start=10, end=45, aspect="9:16", status="queued")); s.commit()
    return True


autopilot._process_video = fake_process
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "viral-test-pass-31"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
    permissions._save({"UCg": {"channel_id": "UCg", "name": "Gamer", "url": "u", "status": "granted", "proof": "Email reply from gamer: yes sure ok go"}})
    v = tc.post("/api/viral/refresh").json()
    ok(len(v["videos"]) == 5 and len(v["shorts"]) == 2, f"board saved: {len(v['videos'])} videos, {len(v['shorts'])} shorts")
    ok(v["day"] == viral.today() and v["updated"], "dated today, with update time")
    picks = {p["video_id"]: p for p in v["picks"]}
    ok("AAAAAAAAAA2" not in picks, "music label never picked")
    ok("AAAAAAAAAA4" not in picks, "creator without public email not picked")
    ok(picks.get("AAAAAAAAAA5", {}).get("why") == "allowed", "creator who said yes -> picked as allowed")
    ok(picks.get("AAAAAAAAAA3", {}).get("why") == "cc", "CC video picked")
    ok(picks.get("AAAAAAAAAA1", {}).get("why") == "ask", "vlogger with public email -> permission needed")
    ok("AAAAAAAAAA6" not in picks, "too-short video not picked")
    done = {x[0]: x for x in processed}
    ok(done.get("AAAAAAAAAA3", (0, 0))[1] == "cc" and done.get("AAAAAAAAAA5", (0, 0))[1] == "permission", "CC + allowed clipped at once")
    ok(done["AAAAAAAAAA5"][2].startswith("Email reply") and done["AAAAAAAAAA5"][3] == "viral", "permission proof attached; viral source used")
    ok("AAAAAAAAAA1" not in done, "permission-needed video NOT clipped before a yes")
    ok(picks["AAAAAAAAAA3"]["status"]["step"] == "queued", "status shows scheduled: " + picks["AAAAAAAAAA3"]["status"]["text"])
    ok(picks["AAAAAAAAAA1"]["status"]["action"] == "send", "status: mail ready -> press Send")
    c = permissions._load()["UCv"]
    ok(c["email"] == "vlog@mail.in" and "Big vlog" in c["message"], "creator in inbox with email + mail naming the video")
    ok(v["ready_mails"] == 1, "1 ready mail counted")
    su = [i for i in tc.get("/api/setup").json()["items"] if i["key"] == "viral"][0]
    ok(not su["done"] and "Send all" in su["blocked"], "checklist tells the owner to send")
    # owner presses Send all
    gmail.connected_email = lambda: "me@x.com"
    gmail.send = lambda to, body, subject=None: {"thread_id": "TH1", "message_id": "M"}
    r = tc.post("/api/viral/send").json()
    ok(r["sent"] == 1, "Send all ready mails sent 1")
    p1 = [p for p in tc.get("/api/viral").json()["picks"] if p["video_id"] == "AAAAAAAAAA1"][0]
    ok(p1["status"]["step"] == "wait", "status: waiting for reply")
    # creator replies yes -> clipped automatically
    gmail.reply_in_thread = lambda t: {"from": "Vlogger <vlog@mail.in>", "date": "d", "snippet": "haan bilkul kar lo", "id": "r1"}
    permissions.check_replies()
    ok(any(x[0] == "AAAAAAAAAA1" and x[1] == "permission" for x in processed), "yes by mail -> that viral video clipped automatically")
    p1 = [p for p in tc.get("/api/viral").json()["picks"] if p["video_id"] == "AAAAAAAAAA1"][0]
    ok(p1["status"]["step"] == "queued", "status now: " + p1["status"]["text"])
    # second refresh same day keeps first_views and computes gain; no duplicate picks
    CHART[0]["statistics"]["viewCount"] = "950000"
    v = tc.post("/api/viral/refresh").json()
    big = [r for r in v["videos"] if r["video_id"] == "AAAAAAAAAA1"][0]
    ok(big["gain"] == 50000, "gain since first seen = 50,000")
    ok(len(v["picks"]) == len({p["video_id"] for p in v["picks"]}), "no duplicate picks")
    ok(sum(1 for k, _ in calls if k == "search") == 1, "Shorts search only once a day (quota)")
    # manual pick + autopilot never runs the viral source
    r = tc.post("/api/viral/pick", json={"video_id": "AAAAAAAAAA4"})
    ok(r.status_code == 200 and any(p["video_id"] == "AAAAAAAAAA4" and p["by_hand"] for p in r.json()["picks"]), "owner can pick by hand")
    ok(tc.post("/api/viral/pick", json={"video_id": "AAAAAAAAAA4"}).status_code == 400, "same video can't be picked twice")
    with SessionLocal() as s:
        ch = s.query(Channel).filter(Channel.kind == "viral").first()
        ch.auto = True; s.commit()
    n = len(processed); autopilot.run_channel(ch.id)
    ok(len(processed) == n, "source timer never runs the viral source")
    ok(tc.get("/api/viral?day=2020-01-01").json()["videos"] == [], "old day: empty, no error")
    ok(tc.get("/api/viral?day=bad").status_code == 422, "bad day rejected")

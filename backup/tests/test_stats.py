import os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "stats-test-pass-44"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, youtube, stats
from app.db import SessionLocal, Destination, ChannelStat, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
state = {"views": 1000, "subs": 50, "v1": 300}
class Req:
    def __init__(self, r): self.r = r
    def execute(self): return self.r
class YT:
    def channels(self): return type("C", (), {"list": lambda s, **k: Req({"items": [{"statistics": {"subscriberCount": str(state["subs"]), "viewCount": str(state["views"]), "videoCount": "2"}, "contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}}]})})()
    def playlistItems(self): return type("P", (), {"list": lambda s, **k: Req({"items": [{"contentDetails": {"videoId": "AAAAAAAAAA1"}}, {"contentDetails": {"videoId": "AAAAAAAAAA2"}}]})})()
    def videos(self): return type("V", (), {"list": lambda s, **k: Req({"items": [
        {"id": "AAAAAAAAAA1", "statistics": {"viewCount": str(state["v1"]), "likeCount": "20", "commentCount": "4"}, "snippet": {"title": "Kachori", "publishedAt": "2026-11-21T04:30:00Z"}, "status": {"privacyStatus": "public"}},
        {"id": "AAAAAAAAAA2", "statistics": {"viewCount": "10"}, "snippet": {"title": "Pizza", "publishedAt": "2026-11-20T04:30:00Z"}, "status": {"privacyStatus": "private"}}]})})()
youtube.client = lambda did=None: YT()
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "stats-test-pass-44"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
        s.add(ChannelStat(destination_id=1, at=utcnow() - timedelta(hours=25), subscribers=40, views=800, videos=2)); s.commit()
    r = tc.post("/api/analytics/sync").json()
    c = r["channels"][0]
    ok(c["subscribers"] == 50 and c["views"] == 1000 and c["subs_24h"] == 10 and c["views_24h"] == 200, f"channel numbers + 24h change {c['subs_24h']} {c['views_24h']}")
    ok(len(r["videos"]) == 2 and r["videos"][0]["title"] == "Kachori" and r["videos"][0]["likes"] == 20, "every video listed with likes")
    state["v1"] = 450; state["views"] = 1150
    r2 = tc.post("/api/analytics/sync?live=1").json()
    ok(r2["channels"][0]["views"] == 1000, "live mode throttled (no second read within 2 min)")
    r3 = tc.post("/api/analytics/sync").json()
    v = [x for x in r3["videos"] if x["title"] == "Kachori"][0]
    ok(v["views"] == 450 and v["new_views"] == 150, f"new views since last update {v['new_views']}")

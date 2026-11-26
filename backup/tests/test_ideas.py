import os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "ideas-test-pass-61"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, youtube, competitors
from app.db import utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
now = utcnow()
vids = [("V%010d" % i, 2000 if i else 90000, 5) for i in range(8)]  # video 0 is an outlier


class Req:
    def __init__(self, r): self.r = r
    def execute(self): return self.r


class YT:
    def videos(self):
        def lst(**k):
            if k.get("chart"):
                return Req({"items": [{"id": "TRENDVID001", "snippet": {"title": "Street food trend", "channelTitle": "X"}, "statistics": {"viewCount": "5000"}}]})
            return Req({"items": [{"id": v, "snippet": {"title": f"Video {v}", "publishedAt": (now - timedelta(days=age)).isoformat() + "Z"},
                                   "statistics": {"viewCount": str(views)}, "contentDetails": {"duration": "PT5M"}} for v, views, age in vids]})
        return type("V", (), {"list": lambda s, **k: lst(**k)})()


youtube.client = lambda did=None: YT()
youtube.api_resolve_channel = lambda q: ("https://www.youtube.com/channel/UCcomp", "Kabita's Kitchen")
youtube.api_list_video_ids = lambda url, n: [v for v, _, _ in vids]
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "ideas-test-pass-61"})
    ok(tc.post("/api/ideas/competitors", json={"query": "@kabitaskitchen"}).status_code == 200, "competitor added")
    r = tc.post("/api/ideas/scan").json()
    ok(len(r["outliers"]) == 1 and r["outliers"][0]["video_id"] == "V0000000000" and r["outliers"][0]["ratio"] >= 3, f"outlier found ({r['outliers'][:1]})")
    g = tc.get("/api/ideas?category=26").json()
    ok(g["trending"][0]["title"] == "Street food trend", "trending list")
    ok(tc.get("/api/ideas?category=abc").status_code == 400, "bad category refused")
    ok(len(tc.delete("/api/ideas/competitors", params={"url": "https://www.youtube.com/channel/UCcomp"}).json()["channels"]) == 0, "remove competitor")

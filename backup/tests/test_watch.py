import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "watch-test-pass-21"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, youtube, stats
from app.db import SessionLocal, Destination
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
stats.sync_all = lambda: {}
def no_scope(did): raise youtube.NeedsReconnect()
youtube.analytics_client = no_scope
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "watch-test-pass-21"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
    c = tc.post("/api/analytics/sync").json()["channels"][0]
    ok(c["watch"] is None and c["watch_status"] == "reconnect", "old login -> asks to reconnect")
    class Q:
        def __init__(self, k): self.k = k
        def execute(self):
            dim = self.k.get("dimensions")
            H = lambda names: [{"name": n} for n in names]
            if dim == "day": return {"columnHeaders": H(["day", "views", "estimatedMinutesWatched", "averageViewDuration", "subscribersGained", "subscribersLost"]), "rows": [["2026-11-20", 10, 30, 20, 2, 0], ["2026-11-21", 20, 90, 25, 3, 1]]}
            if dim == "video": return {"columnHeaders": H(["video", "views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage"]), "rows": [["AAAAAAAAAA1", 30, 120, 22, 63.4]]}
            if dim == "insightTrafficSourceType": return {"columnHeaders": H(["insightTrafficSourceType", "views", "estimatedMinutesWatched"]), "rows": [["SHORTS", 25, 100]]}
            if dim == "country": return {"columnHeaders": H(["country", "views", "estimatedMinutesWatched"]), "rows": [["IN", 28, 110]]}
            return {"columnHeaders": H(["views", "estimatedMinutesWatched"]), "rows": [[100, 500]]}
    class R:
        def query(self, **k): return Q(k)
    youtube.analytics_client = lambda did: type("A", (), {"reports": lambda s: R()})()
    stats.set_setting("watchtime:1", "")  # force refresh
    c = tc.post("/api/analytics/sync").json()["channels"][0]
    w = c["watch"]
    ok(w and w["minutes_28d"] == 120 and w["lifetime_minutes"] == 500 and w["subs_gained_28d"] == 5, f"watch time totals {w and w['minutes_28d']}")
    ok(w["traffic"][0]["source"] == "Shorts feed" and w["countries"][0]["country"] == "IN", "traffic sources + countries")
    ok(c["watch_status"] == "ok", "status ok")

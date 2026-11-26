"""Parked clips go back into the line a few a day; a clip that repeats a moment already made or on YouTube is never used."""
import os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, os.environ.get("RS_CODE", r"D:/fix-main"))
from fastapi.testclient import TestClient
from app import main, autopilot, auth, jobs, youtube
from app.db import SessionLocal, Channel, Clip, Destination, Project, set_setting, get_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
PARK = "Parked: too many clips were waiting to be made. Press Render to make it, then Approve."
with TestClient(main.app, raise_server_exceptions=False) as c, TestClient(main.app, raise_server_exceptions=False) as anon:
    c.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    with SessionLocal() as s:
        dst = Destination(channel_id="UCx", name="Main", token="x"); s.add(dst); s.commit()
        ch = Channel(kind="channel", query="q", name="Src", mode="cc", auto=True, destination_id=dst.id, post_times="18:00", tz_offset=-330, gap_hours=2)
        s.add(ch); s.commit()
        p1 = Project(url="u", video_id="aaaaaaaaaaa", title="t", mode="cc", license=youtube.CC_LICENSE, duration=600, channel_ref=ch.id); s.add(p1)
        p2 = Project(url="u", video_id="bbbbbbbbbbb", title="t", mode="cc", license=youtube.CC_LICENSE, duration=600, channel_ref=ch.id); s.add(p2); s.commit()
        up = Clip(project_id=p1.id, start=100, end=135, aspect="9:16", status="uploaded", destination_id=dst.id, publish_at=utcnow() + timedelta(days=3)); s.add(up)
        q = Clip(project_id=p2.id, start=0, end=35, aspect="9:16", status="queued", destination_id=dst.id, publish_at=utcnow() + timedelta(days=5)); s.add(q)
        dup1 = Clip(project_id=p1.id, start=120, end=155, aspect="9:16", status="draft", error=PARK, destination_id=dst.id, title="T", description="D")  # overlaps the uploaded one
        dup2 = Clip(project_id=p2.id, start=10, end=45, aspect="9:16", status="draft", error=PARK, destination_id=dst.id, title="T", description="D")    # overlaps the queued one
        new = [Clip(project_id=p1.id, start=300 + 40 * i, end=335 + 40 * i, aspect="9:16", status="draft", error=PARK, destination_id=dst.id, title="T", description="D") for i in range(5)]
        other = Clip(project_id=p1.id, start=500, end=530, aspect="9:16", status="draft", error="something else", destination_id=dst.id)
        s.add_all([dup1, dup2, *new, other]); s.commit()
        ids = {"dup1": dup1.id, "dup2": dup2.id, "new": [x.id for x in new], "other": other.id}
    ok(anon.post("/api/admin/unpark", json={"per_day": 3, "now": True}).status_code in (401, 403), "not signed in: refused")
    autopilot.disk_free_gb = lambda: 3.0
    r = c.post("/api/admin/unpark", json={"per_day": 3, "now": True}).json()
    with SessionLocal() as s:
        st = {i: s.get(Clip, i) for i in [ids["dup1"], ids["dup2"], *ids["new"], ids["other"]]}
        started = [i for i in ids["new"] if st[i].status == "fetching"]
        pubs = sorted(st[i].publish_at for i in started)
    ok(r["started"] == 3 and r["duplicates"] == 2 and len(started) == 3, f"3 a day are made again, the 2 repeats are skipped ({r})")
    ok(st[ids["dup1"]].status == "draft" and "repeats the moment" in (st[ids["dup1"]].error or "") and "repeats the moment" in (st[ids["dup2"]].error or ""),
       "a clip that repeats an uploaded or queued moment stays out, with a plain note")
    ok(st[ids["other"]].status == "draft" and st[ids["other"]].error == "something else", "a draft that was not parked is left alone")
    ok(all(st[i].autoqueue and st[i].publish_at for i in started) and len({p.date() for p in pubs}) == 3, f"each one gets its own day ({[str(p)[:16] for p in pubs]})")
    ok(all(p.hour == 12 and p.minute == 30 for p in pubs), "at 18:00 IST")
    ok(get_setting("unpark_per_day") == "3", "the daily number is saved")
    r2 = autopilot.unpark_due()
    ok(r2["started"] == 0, "the same day: nothing more (once a day)")
    set_setting("unpark_day", "2000-01-01")
    autopilot.disk_free_gb = lambda: 1.0
    r3 = autopilot.unpark_due()
    ok(r3["started"] == 0 and "disk is low" in r3["waiting"] and get_setting("unpark_day") == "2000-01-01", f"low disk: waits and tries again later ({r3})")
    autopilot.disk_free_gb = lambda: 3.0
    r4 = autopilot.unpark_due()
    ok(r4["started"] == 2 and r4["left"] == 0, f"next day: the last 2 ({r4})")
    c.post("/api/admin/unpark", json={"per_day": 0})
    ok(autopilot.unpark_due() == {"started": 0, "duplicates": 0, "left": 0, "waiting": ""}, "0 = off")

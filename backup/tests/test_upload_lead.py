"""How early clips go to YouTube: usually 2 days before their publish time; the Super Admin can send everything that
waits as fast as the daily budget allows (YouTube keeps each one private until its own day)."""
import os, sys, tempfile
from datetime import timedelta
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, r"D:/integ-final")
from fastapi.testclient import TestClient
from app import main, auth, youtube, scheduler, telegram, jobs, quota, metadata
from app.db import SessionLocal, Destination, Clip, Project, set_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
CC = youtube.CC_LICENSE
sent = []
youtube.upload = lambda dest_id, path, title, desc, tags, publish_at=None: (sent.append((title, publish_at)), "yt%05d" % len(sent))[1]
youtube.push_thumbnail = lambda c: True
with TestClient(main.app, raise_server_exceptions=False) as c, TestClient(main.app, raise_server_exceptions=False) as anon, TestClient(main.app, raise_server_exceptions=False) as cu:
    c.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    f = Path(d) / "video.mp4"; f.write_bytes(b"x")
    now = utcnow()
    with SessionLocal() as s:
        dest = Destination(channel_id="UC-owner", name="Owner channel", token="x"); s.add(dest); s.commit()
        p = Project(url="u", video_id="vid00000001", title="T", channel="C", mode="cc", license=CC, duration=600); s.add(p); s.commit()
        for i, days in enumerate([1, 10, 30, 60, 90, 120, 150, 180]):
            s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="queued", title=f"day {days}", description="d",
                       output_path=str(f), destination_id=dest.id, publish_at=now + timedelta(days=days)))
        s.commit()
    r = c.get("/api/settings/upload-lead").json()
    ok(r["days"] == 2 and r["usual"] == 2 and quota.upload_lead_hours() == 48, f"nothing chosen: 2 days before the publish time ({r})")
    scheduler.process_queue()
    ok([t for t, _ in sent] == ["day 1"], f"usual: only the clip that goes live within 2 days is sent ({[t for t, _ in sent]})")
    ok(c.post("/api/admin/upload-lead", json={"days": 999}).status_code == 422 and c.post("/api/admin/upload-lead", json={"days": -1}).status_code == 422, "numbers outside 0-400 refused")
    ok(anon.post("/api/admin/upload-lead", json={"days": 365}).status_code in (401, 403), "not signed in: refused")
    anon.post("/api/register", json={"name": "Asha", "brand": "Asha Studio", "email": "asha@example.com", "password": "customer-pass-12345", "accept_terms": True})
    aid = c.get("/api/admin/users").json()["users"][0]["id"]
    c.post(f"/api/admin/users/{aid}/approve")
    cu.post("/api/login", json={"username": "asha@example.com", "password": "customer-pass-12345"})
    ok(cu.post("/api/admin/upload-lead", json={"days": 365}).status_code == 403, "a customer cannot change it")
    r = c.post("/api/admin/upload-lead", json={"days": 365})
    ok(r.status_code == 200 and r.json() == {"days": 365} and quota.upload_lead_hours() == 365 * 24, "the Super Admin sets 365 days")
    scheduler.process_queue()
    cap = quota.capacity()
    titles = [t for t, _ in sent]
    ok(len(titles) == min(8, cap) and titles == ["day 1", "day 10", "day 30", "day 60", "day 90", "day 120", "day 150", "day 180"][:len(titles)],
       f"365 days: clips go up earliest first, no more than today's budget of {cap} ({titles})")
    ok(all(pa is not None and pa > now + timedelta(hours=12) for _, pa in sent), "each one is sent with its own publish time (YouTube keeps it private until then)")
    scheduler.process_queue()
    ok(len(sent) == len(titles), "the same day: nothing more goes up once the budget is used")
    with SessionLocal() as s:
        left = s.query(Clip).filter(Clip.status == "queued").count()
        pubs = {cl.title: cl.publish_at for cl in s.query(Clip).filter(Clip.status == "uploaded")}
    ok(left == 8 - len(titles) and all(pubs[t] is not None for t in titles), f"the rest wait for tomorrow's budget ({left} waiting); publish times are kept")
    set_setting("upload_lead_days", "abc")
    ok(quota.upload_lead_hours() == 48, "a damaged setting falls back to 2 days")
    set_setting("upload_lead_days", "1")
    ok(quota.upload_lead_hours() == 48, "never less than 2 days")
    c.post("/api/admin/upload-lead", json={"days": 0})
    ok(quota.upload_lead_hours() == 48 and c.get("/api/settings/upload-lead").json()["days"] == 2, "0 = back to the usual 2 days")
    # fresh keywords: with a long lead only the clips that go up next are looked at
    set_setting("upload_lead_days", "365")
    seen = []
    metadata.needs_refresh = lambda clip, now=None: (seen.append(clip.title), False)[1]
    metadata.freshen_due()
    ok(len(seen) <= max(8, cap * 2), f"keyword refresh looks at the next few clips only ({len(seen)})")

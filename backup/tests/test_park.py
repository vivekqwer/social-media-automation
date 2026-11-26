"""Too many clips waiting for the helper: sources stop as soon as the list is full (not only at the start of a run);
the Super Admin can park the newest waiting clips as drafts (nothing deleted, Approve brings them back)."""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, os.environ.get("RS_CODE", r"D:/fix-main"))
from fastapi.testclient import TestClient
from app import main, autopilot, auth, jobs
from app.db import SessionLocal, Channel, Clip, Destination, Project, set_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
with TestClient(main.app, raise_server_exceptions=False) as c, TestClient(main.app, raise_server_exceptions=False) as anon:
    c.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    with SessionLocal() as s:
        dst = Destination(channel_id="UCx", name="Main", token="x"); s.add(dst); s.commit()
        from app import youtube as _yt
        p = Project(url="u", video_id="abcdefghijk", title="t", mode="cc", license=_yt.CC_LICENSE, channel="C", duration=600); s.add(p); s.commit()
        ch = Channel(kind="channel", query="q", name="Src", mode="cc", auto=True, destination_id=dst.id,
                     resolved_url="https://www.youtube.com/channel/UC" + "a" * 22, videos_per_run=5, clips_per_video=3)
        s.add(ch); s.commit(); chid, did, pid = ch.id, dst.id, p.id
        for i in range(8):
            s.add(Clip(project_id=pid, start=0, end=30, aspect="9:16", status="queued", destination_id=did))
        s.commit()
    set_setting("queue_cap", "10")
    made = []

    def fake_process(s, ch, vid, log, *a, **k):  # each video makes 3 clips
        for _ in range(3):
            s.add(Clip(project_id=pid, start=0, end=30, aspect="9:16", status="fetching", destination_id=did))
        s.commit(); made.append(vid); return True

    autopilot._process_video = fake_process
    autopilot.list_video_ids = lambda url, n: [f"vid{i:08d}" for i in range(n)]
    autopilot.run_channel(chid)
    with SessionLocal() as s:
        waiting = s.query(Clip).filter(Clip.status.in_(["queued", "fetching", "rendering"])).count()
        st = s.get(Channel, chid).last_status or ""
    ok(len(made) == 1 and waiting == 11, f"8 waiting, limit 10: one video (3 clips) and then it stops, not 5 videos ({len(made)} videos, {waiting} waiting)")
    ok("paused" in st, f"the source says why ({st[:60]})")
    # park
    with SessionLocal() as s:
        for i in range(6):
            s.add(Clip(project_id=pid, start=0, end=30, aspect="9:16", status="fetching", destination_id=did, autoqueue=True))
        s.commit()
        before = s.query(Clip).filter(Clip.status == "fetching").count()
    ok(anon.post("/api/admin/park-waiting", json={"keep": 2}).status_code in (401, 403), "not signed in: refused")
    r = c.post("/api/admin/park-waiting", json={"keep": 2})
    with SessionLocal() as s:
        f = s.query(Clip).filter(Clip.status == "fetching").order_by(Clip.id).all()
        dr = s.query(Clip).filter(Clip.status == "draft").all()
        total = s.query(Clip).count()
    ok(r.status_code == 200 and r.json()["parked"] == before - 2 and len(f) == 2, f"the oldest 2 stay, the rest are parked ({r.json()})")
    ok(len(dr) == before - 2 and all(x.autoqueue is False and x.publish_at is None and "Parked" in (x.error or "") for x in dr), "parked clips are drafts with a plain note")
    ok(total == 8 + 3 + 6, "nothing was deleted")
    r = c.post(f"/api/clips/{dr[0].id}/render")
    with SessionLocal() as s:
        back = s.get(Clip, dr[0].id).status
    ok(r.status_code == 200 and back in ("rendering", "fetching", "queued"), f"Render brings a parked clip back ({r.status_code}, {back})")

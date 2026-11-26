import os, sys, tempfile, subprocess, shutil
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "b5-test-pass-21"
sys.path.insert(0, r"D:\integ-final")
S = r"C:\Users\hp\AppData\Local\Temp\claude\c2cac7217d0d432a\f1b4fc3c-890e-40cf-a8e1-1a5008835727\scratchpad\final_tests"
from pathlib import Path
from fastapi.testclient import TestClient
from app import main, youtube, growth, render, reframe, workers
from app.config import ffmpeg_bin
from app.db import SessionLocal, Destination, Project, Clip, VideoStat, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
os.makedirs(os.path.join(d, "models"), exist_ok=True); shutil.copy(os.path.join(S, "yunet.onnx"), os.path.join(d, "models", "face_detection_yunet.onnx"))
# a 6 s 16:9 test video whose only face is on the right side
img = os.path.join(S, "yt_MPYXXkWmZqU.jpg")
src = os.path.join(d, "face_right.mp4")
subprocess.run([ffmpeg_bin(), "-y", "-loop", "1", "-t", "6", "-i", img, "-vf", "scale=1280:720,format=yuv420p", "-r", "10", src], capture_output=True)
fx = reframe.subject_x(Path(src))
ok(fx is not None and fx > 0.5, f"person found right of centre (x={fx})")
c = Clip(aspect="9:16", effects=[], overlays=[], captions=[], caption_style=None, orig_volume=1, voice_offset=0)
fc = render.build_filter(c, 1080, 1920, 6, Path(d), False, False, "", fx)
ok(f"{fx}*iw-ow/2" in fc, "crop follows the person")
ok("crop=1080:1920," in render.build_filter(c, 1080, 1920, 6, Path(d), False, False, "", None) or "crop=1080:1920[" in render.build_filter(c, 1080, 1920, 6, Path(d), False, False, "", None) or "crop=1080:1920" in render.build_filter(c, 1080, 1920, 6, Path(d), False, False, "", None), "no person -> centre crop")
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "b5-test-pass-21"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
        p = Project(url="u", video_id="abcdefghijk", title="Samosa street", channel="C", mode="cc", duration=100); s.add(p); s.commit()
        wd = Path(d) / "clips" / "1"; wd.mkdir(parents=True)
        shutil.copy(src, wd / "out.mp4"); shutil.copy(src, wd / "body.mp4")
        now = utcnow()
        weak = Clip(project_id=p.id, start=0, end=6, aspect="9:16", status="uploaded", youtube_id="WEAKVID0001", destination_id=1,
                    title="Samosa street", output_path=str(wd / "out.mp4"), attempted_at=now - timedelta(days=4))
        s.add(weak); s.commit()
        for i in range(6):
            s.add(VideoStat(video_id=f"GOODVID{i:04d}", destination_id=1, title="g", privacy="public", views=5000,
                            published_at=now - timedelta(days=4), updated_at=now))
        s.add(VideoStat(video_id="WEAKVID0001", destination_id=1, title="w", privacy="public", views=50, published_at=now - timedelta(days=4), updated_at=now))
        s.commit()
    pushed = []
    youtube.set_thumbnail = lambda dest, vid, path: pushed.append((vid, path))
    done = growth.swap_weak_thumbnails()
    with SessionLocal() as s:
        cw = s.get(Clip, 1)
    ok(done == [1] and pushed and cw.thumb_variant == 1 and os.path.exists(cw.thumb_path), "weak video got a second thumbnail")
    ok(growth.swap_weak_thumbnails() == [], "only once per video")
    r = tc.post("/api/reports/weekly")
    ok(r.status_code == 200 and tc.get("/api/reports/" + r.json()["file"]).headers["content-type"] == "application/pdf", "weekly report PDF made and downloadable")
    ok(tc.get("/api/reports/..%2Fsecret").status_code in (404, 400), "report path is checked")
    reg = tc.post("/api/worker/register", json={"name": "pc"}).json()
    tc.post(f"/api/workers/{reg['worker_id']}/approve", json={"code": workers.pair_code(reg["token"])})
    b = tc.get("/api/worker/backup", headers={"x-worker-token": reg["token"]}).json()
    ok("clips" in b["tables"] and "destinations" in b["tables"] and "video_stats" not in b["tables"], "backup has the data")
    ok(tc.get("/api/worker/backup").status_code == 401, "backup needs the helper token")
    ok(all("token" not in r or r["token"] in ("x",) for r in b["tables"]["destinations"]), "channel logins stay as stored (encrypted in real use)")

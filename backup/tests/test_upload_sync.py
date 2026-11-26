import os, subprocess, sys, tempfile, time
from pathlib import Path
d = Path(tempfile.mkdtemp()); os.environ["DATA_DIR"] = str(d); os.environ["DATABASE_URL"] = "sqlite:///" + str(d).replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "upload-test-pass-9"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, jobs
from app.config import ffmpeg_bin
from app.db import SessionLocal, Destination, Clip, Project
from app.ingest import video_size, stream_seconds
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
jobs.executor.submit = lambda fn, *a, **k: fn(*a, **k)   # run renders inline
main.executor.submit = jobs.executor.submit
ff = ffmpeg_bin()
mk = lambda *a: subprocess.run([ff, "-y", "-v", "error", *a], cwd=d, capture_output=True, text=True)
mk("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=6", "-f", "lavfi", "-i", "sine=frequency=300:duration=6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "wide.mp4")
mk("-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=25:duration=12", "-f", "lavfi", "-i", "sine=frequency=300:duration=12", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "tall.mp4")
mk("-f", "lavfi", "-i", "sine=frequency=800:duration=8", "-c:a", "libmp3lame", "voice.mp3")
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "upload-test-pass-9"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
    r = tc.post("/api/upload-video", files={"file": ("notes.txt", b"hello", "text/plain")})
    ok(r.status_code == 400, "non-video file refused")
    r = tc.post("/api/upload-video", files={"file": ("My_trip video.mp4", open(d / "wide.mp4", "rb"), "video/mp4")}, data={"title": ""})
    j = r.json()
    ok(r.status_code == 200 and j["aspect"] == "16:9" and j["width"] == 1920, f"wide video -> full video upload: {j}")
    c = tc.get(f"/api/clips/{j['clip_id']}").json()
    ok(c["status"] == "rendered" and c["end"] == 6.0, f"rendered by itself, whole length ({c['status']}, {c['end']} s) {c.get('error') or ''}")
    out = Path(c["output_path"]) if c.get("output_path") else d / "clips" / str(c["id"]) / "out.mp4"
    ok(video_size(out) == (1920, 1080), f"Full HD kept: {video_size(out)}")
    with SessionLocal() as s:
        p = s.get(Project, s.get(Clip, c["id"]).project_id)
        ok(p.title == "My trip video" and p.mode == "original", f"title from the file name: {p.title}")
    r = tc.post("/api/upload-video", files={"file": ("reel.mov", open(d / "tall.mp4", "rb"), "video/quicktime")})
    j2 = r.json(); cid = j2["clip_id"]
    ok(j2["aspect"] == "9:16", "tall video -> Short")
    ok(video_size(d / "clips" / str(cid) / "out.mp4") == (1080, 1920), "Short is 1080x1920")
    # sound sync on the 12 s Short with an 8 s voice
    s0 = tc.get(f"/api/clips/{cid}/sync").json()
    ok(not s0["has_voice"], "no voice yet")
    ok(tc.post(f"/api/clips/{cid}/sync", json={"mode": "speed"}).status_code == 400, "sync needs a voice first")
    tc.post(f"/api/clips/{cid}/voice-upload", files={"file": ("v.mp3", open(d / "voice.mp3", "rb"), "audio/mpeg")})
    s1 = tc.get(f"/api/clips/{cid}/sync").json()
    ok(s1["has_voice"] and abs(s1["voice"] - 8) < 0.2 and abs(s1["gap"] - 4) < 0.3 and not s1["in_sync"], f"8 s voice on a 12 s clip: gap {s1['gap']} s")
    r = tc.post(f"/api/clips/{cid}/sync", json={"mode": "clip"}).json()
    ok(abs(r["clip"]["end"] - 8.25) < 0.3 and r["sync"]["in_sync"] and r["clip"]["status"] == "draft", f"clip made as long as the voice: end={r['clip']['end']}, needs render")
    tc.post(f"/api/clips/{cid}/render")
    c = tc.get(f"/api/clips/{cid}").json()
    dur = stream_seconds(d / "clips" / str(cid) / "body.mp4", "v:0")
    ok(c["status"] == "rendered" and abs(dur - 8.25) < 0.4, f"re-rendered: video is {dur:.1f} s")
    # speed fit instead: put the clip back to 12 s
    tc.put(f"/api/clips/{cid}", json={"end": 12.0})
    ok(tc.post(f"/api/clips/{cid}/sync", json={"mode": "speed"}).status_code == 400, "8 s voice on 12 s: too different for speed, refused")
    tc.put(f"/api/clips/{cid}", json={"end": 10.0})
    r = tc.post(f"/api/clips/{cid}/sync", json={"mode": "speed"}).json()
    ok(r["sync"]["voice_fit"] == "speed" and r["sync"]["in_sync"] and abs(r["sync"]["speed"] - 0.8) < 0.03, f"speed fit: x{r['sync']['speed']}")
    tc.post(f"/api/clips/{cid}/render")
    ok(tc.get(f"/api/clips/{cid}").json()["status"] == "rendered", "rendered with the speed-fitted voice")
    r = tc.post(f"/api/clips/{cid}/sync", json={"mode": "none"}).json()
    ok(r["sync"]["voice_fit"] == "" and not r["sync"]["in_sync"], "normal speed again")
    # the picture option is saved and counts as a change
    c0 = tc.get(f"/api/clips/{cid}").json()
    r = tc.put(f"/api/clips/{cid}", json={"fit": "fill"}).json()
    ok(r["fit"] == "fill", "picture option saved")
    ok(tc.put(f"/api/clips/{cid}", json={"fit": "banana"}).status_code == 422, "unknown picture option refused")

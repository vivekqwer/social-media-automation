"""Regression tests for what the independent review found."""
import os, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace as NS
d = Path(tempfile.mkdtemp()); os.environ["DATA_DIR"] = str(d); os.environ["DATABASE_URL"] = "sqlite:///" + str(d).replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "review-test-pass-3"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, jobs, render, scheduler, guide, autopilot
from app.config import ffmpeg_bin
from app.db import SessionLocal, Destination, Clip, Project, Channel, main_destination
from app.ingest import stream_seconds, video_size
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
jobs.executor.submit = lambda fn, *a, **k: fn(*a, **k)
main.executor.submit = jobs.executor.submit
ff = ffmpeg_bin()
mk = lambda *a: subprocess.run([ff, "-y", "-v", "error", *a], cwd=d, capture_output=True, text=True)
# a video whose picture changes every second (frame number burnt in by colour: testsrc shows a seconds counter)
mk("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25:duration=20", "-f", "lavfi", "-i", "sine=frequency=300:duration=20", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "wide.mp4")
mk("-f", "lavfi", "-i", "sine=frequency=800:duration=5", "-c:a", "libmp3lame", "voice5.mp3")

# (1) the original sound must last the whole clip when the voice is shorter
c = NS(aspect="9:16", fit=None, effects=[], overlays=[], caption_style={}, captions=[], orig_volume=1.0, voice_offset=0, voice_fit=None, reframe=True)
for name, kw in [("plain", {}), ("offset3", {"voice_offset": 3}), ("speed", {"voice_fit": "speed"})]:
    cc = NS(**{**c.__dict__, **kw})
    fc = render.build_filter(cc, 1080, 1920, 20.0, d, True, True, "", None, (1280, 720), 5.0)
    r = mk("-i", "wide.mp4", "-i", "voice5.mp3", "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "20", f"a_{name}.mp4")
    a = stream_seconds(d / f"a_{name}.mp4", "a:0")
    ok(r.returncode == 0 and a > 19.5, f"5 s voice on a 20 s clip ({name}): sound lasts {a:.1f} s")
# (10) overlay text scales at Full HD
c16 = NS(**{**c.__dict__, "aspect": "16:9", "overlays": [{"text": "Hello", "size": 60, "start": 0, "end": 3}]})
fc720 = render.build_filter(c16, 1280, 720, 5.0, d, False, True, "", None, (1280, 720))
fc1080 = render.build_filter(c16, 1920, 1080, 5.0, d, False, True, "", None, (1920, 1080))
ok("fontsize=60" in fc720 and "fontsize=90" in fc1080, "overlay text is 1.5x bigger at 1080p (same size on screen)")

with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "review-test-pass-3"})
    with SessionLocal() as s:
        a = Destination(channel_id="UCa", name="Foody", token="x"); b = Destination(channel_id="UCb", name="Funny", token="x"); s.add_all([a, b]); s.commit(); A, B = a.id, b.id
    j = tc.post("/api/upload-video", files={"file": ("trip.mp4", open(d / "wide.mp4", "rb"), "video/mp4")}).json()
    cid = j["clip_id"]; folder = d / "clips" / str(cid)
    ok((folder / "src" / "upload.mp4").exists() and not (folder / "src" / "reel.mp4").exists(), "upload is stored under its own name")
    # (3) trim start is honoured: second 6..9 of the source, not 0..3
    tc.put(f"/api/clips/{cid}", json={"start": 6.0, "end": 9.0}); tc.post(f"/api/clips/{cid}/render")
    def frame_sig(path, t):
        r = subprocess.run([ff, "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-vf", "scale=64:36", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
        return r.stdout
    def diff(x, y): return sum(abs(p - q) for p, q in zip(x, y)) / max(1, len(x))
    out = frame_sig(folder / "body.mp4", 0.5); want = frame_sig(d / "wide.mp4", 6.5); wrong = frame_sig(d / "wide.mp4", 0.5)
    ok(abs(stream_seconds(folder / "body.mp4", "v:0") - 3) < 0.3 and diff(out, want) < diff(out, wrong), f"trim 6-9 s renders seconds 6-9 (match {diff(out, want):.1f} vs {diff(out, wrong):.1f})")
    s1 = tc.get(f"/api/clips/{cid}/sync").json()
    ok(abs(s1["longest_clip"] - 14) < 0.5, f"longest possible clip from second 6 of a 20 s file: {s1['longest_clip']} s")
    # (2) the disk clean-up keeps the upload; (4) Re-download refuses to delete it
    os.utime(folder / "src" / "upload.mp4", (1, 1))  # looks very old
    scheduler.cleanup_disk()
    ok((folder / "src" / "upload.mp4").exists(), "disk clean-up keeps an uploaded video")
    r = tc.post(f"/api/clips/{cid}/refetch")
    ok(r.status_code == 400 and "own uploaded video" in r.json()["detail"] and (folder / "src" / "upload.mp4").exists(), "Re-download is refused for an own upload")
    tc.post(f"/api/clips/{cid}/render")
    ok(tc.get(f"/api/clips/{cid}").json()["status"] == "rendered", "re-render after the clean-up still works")
    # (15) captions button on an own video: plain message
    r = tc.post(f"/api/clips/{cid}/captions", json={"lang": "auto"})
    ok(r.status_code == 400 and "your own video" in r.json()["detail"], "captions on an own video: plain message")
    # (8) a failing upload leaves nothing behind
    with SessionLocal() as s: n0 = (s.query(Project).count(), s.query(Clip).count())
    real = main.shutil.rmtree
    import app.ingest as ing
    orig = ing.video_size
    ing.video_size = lambda p: (_ for _ in ()).throw(OSError("disk full"))
    r = tc.post("/api/upload-video", files={"file": ("x.mp4", open(d / "wide.mp4", "rb"), "video/mp4")})
    ing.video_size = orig
    with SessionLocal() as s: n1 = (s.query(Project).count(), s.query(Clip).count())
    ok(r.status_code == 400 and n0 == n1 and "could not be saved" in r.json()["detail"], "failed upload: plain message, no ghost clip")
    # (11) unknown video length: 'make the clip as long as the voice' never produces an empty clip
    with SessionLocal() as s:
        p = Project(url="u", video_id="abcdefghijk", title="T", channel="C", mode="cc", license="Creative Commons", duration=0); s.add(p); s.commit()
        k = Clip(project_id=p.id, start=30, end=50, aspect="9:16", status="draft", destination_id=A); s.add(k); s.commit(); kid = k.id
        vdir = d / "clips" / str(kid); vdir.mkdir(parents=True, exist_ok=True)
        import shutil; shutil.copy(d / "voice5.mp3", vdir / "voice.mp3"); k.voice_path = str(vdir / "voice.mp3"); s.commit()
    r = tc.post(f"/api/clips/{kid}/sync", json={"mode": "clip"}).json()
    ok(r["clip"]["end"] - r["clip"]["start"] >= 2, f"unknown length: clip stays usable ({r['clip']['start']}–{r['clip']['end']})")
    # (5) Upload again copies the picture/voice choices
    with SessionLocal() as s:
        u = Clip(project_id=p.id, start=0, end=20, aspect="9:16", status="uploaded", youtube_id="Y9", destination_id=A, fit="fill", voice_fit="speed", reframe=False, short_cover=False, title="t", description="d")
        s.add(u); s.commit(); uid = u.id
    main.executor.submit = lambda *a, **k: None
    r = tc.post(f"/api/clips/{uid}/reupload", json={"confirm": True})
    main.executor.submit = jobs.executor.submit
    with SessionLocal() as s:
        n = s.query(Clip).order_by(Clip.id.desc()).first()
        ok(r.status_code == 200 and (n.fit, n.voice_fit, n.reframe, n.short_cover) == ("fill", "speed", False, False), f"Upload again keeps fit/voice/reframe/cover ({r.status_code})")
    # (7) main channel: listed first in every menu, viral picks follow it
    with SessionLocal() as s:
        s.add(Channel(kind="viral", name="Viral picks", query="viral picks", mode="permission", auto=False, destination_id=A)); s.commit()
    tc.post(f"/api/destinations/{B}/main")
    ov = tc.get("/api/overview").json()["destinations"]
    with SessionLocal() as s:
        v = s.query(Channel).filter(Channel.kind == "viral").first()
        ok(ov[0]["id"] == B and ov[0]["is_main"] and v.destination_id == B, "main channel is first in the menus and gets the viral picks")
    # (16) a source saved before 'kind' existed still counts and still runs
    with SessionLocal() as s:
        old = Channel(name="Old source", query="x", mode="cc", auto=True, destination_id=A); s.add(old); s.commit()
        s.execute(main.update(Channel).where(Channel.id == old.id).values(kind=None)); s.commit(); OID = old.id
    ran = []
    real_run = autopilot.run_channel; autopilot.run_channel = lambda i: ran.append(i)
    autopilot.autopilot_tick(); autopilot.run_channel = real_run
    items = {i["key"]: i for i in tc.get("/api/setup").json()["items"]}
    ok(OID in ran and items["source"]["done"], "old source without a kind: still runs, still counted")
    ok(not items["verify"]["done"], "phone-verification step is not green before YouTube accepted a thumbnail")
    # (13) reconnecting ends a login-expired pause
    with SessionLocal() as s:
        x = s.get(Destination, A); x.paused, x.paused_reason = True, "The channel's Google login expired. Press Connect..."; s.commit()
    main.youtube.handle_callback = lambda code, state, limit=None: A
    r = tc.get("/auth/callback?code=c&state=s", follow_redirects=False)
    with SessionLocal() as s:
        ok(r.status_code == 302 and "connected=" in r.headers["location"] and not s.get(Destination, A).paused, "reconnect: uploads resume by themselves")
    # (18) Gmail failure comes back with a message
    r = tc.get("/auth/callback?code=c&state=gmail:bad", follow_redirects=False)
    ok(r.status_code == 302 and "gmail_error=" in r.headers["location"], "Gmail failure -> back to the dashboard with a message")
# (14) error log: no secrets, no duplicates
guide.clear_errors()
for _ in range(4):
    try:
        raise RuntimeError("token ya29.a0AfH6SMBx-abcdefghijklmnopqrstuvwxyz0123456789 for someone@gmail.com failed [parameters: ('secret',)]")
    except RuntimeError as e:
        guide.record_error("GET /api/x", e)
e = guide.recent_errors()
ok(len(e) == 1 and e[0]["count"] == 4, f"the same fault 4 times -> 1 line with ×4 ({len(e)} lines)")
ok("ya29" not in e[0]["text"] and "gmail.com" not in e[0]["text"] and "secret" not in e[0]["text"], "token, e-mail and SQL parameters are hidden: " + e[0]["text"])

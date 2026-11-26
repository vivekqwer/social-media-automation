import os, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace as NS
d = Path(tempfile.mkdtemp()); os.environ["DATA_DIR"] = str(d); os.environ["DATABASE_URL"] = "sqlite:///" + str(d).replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "brand-test-pass-61"
sys.path.insert(0, r"D:\integ-final")
from PIL import Image, ImageDraw
from app import render, branding, faces, thumbs, main, jobs
from app.config import ffmpeg_bin
from app.db import Base, engine, migrate, SessionLocal, Destination, Clip, Project, set_setting
from app.ingest import video_size, stream_seconds
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
Base.metadata.create_all(engine); migrate()
ff = ffmpeg_bin()
mk = lambda *a: subprocess.run([ff, "-y", "-v", "error", *a], cwd=d, capture_output=True, text=True)
mk("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=8", "-f", "lavfi", "-i", "sine=frequency=300:duration=8", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "wide.mp4")
mk("-f", "lavfi", "-i", "sine=frequency=800:duration=5", "-c:a", "libmp3lame", "voice.mp3")
# a stand-in "face": a cut-out person shape on a transparent background
fdir = d / "faces"; fdir.mkdir()
f = Image.new("RGBA", (500, 700), (0, 0, 0, 0)); dr = ImageDraw.Draw(f)
dr.ellipse((150, 40, 350, 260), fill=(224, 172, 140, 255)); dr.rounded_rectangle((60, 250, 440, 700), 80, fill=(40, 90, 200, 255))
f.save(fdir / "default.png")
ok(faces.face_for(None) is not None, "face photo found")
proj = NS(title="Old Delhi Street Food Tour | Mohabbat Ka Sharbat", channel="The Food Monster", mode="cc")


def clip(**k):
    base = dict(id=1, aspect="9:16", fit=None, effects=["fade"], overlays=[], caption_style={}, captions=[], orig_volume=1.0, voice_offset=0,
                voice_fit=None, reframe=True, frame_title=None, thumb_text=None, thumb_face=None, destination_id=None, title=None)
    base.update(k)
    return NS(**base)


def do(name, c, voice=False, p=proj):
    W, H = render.DIMS[c.aspect]
    banner = branding.make_banner(c, p, W, H, d) if c.aspect != "16:9" and c.frame_title is not False else None
    if banner:
        banner["idx"] = 2 if voice else 1
    fc = render.build_filter(c, W, H, 8.0, d, voice, True, "Credit: " + p.channel, None, (1280, 720), 5.0 if voice else 0.0, banner)
    cmd = ["-i", "wide.mp4"] + (["-i", "voice.mp3"] if voice else []) + (["-loop", "1", "-i", banner["path"]] if banner else []) + \
          ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "8", name + ".mp4"]
    r = mk(*cmd)
    if r.returncode:
        print(fc); print(r.stderr[-500:])
    mk("-ss", "4", "-i", name + ".mp4", "-frames:v", "1", name + ".jpg")
    return r.returncode == 0, banner


good, b = do("band", clip())
ok(good and b and b["lines"] and video_size(d / "band.mp4") == (1080, 1920) and abs(stream_seconds(d / "band.mp4", "v:0") - 8) < 0.3, f"Short with band renders (8 s): {b['lines']}")
good, b2 = do("band_voice", clip(), voice=True)
ok(good and b2["idx"] == 2 and abs(stream_seconds(d / "band_voice.mp4", "a:0") - 8) < 0.3, "…also with a voice-over (band is the third input)")
good, b3 = do("band_hindi", clip(thumb_text="दिल्ली का स्ट्रीट फूड"))
ok(good and b3["font"] == "Noto Sans Devanagari", "Hindi title uses the Hindi font")
good, b4 = do("no_face", clip(thumb_face=False))
ok(good and b4 and b4["x"] < 80, "face switched off: title only, starts at the left")
good, b5 = do("off", clip(frame_title=False))
ok(good and b5 is None, "band switched off: plain video as before")
good, b6 = do("video169", clip(aspect="16:9"))
ok(good and b6 is None, "normal 16:9 videos never get the band")
good, b7 = do("square", clip(aspect="1:1"))
ok(good and b7 is not None, "square clips get it too")
# is the band really in the picture? the top strip of the frame must differ between on and off
def strip(name):
    r = subprocess.run([ff, "-v", "error", "-i", str(d / (name + ".jpg")), "-vf", "crop=iw:ih*0.14:0:ih*0.075,scale=64:16", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
    return r.stdout
diff = sum(abs(a - b) for a, b in zip(strip("band"), strip("off"))) / 1024
ok(diff > 8, f"the title band is visible in a middle frame (difference {diff:.0f})")
# thumbnail face size
sizes = {}
for sz in ("S", "M", "L"):
    set_setting("thumb_face_size", sz)
    sizes[sz] = branding.face_scale()
ok(sizes == {"S": 0.55, "M": 0.78, "L": 1.0}, f"face size setting: {sizes}")
from app.db import get_setting
set_setting("thumb_face_size", "")
ok(branding.face_scale() == 0.55, "default is the small face")
print("DIR", d)

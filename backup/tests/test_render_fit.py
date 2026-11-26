import os, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace as NS
d = Path(tempfile.mkdtemp()); os.environ["DATA_DIR"] = str(d); os.environ["DATABASE_URL"] = "sqlite:///" + str(d).replace("\\", "/") + "/t.db"
sys.path.insert(0, r"D:\integ-final")
from app import render
from app.config import ffmpeg_bin
from app.ingest import stream_seconds, video_size
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
ff = ffmpeg_bin()
run = lambda *a, cwd=d: subprocess.run([ff, "-y", "-v", "error", *a], cwd=cwd, capture_output=True, text=True)
# a wide source with picture detail at the far left/right edges + a tone; a tall source; an 8 s "voice"
r = run("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=10", "-f", "lavfi", "-i", "sine=frequency=300:duration=10",
        "-vf", "drawtext=text='LEFT':x=10:y=330:fontsize=60:fontcolor=white:box=1:boxcolor=red,drawtext=text='RIGHT':x=w-tw-10:y=330:fontsize=60:fontcolor=white:box=1:boxcolor=blue",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "wide.mp4")
if r.returncode:  # no drawtext font on this PC: plain pattern is enough
    run("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=10", "-f", "lavfi", "-i", "sine=frequency=300:duration=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "wide.mp4")
run("-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=25:duration=6", "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "tall.mp4")
run("-f", "lavfi", "-i", "sine=frequency=800:duration=8", "-c:a", "libmp3lame", "voice.mp3")
ok(video_size(d / "wide.mp4") == (1280, 720) and video_size(d / "tall.mp4") == (720, 1280), f"video_size: {video_size(d / 'wide.mp4')} {video_size(d / 'tall.mp4')}")


def clip(**k):
    base = dict(aspect="9:16", fit=None, effects=["fade"], overlays=[], caption_style={}, captions=[], orig_volume=1.0,
                voice_offset=0, voice_fit=None, reframe=True)
    base.update(k)
    return NS(**base)


def do(name, c, src="wide.mp4", voice=False, dur=10.0, subject=None):
    W, H = render.DIMS[c.aspect]
    size = video_size(d / src)
    fc = render.build_filter(c, W, H, dur, d, voice, True, "", subject, size, stream_seconds(d / "voice.mp3", "a:0") if voice else 0.0)
    cmd = ["-i", src] + (["-i", "voice.mp3"] if voice else []) + ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                                                                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", f"{dur:.2f}", name + ".mp4"]
    r = run(*cmd)
    if r.returncode:
        print(fc); print(r.stderr[-600:])
    run("-ss", "3", "-i", name + ".mp4", "-frames:v", "1", name + ".jpg")
    return r.returncode == 0, render.layout(c, W, H, size), video_size(d / (name + ".mp4"))


for name, c, exp in [("full", clip(), "full"), ("o_wide", clip(fit="wide"), "wide"), ("fill", clip(fit="fill"), "fill"),
                     ("square_full", clip(aspect="1:1"), "full"), ("video169", clip(aspect="16:9"), "pad")]:
    good, mode, size = do(name, c, subject=0.5 if exp in ("fill", "wide") else None)
    ok(good and mode == exp and size == render.DIMS[c.aspect], f"{name}: layout={mode} size={size}")
good, mode, size = do("tall_src", clip(), src="tall.mp4", dur=6.0)
ok(good and mode == "fill", f"vertical source in Shorts stays full-screen (layout={mode})")
good, mode, size = do("fx", clip(effects=["fade", "punch_in", "shake", "bw"]))
ok(good, "whole-picture layout works with every effect")
# voice sync
good, _, _ = do("voice_duck", clip(), voice=True)
ok(good, "voice mixed (original sound ducks under it)")
good, _, _ = do("voice_plain", clip(orig_volume=0), voice=True)
good2, _, _ = do("voice_speed", clip(orig_volume=0, voice_fit="speed"), voice=True)   # 8 s voice -> 10 s clip
ok(good and good2, "voice slowed to end with the clip")
good, _, _ = do("voice_only", clip(orig_volume=0, voice_fit="speed", voice_offset=2), voice=True)  # 8 s into 8 s of room
ok(good, "original sound off + offset")
ok(abs(render.voice_tempo(8, 10) - 0.8) < 1e-6 and render.voice_tempo(30, 10) == render.TEMPO_MAX and render.voice_tempo(2, 10) == render.TEMPO_MIN,
   "tempo: 8s->10s = 0.8, limits kept")
# where does the voice end in the mixed file? measure loudness of the 800 Hz voice over time
def voice_end(name):
    r = subprocess.run([ff, "-v", "info", "-i", name + ".mp4", "-af", "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-"],
                       cwd=d, capture_output=True, text=True)
    import re
    st = re.findall(r"silence_start: ([\d.]+)", r.stderr)
    return float(st[-1]) if st else 10.0
e1, e2 = voice_end("voice_plain"), voice_end("voice_speed")
ok(7.5 < e1 < 8.6, f"plain: voice ends at {e1:.1f}s (2 s of the clip without voice)")
ok(e2 > 9.5, f"speed-fit: voice ends at {e2:.1f}s = with the clip")
print("DIR", d)

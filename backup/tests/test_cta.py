import os, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace as NS
d = Path(tempfile.mkdtemp()); os.environ["DATA_DIR"] = str(d); os.environ["DATABASE_URL"] = "sqlite:///" + str(d).replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "cta-test-pass-61"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import render, branding, reminder, main, jobs, auth, tenant
from app.config import ffmpeg_bin
from app.db import SessionLocal, Destination, Clip, Project, set_setting, get_setting
from app.ingest import video_size, stream_seconds
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
auth.rate_limited = lambda ip, kind="owner": False
ff = ffmpeg_bin()
mk = lambda *a: subprocess.run([ff, "-y", "-v", "error", *a], cwd=d, capture_output=True, text=True)
mk("-f", "lavfi", "-i", "color=c=0x808080:size=1280x720:rate=25:duration=12", "-f", "lavfi", "-i", "sine=frequency=300:duration=12", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "grey.mp4")
proj = NS(title="Old Delhi Street Food Tour", channel="The Food Monster", mode="cc")
CUES = [{"s": 0, "e": 12, "t": "a caption that is shown for the whole clip"}]


def clip(**k):
    base = dict(id=1, aspect="9:16", fit=None, effects=[], overlays=[], caption_style={}, captions=[], orig_volume=1.0, voice_offset=0,
                voice_fit=None, reframe=True, frame_title=False, thumb_text=None, thumb_face=None, destination_id=None, title=None, cta_on=None)
    base.update(k)
    return NS(**base)


EN = {"on": True, "text": "Like and subscribe for more"}
# ---------- where and when (no rendering) ----------
p = reminder.plan(clip(), 1080, 1920, 30.0, EN)
ok(p and p["start"] == 25.5 and p["end"] == 28.7, f"30 s clip: shown once, {p['start']}–{p['end']} s (about 3 s, near the end)")
ok(reminder.plan(clip(), 1080, 1920, 7.9, EN) is None and reminder.plan(clip(), 1080, 1920, 8.0, EN)["start"] == 5.0, "clips under 8 s get none; never in the first 5 s")
p8 = reminder.plan(clip(), 1080, 1920, 8.0, EN)
ok(p8["end"] <= 7.6 and p8["end"] - p8["start"] >= 2.5, f"8 s clip: {p8['start']}–{p8['end']} s, gone before the end")
ok(reminder.plan(clip(cta_on=False), 1080, 1920, 30.0, EN) is None and reminder.plan(clip(), 1080, 1920, 30.0, {"on": False, "text": "x y z"}) is None, "off for the clip, or off for the account -> none")
box = lambda q: (q["cx"] - q["w"] / 2, q["cy"] - q["h"] / 2, q["cx"] + q["w"] / 2, q["cy"] + q["h"] / 2)
x0, y0, x1, y1 = box(p)
ok(x1 <= 1080 * 0.85 and y1 <= 1920 * 0.78 and y0 >= 1920 * 0.32 and x0 >= 1080 * 0.04, f"Short: inside the free area — not the bottom 22 %, not the right 15 %, below the title band ({x0:.0f},{y0:.0f})–({x1:.0f},{y1:.0f})")
ok(p["w"] <= 1080 * 0.5 and p["h"] <= 1920 * 0.04, f"Short: small ({p['w']}×{p['h']} px on 1080×1920)")
longest = {"on": True, "text": "W" * 40}
pl = reminder.plan(clip(), 1080, 1920, 30.0, longest)
ok(box(pl)[2] <= 1080 * 0.85 + 1 and box(pl)[0] >= 0 and pl["fs"] < p["fs"], f"the longest custom text (40 wide letters) still fits: smaller letters ({pl['fs']} < {p['fs']}), right edge {box(pl)[2]:.0f}")
big = clip(caption_style={"on": True, "pos": "bottom", "size": "L"}, captions=[{"s": 0, "e": 30, "t": "word " * 40}])
pc = reminder.plan(big, 1080, 1920, 30.0, EN)
ok(pc["cy"] < p["cy"] and pc["cy"] >= 1920 * 0.56 - 1,f"very tall captions: the reminder moves up, above them ({pc['cy']} < {p['cy']})")
pw = reminder.plan(clip(aspect="16:9"), 1280, 720, 30.0, EN)
ok(box(pw)[0] > 1280 * 0.5 and box(pw)[3] < 720 * 0.15 and box(pw)[2] <= 1280 * 0.98, f"16:9: small pill in the top-right corner {tuple(round(v) for v in box(pw))}")
pt = reminder.plan(clip(aspect="16:9", caption_style={"on": True, "pos": "top"}, captions=CUES), 1280, 720, 30.0, EN)
ok(box(pt)[2] < 1280 * 0.5 and box(pt)[1] > 720 * 0.6, "16:9 with captions at the top: bottom-left instead")
hd = reminder.plan(clip(aspect="16:9"), 1920, 1080, 30.0, EN)
ok(abs(hd["w"] / 1920 - pw["w"] / 1280) < 0.01, "Full HD video: same share of the picture")
ph = reminder.plan(clip(), 1080, 1920, 30.0, {"on": True, "text": dict(reminder.PRESETS)["hi"]})
ok(ph["font"] == "Noto Sans Devanagari" and ph["fs"] > p["fs"] and box(ph)[2] <= 1080 * 0.85, "Hindi wording: Hindi font, sized to look the same, fits")
ev = reminder.ass_events(p)
ok(len(ev) == 2 and all("\\fad(400,400)" in e for e in ev) and "\\p1" in ev[0] and ev[1].endswith("Like and subscribe for more") and not any(t in "".join(ev) for t in ("\\move", "\\t(", "\\k")), "two subtitle lines (rounded pill + words), gentle fade, nothing that moves or blinks")
ok("\x08" not in "".join(ev) and reminder.ass_events({**p, "text": "a{\\b1}b\nc"})[1].endswith("a(\\\u2060b1)b c"), "wording cannot inject subtitle commands")
# ---------- wording ----------
ok(reminder.clean("  Like 👍 & subscribe!! 🔔\n now ") == "Like & subscribe!! now", "emoji and line breaks are taken out of custom words")
ok(len(reminder.clean("x" * 90)) == 40 and reminder.clean("{\\an8}<b>") == "an8b", "at most 40 letters; no markup")
ok(all(len(t) <= 40 and reminder.clean(t) == t for _, t in reminder.PRESETS), "every ready-made wording is short and drawable")
# ---------- real renders ----------
def do(name, c, cfg=EN, dur=12.0):
    W, H = render.DIMS[c.aspect]
    cta = reminder.plan(c, W, H, dur, cfg)
    fc = render.build_filter(c, W, H, dur, d, False, True, "", None, (1280, 720), 0.0, None, cta)
    r = mk("-i", "grey.mp4", "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", str(dur), name + ".mp4")
    if r.returncode:
        print(fc); print(r.stderr[-500:])
    return r.returncode == 0, cta


def frame(name, t, W, H) -> bytes:
    """One frame as grey pixels (W*H bytes)."""
    r = subprocess.run([ff, "-v", "error", "-ss", f"{t:.2f}", "-i", str(d / (name + ".mp4")), "-frames:v", "1", "-vf", "format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    assert len(r.stdout) == W * H, (name, t, len(r.stdout))
    return r.stdout


def compare(a: bytes, b: bytes, q, W, H):
    """-> (biggest change outside the pill's box, brightest and darkest pixel inside the pill in frame a)."""
    bx0, by0, bx1, by1 = q["cx"] - q["w"] // 2 - 6, q["cy"] - q["h"] // 2 - 6, q["cx"] + q["w"] // 2 + 6, q["cy"] + q["h"] // 2 + 6
    out = 0
    for y in range(0, H, 3):
        ra, rb = a[y * W:(y + 1) * W], b[y * W:(y + 1) * W]
        if ra == rb:
            continue
        for x in range(0, W, 2):
            if not (by0 <= y <= by1 and bx0 <= x <= bx1) and abs(ra[x] - rb[x]) > out:
                out = abs(ra[x] - rb[x])
    y = q["cy"]
    inside = [a[yy * W + x] for yy in range(y - q["h"] // 4, y + q["h"] // 4) for x in range(q["cx"] - (q["w"] - q["h"]) // 2, q["cx"] + (q["w"] - q["h"]) // 2)]
    return out, max(inside), min(inside)


for name, c in (("full", clip()), ("fill", clip(fit="fill")), ("v169", clip(aspect="16:9")), ("square", clip(aspect="1:1"))):
    W, H = render.DIMS[c.aspect]
    good, q = do(name, c)
    good2, none = do(name + "_off", NS(**{**vars(c), "cta_on": False}))
    mid = (q["start"] + q["end"]) / 2
    out, white, dark = compare(frame(name, mid, W, H), frame(name + "_off", mid, W, H), q, W, H)
    ok(good and good2 and none is None and video_size(d / (name + ".mp4")) == (W, H) and white > 200 and dark < 100,
       f"{name}: the pill is in the picture at {mid:.1f} s (dark pill {dark}, white words {white})")
    ok(out < 12, f"{name}: nothing outside the small pill changes (biggest difference {out})")
    b4 = compare(frame(name, 4.0, W, H), frame(name + "_off", 4.0, W, H), q, W, H)
    af = compare(frame(name, q["end"] + 0.3, W, H), frame(name + "_off", q["end"] + 0.3, W, H), q, W, H)
    same_in = lambda t: frame(name, t, W, H) == frame(name + "_off", t, W, H) or max(abs(x - y) for x, y in zip(frame(name, t, W, H)[::5], frame(name + "_off", t, W, H)[::5])) < 12  # noqa: E731
    ok(b4[0] < 12 and af[0] < 12 and same_in(4.0) and same_in(q["end"] + 0.3), f"{name}: absent before its time (4 s) and after it ({q['end'] + 0.3:.1f} s)")
_, q = do("fade", clip())
half = compare(frame("fade", q["start"] + 0.2, 1080, 1920), frame("full_off", q["start"] + 0.2, 1080, 1920), q, 1080, 1920)
full = compare(frame("fade", q["start"] + 1.0, 1080, 1920), frame("full_off", q["start"] + 1.0, 1080, 1920), q, 1080, 1920)
ok(120 < half[1] < full[1] - 15, f"it fades in (words at +0.2 s: {half[1]}, fully shown: {full[1]})")
good, q = do("hindi", clip(), {"on": True, "text": dict(reminder.PRESETS)["hi"]})
on = compare(frame("hindi", (q["start"] + q["end"]) / 2, 1080, 1920), frame("full_off", (q["start"] + q["end"]) / 2, 1080, 1920), q, 1080, 1920)
ok(good and on[1] > 200 and on[2] < 100 and on[0] < 12, "Hindi wording renders inside its pill")
good, q = do("withcaps", clip(caption_style={"on": True, "pos": "bottom", "size": "M"}, captions=CUES, frame_title=None))
ok(good and abs(stream_seconds(d / "withcaps.mp4", "v:0") - 12) < 0.3, "together with captions: renders, same length")
# ---------- settings, the editor switch, re-render and Upload again ----------
with TestClient(main.app) as tc, TestClient(main.app) as c1, TestClient(main.app) as anon:
    tc.post("/api/login", json={"username": "admin", "password": "cta-test-pass-61"})
    r = tc.get("/api/settings/reminder").json()
    ok(r["on"] is True and r["preset"] == "share" and r["text"] == "Like, share and subscribe for more" and [x["key"] for x in r["presets"]] == ["share", "share_hinglish", "share_hi", "en", "en2", "hinglish", "hi"], "default: on, English with share; Hindi and Hinglish can be chosen")
    r = tc.post("/api/settings/reminder", json={"on": True, "preset": "hinglish", "custom": ""}).json()
    ok(r["text"] == "Pasand aaya? Like aur subscribe karein" and reminder.settings()["preset"] == "hinglish", "Hinglish chosen")
    r = tc.post("/api/settings/reminder", json={"on": True, "preset": "custom", "custom": "Follow for daily food 😋"})
    ok(r.status_code == 200 and r.json()["text"] == "Follow for daily food", "own words: saved without the emoji")
    bad = tc.post("/api/settings/reminder", json={"on": True, "preset": "custom", "custom": "😋😋"})
    ok(bad.status_code == 400 and "plain words" in bad.json()["detail"], "own words with nothing drawable: clear message")
    ok(tc.post("/api/settings/reminder", json={"on": True, "preset": "custom", "custom": "x" * 41}).status_code == 400 and tc.post("/api/settings/reminder", json={"on": True, "preset": "nope"}).status_code == 400, "too long / unknown choice refused")
    r = tc.post("/api/settings/reminder", json={"on": False, "preset": "en", "custom": ""}).json()
    ok(r["on"] is False and reminder.plan(clip(), 1080, 1920, 30.0) is None, "switched off for the whole account: no clip gets it")
    tc.post("/api/settings/reminder", json={"on": True, "preset": "en", "custom": ""})
    set_setting("reminder", "not json")
    ok(reminder.settings()["on"] is True and reminder.settings()["text"] == "Like, share and subscribe for more", "a damaged setting falls back to the default")
    with SessionLocal() as s:
        s.add(Destination(channel_id="UCa", name="Foody", token="x")); s.commit()
        p = Project(url="https://youtu.be/abcdefghijk", video_id="abcdefghijk", title="T", channel="C", mode="cc", duration=600); s.add(p); s.commit()
        s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="rendered", title="one", destination_id=1)); s.commit()
        s.add(Clip(project_id=p.id, start=0, end=30, aspect="16:9", status="uploaded", title="two", destination_id=1, youtube_id="yt1", cta_on=False)); s.commit()
        c = s.get(Clip, 1)
        ok(c.cta_on is None and reminder.plan(c, 1080, 1920, 30.0) is not None, "a new clip (and every old one: empty column) has the reminder on")
        ok(reminder.plan(s.get(Clip, 2), 1280, 720, 30.0) is None, "…unless it was switched off for that clip")
    g = tc.get("/api/clips/1").json()
    ok("cta_on" in g and g["cta_on"] is None, "the editor gets the clip's switch")
    r = tc.put("/api/clips/1", json={"cta_on": True}).json()
    ok(r["status"] == "rendered", "saving 'on' for a clip that had it on by default changes nothing (no re-render asked)")
    r = tc.put("/api/clips/1", json={"cta_on": False}).json()
    ok(r["status"] == "draft" and r["cta_on"] is False, "switching it off for a clip asks for a new render")
    jobs.executor.submit = lambda *a, **k: None
    new = tc.post("/api/clips/2/reupload", json={"confirm": True}).json()["id"]
    with SessionLocal() as s:
        ok(s.get(Clip, new).cta_on is False, "'Upload again' keeps the clip's choice")
    # each customer has their own setting
    anon.post("/api/register", json={"name": "Asha Rao", "brand": "Asha Clips", "email": "asha@example.com", "password": "customer-one-pass-1", "accept_terms": True})
    uid = tc.get("/api/admin/users").json()["users"][0]["id"]
    tc.post(f"/api/admin/users/{uid}/approve")
    c1.post("/api/login", json={"username": "asha@example.com", "password": "customer-one-pass-1"})
    c1.post("/api/settings/reminder", json={"on": True, "preset": "hi", "custom": ""})
    ok(c1.get("/api/settings/reminder").json()["preset"] == "hi" and tc.get("/api/settings/reminder").json()["preset"] == "share", "the wording is each customer's own")
    with tenant.use(c1.get("/api/me").json()["tenant"]):
        ok(reminder.plan(clip(), 1080, 1920, 30.0)["font"] == "Noto Sans Devanagari", "…and a customer's renders use that customer's wording")
    ok(anon.get("/api/settings/reminder").status_code == 401, "closed without a login")
page = open(r"D:\integ-final\static\index.html", encoding="utf-8").read()
ok(all(x in page for x in ('id="e-cta"', 'id="rm-on"', 'id="rm-preset"', "cta_on:$('#e-cta').checked", "/api/settings/reminder")), "dashboard: the card in My channels and the switch in the editor")
print("DIR", d)

"""Thumbnail designer (app/thumbdesign.py): real thumbnails from small test videos (ffmpeg), sizes and dimensions,
the layout per clip id, the A/B variant, the Super Admin switch (off = the old thumbnail, also in a customer's copy),
the owner's own thumbnail text, custom thumbnails left alone, the slow re-make job (daily cap, YouTube allowance,
uploaded clips re-sent not re-rendered), "Another design", and a thumbnail before every upload. No real network."""
import io, json, os, re, shutil, socket, subprocess, sys, tempfile
from datetime import timedelta
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
os.environ["THUMB_REDESIGN_PER_DAY"] = "3"
if not os.environ.get("FFMPEG_BIN") and os.path.exists(r"D:\social medi engin\data\bin\ffmpeg.exe"):
    os.environ["FFMPEG_BIN"] = r"D:\social medi engin\data\bin\ffmpeg.exe"
WT = os.environ.get("RS_WT", r"D:\social medi engin\.claude\worktrees\agent-a3393795c2cecd874")
sys.path.insert(0, WT)

_connect = socket.socket.connect
def _no_net(self, addr):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise OSError("no network in tests: " + str(host))
    return _connect(self, addr)
socket.socket.connect = _no_net

from PIL import Image
from fastapi.testclient import TestClient
from app import main, acl, tenant, auth, jobs, youtube, telegram, quota, thumbs, thumbdesign, render, scheduler, faces
from app.config import DATA_DIR, ffmpeg_bin
from app.db import SessionLocal, Destination, Project, Clip, set_setting, get_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
pushed, covered = [], []
def fake_push(c):
    pushed.append(c.id); c.thumb_note, c.thumb_pending = "Thumbnail set on YouTube", False; return True
youtube.push_thumbnail = fake_push
_real_cover = render.apply_short_cover
def spy_cover(c):
    covered.append(c.id); return _real_cover(c)
render.apply_short_cover = spy_cover
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()

VID = Path(d) / "vids"; VID.mkdir()
def make_video(name, size, secs=4):
    p = VID / name
    r = subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=15:duration={secs}",
                        "-f", "lavfi", "-i", f"color=c=black:size={size}:rate=15:duration=0.6",
                        "-filter_complex", "[1:v][0:v]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-300:]
    return p
tall, wide = make_video("tall.mp4", "540x960"), make_video("wide.mp4", "960x540")
seq = [0]

def clip(status="rendered", aspect="9:16", title="Indian STREET FOOD Faceoff Clean vs Dirty for Tourists | Best Moment",
         publish_in_h=None, **kw):
    with SessionLocal() as s:
        dest = s.query(Destination).first()
        seq[0] += 1
        p = Project(url="https://youtu.be/vid%08d" % seq[0], video_id="vid%08d" % seq[0], title=title, channel="AfroTalesTV24",
                    mode="original", license="Original content (my own video)", duration=60)
        s.add(p); s.commit()
        c = Clip(project_id=p.id, start=0, end=4, aspect=aspect, status=status, title=title, description="d",
                 destination_id=dest.id if dest else None,
                 publish_at=(utcnow() + timedelta(hours=publish_in_h)) if publish_in_h is not None else None, **kw)
        s.add(c); s.commit()
        wd = DATA_DIR / "clips" / str(c.id); wd.mkdir(parents=True, exist_ok=True)
        src = tall if aspect == "9:16" else wide
        shutil.copyfile(src, wd / "body.mp4"); shutil.copyfile(src, wd / "out.mp4")
        c.output_path = str(wd / "out.mp4"); s.commit()
        return c.id

def get(cid):
    with SessionLocal() as s:
        return s.get(Clip, cid)

def make(cid):
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        out = thumbs.make_thumbnail(c, s.get(Project, c.project_id))
        c.thumb_path = str(out); s.commit()
        return out, c.thumb_style

with mk() as owner, mk() as anon, mk() as c1:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Owner channel", token="x")); s.commit()

    # ---------------------------------------------------------------- the switch
    ok(acl.DEFAULT.get("thumbs.designer") is True and dict((k, g) for k, g, _l, _d in acl.PERMS)["thumbs.designer"] == "clips",
       "Super Admin switch thumbs.designer exists (Clips group), on by default")
    ok(acl.rule_for("POST", "/api/clips/5/thumbnail/design")[0] == "thumbs.designer", "'Another design' address is guarded by the switch")
    ok("'thumbs.designer':'#e-tdesign'" in html and 'id="e-tdesign"' in html, "the page hides the 'Another design' button when it is off")
    src = open(os.path.join(WT, "app", "scheduler.py"), encoding="utf-8").read()
    ok('when_on("thumbs.designer"' in src and "thumb_redesign" in src, "the re-make job runs per account only with the switch on")

    # ---------------------------------------------------------------- words
    hw = thumbdesign.hook_words
    w1 = hw("Indian STREET FOOD Faceoff Clean vs Dirty for Tourists | Best Moment")
    ok(2 <= len(w1) <= 4 and "Best" not in w1 and "Moment" not in w1 and "Dirty" in w1, f"old title -> strong words, no '| Best Moment': {w1}")
    w2 = hw("Yeh Street Food Dekh Ke Hosh Ud Jayenge \U0001f631")
    ok(2 <= len(w2) <= 4 and not any("\U0001f631" in w for w in w2) and "Hosh" in w2, f"new hook title -> hook words, no emoji: {w2}")
    w3 = hw("Big Buck Bunny 60fps 4K 2024 - Official Blender Foundation Short Film | Reaction", "Blender Foundation")
    ok(not any(re.fullmatch(r"(?i)4k|60fps|2024|official|reaction|blender|foundation", w) for w in w3), f"years, 4K, fps, channel name dropped: {w3}")
    w4 = hw("Making Indian Most Clean Bread in Bulk | क्या आपने Bread बनते हुए देखी है कभी | Best Moment")
    ok(2 <= len(w4) <= 4, f"mixed Hindi title -> a few words: {w4}")

    # ---------------------------------------------------------------- real thumbnails
    a = clip()
    out, style = make(a)
    im = Image.open(out)
    ok(style == thumbdesign.VERSION and im.size == (1080, 1920) and im.format == "JPEG", f"9:16 designer thumbnail: 1080x1920 JPEG ({style})")
    ok(out.stat().st_size <= 1_900_000, f"at most 1.9 MB ({out.stat().st_size // 1024} KB)")
    first = out.read_bytes()
    b = clip(aspect="16:9")
    outb, styleb = make(b)
    ok(Image.open(outb).size == (1280, 720) and outb.stat().st_size <= 1_900_000, "16:9 designer thumbnail: 1280x720, at most 1.9 MB")
    ok(not list((DATA_DIR / "clips" / str(a)).glob("td*")), "no temporary files left next to the video")

    # layout per clip id, variant
    lays = [thumbdesign.layout_for(i) for i in range(40)]
    ok(lays == [thumbdesign.layout_for(i) for i in range(40)] and len(set(lays)) == 4, "the layout is fixed per clip id, all 4 layouts used")
    ok(all(thumbdesign.layout_for(i, 1) != thumbdesign.layout_for(i, 0) for i in range(40)), "the A/B variant always uses another layout")
    sc = [(9.0, 1.0), (8.5, 1.2), (7.0, 3.5), (2.0, 0.2)]
    ok(thumbdesign.choose_time(sc, 4.0, 0) == 1.0 and thumbdesign.choose_time(sc, 4.0, 1) == 3.5,
       "the variant takes the best frame far enough from the first one (another moment)")
    with SessionLocal() as s:
        c = s.get(Clip, a); c.thumb_variant = 1; s.commit()
    outv, _ = make(a)
    ok(outv.read_bytes() != first and Image.open(outv).size == (1080, 1920), "variant 1 makes a different thumbnail")
    with SessionLocal() as s:
        c = s.get(Clip, a); c.thumb_variant = 0; s.commit()
    from app.project_thumbs import remake_thumbnail
    with SessionLocal() as s:
        c = s.get(Clip, a); c.thumb_variant = 1
        ok(remake_thumbnail(s, c) and c.thumb_style == thumbdesign.VERSION, "the A/B swap (growth._push -> remake_thumbnail) uses the designer")
        c.thumb_variant = 0; s.commit()

    # every layout, Hindi, owner's text, a face
    for lay in range(4):
        p = thumbdesign.compose(Image.open(out).convert("RGB"), 1080, 1920, ["CLEAN", "VS", "DIRTY"], False, lay, None, 1.0,
                                Path(d), Path(d) / f"lay{lay}.jpg")
        ok(Image.open(p).size == (1080, 1920), f"layout {thumbdesign.LAYOUTS[lay]} renders")
    p = thumbdesign.compose(Image.open(out).convert("RGB"), 1080, 1920, ["ब्रेड", "बनते", "देखी?"], True, 1, None, 1.0, Path(d), Path(d) / "hi.jpg")
    ok(Image.open(p).size == (1080, 1920), "Devanagari words render")
    h = clip(thumb_text="मेरा अपना टेक्स्ट")
    with SessionLocal() as s:
        c = s.get(Clip, h)
        ws, own = thumbdesign.words_for(c, s.get(Project, c.project_id))
    ok(own and ws == ["मेरा", "अपना", "टेक्स्ट"], "the owner's own thumbnail text is used as typed")
    o = clip(thumb_text="my own words")
    with SessionLocal() as s:
        c = s.get(Clip, o)
        ws, own = thumbdesign.words_for(c, s.get(Project, c.project_id))
    ok(own and ws == ["MY", "OWN", "WORDS"], "owner's Latin text: same words, in capitals")
    face = Image.new("RGBA", (400, 500), (0, 0, 0, 0))
    from PIL import ImageDraw
    ImageDraw.Draw(face).ellipse((50, 20, 350, 480), fill=(220, 170, 140, 255))
    faces.DEFAULT_FACE.parent.mkdir(parents=True, exist_ok=True); face.save(faces.DEFAULT_FACE)
    f1, st = make(clip())
    f2, st2 = make(clip(aspect="16:9"))
    ok(st == st2 == thumbdesign.VERSION and Image.open(f1).size == (1080, 1920) and Image.open(f2).size == (1280, 720),
       "with the channel's face photo: both sizes still made by the designer")
    faces.DEFAULT_FACE.unlink()

    # kept frame (the video went to the PC)
    k = clip()
    with SessionLocal() as s:
        c = s.get(Clip, k); thumbs.save_frame(c, s.get(Project, c.project_id), 0)
    wd = DATA_DIR / "clips" / str(k)
    kept = Image.open(wd / "thumb_frame_v0.png")
    (wd / "body.mp4").unlink(); (wd / "out.mp4").unlink()
    outk, stk = make(k)
    ok(kept.size == (540, 960) and stk == thumbdesign.VERSION and Image.open(outk).size == (1080, 1920),
       "the full-size kept frame makes a designer thumbnail when the video is only on the PC")
    real = thumbdesign.enabled
    thumbdesign.enabled = lambda: False
    outk2, stk2 = make(k)
    ok(stk2 == thumbdesign.CLASSIC and Image.open(outk2).size == (1080, 1920), "the old thumbnail copes with a full-size kept frame too")

    # ---------------------------------------------------------------- switch off -> the old thumbnail
    c_off = clip()
    out_off, st_off = make(c_off)
    ok(st_off == thumbdesign.CLASSIC and Image.open(out_off).size == (1080, 1920), "switch off: the old thumbnail ('classic')")
    thumbdesign.enabled = real
    with SessionLocal() as s:  # the designer failing never leaves a clip without a thumbnail
        c = s.get(Clip, c_off)
        orig = thumbdesign.make
        thumbdesign.make = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        o2 = thumbs.make_thumbnail(c, s.get(Project, c.project_id))
        thumbdesign.make = orig
        ok(c.thumb_style == thumbdesign.CLASSIC and o2.exists(), "the designer failing -> the old thumbnail, never none")

    # a customer with the switch off (their own copy)
    anon.post("/api/register", json={"name": "Asha", "brand": "Asha Studio", "email": "asha@example.com", "password": PW, "accept_terms": True})
    aid = next(u["id"] for u in owner.get("/api/admin/users").json()["users"] if u["email"] == "asha@example.com")
    tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
    c1.post("/api/login", json={"username": "asha@example.com", "password": PW})
    with tenant.use(tid):
        set_setting("onboarding", '{"finished": true}')
        with SessionLocal() as s:
            s.add(Destination(channel_id="UC-asha", name="Asha channel", token="x")); s.commit()
        ok(thumbdesign.enabled(), "a customer's copy has the designer on by default")
        cc = clip()
        _o, st_c = make(cc)
        ok(st_c == thumbdesign.VERSION, "a customer's clip gets a designer thumbnail")
    owner.post(f"/api/admin/users/{aid}/access", json={"perms": {"thumbs.designer": False}})
    with tenant.use(tid):
        ok(not thumbdesign.enabled(), "Super Admin switched it off for that customer")
        _o, st_c2 = make(cc)
        ok(st_c2 == thumbdesign.CLASSIC, "that customer's clips get the old thumbnail")
        r = c1.post(f"/api/clips/{cc}/thumbnail/design")
        ok(r.status_code == 403, f"'Another design' refused for that customer ({r.status_code})")
    ok(thumbdesign.enabled(), "the owner's own copy is not affected")

    # ---------------------------------------------------------------- "Another design"
    ad = clip()
    r1 = owner.post(f"/api/clips/{ad}/thumbnail/design").json()
    r2 = owner.post(f"/api/clips/{ad}/thumbnail/design").json()
    base = thumbdesign.layout_for(ad)
    ok(r1.get("thumb_style") == f"d1:{(base + 1) % 4}" and r2.get("thumb_style") == f"d1:{(base + 2) % 4}",
       f"'Another design' cycles the layouts and keeps the choice ({r1.get('thumb_style')}, {r2.get('thumb_style')})")
    ok(ad in covered, "the Short's opening frame is made again with the new design")
    with SessionLocal() as s:
        c = s.get(Clip, ad)
        thumbs.make_thumbnail(c, s.get(Project, c.project_id))
        ok(c.thumb_style == r2["thumb_style"], "a later re-make keeps the layout the owner chose")

    # ---------------------------------------------------------------- the slow re-make job
    with SessionLocal() as s:  # start clean: only the clips below are old-style
        for c in s.query(Clip).all():
            c.thumb_style = c.thumb_style or "d1"
        s.commit()
    custom = clip(thumb_custom=True, thumb_style=None)
    soon = clip(status="queued", publish_in_h=1, thumb_style=None)
    up_public = clip(status="uploaded", youtube_id="YTPUB", publish_in_h=-5, thumb_style=None)
    up_wait = clip(status="uploaded", youtube_id="YTWAIT", publish_in_h=30, thumb_style=None)
    olds = [clip(thumb_style="classic") for _ in range(4)]
    far = clip(status="queued", publish_in_h=48, thumb_style=None)
    with SessionLocal() as s:
        ids = [c.id for c in thumbdesign.candidates(s, utcnow())]
    ok(custom not in ids and soon not in ids and up_public not in ids, "custom thumbnails, clips about to upload and public videos are left alone")
    ok(ids[0] == up_wait and far in ids and all(x in ids for x in olds), "uploaded-not-public first, then queued, then waiting for review")
    pushed.clear(); covered.clear()
    used0 = quota.used
    quota.used = lambda: quota.DAILY_UNITS - 100  # almost nothing left today: no thumbnails.set
    r = thumbdesign.refresh_old()
    ok(r["pushed"] == 0 and not pushed and get(up_wait).thumb_style is None, "no room in the day's allowance: nothing is sent (uploads first)")
    quota.used = used0
    r = thumbdesign.refresh_old(max_run=10)
    ok(r["remade"] == 3 and r["left_today"] == 0, f"at most THUMB_REDESIGN_PER_DAY (3 here) a day: {r}")
    ok(pushed == [up_wait] and up_wait not in covered, "the uploaded, not-yet-public video: new thumbnail sent to YouTube, video not touched")
    ok(get(up_wait).thumb_style == thumbdesign.VERSION and far in covered, "a queued clip far from its upload: thumbnail + the Short's opening frame made again")
    r = thumbdesign.refresh_old(max_run=10)
    ok(r["remade"] == 0, "the day's cap holds on the next run")
    set_setting(thumbdesign._today_key(), "0")
    r = thumbdesign.refresh_old(max_run=10)
    ok(r["remade"] == 3 and get(custom).thumb_style is None and get(custom).thumb_custom, "next day: the next ones; the custom thumbnail is never replaced")
    ok(get(soon).thumb_style is None and get(up_public).thumb_style is None, "clip about to upload and public video still untouched")

    # ---------------------------------------------------------------- a thumbnail before every upload
    nt = clip(status="queued")
    with SessionLocal() as s:
        c = s.get(Clip, nt); c.thumb_path = None; s.commit()
        scheduler._ensure_thumb(s, c, s.get(Project, c.project_id))
        ok(c.thumb_path and Path(c.thumb_path).exists() and c.thumb_style == thumbdesign.VERSION, "a clip without a thumbnail gets one just before its upload")
    cu = clip(status="queued", thumb_custom=True)
    with SessionLocal() as s:
        c = s.get(Clip, cu); c.thumb_path = "/nope.jpg"; s.commit()
        scheduler._ensure_thumb(s, c, s.get(Project, c.project_id))
        ok(c.thumb_path == "/nope.jpg", "a custom thumbnail is never replaced before the upload")

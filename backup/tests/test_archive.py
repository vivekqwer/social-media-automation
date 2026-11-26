"""Uploaded videos are saved on the helper PC (sha256 checked), then removed from the server; they can be brought back.
Archive jobs only for uploaded, unsaved clips and only to that account's helper; a wrong sha256 is never recorded;
the clean-up deletes only after a confirmed copy (own files and pending Instagram/Facebook posts included); the
14-day Problems line; Get back + Upload again; the migration; ROUTES; the helper's side."""
import hashlib, importlib.util, io, json, os, sqlite3, subprocess, sys, tempfile, time
from datetime import timedelta
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = os.environ.get("RS_WT", r"D:\social medi engin\.claude\worktrees\agent-a18fd1cc3eb839fbb")
sys.path.insert(0, WT); sys.path.insert(0, WT + "/worker")
from fastapi.testclient import TestClient
from app import main, acl, tenant, auth, youtube, scheduler, telegram, jobs, meta, archive, db
from app.db import SessionLocal, Destination, Clip, Project, Worker, set_setting, get_setting, utcnow
from app import workers as W
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
submitted = []
jobs.executor.submit = lambda fn, *a, **k: submitted.append((getattr(fn, "__name__", "?"), a))
youtube.upload = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no real uploads in tests"))
META = {"connected": False, "on": True}
meta.status = lambda: dict(META)
CC = youtube.CC_LICENSE
OWN = "Original content (my own video)"
PW = "customer-pass-12345"
sha = lambda b: hashlib.sha256(b).hexdigest()
OTOK, CTOK = "tok-" + "a" * 40, None
mk = lambda: TestClient(main.app, raise_server_exceptions=False)


def ffmpeg_video(path: Path):
    ff = os.environ.get("FFMPEG_BIN") or "ffmpeg"
    r = subprocess.run([ff, "-y", "-f", "lavfi", "-i", "testsrc=size=180x320:rate=10:duration=2", "-pix_fmt", "yuv420p", str(path)],
                       capture_output=True)
    return r.returncode == 0 and path.exists()


with mk() as owner, mk() as c1, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    anon.post("/api/register", json={"name": "Asha", "brand": "Asha Studio", "email": "asha@example.com", "password": PW, "accept_terms": True})
    aid = next(u["id"] for u in owner.get("/api/admin/users").json()["users"] if u["email"] == "asha@example.com")
    tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
    c1.post("/api/login", json={"username": "asha@example.com", "password": PW})
    CTOK = f"t{tid}." + "b" * 40
    now = utcnow()

    def make(tid_, status="uploaded", mode="cc", license=CC, files=("out",), days=0, aspect="9:16", title="clip", meta_result=None):
        with tenant.use(tid_):
            with SessionLocal() as s:
                if not s.query(Destination).count():
                    s.add(Destination(channel_id=f"UC-{tid_}", name="ch", token="x")); s.commit()
                p = Project(url="https://youtu.be/abcdefghijk", video_id="abcdefghijk" if mode == "cc" else None, title="Src " + title,
                            channel="C", mode=mode, license=license, duration=600)
                s.add(p); s.commit()
                c = Clip(project_id=p.id, start=0, end=30, aspect=aspect, status=status, title=title, description="d",
                         youtube_id="yt" + title[:8] if status == "uploaded" else None, attempted_at=now - timedelta(days=days),
                         destination_id=s.query(Destination).first().id, meta_result=meta_result)
                s.add(c); s.commit()
                folder = Path(os.fspath(archive.DATA_DIR / "clips" / str(c.id)))
                (folder / "src").mkdir(parents=True, exist_ok=True)
                body = {}
                if "out" in files:
                    body["out"] = (f"video of {title} in {tid_} " * 50).encode()
                    (folder / "out.mp4").write_bytes(body["out"])
                    (folder / "body.mp4").write_bytes(b"body")
                    c.output_path = str(folder / "out.mp4")
                if "upload" in files:
                    body["upload"] = (f"my own file {title} " * 80).encode()
                    (folder / "src" / "upload.mp4").write_bytes(body["upload"])
                if "dl" in files:
                    (folder / "src" / "src_0_31.mp4").write_bytes(b"downloaded section")
                (folder / "thumb.jpg").write_bytes(b"jpg")
                c.thumb_path = str(folder / "thumb.jpg")
                s.commit()
                return c.id, folder, body

    def clip(tid_, cid):
        with tenant.use(tid_):
            with SessionLocal() as s:
                return s.get(Clip, cid)

    def setc(tid_, cid, **kw):
        with tenant.use(tid_):
            with SessionLocal() as s:
                c = s.get(Clip, cid)
                for k, v in kw.items():
                    setattr(c, k, v)
                s.commit()

    with SessionLocal() as s:
        s.add(Worker(name="pc", token_hash=W.token_hash(OTOK), approved=True, last_seen=utcnow())); s.commit()
    with tenant.use(tid):
        with SessionLocal() as s:
            s.add(Worker(name="asha-pc", token_hash=W.token_hash(CTOK), approved=True, last_seen=utcnow())); s.commit()
    HO = {"x-worker-token": OTOK, "x-worker-archive": "D%3A%5Chelper%5Carchive"}
    HC = {"x-worker-token": CTOK, "x-worker-archive": "C%3A%5Casha%5Carchive"}
    poll = lambda h: [j for j in owner.get("/api/worker/jobs", headers=h).json().get("jobs", []) if j["type"] in ("archive", "restore")]
    clear_leases = lambda t: [setc(t, c.id, archive_lease=None) for c in _all(t)]

    def _all(t):
        with tenant.use(t):
            with SessionLocal() as s:
                return s.query(Clip).all()

    # ---------- clips of the owner
    A, fa, ba = make(1, title="alpha", files=("out", "dl"))            # uploaded, downloaded clip
    B, fb, bb = make(1, status="queued", title="beta")                  # still waiting: never archived
    C, fc, _ = make(1, title="gamma", files=())                          # uploaded, but no file on the server
    D, fd, bd = make(1, mode="original", license=OWN, title="delta", files=("out", "upload"))  # my own uploaded video
    # ---------- one clip of the customer (its number repeats an owner clip's number)
    E, fe, be = make(tid, title="echo")

    j0 = [j for j in owner.get("/api/worker/jobs", headers={"x-worker-token": OTOK}).json()["jobs"] if j["type"] == "archive"]
    ok(j0 == [], "an older helper (it does not send its archive folder) gets no archive jobs")
    clear_leases(1)
    j1 = poll(HO)
    ids1 = sorted(j["clip_id"] for j in j1)
    ok(ids1 == sorted([A, D]) and len(j1) == archive.ARCHIVE_PER_POLL, f"uploaded clips with a file get archive jobs, at most {archive.ARCHIVE_PER_POLL} per poll ({ids1})")
    jd = next(j for j in j1 if j["clip_id"] == D)
    ja = next(j for j in j1 if j["clip_id"] == A)
    ok({f["name"]: f["sha"] for f in jd["files"]} == {"video": sha(bd["out"]), "source": sha(bd["upload"])},
       "my own video: the job carries the finished video AND the original file, with their sha256")
    ok(ja["key"] == f"t1-c{A}" and ja["info"]["youtube_id"] == "ytalpha" and ja["info"]["source_url"] and ja["info"]["title"] == "alpha",
       "the job names the clip with the account number and carries title, YouTube id and source link")
    ok(poll(HO) == [], "a job that was handed out is not handed out again while its lease runs")
    with SessionLocal() as s:
        w = s.query(Worker).first()
    ok(w.archive_dir == "D:\\helper\\archive", f"the helper's archive folder is remembered ({w.archive_dir})")
    jc = poll(HC)
    ok([j["clip_id"] for j in jc] == [E] and jc[0]["key"] == f"t{tid}-c{E}", f"the customer's helper gets only the customer's clip ({[(j['clip_id'], j['key']) for j in jc]})")
    ok(all(j["clip_id"] != E or j["key"].startswith("t1-") for j in j1), "the owner's helper never got the customer's clip")
    # file download: only the own copy's clips
    r = owner.get(f"/api/worker/archive/{A}/video", headers=HO)
    ok(r.status_code == 200 and r.content == ba["out"], "the owner's helper downloads the finished video")
    r = owner.get(f"/api/worker/archive/{E}/video", headers=HC)
    ok(r.status_code == 200 and r.content == be["out"], "the customer's helper downloads its own clip")
    r = owner.get(f"/api/worker/archive/{D}/video", headers=HC)
    ok(r.status_code == 404, f"the customer's helper cannot read an owner clip by its number ({r.status_code})")
    ok(owner.get(f"/api/worker/archive/{B}/video", headers=HO).status_code == 404, "a clip that waits for its upload is not handed out")
    ok(owner.get(f"/api/worker/archive/{A}/../../t.db", headers=HO).status_code == 404 and
       owner.get(f"/api/worker/archive/{A}/other", headers=HO).status_code == 404, "only the names video/source exist")
    ok(anon.get(f"/api/worker/archive/{A}/video").status_code == 401, "no helper token: refused")

    # ---------- sha256 check
    r = owner.post(f"/api/worker/archived/{A}", headers=HO, json={"files": {"video": "0" * 64}})
    ok(r.status_code == 409 and clip(1, A).archived_at is None, "a wrong sha256 is not recorded")
    r = owner.post(f"/api/worker/archived/{D}", headers=HO, json={"files": {"video": sha(bd["out"])}})
    ok(r.status_code == 409 and clip(1, D).archived_at is None, "my own video: without the original file's copy nothing is recorded")
    r = owner.post(f"/api/worker/archived/{E}", headers=HO, json={"files": {"video": sha(be["out"])}})
    ok(clip(tid, E).archived_at is None, "the owner's helper cannot confirm the customer's clip (its sha256 is checked in the owner's copy)")

    # ---------- clean-up before the copy is confirmed: nothing goes
    scheduler.cleanup_disk()
    ok((fa / "out.mp4").exists() and (fd / "src" / "upload.mp4").exists() and (fb / "out.mp4").exists(), "not saved on the PC yet: nothing is deleted")
    r = owner.post(f"/api/worker/archived/{A}", headers=HO, json={"files": {"video": sha(ba["out"])}})
    ca = clip(1, A)
    ok(r.status_code == 200 and ca.archived_at and ca.archived_sha == sha(ba["out"]), "the right sha256 is recorded with the time")
    r = owner.post(f"/api/worker/archived/{D}", headers=HO, json={"files": {"video": sha(bd["out"]), "source": sha(bd["upload"])}})
    ok(r.status_code == 200 and clip(1, D).archived_src_sha == sha(bd["upload"]), "my own video: both files recorded")
    r = owner.post(f"/api/worker/archived/{E}", headers=HC, json={"files": {"video": sha(be["out"])}})
    ok(r.status_code == 200 and clip(tid, E).archived_at, "the customer's helper confirms the customer's clip")
    ok(poll(HO) == [] and not any(j["clip_id"] in (A, D) for j in poll(HO)), "saved clips get no new archive job")

    # a real video for A so the frame for a later second thumbnail can be kept
    real = ffmpeg_video(fa / "real.mp4")
    if real:
        os.replace(fa / "real.mp4", fa / "out.mp4")
        setc(1, A, archived_sha=archive.sha256(fa / "out.mp4"), archived_size=(fa / "out.mp4").stat().st_size)
        ba["out"] = (fa / "out.mp4").read_bytes()
    scheduler.cleanup_disk()
    ca = clip(1, A)
    ok(not (fa / "out.mp4").exists() and not (fa / "body.mp4").exists() and not (fa / "src").exists(), "saved on the PC: out.mp4, body.mp4 and src are deleted")
    ok((fa / "thumb.jpg").exists() and ca.thumb_path and ca.youtube_id == "ytalpha" and ca.title == "alpha" and ca.output_path,
       "the thumbnail and every field stay")
    ok(not (fd / "out.mp4").exists() and not (fd / "src" / "upload.mp4").exists(), "my own video: deleted only now that both files are saved")
    with tenant.use(tid):
        scheduler.cleanup_disk()
    ok(not (fe / "out.mp4").exists(), "the customer's saved clip is cleaned in the customer's copy")
    ok((fb / "out.mp4").exists(), "a clip waiting in the queue keeps its video")
    if real:
        ok((fa / "thumb_frame_v1.png").exists(), "a frame for a later second thumbnail is kept before the video goes")
        from app.project_thumbs import remake_thumbnail
        with SessionLocal() as s:
            cc = s.get(Clip, A); cc.thumb_variant = 1
            try:
                done = remake_thumbnail(s, cc)
            except Exception as e:  # noqa: BLE001
                done = f"error {e}"
        ok(done is True, f"the second thumbnail can still be made without the video ({done})")
    else:
        print("INFO ffmpeg not found: kept-frame check skipped")
    r = owner.get(f"/api/clips/{A}/video")
    ok(r.status_code == 404 and "saved on your PC" in r.json()["detail"], "the dashboard's video link says where the video is")
    card = owner.get(f"/api/clips/{A}").json()
    ok(card["on_server"] is False and card["on_pc"] is True, "the clip says: saved on your PC, not on the server")

    # ---------- never otherwise
    F, ff, bf = make(1, title="foxtrot", files=("out",))  # Instagram/Facebook post still to come
    setc(1, F, archived_at=now, archived_sha=sha(bf["out"]))
    META["connected"] = True
    scheduler.cleanup_disk()
    ok((ff / "out.mp4").exists(), "a cross-post that still has to fetch the file: kept")
    setc(1, F, meta_result={"instagram": "1"}, meta_done=True)
    scheduler.cleanup_disk()
    ok(not (ff / "out.mp4").exists(), "…deleted once the cross-post is done")
    F2, ff2, bf2 = make(1, title="foxtwo", files=("out",), days=3)
    setc(1, F2, archived_at=now, archived_sha=sha(bf2["out"]))
    scheduler.cleanup_disk()
    ok(not (ff2 / "out.mp4").exists(), "a cross-post that never happened in 2 days does not keep the file for ever")
    F3, ff3, bf3 = make(1, title="foxwide", files=("out",), aspect="16:9")
    setc(1, F3, archived_at=now, archived_sha=sha(bf3["out"]))
    scheduler.cleanup_disk()
    ok(not (ff3 / "out.mp4").exists(), "a wide video is never cross-posted: deleted")
    META["connected"] = False
    G, fg, bg = make(1, mode="original", license=OWN, title="golf", files=("out", "upload"), days=40)
    scheduler.cleanup_disk()
    ok((fg / "src" / "upload.mp4").exists() and (fg / "out.mp4").exists(), "my own video, 40 days after upload but not saved on the PC: both files stay")
    H, fh, bh = make(1, title="hotel", files=("out", "dl"), days=5)
    scheduler.cleanup_disk()
    ok((fh / "out.mp4").exists() and not (fh / "src" / "src_0_31.mp4").exists(), "not saved: a downloaded section still goes after 3 days, out.mp4 stays")
    I, fi, bi = make(1, title="india", files=("out",))
    setc(1, I, archived_at=now, archived_sha=sha(bi["out"]))
    (fi / "out.mp4").write_bytes(b"changed after it was saved")
    scheduler.cleanup_disk()
    ok((fi / "out.mp4").exists() and clip(1, I).archived_at is None, "the file changed after it was saved: kept, and saved again first")
    J, fj, bj = make(1, mode="original", license=OWN, title="juliet", files=("out", "upload"))
    setc(1, J, archived_at=now, archived_sha=sha(bj["out"]))  # an archive without the original file
    scheduler.cleanup_disk()
    ok((fj / "out.mp4").exists() and (fj / "src" / "upload.mp4").exists(), "my own video saved without its original: nothing goes")

    # ---------- 14 days without a PC copy: one line in the Problems list
    late = lambda: [i for i in owner.get("/api/health").json()["items"] if i.get("key") == "archive_late"]
    K, fk, bk = make(1, title="kilo", files=("out",), days=20)
    set_setting("archive_since", (now - timedelta(days=30)).isoformat())
    L = late()
    ok(len(L) == 1 and L[0]["text"].startswith("2 uploaded video(s) are not saved on your PC 14 days"), f"one plain line for clips not saved within 14 days ({[x['text'][:60] for x in L]})")
    set_setting("archive_since", now.isoformat())
    ok(late() == [], "old uploads get the same 14 days from the day this started")
    set_setting("archive_since", (now - timedelta(days=30)).isoformat())
    ok(not any(i.get("key") == "archive_late" for i in c1.get("/api/health").json()["items"]), "the customer's list counts only the customer's clips")

    # ---------- getting it back, then Upload again
    ok(owner.post(f"/api/clips/{H}/get-back").status_code == 409, "a clip that was never saved on the PC: nothing to get back")
    ok(owner.post(f"/api/clips/{B}/get-back").status_code == 409, "a clip in the queue: refused")
    r = owner.post(f"/api/clips/{A}/get-back")
    ok(r.status_code == 200 and clip(1, A).restore_wanted, "Get the video back from the PC: asked")
    ok(owner.get("/api/overview").json()["helper"]["waiting"] >= 1, "the helper counts it as waiting work")
    clear_leases(1)
    jr = [j for j in poll(HO) if j["type"] == "restore"]
    ok(len(jr) == 1 and jr[0]["clip_id"] == A and jr[0]["files"] == [{"name": "video", "sha": sha(ba["out"])}], "the helper gets a restore job with the saved sha256")
    ok(poll(HC) == [] or all(j["clip_id"] != A or j["key"] != f"t1-c{A}" for j in poll(HC)), "the customer's helper never gets it")
    r = owner.post(f"/api/worker/restore/{A}/video", headers=HO, content=b"some other video")
    ok(r.status_code == 409 and not (fa / "out.mp4").exists() and not clip(1, A).restore_wanted, "a file with another sha256 is refused (and not asked again)")
    ok(not list(fa.glob("*.part")), "the refused file is not left behind")
    owner.post(f"/api/clips/{A}/get-back")
    r = owner.post(f"/api/worker/restore/{A}/video", headers=HC, content=ba["out"])
    ok(r.status_code in (404, 409) and not (fa / "out.mp4").exists(), f"another account's helper cannot put a file into this clip ({r.status_code})")
    r = owner.post(f"/api/worker/restore/{A}/video", headers=HO, content=ba["out"])
    ca = clip(1, A)
    ok(r.status_code == 200 and (fa / "out.mp4").read_bytes() == ba["out"] and ca.restore_wanted is None, "the right file is put back as out.mp4")
    scheduler.cleanup_disk()
    ok((fa / "out.mp4").exists(), "a video brought back stays a few days (for Upload again)")
    ok(owner.get(f"/api/clips/{A}/video").status_code == 200, "the dashboard plays it again")
    submitted.clear()
    r = owner.post(f"/api/clips/{A}/reupload", json={"confirm": True})
    new = clip(1, r.json().get("id", 0)) if r.status_code == 200 else None
    ok(new and new.status == "rendered" and Path(new.output_path).read_bytes() == ba["out"] and not any(n == "run_render" for n, _ in submitted),
       f"Upload again uses the video from the PC as it is (no new download, no re-render) ({r.status_code} {new and new.status})")
    ok(new and new.thumb_path and Path(new.thumb_path).exists(), "…with its thumbnail")
    marker = fa / "restored"
    os.utime(marker, (time.time() - 4 * 86400, time.time() - 4 * 86400))
    scheduler.cleanup_disk()
    ok(not (fa / "out.mp4").exists(), "after the few days it is removed from the server again (the PC still has it)")
    # own video: Upload again first needs it back; the original comes back too, then Upload again renders from it as before
    r = owner.post(f"/api/clips/{D}/reupload", json={"confirm": True})
    ok(r.status_code == 409 and "Get the video back from your PC first" in r.json()["detail"], "my own video only on the PC: Upload again says to get it back first")
    owner.post(f"/api/clips/{D}/get-back")
    clear_leases(1)
    jr = [j for j in poll(HO) if j["type"] == "restore" and j["clip_id"] == D]
    ok(jr and sorted(f["name"] for f in jr[0]["files"]) == ["source", "video"], "my own video: both files are asked back")
    r1 = owner.post(f"/api/worker/restore/{D}/video", headers=HO, content=bd["out"])
    ok(r1.status_code == 200 and clip(1, D).restore_wanted, "one file back: the ask stays until the other one is back too")
    r = owner.post(f"/api/worker/restore/{D}/source", headers=HO, content=bd["upload"])
    ok(r.status_code == 200 and not clip(1, D).restore_wanted and (fd / "src" / "upload.mp4").read_bytes() == bd["upload"], "both are back")
    submitted.clear()
    r = owner.post(f"/api/clips/{D}/reupload", json={"confirm": True})
    ok(r.status_code == 200 and any(n == "run_render" for n, _ in submitted), "Upload again of my own video renders from the original file, as before")
    # no PC copy (made before this): Upload again works as today (downloads the source again)
    submitted.clear()
    r = owner.post(f"/api/clips/{C}/reupload", json={"confirm": True})
    ok(r.status_code == 200 and any(n == "run_render" for n, _ in submitted) and clip(1, r.json()["id"]).status == "draft",
       "no saved copy: Upload again makes the clip again from the source, as before")
    # helper says it cannot find the file
    r = c1.post(f"/api/clips/{E}/get-back")
    ok(r.status_code == 200, "the customer asks its own clip back")
    owner.post(f"/api/worker/fail/{E}", headers=HC, json={"kind": "restore", "error": "file not in archive"})
    ce = clip(tid, E)
    ok(ce.restore_wanted is None and "could not send" in (ce.archive_note or ""), "the PC could not find it: the ask ends with a plain note")

    # ---------- review fixes
    OTOK2 = "tok-" + "z" * 40
    with SessionLocal() as s:
        s.add(Worker(name="pc2", token_hash=W.token_hash(OTOK2), approved=True, last_seen=utcnow())); s.commit()
        wid1 = s.query(Worker).filter(Worker.name == "pc").first().id
        wid2 = s.query(Worker).filter(Worker.name == "pc2").first().id
    HO2 = {"x-worker-token": OTOK2, "x-worker-archive": "E%3A%5Carch"}
    ok(clip(1, D).archived_by == wid1 and clip(1, D).archived_size == len(bd["out"]) and clip(1, D).archived_src_size == len(bd["upload"]),
       "the helper that saved it and the file sizes are recorded")
    R, fr, br = make(1, title="romeo", files=("out",))
    T, ft, bt = make(1, title="tango", files=("out",))
    owner.post(f"/api/worker/archived/{R}", headers=HO, json={"files": {"video": sha(br["out"])}})
    owner.post(f"/api/worker/archived/{T}", headers=HO2, json={"files": {"video": sha(bt["out"])}})
    ok(clip(1, T).archived_by == wid2, "a second helper's copy is recorded as its own")
    r = owner.post(f"/api/worker/fail/{R}", headers=HO2, json={"kind": "archive", "error": "late failure"})
    ok(clip(1, R).archived_at and "late failure" not in (clip(1, R).archive_note or ""), "a late failure from another helper never overwrites the saved state")
    r = owner.post(f"/api/worker/restore/{R}/video", headers=HO, content=br["out"])
    ok(r.status_code == 409 and (fr / "out.mp4").read_bytes() == br["out"], "nothing asked back: refused before a byte is saved")
    setc(1, R, restore_wanted=utcnow())
    scheduler.cleanup_disk()
    ok((fr / "out.mp4").exists(), "the clean-up never deletes a clip that is being asked back")
    setc(1, R, restore_wanted=None)
    scheduler.cleanup_disk()
    ok(not (fr / "out.mp4").exists() and not (ft / "out.mp4").exists(), "(both saved clips cleaned)")
    owner.post(f"/api/clips/{R}/get-back")
    owner.post(f"/api/clips/{T}/get-back")
    clear_leases(1)
    j1 = {j["clip_id"] for j in owner.get("/api/worker/jobs", headers=HO).json()["jobs"] if j["type"] == "restore"}
    j2 = {j["clip_id"] for j in owner.get("/api/worker/jobs", headers=HO2).json()["jobs"] if j["type"] == "restore"}
    ok(R in j1 and T not in j1 and T in j2 and R not in j2, f"each restore goes only to the helper that saved it ({j1}, {j2})")
    r = owner.post(f"/api/worker/restore/{R}/video", headers=HO2, content=br["out"])
    ok(r.status_code == 409 and not (fr / "out.mp4").exists(), "another helper of the same account cannot send it")
    r = owner.post(f"/api/worker/restore/{R}/video", headers=HO, content=br["out"] + b"x" * len(br["out"]))
    ok(r.status_code == 413 and not (fr / "out.mp4").exists() and not list(fr.glob("*.part")), "larger than the saved file + 10%: refused, nothing kept")

    def chunks():
        for _ in range(4):
            yield br["out"]
    r = owner.post(f"/api/worker/restore/{R}/video", headers=HO, content=chunks())
    ok(r.status_code == 413 and not (fr / "out.mp4").exists() and not list(fr.glob("*.part")), "a body without a length is counted as it arrives: refused too")
    r = owner.post(f"/api/worker/restore/{R}/video", headers=HO, content=br["out"])
    ok(r.status_code == 200 and (fr / "out.mp4").read_bytes() == br["out"] and (fr / "restored").exists(), "the right size goes through (marker written)")
    # restore asks that nobody answers end after 3 days
    setc(1, T, restore_wanted=utcnow() - timedelta(days=4))
    scheduler.cleanup_disk()
    ct = clip(1, T)
    ok(ct.restore_wanted is None and "within 3 days" in (ct.archive_note or ""), f"no answer in 3 days: the ask ends, said on the clip ({ct.archive_note})")
    # the helper that saved it was removed / is offline for long
    owner.delete(f"/api/workers/{wid2}")
    r = owner.post(f"/api/clips/{T}/get-back")
    ok(r.status_code == 409 and "removed" in r.json()["detail"], f"the helper that saved it was removed: said plainly ({r.json().get('detail', '')[:70]})")
    with SessionLocal() as s:
        s.get(Worker, wid1).last_seen = utcnow() - timedelta(days=5); s.commit()
    U, fu, bu = make(1, title="uniform", files=("out",))
    owner.post(f"/api/worker/archived/{U}", headers=HO, json={"files": {"video": sha(bu["out"])}})
    with SessionLocal() as s:  # (that answer counted as being seen: make it old again)
        s.get(Worker, wid1).last_seen = utcnow() - timedelta(days=5); s.commit()
    scheduler.cleanup_disk()
    r = owner.post(f"/api/clips/{U}/get-back")
    ok(r.status_code == 200 and "has not been online since" in (clip(1, U).archive_note or ""), f"its helper offline for days: asked, and the clip says so ({clip(1, U).archive_note})")
    setc(1, U, restore_wanted=None)
    with SessionLocal() as s:
        s.get(Worker, wid1).last_seen = utcnow(); s.commit()
    # one unreadable file never breaks the job list
    V1, fv1, _ = make(1, title="victor", files=("out",))
    V2, fv2, _ = make(1, title="vwhisky", files=("out",))
    real_sha = archive.sha256
    archive.sha256 = lambda p: (_ for _ in ()).throw(OSError("I/O error")) if Path(p).parent.name == str(V1) else real_sha(p)
    with SessionLocal() as s:  # only these two wait for a copy
        for c in s.query(Clip).all():
            c.archive_lease = None if c.id in (V1, V2) else utcnow()
        s.commit()
    r = owner.get("/api/worker/jobs", headers=HO)
    arch = [j["clip_id"] for j in r.json().get("jobs", []) if j["type"] == "archive"] if r.status_code == 200 else None
    archive.sha256 = real_sha
    ok(r.status_code == 200 and arch == [V2], f"a file that cannot be read is skipped; the other jobs still come ({r.status_code} {arch})")
    # Upload again of a clip that was not saved: made again from the source with the current look (as before)
    submitted.clear()
    r = owner.post(f"/api/clips/{V2}/reupload", json={"confirm": True})
    ok(r.status_code == 200 and any(n == "run_render" for n, _ in submitted) and clip(1, r.json()["id"]).status == "draft",
       "not saved on the PC: Upload again renders again (the old out.mp4 is not reused)")
    # a folder inside src never stops the clean-up
    (fh / "src" / "subdir").mkdir(parents=True, exist_ok=True)
    (fb / "src" / "subdir").mkdir(parents=True, exist_ok=True)
    try:
        scheduler.cleanup_disk(); crashed = None
    except Exception as e:  # noqa: BLE001
        crashed = e
    ok(crashed is None, f"a folder inside src: the clean-up goes on ({crashed})")
    # a new thumbnail text on a clip whose video is on the PC only
    if real:
        r = owner.post(f"/api/clips/{A}/thumbnail", json={"text": "NEW TEXT"})
        ok(not (fa / "out.mp4").exists() and r.status_code == 200, f"new thumbnail text on a saved clip uses the kept frame ({r.status_code} {r.text[:100]})")
    # an old helper (it never says where it saves): plain words in the Problems list and on the Helper card
    with SessionLocal() as s:
        for w in s.query(Worker).all():
            w.archive_dir = None
        s.commit()
    L = [i["text"] for i in owner.get("/api/health").json()["items"] if i.get("key") == "archive_late"]
    hp = owner.get("/api/overview").json()["helper"]
    ok(L and "Update the helper PC so it can save uploaded videos" in L[0] and hp["archive_ready"] is False,
       f"helper too old to save videos: said plainly ({L[:1]})")
    owner.get("/api/worker/jobs", headers=HO)  # it tells its folder again
    ok(owner.get("/api/overview").json()["helper"]["archive_ready"] is True, "…and the line changes once the helper is new enough")
    # the daily folder list
    with SessionLocal() as s:
        s.get(Worker, wid1).inventory_at = None; s.commit()
    ok(any(j["type"] == "inventory" for j in owner.get("/api/worker/jobs", headers=HO).json()["jobs"]), "once a day the helper is asked for its folder list")
    ok(not any(j["type"] == "inventory" for j in owner.get("/api/worker/jobs", headers=HO).json()["jobs"]), "…not again the same day")
    W1, fw1, bw1 = make(1, title="whiskey", files=("out",))
    owner.post(f"/api/worker/archived/{W1}", headers=HO, json={"files": {"video": sha(bw1["out"])}})  # still on the server
    cdir = lambda x: Path(os.fspath(archive.DATA_DIR / "clips" / str(x)))
    gone_before = {c.id for c in _all(1) if c.archived_by == wid1 and c.archived_at and not archive.all_back(c)}
    before = sorted(str(p) for p in Path(os.fspath(archive.DATA_DIR / "clips")).rglob("*"))
    r = owner.post("/api/worker/archive-inventory", headers=HO, json={"names": [f"t1-c{U}.mp4"]})
    cw1, cu = clip(1, W1), clip(1, U)
    ok(r.status_code == 200 and cw1.archived_at is None and "saved again" in (cw1.archive_note or ""), f"a copy missing on the PC while the server still has it: saved again ({r.text[:80]})")
    ok(U in gone_before and cu.archive_missing is None and len(gone_before) > 1 and all(clip(1, x).archive_missing for x in gone_before - {U}),
       "copies on the PC only that are not listed: marked missing (the listed one is fine)")
    ok(before == sorted(str(p) for p in Path(os.fspath(archive.DATA_DIR / "clips")).rglob("*")), "nothing is deleted because of the list")
    M = [i["text"] for i in owner.get("/api/health").json()["items"] if i.get("key") == "archive_missing"]
    ok(M and "missing from its archive folder" in M[0] and "Nothing was deleted" in M[0], "one plain Problems line for them")
    scheduler.cleanup_disk()
    ok((fw1 / "out.mp4").exists(), "the server keeps the file whose PC copy went missing")
    names = []
    for x in gone_before:
        cx = clip(1, x)
        names += [f"t1-c{x}.mp4"] + ([f"t1-c{x}.source.mp4"] if cx.archived_src_sha else [])
    owner.post("/api/worker/archive-inventory", headers=HO, json={"names": names})
    ok(not any(clip(1, x).archive_missing for x in gone_before), "listed again: no longer missing")
    ok(not [i for i in owner.get("/api/health").json()["items"] if i.get("key") == "archive_missing"], "…and the line is gone")

    # ---------- the Helper PC card
    hp = owner.get("/api/overview").json()["helper"]
    ok(hp["archived"] >= 2 and hp["archive_dir"] == "D:\\helper\\archive", f"My channels → Helper PC: how many are saved and where ({hp['archived']}, {hp['archive_dir']})")

    # ---------- ROUTES
    ok(acl.rule_for("GET", f"/api/worker/archive/{A}/video")[0] == acl.HELPER and acl.rule_for("POST", f"/api/worker/archived/{A}")[0] == acl.HELPER
       and acl.rule_for("POST", f"/api/worker/restore/{A}/video")[0] == acl.HELPER and acl.rule_for("POST", "/api/worker/archive-inventory")[0] == acl.HELPER, "the helper's new addresses are the helper's")
    ok(acl.rule_for("POST", f"/api/clips/{A}/get-back")[0] == "clips.create" and acl.rule_for("POST", f"/api/clips/{A}/restore")[0] == "clips.discard",
       "Get back needs 'Make a clip'; the older 'show again' address is unchanged")
    ok(all(r[2] == acl.SUPER for r in acl.ROUTES if r[1].startswith("/api/admin/")) and all(r[1].startswith("/api/admin/") for r in acl.ROUTES if r[2] == acl.SUPER),
       "/api/admin/* is Super Admin only and nothing else is")
    owner.post(f"/api/admin/users/{aid}/access", json={"perms": {"clips.create": False}})
    r = c1.post(f"/api/clips/{E}/get-back")
    ok(r.status_code == 403, f"switched off for the customer: refused ({r.status_code})")
    owner.post(f"/api/admin/users/{aid}/access", json={"perms": {"clips.create": True}})

    # ---------- after the merge with the phone helper / central helper: saving videos stays in the helper's own copy
    KIND3 = ("archive", "restore", "inventory")
    reg = mk()
    reg.post("/api/register", json={"name": "Ravi", "brand": "Ravi Studio", "email": "ravi@example.com", "password": PW, "accept_terms": True})
    aid2 = next(u["id"] for u in owner.get("/api/admin/users").json()["users"] if u["email"] == "ravi@example.com")
    tid2 = owner.post(f"/api/admin/users/{aid2}/approve").json()["tenant_id"]
    # 1. the laptop helper running today: old code, no x-worker-can, no x-worker-archive
    G, _, _ = make(1, status="fetching", title="golf", files=())
    setc(1, G, captions=[{"s": 0, "e": 1, "t": "x"}])
    Hh, _, _ = make(1, status="draft", title="hotel", files=())
    setc(1, Hh, tts_pending=json.dumps({"text": "namaste", "voice": "v"}))
    I1, fi1, bi1 = make(1, title="india", files=("out",))
    clear_leases(1)
    with SessionLocal() as s:
        for w in s.query(Worker).all():
            w.inventory_at = None
        s.commit()
        dir_before = s.get(Worker, wid1).archive_dir
    r = owner.get("/api/worker/jobs", headers={"x-worker-token": OTOK})
    jo = r.json().get("jobs", []) if r.status_code == 200 else None
    types = sorted({j["type"] for j in jo or []})
    ok(jo is not None and not [j for j in jo if j["type"] in KIND3], f"an old helper (no x-worker-can, no x-worker-archive): no archive / restore / inventory job ({types})")
    ok(any(j["type"] == "source" and j["clip_id"] == G for j in jo or []) and any(j["type"] == "tts" and j["clip_id"] == Hh for j in jo or [])
       and not any("ticket" in j for j in jo or []), f"…and it still gets its downloads and voice-overs, without tickets ({types})")
    with SessionLocal() as s:
        wo = s.get(Worker, wid1)
        ok(wo.can is None and wo.archive_dir == dir_before and clip(1, I1).archive_lease is None,
           "…its record says 'older helper' and nothing was leased for saving")
    # 2. the phone (can: source,analyze) — even if it sent an archive folder
    OTOK4 = "tok-" + "p" * 40
    with SessionLocal() as s:
        s.add(Worker(name="Android phone", token_hash=W.token_hash(OTOK4), approved=True, last_seen=utcnow())); s.commit()
        wid4 = s.query(Worker).filter(Worker.name == "Android phone").first().id
    clear_leases(1)
    for hdr in ({"x-worker-can": "source,analyze", "x-worker-slots": "2"},
                {"x-worker-can": "source,analyze", "x-worker-slots": "2", "x-worker-archive": "X%3A%5Cphone"}):
        r = owner.get("/api/worker/jobs", headers={"x-worker-token": OTOK4, **hdr})
        jp = r.json().get("jobs", []) if r.status_code == 200 else None
        ok(jp is not None and not [j for j in jp if j["type"] in KIND3],
           f"the phone never gets archive / restore / inventory jobs ({'with' if 'x-worker-archive' in hdr else 'without'} an archive folder; {sorted({j['type'] for j in jp or []})})")
    with SessionLocal() as s:
        wp = s.get(Worker, wid4)
        ok(wp.can == "source,analyze" and wp.archive_dir is None and wp.inventory_at is None, f"…its record keeps no archive folder ({wp.can}, {wp.archive_dir})")
    ok(clip(1, I1).archive_lease is None, "…and the waiting clip was not leased to it")
    with SessionLocal() as s:  # only this one waits for a copy (at most 2 per poll, oldest first)
        for c in s.query(Clip).all():
            c.archive_lease = None if c.id == I1 else utcnow()
        s.commit()
    ja =[j for j in owner.get("/api/worker/jobs", headers={**HO, "x-worker-can": "all"}).json()["jobs"] if j["type"] in KIND3]
    ok(any(j["type"] == "archive" and j["clip_id"] == I1 for j in ja), "the PC helper (can: all + its folder) gets that same archive job")
    with SessionLocal() as s:
        ok(s.get(Worker, wid1).can == "all", f"'all' is stored for the PC helper ({s.get(Worker, wid1).can})")
    # 3. the central helper: another account's archive / restore / inventory jobs never reach it
    OTOK3 = "tok-" + "k" * 40
    with SessionLocal() as s:
        s.add(Worker(name="central-pc", token_hash=W.token_hash(OTOK3), approved=True, last_seen=utcnow())); s.commit()
        cwid = s.query(Worker).filter(Worker.name == "central-pc").first().id
    HK = {"x-worker-token": OTOK3, "x-worker-can": "all", "x-worker-archive": "F%3A%5Ccentral"}
    owner.get("/api/worker/jobs", headers=HK)  # it says what it can do
    r = owner.post(f"/api/admin/workers/{cwid}/central", json={"on": True})
    ok(r.status_code == 200 and r.json()["central"], f"(a helper of the owner is made central: {r.status_code})")
    X2, fx2, bx2 = make(tid2, title="xray", files=("out",))           # the customer's uploaded video, waiting for a PC copy
    Y2, fy2, _ = make(tid2, title="yankee", files=())                  # saved earlier and asked back
    setc(tid2, Y2, archived_at=utcnow(), archived_sha="a" * 64, archived_size=100, restore_wanted=utcnow(), archived_by=cwid)
    F2, _, _ = make(tid2, status="fetching", title="foxtrot", files=())
    setc(tid2, F2, captions=[{"s": 0, "e": 1, "t": "x"}])
    with tenant.use(tid2):
        ok(get_setting("shared_helper") == "1", "(the new customer allows the shared helper)")
    clear_leases(1)
    with SessionLocal() as s:
        s.get(Worker, cwid).inventory_at = None; s.commit()
    jk = owner.get("/api/worker/jobs", headers=HK).json()["jobs"]
    foreign = [j for j in jk if j.get("ticket")]
    ok(any(j["type"] == "source" and j["clip_id"] == F2 for j in foreign), f"(the central helper does serve that customer's download: {[(j['type'], j.get('clip_id')) for j in foreign]})")
    ok(all(j["type"] in ("source", "analyze") for j in foreign), "another account's jobs are only downloads / analysis")
    ok(not [j for j in jk if j["type"] in KIND3 and (j.get("ticket") or str(j.get("key", "")).startswith(f"t{tid2}-"))],
       "the central helper never gets another account's archive / restore / inventory job, and none carries a ticket")
    ok(all(str(j.get("key", "")).startswith("t1-") for j in jk if j["type"] in ("archive", "restore")), "…its archive jobs are only the owner's own clips")
    ok(clip(tid2, X2).archive_lease is None and clip(tid2, Y2).archive_lease is None, "…and the customer's clips were not leased (restore not even to a same-numbered helper)")
    # 4. no job ticket opens the archive / restore / inventory addresses (nor fail with an archive kind)
    tk_src = W.make_ticket(tid2, "source", X2, cwid)
    tk_ana = W.make_ticket(tid2, "analyze", X2, cwid)
    tk_own = W.make_ticket(1, "source", I1, cwid)
    for name, tk in (("another account's", tk_src), ("an analysis", tk_ana), ("the owner's own", tk_own)):
        TH = {"x-worker-token": OTOK3, "x-worker-can": "all", "x-job-ticket": tk}
        codes = [owner.get(f"/api/worker/archive/{X2}/video", headers=TH).status_code,
                 owner.post(f"/api/worker/archived/{X2}", headers=TH, json={"files": {"video": sha(bx2["out"])}}).status_code,
                 owner.post(f"/api/worker/restore/{Y2}/video", headers=TH, content=b"evil bytes").status_code,
                 owner.post("/api/worker/archive-inventory", headers=TH, json={"names": []}).status_code,
                 owner.post(f"/api/worker/fail/{X2}", headers=TH, json={"kind": "archive", "error": "evil"}).status_code,
                 owner.post(f"/api/worker/fail/{Y2}", headers=TH, json={"kind": "restore", "error": "evil"}).status_code]
        ok(all(c == 403 for c in codes), f"{name} ticket opens none of the archive / restore / inventory addresses ({codes})")
    cx, cy = clip(tid2, X2), clip(tid2, Y2)
    ok(cx.archived_at is None and not cx.archive_note and cy.restore_wanted is not None and cy.archive_missing is None
       and not (fy2 / "out.mp4").exists() and not list(fy2.glob("*.part")), "…and nothing changed in the customer's copy")
    ok(clip(1, I1).archived_at is None and not (clip(1, I1).archive_note or "").startswith("Saving"), "…nor in the owner's")
    r = owner.get(f"/api/worker/archive/{I1}/video", headers=HK)
    ok(r.status_code == 200 and r.content == bi1["out"], "without a ticket the central helper is an ordinary helper of the owner's copy")

# ---------- migration of an existing database (owner + a customer)
from sqlalchemy import inspect, text
with db.engine.begin() as conn:
    conn.execute(text("ALTER TABLE clips DROP COLUMN archived_at"))
    conn.execute(text("ALTER TABLE workers DROP COLUMN archive_dir"))
cols = lambda eng, t: {c["name"] for c in inspect(eng).get_columns(t)}
ok("archived_at" not in cols(db.engine, "clips"), "an owner database from before (no archive columns)")
db.migrate()
ok({"archived_at", "archived_sha", "archived_src_sha", "archive_lease", "restore_wanted", "archive_note"} <= cols(db.engine, "clips")
   and "archive_dir" in cols(db.engine, "workers"), "the owner's database gets the new columns")
old = tenant.root(77) / "app.db"
con = sqlite3.connect(old)
con.execute("CREATE TABLE clips (id INTEGER PRIMARY KEY, project_id INTEGER, status VARCHAR(20), output_path VARCHAR(500))")
con.execute("CREATE TABLE workers (id INTEGER PRIMARY KEY, name VARCHAR(120))")
con.execute("INSERT INTO clips (id, project_id, status, output_path) VALUES (1, 1, 'uploaded', 'x')")
con.commit(); con.close()
db.migrate(77)
eng = db.engine_for(77)
ok({"archived_at", "archived_sha", "archived_src_sha", "restore_wanted"} <= cols(eng, "clips") and "archive_dir" in cols(eng, "workers"),
   "a customer's existing database gets them too")
with eng.connect() as conn:
    row = conn.execute(text("SELECT status, archived_at FROM clips WHERE id = 1")).first()
ok(row == ("uploaded", None), "existing rows keep their data; the new fields start empty")

# ---------- the helper's side
sys.argv = ["x", "http://localhost:9"]
spec = importlib.util.spec_from_file_location("cw", WT + "/worker/clipstudio_worker.py"); cw = importlib.util.module_from_spec(spec); spec.loader.exec_module(cw)
cw.log = lambda m: None
ok(cw.ARCHIVE_DIR.name == "RajiStudioArchive" and WT.lower() not in str(cw.ARCHIVE_DIR).lower(), f"helper: the archive folder is outside the project folder ({cw.ARCHIVE_DIR})")
os.environ["RAJI_ARCHIVE_DIR"] = r"E:\vids"
ok(str(cw._archive_dir()) == r"E:\vids", "helper: RAJI_ARCHIVE_DIR moves it")
del os.environ["RAJI_ARCHIVE_DIR"]
ok("archive/" in open(WT + "/.gitignore", encoding="utf-8").read().split(), ".gitignore keeps any archive folder in the project out of git")
cw.ARCHIVE_DIR = Path(tempfile.mkdtemp()) / "archive"
cw.OLD_ARCHIVE_DIR = Path(tempfile.mkdtemp()) / "old"
VID = b"finished video bytes " * 1000


class Resp:
    def __init__(self, status=200, body=b"", js=None):
        self.status_code, self.content, self._js, self.headers = status, body, js, {"content-type": "application/json"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cw.httpx.HTTPStatusError("x", request=None, response=self)

    def json(self):
        return self._js or {}

    def iter_bytes(self, n):
        for i in range(0, len(self.content), 1000):
            yield self.content[i:i + 1000]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


posts, served = [], {"body": VID}
cw.httpx.stream = lambda method, url, **kw: Resp(200, served["body"])
cw.httpx.post = lambda url, **kw: (posts.append((url, kw.get("json"), kw["content"].read() if kw.get("content") is not None else None)), Resp(200))[1]
job = {"type": "archive", "clip_id": 5, "key": "t1-c5", "files": [{"name": "video", "sha": sha(VID)}],
       "info": {"title": "हिंदी title", "youtube_id": "yt1", "publish_at": "2026-12-01T10:00:00Z", "source_url": "https://youtu.be/x", "clip": "t1-c5"}}
cw.handle(job, {"x-worker-token": "t"})
saved = cw.ARCHIVE_DIR / "t1-c5.mp4"
side = json.loads((cw.ARCHIVE_DIR / "t1-c5.json").read_text(encoding="utf-8"))
ok(saved.read_bytes() == VID and side["youtube_id"] == "yt1" and side["title"] == "हिंदी title" and side["files"]["video"]["sha256"] == sha(VID),
   "helper: the video lands in archive\\ with a small JSON next to it")
ok(posts and posts[-1][0].endswith("/api/worker/archived/5") and posts[-1][1] == {"files": {"video": sha(VID)}}, "helper: confirms with the sha256")
posts.clear(); served["body"] = b"damaged"
job2 = {**job, "clip_id": 6, "key": "t1-c6"}
cw.handle(job2, {"x-worker-token": "t"})
ok(not (cw.ARCHIVE_DIR / "t1-c6.mp4").exists() and not list(cw.ARCHIVE_DIR.glob("*.part")) and posts and posts[-1][0].endswith("/api/worker/fail/6")
   and posts[-1][1]["kind"] == "archive", "helper: a damaged download is not kept and is reported (no confirmation)")
posts.clear(); served["body"] = b"another video " * 50
job3 = {**job, "files": [{"name": "video", "sha": sha(served["body"])}]}
cw.handle(job3, {"x-worker-token": "t"})
ok(saved.read_bytes() == VID and (cw.ARCHIVE_DIR / f"t1-c5-{sha(served['body'])[:12]}.mp4").exists(), "helper: a saved file is never overwritten by a different one")
ok(cw.handle({**job, "key": "..\\..\\evil"}, {"x-worker-token": "t"}) is None and not (cw.ARCHIVE_DIR.parent / "evil.mp4").exists(), "helper: odd clip names are refused")
posts.clear()
cw.handle({"type": "restore", "clip_id": 5, "key": "t1-c5", "files": [{"name": "video", "sha": sha(VID)}]}, {"x-worker-token": "t"})
ok(posts and posts[-1][0].endswith("/api/worker/restore/5/video") and posts[-1][2] == VID, "helper: restore sends the saved file back")
posts.clear()
cw.handle({"type": "restore", "clip_id": 9, "key": "t1-c9", "files": [{"name": "video", "sha": "1" * 64}]}, {"x-worker-token": "t"})
ok(posts and posts[-1][0].endswith("/api/worker/fail/9") and posts[-1][1]["kind"] == "restore", "helper: a missing file is reported")
posts.clear()
cw.OLD_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True); (cw.OLD_ARCHIVE_DIR / "t1-c3.mp4").write_bytes(b"old")
cw.handle({"type": "inventory"}, {"x-worker-token": "t"})
ok(posts and posts[-1][0].endswith("/api/worker/archive-inventory") and {"t1-c5.mp4", "t1-c3.mp4"} <= set(posts[-1][1]["names"])
   and not any(n.endswith(".json") for n in posts[-1][1]["names"]), "helper: the daily folder list names the saved videos (new and old folder)")
order = []
cw.handle = lambda j, h: order.append(j["type"])
cw.handle_all([{"type": "archive"}, {"type": "source", "clip_id": 1}, {"type": "restore"}, {"type": "tts"}, {"type": "archive"}, {"type": "source", "clip_id": 2}], {})
ok(order[:2] == ["source", "source"] and order[2] == "tts" and order[3:] == ["restore", "archive", "archive"], f"helper: downloads first, saving last, one at a time ({order})")
ok(cw.ZIP_ALLOWED.fullmatch("worker/clipstudio_worker.py") and cw.ZIP_ALLOWED.fullmatch("app/archive.py") is not None, "the self-update still accepts the helper files")
ok("x-worker-archive" in open(WT + "/worker/clipstudio_worker.py", encoding="utf-8").read(), "helper: tells the server its archive folder on every poll")
print("DONE", flush=True)

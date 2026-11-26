"""PC folder -> dashboard -> YouTube. The helper watches a folder (default D:\\youtube), reports each video once (stable
for 60 s, keyed by sha256; moved/renamed = same item; deleted = missing), only details + a small picture. The dashboard
lists them (Approve / Approve all / Skip / topic); the switch approves new ones by itself ("approve": "manual" waits);
slots are one a day 18:00 IST after the last scheduled day (or the info.json date when later and free). The file comes
only shortly before its upload (at most N on the server, never under 1.5 GB free), is uploaded with publishAt and
deleted; a missed time moves the clip to a new day. Never-use list, tenant isolation, routes, the helper's side."""
import hashlib, importlib.util, io, json, os, shutil, sys, tempfile, time
from datetime import datetime, timedelta
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = os.environ.get("RS_WT", r"D:\pc-folder")
sys.path.insert(0, WT); sys.path.insert(0, WT + "/worker")
from fastapi.testclient import TestClient
from app import main, acl, tenant, auth, youtube, scheduler, telegram, jobs, meta, archive, autopilot, pcfolder, strikes, quota
from app.db import SessionLocal, Destination, Clip, Project, Worker, PcItem, set_setting, get_setting, utcnow
from app.db import engine_for as db_engine, migrate as db_migrate
from sqlalchemy import func
from app import workers as W
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
submitted = []
jobs.executor.submit = lambda fn, *a, **k: submitted.append((getattr(fn, "__name__", "?"), a))
sent = []
youtube.upload = lambda dest_id, path, title, desc, tags, publish_at=None: (sent.append({"path": path, "title": title, "publish_at": publish_at, "desc": desc}), "yt%05d" % len(sent))[1]
youtube.push_thumbnail = lambda c: True
meta.status = lambda: {"connected": False, "on": True}
autopilot.disk_free_gb = lambda: 10.0
IST = timedelta(hours=5, minutes=30)
sha = lambda b: hashlib.sha256(b).hexdigest()
JPG = b"\xff\xd8\xff\xe0" + b"j" * 200
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
OTOK, CTOK, PTOK = "tok-" + "a" * 40, None, "tok-" + "p" * 40
today = datetime.combine(utcnow().date(), datetime.min.time())
slot = lambda n: today + timedelta(days=n, hours=12, minutes=30)  # 18:00 IST, n days from today


def items(tid=1):
    with tenant.use(tid):
        with SessionLocal() as s:
            return {r.id: r for r in s.query(PcItem).all()}


def item_by(name, tid=1):
    return next(r for r in items(tid).values() if r.name == name)


def clip(cid, tid=1):
    with tenant.use(tid):
        with SessionLocal() as s:
            return s.get(Clip, cid)


def setc(cid, tid=1, **kw):
    with tenant.use(tid):
        with SessionLocal() as s:
            c = s.get(Clip, cid)
            for k, v in kw.items():
                setattr(c, k, v)
            s.commit()


def seti(iid, tid=1, **kw):
    with tenant.use(tid):
        with SessionLocal() as s:
            r = s.get(PcItem, iid)
            for k, v in kw.items():
                setattr(r, k, v)
            s.commit()


def f(name, data, folder="", info=None, thumb=True, w=1080, h=1920, dur=30.0, desc=""):
    return {"sha": sha(data), "rel": (folder + "\\" if folder else "") + name, "name": name, "folder": folder, "size": len(data),
            "duration": dur, "width": w, "height": h, "info": info, "description_txt": desc, "thumb": thumb}


with mk() as owner, mk() as c1, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    anon.post("/api/register", json={"name": "Asha", "brand": "Asha Studio", "email": "asha@example.com", "password": "customer-pass-12345", "accept_terms": True})
    aid = next(u["id"] for u in owner.get("/api/admin/users").json()["users"] if u["email"] == "asha@example.com")
    tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
    c1.post("/api/login", json={"username": "asha@example.com", "password": "customer-pass-12345"})
    CTOK = f"t{tid}." + "b" * 40
    for t_ in (1, tid):
        with tenant.use(t_):
            with SessionLocal() as s:
                s.add(Destination(channel_id=f"UC-{t_}", name=f"ch{t_}", token="x")); s.commit()
    with SessionLocal() as s:
        dst = s.query(Destination).first().id
        s.add(Worker(name="pc", token_hash=W.token_hash(OTOK), approved=True, last_seen=utcnow(), can="all"))
        s.add(Worker(name="phone", token_hash=W.token_hash(PTOK), approved=True, last_seen=utcnow(), can="source,analyze"))
        p = Project(url="u", video_id="vvvvvvvvvvv", title="src", channel="C", mode="cc", license=youtube.CC_LICENSE, duration=600)
        s.add(p); s.commit()
        for n in (1, 2, 3):  # already scheduled, one a day
            s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="queued", title=f"old {n}", description="d",
                       destination_id=dst, publish_at=slot(n), output_path=str(Path(d) / "nofile.mp4")))
        s.commit()
        wid = s.query(Worker).filter(Worker.name == "pc").first().id
        pwid = s.query(Worker).filter(Worker.name == "phone").first().id
        base_pid = p.id
    with tenant.use(tid):
        with SessionLocal() as s:
            s.add(Worker(name="asha-pc", token_hash=W.token_hash(CTOK), approved=True, last_seen=utcnow(), can="all")); s.commit()
            cwid = s.query(Worker).first().id
    HO = {"x-worker-token": OTOK, "x-worker-can": "all", "x-worker-pc": "1"}
    HC = {"x-worker-token": CTOK, "x-worker-can": "all", "x-worker-pc": "1"}
    rep = lambda body, h=HO: owner.post("/api/worker/pc-items", headers=h, json=body)

    # ---------- the folder setting
    j = owner.get("/api/worker/jobs", headers=HO).json()
    ok(j.get("pc_folder") == r"D:\youtube", f"the owner's helper watches D:\\youtube by default ({j.get('pc_folder')})")
    ok("pc_folder" not in owner.get("/api/worker/jobs", headers={"x-worker-token": OTOK, "x-worker-can": "all"}).json(), "an older helper (no x-worker-pc) is told nothing")
    ok("pc_folder" not in owner.get("/api/worker/jobs", headers={"x-worker-token": PTOK, "x-worker-can": "source,analyze", "x-worker-pc": "1"}).json(), "the phone (no folder) is told nothing")
    ok(owner.get("/api/worker/jobs", headers=HC).json().get("pc_folder") == "", "a customer's helper watches no folder until the customer chooses one")
    r = owner.post(f"/api/workers/{wid}/pc-folder", json={"folder": "videos"})
    ok(r.status_code == 400, f"a folder without a drive / full path is refused ({r.status_code})")
    r = owner.post(f"/api/workers/{wid}/pc-folder", json={"folder": r"E:\my videos"})
    ok(r.status_code == 200 and owner.get("/api/worker/jobs", headers=HO).json()["pc_folder"] == r"E:\my videos", "the dashboard changes the folder per helper")
    owner.post(f"/api/workers/{wid}/pc-folder", json={"default": True})
    ok(owner.get("/api/worker/jobs", headers=HO).json()["pc_folder"] == r"D:\youtube", "…and back to the default")
    wl = owner.get("/api/workers").json()
    ok(next(x for x in wl if x["id"] == wid)["pc_folder"] == r"D:\youtube" and next(x for x in wl if x["id"] == pwid)["pc_folder"] == "", "the Helper card shows each helper's folder (the phone: none)")

    # ---------- info.json
    ci = pcfolder.clean_info({"title": " T ", "tags": "a, #b ,c", "license": "Creative Commons Attribution license (reuse allowed)", "source_link": "https://youtu.be/abcdefghijk",
                              "suggested_upload_date": "2027-07-01", "approve": "Manual", "clip_id": 17, "credit": ["Credit: X", "CC BY"]}, "desc from txt")
    ok(ci["title"] == "T" and ci["tags"] == ["a", "b", "c"] and ci["licence"].startswith("Creative Commons") and ci["source"] == "https://youtu.be/abcdefghijk"
       and ci["manual"] and ci["clip_id"] == "17" and ci["description"] == "desc from txt" and "CC BY" in ci["credit"], f"info.json: names, tags, licence, source, manual, clip id, credit ({ci})")
    ok(ci["publish_at"] == "2027-07-01T12:30:00Z", f"a date alone = 18:00 India time that day ({ci['publish_at']})")
    ok(pcfolder.parse_date("2027-07-01T20:00:00+05:30") == datetime(2027, 7, 1, 14, 30) and pcfolder.parse_date("2027-07-01T20:00") == datetime(2027, 7, 1, 14, 30)
       and pcfolder.parse_date("nonsense") is None, "times with and without a zone (without = India time); nonsense is ignored")
    ok(pcfolder.clean_info(None) is None and pcfolder.clean_info([1]) is None and pcfolder.clean_info({"title": "x" * 999})["title"] == "x" * 200, "no info.json = None; long values are cut")
    ok(pcfolder.topic_from_name("012_street food delhi") == "street food delhi" and pcfolder.topic_from_name("goa-beach_trip.mp4") == "goa beach trip"
       and pcfolder.topic_from_name("VID_20241124_103000.mp4") == "" and pcfolder.topic_from_name("IMG-1234.MOV") == "", "topics from folder / file names; camera names give none")

    # ---------- a loose file of the owner: approved by itself (switch ON by default), the next daily slot
    A = b"goa beach video " * 500
    r = rep({"folder": r"D:\youtube", "exists": True, "items": [f("goa beach trip.mp4", A)]})
    ok(r.status_code == 200 and sha(A) in r.json()["ids"] and r.json()["thumbs"] == [sha(A)], f"a report gives the item id and asks for its picture ({r.status_code} {r.text[:200]})")
    ia = item_by("goa beach trip.mp4")
    ca = clip(ia.clip_id) if ia.clip_id else None
    ok(ia.state == "approved" and ca is not None and ca.status == "queued" and ca.pc_item_id == ia.id, f"switch ON (default): a loose file is approved by itself ({ia.state})")
    ok(ca.publish_at == slot(4), f"…on the next free day after the last scheduled one, 18:00 IST ({ca.publish_at} vs {slot(4)})")
    with SessionLocal() as s:
        pa = s.get(Project, ca.project_id)
    ok(pa.mode == "original" and pa.license == "Original content (my own video)" and not ca.credit_on, "a loose file is the owner's own video (no credit)")
    ok(ca.output_path is None and ca.archived_at is not None and ca.archived_sha == sha(A) and ca.archived_by == wid, "its video stays on the PC; the clip counts as saved there (no archive job)")
    ok(ca.title == "goa beach trip" and ("fill_topic", (ca.id, "goa beach trip")) in submitted, f"title from the file name, then the topic research runs ({ca.title}, {submitted[-1:]})")
    ok(ca.aspect == "9:16", "a tall, short video is a Short")
    r = owner.post(f"/api/worker/pc-thumb/{ia.id}?kind=frame", headers=HO, content=JPG)
    ok(r.status_code == 200 and owner.get(f"/api/pc-items/{ia.id}/thumb").content == JPG, "the small picture is saved and shown in the dashboard")
    ok(owner.post(f"/api/worker/pc-thumb/{ia.id}?kind=frame", headers=HO, content=b"<html>").status_code == 400, "a picture that is not JPEG / PNG is refused")
    ok(owner.post(f"/api/worker/pc-thumb/{ia.id}?kind=frame", headers=HO, content=b"\xff\xd8\xff" + b"x" * (3 * 1024 * 1024)).status_code == 413, "a picture over 2 MB is refused")
    ok(clip(ca.id).thumb_path is None, "a frame is not sent to YouTube as the thumbnail (only the folder's own thumbnail is)")

    # ---------- once per sha; moved / renamed = same item; deleted = missing
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("goa beach trip.mp4", A)]})
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("renamed.mp4", A, folder="trips")], "present": [{"sha": sha(A), "rel": "trips\\renamed.mp4", "name": "renamed.mp4", "folder": "trips"}]})
    rows = [r_ for r_ in items().values() if r_.sha == sha(A)]
    ok(len(rows) == 1 and rows[0].rel == "trips\\renamed.mp4" and rows[0].clip_id == ca.id, f"reported again / moved / renamed: still one item, one clip ({len(rows)}, {rows[0].rel})")
    ok(len([c for c in [clip(x) for x in range(1, 40)] if c is not None and c.pc_item_id == ia.id]) == 1, "…no second clip")

    # ---------- the camera name: waits for a few words
    V = b"camera video " * 400
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("VID_20241124_103000.mp4", V)]})
    iv = item_by("VID_20241124_103000.mp4")
    ok(iv.state == "new" and iv.clip_id is None, "a camera-named file waits (its name says nothing to make a title from)")
    pr = [x for x in owner.get("/api/health").json()["items"] if x["key"] == "pc_topic"]
    ok(len(pr) == 1, "…and the Problems list asks for a few words")

    # ---------- a sub-folder with info.json "approve": "manual": always waits
    M = b"generated clip 1 " * 300
    INFO_M = {"title": "Generated one", "description": "about it", "tags": ["x"], "license": youtube.CC_LICENSE, "source_url": "https://youtu.be/mmmmmmmmmmm",
              "channel_url": "https://www.youtube.com/channel/UCgoodgoodgoodgoodgood1", "approve": "manual", "suggested_upload_date": "2027-07-01"}
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", M, folder="001_generated one", info=INFO_M, desc="Words.\nCredit: Good channel (CC BY)")]})
    im = item_by("video.mp4")
    ok(im.state == "new" and im.info["manual"], "\"approve\": \"manual\" waits even with the switch ON")
    pcfolder.due(utcnow() + timedelta(hours=1))
    ok(item_by("video.mp4").state == "new", "…also after the timed job")

    # ---------- [M1] a sub-folder goes by itself ONLY with "approve": "auto"; no info.json / {} / no approve key / licence = waits
    N = b"no info video " * 300
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mov", N, folder="002_street food delhi")]})
    pcfolder.due(utcnow() + timedelta(hours=1))
    ok(item_by("video.mov").state == "new" and item_by("video.mov").clip_id is None, "[M1] a sub-folder without info.json waits for Approve")
    for k, inf in enumerate([{}, {"title": "No approve key"}, {"title": "Licence only", "license": youtube.CC_LICENSE, "source_link": "https://youtu.be/lllllllllll"},
                             {"approve": "", "title": "empty approve"}, {"approve": "yes"}, {"approve": "auto", "manual": True}]):
        data = f"m1 case {k} ".encode() * 300
        rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", data, folder=f"02{k}_case", info=inf)]})
        rk = next(r_ for r_ in items().values() if r_.sha == sha(data))
        ok(rk.state == "new" and rk.clip_id is None, f"[M1] info.json {inf} in a sub-folder: waits (never 'my own video' by itself)")
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("clip.mp4", b"auto but not video.mp4 " * 300, folder="028_named", info={"approve": "auto"})]})
    ok(item_by("clip.mp4").state == "new", "[CW1] a sub-folder whose file is not video.* never goes by itself")
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", b"no length " * 300, folder="029_nolen", info={"approve": "auto"}, dur=0)]})
    ok(next(r_ for r_ in items().values() if r_.sha == sha(b"no length " * 300)).state == "new", "[CW1] a video without a length (unreadable) never goes by itself")
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("unreadable clip.mp4", b"no picture " * 300, w=0, h=0)]})
    ok(item_by("unreadable clip.mp4").state == "new", "[CW1] a loose file without a picture never goes by itself")

    # ---------- info.json without "manual": by itself; CC credit kept; the suggested date (later and free) is used
    G = b"cc clip " * 333
    INFO_G = {"title": "CC one <b>", "tags": ["cc", "tag two"], "license": youtube.CC_LICENSE, "source_url": "https://youtu.be/ggggggggggg",
              "channel_url": "https://www.youtube.com/channel/UCgoodgoodgoodgoodgood2", "channel_name": "Good Two", "credit": "Credit: Good Two (CC BY)",
              "suggested_upload_date": slot(20).strftime("%Y-%m-%d"), "approve": "auto"}
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", G, folder="003_cc one", info=INFO_G, desc="Hook line")]})
    ig = next(r_ for r_ in items().values() if r_.sha == sha(G))
    cg = clip(ig.clip_id)
    with SessionLocal() as s:
        pg = s.get(Project, cg.project_id)
    ok(ig.state == "approved" and cg.publish_at == slot(20), f"with info.json: approved by itself on its suggested day (later and free) ({cg.publish_at} vs {slot(20)})")
    ok(cg.title == "CC one b" and "Credit: Good Two (CC BY)" in cg.description and "Hook line" in cg.description and cg.tags[:2] == ["cc", "tag two"],
       f"title / description / tags from info.json + description.txt; the credit line is kept ({cg.title!r})")
    ok(pg.mode == "cc" and pg.license == youtube.CC_LICENSE and pg.channel_url.endswith("2") and cg.meta_edited, "a CC item keeps its licence and source; its own words are not rewritten")
    H2 = b"same day " * 333
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", H2, folder="004_same day", info={**INFO_G, "title": "Same day", "source_url": "https://youtu.be/hhhhhhhhhhh"})]})
    ch2 = clip(next(r_ for r_ in items().values() if r_.sha == sha(H2)).clip_id)
    ok(ch2.publish_at == slot(21), f"a suggested day that is taken: the next free day after it ({ch2.publish_at})")
    E2 = b"early date " * 333
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", E2, folder="005_early", info={**INFO_G, "title": "Early", "suggested_upload_date": slot(1).strftime("%Y-%m-%d"), "source_url": "https://youtu.be/eeeeeeeeeee"})]})
    ce = clip(next(r_ for r_ in items().values() if r_.sha == sha(E2)).clip_id)
    ok(ce.publish_at == slot(22), f"a suggested day earlier than the line: the next free day after the last scheduled one ({ce.publish_at})")

    # ---------- the dashboard: list, Approve (with a topic), Skip, Show again, Approve all
    L = owner.get("/api/pc-items").json()
    ok({"VID_20241124_103000.mp4", "video.mp4", "video.mov"} <= {x["name"] for x in L["waiting"]} and L["auto"] is True and "rights" in L["notice"],
       f"From my PC lists what waits, the switch, and the one plain line about rights ({[x['name'] for x in L['waiting']]})")
    wv = next(x for x in L["waiting"] if x["name"].startswith("VID"))
    wm = next(x for x in L["waiting"] if x["name"] == "video.mp4" and x["title"] == "Generated one")
    ok(wv["needs_topic"] and not wv["has_info"] and wm["has_info"] and wm["manual"] and wm["title"] == "Generated one" and wm["suggested"] == "2027-07-01T12:30:00Z",
       "each item: title, thumbnail, duration, suggested date; the topic box only without info.json")
    ok(any(h["id"] == wid and h["folder"] == r"D:\youtube" for h in L["helpers"]) and not any(h["id"] == pwid for h in L["helpers"]), "the helpers' folders are shown (not the phone)")
    n_sub = len(submitted)
    r = owner.post(f"/api/pc-items/{iv.id}/approve", json={"topic": "  my village   wedding "})
    cv = clip(r.json().get("clip_id") or 0) if r.status_code == 200 else None
    ok(r.status_code == 200 and cv.status == "queued" and ("fill_topic", (cv.id, "my village wedding")) in submitted[n_sub:], f"Approve with a topic: queued, and the topic research runs ({r.status_code})")
    ok(owner.post(f"/api/pc-items/{iv.id}/approve", json={}).status_code == 409, "approving twice is refused")
    S = b"skip me " * 300
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("skip me.mp4", S)]})  # switch ON: goes by itself... so switch OFF first below
    owner.post("/api/pc-items/auto", json={"on": False})
    ok(get_setting("pc_auto") == "0" and owner.get("/api/pc-items").json()["auto"] is False, "the switch can be turned OFF")
    S2 = b"wait for me " * 300
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("wait for me.mp4", S2)]})
    isk = item_by("wait for me.mp4")
    ok(isk.state == "new", "switch OFF: a new loose file waits for Approve")
    pcfolder.due(utcnow() + timedelta(hours=2))
    ok(item_by("wait for me.mp4").state == "new", "…also after the timed job")
    r = owner.post(f"/api/pc-items/{isk.id}/skip")
    ok(r.status_code == 200 and item_by("wait for me.mp4").state == "skipped" and item_by("wait for me.mp4").clip_id is None, "Skip: it stays on the PC, nothing is made")
    owner.post(f"/api/pc-items/{isk.id}/unskip")
    ok(item_by("wait for me.mp4").state == "new", "Show again puts it back")
    T3 = b"third waiting " * 300
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("third one.mp4", T3)]})
    for nm in ("clip.mp4", "unreadable clip.mp4"):
        owner.post(f"/api/pc-items/{item_by(nm).id}/skip")
    for r_ in list(items().values()):
        if r_.folder and r_.state == "new" and r_.folder not in ("001_generated one", "002_street food delhi"):
            owner.post(f"/api/pc-items/{r_.id}/skip")
    n_wait = len([r_ for r_ in items().values() if r_.state == "new" and not r_.missing_at])
    r = owner.post("/api/pc-items/approve-all").json()
    ok(r.get("running") is not None and r.get("total") == n_wait, f"[CW6] Approve all runs in the background and says how many ({r})")
    for _ in range(300):
        b = owner.get("/api/pc-items").json()["bulk"]
        if not b.get("running"):
            break
        time.sleep(0.1)
    ok(b["approved"] == n_wait == 4 and b["done"] == n_wait and item_by("wait for me.mp4").state == "approved" and item_by("third one.mp4").state == "approved"
       and item_by("video.mov").state == "approved", f"Approve all: every waiting item, also the \"manual\" ones (it is the owner's press) ({b})")
    gm = next(r_ for r_ in items().values() if r_.sha == sha(M))
    ok(gm.state == "approved", "…the manual folder too")
    ok(("fill_topic", (item_by("video.mov").clip_id, "street food delhi")) in submitted, "…a sub-folder without info.json gets its folder's words as the topic")
    days = sorted(clip(x.clip_id).publish_at for x in items().values() if x.clip_id and clip(x.clip_id).status == "queued")
    ok(len(days) == len(set((t + IST).date() for t in days)), "one a day: no two PC videos share a day")
    ok(clip(gm.clip_id).publish_at == datetime(2027, 7, 1, 12, 30), f"the manual item keeps its suggested date ({clip(gm.clip_id).publish_at})")
    owner.post("/api/pc-items/auto", json={"on": True})

    # ---------- never-use list after a strike
    BAD = "https://www.youtube.com/channel/UCbadbadbadbadbadbadbad1"
    strikes._block(strikes.creator_key(BAD), "Bad Creator", BAD, 0)
    B = b"blocked creator " * 300
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", B, folder="006_bad", info={"title": "Bad", "license": youtube.CC_LICENSE, "source_url": "https://youtu.be/bbbbbbbbbbb", "channel_url": BAD})]})
    ib = next(r_ for r_ in items().values() if r_.sha == sha(B))
    ok(ib.state == "blocked" and ib.clip_id is None and "never-use" in (ib.note or ""), f"a creator on the never-use list is not approved, not even by the switch ({ib.state}: {ib.note})")
    ok(owner.post(f"/api/pc-items/{ib.id}/approve", json={}).status_code == 409, "…nor by Approve")
    with SessionLocal() as s:
        s.add(Project(url="https://youtu.be/kkkkkkkkkkk", video_id="kkkkkkkkkkk", title="old", channel="Bad", channel_url=BAD, mode="cc", license=youtube.CC_LICENSE)); s.commit()
    K = b"same video, other link " * 200
    owner.post("/api/pc-items/auto", json={"on": False})
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", K, folder="007_k", info={"title": "K", "source_url": "https://www.youtube.com/watch?v=kkkkkkkkkkk"})]})
    ik = next(r_ for r_ in items().values() if r_.sha == sha(K))
    ok(next(r_ for r_ in items().values() if r_.sha == sha(K)).state == "blocked" and owner.post(f"/api/pc-items/{ik.id}/approve", json={}).status_code == 409,
       "a source video of a blocked creator (known from an earlier clip) is refused too")
    owner.post("/api/pc-items/auto", json={"on": True})

    # ---------- a parked draft made on the PC (info.json clip id): no second copy
    with SessionLocal() as s:
        pp = Project(url="https://youtu.be/ppppppppppp", video_id="ppppppppppp", title="parked src", channel="Good", channel_url="https://www.youtube.com/channel/UCgoodgoodgoodgoodgood3",
                     mode="cc", license=youtube.CC_LICENSE, duration=600); s.add(pp); s.commit()
        pk = Clip(project_id=pp.id, start=100, end=135, aspect="9:16", status="draft", error="Parked: too many clips were waiting.", destination_id=dst, title="T", description="D")
        q2 = Clip(project_id=pp.id, start=300, end=335, aspect="9:16", status="queued", destination_id=dst, title="Q", description="D", publish_at=slot(60))
        s.add_all([pk, q2]); s.commit(); pkid, q2id = pk.id, q2.id
    P = b"parked made on pc " * 300
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", P, folder="008_parked", info={"title": "Parked one", "clip_id": f"t1-c{pkid}", "approve": "manual", "license": youtube.CC_LICENSE})]})
    ip = next(r_ for r_ in items().values() if r_.sha == sha(P))
    ok(clip(pkid).error.startswith("Parked:"), "[S3] the parked draft keeps its Parked note (nothing is lost)")
    autopilot.unpark_due(force=50)
    ok(clip(pkid).status == "draft", "[S3] …but the daily un-parking leaves it alone while the PC item waits")
    owner.post(f"/api/pc-items/{ip.id}/skip")
    with SessionLocal() as s:
        ok(pkid not in pcfolder.claimed_clips(s), "[S3] skipped: the draft is free again for the daily un-parking")
    owner.post(f"/api/pc-items/{ip.id}/unskip")
    with SessionLocal() as s:
        ok(pkid in pcfolder.claimed_clips(s), "…shown again: held again")
    n_clips = len([1 for x in range(1, 200) if clip(x) is not None])
    r = owner.post(f"/api/pc-items/{ip.id}/approve", json={})
    ok(r.status_code == 200 and r.json()["clip_id"] == pkid and clip(pkid).status == "queued" and clip(pkid).pc_item_id == ip.id and clip(pkid).title == "Parked one",
       f"Approve uses that same clip (its source, licence and moment) — no second clip ({r.status_code})")
    ok(len([1 for x in range(1, 200) if clip(x) is not None]) == n_clips, "…the number of clips is unchanged")
    Q = b"would be a twin " * 300
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", Q, folder="009_twin", info={"title": "Twin", "clip_id": str(q2id), "approve": "manual"})]})
    iq = next(r_ for r_ in items().values() if r_.sha == sha(Q))
    r = owner.post(f"/api/pc-items/{iq.id}/approve", json={})
    ok(r.status_code == 409 and "twice" in r.json()["detail"], f"a clip id that is already queued: refused, it would upload the same clip twice ({r.json().get('detail')})")
    ok(not owner.post(f"/api/pc-items/{iq.id}/skip").json().get("detail"), "(skipped)")

    # ---------- when the file is asked for: about a day before its turn, at most N on the server, never under 1.5 GB free
    with SessionLocal() as s:  # start clean: the old non-PC clips go away from the line for this part
        for c in s.query(Clip).filter(Clip.status == "queued", Clip.pc_item_id.is_(None)).all():
            c.status = "draft"
        s.commit()
        pcs = s.query(Clip).filter(Clip.status == "queued", Clip.pc_item_id.isnot(None)).order_by(Clip.publish_at).all()
        for i, c in enumerate(pcs):
            c.publish_at = slot(10 + i)  # far away: lead is the usual 2 days
        s.commit()
        pc_ids = [c.id for c in pcs]
    out = pcfolder.due()
    ok(out["asked"] == 0 and not any(r_.wanted_at for r_ in items().values()), f"usual 2-day lead: nothing is asked 10+ days ahead ({out})")
    setc(pc_ids[0], publish_at=utcnow() + timedelta(days=2, hours=20))
    out = pcfolder.due()
    ok(out["asked"] == 1 and items()[clip(pc_ids[0]).pc_item_id].wanted_at is not None, f"about a day before its turn (publish - lead - 1 day), the file is asked for ({out})")
    set_setting("upload_lead_days", "365")
    autopilot.disk_free_gb = lambda: 1.4
    out = pcfolder.due()
    ok(out["asked"] == 0, f"with less than 1.5 GB free nothing more is asked ({out})")
    autopilot.disk_free_gb = lambda: 10.0
    qc = quota.capacity
    quota.capacity = lambda: 50
    out = pcfolder.due()
    wanted = [r_ for r_ in items().values() if r_.wanted_at]
    ok(len(wanted) == pcfolder.MAX_FILES, f"a long lead: at most {pcfolder.MAX_FILES} PC files on (or coming to) the server at once ({len(wanted)}, {out})")
    quota.capacity = qc
    with SessionLocal() as s:
        for r_ in s.query(PcItem).all():
            r_.wanted_at = None
        s.commit()
    with SessionLocal() as s:  # a day of other uploads ahead of it: not yet
        for i in range(quota.capacity()):
            s.add(Clip(project_id=base_pid, start=0, end=30, aspect="9:16", status="queued", title=f"ahead {i}", description="d", destination_id=dst,
                       publish_at=slot(9) - timedelta(hours=i + 1), output_path=str(Path(d) / "nofile.mp4")))
        s.commit()
    setc(pc_ids[0], publish_at=slot(10))
    out = pcfolder.due()
    ok(out["asked"] == 0, f"when a day of other uploads comes first, the PC file is not asked yet ({out})")
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.title.like("ahead %")).all():
            c.status = "draft"
        s.commit()
    pcfolder.MAX_FILES, mf = 1, pcfolder.MAX_FILES
    out = pcfolder.due()
    first = clip(pc_ids[0])
    ifirst = items()[first.pc_item_id]
    ok(out["asked"] == 1 and ifirst.wanted_at is not None, f"…once they are out of the way it is asked ({out})")

    # ---------- the job, the file, the upload, the delete
    jo = [x for x in owner.get("/api/worker/jobs", headers=HO).json()["jobs"] if x["type"] == "pc_send"]
    ok(len(jo) == 1 and jo[0]["item_id"] == ifirst.id and jo[0]["sha"] == ifirst.sha and jo[0]["clip_id"] == first.id, f"the helper whose folder has it gets one \"send it\" job ({jo})")
    ok(not [x for x in owner.get("/api/worker/jobs", headers=HO).json()["jobs"] if x["type"] == "pc_send"], "…not again while it is sending")
    ok(not [x for x in owner.get("/api/worker/jobs", headers=HC).json()["jobs"] if x["type"] == "pc_send"], "the customer's helper never gets it")
    body_of = {sha(x): x for x in (A, V, M, N, G, H2, E2, S, S2, T3, P)}
    data = body_of[ifirst.sha]
    ok(owner.post(f"/api/worker/pc-file/{ifirst.id}", headers=HC, content=data).status_code == 404, "another account's helper cannot send it (its copy has no such item)")
    ok(owner.post(f"/api/worker/pc-file/{ifirst.id}", headers=HO, content=data + b"x").status_code == 413, "a file larger than the one in the list is refused while it arrives")
    r = owner.post(f"/api/worker/pc-file/{ifirst.id}", headers=HO, content=b"y" * len(data))
    ok(r.status_code == 409 and not pcfolder.on_server(clip(first.id)) and not list(pcfolder.video_path(first).parent.glob("*.part")), f"a different file (sha256) is not kept ({r.status_code})")
    sent.clear()
    scheduler.process_queue()
    ok(not sent, "no upload while the file is still on the PC")
    seti(ifirst.id, lease_at=None)
    r = owner.post(f"/api/worker/pc-file/{ifirst.id}", headers=HO, content=data)
    first = clip(first.id)
    ok(r.status_code == 200 and pcfolder.on_server(first) and Path(first.output_path).read_bytes() == data and items()[ifirst.id].wanted_at is None, f"the right file is saved for its upload ({r.status_code})")
    ok(owner.post(f"/api/worker/pc-file/{ifirst.id}", headers=HO, content=data).status_code == 409, "a second copy is refused")
    others = [c for c in pc_ids[1:]]
    scheduler.process_queue()
    first = clip(first.id)
    ok(len(sent) == 1 and sent[0]["path"] == first.output_path and sent[0]["publish_at"] == first.publish_at and first.status == "uploaded",
       f"it is uploaded with its slot as publishAt ({[(x['title'], x['publish_at']) for x in sent]})")
    ok(not Path(first.output_path).exists() and items()[ifirst.id].state == "done", "…and the server's copy is deleted at once; the item is done")
    ok(all(clip(c).status == "queued" for c in others), "the other PC clips (files still on the PC) wait")
    jobs_ = owner.get("/api/worker/jobs", headers={**HO, "x-worker-archive": "C%3A%5Carch"}).json()["jobs"]
    ok(not [x for x in jobs_ if x["type"] == "archive" and x["clip_id"] == first.id], "no archive job for it (it is on the PC already)")
    with SessionLocal() as s:
        archive.inventory(s, wid, []); s.commit()
    ok(clip(first.id).archive_missing is None, "the archive folder list never marks a PC-folder video missing")
    r = owner.post(f"/api/clips/{first.id}/get-back")
    jr = [x for x in owner.get("/api/worker/jobs", headers={**HO, "x-worker-archive": "C%3A%5Carch"}).json()["jobs"] if x["type"] == "restore"]
    ok(r.status_code == 200 and jr and jr[0]["files"][0]["sha"] == ifirst.sha, "Get the video back asks the PC for it by the same sha256")
    pcfolder.MAX_FILES = mf

    # ---------- the PC is off when the file is needed: never late without a time; moved to the next free day
    late = pc_ids[1]
    il = clip(late).pc_item_id
    setc(late, publish_at=utcnow() + timedelta(minutes=20))
    seti(il, wanted_at=utcnow() - timedelta(hours=3))
    with SessionLocal() as s:
        s.get(Worker, wid).last_seen = utcnow() - timedelta(hours=5); s.commit()
    sent.clear()
    scheduler.process_queue()
    ok(not sent, "nothing goes up late")
    out = pcfolder.due()
    cl = clip(late)
    ok(out["moved"] >= 1 and cl.publish_at > utcnow() + timedelta(hours=1) and (cl.publish_at + IST).hour == 18 and cl.status == "queued", f"its publish time passed: moved to the next free day, 18:00 IST ({cl.publish_at})")
    ok("Moved to" in (cl.error or "") and "PC was off" in cl.error and items()[il].moved_at is not None, f"…said on the clip ({cl.error})")
    hk = {x["key"] for x in owner.get("/api/health").json()["items"]}
    ok({"pc_moved", "pc_waiting"} <= hk, f"…and in the Problems list ({sorted(k for k in hk if k.startswith('pc'))})")
    ok(pcfolder.day_free(SessionLocal(), dst, cl.publish_at, skip_id=late), "the new day was free")
    with SessionLocal() as s:
        s.get(Worker, wid).last_seen = utcnow(); s.commit()

    # ---------- deleted on the PC: missing; found again
    keep = [{"sha": r_.sha, "rel": r_.rel, "name": r_.name, "folder": r_.folder} for r_ in items().values() if r_.sha != sha(T3)]
    rep({"folder": r"D:\youtube", "exists": True, "items": [], "present": keep})
    it3 = item_by("third one.mp4")
    ok(it3.missing_at is not None and "Missing" in it3.note, "a file no longer in the folder is marked missing on the PC")
    ok(all(r_.missing_at is None for r_ in items().values() if r_.sha != sha(T3)), "…only that one")
    ok("pc_missing" in {x["key"] for x in owner.get("/api/health").json()["items"]}, "an approved video missing on the PC is in the Problems list")
    rep({"folder": r"D:\youtube", "exists": False, "items": [], "present": []})
    ok(all(r_.missing_at is None for r_ in items().values() if r_.sha != sha(T3)), "a folder that is not there (drive unplugged) marks nothing missing")
    rep({"folder": r"D:\youtube", "exists": True, "items": [], "present": keep + [{"sha": sha(T3), "rel": "back\\third one.mp4", "name": "third one.mp4", "folder": "back"}]})
    ok(item_by("third one.mp4").missing_at is None and item_by("third one.mp4").rel == "back\\third one.mp4", "put back (elsewhere in the folder): found again")

    # ---------- a PC video is never made again on the server
    r = owner.post(f"/api/clips/{pc_ids[2]}/render")
    ok(r.status_code == 409 and "PC" in r.json()["detail"], "Render is refused for a PC video")
    from app.render import run_render
    setc(pc_ids[2], status="draft")
    run_render(pc_ids[2])
    ok(clip(pc_ids[2]).status == "queued", "a render that starts anyway puts it straight back in the queue")
    r = owner.post("/api/fix/rerender-waiting", json={})
    ok(clip(pc_ids[2]).status == "queued", "Make all waiting clips again skips PC videos")

    # ---------- tenant isolation
    CV = b"asha own video " * 300
    owner.post(f"/api/workers/{cwid}/pc-folder", headers={}, json={"folder": r"D:\asha"})  # (the owner's copy has no worker with that id; that is fine)
    r = c1.post(f"/api/workers/{cwid}/pc-folder", json={"folder": r"D:\asha"})
    ok(r.status_code == 200 and owner.get("/api/worker/jobs", headers=HC).json()["pc_folder"] == r"D:\asha", "the customer chooses their helper's folder")
    r = rep({"folder": r"D:\asha", "exists": True, "items": [f("asha trip.mp4", CV)]}, HC)
    ok(r.status_code == 200, "the customer's helper reports its folder")
    ok(any(x.sha == sha(CV) for x in items(tid).values()) and not any(x.sha == sha(CV) for x in items(1).values()), "…into the customer's copy only")
    Lc = c1.get("/api/pc-items").json()
    ok(all(x["name"] != "goa beach trip.mp4" for x in Lc["waiting"] + Lc["recent"]) and any(x["name"] == "asha trip.mp4" for x in Lc["waiting"] + Lc["recent"]),
       "the customer's list shows only their own videos")
    ok(not any(x["name"] == "asha trip.mp4" for x in owner.get("/api/pc-items").json()["waiting"] + owner.get("/api/pc-items").json()["recent"]), "the owner's list never shows the customer's")
    ia_c = next(x for x in items(tid).values() if x.sha == sha(CV))
    ok(c1.get(f"/api/pc-items/{ia_c.id}/thumb").status_code in (200, 404) and owner.post(f"/api/worker/pc-thumb/{ia_c.id}?kind=frame", headers=HC, content=JPG).status_code == 200,
       "the customer's helper sends its own picture")
    # the central helper: another account's PC items never travel through it
    OTOK3 = "tok-" + "k" * 40
    with SessionLocal() as s:
        s.add(Worker(name="central", token_hash=W.token_hash(OTOK3), approved=True, last_seen=utcnow(), can="all")); s.commit()
        kwid = s.query(Worker).filter(Worker.name == "central").first().id
    HK = {"x-worker-token": OTOK3, "x-worker-can": "all", "x-worker-pc": "1"}
    owner.get("/api/worker/jobs", headers=HK)
    owner.post(f"/api/admin/workers/{kwid}/central", json={"on": True})
    with tenant.use(tid):
        with SessionLocal() as s:
            s.get(Worker, cwid).last_seen = utcnow() - timedelta(days=1)
            r_ = s.get(PcItem, ia_c.id)
            s.commit()
    c1.post(f"/api/pc-items/{ia_c.id}/approve", json={})
    with tenant.use(tid):
        with SessionLocal() as s:
            r_ = s.get(PcItem, ia_c.id); r_.wanted_at = utcnow(); s.commit()
            ccid = r_.clip_id
    jk = owner.get("/api/worker/jobs", headers=HK).json()["jobs"]
    ok(not [x for x in jk if x["type"] == "pc_send" and (x.get("ticket") or x["item_id"] == ia_c.id and x["sha"] == sha(CV))], "the central helper never gets another account's PC video")
    tk = W.make_ticket(tid, "source", ccid or 1, kwid)
    TH = {**HK, "x-job-ticket": tk}
    codes = [owner.post("/api/worker/pc-items", headers=TH, json={"items": []}).status_code,
             owner.post(f"/api/worker/pc-thumb/{ia_c.id}", headers=TH, content=JPG).status_code,
             owner.post(f"/api/worker/pc-file/{ia_c.id}", headers=TH, content=CV).status_code,
             owner.post(f"/api/worker/fail/{ia_c.id}", headers=TH, json={"kind": "pc_send", "error": "x"}).status_code]
    ok(all(c == 403 for c in codes), f"no job ticket opens the PC-folder addresses ({codes})")
    with tenant.use(tid):
        ok(not pcfolder.on_server(clip(ccid, tid)), "…nothing arrived in the customer's copy")

    # ---------- review round 1
    # [M2] the same file twice in ONE report: "manual" always wins, whatever the order
    for k, order in enumerate(([("001_Real", {"approve": "manual", "title": "Real"}), ("007_Real - Copy", None)],
                               [("007_Copy first", None), ("001_Real", {"approve": "manual", "title": "Real"})],
                               [("001_Auto", {"approve": "auto", "title": "A"}), ("002_Manual", {"approve": "manual", "title": "M"})])):
        X = f"same file twice {k} ".encode() * 300
        r = rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", X, folder=fo_, info=inf) for fo_, inf in order]})
        rx = next(r_ for r_ in items().values() if r_.sha == sha(X))
        ok(r.status_code == 200 and rx.state == "new" and rx.clip_id is None and rx.info and rx.info["manual"],
           f"[M2] same sha twice in one report ({[o[0] for o in order]}): it waits, manual wins ({rx.state})")
    # …and across reports: a copy without info.json (another place) never wipes the manual flag
    X = b"across reports " * 300
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", X, folder="010_Real", info={"approve": "manual", "title": "Real"})]})
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", X, folder="011_Real - Copy")]})
    rx = next(r_ for r_ in items().values() if r_.sha == sha(X))
    ok(rx.state == "new" and rx.info and rx.info["manual"], "[M2] a later report of a copy without info.json keeps the manual flag")
    # [S1] one bad item never stops the batch; dates that overflow are no date
    ok(pcfolder.parse_date("0001-01-01T00:00:00") is None and pcfolder.parse_date("9999-12-31T23:59:59-12:00") is None
       and pcfolder.parse_date("2027-02-30") is None and pcfolder.parse_date(12345678901234567890) is None, "[S1] parse_date: overflow / nonsense = no date, never an error")
    OKV = b"good one in a bad batch " * 300
    r = rep({"folder": r"D:\youtube", "exists": True, "items": [
        {"sha": "z" * 64, "rel": "x.mp4", "name": "x.mp4", "size": 1},
        {**f("video.mp4", b"bad size " * 10, folder="030_bad"), "size": -5},
        {**f("video.mp4", b"bad date " * 300, folder="031_baddate", info={"approve": "manual", "suggested_upload_utc": "0001-01-01T00:00:00Z"})},
        "not even a dict",
        f("good one in a bad batch.mp4", OKV)]})
    j_ = r.json() if r.status_code == 200 else {}
    ok(r.status_code == 200 and sha(OKV) in j_.get("ids", {}) and sha(b"bad date " * 300) in j_["ids"] and "z" * 64 in j_.get("errors", {})
       and sha(b"bad size " * 10) in j_["errors"], f"[S1] the server takes a batch item by item and names the bad ones ({r.status_code} {str(j_.get('errors'))[:200]})")
    # [S2] the real info.json fields (copied from D:\youtube\001_…)
    REAL = {"approve": "auto", "number": 1, "clip_id": 99999, "title": "Car Rants Got The Roblox Rizz | Best Moment",
            "tags": ["car on roblox", "reacts"], "source_link": "https://www.youtube.com/watch?v=TQEUHikKcqI&t=0s",
            "source_channel": "C Daily", "source_channel_link": "https://www.youtube.com/channel/UCmmlHsRZzocU9UrXM2mcsng",
            "licence": youtube.CC_LICENSE, "licence_line": "License: Creative Commons Attribution (CC BY). Changes made: clipped and edited.",
            "mode": "cc", "suggested_upload_ist": "2027-08-01T18:00:00+05:30", "suggested_upload_utc": "2027-08-01T12:30:00Z"}
    DESC = ("Car on Roblox — the best moment from this video by C Daily.\n\n#shorts\n\nOriginal video: Car Rants Got The Roblox Rizz\n"
            "Credit: C Daily - https://www.youtube.com/channel/UCmmlHsRZzocU9UrXM2mcsng\nSource: https://youtu.be/TQEUHikKcqI?t=0\n"
            "License: Creative Commons Attribution (CC BY). Changes made: clipped and edited.")
    ci = pcfolder.clean_info(REAL, DESC)
    ok(ci["channel"] == REAL["source_channel_link"] and ci["channel_name"] == "C Daily" and ci["licence_line"].startswith("License:")
       and ci["publish_at"] == "2027-08-01T12:30:00Z" and ci["auto"] and not ci["manual"], f"[S2] real fields: channel address vs name, licence_line, suggested_upload_utc/ist ({ci['channel']}, {ci['publish_at']})")
    ok(pcfolder.clean_info({**REAL, "suggested_upload_utc": None})["publish_at"] == "2027-08-01T12:30:00Z", "[S2] suggested_upload_ist alone works too")
    RV = b"real format video " * 300
    with SessionLocal() as s:
        s.add(Project(url="https://youtu.be/xx", video_id="TQEUHikKcqX", title="z", channel="z", mode="cc", license=youtube.CC_LICENSE)); s.commit()
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", RV, folder="001_Car-Rants", info=REAL, desc=DESC)]})
    rr = next(r_ for r_ in items().values() if r_.sha == sha(RV))
    cr = clip(rr.clip_id) if rr.clip_id else None
    with SessionLocal() as s:
        prr = s.get(Project, cr.project_id) if cr else None
    ok(cr is not None and prr.channel_url == REAL["source_channel_link"] and prr.channel == "C Daily" and prr.mode == "cc",
       f"[S2] the creator's ADDRESS is the project's channel_url (never-use list / strike hold match it) ({prr and prr.channel_url})")
    ok(cr.description.count("TQEUHikKcqI") == 1 and cr.description.count("License:") == 1 and "Licence:" not in cr.description,
       f"[S2] no second Source / Licence block when description.txt has one (video id compared, not the address)")
    ok(cr.publish_at == datetime(2027, 8, 1, 12, 30), f"[S2] the suggested day is used ({cr.publish_at})")
    strikes._block(strikes.creator_key("https://www.youtube.com/channel/UCbadXXXXXXXXXXXXXXXXXXX"), "B", "", 0)
    BB = b"blocked by real field " * 300
    rep({"folder": r"D:\youtube", "exists": True, "items": [f("video.mp4", BB, folder="099_b", info={**REAL, "source_channel_link": "https://www.youtube.com/channel/UCbadXXXXXXXXXXXXXXXXXXX", "clip_id": 99998})]})
    ok(next(r_ for r_ in items().values() if r_.sha == sha(BB)).state == "blocked", "[S2] the never-use list matches source_channel_link")
    # [S3] the reminder: 30 days before the last scheduled day while videos wait for Approve
    with SessionLocal() as s:
        last = s.query(func.max(Clip.publish_at)).filter(Clip.status.in_(["queued", "uploaded"]), Clip.publish_at > utcnow()).scalar()
    keys = {x["key"] for x in owner.get("/api/health").json()["items"]}
    ok("pc_approve_soon" not in keys, f"[S3] no reminder while the line is long (last day {last})")
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.publish_at > utcnow() + timedelta(days=25)).all():
            c.publish_at = utcnow() + timedelta(days=25)
        s.commit()
    it_ = [x for x in owner.get("/api/health").json()["items"] if x["key"] == "pc_approve_soon"]
    ok(len(it_) == 1 and "Approve them before" in it_[0]["text"], f"[S3] 30 days before the last scheduled day: 'approve them before …' ({it_[:1]})")
    # [CW1] a drive root is not a folder of videos
    for bad_ in ("D:\\", "D:", "/", "E:/"):
        ok(owner.post(f"/api/workers/{wid}/pc-folder", json={"folder": bad_}).status_code == 400, f"[CW1] folder {bad_!r} (a whole drive) is refused")
    # [CW3] stale partial files are cleaned; a failed PC upload's file counts toward the cap
    part = pcfolder.video_path(clip(pc_ids[3])).parent / "pc.dead.part"
    part.parent.mkdir(parents=True, exist_ok=True); part.write_bytes(b"x")
    os.utime(part, (time.time() - 3 * 3600, time.time() - 3 * 3600))
    fresh_part = part.with_name("pc.fresh.part"); fresh_part.write_bytes(b"x")
    scheduler.cleanup_disk()
    ok(not part.exists() and fresh_part.exists(), "[CW3] cleanup_disk removes a PC file that broke off over 2 h ago (not a fresh one)")
    fresh_part.unlink()
    with SessionLocal() as s:
        for r_ in s.query(PcItem).all():
            r_.wanted_at = None
        s.commit()
    fc = clip(pc_ids[3])
    pcfolder.video_path(fc).write_bytes(b"failed upload file")
    setc(fc.id, status="failed", output_path=str(pcfolder.video_path(fc)))
    pcfolder.MAX_FILES, mf2 = 1, pcfolder.MAX_FILES
    set_setting("upload_lead_days", "365"); quota.capacity, qc2 = (lambda: 50), quota.capacity
    out = pcfolder.due()
    ok(out["asked"] == 0, f"[CW3] a failed PC upload whose file is still on the server counts toward the limit ({out})")
    pcfolder.MAX_FILES, quota.capacity = mf2, qc2
    setc(fc.id, status="queued")
    # [CW4] "uploaded" is saved before the server copy is deleted
    with SessionLocal() as s:
        for r_ in s.query(PcItem).all():
            r_.wanted_at = None
        for c in s.query(Clip).filter(Clip.status == "queued").all():
            c.publish_at = max(c.publish_at or utcnow(), utcnow() + timedelta(days=2))
        s.commit()
    order_ = []
    real_after = pcfolder.after_upload

    def spy(s_, c_):
        with SessionLocal() as s2:
            order_.append(s2.get(Clip, c_.id).status)
        raise OSError("disk hiccup")
    pcfolder.after_upload = spy
    sent.clear()
    scheduler.process_queue()
    pcfolder.after_upload = real_after
    ok(order_ and order_[0] == "uploaded" and clip(fc.id).status == "uploaded" and clip(fc.id).youtube_id, f"[CW4] the clip is saved as uploaded before its file is deleted; a failing delete changes nothing ({order_})")
    # [CW8] the index on existing databases
    from sqlalchemy import inspect as _insp, text as _text
    with db_engine().begin() as conn:
        conn.execute(_text("DROP INDEX IF EXISTS ix_clips_pc_item_id"))
    db_migrate()
    ok("ix_clips_pc_item_id" in {i["name"] for i in _insp(db_engine()).get_indexes("clips")}, "[CW8] migrate creates the pc_item_id index on an existing database")

    # ---------- routes
    for m_, p_, rule in [("GET", "/api/pc-items", "page.clips"), ("GET", "/api/pc-items/3/thumb", "page.clips"), ("POST", "/api/pc-items/3/approve", "clips.approve"),
                         ("POST", "/api/pc-items/approve-all", "clips.approve"), ("POST", "/api/pc-items/3/skip", "clips.discard"), ("POST", "/api/pc-items/3/unskip", "clips.discard"),
                         ("POST", "/api/pc-items/auto", "uploads.auto"), ("POST", "/api/workers/3/pc-folder", "helper.manage"),
                         ("POST", "/api/worker/pc-items", "helper"), ("POST", "/api/worker/pc-thumb/3", "helper"), ("POST", "/api/worker/pc-file/3", "helper")]:
        got = acl.rule_for(m_, p_)
        ok(got is not None and got[0] == rule, f"ROUTES: {m_} {p_} -> {rule} ({got and got[0]})")
    from fastapi.routing import APIRoute
    real = {(m_, r_.path) for r_ in main.app.routes if isinstance(r_, APIRoute) for m_ in r_.methods if m_ not in ("HEAD", "OPTIONS")}
    ok(all(acl.rule_for(*x) is not None for x in real), "every route still has a decision")
    ok(anon.get("/api/pc-items").status_code == 401 and anon.post("/api/pc-items/1/approve", json={}).status_code == 401, "not signed in: refused")
    ok(owner.post("/api/worker/pc-items", json={"items": []}).status_code == 401, "the helper addresses need a helper token")
    r = owner.post(f"/api/admin/users/{aid}/access", json={"perms": {"clips.approve": False, "uploads.auto": False}})
    ok(r.status_code == 200 and c1.post(f"/api/pc-items/{ia_c.id}/approve", json={}).status_code == 403
       and c1.post("/api/pc-items/approve-all").status_code == 403, "a customer with Approve switched off cannot approve")
    CV2 = b"asha second " * 300
    rep({"folder": "D:/asha", "exists": True, "items": [f("asha second.mp4", CV2)]}, HC)
    ok(next(x for x in items(tid).values() if x.sha == sha(CV2)).state == "new", "…and with \"Automatic upload\" switched off by the Super Admin nothing goes by itself")

# ---------- the helper's side
sys.argv = ["x", "http://localhost:9"]
spec = importlib.util.spec_from_file_location("cw", WT + "/worker/clipstudio_worker.py"); cw = importlib.util.module_from_spec(spec); spec.loader.exec_module(cw)
cw.log = lambda m: None
pf = sys.modules["pc_folder"]
ok(cw.ZIP_ALLOWED.fullmatch("worker/pc_folder.py") is not None and "pc_folder.py" in main.HELPER_WORKER_FILES, "the folder code is in the helper ZIP and its self-update")
src = open(WT + "/worker/clipstudio_worker.py", encoding="utf-8").read()
ok('"x-worker-pc": "1"' in src and "pc_tick(data[\"pc_folder\"]" in src, "the helper says it knows the folder and looks at it after each poll")
root = Path(tempfile.mkdtemp()) / "youtube"
(root / "001_street food").mkdir(parents=True)
(root / "tool" / "bin").mkdir(parents=True)
(root / "tool" / "make_local.py").write_text("print(1)")
(root / "tool" / "bin" / "x.mp4").write_bytes(b"not at the top of tool")
(root / "tool" / "test render.mp4").write_bytes(b"a program folder test file")
L1 = b"loose video bytes " * 1000
(root / "my trip.mp4").write_bytes(L1)
(root / "half.mp4.part").write_bytes(b"partial")
SV = b"folder video bytes " * 900
(root / "001_street food" / "video.mp4").write_bytes(SV)
(root / "001_street food" / "info.json").write_text(json.dumps({"title": "Street food", "approve": "manual", "license": "CC"}), encoding="utf-8")
(root / "001_street food" / "description.txt").write_text("Desc हिंदी", encoding="utf-8")
(root / "001_street food" / "thumbnail.jpg").write_bytes(JPG)
clock = {"t": 1000.0}
hashed = []
real_sha = pf.sha256
pf.sha256 = lambda p: (hashed.append(str(p)), real_sha(p))[1]
posts = []


class Resp:
    def __init__(self, code=200, js=None):
        self.status_code, self._js = code, js or {}

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    answer = None

    def post(self, url, headers=None, json=None, content=None, timeout=None):
        body = content.read() if hasattr(content, "read") else content
        posts.append((url, json, body))
        if url.endswith("/api/worker/pc-items"):
            ids = {it["sha"]: i + 1 for i, it in enumerate(json.get("items") or [])}
            return Resp(200, self.answer if self.answer is not None else {"ids": ids, "thumbs": list(ids)})
        return Resp(200)


http = FakeHttp()
fo = pf.Folder(Path(tempfile.mkdtemp()) / "pc_folder.json", log=lambda m: None, now=lambda: clock["t"],
               prober=lambda p: {"duration": 42.5, "width": 1920, "height": 1080}, framer=lambda p, at, w: JPG)
fo.set_root(str(root))
fo.tick("S", {"x-worker-token": "t"}, http)
ok(not [p for p in posts if p[1] and p[1].get("items")], "a new file is not reported before it has been unchanged for 60 s")
clock["t"] += 30
(root / "my trip.mp4").write_bytes(L1 + b"more")  # still being copied
L1 += b"more"
fo.tick("S", {"x-worker-token": "t"}, http)
clock["t"] += 45
fo.tick("S", {"x-worker-token": "t"}, http)
rep_ = [p for p in posts if p[1] and p[1].get("items")]
ok(len(rep_) == 1 and [it["name"] for it in rep_[0][1]["items"]] == ["video.mp4"], f"only the stable file is reported; a file that grew waits 60 s again ({[it['name'] for p in rep_ for it in p[1]['items']]})")
clock["t"] += 61
fo.tick("S", {"x-worker-token": "t"}, http)
rep_ = [p for p in posts if p[1] and p[1].get("items")]
items_sent = [it for p in rep_ for it in p[1]["items"]]
ok(sorted(it["name"] for it in items_sent) == ["my trip.mp4", "video.mp4"], f"then it is reported too; a program folder (tool, even with a video at its top) and .part files are not ({[it['rel'] for it in items_sent]})")
iv_ = next(it for it in items_sent if it["name"] == "video.mp4")
il_ = next(it for it in items_sent if it["name"] == "my trip.mp4")
ok(iv_["sha"] == sha(SV) and il_["sha"] == sha(L1) and iv_["size"] == len(SV) and iv_["duration"] == 42.5 and iv_["folder"] == "001_street food", "name, size, sha256, duration, sub-folder")
ok(iv_["info"]["approve"] == "manual" and iv_["description_txt"] == "Desc हिंदी" and il_["info"] is None, "the sub-folder's info.json and description.txt go with it; a loose file has none")
ok(all(len(json.dumps(p[1])) < 20000 for p in rep_) and not any(b"loose video bytes" in json.dumps(p[1]).encode() for p in rep_), "the report holds details only, never the video")
thumbs_ = [p for p in posts if "/pc-thumb/" in p[0]]
ok(any("kind=file" in p[0] and p[2] == JPG for p in thumbs_) and any("kind=frame" in p[0] for p in thumbs_), "the folder's thumbnail is sent as is; a loose file gets a small frame")
n = len(posts); hashed.clear()
clock["t"] += 31
fo.tick("S", {"x-worker-token": "t"}, http, force=True)
ok(not [p for p in posts[n:] if p[1] and p[1].get("items")] and not hashed, "each file is reported once (and not hashed again)")
os.replace(root / "my trip.mp4", root / "renamed trip.mp4")
clock["t"] += 31
fo.tick("S", {"x-worker-token": "t"}, http)
last = [p for p in posts if p[0].endswith("/pc-items")][-1][1]
ok(any(x["rel"] == "renamed trip.mp4" and x["sha"] == sha(L1) for x in last["present"]) and not any(x["rel"] == "my trip.mp4" for x in last["present"]) and not hashed,
   "a renamed file is the same sha256 at once (no 60 s wait, no new hash, never 'missing' in between)")
(root / "renamed trip.mp4").unlink()
clock["t"] += 31
fo.tick("S", {"x-worker-token": "t"}, http)
last = [p for p in posts if p[0].endswith("/pc-items")][-1][1]
ok(not any(x["sha"] == sha(L1) for x in last["present"]) and last["exists"], "a deleted file is no longer in the list (the server marks it missing)")
fo2 = pf.Folder(fo.state_file, log=lambda m: None, now=lambda: clock["t"], prober=lambda p: {}, framer=lambda p, at, w: b"")
fo2.set_root(str(root))
n = len(posts)
clock["t"] += 31
fo2.tick("S", {"x-worker-token": "t"}, http, force=True)
ok(not [p for p in posts[n:] if p[1] and p[1].get("items")], "after a restart of the helper nothing is reported twice (kept in a small file)")
(root / "001_street food" / "info.json").write_text(json.dumps({"title": "Street food", "approve": "auto"}), encoding="utf-8")
clock["t"] += 31
fo2.tick("S", {"x-worker-token": "t"}, http)
ok([it["info"]["approve"] for p in posts[n:] if p[1] for it in p[1].get("items") or []] == ["auto"], "a changed info.json is sent again (same sha256, new details)")
(root / "002_bad").mkdir()
(root / "002_bad" / "video.mkv").write_bytes(b"bad info " * 100)
(root / "002_bad" / "info.json").write_text("{not json", encoding="utf-8")
clock["t"] += 61
fo2.tick("S", {"x-worker-token": "t"}, http); clock["t"] += 61; fo2.tick("S", {"x-worker-token": "t"}, http)
bad = [it for p in posts if p[1] for it in p[1].get("items") or [] if it["name"] == "video.mkv"]
ok(bad and bad[-1]["info"]["approve"] == "manual", "a broken info.json makes the video wait for a person")
# the file, when the server asks
n = len(posts)
fo2.send_file({"item_id": 7, "sha": sha(SV), "rel": "001_street food\\video.mp4"}, "S", {"x-worker-token": "t"}, http)
ok(posts[-1][0] == "S/api/worker/pc-file/7" and posts[-1][2] == SV, "pc_send: the file is sent as the raw body")
os.replace(root / "001_street food", root / "001_moved")
fo2.send_file({"item_id": 7, "sha": sha(SV), "rel": "001_street food\\video.mp4"}, "S", {"x-worker-token": "t"}, http)
ok(posts[-1][0] == "S/api/worker/pc-file/7" and posts[-1][2] == SV and len(posts) == n + 2, "…found by its sha256 after its folder was moved too")
try:
    fo2.send_file({"item_id": 8, "sha": "1" * 64, "rel": "..\\..\\secret.mp4"}, "S", {}, http)
    ok(False, "an unknown file is refused")
except RuntimeError as e:
    ok("not in the folder" in str(e), "an unknown sha256 (or a path out of the folder) is never sent")
cw.PC = fo2
cw.httpx.post = lambda url, **kw: (posts.append((url, kw.get("json"), None)), Resp(200))[1]
cw.handle({"type": "pc_send", "item_id": 9, "sha": "2" * 64, "rel": "x.mp4"}, {"x-worker-token": "t"})
ok(posts[-1][0].endswith("/api/worker/fail/9") and posts[-1][1]["kind"] == "pc_send", "a file that is gone is reported (kind pc_send)")
ok(cw._find_saved("t1-c5", "video", sha(SV)) is not None, "Get the video back finds a PC-folder video in its folder")
fo3 = pf.Folder(Path(tempfile.mkdtemp()) / "s.json", log=lambda m: None, now=lambda: clock["t"])
fo3.set_root(str(root.parent / "nothing here"))
n = len(posts)
fo3.tick("S", {}, http)
ok(posts[-1][1] == {"folder": str(root.parent / "nothing here"), "exists": False, "items": []}, "a folder that is not on the PC is said once (nothing marked missing)")
# ---------- review round 1: the helper
# [M2] the same file twice: one entry, the copy with info.json; no re-post every 30 s
root2 = Path(tempfile.mkdtemp()) / "yt2"
(root2 / "007_Real - Copy").mkdir(parents=True); (root2 / "001_Real").mkdir(parents=True)
DUP = b"the same video twice " * 500
(root2 / "001_Real" / "video.mp4").write_bytes(DUP)
(root2 / "001_Real" / "info.json").write_text('{"approve": "manual"}', encoding="utf-8")
shutil.copy2(root2 / "001_Real" / "video.mp4", root2 / "007_Real - Copy" / "video.mp4")
posts.clear()
fo4 = pf.Folder(Path(tempfile.mkdtemp()) / "s4.json", log=lambda m: None, now=lambda: clock["t"], prober=lambda p: {"duration": 5, "width": 10, "height": 10}, framer=lambda p, at, w: JPG)
fo4.set_root(str(root2))
fo4.tick("S", {}, http); clock["t"] += 61; fo4.tick("S", {}, http)
its = [it for p in posts if p[1] for it in p[1].get("items") or []]
ok(len(its) == 1 and its[0]["folder"] == "001_Real" and its[0]["info"] == {"approve": "manual"}, f"[M2] helper: one entry per sha, the copy with info.json ({[i['folder'] for i in its]})")
ok(len([p for p in posts if p[1] and "present" in p[1]][-1][1]["present"]) == 1, "[M2] …and one entry in the list")
n = len(posts)
for _ in range(4):
    clock["t"] += 31; fo4.tick("S", {}, http)
ok(not [p for p in posts[n:] if p[1] and p[1].get("items")], "[M2] the same items are not posted again every 30 s")
# [S1] the helper moves past items the server refuses, and backs off on a lasting error
root3 = Path(tempfile.mkdtemp()) / "yt3"
root3.mkdir()
for k in range(3):
    (root3 / f"clip {k} of three.mp4").write_bytes(f"three {k} ".encode() * 300)


class Http2:
    mode = "400-one"
    calls = []

    def post(self, url, headers=None, json=None, content=None, timeout=None):
        self.calls.append((url, json))
        if url.endswith("/pc-items"):
            names = [i["name"] for i in (json or {}).get("items") or []]
            if self.mode == "500":
                return Resp(500)
            if self.mode == "400-one" and "clip 1 of three.mp4" in names:
                return Resp(400)
            if self.mode == "errors":
                return Resp(200, {"ids": {}, "thumbs": [], "errors": {i["sha"]: "no" for i in json["items"]}})
            return Resp(200, {"ids": {}, "thumbs": []})
        return Resp(200)


h2 = Http2()
fo5 = pf.Folder(Path(tempfile.mkdtemp()) / "s5.json", log=lambda m: None, now=lambda: clock["t"], prober=lambda p: {"duration": 5, "width": 10, "height": 10}, framer=lambda p, at, w: JPG)
fo5.set_root(str(root3))
fo5.tick("S", {}, h2); clock["t"] += 61
out5 = fo5.tick("S", {}, h2)
ok(out5["sent"] == 2 and out5["refused"] == 1 and len(fo5.state["sent"]) == 3, f"[S1] a batch with one refused item: the others are taken, the bad one is set aside ({out5})")
n = len(h2.calls); clock["t"] += 31; fo5.tick("S", {}, h2)
ok(not [c for c in h2.calls[n:] if c[1] and c[1].get("items")], "[S1] the refused item is not sent again (until its details change)")
(root3 / "clip 3 of three.mp4").write_bytes(b"a fourth " * 300)
h2.mode = "500"
clock["t"] += 61; fo5.tick("S", {}, h2); clock["t"] += 61
o1 = fo5.tick("S", {}, h2)
d1 = fo5.fail_delay
n = len(h2.calls); clock["t"] += 31
fo5.tick("S", {}, h2)
ok(d1 >= 60 and len(h2.calls) == n, f"[S1] a server error: the helper backs off instead of re-sending every 30 s ({d1} s)")
clock["t"] += d1 + 1; fo5.tick("S", {}, h2)
ok(fo5.fail_delay >= 2 * d1, f"[S1] …longer each time ({fo5.fail_delay} s, at most {pf.BACKOFF_MAX})")
h2.mode = "ok"; clock["t"] += fo5.fail_delay + 1
o2 = fo5.tick("S", {}, h2)
ok(o2["sent"] == 1 and fo5.fail_delay == 0, f"[S1] back to normal once the server takes it ({o2})")
h2.mode = "errors"; (root3 / "clip 4.mp4").write_bytes(b"a fifth " * 300); clock["t"] += 61; fo5.tick("S", {}, h2); clock["t"] += 61
o3 = fo5.tick("S", {}, h2)
n = len(h2.calls); clock["t"] += 31; fo5.tick("S", {}, h2)
ok(o3["refused"] == 1 and not [c for c in h2.calls[n:] if c[1] and c[1].get("items")], f"[S1] an item named in the server's errors is not re-sent every 30 s ({o3})")
# [M1] the helper's side of an empty / broken info.json
d_ = Path(tempfile.mkdtemp())
for txt, want in (("", {}), ("null", {"approve": "manual"}), ('{"appr', {"approve": "manual"})):
    (d_ / "info.json").write_text(txt, encoding="utf-8")
    got = pf.read_info(d_)[0]
    ok(got is not None and pcfolder.clean_info(got)["manual"], f"[M1] info.json {txt!r}: sent as info, which waits for a person ({got})")
# [CW5] a missing pc_folder.py never stops the helper
saved_mod = sys.modules.pop("pc_folder")
sys.modules["pc_folder"] = None  # import fails
spec2 = importlib.util.spec_from_file_location("cw2", WT + "/worker/clipstudio_worker.py"); cw2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(cw2)
sys.modules["pc_folder"] = saved_mod
cw2.log = lambda m: None
ok(cw2.PC is None and cw2.pc_tick("D:/x", {}) is None and cw2._find_saved("t1-c1", "video", "a" * 64) is None, "[CW5] without pc_folder.py the helper still loads and runs")
cw2.httpx.post = lambda url, **kw: (posts.append((url, kw.get("json"), None)), Resp(200))[1]
cw2.handle({"type": "pc_send", "item_id": 4, "sha": "a" * 64}, {})
ok(posts[-1][0].endswith("/api/worker/fail/4"), "[CW5] …and says so when it is asked for a file")
# [CW7] the folder is looked at in its own thread
slow = {"n": 0}


class SlowPC:
    def set_root(self, f):
        pass

    def tick(self, *a):
        slow["n"] += 1
        time.sleep(0.5)


cw.PC = SlowPC()
t0 = time.time()
cw.pc_tick("D:/x", {}); cw.pc_tick("D:/x", {})
ok(time.time() - t0 < 0.3, "[CW7] pc_tick returns at once (hashing / ffmpeg run in their own thread)")
cw._PC_THREAD[0].join(2)
ok(slow["n"] == 1, "[CW7] …and never two looks at the same time")
print("DONE", flush=True)

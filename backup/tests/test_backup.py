"""Backup & restore from the dashboard: download, daily backups (keep 7), check + preview, restore with safety backup,
tenant isolation, refusals (other account, bad / oversized / gzip bomb / garbage files, limits), restore flag, ACL, worker backup."""
import base64, gzip, hashlib, io, json, os, sys, tempfile, time
from datetime import datetime, timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "backup-test-pass-77"
os.environ["KEYWORDS_OFFLINE"] = "1"
import imageio_ffmpeg, shutil, atexit
os.environ["FFMPEG_BIN"] = imageio_ffmpeg.get_ffmpeg_exe()
atexit.register(lambda: shutil.rmtree(d, ignore_errors=True))
WT = r"D:\integ-final"
sys.path.insert(0, WT)
from fastapi.testclient import TestClient
from app import main, backup, tenant, auth, acl, jobs, telegram, scheduler, growth
from app.db import SessionLocal, Destination, Clip, Project, Channel, Setting, Worker, Alert, utcnow, set_setting, get_setting
from app.config import DATA_DIR
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
main.executor.submit = lambda *a, **k: None
jobs.executor.submit = lambda *a, **k: None
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)


def gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj).encode())


def upload(client, blob, name="b.json.gz"):
    return client.post("/api/backup/check", files={"file": (name, blob, "application/gzip")})


def snap(tid):
    """Everything that a restore must bring back exactly (settings: the ones taken from the file)."""
    with tenant.use(tid):
        t = backup.make()["tables"]
    t["settings"] = sorted((r for r in t["settings"] if not backup._protected(r["key"]) and r["key"] != "guide_errors"), key=lambda r: r["key"])
    return t


def diff(a, b):
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            ra, rb = a.get(k) or [], b.get(k) or []
            for x, y in zip(ra, rb):
                if x != y:
                    print("  DIFF", k, {c: (x.get(c), y.get(c)) for c in set(x) | set(y) if x.get(c) != y.get(c)})
                    break
            if len(ra) != len(rb):
                print("  DIFF", k, len(ra), len(rb))
    return a == b


def faces(tid):
    with tenant.use(tid):
        f = DATA_DIR / "faces"
        return sorted(x.name for x in f.glob("*.png")) if f.exists() else []


with mk() as owner, mk() as c1, mk() as c2, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "backup-test-pass-77"})

    def customer(client, email, name):
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email, "password": PW, "accept_terms": True})
        aid = next(u["id"] for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email)
        tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        client.post("/api/login", json={"username": email, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
        return aid, tid

    A1, T1 = customer(c1, "one@example.com", "One")
    A2, T2 = customer(c2, "two@example.com", "Two")

    def fill(tid, tag):
        with tenant.use(tid):
            with SessionLocal() as s:
                dst = Destination(channel_id="UC-" + tag, name=tag + " channel", token="enc:v1:secret-" + tag); s.add(dst); s.commit()
                pr = Project(url="https://youtu.be/abcdefghijk", video_id="abcdefghijk", title=tag + " video", mode="cc", duration=600,
                             tags=["food", tag], heatmap=[{"t": 1, "v": 0.5}]); s.add(pr); s.commit()
                for i in range(3):
                    s.add(Clip(project_id=pr.id, start=10.5 * i, end=10.5 * i + 30, aspect="9:16", title=f"{tag} clip {i}", tags=["a", "b"],
                               status="queued", publish_at=utcnow() + timedelta(days=i + 1), destination_id=dst.id, captions=[{"s": 0, "e": 1, "t": "hi"}]))
                s.add(Channel(kind="channel", query="@" + tag, name=tag + " source", destination_id=dst.id, seen=["x1", "x2"]))
                s.add(Alert(clip_id=1, kind="removed", message="gone", details={"title": "t"}))
                s.add(Worker(name=tag + " PC", token_hash=hashlib.sha256(("tok-" + tag).encode()).hexdigest(), approved=True))
                s.commit()
            set_setting("reminder", json.dumps({"on": True, "text": tag}))
            set_setting("admin_emails", tag + "@example.com")
            (DATA_DIR / "faces").mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "faces" / "0.png").write_bytes(PNG + tag.encode())

    fill(1, "owner"); fill(T1, "one"); fill(T2, "two")

    # ---- download
    r = owner.get("/api/backup/download")
    ok(r.status_code == 200 and r.headers["content-type"].startswith("application/gzip") and ".json.gz" in r.headers.get("content-disposition", ""),
       "Download backup now gives a .json.gz file")
    OWNER_FILE = r.content
    data = json.loads(gzip.decompress(OWNER_FILE))
    ok(data["format"] == "raji-studio-backup" and data["version"] == 1 and data["tenant"] == 1 and data["made_at"].endswith("Z")
       and data["brand"] == "Raji Studio" and data["counts"]["clips"] == 3 and "app_commit" in data, "the file says format, version, made_at, account, brand, commit, row counts")
    ok("video_stats" not in data["tables"] and "accounts" not in data["tables"] and "activity" not in data["tables"],
       "the shared accounts / activity tables and YouTube numbers are never in a backup")
    ok(data["tables"]["destinations"][0]["token"].startswith("enc:v1:") and data["faces"]["0.png"], "channel logins stay encrypted; faces included")
    ok(all(c["title"].startswith("owner") for c in data["tables"]["clips"]), "the owner's file has only the owner's rows")
    r = c1.get("/api/backup/download"); C1_FILE = r.content; c1data = json.loads(gzip.decompress(C1_FILE))
    ok(r.status_code == 200 and c1data["tenant"] == T1 and c1data["brand"] == "One Studio" and all(c["title"].startswith("one") for c in c1data["tables"]["clips"]),
       "a customer's download is their own copy only (tenant-aware), with their brand")
    ok("one-studio-backup-" in r.headers["content-disposition"], "file name carries the system name")

    # ---- worker backup still works (helper PC daily pull)
    with tenant.use(1):
        with SessionLocal() as s:
            s.add(Worker(name="owner helper", token_hash=hashlib.sha256(b"helper-token-1").hexdigest(), approved=True)); s.commit()
    r = anon.get("/api/worker/backup", headers={"x-worker-token": "helper-token-1"})
    wd = r.json() if r.status_code == 200 else {}
    ok(r.status_code == 200 and wd.get("tables", {}).get("clips") and wd.get("made_at") and "faces" in wd and wd.get("tenant") == 1,
       "/api/worker/backup still gives the helper the same data (tables, faces, made_at)")
    with tenant.use(T1):
        with SessionLocal() as s:
            s.add(Worker(name="c1 helper", token_hash=hashlib.sha256(f"t{T1}.helper".encode()).hexdigest(), approved=True)); s.commit()
    r = anon.get("/api/worker/backup", headers={"x-worker-token": f"t{T1}.helper"})
    ok(r.status_code == 200 and r.json()["tenant"] == T1 and all(c["title"].startswith("one") for c in r.json()["tables"]["clips"]),
       "a customer's helper gets only that customer's backup")
    ok(anon.get("/api/worker/backup", headers={"x-worker-token": "nope"}).status_code == 401, "unknown helper refused")
    with tenant.use(1):
        ok(set(growth.backup_data()) >= {"tables", "faces", "made_at"}, "growth.backup_data() kept (same shape)")

    with tenant.use(1):
        set_setting("razorpay", "old-keys"); set_setting("plan_prices", "old-prices")
    OWNER_FILE = owner.get("/api/backup/download").content; data = json.loads(gzip.decompress(OWNER_FILE))  # now with the helpers
    with tenant.use(1):
        set_setting("razorpay", "new-keys"); set_setting("plan_prices", "new-prices")
    C1_FILE = c1.get("/api/backup/download").content
    # ---- round trip in the owner's copy
    before = snap(1)
    with tenant.use(1):
        with SessionLocal() as s:
            s.delete(s.query(Clip).first()); s.query(Clip).first().title = "CHANGED"
            s.add(Channel(kind="topic", query="new topic", name="new source")); s.commit()
        set_setting("reminder", "changed"); set_setting("brand_new_key", "x"); set_setting("admin_emails", "now@example.com")
        (DATA_DIR / "faces" / "0.png").unlink(); (DATA_DIR / "faces" / "9.png").write_bytes(PNG)
    ok(snap(1) != before, "data changed after the backup")
    p = upload(owner, OWNER_FILE).json()
    ok(p.get("ref") == "upload" and p["counts"]["clips"] == 3 and p["current"]["clips"] == 2 and p["counts"]["channels"] == 1 and p["current"]["channels"] == 2
       and p["faces"] == 1 and p["same_account"] and p["made_at"] == data["made_at"] and p["brand"] == "Raji Studio", "preview: rows per table (backup vs now), made_at, brand")
    ok(any("Reconnect" in n for n in p["notes"]), "preview says channel logins only work on this server (Reconnect)")
    r = owner.post("/api/backup/restore", json={"ref": "upload", "sha": p["sha"], "confirm": "restore"})
    ok(r.status_code == 400 and "RESTORE" in r.json()["detail"] and snap(1) != before, "without typing RESTORE nothing happens")
    r = owner.post("/api/backup/restore", json={"ref": "upload", "sha": "0" * 64, "confirm": "RESTORE"})
    ok(r.status_code == 400 and snap(1) != before, "a different file than the one checked is refused")
    r = owner.post("/api/backup/restore", json={"ref": "upload", "sha": p["sha"], "confirm": "RESTORE"})
    out = r.json()
    ok(r.status_code == 200 and out["restored"]["clips"] == 3 and out["safety"].startswith("safety-"), "restore returns counts and the safety backup's name")
    ok(diff(snap(1), before), "round trip: every table is exactly as in the backup")
    with tenant.use(1):
        ok(get_setting("admin_emails") == "now@example.com" and get_setting("brand_new_key") is None and get_setting("reminder") == json.dumps({"on": True, "text": "owner"}),
           "settings from the file; password-reset list (admin_emails) keeps their current value")
        ok(get_setting("razorpay") == "new-keys" and get_setting("plan_prices") == "new-prices", "an owner restore keeps the current Razorpay keys and plan prices")
    ok(faces(1) == ["0.png"], "faces restored (and the newer one removed)")
    with tenant.use(1):
        with SessionLocal() as s:
            c = Clip(project_id=1, start=0, end=5, status="draft"); s.add(c); s.commit()
            ok(c.id > max(x["id"] for x in data["tables"]["clips"]), "new rows get new numbers after a restore")
            s.delete(c); s.commit()
    lst = owner.get("/api/backup").json()
    saf = [f for f in lst["files"] if f["kind"] == "safety"]
    ok(len(saf) == 1 and saf[0]["name"] == out["safety"], "the safety backup is listed")
    sd = json.loads(gzip.decompress(owner.get("/api/backup/files/" + saf[0]["name"]).content))
    ok(any(c["name"] == "new source" for c in sd["tables"]["channels"]) and any(c["title"] == "CHANGED" for c in sd["tables"]["clips"]),
       "the safety backup holds the state from just before the restore (download works)")
    ok(not os.path.exists(os.path.join(d, "backups", "upload-pending.json.gz")), "the uploaded file is removed after the restore")
    # restore the safety backup from the server list -> back to the changed state
    p2 = owner.post("/api/backup/files/" + saf[0]["name"] + "/check").json()
    ok(p2["ref"] == saf[0]["name"] and p2["counts"]["channels"] == 2, "a backup kept on the server can be checked")
    r = owner.post("/api/backup/restore", json={"ref": saf[0]["name"], "confirm": "RESTORE"})
    ok(r.status_code == 200 and snap(1) != before and faces(1) == ["9.png"], "…and restored (undo)")
    owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE"})  # pending file gone -> 400
    r = owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE"})
    ok(r.status_code == 400 and "Choose it again" in r.json()["detail"], "no file waiting -> plain message")
    upload(owner, OWNER_FILE); owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE"})
    ok(snap(1) == before, "restored again")
    with tenant.use(1):
        with SessionLocal() as s:
            ok(s.query(Project).filter(Project.quick.is_(None)).count() == 1 and s.query(Clip).filter(Clip.kw.is_(None)).count() == 3,
               "empty JSON columns come back as real NULL (not the JSON word null)")
            c = s.query(Clip).order_by(Clip.id).first(); CID = c.id; c.status, c.youtube_id = "uploaded", "YTAFTER0001"; s.commit()
    upload(owner, OWNER_FILE); r = owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE"})
    with tenant.use(1):
        with SessionLocal() as s:
            c = s.get(Clip, CID)
            ok(r.json().get("kept_uploaded") == 1 and c.status == "uploaded" and c.youtube_id == "YTAFTER0001",
               "a clip uploaded after the backup was made stays uploaded (never uploaded twice)")
            c.status, c.youtube_id = "queued", None; s.commit()
    ok(snap(1) == before, "back to the backup's state")

    # ---- tenant isolation
    o_before, t2_before = snap(1), snap(T2)
    own_files = sorted(os.listdir(os.path.join(d, "backups")))
    with tenant.use(5):
        from app.db import ensure_tenant
        ensure_tenant(5)
        res = backup.restore(json.loads(gzip.decompress(C1_FILE)), is_super=True)
        with SessionLocal() as s:
            ok(s.query(Clip).count() == 3 and all(c.title.startswith("one") for c in s.query(Clip)), "restore into copy #5 fills copy #5")
    ok(snap(1) == o_before and snap(T2) == t2_before and faces(1) == ["0.png"], "…and does not touch copy #1 or any other customer")
    ok(os.path.exists(os.path.join(d, "tenants", "5", "backups", res["safety"])) and sorted(os.listdir(os.path.join(d, "backups"))) == own_files,
       "copy #5's safety backup is in its own folder")

    # ---- customer
    backup._hits.clear()
    c1_before = snap(T1)
    with tenant.use(T1):
        with SessionLocal() as s:
            for c in s.query(Clip).all():
                s.delete(c)
            s.commit()
    r = upload(c1, OWNER_FILE)
    ok(r.status_code == 400 and r.json()["detail"] == backup.NOT_YOURS, "a customer cannot restore another account's backup (refused at the check)")
    legacy = json.loads(gzip.decompress(C1_FILE)); legacy.pop("tenant"); legacy.pop("format"); legacy.pop("version")
    r = upload(c1, gz(legacy))
    ok(r.status_code == 400 and r.json()["detail"] == backup.NOT_YOURS, "a file without an account number / signature (old helper file) is refused for customers")
    r = c1.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE"})
    ok(r.status_code == 400, "nothing waiting for the customer after a refused check")
    # forged: customer's own file with the owner's id in it is refused even at the restore step
    owner_named = json.loads(gzip.decompress(C1_FILE)); owner_named["tenant"] = 1
    with tenant.use(T1):
        try:
            backup.restore(owner_named, is_super=False); ok(False, "restore() refuses another account's file for customers")
        except backup.BackupError:
            ok(True, "restore() refuses another account's file for customers")
    # signature: the account number and every row are covered
    ok(c1data.get("signature", {}).get("mac") and backup.signed(c1data) and backup.signed(data), "every backup is signed by the server")
    for what, change in [("account number changed to this customer", lambda x: x.update(tenant=T1)),
                         ("one row changed", lambda x: x["tables"]["clips"][0].update(title="hacked")),
                         ("a channel login added", lambda x: x["tables"]["destinations"].append(dict(x["tables"]["destinations"][0], id=77, channel_id="UC-steal"))),
                         ("signature removed", lambda x: x.pop("signature"))]:
        src = json.loads(gzip.decompress(C1_FILE if what != "account number changed to this customer" else OWNER_FILE)); change(src)
        r = upload(c1, gz(src))
        ok(r.status_code == 400 and r.json()["detail"] == backup.NOT_YOURS, f"customer: refused when the file was edited ({what})")
        with tenant.use(T1):
            try:
                backup.restore(src, is_super=False); ok(False, f"restore() refuses too ({what})")
            except backup.BackupError as e:
                ok(str(e) == backup.NOT_YOURS, f"restore() refuses too ({what})")
    backup._hits.clear()
    T2_FILE = c2.get("/api/backup/download").content
    r = upload(c1, T2_FILE)
    ok(r.status_code == 400 and r.json()["detail"] == backup.NOT_YOURS, "customer: another customer's genuine (signed) backup refused")
    real_key = backup._sign_key
    backup._sign_key = lambda: b"another server's key"
    r = upload(c1, C1_FILE)
    backup._sign_key = real_key
    ok(r.status_code == 400 and r.json()["detail"] == backup.NOT_YOURS, "customer: a backup signed by another server is refused")
    backup._hits.clear()
    p = upload(c1, C1_FILE).json()
    ok(isinstance(p.get("faces_now"), int), "preview shows how many face photos there are now")
    ok(p["same_account"] and p["counts"]["clips"] == 3 and p["current"]["clips"] == 0, "customer: own backup is accepted, preview shows the counts")
    r = c1.post("/api/backup/restore", json={"ref": "upload", "sha": p["sha"], "confirm": "RESTORE"})
    ok(r.status_code == 200 and snap(T1) == c1_before and snap(1) == o_before and snap(T2) == t2_before, "customer restore brings their data back; owner and others untouched")
    acts = owner.get(f"/api/admin/activity?account_id={A1}").json()
    acts = acts["rows"]
    ok(any("Restored a backup" in (a.get("detail") or "") for a in acts), "the restore is in the activity log")
    # the helper's daily file (from /api/worker/backup) is signed too and its owner can restore it
    backup._hits.clear()
    hf = anon.get("/api/worker/backup", headers={"x-worker-token": f"t{T1}.helper"}).json()
    r = upload(c1, gz(hf))
    ok(r.status_code == 200 and r.json()["same_account"] and r.json()["signed"], "the helper PC's saved backup is signed and restorable by its owner")
    # forged settings cannot lift what the Super Admin decided (even the Super Admin's restore keeps them)
    forged = json.loads(gzip.decompress(C1_FILE))
    forged["tables"]["settings"] = [r for r in forged["tables"]["settings"] if r["key"] not in ("shared_google", "onboarding")] + \
        [{"key": "shared_google", "value": "1"}, {"key": "onboarding", "value": "{}"}]
    ok(upload(c1, gz(forged)).json()["detail"] == backup.NOT_YOURS, "customer: a file with changed settings is refused")
    owner.post(f"/api/admin/users/{A1}/view")
    upload(owner, gz(forged)); r = owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE", "other_account": True})
    owner.post("/api/admin/view/exit")
    with tenant.use(T1):
        ok(r.status_code == 200 and get_setting("shared_google") is None and get_setting("onboarding") == '{"finished": true}',
           "Super Admin decisions (shared Google project, onboarding) are never taken from a file")
    # helper PCs: a customer's restore keeps the current ones, even from a genuine (signed) file with an extra approved helper
    backup._hits.clear()
    wk = json.loads(gzip.decompress(C1_FILE))
    wk["tables"]["workers"].append({"id": 500, "name": "sneaky", "token_hash": hashlib.sha256(f"t{T1}.sneaky".encode()).hexdigest(), "approved": True})
    with tenant.use(T1):
        with SessionLocal() as s:
            before_w = sorted(w.token_hash for w in s.query(Worker))
        backup.restore(backup.sign(wk), is_super=False)
        with SessionLocal() as s:
            after_w = sorted(w.token_hash for w in s.query(Worker))
    ok(after_w == before_w and anon.get("/api/worker/jobs", headers={"x-worker-token": f"t{T1}.sneaky"}).status_code == 401,
       "customer restore keeps the current helper PCs; a helper in the file is not added")
    with tenant.use(T1):
        s0 = snap(T1)
    ok(s0 == c1_before, "…and the rest of the data is restored")
    # limits: a file with more channels than the plan allows
    two = json.loads(gzip.decompress(C1_FILE)); d2 = dict(two["tables"]["destinations"][0]); d2.update(id=99, channel_id="UC-extra"); two["tables"]["destinations"].append(d2)
    backup._hits.clear()
    r = upload(c1, gz(backup.sign(two)))
    ok(r.status_code == 400 and "plan allows 1" in r.json()["detail"], "a backup with more channels than the plan allows is refused")
    # Super Admin viewing a customer may put another account's backup there, with a warning and a tick
    owner.post(f"/api/admin/users/{A2}/view")
    p = upload(owner, C1_FILE).json()
    ok(not p["same_account"] and p["signed"] and any("WARNING" in w and "another account" in w for w in p["warnings"]), "Super Admin: another account's backup gives a clear warning")
    r = owner.post("/api/backup/restore", json={"ref": "upload", "sha": p["sha"], "confirm": "RESTORE"})
    ok(r.status_code == 400 and "Tick" in r.json()["detail"] and snap(T2) == t2_before, "…and needs the extra tick")
    r = owner.post("/api/backup/restore", json={"ref": "upload", "sha": p["sha"], "confirm": "RESTORE", "other_account": True})
    with tenant.use(T2):
        with SessionLocal() as s:
            ok(r.status_code == 200 and all(c.title.startswith("one") for c in s.query(Clip)), "…then it is restored into the copy being viewed")
    ok(snap(1) == o_before and snap(T1) == c1_before, "…without touching the owner or the account the file came from")
    owner.post("/api/admin/view/exit")

    # ---- bad files
    def refused(blob, words, m):
        r = upload(owner, blob)
        ok(r.status_code in (400, 413) and words.lower() in r.json()["detail"].lower(), m + f" ({r.json().get('detail', '')[:70]})")
    refused(b"", "empty", "empty file refused")
    refused(os.urandom(5000), "not a raji studio backup", "garbage refused")
    refused(b"\x1f\x8b" + os.urandom(3000), "damaged", "broken gzip refused")
    refused(gz({"hello": 1}), "not a raji studio backup", "JSON that is not a backup refused")
    refused(gz({"format": "other", "tables": {"clips": []}}), "another program", "another program's file refused")
    refused(gz({"format": backup.FORMAT, "version": 99, "tables": {"clips": []}}), "newer version", "a newer version's file refused")
    refused(gz({"format": backup.FORMAT, "version": 1, "tenant": 1, "tables": {"foo": []}}), "none of raji studio", "no known table refused")
    refused(gz({"format": backup.FORMAT, "version": 1, "tenant": 1, "tables": {"clips": "x"}}), "damaged", "a table that is not a list refused")
    bomb = io.BytesIO()
    with gzip.GzipFile(fileobj=bomb, mode="wb", compresslevel=9) as g:
        g.write(b"{" + b" " * (310 * 1024 * 1024))
    ok(len(bomb.getvalue()) < 2_000_000, f"(gzip bomb is {len(bomb.getvalue()) // 1024} KB packed)")
    refused(bomb.getvalue(), "300 MB", "gzip bomb refused (unpacked size capped)")
    r = owner.post("/api/backup/check", files={"file": ("big.json.gz", b"0" * (102 * 1024 * 1024), "application/gzip")})
    ok(r.status_code == 413 and "100 MB" in r.json()["detail"], "a file over 100 MB is refused before it is read")
    try:
        backup.unpack(b"0" * (backup.MAX_UPLOAD + 1)); ok(False, "unpack refuses > 100 MB")
    except backup.BackupError as e:
        ok("100 MB" in str(e), "unpack refuses > 100 MB")
    # a broken row: nothing is changed (one transaction)
    bad = json.loads(gzip.decompress(OWNER_FILE)); bad["tables"]["clips"][0]["start"] = "not a number"
    o_now = snap(1); upload(owner, gz(bad)); r = owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE"})
    ok(r.status_code == 400 and "Tick" in r.json()["detail"] and snap(1) == o_now, "Super Admin: an edited (unsigned) file needs the tick")
    p = upload(owner, gz(bad)).json()
    ok(not p["signed"] and any("not made by this server" in w for w in p["warnings"]), "…and the preview says it was not made by this server")
    r = owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE", "other_account": True})
    ok(r.status_code == 400 and "wrong value" in r.json()["detail"] and snap(1) == o_now, "a damaged row is refused and nothing changes")
    dup = json.loads(gzip.decompress(OWNER_FILE)); dup["tables"]["clips"].append(dict(dup["tables"]["clips"][0]))
    upload(owner, gz(dup)); r = owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE", "other_account": True})
    ok(r.status_code == 500 and snap(1) == o_now, "a database error in the middle rolls everything back")
    errs = json.loads(get_setting("guide_errors") or "[]")
    ok(any("Restore a backup" in e["where"] for e in errs), "…and goes to Alerts → System errors")
    # PostgreSQL-only failures are refused before anything is changed (plain 400, not 500)
    def refused_restore(obj, words, m):
        upload(owner, gz(obj)); r = owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE", "other_account": True})
        ok(r.status_code == 400 and all(w in r.json()["detail"] for w in words) and snap(1) == o_now and 1 not in jobs.RESTORING,
           m + f" ({r.json().get('detail', '')[:90]})")
    orphan = json.loads(gzip.decompress(OWNER_FILE)); orphan["tables"]["clips"][0]["project_id"] = 9999
    refused_restore(orphan, ["clips", "project_id", "9999", "projects"], "a clip whose project is not in the file is refused (foreign keys checked)")
    nul = json.loads(gzip.decompress(OWNER_FILE)); nul["tables"]["projects"][0]["title"] = "a\x00b"
    refused_restore(nul, ["projects", "NUL"], "a NUL character in a text is refused")
    nulj = json.loads(gzip.decompress(OWNER_FILE)); nulj["tables"]["projects"][0]["tags"] = ["ok", "b\x00"]
    refused_restore(nulj, ["projects", "NUL"], "a NUL character inside a JSON value is refused")
    big = json.loads(gzip.decompress(OWNER_FILE)); big["tables"]["projects"][0]["id"] = 2 ** 40
    refused_restore(big, ["projects", "too large"], "an integer outside the 32-bit range is refused")
    neg = json.loads(gzip.decompress(OWNER_FILE)); neg["tables"]["clips"][0]["id"] = -2 ** 31 - 1
    refused_restore(neg, ["clips", "too large"], "…also below the range")
    edge = json.loads(gzip.decompress(OWNER_FILE)); edge["tables"]["clips"][0]["id"] = 2 ** 31 - 1
    with tenant.use(1):
        ok(backup._rows(Clip.__table__, edge["tables"]["clips"])[0]["id"] == 2 ** 31 - 1, "…the largest 32-bit number itself is accepted")
    # a row held by another connection: the restore gives up with a plain message (flag cleared, safety backup kept)
    from sqlalchemy.exc import OperationalError
    class _Orig(Exception):
        pgcode = "55P03"
    real_replace = backup._replace
    backup._replace = lambda *a, **k: (_ for _ in ()).throw(OperationalError("UPDATE clips", {}, _Orig("canceling statement due to lock timeout")))
    with tenant.use(1):
        safeties = len([x for x in backup.listing() if x["name"].startswith("safety")])
    upload(owner, OWNER_FILE); r = owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE"})
    backup._replace = real_replace
    with tenant.use(1):
        after = backup.listing()
    ok(r.status_code == 400 and r.json()["detail"] == backup.BUSY and 1 not in jobs.RESTORING and snap(1) == o_now,
       "a lock timeout gives the plain 'busy, nothing was changed' message and clears the restore flag")
    ok(len([x for x in after if x["name"].startswith("safety")]) >= min(safeties + 1, backup.KEEP_SAFETY), "…the safety backup is kept")
    from app.db import IS_SQLITE, engine_for
    if not IS_SQLITE:  # the real thing: a row locked by another connection
        import threading
        from sqlalchemy import text as _text
        holder = engine_for(1).connect(); hold = holder.begin()
        holder.execute(_text("SELECT id FROM clips ORDER BY id LIMIT 1 FOR UPDATE"))
        out = {}
        def go():
            with tenant.use(1):
                try:
                    backup.restore(json.loads(gzip.decompress(OWNER_FILE)), True); out["r"] = "restored"
                except backup.BackupError as e:
                    out["r"] = str(e)
        t0 = time.time(); th = threading.Thread(target=go); th.start(); th.join(60)
        hold.rollback(); holder.close()
        ok(out.get("r") == backup.BUSY and time.time() - t0 < 40 and 1 not in jobs.RESTORING and snap(1) == o_now,
           f"PostgreSQL: a locked row makes the restore stop after the lock timeout ({out.get('r')}, {time.time() - t0:.0f}s)")
    # backups of the same data are identical (rows ordered by number), also after a row was changed and changed back
    with tenant.use(1):
        a1 = backup.make()["tables"]
        with SessionLocal() as s:
            c = s.query(Clip).order_by(Clip.id).first(); old = c.title; c.title = "x"; s.commit(); c.title = old; s.commit()
        a2 = backup.make()["tables"]
    ok(a1 == a2 and all([r["id"] for r in a1[k]] == sorted(r["id"] for r in a1[k]) for k in a1 if a1[k] and "id" in a1[k][0]),
       "two backups of the same data are identical, rows in number order")
    # unknown columns dropped, missing columns default
    odd = json.loads(gzip.decompress(OWNER_FILE))
    for c in odd["tables"]["clips"]:
        c["future_column"] = 1; c.pop("cta_on", None); c.pop("reframe", None)
    upload(owner, gz(odd)); r = owner.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE", "other_account": True})
    with tenant.use(1):
        with SessionLocal() as s:
            ok(r.status_code == 200 and all(c.reframe is True and c.cta_on is None for c in s.query(Clip)), "unknown columns dropped, missing columns get their default")

    # ---- customers: smaller caps, rate limits
    backup._hits.clear()
    big = gzip.compress(os.urandom(21 * 1024 * 1024), compresslevel=1)
    r = upload(c2, big)
    ok(r.status_code == 400 and "Super Admin" in r.json()["detail"] and "20 MB" in r.json()["detail"], "customer: a file over 20 MB is refused, plainly (the Super Admin can restore it)")
    r = upload(owner, big)
    ok(r.status_code == 400 and "not a raji studio backup" in r.json()["detail"].lower(), "…the Super Admin may send up to 100 MB")
    fat = io.BytesIO()
    with gzip.GzipFile(fileobj=fat, mode="wb", compresslevel=9) as g:
        g.write(b"{" + b" " * (60 * 1024 * 1024))
    backup._hits.clear()
    r = upload(c2, fat.getvalue())
    ok(r.status_code == 400 and "50 MB" in r.json()["detail"] and "Super Admin" in r.json()["detail"], "customer: a file that unpacks to more than 50 MB is refused")
    r = upload(owner, fat.getvalue())
    ok(r.status_code == 400 and "damaged" in r.json()["detail"], "…the Super Admin's limit is 300 MB")
    try:
        backup.unpack(big, is_super=False); ok(False, "unpack() caps customers")
    except backup.BackupError as e:
        ok("Super Admin" in str(e), "unpack() caps customers (a big own backup: the Super Admin restores it)")
    backup._hits.clear()
    codes = [upload(c2, b"garbage").status_code for _ in range(7)]
    ok(codes[:6] == [400] * 6 and codes[6] == 429, f"customer: at most 6 checks an hour, then a plain 429 ({codes})")
    ok("Too many" in upload(c2, b"x").json()["detail"] and upload(owner, b"x").status_code == 400, "…the Super Admin is not limited")
    ok(upload(c1, b"x").status_code == 400, "…and the limit is per account")
    codes = [c2.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE"}).status_code for _ in range(4)]
    ok(codes[:3] == [400] * 3 and codes[3] == 429, f"customer: at most 3 restores an hour ({codes})")
    backup._hits.clear()

    # ---- daily backups: keep 7, size cap, alert
    clock = [datetime(2026, 11, 1, 21, 30)]
    real_now = backup.utcnow
    backup.utcnow = lambda: clock[0]
    with tenant.use(T1):
        for i in range(9):
            clock[0] = datetime(2026, 11, 1, 21, 30) + timedelta(days=i)
            backup.daily()
        n1 = backup.daily()  # same day again -> skipped
        names = [f["name"] for f in backup.listing() if f["kind"] == "auto"]
    ok(len(names) == 7 and names[0].startswith("auto-20261109") and names[-1].startswith("auto-20261103") and n1 is None,
       "daily backups: one a day, the last 7 kept, oldest removed")
    ok(all(f["size"] < 200_000 for f in c1.get("/api/backup").json()["files"]), "backups are small (data only, no videos)")
    ok(not any(f["kind"] == "auto" for f in owner.get("/api/backup").json()["files"]), "the owner's list does not show the customer's backups")
    backup.MAX_AUTO, real_max = 100, backup.MAX_AUTO
    clock[0] = datetime(2026, 11, 20, 21, 30)
    with tenant.use(T1):
        res = backup.daily()
        errs = json.loads(get_setting("guide_errors") or "[]")
        n_auto = len([f for f in backup.listing() if f["kind"] == "auto"])
    backup.MAX_AUTO = real_max
    ok(res is None and n_auto == 7 and any("Daily backup" in e["where"] and "50 MB" in e["text"] for e in errs), "a backup over 50 MB is not kept and an alert says so")
    backup.MAX_TOTAL, real_tot = 1, backup.MAX_TOTAL
    clock[0] = datetime(2026, 11, 21, 21, 30)
    with tenant.use(T1):
        backup.daily(); n_all = len(backup.listing())
    backup.MAX_TOTAL = real_tot
    ok(n_all == 1, "total size cap: oldest backups go, the newest always stays")
    backup.utcnow = real_now
    ok(c1.get("/api/backup/files/..%2F..%2Fapp.db").status_code in (404, 400) and c1.get("/api/backup/files/auto-x.json.gz").status_code == 404,
       "only backup file names can be downloaded")
    # scheduler job is registered
    src = open(os.path.join(WT, "app", "scheduler.py"), encoding="utf-8").read()
    ok("daily_backup" in src and "for_each_customer" in src, "daily backup is a timed job for every account")

    # ---- while a restore runs
    jobs.RESTORING.add(T1)
    r = c1.post("/api/channels/1/toggle")
    ok(r.status_code == 423 and "restored" in r.json()["detail"], "changes to that account wait while its restore runs")
    ok(c1.get("/api/backup").status_code == 200 and owner.post("/api/backup/check", files={"file": ("x", b"", "x")}).status_code == 400,
       "reading still works, and other accounts are not held up")
    r = anon.get("/api/worker/jobs", headers={"x-worker-token": f"t{T1}.helper"})
    ok(r.status_code == 503, "that account's helper is asked to come back later")
    ran = []
    scheduler.for_each_customer(lambda: ran.append(tenant.current()))()
    ok(T1 not in ran and 1 in ran and T2 in ran, "timed jobs skip only the account being restored")
    with tenant.use(T1):
        try:
            backup.restore(json.loads(gzip.decompress(C1_FILE)), is_super=False); ok(False, "a second restore at the same time is refused")
        except backup.BackupError as e:
            ok("already running" in str(e), "a second restore at the same time is refused")
    jobs.RESTORING.discard(T1)
    jobs._running[T1] = 1
    real_wait = jobs.wait_idle
    jobs.wait_idle = lambda tid, s: real_wait(tid, 0.2)
    with tenant.use(T1):
        try:
            backup.restore(json.loads(gzip.decompress(C1_FILE)), is_super=False); ok(False, "busy refused")
        except backup.BackupError as e:
            ok("busy" in str(e) and T1 not in jobs.RESTORING, "while a clip is being made for that account the restore waits, then says try later (flag cleared)")
    jobs._running[T1] = 0; jobs.wait_idle = real_wait

    # ---- access control
    owner.post(f"/api/admin/users/{A1}/access", json={"perms": {"page.settings": False}})
    ok(c1.get("/api/backup").status_code == 403 and c1.post("/api/backup/restore", json={"ref": "upload", "confirm": "RESTORE"}).status_code == 403
       and c1.get("/api/backup/download").status_code == 403, "My channels switched off -> backup routes refused for that customer")
    ok(c2.get("/api/backup").status_code == 200 and owner.get("/api/backup").status_code == 200, "…other customers and the Super Admin unaffected")
    owner.post(f"/api/admin/users/{A1}/access", json={"perms": {"page.settings": True}})
    for m, pth in [("GET", "/api/backup"), ("GET", "/api/backup/download"), ("GET", "/api/backup/files/auto-1.json.gz"), ("POST", "/api/backup/check"),
                   ("POST", "/api/backup/files/x/check"), ("POST", "/api/backup/restore")]:
        ok(acl.rule_for(m, pth) and acl.rule_for(m, pth)[0] == "page.settings", f"ACL line for {m} {pth}")
    ok(acl.rule_for("POST", "/api/backup/restore")[1], "restore is written to the activity log")
    html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
    ok("Backup &amp; restore" in html and "Download backup now" in html and "Type RESTORE" in html and "Reconnect" in html, "dashboard card is there")
    for f in ("backup.py", "main.py", "jobs.py", "scheduler.py", "growth.py", "acl.py"):
        ok(b"\x08" not in open(os.path.join(WT, "app", f), "rb").read(), f"no backspace byte in app/{f}")

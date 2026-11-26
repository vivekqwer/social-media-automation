"""Regression tests for the final review of the merged features."""
import os, sys, tempfile, json
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "r4-test-pass-551"
os.environ["KEYWORDS_OFFLINE"] = "1"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, telegram, accounts, tenant, metadata, acl
from app.db import SessionLocal, Destination, Clip, Project, set_setting, utcnow, tenant_exists
from datetime import timedelta
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
main.executor.submit = lambda *a, **k: None
PW = "customer-r4-pass-1"
with TestClient(main.app) as owner, TestClient(main.app) as c, TestClient(main.app) as anon:
    owner.post("/api/login", json={"username": "admin", "password": "r4-test-pass-551"})
    rr = anon.post("/api/register", json={"name": "Wipe Me", "brand": "Wipe Co", "email": "w@example.com", "password": PW, "accept_terms": True}); print("register", rr.status_code, rr.text[:120])
    aid = owner.get("/api/admin/users").json()["users"][0]["id"]; owner.post(f"/api/admin/users/{aid}/approve"); T = aid + 1
    ok(tenant_exists(T), "approved customer has a copy")
    r = owner.post(f"/api/admin/users/{aid}/delete-data", json={"email": "w@example.com", "confirm": "w@example.com"})
    if r.status_code != 200:
        r = owner.post(f"/api/admin/users/{aid}/delete-data", json={"confirm_email": "w@example.com"})
    ok(r.status_code == 200, f"account data deleted ({r.status_code} {r.text[:80]})")
    import app.db as dbm
    dbm._ready.discard(T); dbm._engines.pop(T, None); dbm._makers.pop(T, None)
    ok(not tenant_exists(T), "its copy is gone")
    owner.get("/api/admin/users"); owner.get(f"/api/admin/users/{aid}/access")
    ok(not tenant_exists(T) and not (Path(d) / "tenants" / str(T) / "app.db").exists(), "opening the Users list does NOT create an empty copy again")
    # approve keeps what the owner reviewed
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x"))
        p = Project(url="u", video_id="abcdefghijk", title="Butter Chicken Recipe", mode="cc", license="Creative Commons Attribution license (reuse allowed)", duration=60)
        s.add(p); s.commit()
        old = (utcnow() - timedelta(days=3)).isoformat() + "Z"
        cl = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="rendered", title="Owner reviewed title", description="d", tags=["a"],
                  destination_id=1, kw={"at": old, "items": [], "net": "ok"}, output_path=__file__)
        s.add(cl); s.commit(); CID = cl.id
    called = []
    metadata.propose = lambda *a, **k: called.append(a) or {"title": "NEW", "description": "x", "tags": ["x"], "kw": {"net": "ok", "items": []}}
    owner.post(f"/api/clips/{CID}/approve", json={})
    with SessionLocal() as s:
        ok(s.get(Clip, CID).title == "Owner reviewed title" and not called, "Approve keeps exactly the title the owner reviewed (no network calls)")
    # the daily refresh keeps the owner's own picks
    with SessionLocal() as s:
        x = s.get(Clip, CID); x.status = "queued"; x.kw = {"at": old, "net": "ok", "items": [{"k": "my own pick", "src": ["extra"]}, {"k": "auto", "src": ["typed"]}]}; s.commit()
    metadata.freshen_clip(CID)
    ok(called and called[-1][1] == ["my own pick"], f"refresh re-uses the owner's own keyword picks: {called[-1] if called else None}")
    # access-control lines each have their own id
    st = {"perms": {"uploads.youtube": True, "uploads.auto": False}, "controls": {"uploads_paused": True, "sources_stopped": True}, "limits": {}}
    acl_state = acl.state
    acl.state = lambda *a, **k: st
    keys = [i.get("key") for i in acl.health_items()]
    acl.state = acl_state
    ok(len(keys) == 3 and len(set(keys)) == 3 and all(keys), f"each access-control line has its own id: {keys}")
    # keyword research: Ideas page switch + per-customer limit
    hit = [r for r in acl.ROUTES if r[1] == "/api/keywords"]
    ok(hit and hit[0][2] == "page.ideas", "keyword research needs the Ideas page")
    main._kw_calls.clear()
    lim = [main._kw_rate_limited(5) for _ in range(41)]
    ok(lim[:40] == [False] * 40 and lim[40] is True and not any(main._kw_rate_limited(1) for _ in range(60)), "a customer is limited to 40 researches an hour; the owner is not")
    # blocked in some countries = still live: not removable; removed clips do not count as uploaded
    with SessionLocal() as s:
        b = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="BBBBBBBBBBB", yt_state="blocked:3", destination_id=1)
        g = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="GGGGGGGGGGG", yt_state="removed", destination_id=1)
        s.add_all([b, g]); s.commit(); B, G = b.id, g.id
    ok(owner.post(f"/api/clips/{B}/remove").status_code == 409, "a video blocked only in some countries is still live: Remove refused")
    n0 = owner.get("/api/overview").json()["counts"]["uploaded"]
    owner.post(f"/api/clips/{G}/remove")
    ok(owner.get("/api/overview").json()["counts"]["uploaded"] == n0 - 1, "a removed clip no longer counts in 'uploaded'")

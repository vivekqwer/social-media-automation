import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "chan-test-pass-44"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, autopilot, youtube
from app.db import SessionLocal, Destination, Channel, Clip, Project, main_destination, adopt_orphans
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "chan-test-pass-44"})
    with SessionLocal() as s:
        a = Destination(channel_id="UCa", name="Foody", token="x"); s.add(a); s.commit()
        src1 = Channel(kind="topic", query="street food", name="Street food", mode="cc", destination_id=a.id, auto=False)
        src2 = Channel(kind="reel", query="Food facts", name="Reels", mode="original", destination_id=a.id, auto=False)
        s.add_all([src1, src2]); s.commit()
        p = Project(url="u", video_id="abcdefghijk", title="T", channel="C", mode="cc", duration=60, channel_ref=src1.id); s.add(p); s.commit()
        s.add_all([Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="queued", destination_id=a.id),
                   Clip(project_id=p.id, start=30, end=50, aspect="9:16", status="rendered", destination_id=a.id),
                   Clip(project_id=p.id, start=0, end=20, aspect="9:16", status="uploaded", youtube_id="Y1", destination_id=a.id)])
        s.commit(); A, S1, S2 = a.id, src1.id, src2.id
    L = tc.get("/api/destinations").json()
    ok(len(L) == 1 and L[0]["is_main"] and L[0]["sources"] == 2 and L[0]["pending"] == 2 and L[0]["uploaded"] == 1, f"one channel: main, 2 sources, 2 waiting clips, 1 uploaded")
    # 1) keep the channel AND add a second one
    with SessionLocal() as s:
        b = Destination(channel_id="UCb", name="Funny", token="x"); s.add(b); s.commit(); B = b.id
    L = {x["name"]: x for x in tc.get("/api/destinations").json()}
    ok(L["Foody"]["is_main"] and not L["Funny"]["is_main"] and L["Funny"]["sources"] == 0, "second channel added; nothing moved; Foody stays main")
    r = tc.post(f"/api/channels/{S2}/destination", json={"destination_id": B})
    ok(r.status_code == 200 and r.json()["destination_id"] == B, "one source sent to the new channel; the other stays")
    r = tc.post(f"/api/destinations/{B}/main").json()
    with SessionLocal() as s:
        ok(r["is_main"] and main_destination(s).id == B, "new channel made main (new sources / uploads go there)")
    r = tc.post("/api/upload-video", files={"file": ("x.txt", b"x", "text/plain")})
    # 2) switch: move everything to the new channel in one press
    r = tc.post(f"/api/destinations/{B}/take-over", json={}).json()
    ok(r["sources"] == 1 and r["clips"] == 2, f"take-over moved the rest: {r['sources']} source, {r['clips']} waiting clips")
    with SessionLocal() as s:
        up = s.query(Clip).filter(Clip.status == "uploaded").first()
        ok(up.destination_id == A, "the video already on YouTube stays with its channel")
    # 3) disconnect the old channel: nothing depends on it any more
    r = tc.delete(f"/api/destinations/{A}")
    ok(r.status_code == 200 and r.json()["deleted"], "old channel disconnected without errors")
    # 4) disconnect the only channel while it has work: refused with a clear reason, then forced; the work waits
    r = tc.delete(f"/api/destinations/{B}")
    ok(r.status_code == 409 and "Connect the new channel first" in r.json()["detail"], "last channel with work: explained, not silently broken")
    r = tc.delete(f"/api/destinations/{B}?force=true").json()
    ok(r["deleted"] and r["waiting"], "disconnect anyway: the work is kept and waits")
    with SessionLocal() as s:
        ok(s.query(Channel).filter(Channel.destination_id.is_(None)).count() == 2, "sources kept (no channel yet)")
    autopilot.run_channel(S1)
    with SessionLocal() as s:
        ok("connect a YouTube channel" in (s.get(Channel, S1).last_status or ""), "a source without a channel waits and says why")
    h = tc.get("/api/health").json()
    ok(any(i.get("fix") == "connect" for i in h["items"]), "Problems & fixes: no channel connected -> Connect button")
    # 5) a brand-new channel is connected: everything attaches to it by itself
    with SessionLocal() as s:
        c = Destination(channel_id="UCc", name="New one", token="x"); s.add(c); s.commit(); C = c.id
        n = adopt_orphans(s)
    ok(n["sources"] == 2 and n["clips"] == 2, f"new channel adopted everything by itself: {n}")
    L = tc.get("/api/destinations").json()
    ok(len(L) == 1 and L[0]["is_main"] and L[0]["sources"] == 2 and L[0]["pending"] == 2, "new channel is main with all sources and waiting clips")
    # 6) move-to on disconnect
    with SessionLocal() as s:
        e = Destination(channel_id="UCe", name="Extra", token="x"); s.add(e); s.commit(); E = e.id
    r = tc.delete(f"/api/destinations/{C}")
    ok(r.status_code == 409 and "Choose the channel" in r.json()["detail"], "with another channel: asks where the work should go")
    r = tc.delete(f"/api/destinations/{C}?move_to={E}").json()
    ok(r["deleted"] and r["sources"] == 2 and r["clips"] == 2, "disconnect + move everything in one step")
    ok(tc.delete(f"/api/destinations/{E}?move_to={E}").status_code == 400, "cannot move to itself")
    ok(tc.post("/api/destinations/9999/main").status_code == 404, "unknown channel -> 404")

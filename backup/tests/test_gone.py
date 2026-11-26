"""A video deleted from YouTube: Upload again / Remove, and the clip and the Alerts tab always agree."""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "gone-test-pass-73"
os.environ["KEYWORDS_OFFLINE"] = "1"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, telegram, accounts
from app.db import SessionLocal, Destination, Clip, Project, Alert, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
main.executor.submit = lambda *a, **k: None
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "gone-test-pass-73"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); p = Project(url="u", video_id="abcdefghijk", title="T", mode="cc", license="Creative Commons", duration=60); s.add(p); s.commit()
        a = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="AAAAAAAAAAA", yt_state="removed", title="Gone one", description="d", destination_id=1)
        b = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="BBBBBBBBBBB", yt_state="removed", title="Gone two", description="d", destination_id=1)
        live = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="CCCCCCCCCCC", yt_state="ok", title="Live", description="d", destination_id=1)
        cr = Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="DDDDDDDDDDD", yt_state="rejected:copyright", title="Strike", description="d", destination_id=1)
        s.add_all([a, b, live, cr]); s.commit()
        for c, k in ((a, "removed"), (b, "removed"), (cr, "rejected")):
            s.add(Alert(clip_id=c.id, kind=k, message="The video is no longer on YouTube.", details={"title": c.title, "youtube_id": c.youtube_id, "state": c.yt_state}))
        s.commit(); A, B, L, CR = a.id, b.id, live.id, cr.id
    board = lambda: {i["clip_id"]: i for i in tc.get("/api/alerts/board").json()["tabs"]["deleted"] + tc.get("/api/alerts/board").json()["tabs"]["strikes"]}
    acts = lambda i: [x["do"] for x in i["actions"]]
    bd = board()
    ok("again" in acts(bd[A]) and "remove" in acts(bd[A]), "Deleted tab: the deleted video offers Upload again and Remove")
    ok("again" not in acts(bd[CR]) and "remove" in acts(bd[CR]), "copyright removal: Remove yes, Upload again no (strike risk)")
    # Upload again from the Alerts tab -> the clip knows it, the tab shows it, no double copies
    r = tc.post(f"/api/clips/{A}/reupload", json={"confirm": True}); new = r.json()["id"]
    ca = tc.get(f"/api/clips/{A}").json()
    bd = board()
    ok(ca["reuploaded_as"] == new and "again" not in acts(bd[A]) and "Uploaded again as clip" in bd[A]["text"] and bd[A]["seen"], f"after Upload again: clip and Alerts both say 'uploaded again as #{new}', alert answered")
    ok([x for x in bd[A]["actions"] if x["do"] == "open_clip"][0]["id"] == new, "the alert opens the new copy")
    ok(tc.post(f"/api/clips/{A}/reupload", json={"confirm": True}).status_code == 409, "a second Upload again is refused (no duplicate copies)")
    # Remove from the clip -> gone from the lists, the Alerts tab shows it as removed, can be shown again
    ok(tc.post(f"/api/clips/{L}/remove").status_code == 409, "a video that is still live cannot be removed here")
    r = tc.post(f"/api/clips/{B}/remove")
    listed = [c["id"] for c in tc.get("/api/clips").json()]
    bd = board()
    ok(r.status_code == 200 and B not in listed and L in listed, "Removed: it leaves the clip lists, others stay")
    ok("Removed from the dashboard" in bd[B]["text"] and acts(bd[B]) == ["link", "restore"] and bd[B]["seen"], "the Alerts tab shows the same: removed, with 'Show it again'")
    ok(tc.get(f"/api/clips/{B}").status_code == 200, "its history is kept (the clip still opens)")
    tc.post(f"/api/clips/{B}/restore")
    ok(B in [c["id"] for c in tc.get("/api/clips").json()] and "remove" in acts(board()[B]), "Show it again: back in the lists, Remove offered again")
    # remove from the Alerts tab for the copyright one
    tc.post(f"/api/clips/{CR}/remove")
    ok(CR not in [c["id"] for c in tc.get("/api/clips").json()] and tc.get(f"/api/clips/{CR}").json()["removed_at"], "Remove from the Alerts tab works the same as from the clip")
    ok(tc.get("/api/overview").json()["alerts_badge"] <= 1, "answered alerts no longer count in the sidebar number")

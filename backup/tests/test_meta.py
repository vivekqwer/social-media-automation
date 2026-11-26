import os, sys, tempfile, time
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "meta-test-pass-81"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, meta
from app.db import SessionLocal, Destination, Project, Clip, get_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
calls = []


class R:
    def __init__(self, d): self.d = d
    def json(self): return self.d


def fake_get(url, params=None, **k):
    calls.append(("GET", url))
    if url.endswith("/me/accounts"):
        return R({"data": [{"id": "PAGE1", "name": "Foody Page", "access_token": "PAGETOKEN123", "instagram_business_account": {"id": "IG1", "username": "foody.fan"}}]})
    return R({"status_code": "FINISHED"})


def fake_post(url, data=None, **k):
    calls.append(("POST", url, dict(data or {})))
    if url.endswith("/IG1/media"):
        return R({"id": "CONT1"})
    if url.endswith("/media_publish"):
        return R({"id": "IGPOST1"})
    if url.endswith("/videos"):
        return R({"id": "FBVID1"})
    return R({})


meta.httpx.get, meta.httpx.post = fake_get, fake_post
meta.time.sleep = lambda s: None
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "meta-test-pass-81"})
    r = tc.post("/api/meta/setup", json={"token": "USERTOKEN-abcdefghijklmnop"}).json()
    ok(r["connected"] and r["page"] == "Foody Page" and r["instagram"] == "foody.fan", "Page + Instagram found from the token")
    ok("PAGETOKEN123" not in (get_setting("meta_token") or ""), "Page token stored encrypted")
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
        out = os.path.join(d, "o.mp4"); open(out, "wb").write(b"video-bytes")
        p_cc = Project(url="u", video_id="abcdefghijk", title="T", channel="Cook", mode="cc", duration=60); s.add(p_cc)
        p_perm = Project(url="u", video_id="bbbbbbbbbbb", title="T", channel="Cook", mode="permission", duration=60); s.add(p_perm); s.commit()
        s.add_all([Clip(project_id=p_cc.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="Y1", title="Samosa", description="#shorts\n\nCredit: Cook\n--- Original description ---\nxyz", output_path=out),
                   Clip(project_id=p_perm.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id="Y2", title="Perm", description="d", output_path=out)])
        s.commit()
    res = meta.crosspost(1)
    ok(res.get("instagram") == "IGPOST1" and res.get("facebook") == "FBVID1", f"posted to Instagram + Facebook {res}")
    cap = [c[2] for c in calls if c[0] == "POST" and c[1].endswith("/IG1/media")][0]["caption"]
    ok("Samosa" in cap and "Credit: Cook" in cap and "Original description" not in cap, "caption keeps title + credit")
    ok(meta.crosspost(1).get("skipped"), "never posted twice")
    ok(meta.crosspost(2).get("skipped"), "permission-mode clips are not cross-posted")
    url = [c[2] for c in calls if c[0] == "POST" and c[1].endswith("/IG1/media")][0]["video_url"]
    path = url.split("://", 1)[1].split("/", 1)[1]
    ok(tc.get("/" + path).content == b"video-bytes", "signed link serves the video without login")
    bad = "/" + path.rsplit("/", 1)[0] + "/" + "0" * 32 + ".mp4"
    ok(tc.get(bad).status_code == 404, "wrong signature refused")
    exp_old = f"/media/1/{int(time.time()) - 10}/{meta._sig(1, int(time.time()) - 10)}.mp4"
    ok(tc.get(exp_old).status_code == 404, "expired link refused")

"""My own video + "what is it about" -> title, description, tags, thumbnail text and an upload time from a search on
the topic (faked YouTube); the search budget is respected with a fallback; no credit; nothing is posted."""
import json, os, subprocess, sys, tempfile
from datetime import timedelta
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = os.environ.get("RS_WT", r"D:\social medi engin\.claude\worktrees\agent-a18fd1cc3eb839fbb")
sys.path.insert(0, WT)
from fastapi.testclient import TestClient
from app import main, auth, youtube, telegram, jobs, quota, owntopic, learn, metadata, acl
from app import keywords as K
from app.db import SessionLocal, Destination, Clip, Project, set_setting, get_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
submitted = []
jobs.executor.submit = lambda fn, *a, **k: submitted.append((getattr(fn, "__name__", "?"), a))
uploads = []
youtube.upload = lambda *a, **k: uploads.append(a) or "x"
SUG = {"street food delhi": ["street food delhi", "street food delhi chandni chowk", "street food delhi under 100", "street food delhi vlog"],
       "गाजर का हलवा": ["गाजर का हलवा", "गाजर का हलवा रेसिपी", "gajar ka halwa"]}
K._get = lambda q, hl, gl: SUG.get(q, [q + " recipe"] if q.startswith("gajar") else [])
youtube.configured = lambda: True
calls = {"search": 0, "videos": 0}
TOP = [("Delhi Street Food Tour | Chandni Chowk", ["street food delhi", "chandni chowk", "delhi food", "indian street food"], 2_400_000, "PT58S"),
       ("Rs 100 Street Food Challenge in Delhi", ["street food delhi under 100", "food challenge", "delhi"], 1_100_000, "PT12M"),
       ("Best Paratha in Delhi", ["paratha", "street food delhi", "parathe wali gali"], 900_000, "PT45S"),
       ("Old Delhi Food Walk", ["old delhi food", "street food delhi", "food walk"], 700_000, "PT20M"),
       ("Momos in Delhi", ["momos", "delhi street food"], 500_000, "PT30S"),
       ("Chole Bhature Delhi", ["chole bhature", "street food delhi"], 300_000, "PT50S")]


class Exec:
    def __init__(self, data): self.data = data
    def execute(self): return self.data


class Fake:
    def search(self):
        class S:
            def list(self, **kw):
                calls["search"] += 1
                calls["last"] = kw
                return Exec({"items": [{"id": {"videoId": f"v{i}"}} for i in range(len(TOP))]})
        return S()

    def videos(self):
        class V:
            def list(self, **kw):
                calls["videos"] += 1
                return Exec({"items": [{"id": f"v{i}", "snippet": {"title": t, "channelTitle": "Foodie", "tags": tg},
                                        "statistics": {"viewCount": str(v)}, "contentDetails": {"duration": du}}
                                       for i, (t, tg, v, du) in enumerate(TOP)]})
        return V()


youtube.client = lambda dest_id=None: Fake()
learn.current = lambda now=None: {"enough": False, "hours": []}


def video(path: Path, w=360, h=640, secs=3):
    ff = os.environ.get("FFMPEG_BIN") or "ffmpeg"
    subprocess.run([ff, "-y", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=10:duration={secs}", "-pix_fmt", "yuv420p", str(path)], capture_output=True)
    return path


def clip(cid):
    with SessionLocal() as s:
        return s.get(Clip, cid)


with TestClient(main.app, raise_server_exceptions=False) as c:
    c.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Raji Vlogs", token="x")); s.commit()
    f = video(Path(d) / "VID_20261101_123456.mp4")
    r = c.post("/api/upload-video", files={"file": ("VID_20261101_123456.mp4", open(f, "rb"), "video/mp4")},
               data={"title": "VID_20261101_123456", "topic": "  street food   delhi "})
    ok(r.status_code == 200 and r.json().get("topic") is True, f"upload with a topic is accepted ({r.status_code} {r.text[:120]})")
    cid = r.json()["clip_id"]
    ok([n for n, _ in submitted] == ["fill_then_render"] and submitted[0][1] == (cid, "street food delhi"),
       f"the search runs in the background, before the render ({submitted})")
    submitted.clear()
    used0 = quota.used()
    kwrec = owntopic.fill(cid, "street food delhi")
    cl = clip(cid)
    ok(calls["search"] == 1 and calls["videos"] == 1 and calls["last"]["regionCode"] == "IN" and calls["last"]["order"] == "viewCount"
       and calls["last"]["q"] == "street food delhi" and "publishedAfter" in calls["last"],
       "one official search: India, most viewed, this week, for the topic; then the videos' details")
    ok(quota.used() - used0 == 101 and quota.spent_on_search() == 100, f"its cost is counted: 100 + 1 units, 100 in the search budget ({quota.used() - used0})")
    ok(cl.title.startswith("Street Food Delhi") and "VID_" not in cl.title and len(cl.title) <= 100, f"title from what people type, not the camera file name ({cl.title})")
    lines = cl.description.split("\n")
    tags_line = next(l for l in lines if l.startswith("#"))
    n_hash = len(tags_line.split())
    ok(lines[0].startswith("Street Food Delhi") and 3 <= n_hash <= 5 and "#shorts" in tags_line, f"description: hook line, then 3-5 hashtags ({lines[0]} / {tags_line})")
    ok("This video is about street food delhi." in cl.description and "Subscribe to Raji Vlogs" in cl.description, "…a line about the video and the channel's call to subscribe")
    ok("Credit" not in cl.description and "Original video" not in cl.description and cl.credit_on is False, "no credit lines for my own video")
    ok(cl.tags and metadata.tag_chars(cl.tags) <= 500 and "street food delhi" in [t.lower() for t in cl.tags] and "chandni chowk" in [t.lower() for t in cl.tags],
       f"tags from the suggestions and the top videos' tags, inside 500 characters ({metadata.tag_chars(cl.tags)})")
    ok(cl.thumb_text and cl.thumb_text == cl.thumb_text.upper() and "STREET FOOD" in cl.thumb_text, f"thumbnail text ({cl.thumb_text})")
    b = cl.kw["basis"]
    ok(len(b["videos"]) == 5 and b["videos"][0]["title"].startswith("Delhi Street Food Tour") and b["videos"][0]["views"] == 2_400_000,
       "what it found is stored: the top 5 videos with their views")
    ok(b["line"].startswith("based on: top 5 Shorts and videos for “street food delhi” in India this week") and b["source"] == "live" and not b["why_cached"],
       f"…and why, in one line ({b['line']})")
    ok(cl.kw["note"].startswith("Chosen from what people are searching and watching now") and "viral" not in json.dumps(cl.kw).lower(),
       "the words are honest: chosen from what people search and watch now; no promise of viral")
    ok(cl.publish_at and cl.publish_at > utcnow() and "next free upload time" in b["slot_why"], f"an upload time: the next free slot while there are too few numbers ({b['slot_why']})")
    ok(cl.status == "draft" and not cl.autoqueue and not uploads, "nothing is queued or posted: the owner approves first")
    ok(metadata.needs_refresh(cl) is False and metadata.freshen_clip(cid) is False and clip(cid).title == cl.title,
       "the timed keyword refresh leaves it alone (no second search before the upload)")
    g = c.get(f"/api/clips/{cid}").json()
    ok(g["kw"]["basis"]["videos"][0]["views"] == 2_400_000 and g["own_upload"], "the editor gets what it was based on")
    r = c.put(f"/api/clips/{cid}", json={"title": "My own title", "tags": ["a", "b"]})
    ok(r.status_code == 200 and clip(cid).title == "My own title" and clip(cid).tags == ["a", "b"], f"everything stays editable ({r.status_code})")

    # ---------- What works: the best hour when there are enough numbers
    learn.current = lambda now=None: {"enough": True, "hours": [{"key": 19, "label": "19:00", "ratio": 1.8, "n": 7}, {"key": 9, "ratio": 0.9, "n": 6}]}
    with SessionLocal() as s:
        p = Project(url="", title="My video", channel="Raji Vlogs", mode="original", license="Original content (my own video)", duration=40)
        s.add(p); s.commit()
        c2 = Clip(project_id=p.id, start=0, end=40, aspect="16:9", status="draft", credit_on=False, destination_id=1)
        s.add(c2); s.commit(); cid2 = c2.id
    owntopic.fill(cid2, "street food delhi")
    cl2 = clip(cid2)
    ist = cl2.publish_at + timedelta(hours=5, minutes=30)
    ok(ist.hour == 19 and ist.minute == 0 and cl2.publish_at > utcnow() and "1.8×" in cl2.kw["basis"]["slot_why"], f"enough numbers: the best hour from What works ({ist:%H:%M} IST)")
    ok("#shorts" not in cl2.description and 3 <= len(next(l for l in cl2.description.split("\n") if l.startswith("#")).split()) <= 5,
       "a wide video: no #shorts, still 3-5 hashtags")
    # the daily upload budget: a full day moves the best hour to the next day
    with SessionLocal() as s:
        for _ in range(quota.capacity()):
            s.add(Clip(project_id=p.id, start=0, end=40, aspect="9:16", status="queued", publish_at=cl2.publish_at, destination_id=1))
        c2b = Clip(project_id=p.id, start=0, end=40, aspect="16:9", status="draft", credit_on=False, destination_id=1)
        s.add(c2b); s.commit(); cid2b = c2b.id
    owntopic.fill(cid2b, "street food delhi")
    moved = clip(cid2b).publish_at
    ok(moved - cl2.publish_at == timedelta(days=1), f"that day is full ({quota.capacity()} uploads): the same hour on the next day ({moved} vs {cl2.publish_at})")
    with SessionLocal() as s:
        s.query(Clip).filter(Clip.status == "queued", Clip.publish_at == cl2.publish_at).delete(); s.commit()
    # Approve from the grid (no time sent) keeps the proposed time, like the editor's Approve
    rendered = Path(d) / "rendered.mp4"; rendered.write_bytes(b"x")
    with SessionLocal() as s:
        cc = s.get(Clip, cid2); cc.status, cc.output_path = "rendered", str(rendered); s.commit()
    proposed = clip(cid2).publish_at
    r = c.post(f"/api/clips/{cid2}/approve", json={})
    ok(r.status_code == 200 and clip(cid2).status == "queued" and clip(cid2).publish_at == proposed, f"grid Approve keeps the proposed time ({r.status_code} {r.text[:120]})")
    learn.current = lambda now=None: {"enough": False, "hours": []}

    # ---------- the search budget: no search, the saved one is used and it says so
    set_setting("quota-search:" + quota._pt_day(), str(quota.SEARCH_BUDGET))
    with SessionLocal() as s:
        p = Project(url="", title="clip", channel="", mode="original", license="Original content (my own video)", duration=40)
        s.add(p); s.commit()
        c3 = Clip(project_id=p.id, start=0, end=40, aspect="9:16", status="draft", credit_on=False, destination_id=1)
        c4 = Clip(project_id=p.id, start=0, end=40, aspect="9:16", status="draft", credit_on=False, destination_id=1)
        s.add_all([c3, c4]); s.commit(); cid3, cid4 = c3.id, c4.id
    before = calls["search"]
    owntopic.fill(cid3, "street food delhi")
    cl3 = clip(cid3)
    b3 = cl3.kw["basis"]
    ok(calls["search"] == before, "budget used up: no search is made")
    ok(b3["source"] == "saved" and "search budget" in b3["why_cached"] and "saved" in b3["why_cached"] and len(b3["videos"]) == 5
       and "Not a live search" in cl3.kw["note"], f"…the search saved earlier for this topic is used, and it says so ({b3['why_cached']})")
    ok(cl3.title.startswith("Street Food Delhi") and cl3.tags and cl3.thumb_text, "…and everything is still filled in")
    owntopic.fill(cid4, "गाजर का हलवा")
    cl4 = clip(cid4)
    b4 = cl4.kw["basis"]
    ok(b4["source"] == "saved" and b4["videos"] == [] and "only the search suggestions" in b4["why_cached"] and cl4.title and "गाजर" in cl4.title,
       f"a new topic without a saved search: the search suggestions alone, any language ({cl4.title})")
    ok(b4["line"].startswith("based on: what people type"), "…and the reason says so")
    youtube.configured = lambda: False
    with SessionLocal() as s:
        c5 = Clip(project_id=p.id, start=0, end=40, aspect="9:16", status="draft", credit_on=False, destination_id=1); s.add(c5); s.commit(); cid5 = c5.id
    set_setting("quota-search:" + quota._pt_day(), "0")
    owntopic.fill(cid5, "street food delhi")
    ok(calls["search"] == before and "not set up" in clip(cid5).kw["basis"]["why_cached"], "no Google keys: no search, said plainly")
    youtube.configured = lambda: True
    def boom(dest_id=None):
        raise RuntimeError("connection reset")
    youtube.client = boom
    with SessionLocal() as s:
        c6 = Clip(project_id=p.id, start=0, end=40, aspect="9:16", status="draft", credit_on=False, destination_id=1); s.add(c6); s.commit(); cid6 = c6.id
    owntopic.fill(cid6, "street food delhi")
    ok(clip(cid6).title and "did not answer" in clip(cid6).kw["basis"]["why_cached"], "a failed search never stops it")
    youtube.client = lambda dest_id=None: Fake()
    # ---------- long tags stay inside YouTube's limit
    TOP[:] = [(f"Video {i}", [f"street food delhi very long tag number {i} " + "x" * 20 for _ in range(1)] + [f"delhi street food {i} special edition long"], 10 ** 6 - i, "PT30S") for i in range(25)]
    SUG["street food delhi"] = SUG["street food delhi"] + [f"street food delhi {w} long phrase people type" for w in "abcdefghij"]
    with SessionLocal() as s:
        c7 = Clip(project_id=p.id, start=0, end=40, aspect="9:16", status="draft", credit_on=False, destination_id=1); s.add(c7); s.commit(); cid7 = c7.id
    owntopic.fill(cid7, "street food delhi")
    ok(metadata.tag_chars(clip(cid7).tags) <= 500 and len(clip(cid7).tags) >= 5, f"many long tags: still inside 500 characters ({metadata.tag_chars(clip(cid7).tags)})")
    # ---------- without a topic: as before
    submitted.clear()
    r = c.post("/api/upload-video", files={"file": ("trip.mp4", open(f, "rb"), "video/mp4")}, data={"title": "trip"})
    ok(r.status_code == 200 and r.json()["topic"] is False and [n for n, _ in submitted] == ["run_render"], "no topic: the upload works exactly as before")
    r = c.post("/api/upload-video", files={"file": ("trip.mp4", open(f, "rb"), "video/mp4")}, data={"title": "trip", "topic": "x" * 121})
    ok(r.status_code == 422, "a topic longer than 120 characters is refused")
    ok(acl.rule_for("POST", "/api/upload-video")[0] == "clips.own_video", "the upload stays behind 'Upload my own video'")
    # YouTube says the budget is used up: the app shows it as full (no more searches or uploads tried today)
    def over(dest_id=None):
        raise RuntimeError('<HttpError 403 "The request cannot be completed because you have exceeded your quota." reason: quotaExceeded>')
    youtube.client = over
    with SessionLocal() as s:
        c8 = Clip(project_id=p.id, start=0, end=40, aspect="9:16", status="draft", credit_on=False, destination_id=1); s.add(c8); s.commit(); cid8 = c8.id
    owntopic.fill(cid8, "street food delhi")
    ok(quota.used() >= quota.DAILY_UNITS and not quota.can_search() and "budget is used up" in clip(cid8).kw["basis"]["why_cached"],
       f"quotaExceeded: the budget is marked used up ({quota.used()})")

page = open(WT + "/static/index.html", encoding="utf-8").read()
box = page[page.index('id="own-box"'):page.index('id="own-box"') + 1200]
ok("What is this video about?" in box and "chosen from what people are searching and watching now" in box and "viral" not in box.lower(),
   "the form: one more field, honest words, no promise of viral")
ok("Chosen from what people are searching and watching now" in page and "Nobody can promise views" in page, "the editor shows why, without promises")
import re as _re
offers = page[page.index("function loadOffers") if "function loadOffers" in page else 0:]
ok("const upBox=$('#up-box')" in page and "$('#own-box').style" not in page and _re.search(r'<details id="up-box"[^>]*><summary[^>]*>More channels', page),
   "the plan tiles hide the 'More channels' box (#up-box), never the own-video topic box")
own = page[page.index('id="own-box"'):]
own = own[:own.index('</div></div></div>')]
ok('id="up-topic"' in own and 'id="up-pick"' in own, "the topic field and the file button sit in the own-video box")
print("DONE", flush=True)

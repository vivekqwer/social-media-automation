"""Growth fixes (26 Nov 2026, branch viral-fixes): hook titles instead of "| Best Moment", retitles of scheduled videos
(20 a day, uploads first), one topic per channel (park off-topic waiting clips, never more than 40%), 20 s clips that
end at a pause, 2 public posts a day. Each behind a Super Admin switch."""
import json, os, sys, tempfile
from datetime import timedelta
from types import SimpleNamespace as N
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = os.environ.get("RS_WT", r"D:\social medi engin\.claude\worktrees\agent-a2f16713871e26882")
sys.path.insert(0, WT)
from fastapi.testclient import TestClient
from app import main, auth, jobs, youtube, acl, metadata, hooks, niche_guard, tuneups, autopilot, quota, phone
from app.db import (Base, engine, migrate, SessionLocal, Channel, Clip, Destination, Project, set_setting, get_setting, utcnow)
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
main.scheduler.start = lambda: N(shutdown=lambda wait=False: None)  # no timed jobs (they would upload / sync during the test)

# ------------------------------------------------------------ before the first start: the owner's existing sources
Base.metadata.create_all(engine); migrate()
with SessionLocal() as s:
    foody = Destination(channel_id="UC-foody", name="Foody", token="x"); s.add(foody); s.commit()
    FOODY = foody.id
    s.add_all([Channel(kind="topic", query="indian street food", name="Topic: street food", mode="cc", auto=True, destination_id=FOODY,
                       niche="food", clip_len=35, post_times="18:00", tz_offset=-330),
               Channel(kind="channel", query="@cook", name="Cook", mode="permission", auto=True, destination_id=FOODY, clip_len=30,
                       post_times="18:00", tz_offset=-330),
               Channel(kind="channel", query="@long", name="Long", mode="cc", auto=True, destination_id=FOODY, clip_len=45),
               Channel(kind="viral", query="viral", name="Viral picks", mode="cc", auto=True, destination_id=FOODY, clip_len=35,
                       post_times="09:00, 12:00, 15:00, 18:00, 21:00", tz_offset=-330)])
    s.commit()

seq = [0]


def project(title, mode="cc", tags=None, channel="Street Eats", url=None, license=None):
    seq[0] += 1
    with SessionLocal() as s:
        p = Project(url="https://youtu.be/v%010d" % seq[0], video_id="v%010d" % seq[0], title=title, channel=channel,
                    channel_url=url or "https://www.youtube.com/channel/UC" + channel.replace(" ", ""), mode=mode,
                    license=license or youtube.CC_LICENSE, duration=900, tags=tags or [], description="Original words #a #b")
        s.add(p); s.commit()
        return p.id


def clip(pid, dest=None, status="queued", **kw):
    with SessionLocal() as s:
        c = Clip(project_id=pid, start=kw.pop("start", 60), end=kw.pop("end", 95), aspect="9:16", status=status,
                 destination_id=dest or FOODY, **kw)
        s.add(c); s.commit()
        return c.id


def get(cid):
    with SessionLocal() as s:
        return s.get(Clip, cid)


def P(pid):
    with SessionLocal() as s:
        return s.get(Project, pid)


with TestClient(main.app, raise_server_exceptions=False) as c:
    c.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})

    # ======================================================== 4. clip length migration (ran at start, once)
    with SessionLocal() as s:
        lens = {ch.name: ch.clip_len for ch in s.query(Channel)}
    ok(lens["Topic: street food"] == 20 and lens["Cook"] == 20 and lens["Viral picks"] == 20, f"start-up: the owner's 30 / 35 s sources make 20 s clips ({lens})")
    ok(lens["Long"] == 45, "a length the owner chose himself (45 s) is kept")
    ok(bool(get_setting(tuneups.MIG_LEN)), "the clip-length migration is marked done")
    with SessionLocal() as s:
        s.query(Channel).filter(Channel.name == "Cook").one().clip_len = 30; s.commit()
    ok(tuneups.migrate_clip_len() == 0, "the migration runs once: a second call changes nothing")
    with SessionLocal() as s:
        ok(s.query(Channel).filter(Channel.name == "Cook").one().clip_len == 30, "a length set again by the owner after the migration stays")
        nc = Channel(kind="topic", query="x", name="New", mode="cc"); s.add(nc); s.commit()
        ok(nc.clip_len == 20, "a NEW source makes 20 s clips by default")
        s.delete(nc); s.commit()
    ok(main.ChannelIn(query="x").clip_len == 20, "the add-source form's default is 20 s too")

    # ======================================================== 5. posts per day (migration at start, budget checked)
    with SessionLocal() as s:
        times = {ch.name: (ch.post_times, ch.tz_offset) for ch in s.query(Channel)}
    ok(times["Topic: street food"] == ("13:00, 19:00", -330) and times["Cook"] == ("13:00, 19:00", -330),
       f"start-up: the owner's sources post 2 a day at 13:00 and 19:00 IST ({times['Cook']})")
    ok(times["Viral picks"][0].startswith("09:00"), "viral picks keep their own times")
    ok(bool(get_setting(tuneups.MIG_POSTS)), "the posts-a-day migration is marked done (runs once)")
    ok(quota.capacity() == 5, f"YouTube budget: {quota.capacity()} uploads a day on one Google project")
    # slots: 2 a day; a clip that already has a time keeps it
    keep_at = utcnow() + timedelta(days=3)
    kid = clip(project("Old scheduled"), publish_at=keep_at)
    with SessionLocal() as s:
        ch = s.query(Channel).filter(Channel.name == "Cook").one()
        slots = []
        for i in range(4):
            t = autopilot._next_slot(s, ch.gap_hours, FOODY, ch.post_times, ch.tz_offset)
            slots.append(t)
            s.add(Clip(project_id=1, start=0, end=20, status="queued", destination_id=FOODY, publish_at=t)); s.commit()
    hm = sorted({(t.hour, t.minute) for t in slots})
    ok(hm == [(7, 30), (13, 30)], f"new slots are at 13:00 and 19:00 IST (07:30 / 13:30 UTC): {hm}")
    per_day = {}
    for t in slots:
        per_day[t.date()] = per_day.get(t.date(), 0) + 1
    ok(max(per_day.values()) == 2 and len(set(slots)) == 4, f"two slots a day ({sorted(str(t)[:16] for t in slots)})")
    ok(get(kid).publish_at == keep_at, "a clip that already had a publish time keeps it")
    with SessionLocal() as s:
        for x in s.query(Clip).filter(Clip.project_id == 1).all():
            s.delete(x)
        s.commit()
    r = c.post("/api/admin/posts-per-day", json={"n": 3})
    with SessionLocal() as s:
        d2 = Destination(channel_id="UC-two", name="Second", token="x"); s.add(d2); s.commit(); SECOND = d2.id
        s.add(Channel(kind="topic", query="gta", name="Games", mode="cc", auto=True, destination_id=SECOND, post_times="18:00")); s.commit()
    ok(r.status_code == 200 and r.json()["times"] == "10:00, 14:00, 19:00", f"one channel: 3 a day fits the budget ({r.json()})")
    r = c.post("/api/admin/posts-per-day", json={"n": 3})
    ok(r.status_code == 400 and "budget" in r.text, f"two channels x 3 = 6 > 5: refused, nothing changed ({r.text[:120]})")
    r = c.post("/api/admin/posts-per-day", json={"n": 2, "times": "12:30, 20:00"})
    ok(r.status_code == 200 and r.json()["times"] == "12:30, 20:00", "own times can be given")
    ok(c.post("/api/admin/posts-per-day", json={"n": 2, "times": "12:30"}).status_code == 400, "wrong number of times: refused")
    ok(c.get("/api/settings/posts-per-day").json()["n"] == 2, "GET posts-per-day shows the setting")
    c.post("/api/admin/posts-per-day", json={"n": 2})

    # natural end
    words = [{"tStartMs": 0, "dDurationMs": 60000, "segs": [{"utf8": "w", "tOffsetMs": int(t * 1000)} for t in
             [x * 0.4 for x in range(0, 53)] + [22.4 + x * 0.4 for x in range(0, 40)]]}]  # a 1.6 s pause after 20.8 s
    track = {"events": words}
    e = tuneups.natural_end(track, 0.0, 20.0, 600)
    ok(e is not None and 21.0 <= e <= 22.4, f"the clip ends in the pause of the speech (20.0 -> {e})")
    ok(tuneups.natural_end(track, 0.0, 30.0, 600) is None, "no pause within 3 s: the planned end stays")
    ok(tuneups.natural_end(None, 0.0, 20.0) is None, "no captions: the planned end stays")
    lines = {"events": [{"tStartMs": 0, "dDurationMs": 9000, "segs": [{"utf8": "First line."}]},
                        {"tStartMs": 9800, "dDurationMs": 9500, "segs": [{"utf8": "second part"}]},
                        {"tStartMs": 19500, "dDurationMs": 4000, "segs": [{"utf8": "third bit."}]}]}
    e2 = tuneups.natural_end(lines, 0.0, 21.0, 600)
    ok(e2 is not None and abs(e2 - 19.35) < 0.3, f"manual captions: the end moves to the line break (21.0 -> {e2})")

    # ======================================================== 1. hook titles
    pid = project("Indian Street Food Must Be Stopped", tags=["street food", "indian food"])
    ids = [clip(pid, status="draft") for _ in range(3)]
    metas = [metadata.build(P(pid), get(i), [], "en") for i in ids]
    titles = [m["title"] for m in metas]
    ok(all("Best Moment" not in t and "|" not in t for t in titles), f"no '| Best Moment' any more: {titles}")
    ok(len(set(titles)) == 3, "the 3 clips cut from the same video get 3 different titles")
    ok(all("Indian Street Food" in t for t in titles) and all(len(t) <= 60 for t in titles), "short (<= 60) and about the topic")
    ok(metadata.build(P(pid), get(ids[0]), [], "en")["title"] == titles[0], "the same clip always gets the same title (stable re-runs)")
    hg = metadata.build(P(pid), get(ids[0]), [], "hinglish")["title"]
    hi = metadata.build(P(pid), get(ids[0]), [], "hi")["title"]
    ok(any(w in hg for w in ("Dekh", "Kya", "Yeh", "Kabhi", "Ka ", "Sab", "Bhai", "Aisa", "Hua", "Try")) and "Best Moment" not in hg, f"Hinglish hook: {hg}")
    ok(any("\u0900" <= ch <= "\u097f" for ch in hi) and "सबसे बढ़िया पल" not in hi, f"Hindi hook: {hi}")
    ok(len(hooks.TEMPLATES["en"]) >= 12 and len(hooks.TEMPLATES["hinglish"]) >= 12 and len(hooks.TEMPLATES["hi"]) >= 12, "12+ templates per language")
    # credit block exactly as before: the description is the same with the switch on and off
    on_meta = metadata.build(P(pid), get(ids[0]), [], "en")
    hooks.set_off("titles.hook", True)
    off_meta = metadata.build(P(pid), get(ids[0]), [], "en")
    ok(off_meta["title"] == "Indian Street Food Must Be Stopped | Best Moment", f"switch off: the old title comes back ({off_meta['title']})")
    ok(on_meta["description"] == off_meta["description"] and on_meta["tags"] == off_meta["tags"], "description (credit, licence, original description) and tags are unchanged")
    ok("Credit: Street Eats - https://www.youtube.com/channel/UCStreetEats" in on_meta["description"] and "--- Original description ---" in on_meta["description"]
       and "Original video: Indian Street Food Must Be Stopped" in on_meta["description"], "the credit block is still there word for word")
    plan = {"at": "2026-11-26T00:00:00Z", "lead": "indian street food", "top": ["indian street food", "delhi food"], "hashtags": ["streetfood"],
            "items": [{"k": "indian street food", "src": ["typed"], "hot": False}], "net": "ok", "hint": ""}
    c_off = metadata.compose(P(pid), get(ids[1]), json.loads(json.dumps(plan)), "hinglish")
    hooks.set_off("titles.hook", False)
    c_on = metadata.compose(P(pid), get(ids[1]), json.loads(json.dumps(plan)), "hinglish")
    ok("Best Moment" not in c_on["title"] and "Indian Street Food" in c_on["title"], f"with keyword research: hook from the lead phrase ({c_on['title']})")
    ok(c_on["description"] == c_off["description"], "with keyword research: the description is the same as before")
    hint_t = hooks.hook_title(P(pid), N(id=None, project_id=None, kw={"hint": "delhi chaat"}), "en")
    ok("Delhi Chaat" in hint_t, f"the clip's own hint leads the title ({hint_t})")
    # what never changes
    own = project("My trip to Goa, part 2", mode="original", license="Original content (my own video)")
    oc = clip(own, status="draft")
    ok(metadata.build(P(own), get(oc), [], "en")["title"] == "My trip to Goa, part 2", "my own uploaded video keeps the title I typed")
    ed = clip(pid, status="queued", title="My own words", meta_edited=True, kw={"at": "2020-01-01T00:00:00Z", "net": "ok", "items": []})
    ok(metadata.freshen_clip(ed, force=True) is False and get(ed).title == "My own words", "a hand-edited clip is never rewritten")
    core = {t: hooks.core_topic(t, "H3 Podcast") for t in ["The Ethan Klein Files: Exposed | H3 Podcast Reaction",
                                                           "GTA 6 REVIEW - IS IT WORTH IT? (4K 60FPS)",
                                                           "Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film",
                                                           "STREET FOOD IN OLD DELHI 2024 🔥🔥 | Best Moment"]}
    ok(list(core.values()) == ["Ethan Klein Files", "GTA 6 Review", "Big Buck Bunny", "Street Food in Old Delhi"],
       f"source titles cleaned (channel, Reaction, |, Official, years, 4K/60fps, CAPS, emoji): {list(core.values())}")
    r = c.post(f"/api/clips/{ids[0]}/hook-title")
    ok(r.status_code == 200 and r.json()["title"] == titles[0] and get(ids[0]).title is None, "editor button: a suggestion, nothing saved")
    ok(c.post(f"/api/clips/{oc}/hook-title").status_code == 400, "editor button: my own video keeps its title")

    # ======================================================== 2. retitle scheduled videos
    calls = {"list": 0, "update": []}
    yt_title = {}

    class Req:
        def __init__(self, fn):
            self.fn = fn

        def execute(self):
            return self.fn()

    class Videos:
        def list(self, part, id, maxResults=1):
            calls["list"] += 1
            return Req(lambda: {"items": [{"id": id, "snippet": {"title": yt_title.get(id, ""), "description": "DESC " + id,
                                                                 "tags": ["t1"], "categoryId": "26"},
                                           "status": {"privacyStatus": "private", "publishAt": "2027-01-01T13:30:00Z"}}]})

        def update(self, part, body):
            calls["update"].append(body)
            yt_title[body["id"]] = body["snippet"]["title"]
            return Req(lambda: body)

    youtube.comment_client = lambda did: N(videos=lambda: Videos())
    rp = project("Women are THIRSTING over Duke Dennis 😂", channel="Drama Daily")
    up_ids = []
    for i in range(24):
        t = f"Old title {i} | Best Moment"
        cid = clip(rp, status="uploaded", youtube_id="yt%04d" % i, title=t, publish_at=utcnow() + timedelta(days=10 + i))
        yt_title["yt%04d" % i] = t
        up_ids.append(cid)
    pub = clip(rp, status="uploaded", youtube_id="ytpub", title="Public one", publish_at=utcnow() - timedelta(days=1)); yt_title["ytpub"] = "Public one"
    edt = clip(rp, status="uploaded", youtube_id="ytedt", title="Edited", meta_edited=True, publish_at=utcnow() + timedelta(days=5)); yt_title["ytedt"] = "Edited"
    byhand = clip(rp, status="uploaded", youtube_id="ythand", title="Mine | Best Moment", publish_at=utcnow() + timedelta(days=4)); yt_title["ythand"] = "Typed in Studio"
    used0 = quota.used()
    total = 0
    for _ in range(12):
        total += hooks.retitle_due()["done"]
    ok(total == hooks.RETITLE_PER_DAY == 20 and len(calls["update"]) == 20, f"at most 20 retitles a day ({total}, {len(calls['update'])} updates)")
    ok(quota.used() - used0 == 20 * (quota.COST["update"] + quota.COST["read"]) + quota.COST["read"],
       f"quota counted: 51 units per video (+1 for the one read and left alone) = {quota.used() - used0}")
    b0 = calls["update"][0]
    ok(b0["snippet"]["description"].startswith("DESC ") and b0["snippet"]["tags"] == ["t1"] and b0["snippet"]["categoryId"] == "26",
       "description, tags and category are sent back exactly as YouTube has them")
    ok(all("Best Moment" not in u["snippet"]["title"] for u in calls["update"]), "the new titles are hook titles")
    ok(get(pub).title == "Public one" and get(edt).title == "Edited", "public and hand-edited videos are left alone")
    ok((get(byhand).kw or {}).get("retitled", {}).get("skip") == "title changed on YouTube by hand" and yt_title["ythand"] == "Typed in Studio",
       "a title changed in YouTube Studio is never overwritten")
    done_ids = [i for i in up_ids if (get(i).kw or {}).get("retitled", {}).get("at")]
    ok(len(done_ids) == 20 and all(get(i).title == yt_title[get(i).youtube_id] for i in done_ids), "each done clip is marked and its title matches YouTube")
    set_setting(hooks._count_key(), "0")  # the next day
    quota.spend("read", units=quota.DAILY_UNITS - quota.used() - 100)  # today's budget nearly used
    r = hooks.retitle_due()
    ok(r["done"] == 0 and "budget" in r["stopped"], f"no room in today's budget: nothing is retitled, uploads first ({r})")
    with SessionLocal() as s:
        s.query(__import__("app.db", fromlist=["Setting"]).Setting).filter_by(key="quota:" + quota._pt_day()).delete(); s.commit()
    n_before = len(calls["update"])
    r = hooks.retitle_due(limit=50)
    ok(r["done"] == 4 and len(calls["update"]) == n_before + 4, f"next day: the last 4, each video only once ({r})")
    ok(hooks.retitle_due(limit=50)["done"] == 0, "nothing left: done once per video")
    hooks.set_off("titles.retitle", True)
    ok(hooks.retitle_due()["stopped"] == "switched off", "switch off: the job does nothing")
    hooks.set_off("titles.retitle", False)
    ok(c.get("/api/retitle").json()["done"] == 24, "GET /api/retitle counts the retitled videos")

    # ======================================================== 3. one topic per channel
    with SessionLocal() as s:
        for x in s.query(Clip).filter(Clip.status.in_(["queued", "fetching", "rendered", "draft"])).all():
            s.delete(x)
        s.commit()
    on_t = ["Delhi Street Food Tour", "Best Biryani in Hyderabad", "Momos Making in Old Delhi", "Spicy Pani Puri Challenge",
            "Paneer Tikka Recipe", "Mumbai Vada Pav", "Chole Bhature at a Dhaba", "Indian Street Food Must Be Stopped"]
    on_ids = [clip(project(t), status="queued", publish_at=utcnow() + timedelta(days=2 + i)) for i, t in enumerate(on_t)]
    off1 = clip(project("Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film", channel="Blender"), status="queued", publish_at=utcnow() + timedelta(days=20))
    off2 = clip(project("Women are THIRSTING over Duke Dennis", channel="Drama Daily"), status="fetching", publish_at=utcnow() + timedelta(days=21))
    off_up = clip(project("The Ethan Klein Files: Exposed", channel="Drama Daily"), status="uploaded", youtube_id="ytdrama", publish_at=utcnow() + timedelta(days=9))
    own_off = clip(project("My GTA stream highlights", mode="own", channel="Foody"), status="queued", publish_at=utcnow() + timedelta(days=22))
    no_niche = [clip(project("GTA 6 Review"), dest=SECOND, status="queued"), clip(project("Funny Cats"), dest=SECOND, status="queued")]
    pv = c.get("/api/admin/niche-guard").json()
    row = next(x for x in pv["channels"] if x["dest_id"] == FOODY)
    ok(sorted(o["clip_id"] for o in row["off"]) == sorted([off1, off2]), f"dry run lists exactly the 2 off-topic waiting clips ({[o['source'] for o in row['off']]})")
    ok(get(off1).status == "queued" and get(off2).status == "fetching", "the dry run changes nothing")
    row2 = next(x for x in pv["channels"] if x["dest_id"] == SECOND)
    ok(row2["niche"] is None and not row2["off"], "a channel with no niche is not checked")
    ok(c.get("/api/niche-guard").status_code == 200, "the account's own preview address answers")
    told = []
    phone._say = lambda text, *a, **k: told.append(text)
    r = c.post("/api/admin/niche-guard", json={"apply": True}).json()["result"]
    ok(r["parked"] == 2 and not r["refused"], f"applied: 2 parked ({r})")
    ok(get(off1).status == "draft" and get(off1).error.startswith("Parked: off topic for Foody") and get(off1).publish_at is None
       and get(off2).status == "draft", "off-topic clips are parked (draft, 'Parked:' note), never deleted")
    ok(all(get(i).status == "queued" for i in on_ids), "every on-topic clip stays queued (biryani, momos, vada pav... too)")
    ok(get(off_up).status == "uploaded" and get(own_off).status == "queued", "a video already on YouTube and my own video are never parked")
    ok(all(get(i).status == "queued" for i in no_niche), "the channel without a niche is untouched")
    set_setting("unpark_per_day", "5"); autopilot.disk_free_gb = lambda: 9.0
    autopilot.unpark_due()
    ok(get(off1).status == "draft" and get(off2).status == "draft", "the daily un-parking leaves niche-parked clips parked while the guard is on")
    set_setting("unpark_per_day", "")
    # more than 40 % off topic: report only, the owner is told
    with SessionLocal() as s:
        d3 = Destination(channel_id="UC-mixed", name="Mixed", token="x"); s.add(d3); s.commit(); MIXED = d3.id
        s.add(Channel(kind="topic", query="desi khana", name="Food 2", mode="cc", auto=True, destination_id=MIXED, niche="food")); s.commit()
    mixed = [clip(project(t), dest=MIXED, status="queued") for t in ["Samosa Recipe", "Kaju Katli Sweet", "GTA 6 Review", "Funny Cats Compilation", "Car Crash Fails"]]
    r = niche_guard.apply()
    ok(r["refused"] and r["refused"][0]["dest_id"] == MIXED and r["refused"][0]["off"] == 3, f"3 of 5 (60%) off topic: refused ({r['refused']})")
    ok(all(get(i).status == "queued" for i in mixed), "nothing of that channel is parked (the queue is not emptied)")
    ok(len(told) == 1 and "Mixed" in told[0], "the owner is told (phone / Telegram / mail path)")
    niche_guard.apply()
    ok(len(told) == 1, "told once a day, not every round")
    # new videos
    with SessionLocal() as s:
        why = niche_guard.off_topic_video(s, FOODY, {"title": "GTA 6 Trailer Breakdown", "tags": ["gta"], "channel_url": "x"}, "cc")
        fine = niche_guard.off_topic_video(s, FOODY, {"title": "Street Food of Kolkata", "tags": [], "channel_url": "x"}, "cc")
        none = niche_guard.off_topic_video(s, SECOND, {"title": "Cooking show", "tags": [], "channel_url": "x"}, "cc")
    ok(why and not fine and not none, "a new off-topic video is skipped before it becomes a clip; on-topic / no-niche ones go on")
    # Super Admin: niche words for a channel without a niche; switching the guard off
    c.post("/api/admin/niche-guard", json={"dest_id": SECOND, "words": "gaming games gta"})
    row2 = next(x for x in c.get("/api/admin/niche-guard").json()["channels"] if x["dest_id"] == SECOND)
    ok(row2["niche"] == "gaming games gta" and [o["source"] for o in row2["off"]] == ["Funny Cats"], "niche words set by the Super Admin count")
    hooks.set_off("niche.guard", True)
    ok(niche_guard.apply().get("off") and get(no_niche[1]).status == "queued", "switch off: the guard parks nothing")
    ok(c.get("/api/settings/growth").json()["on"]["niche.guard"] is False, "the My channels card sees the switch off")
    hooks.set_off("niche.guard", False)

    # ======================================================== switches in the catalogue
    for k in ("titles.hook", "titles.retitle", "niche.guard", "clips.short"):
        ok(acl.DEFAULT.get(k) is True, f"switch {k} exists, on by default ({acl.LABEL.get(k)})")
    html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
    ok(all(f"'{k}':" in html for k in ("titles.hook", "titles.retitle", "niche.guard", "clips.short")), "the page hides each one when switched off (PERM_HIDE)")
    used = {r.replace("inside:", "") for _m, _p, r, _t in acl.ROUTES}
    ok(all(k in used for k in ("titles.hook", "titles.retitle", "niche.guard", "clips.short")), "each switch guards an address")
    ok(c.post("/api/admin/growth-fixes", json={"key": "clips.short", "on": False}).json()["off"] == ["clips.short"]
       and not hooks.on("clips.short"), "the Super Admin can switch a fix off in the owner's own copy")
    c.post("/api/admin/growth-fixes", json={"key": "clips.short", "on": True})
    ok(c.get("/api/settings/short-clips").json()["new_len"] == 20, "short-clips status")

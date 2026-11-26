import os, sys, tempfile, json
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "kw-test-pass-77"
sys.path.insert(0, r"D:\integ-final")
from app import tenant, keywords as K, metadata as M
from app.db import Base, engine, SessionLocal, Clip, Project, Setting, get_setting, set_setting, migrate, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
Base.metadata.create_all(engine); migrate()

# ---------- a fake YouTube suggestion service ----------
CALLS = []
NET = {"up": True}
ANSWERS = {
    "butter chicken recipe": ["butter chicken recipe", "butter chicken recipe in hindi", "butter chicken recipe restaurant style",
                              "butter chicken recipes", "butter chicken recipe ranveer brar", "butter chicken recipe kunal kapoor",
                              "butter chicken recipe at home", "butter chicken recipe full movie", "butter chicken recipe download",
                              "recipe butter chicken"],
    "butter chicken": ["butter chicken", "butter chicken recipe", "butter chicken masala", "butter chicken song"],
    "murgh makhani": ["murgh makhani", "murgh makhani recipe", "murgh makhani restaurant style"],
    "chicken curry": ["chicken curry", "chicken curry recipe", "chicken curry in tamil"],
}


def fake(q, hl, gl):
    CALLS.append((q, hl, gl))
    if not NET["up"]:
        raise OSError("network is down")
    if hl == "hi":
        return ["butter chicken recipe in hindi", "butter chicken recipe", "बटर चिकन रेसिपी"] if q.startswith("butter chicken recipe") else []
    return list(ANSWERS.get(q, []))


K._get = fake


def reset():
    CALLS.clear(); NET["up"] = True; K._down_until = 0.0


# ---------- words ----------
ok(K.tokens("बटर चिकन की रेसिपी कैसे बनाएं") == ["बटर", "चिकन", "की", "रेसिपी", "कैसे", "बनाएं"], "Hindi words stay whole (vowel signs are not cut off)")
ok(K.tokens("Kabita's Kitchen: GTA-6!") == ["kabita", "kitchen", "gta", "6"], "English words, possessive and punctuation")
ok(K.dupkey("Indian street foods") == K.dupkey("street food indian") and K.dupkey("street food") != K.dupkey("street food tour"), "same words / plural / order = one keyword")
ok(K.hashtag("बटर चिकन रेसिपी") == "बटरचिकनरेसिपी" and K.hashtag("butter chicken recipe") == "butterchickenrecipe" and K.hashtag("2026") is None, "hashtags keep Hindi letters whole")

# ---------- ranking + de-duplication (research tool) ----------
reset()
r = K.research("butter chicken recipe")
kws = r["keywords"]
ok(kws[0] == "butter chicken recipe" and kws[1] == "butter chicken recipe in hindi", f"earliest and most repeated first: {kws[:3]}")
ok("butter chicken recipes" not in kws and "recipe butter chicken" not in kws, "plural and re-ordered duplicates dropped")
ok(not any("full movie" in k or "download" in k for k in kws), "risky / spam phrases dropped")
ok("butter chicken recipe in hindi" in r["groups"]["hindi"] and "बटर चिकन रेसिपी" in r["groups"]["hindi"], "Hindi / Hinglish group")
ok(all(len(K.tokens(k)) >= 4 for k in r["groups"]["long"]) and r["groups"]["long"], "long phrases group")
ok(r["net"] == "ok" and not any(isinstance(x, (int, float)) for x in kws) and "score" not in json.dumps(r), "no numbers presented as search volume")
n1 = len(CALLS)
ok(0 < n1 <= K.BUDGET["full"], f"a full research stays inside its request budget ({n1})")
ok(all(c[2] == "IN" for c in CALLS) and any(c[1] == "hi" for c in CALLS), "asked for India, in English and Hindi")
r2 = K.research("butter chicken recipe")
ok(len(CALLS) == n1 and r2["keywords"] == kws, "second time: answered from the cache, no network call")
ok(K.suggest("butter chicken recipe", 5) == kws[:5], "old suggest() still works")

# ---------- keywords for one video ----------
reset()
TAGS = ["butter chicken", "murgh makhani", "chicken curry", "Ranveer Brar", "viral", "shorts", "restaurant style", "easy dinner",
        "full movie hd", "Butter Chicken", "butter chickens", "indian food"]
DESC = "Today we cook.\n#ButterChicken #IndianFood #dinner_ideas\nFollow me"
set_setting("competitors", json.dumps({"channels": [{"url": "u", "name": "Kunal Kapoor"}], "outliers": [
    {"title": "Butter chicken masala in 10 minutes", "channel": "Ranveer Brar"}], "checked_at": None}))
set_setting("trending:26", json.dumps({"at": utcnow().isoformat(), "items": [
    {"title": "Dhaba style butter chicken", "channel": "Some Chef", "tags": ["dhaba style butter chicken", "minecraft", "butter chicken street style"]}]}))
p = K.plan("Butter Chicken Recipe | Restaurant Style Murgh Makhani at Home", TAGS, DESC, channel="Cook With Asha")
names = [i["k"] for i in p["items"]]
low = [n.lower() for n in names]
ok(p["lead"] == "butter chicken recipe", f"lead phrase = the most typed one about this video: {p['lead']}")
ok(set(low[:2]) == {"butter chicken", "butter chicken recipe"} and "typed" in p["items"][0]["src"] and low.index("easy dinner") > low.index("murgh makhani"),
   f"strongest first (typed + the creator's first tags), loose tags last: {names[:4]}")
ok(all(t in low for t in ["murgh makhani", "chicken curry", "restaurant style", "easy dinner", "indian food", "dinner ideas"]), "every usable tag of the source video is used, hashtags of its description too")
ok(not any(x in low for x in ["viral", "shorts", "full movie hd"]), "junk and risky tags dropped")
stuffed = K.plan("GTA 6 Trailer 2 Breakdown", ["gta 6", "gta 6 trailer 2", "gta vi", "rockstar games", "gta 6 gameplay", "gta 6 release date",
                                               "gta 6 map leak", "minecraft", "fortnite battle royale", "rockstar news"], "")
sl = [i["k"].lower() for i in stuffed["items"]]
ok("minecraft" not in sl and "fortnite battle royale" not in sl and "rockstar games" in sl and "gta 6 map leak" in sl and "rockstar news" in sl,
   f"tags far down the list that have nothing to do with the video are left out: {sl}")
ok(low.count("butter chicken") == 1 and "butter chickens" not in low, "duplicate tags dropped")
ok(not any("ranveer" in n or "kunal" in n for n in low), f"other creators' names dropped (typed and tags)")
ok("butter chicken recipe at home" in low and "butter chicken recipe restaurant style" in low, "typed phrases made of the video's own words are kept")
ok("chicken curry in tamil" not in low and "butter chicken song" not in low, "phrases with words the video does not have are dropped")
both = next(i for i in p["items"] if i["k"].lower() == "butter chicken")
ok(set(both["src"]) >= {"typed", "video"}, f"a phrase from two sources shows both: {both['src']}")
ok(any(i["k"] == "dhaba style butter chicken" and "trending" in i["src"] for i in p["items"]) and "minecraft" not in low, "trending tags about this video added, others not")
ok(both["hot"] and not next(i for i in p["items"] if i["k"].lower() == "easy dinner")["hot"], "a phrase that is on today's saved lists is marked trending, others are not")
ok("butter chicken masala" not in low, "a typed phrase with a word the video does not have (masala) is left out")
ok(0 < len(CALLS) <= K.BUDGET["clip"], f"a clip costs few requests ({len(CALLS)})")
ok(len(p["hashtags"]) >= 3 and all(" " not in h for h in p["hashtags"]), f"hashtags from the strongest phrases: {p['hashtags']}")
h = K.plan("बटर चिकन रेसिपी | घर पर बनाएं", ["बटर चिकन", "चिकन रेसिपी"], "")
ok("बटर चिकन" in [i["k"] for i in h["items"]] and all(len(i["k"]) > 2 for i in h["items"]), "a Hindi title gives whole Hindi keywords")

# ---------- title / description / tags ----------
with SessionLocal() as s:
    pr = Project(url="u", video_id="abcdefghijk", title="Butter Chicken Recipe | Restaurant Style Murgh Makhani at Home", channel="Cook With Asha",
                 channel_url="https://www.youtube.com/channel/UCasha", mode="cc", tags=TAGS, description=DESC, license="Creative Commons")
    s.add(pr); s.commit()
    c = Clip(project_id=pr.id, start=12, end=42, aspect="9:16", status="rendered"); s.add(c); s.commit()
    pid, cid = pr.id, c.id
reset()
m = M.auto_metadata(cid)
with SessionLocal() as s:
    c = s.get(Clip, cid); pr = s.get(Project, pid)
    old = M.build(pr, c, [])
ok(m["title"] == old["title"] == "Butter Chicken Recipe | Restaurant Style Murgh Makhani at Home | Best Moment", "title already leads with the phrase: left as it was")
lines = m["description"].split("\n")
ok(lines[0] == "Butter Chicken Recipe — the best moment from this video by Cook With Asha." and lines[1].startswith("This video is about ")
   and "Murgh Makhani" in lines[1] and lines[1].lower().count("butter chicken") <= 1, f"first two lines carry the top phrases, without repeating: {lines[:2]}")
block = old["description"][old["description"].index("Original video:"):]
ok(m["description"].endswith(block) and "License: Creative Commons Attribution (CC BY)" in m["description"] and "Source: https://youtu.be/abcdefghijk?t=12" in m["description"],
   "credit, source, licence note and original description are exactly the old block")
tagline = next(l for l in lines if l.startswith("#"))
ok(tagline.startswith("#shorts ") and 3 <= len(tagline.split()) <= 5, f"3-5 hashtags, #shorts first: {tagline}")
ok(M.tag_chars(["a b", "c"]) == 7 and M.tag_chars(m["tags"]) <= 500 and len(m["title"]) <= 100, f"tags counted YouTube's way: {M.tag_chars(m['tags'])} of 500")
ok(set(m["tags"][:2]) == {"butter chicken", "butter chicken recipe"} and m["tags"][-2:] == ["Cook With Asha", "shorts"], f"strongest tags first, credit and 'shorts' last: {m['tags'][:3]}")
ok(c.kw["lead"] == "butter chicken recipe" and c.kw["at"] and c.kw["note"].startswith("Chosen by the system") and any(i["used"] for i in c.kw["items"])
   and c.meta_edited is False, f"the clip remembers keywords, sources and time: {c.kw['note']}")

# the phrase is not in the first 60 characters -> it leads the title
with SessionLocal() as s:
    p2 = Project(url="u", video_id="zzzzzzzzzzz", title="What my grandmother cooked for the whole family every single Sunday | BUTTER CHICKEN recipe", channel="Cook With Asha",
                 channel_url="", mode="permission", tags=["butter chicken recipe", "murgh makhani"], description="x")
    s.add(p2); s.commit()
    c2 = Clip(project_id=p2.id, start=0, end=30, aspect="16:9", status="rendered"); s.add(c2); s.commit(); cid2 = c2.id
m2 = M.auto_metadata(cid2)
ok(m2["title"] == "BUTTER CHICKEN recipe | What my grandmother cooked for the whole family every single Sunday",
   f"the phrase stood after the first 60 characters -> it leads now, said once, in the title's own spelling: {m2['title']}")
with SessionLocal() as s:
    p4 = Project(url="u", video_id="wwwwwwwwwww", title="Restaurant Style Murgh Makhani at Home", channel="Cook With Asha", channel_url="", mode="cc",
                 tags=["butter chicken recipe", "murgh makhani"], description="x")
    s.add(p4); s.commit()
    c4 = Clip(project_id=p4.id, start=0, end=30, aspect="9:16", status="rendered"); s.add(c4); s.commit()
    m4 = M.auto_metadata(c4.id)
ok(m4["title"] == "Restaurant Style Murgh Makhani at Home | Best Moment" and m4["kw"]["lead"].startswith("murgh makhani"),
   f"a title that already says what people type is not stuffed with a second phrase (lead: {m4['kw']['lead']})")
ok("butter chicken recipe" in m4["tags"], "…the creator's other keyword still goes into the tags")
ok("Used with the creator's permission." in m2["description"] and "#shorts" not in m2["description"] and "shorts" not in m2["tags"], "permission note kept; no #shorts on a wide video")

# limits with a huge source
big_tags = [f"butter chicken idea number {i} for dinner" for i in range(80)]
big_desc = " ".join(f"#tag{i}" for i in range(40)) + " end"
with SessionLocal() as s:
    p3 = Project(url="u", video_id="yyyyyyyyyyy", title="Butter Chicken Recipe " + "very long title " * 8, channel="C", channel_url="", mode="cc", tags=big_tags, description=big_desc)
    s.add(p3); s.commit()
    c3 = Clip(project_id=p3.id, start=0, end=30, aspect="9:16", status="rendered"); s.add(c3); s.commit(); cid3 = c3.id
m3 = M.auto_metadata(cid3)
import re
nh = len(re.findall(r"#\w", m3["title"] + " " + m3["description"]))
ok(M.tag_chars(m3["tags"]) <= 500 and len(m3["tags"]) <= 30 and m3["tags"][-1] == "shorts", f"80 long tags -> {len(m3['tags'])} tags, {M.tag_chars(m3['tags'])} characters")
ok(len(m3["title"]) <= 100 and nh <= 15, f"title {len(m3['title'])} characters, {nh} hashtags (never more than 15)")
ok("Credit: C - " in m3["description"] and "--- Original description ---" in m3["description"], "credit lines intact when the copied description is cut for the hashtag limit")

# ---------- network failure ----------
reset(); NET["up"] = False
before = len(CALLS)
try:
    with tenant.use(7):
        Base.metadata.create_all  # (tenant 7 is created on first use)
        f = K.plan("Paneer Tikka Recipe at Home", ["paneer tikka", "tandoori paneer"], "")
    ok(f["net"] == "offline" and [i["k"] for i in f["items"]] == ["paneer tikka", "tandoori paneer"] and f["lead"] is None, "network down: no exception, the video's own tags are used")
except Exception as e:  # noqa: BLE001
    ok(False, f"network down raised {e!r}")
ok(len(CALLS) - before == 2, f"after two failures nothing more is asked ({len(CALLS) - before} tries)")
with tenant.use(7):
    K.plan("Paneer Tikka Recipe at Home", ["paneer tikka"], "")
ok(len(CALLS) - before == 2, "and nothing at all for the next minutes")
reset(); NET["up"] = False
old_at = (utcnow() - timedelta(hours=30)).isoformat()
set_setting(K._ckey("dal makhani", "en"), json.dumps({"at": old_at, "q": "dal makhani", "hl": "en", "items": ["dal makhani recipe"]}))
st = K.research("dal makhani", "clip")
ok(st["net"] == "stale" and st["keywords"] == ["dal makhani recipe"], "network down with an old saved answer: the saved answer is used")
ok(K.explain([], "offline").startswith("YouTube's search suggestions could not be reached"), "plain message when YouTube cannot be reached")

# ---------- one cache per customer ----------
reset()
K.research("murgh makhani", "clip")
a = len(CALLS)
with tenant.use(2):
    ok(get_setting(K._ckey("murgh makhani", "en")) is None, "customer 2 does not see the owner's saved research")
    K.research("murgh makhani", "clip")
    ok(len(CALLS) > a and get_setting(K._ckey("murgh makhani", "en")), "customer 2 asks for itself and saves its own copy")
    b = len(CALLS)
    K.research("murgh makhani", "clip")
    ok(len(CALLS) == b, "customer 2's second research costs nothing")
with SessionLocal() as s:
    s.add(Setting(key="kwc:old", value=json.dumps({"at": (utcnow() - timedelta(days=9)).isoformat(), "items": []}))); s.commit()
n = K.purge_cache()
ok(n == 1 and get_setting("kwc:old") is None and get_setting(K._ckey("murgh makhani", "en")), "saved research older than a week is deleted, newer kept")

# ---------- hand edits + the freshness pass ----------
from fastapi.testclient import TestClient
from app import main
main.run_render = lambda cid: None
with TestClient(main.app) as tc:
    tc.post("/api/login", json={"username": "admin", "password": "kw-test-pass-77"})
    g = tc.get(f"/api/clips/{cid}").json()
    ok(g["kw"]["items"] and g["meta_edited"] is False and "kw" not in tc.get("/api/clips").json()[0], "editor gets the keywords; the clip list stays small")
    r = tc.put(f"/api/clips/{cid}", json={"title": g["title"], "description": g["description"].replace("\n", "\r\n"), "tags": g["tags"], "orig_volume": 0.5})
    ok(r.status_code == 200 and not r.json()["meta_edited"], "saving the editor without changing the words does not count as a hand edit")
    reset()
    pv = tc.post(f"/api/keywords/clip/{cid}", json={"topic": ""})
    ok(pv.status_code == 200 and pv.json()["kw"]["items"] and tc.get(f"/api/clips/{cid}").json()["title"] == g["title"], "Find better keywords shows a proposal and saves nothing")
    r = tc.put(f"/api/clips/{cid2}", json={"title": "My own title for this clip"})
    ok(r.json()["meta_edited"] is True, "changing the title in the editor marks the clip as edited by hand")
    kr = tc.get("/api/keywords", params={"q": "butter chicken recipe"})
    ok(kr.status_code == 200 and kr.json()["groups"]["top"][0] == "butter chicken recipe" and "note" in kr.json(), "research endpoint returns groups")
    ok(tc.get("/api/keywords", params={"q": " "}).status_code == 400, "empty topic -> a plain message")
    NET["up"] = False; K._down_until = 0.0
    e = tc.get("/api/keywords", params={"q": "something never asked before"})
    ok(e.status_code == 502 and "could not be reached" in e.json()["detail"], f"YouTube unreachable -> plain message: {e.json()['detail'][:60]}")
    reset()

    # make both clips old and queued, due soon
    with SessionLocal() as s:
        for i in (cid, cid2):
            x = s.get(Clip, i)
            x.kw = {**x.kw, "at": (utcnow() - timedelta(hours=30)).isoformat() + "Z"}
            x.status, x.publish_at, x.output_path = "queued", utcnow() + timedelta(hours=3), "x.mp4"
        s.get(Clip, cid).title = "stale title written by the system"
        far = Clip(project_id=pid, start=0, end=20, aspect="9:16", status="queued", title="far away", description="d", tags=[],
                   publish_at=utcnow() + timedelta(days=9), kw={"at": (utcnow() - timedelta(hours=30)).isoformat() + "Z", "items": []})
        legacy = Clip(project_id=pid, start=0, end=20, aspect="9:16", status="queued", title="made before the keyword helper", description="d", tags=["x"],
                      publish_at=utcnow() + timedelta(hours=3))
        s.add_all([far, legacy]); s.commit(); far_id, legacy_id = far.id, legacy.id
    ANSWERS["butter chicken recipe"] = ["butter chicken recipe", "butter chicken recipe new murgh makhani style"] + ANSWERS["butter chicken recipe"][1:]
    with SessionLocal() as s:  # the day-old cache must not hide the new answer
        for row in s.query(Setting).filter(Setting.key.like("kwc:%")).all():
            row.value = json.dumps({**json.loads(row.value), "at": (utcnow() - timedelta(hours=30)).isoformat()})
        s.commit()
    done = M.freshen_due()
    with SessionLocal() as s:
        a, b, f, lg = s.get(Clip, cid), s.get(Clip, cid2), s.get(Clip, far_id), s.get(Clip, legacy_id)
        ok(done == 1 and a.title.startswith("Butter Chicken Recipe") and "butter chicken recipe new murgh makhani style" in [t.lower() for t in a.tags] and a.status == "queued",
           f"freshness pass rewrote the stale queued clip ({done}): {a.title[:40]}")
        ok(utcnow() - M.datetime.fromisoformat(a.kw["at"].rstrip("Z")) < timedelta(minutes=5), "and stamped the new research time")
        ok(b.title == "My own title for this clip" and b.kw["at"] < a.kw["at"], "the hand-edited clip was left alone")
        ok(f.title == "far away", "a clip that goes up much later waits")
        ok(lg.title == "made before the keyword helper" and lg.tags == ["x"], "a clip from before the keyword helper (maybe hand-written) is never touched")
    ok(M.freshen_due() == 0, "second run: nothing to do")
    # YouTube unreachable -> the clip keeps what it has
    with SessionLocal() as s:
        x = s.get(Clip, cid); x.kw = {**x.kw, "at": (utcnow() - timedelta(hours=30)).isoformat() + "Z"}; keep = x.title
        for row in s.query(Setting).filter(Setting.key.like("kwc:%")).all():
            s.delete(row)
        s.commit()
    NET["up"] = False; K._down_until = 0.0
    ok(M.freshen_due() == 0 and SessionLocal().get(Clip, cid).title == keep, "freshness pass with YouTube unreachable: nothing is overwritten, no exception")
    reset()
    # an uploading clip is never rewritten
    with SessionLocal() as s:
        x = s.get(Clip, cid); x.status = "uploading"; s.commit()
    ok(M.freshen_clip(cid, force=True) is False, "a clip that is being uploaded is not touched")
    with SessionLocal() as s:
        x = s.get(Clip, cid); x.status = "rendered"; s.commit()
    # approving a clip refreshes stale keywords first; 'Use these' takes a hand-edited clip back
    u = tc.post(f"/api/clips/{cid2}/metadata", json={"topic": "murgh makhani"})
    j = tc.get(f"/api/clips/{cid2}").json()
    ok(u.status_code == 200 and j["meta_edited"] is False and j["kw"]["hint"] == "murgh makhani" and j["title"] != "My own title for this clip",
       f"'Use these' writes everything again and remembers the owner's topic: {j['title'][:50]}")
    ok(any(i["k"].startswith("murgh makhani") and "typed" in i["src"] for i in j["kw"]["items"]), "the owner's topic words were researched too")
    with SessionLocal() as s:
        x = s.get(Clip, cid2); x.status, x.youtube_id = "uploaded", "YT123"; s.commit()
    ok(tc.post(f"/api/clips/{cid2}/metadata", json={}).status_code == 409 and tc.post(f"/api/keywords/clip/{cid2}", json={}).status_code == 409,
       "an uploaded video: a plain message instead of a silent change")
    ok(tc.post("/api/keywords/clip/99999", json={}).status_code == 404, "unknown clip -> 404")

# ---------- the timed job is registered, the guide explains it ----------
src = open(r"D:\integ-final\app\scheduler.py", encoding="utf-8").read()
i = src.index("add(freshen_due")
ok(i < src.index("sch.start()"), "freshness job registered before sch.start()")
from app import guide
qs = [h[0] for h in guide.HELP]
ok("How are keywords chosen?" in qs and qs.index("How are keywords chosen?") + 1 == next(i for i, q in enumerate(qs) if q.startswith("I want to upload my own video file")),
   "Setup guide explains how keywords are chosen (right before 'upload my own video file')")
html = open(r"D:\integ-final\static\index.html", encoding="utf-8").read()
ok("\x08" not in html and "\x08" not in open(K.__file__, encoding="utf-8").read() and "\x08" not in open(M.__file__, encoding="utf-8").read(), "no stray backspace bytes")
ok("search volume" not in html.lower() and "most typed first" in html, "dashboard says 'most typed first', never a search volume")

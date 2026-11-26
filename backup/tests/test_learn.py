"""Ideas -> What works: performance model, card, auto-tune (bounded, undo, limits, switch, per account), topics."""
import os, sys, tempfile
from datetime import datetime, timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
os.environ["KEYWORDS_OFFLINE"] = "1"
WT = r"D:\integ-final"
sys.path.insert(0, WT)
from fastapi.testclient import TestClient
from app import main, acl, tenant, auth, telegram, jobs, learn, keywords
from app.db import SessionLocal, Destination, Channel, Clip, Project, VideoStat, set_setting, get_setting, utcnow
assert learn.__file__.startswith(WT), learn.__file__
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
main.executor.submit = lambda fn, *a, **k: None

# ---------------------------------------------------------------- 1. the pure model
NOW = datetime(2026, 11, 22, 12, 0)
def st(i, vpd, days=5, dest=1, likes=None, hour_utc=12):
    pub = (NOW - timedelta(days=days)).replace(hour=hour_utc, minute=30)
    age = (NOW - pub).total_seconds() / 86400
    return {"video_id": f"v{i}", "dest": dest, "views": int(vpd * age), "likes": likes if likes is not None else int(vpd * age * 0.05), "published_at": pub}
stats, clips = [], []
def add(src, name, vpd, n, words, length=30, layout="full", hour_utc=12, start=0):
    for k in range(n):
        i = len(stats)
        stats.append(st(i, vpd, hour_utc=hour_utc))
        clips.append({"youtube_id": f"v{i}", "source_id": src, "source": name, "words": set(words), "length": length, "tz": None, "layout": layout})
add(1, "Kabita", 300, 6, {"biryani", "food"}, length=30, hour_utc=12)
add(2, "Ranveer", 20, 6, {"samosa", "food"}, length=50, layout="fill", hour_utc=3)
add(3, "Chai Guy", 100, 6, {"chai", "food"}, length=50, hour_utc=6)
add(4, "Tiny", 500, 3, {"rare"}, length=10)
for j in range(2):
    stats.append(st(len(stats), 100))  # channel videos not made by the app
stats.append(st(len(stats), 99999, days=1))  # a day old: not counted yet
clips.append({"youtube_id": f"v{len(stats) - 1}", "source_id": 1, "source": "Kabita", "words": {"biryani"}, "length": 30, "tz": None, "layout": "full"})
m = learn.model(stats, clips, NOW)
src = {e["key"]: e for e in m["sources"]}
ok(m["enough"] and m["videos"] == 21 and abs(m["usual"] - 100) < 1, f"model: 21 clips judged (the 1-day-old one is left out), channel usual = median 100/day ({m['videos']}, {m['usual']})")
ok(src[1]["verdict"] == "win" and src[1]["ratio"] == 3.0 and src[1]["n"] == 6, "a source 3× the usual is a winner")
ok(src[2]["verdict"] == "weak" and src[2]["ratio"] == 0.2 and src[3]["verdict"] == "usual", "a source at 0.2× is weak, 1× is usual")
ok(4 not in src, "a source with only 3 videos is not judged (minimum 5)")
words = {e["key"]: e for e in m["words"]}
ok(words.get("biryani", {}).get("verdict") == "win" and words.get("samosa", {}).get("verdict") == "weak" and "food" not in words and "rare" not in words,
   "words: winner/weak found; a word in nearly every title and a word with 3 videos say nothing")
ok(m["lengths"][0]["label"] == "20–40 s" and m["lengths"][0]["verdict"] == "win", "best clip length bucket")
ok(m["hours"][0]["label"] == "18:00" and m["hours"][0]["ratio"] == 3.0, "best upload hour in local (India) time: 12:30 UTC -> 18:00")
ok({e["key"] for e in m["layouts"]} == {"full", "fill"} and m["layouts"][0]["key"] == "full", "layouts compared")
ok(abs(src[1]["like_rate"] - 5.0) < 0.1, "like rate of the group shown")
few = learn.model(stats[:4], clips[:4], NOW)
ok(not few["enough"] and few["sources"] == [] and few["usual"] is None, "a channel with few videos: graceful 'not enough' (no channel median)")
ok(few["why"].startswith("your channel has 4 public videos older than 2 days") and "at least 5" in few["why"], f"not enough: the real reason with the real numbers ({few['why']})")
two = learn.model(stats[:2], clips[:2], NOW)
ok("has 2 public videos" in two["why"], "2 old videos -> 'your channel has 2 public videos…', not '0 of 5 clips'")
ok("3 of your uploaded clips are older" in learn.model(stats, clips[:3], NOW)["why"], "channel big enough but few clips judged: says so")
ok(learn.model([], [], NOW)["enough"] is False, "no stats at all: graceful")
ok(all(e["ratio"] == round(e["ratio"], 1) for dim in ("sources", "words", "lengths", "hours", "layouts") for e in m[dim]), "ratios have one decimal at most")
utc = learn.model(stats, [{**c, "tz": 0} for c in clips], NOW)
ok(utc["hours"][0]["label"] == "12:00", "a source in UTC (offset 0) keeps UTC hours, no fallback to India time")
other = learn.model([{**x, "dest": 2} for x in stats[:3]] + stats[3:], clips, NOW)
ok(other["enough"], "a second channel with too few videos is skipped, the rest still judged")
ok(learn.words_of("Chicken Biryani | Best Moment #shorts", ["Kabitas Kitchen", "biryanis"], [{"k": "biryani recipe", "used": True}], "Kabitas Kitchen")
   == {"chicken", "biryani", "recipe"}, "words of a clip: plural folded, filler and the credited channel's name left out")

# ---------------------------------------------------------------- 2. the app: card, auto-tune, undo, switch
def seed(prefix):
    """Destination + sources A (win) B (weak) C (usual) D (win, but stopped) with uploaded clips and their numbers."""
    now = utcnow()
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-" + prefix, name=prefix + " channel", token="x")); s.commit()
        did = s.query(Destination).first().id
        ids = {}
        for key, name, vpr, auto in (("A", "Kabita", 3, True), ("B", "Ranveer", 2, True), ("C", "Chai Guy", 2, True), ("D", "Stopped one", 2, False)):
            ch = Channel(kind="channel", query=name, name=name, videos_per_run=vpr, clips_per_video=2, auto=auto, destination_id=did, mode="cc")
            s.add(ch); s.commit(); ids[key] = ch.id
        n = 0
        for key, vpd, word, cnt in (("A", 300, "biryani", 6), ("B", 20, "samosa", 6), ("C", 100, "chai", 6), ("D", 300, "biryani", 6), (None, 100, "", 2)):
            for k in range(cnt):
                n += 1
                vid = f"{prefix}{n:09d}"[:11]
                pub = now - timedelta(days=5)
                if key:
                    p = Project(url="u", video_id="src" + vid, title="t", channel="Some Creator", mode="cc", channel_ref=ids[key]); s.add(p); s.commit()
                    s.add(Clip(project_id=p.id, start=0, end=30, aspect="9:16", status="uploaded", youtube_id=vid, destination_id=did,
                               title=f"{word.title()} street food special {k}", tags=[word, "street food"], attempted_at=pub))
                s.add(VideoStat(video_id=vid, destination_id=did, title="x", published_at=pub, privacy="public", views=vpd * 5, likes=vpd // 4, updated_at=now))
        s.commit()
    return ids
vpr = lambda i: SessionLocal().get(Channel, i).videos_per_run
auto = lambda i: SessionLocal().get(Channel, i).auto

PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
with mk() as owner, mk() as c1, mk() as c2, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    card = owner.get("/api/insights").json()
    ok(card["enough"] is False and card["autotune"]["on"] is True and card["lines"] == [] and card["topics"] == [], "empty account: card says 'not enough yet', auto-tune on by default")
    ok(card["why"].startswith("your channel has 0 public videos"), "empty account: the card says why")
    ok(owner.get("/api/settings/face-size").json()["has_face"] is False, "no face photo: the dashboard is told so (no 404 probe of /api/faces/0)")
    html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
    ok("fetch('/api/faces/0'" not in html and 'id="lg-acc" style="width:auto;max-width:100%"' in html and '<div style="overflow-x:auto;max-width:100%"><table' in html,
       "phone width: tables scroll inside their box, the activity-log filter fits; no face request without a face")
    ok(learn.daily(force=True) == [], "empty account: auto-tune changes nothing")
    O = seed("ow")
    card = owner.get("/api/insights").json()
    ok(card["enough"] and any("“Kabita” get 3× your usual views" in l for l in card["lines"]) and not any("more views than" in l for l in card["lines"]) and any("Ranveer" in l and "only 0.2×" in l for l in card["lines"]),
       f"card: plain sentences with the numbers ({card['lines'][:2]})")
    ok(any("biryani" in l for l in card["lines"]) and card["words"][0]["key"] == "biryani", "card: winning words")
    ok([t["topic"] for t in card["topics"]] == ["biryani"], f"topic ideas before anything is saved: the winning word itself ({card['topics']})")
    import json as _j
    keywords._cache_put("biryani", "en", ["biryani recipe", "chicken biryani", "biryani song download", "samosa"])
    set_setting("trending:26", _j.dumps({"items": [{"title": "Hyderabadi biryani in the old city", "tags": ["hyderabadi biryani"], "channel": "Some Channel"}]}))
    tp = [t["topic"] for t in owner.get("/api/insights").json()["topics"]]
    ok("biryani recipe" in tp and "chicken biryani" in tp and "hyderabadi biryani" in tp and not any("download" in t for t in tp)
       and "samosa" not in tp and 5 <= len(tp) + 3 and len(tp) <= 10, f"topic ideas: what people search + trending, risky phrases left out ({tp})")
    ok('data-wwsrc' in open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read(), "each idea has 'Find sources for this' (opens Add source with the topic)")
    # auto-tune, day 1
    done = learn.daily(force=True)
    ok(vpr(O["A"]) == 4 and vpr(O["B"]) == 1 and vpr(O["C"]) == 2, f"day 1: winner +1 (3→4), weak −1 (2→1), usual unchanged ({done})")
    ok(vpr(O["D"]) == 2 and auto(O["D"]) is False and all(auto(O[k]) for k in "ABC"), "a stopped source is not changed and nothing is paused or started")
    ok(len(done) == 2 and "because its clips get 3× your usual views per day (6 videos)" in done[0], "every change says why")
    card = owner.get("/api/insights").json()
    ok(len(card["autotune"]["log"]) == 2 and {c["source"] for c in card["autotune"]["changed"]} == {"Kabita", "Ranveer"}
       and any("making more of them" in l for l in card["lines"]), "card shows what auto-tune did and what is changed now")
    ok(learn.daily() == [], "the hourly job acts only once a day")
    ok(get_setting(learn.K_WORDS) and "biryani" in learn.win_words(), "winning words handed to the keyword helper")
    for _ in range(6):
        learn.daily(force=True)
    ok(vpr(O["A"]) == 6 and vpr(O["B"]) == 1, f"bounded: winner stops at 2× its own setting (6), weak never below 1 ({vpr(O['A'])}, {vpr(O['B'])})")
    # undo
    r = owner.post("/api/insights/undo").json()
    ok(r["restored"] == 2 and vpr(O["A"]) == 3 and vpr(O["B"]) == 2 and r["card"]["autotune"]["on"] is False and r["card"]["autotune"]["changed"] == [],
       "Undo: every source back to its own setting, auto-tune off")
    ok(learn.daily(force=True) == [] and vpr(O["A"]) == 3 and learn.win_words() == [], "switch off: nothing is changed, no word boost")
    r = owner.post("/api/insights/autotune", json={"on": True}).json()
    ok(r["on"] is True and r["card"]["autotune"]["on"] is True, "switch on again")
    learn.daily(force=True)
    ok(vpr(O["A"]) == 4, "on again: acts")
    # the owner changes a tuned source by hand: his number is the new own setting, undo leaves it alone
    owner.put(f"/api/channels/{O['A']}", json={"videos_per_run": 7})
    learn.daily(force=True)
    ok(vpr(O["A"]) == 8 and learn._load(learn.K_ORIG, {})[str(O["A"])]["orig"] == 7, "a number changed by hand becomes the source's own setting")
    owner.put(f"/api/channels/{O['A']}", json={"videos_per_run": 5})
    owner.post("/api/insights/undo")
    ok(vpr(O["A"]) == 5 and vpr(O["B"]) == 2, "Undo does not overwrite a number changed by hand since")
    owner.post("/api/insights/autotune", json={"on": True})
    # numbers not read from YouTube for days: wait
    with SessionLocal() as s:
        for v in s.query(VideoStat).all():
            v.updated_at = utcnow() - timedelta(days=4)
        s.commit()
    ok(learn.daily(force=True) == [] and owner.get("/api/insights").json()["stale"] is True, "numbers more than 2 days old: auto-tune waits")
    with SessionLocal() as s:
        for v in s.query(VideoStat).all():
            v.updated_at = utcnow()
        s.commit()
    # back to usual -> one step towards its own setting
    learn.daily(force=True)
    before = vpr(O["B"])
    with SessionLocal() as s:
        for v in s.query(VideoStat).filter(VideoStat.views == 100).all():
            v.views = 500  # samosa clips recover to the usual
        s.commit()
    learn.daily(force=True)
    ok(vpr(O["B"]) == before + 1 or vpr(O["B"]) == 2, f"a weak source back to usual moves one step back to its own setting ({before} -> {vpr(O['B'])})")

    # ---------------------------------------------------------------- 3. keyword helper boost
    title = "Chicken biryani and mutton curry street food"
    tags = ["mutton curry", "chicken biryani"] + [f"street food {w}" for w in ("delhi", "mumbai", "kolkata", "chennai", "pune", "jaipur", "indore", "agra")]
    set_setting(learn.K_ON, "off")
    first_off = keywords.plan(title, tags)["items"][0]["k"]
    set_setting(learn.K_ON, "on"); set_setting(learn.K_WORDS, '["biryani"]')
    first_on = keywords.plan(title, tags)["items"][0]["k"]
    ok(first_off == "mutton curry" and first_on == "chicken biryani", f"keyword helper: a winning word moves up among the video's own keywords ({first_off} -> {first_on})")
    set_setting(learn.K_WORDS, '["unrelatedword"]')
    ok({i["k"] for i in keywords.plan(title, tags)["items"]} == {i["k"] for i in keywords.plan(title, tags, )["items"]}
       and not any("unrelatedword" in i["k"] for i in keywords.plan(title, tags)["items"]), "a winning word never adds an off-topic keyword")

    # ---------------------------------------------------------------- 4. customers: limits, switches, isolation
    def register(email, name):
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email, "password": PW, "accept_terms": True})
        return next(u for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email)
    def customer(client, email, name):
        aid = register(email, name)["id"]
        tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        client.post("/api/login", json={"username": email, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
        return aid, tid
    a1, t1 = customer(c1, "asha@example.com", "Asha")
    a2, t2 = customer(c2, "ravi@example.com", "Ravi")
    ok(c1.get("/api/insights").json()["enough"] is False, "a new customer sees only their own (empty) numbers, not the owner's")
    with tenant.use(t1):
        S1 = seed("cu")
    with tenant.use(t2):
        S2 = seed("cv")
    owner.post(f"/api/admin/users/{a1}/access", json={"limits": {"clips_per_day": 30}})
    with tenant.use(t1):
        done = learn.daily(force=True)
        ok(vpr(S1["A"]) == 3 and vpr(S1["B"]) == 1, f"clips-a-day limit: the winner is NOT raised above the account's limit, the weak one still lowered ({done})")
        held = learn._load(learn.K_HELD, [])
        ok(held and "limit of 30 clips a day" in held[0], f"…and the card says why ({held})")
    ok(c1.get("/api/insights").json()["autotune"]["held"], "held reason shown on the customer's card")
    owner.post(f"/api/admin/users/{a2}/access", json={"perms": {"sources.edit": False}})
    with tenant.use(t2):
        learn.daily(force=True)
        ok(vpr(S2["A"]) == 3 and vpr(S2["B"]) == 2, "'Change sources' switched off by the Super Admin: auto-tune does not change that account's sources")
    ok(c2.post("/api/insights/autotune", json={"on": False}).status_code == 403 and c2.post("/api/insights/undo").status_code == 403,
       "…and the switch / undo are refused for that account")
    ok(c2.get("/api/insights").status_code == 200, "the card itself stays readable (Ideas page)")
    owner.post(f"/api/admin/users/{a2}/access", json={"perms": {"page.ideas": False}})
    ok(c2.get("/api/insights").status_code == 403, "Ideas page switched off: the card is refused")
    # isolation: switching off in one account changes nobody else
    ok(c1.post("/api/insights/autotune", json={"on": False}).status_code == 200, "a customer can switch their own auto-tune off")
    with tenant.use(t1):
        ok(learn.enabled() is False, "…in their copy")
    ok(learn.enabled() is True and owner.get("/api/insights").json()["autotune"]["on"] is True, "…the owner's auto-tune is still on")
    with tenant.use(t2):
        ok(learn.enabled() is True and learn._load(learn.K_LOG, []) == [], "…another customer has their own switch and an empty history")
    owner_log = learn._load(learn.K_LOG, [])
    ok(len(owner_log) >= 4 and not any("cu" in x["text"] for x in owner_log), "the owner's history holds only the owner's changes")
    with tenant.use(t1):
        r = learn.undo()
        ok(vpr(S1["B"]) == 2 and r["restored"] == 1, "customer undo restores their own source")
    acts = acl.activity(a1)
    ok(any(x["action"] == "Auto-tune" for x in acts), "auto-tune changes are written in the customer's activity log")
    # guide
    g = owner.get("/api/guide").json()
    ok("auto-tune" in str(g).lower(), "Setup guide explains auto-tune")
for f in ("app/learn.py", "app/keywords.py", "app/main.py", "app/acl.py", "app/scheduler.py", "app/guide.py", "static/index.html"):
    ok(b"\x08" not in open(os.path.join(WT, f), "rb").read(), f"no backspace byte in {f}")

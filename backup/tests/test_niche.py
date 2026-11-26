"""Pick your niche -> sources made for you: niche data, the Get started step, plan limits, typed topics, Remove,
the shared 24-hour search cache, per-account search turns, uploads first, auto-tune, routes."""
import json, os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-niche-pass-77"
os.environ["GOOGLE_CLIENT_ID"] = "999999999999-ownerownerownerownerowner.apps.googleusercontent.com"; os.environ["GOOGLE_CLIENT_SECRET"] = "OWNER-SECRET-000000000000"
os.environ["BASE_URL"] = "http://studio.example.com"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, acl, tenant, youtube, telegram, onboarding, quota, niches, niche_sources, search_cache, autopilot, learn
from app import keywords as kw
from app.db import SessionLocal, Destination, Channel, Worker, Project, Clip, VideoStat, Setting, get_setting, set_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
main.executor.submit = lambda *a, **k: None
onboarding.check_with_google = lambda cid, sec: ""
kw._get = lambda q, hl, gl: [f"{q} tips", f"{q} ideas", f"{q} at home"]  # no network: the keyword helper's one request
PW = "customer-niche-pass-1"

# ---------------------------------------------------------------- the data
ok(len(niches.NICHES) == 12 and len(niches.BY_KEY) == 12, "12 ready-made niches, each with its own key")
ok(all(4 <= len(n["phrases"]) <= 6 and all(p.strip() for p in n["phrases"]) and len(set(p.lower() for p in n["phrases"])) == len(n["phrases"])
       for n in niches.NICHES), "every niche has 4-6 different search phrases")
allp = [p for n in niches.NICHES for p in n["phrases"]]
ok(sum(1 for p in allp if niches.DEVANAGARI.search(p)) >= 6 and sum(1 for p in allp if "hindi" in p) >= 10
   and sum(1 for p in allp if not niches.DEVANAGARI.search(p) and "hindi" not in p) >= 20, "phrases mix Hindi and English for Indian viewers")
ok(all(n["trending"] in (None, *niche_sources.TRENDS) for n in niches.NICHES) and set(niche_sources.TRENDS) <= set(main.TREND_CATS),
   "trending categories are ones the trending source already knows (or none)")
ph = niches.phrases_for_topic("  home   gardening ")
ok(ph == ["home gardening", "home gardening shorts", "home gardening best moments", "home gardening hindi"], f"typed topic -> base, shorts, best moments, Hindi variant: {ph}")
ok(len(niches.phrases_for_topic("बागवानी")) == 3 and niches.phrases_for_topic("https://youtube.com/@x") == [] and niches.phrases_for_topic("") == [],
   "a Hindi topic has no extra Hindi variant; a link or nothing gives no phrases")
ok(niches.low_cc("bollywood songs") and niches.low_cc("नया गाना") and niches.low_cc("IPL cricket") and not niches.low_cc("street food"),
   "music / films / cricket topics are flagged as having almost no Creative Commons videos")


class FakeReq:
    def __init__(self, kw): self.kw = kw

    def execute(self):
        calls.append((tenant.current(), self.kw["q"], self.kw.get("pageToken")))
        page = 2 if self.kw.get("pageToken") == "P2" else 1
        q = self.kw["q"].replace(" ", "")[:5]
        return {"items": [{"id": {"videoId": f"{q}{page}{i:05d}"}, "snippet": {"title": f"{self.kw['q']} video {page}-{i}", "channelTitle": "x"}} for i in range(5)],
                "nextPageToken": "P2" if page == 1 else None}


class FakeYT:
    def search(self):
        class S:
            def list(self, **kw): return FakeReq(kw)
        return S()


calls = []
youtube.client = lambda *a, **k: FakeYT()


def customer(anon, owner, email, name):
    anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email, "password": PW, "accept_terms": True})
    aid = [u for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email][0]["id"]
    owner.post(f"/api/admin/users/{aid}/approve")
    c = TestClient(main.app)
    c.post("/api/login", json={"username": email, "password": PW})
    return c, aid, aid + 1


def ready_to_source(c, owner, aid, T):
    owner.post(f"/api/admin/users/{aid}/shared-google", json={"on": True})  # step 1 done: the owner's Google project
    with tenant.use(T):
        with SessionLocal() as s:
            s.add(Destination(channel_id=f"UC-{T}", name=f"Channel {T}", token="x")); s.add(Worker(name="pc", approved=True)); s.commit()
    c.post("/api/onboarding/ack", json={"key": "verify"}); c.post("/api/onboarding/ack", json={"key": "face"}); c.post("/api/onboarding/ack", json={"key": "gmail"})


def srcs(T, **f):
    with tenant.use(T):
        with SessionLocal() as s:
            q = s.query(Channel)
            for k, v in f.items():
                q = q.filter(getattr(Channel, k) == v)
            return q.order_by(Channel.id).all()


with TestClient(main.app) as owner, TestClient(main.app) as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-niche-pass-77"})
    # ------------------------------------------------------------ routes
    from fastapi.routing import APIRoute
    real = {(m, r.path) for r in main.app.routes if isinstance(r, APIRoute) for m in r.methods}
    rules = {(m, p): rule for m, p, rule, _t in acl.ROUTES}
    ok(all(x in real for x in [("GET", "/api/niches"), ("POST", "/api/niches"), ("POST", "/api/niches/remove")]), "the three niche addresses exist")
    ok(rules.get(("GET", "/api/niches")) == acl.SIGNED and rules.get(("POST", "/api/niches")) == "sources.add"
       and rules.get(("POST", "/api/niches/remove")) == "sources.edit", "…and each has its decision in acl.ROUTES (read: signed-in; pick: Add sources; remove: Change sources)")

    # ------------------------------------------------------------ Get started: "What is your channel about?"
    c, aid, T = customer(anon, owner, "asha@example.com", "Asha")
    c2, aid2, T2 = customer(anon, owner, "ravi@example.com", "Ravi")
    o = c.get("/api/onboarding").json()
    ok(o["steps"][-1]["key"] == "source" and o["steps"][-1]["title"] == "What is your channel about?" and len(o["steps"]) == 7,
       "the last Get started step is “What is your channel about?” (same 7 steps)")
    ok(c.post("/api/niches", json={"keys": ["food"]}).status_code == 409, "picking a niche before the earlier steps is refused")
    st = c.get("/api/niches").json()
    ok(len(st["catalog"]) == 12 and st["mine"] == [] and st["topic_on"] and "Creative Commons" in st["note"], "the picker lists the niches and says plainly where videos come from")
    ready_to_source(c, owner, aid, T)
    ok(c.get("/api/onboarding").json()["current"] == "source", "customer A reached the niche step")
    ok(c.post("/api/niches", json={"keys": []}).status_code == 400 and c.post("/api/niches", json={"topic": "https://youtu.be/abc"}).status_code == 400,
       "nothing picked / a link instead of words: refused with a reason")
    r = c.post("/api/niches", json={"keys": ["food", "devops", "nope"], "review_first": True, "tz_offset": -330})
    j = r.json()
    ok(r.status_code == 200 and len(j["made"]) == 12, f"food + devops: 5 + 5 topic sources and 2 trending sources made ({r.status_code}, {len(j.get('made', []))})")
    S = srcs(T, niche="food") + srcs(T, niche="devops")
    tops = [x for x in S if x.kind == "topic"]
    ok(len(tops) == 10 and all(x.mode == "cc" and x.auto and x.review_first and x.resolved_url == "topic" for x in tops),
       "topic sources: Creative Commons only, running by themselves, first clips wait for review")
    dest = srcs(T)[0].destination_id
    ok(all(x.destination_id == dest and x.post_times == "10:00, 18:00" and x.tz_offset == -330 and x.videos_per_run == 1 and x.clips_per_video == 1 for x in S),
       "they upload to the account's channel at its upload times, one video a run")
    ok(sorted(x.query for x in S if x.kind == "trending") == ["26", "28"] and all(x.interval_hours == 48 for x in S),
       f"one trending source per category; runs spaced so 12 sources make about 6 clips a day ({S[0].interval_hours} h)")
    ok({x.query for x in tops} >= set(niches.BY_KEY["food"]["phrases"]) and any(x.name == "Food and street food: indian street food" for x in tops),
       "every phrase of the niche became a source, named after the niche")
    ok(srcs(tenant.OWNER) == [] and srcs(T2) == [], "nothing was made in the owner's copy or another customer's copy")
    ok(not c.get("/api/onboarding").json()["active"], "the niche step satisfies the source step: setup finished")
    r = c.post("/api/niches", json={"keys": ["food"]}).json()
    ok(r["made"] == [] and r["turned_on"] == [] and len(srcs(T, niche="food")) == 6, "picking the same niche again makes no duplicates")
    # length limits (follow-up 8)
    ok(c.post("/api/niches", json={"keys": ["x" * 41]}).status_code == 422 and c.post("/api/niches", json={"topic": "y" * 61}).status_code == 422
       and c.get("/api/niches", params={"topic": "z" * 61}).status_code == 422, "an over-long niche key or topic is refused (422)")

    # ------------------------------------------------------------ typed topic + preview + honesty
    p = c.get("/api/niches", params={"topic": "home gardening"}).json()["preview"]
    ok(p["phrases"][0] == "home gardening" and p["warning"] == "" and "home gardening tips" in p["suggest"], f"preview of a typed topic: its phrases and what people also search ({p['suggest'][:2]})")
    ok("Creative Commons" in c.get("/api/niches", params={"topic": "bollywood songs"}).json()["preview"]["warning"], "a music / film topic gets a plain warning before it is made")
    r = c.post("/api/niches", json={"topic": "Home Gardening"}).json()
    G = srcs(T, niche="custom:home-gardening")
    ok(len(r["made"]) == 4 and len(G) == 4 and {x.query for x in G} == {"Home Gardening", "Home Gardening shorts", "Home Gardening best moments", "Home Gardening hindi"},
       "a typed topic becomes 4 topic sources kept together as one niche")
    on = [x for x in srcs(T) if x.niche and x.auto]
    ok(len(on) == 12 and r["parked"] == 4 and all(x.interval_hours == 48 for x in on) and "phrases wait" in r["why"],
       f"3 niches: still 12 sources on (4 later phrases wait switched off), ALL re-timed to 48 h = about 6 clips a day ({len(on)}, {r['parked']})")
    r = c.post("/api/niches", json={"topic": "bollywood songs"}).json()
    ok(any("Creative Commons" in w for w in r["warnings"]), "…and a music topic says plainly that few or no clips can be expected")
    with tenant.use(T):
        with SessionLocal() as s:
            for x in s.query(Channel).filter(Channel.niche == "custom:home-gardening"):
                x.last_run, x.last_status = utcnow(), "no new Creative Commons videos found for this topic"
            s.commit()
    mine = {m["key"]: m for m in c.get("/api/niches").json()["mine"]}
    ok(any("found little" in w for w in mine["custom:home-gardening"]["warnings"]) and not mine["food"]["warnings"],
       "a typed topic whose first searches found little gets a warning on the Sources page")

    # ------------------------------------------------------------ Remove: sources off, nothing deleted, clips stay
    f0 = srcs(T, niche="food")[0]
    with tenant.use(T):
        with SessionLocal() as s:
            pr = Project(url="u", video_id="vidfood0001", mode="cc", channel_ref=f0.id); s.add(pr); s.commit()
            s.add(Clip(project_id=pr.id, status="queued", destination_id=dest)); s.commit()
    food_on = sum(1 for x in srcs(T, niche="food") if x.auto)
    r = c.post("/api/niches/remove", json={"key": "food"})
    ok(r.status_code == 200 and r.json()["switched_off"] == food_on and len(srcs(T, niche="food")) == 6 and not any(x.auto for x in srcs(T, niche="food")),
       f"Remove switches the niche's {food_on} running sources off; none of its 6 is deleted")
    with tenant.use(T):
        with SessionLocal() as s:
            ok(s.query(Clip).count() == 1 and s.query(Project).filter(Project.channel_ref == f0.id).count() == 1, "the clip it already made stays")
    on = [x for x in srcs(T) if x.niche and x.auto]
    ok(any(x.auto for x in srcs(T, niche="devops")) and len(on) == 12 and all(x.interval_hours == 48 for x in on),
       "other niches keep running and get the freed places (still 12 on, all re-timed)")
    mine = {m["key"]: m for m in r.json()["mine"]}
    ok(not mine["food"]["active"] and mine["food"]["videos"] == 1 and mine["devops"]["active"], "the card shows food as off (1 video used so far)")
    r = c.post("/api/niches", json={"keys": ["food"]}).json()
    ok(len(r["turned_on"]) == 3 and r["made"] == [] and sum(1 for x in srcs(T, niche="food") if x.auto) == 3,
       "picking it again switches its first 3 sources back on (4 niches share the 12 places)")
    ok(c.post("/api/niches", json={"keys": ["travel"]}).status_code == 400 and srcs(T, niche="travel") == [],
       "a 5th niche at once is refused with a reason (at most 4)")
    c.post("/api/niches/remove", json={"key": "custom:home-gardening"})
    r = c.post("/api/niches", json={"keys": ["custom:home-gardening"]}).json()
    ok(len(r["turned_on"]) == 3 and sum(1 for x in srcs(T, niche="custom:home-gardening") if x.auto) == 3, "a typed topic can be switched on again from the card")
    ok(sum(24 / x.interval_hours for x in srcs(T) if x.niche and x.auto) <= 6.01, "after every change the account's niche sources run about 6 times a day in total")
    ok(c.post("/api/niches/remove", json={"key": "nope"}).status_code == 404, "removing a niche the account does not have: 404")
    ok(c2.post("/api/niches/remove", json={"key": "food"}).status_code in (404, 409) and sum(1 for x in srcs(T, niche="food") if x.auto) == 3,
       "another customer cannot switch this account's niche off")

    # ------------------------------------------------------------ plan limits (customer B)
    ready_to_source(c2, owner, aid2, T2)
    owner.post(f"/api/admin/users/{aid2}/access", json={"perms": {"source.topic": False}})
    ok(c2.post("/api/niches", json={"keys": ["travel"]}).status_code == 403 and srcs(T2) == [], "topic sources switched off for the account: niches refused (403), nothing made")
    ok(not c2.get("/api/niches").json()["topic_on"], "…and the picker knows it")
    owner.post(f"/api/admin/users/{aid2}/access", json={"perms": {"source.topic": True, "source.trending": False}, "limits": {"sources": 3, "clips_per_day": 2}})
    r = c2.post("/api/niches", json={"keys": ["food", "travel"]})
    j = r.json()
    B = srcs(T2)
    ok(r.status_code == 200 and len(B) == 3 and j["skipped"] > 0 and "3 sources" in j["why"], f"source limit 3: only 3 made, the rest named as not made ({len(B)}, {j.get('why', '')[:60]})")
    ok("switched-off ones count too" in j["why"] and "Remove a niche" not in j["why"], "the message says switched-off sources count too (no “remove a niche to make room”)")
    ok({x.niche for x in B} == {"food", "travel"} and [x.query for x in B][:2] == ["indian street food", "india travel vlog"],
       "both picked niches got their first phrase first")
    ok(not any(x.kind == "trending" for x in B), "trending sources switched off: none made")
    ok(all(x.interval_hours == 36 for x in B), f"clips-a-day limit 2: runs spaced so 3 sources stay near 2 clips a day ({B[0].interval_hours} h)")
    ok(c2.post("/api/niches", json={"keys": ["gaming"]}).status_code == 403 and len(srcs(T2)) == 3, "no room left: refused with the plan's message, nothing made")
    ok(c2.get("/api/niches").json()["room"] == 0, "the picker shows how many more sources the plan allows")
    owner.post(f"/api/admin/users/{aid2}/access", json={"limits": {"sources": 0, "clips_per_day": 0}})
    owner.post(f"/api/admin/users/{aid2}/access", json={"perms": {"page.ideas": False}})
    p = c2.get("/api/niches", params={"topic": "yoga at home"}).json()["preview"]
    ok(p["phrases"] and p["suggest"] == [], "Ideas page switched off: the preview shows the phrases but no keyword suggestions")
    owner.post(f"/api/admin/users/{aid2}/access", json={"perms": {"page.ideas": True}})

    # ------------------------------------------------------------ the shared 24-hour search cache
    quota.SEARCH_BUDGET = 100000  # (the day's search budget is tested at the end)
    with tenant.use(T):
        a = youtube.api_search_cc("indian street food", set(), 3)
    ok(len(a) == 3 and len(calls) == 1 and calls[0][0] == T, "customer A's first search asks YouTube once")
    with tenant.use(T2):
        set_setting("google_client_id", "123456789012-abcdefghijklmnopqrstuvwxyz012345.apps.googleusercontent.com")  # B: own Google project
        b = youtube.api_search_cc("Indian  Street Food", set(), 3)
    ok(b == a and len(calls) == 1, "customer B gets the same results from the shared cache: no second search")
    with tenant.use(T):
        two = youtube.api_search_cc("indian street food", set(a), 3)
    ok(len(two) == 2 and len(calls) == 1, "2 unused videos left in the cache (3 wanted): they are used, no next page is searched yet")
    with tenant.use(tenant.OWNER):
        rows = [r for r in SessionLocal().query(Setting).filter(Setting.key.like("ytcc:%"))]
    e = json.loads(rows[0].value) if rows else {}
    ok(len(rows) == 1 and set(e) == {"at", "q", "items", "pages", "done", "tokens"} and all(len(x) == 2 for x in e["items"])
       and all(set(t) == {"token", "page"} for t in e["tokens"].values()),
       "the cache keeps the public result list only (video id + title, page tokens per project), in one shared place")
    with tenant.use(T):
        mine_keys = [r.key for r in SessionLocal().query(Setting).filter(Setting.key.like("ytcc:%"))]
    ok(mine_keys == [], "…not in a customer's own copy")
    first5 = {x[0] for x in e["items"]}
    with tenant.use(T2):
        b2 = youtube.api_search_cc("indian street food", first5, 3)
    ok(len(b2) == 3 and len(calls) == 3 and calls[1] == (T2, "indian street food", None) and calls[2] == (T2, "indian street food", "P2")
       and not (set(b2) & first5),
       "an account (other Google project) that used every cached video: its own page 1, then its own next-page token — never another project's")
    with tenant.use(T):
        n0 = len(calls); x = youtube.api_search_cc("indian street food", first5 | set(b2), 3)
    ok(len(calls) == n0 and len(x) == 2, "customer A then takes the rest of page 2 from the cache, without searching")
    with tenant.use(T2):
        allids = {i for i, _t in search_cache.get("indian street food")["items"]}
        z = youtube.api_search_cc("indian street food", allids, 3)
        why = youtube.SEARCH_NOTE.get()
    ok(z == [] and len(calls) == n0 and "used already" in why, "every page known and used: no search at all until tomorrow")
    with tenant.use(tenant.OWNER):
        k = search_cache._key("indian street food")
        v = json.loads(get_setting(k)); v["at"] = (utcnow() - timedelta(hours=25)).isoformat(); set_setting(k, json.dumps(v))
    ok(search_cache.get("indian street food") is None, "results older than 24 hours are not used")
    with tenant.use(T2):
        youtube.api_search_cc("indian street food", allids, 3)
    ok(len(calls) > n0 and calls[n0][2] is None, "…a new search starts from page 1 again")
    ok(search_cache.purge(utcnow() + timedelta(hours=25)) == 1, "old results are cleaned up")
    with tenant.use(T2):
        n0 = len(calls); youtube.api_search_cc("  Yoga   AT Home ", set(), 3)
    ok(len(calls) == n0 + 1 and calls[-1][1] == "yoga at home" and search_cache.get("yoga at home") is not None,
       "YouTube is asked with exactly the (normalised) words the cache keeps")
    with tenant.use(T2):
        ids1 = set(youtube.api_search_cc("token test", set(), 5))
    with tenant.use(tenant.OWNER):
        n0 = len(calls); youtube.api_search_cc("token test", ids1, 3)
    ok(calls[n0:] == [(1, "token test", None), (1, "token test", "P2")], f"the owner's project does not reuse customer B's page token ({calls[n0:]})")

    # ------------------------------------------------------------ per-account turns on the shared Google project
    with tenant.use(T):
        n0 = len(calls); youtube.api_search_cc("phrase one", set(), 3)
        ok(len(calls) == n0 + 1 and quota.searches_today() == 2, "customer A (owner's project) made its 2nd topic search today")
        y = youtube.api_search_cc("phrase two", set(), 3); why = youtube.SEARCH_NOTE.get()
    ok(y == [] and len(calls) == n0 + 1 and "2 topic searches" in why, f"its 3rd is refused: at most 2 a day on the shared project ({why[:60]})")
    with tenant.use(T2):
        for q in ("own one", "own two", "own three"):
            youtube.api_search_cc(q, set(), 3)
    ok(len(calls) == n0 + 4, "customer B (own Google project) is not held to the shared cap")
    with tenant.use(tenant.OWNER):
        youtube.api_search_cc("owner one", set(), 3)
    ok(len(calls) == n0 + 5, "the owner still searches although customer A used its turns")
    c3, aid3, T3 = customer(anon, owner, "mina@example.com", "Mina")
    owner.post(f"/api/admin/users/{aid3}/shared-google", json={"on": True})
    with tenant.use(T3):
        quota.note_turn(False)  # customer C asked and had to wait (e.g. the budget was used at that moment)
    with tenant.use(tenant.OWNER):
        why = quota.search_block()
        youtube.api_search_cc("owner two", set(), 3)
    ok("waiting for its turn" in why and len(calls) == n0 + 5, "round-robin: the owner (3 searches today) waits while customer C (0) waits for a turn")
    with tenant.use(T3):
        ok(quota.search_block() == "", "customer C, with the fewest searches, may search")
        youtube.api_search_cc("mina one", set(), 3)
    with tenant.use(tenant.OWNER):
        ok(quota.search_block() == "", "after C's turn the owner may search again")
        youtube.api_search_cc("owner two", set(), 3); youtube.api_search_cc("owner three", set(), 3)
        ok(quota.searches_today() == 5 > quota.SEARCHES_PER_ACCOUNT, "the owner itself is not held to the per-account cap")
    with tenant.use(tenant.OWNER):
        turns = json.loads(get_setting("quota-turns:" + quota._pt_day()))
    ok(set(turns) >= {str(T), str(T3), "1"} and str(T2) not in turns, "turns of the accounts on the owner's project are counted in the owner's budget; B's in its own")

    # ------------------------------------------------------------ a niche source reports why it did not search
    dv = [x for x in srcs(T, niche="devops") if x.kind == "topic"][0]
    with tenant.use(T):
        autopilot.run_channel(dv.id)
    st = srcs(T, id=dv.id)[0].last_status
    ok("search skipped" in st and "2 topic searches" in st, f"a niche source of an account without turns left says so in Sources ({st[:70]})")

    # ------------------------------------------------------------ What works: niche sources take part in auto-tune
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Owner", token="x"))
        s.add(Channel(kind="channel", query="@somecreator", mode="cc", post_times="18:00", tz_offset=-330, auto=False)); s.commit()
    r = owner.post("/api/niches", json={"keys": ["fitness"], "review_first": False})
    ok(r.status_code == 200 and len(srcs(tenant.OWNER, niche="fitness")) == 5, "the owner can pick a niche too (no setup lock)")
    ok(all(x.post_times == "18:00" and x.tz_offset == -330 and not x.review_first for x in srcs(tenant.OWNER, niche="fitness")),
       "niche sources take the account's own upload times (those of its sources)")
    fit = srcs(tenant.OWNER, niche="fitness")[0]
    old = utcnow() - timedelta(days=5)
    with SessionLocal() as s:
        did = s.query(Destination).first().id
        for i in range(10):
            vid = f"ytfit{i:06d}"
            s.add(VideoStat(video_id=vid, destination_id=did, title="t", published_at=old, privacy="public", views=1000 if i < 5 else 100))
            if i < 5:
                pr = Project(url="u", video_id=f"srcfit{i:05d}", mode="cc", channel_ref=fit.id, channel="Creator"); s.add(pr); s.commit()
                s.add(Clip(project_id=pr.id, status="uploaded", youtube_id=vid, destination_id=did, title=f"home workout {i}", start=0, end=30))
        s.commit()
    done = learn.daily(force=True)
    ok(srcs(tenant.OWNER, id=fit.id)[0].videos_per_run == 2 and any("fitness" in t.lower() or "home workout" in t for t in done),
       f"auto-tune raises a well-doing niche source like any topic source (1 → 2): {done[:1]}")

    # ------------------------------------------------------------ follow-up 1: volume stays near the target on every pick
    ok(all(x.interval_hours == 20 for x in srcs(tenant.OWNER, niche="fitness")), "one niche of 5 sources: every 20 h (5 × 24 / 20 = 6 runs a day)")
    r = owner.post("/api/niches", json={"keys": ["food", "travel", "gaming"]}).json()
    on = [x for x in srcs(tenant.OWNER) if x.niche and x.auto]
    ok(len(on) == 12 and all(x.interval_hours == 48 for x in on) and sum(24 / x.interval_hours for x in on) == 6,
       f"4 niches: 12 sources on, ALL of them (also the older fitness ones) re-timed to 48 h = 6 runs a day ({len(on)})")
    ok({x.niche for x in on} == {"fitness", "food", "travel", "gaming"} and all(sum(1 for x in on if x.niche == k) == 3 for k in ("fitness", "food", "travel", "gaming")),
       "every niche gets the same share (its first 3 phrases)")
    ok(srcs(tenant.OWNER, id=fit.id)[0].auto, "the niche's first phrase stays on")
    r = owner.post("/api/niches", json={"keys": ["cars"]})
    ok(r.status_code == 400 and "up to 4" in r.json()["detail"], "a 5th niche is refused: remove one first")
    owner.post("/api/niches/remove", json={"key": "travel"})
    on = [x for x in srcs(tenant.OWNER) if x.niche and x.auto]
    ok(len(on) == 12 and sum(24 / x.interval_hours for x in on) <= 6.01 and not any(x.niche == "travel" for x in on),
       "after Remove the other 3 niches share the 12 places, still about 6 runs a day")

    # ------------------------------------------------------------ follow-up 4: a trending source two niches share
    ready_to_source(c3, owner, aid3, T3)
    r = c3.post("/api/niches", json={"keys": ["food", "beauty"]})
    tr = [x for x in srcs(T3) if x.kind == "trending"]
    ok(r.status_code == 200 and len(tr) == 1 and tr[0].query == "26", "food and beauty share one trending source (category 26)")
    c3.post("/api/niches/remove", json={"key": tr[0].niche})
    tr = srcs(T3, id=tr[0].id)[0]
    ok(tr.auto and tr.niche == "beauty" and not any(x.auto for x in srcs(T3, niche="food")),
       "removing food keeps the shared trending source on — it now belongs to beauty")

    # ------------------------------------------------------------ follow-up 2: clips waiting for review have a ceiling
    autopilot.REVIEW_CAP = 3
    with tenant.use(T):
        with SessionLocal() as s:
            pid = s.query(Project).first().id
            for i in range(3):
                s.add(Clip(project_id=pid, status="rendered", destination_id=dest))
            x = s.get(Channel, dv.id); x.auto, x.review_first = True, True
            s.add(Channel(kind="topic", query="stale test", name="Stale test", mode="cc", auto=True, resolved_url="topic",
                          destination_id=dest, last_run=utcnow() - timedelta(hours=10)))
            x.last_run = utcnow() - timedelta(hours=10); x.interval_hours = 48
            s.commit()
    h = c.get("/api/health").json()["items"]
    ok(any(i["key"] == "review_full" and "3 clips are waiting for your review" in i["text"] for i in h),
       "Problems list: “3 clips are waiting for your review”")
    stale = [i["text"] for i in h if i["key"] == "source_stale"]
    ok(any("Stale test" in t for t in stale) and not any(dv.name in t for t in stale),
       "a source that runs every 48 h is not called late after 10 h (a 6-hour source is)")
    with tenant.use(T):
        autopilot.run_channel(dv.id)
    st = srcs(T, id=dv.id)[0].last_status
    ok(st.startswith("paused: 3 clips are waiting for your review"), f"a Review-first source pauses at the account's review ceiling ({st[:60]})")
    with tenant.use(T):
        with SessionLocal() as s:
            s.get(Channel, dv.id).review_first = False; s.commit()
        autopilot.run_channel(dv.id)
    st = srcs(T, id=dv.id)[0].last_status
    ok("review" not in st, f"a source that uploads by itself is not held by the review ceiling ({st[:50]})")
    autopilot.REVIEW_CAP = 40
    # disk: customers' sources pause first
    autopilot.disk_free_gb = lambda: 1.2
    with tenant.use(T):
        autopilot.run_channel(dv.id)
    autopilot.run_channel(fit.id)
    ok("disk is almost full" in srcs(T, id=dv.id)[0].last_status and "disk" not in (srcs(tenant.OWNER, id=fit.id)[0].last_status or ""),
       "1.2 GB free: the customer's sources wait, the owner's still run")
    autopilot.disk_free_gb = lambda: 0.5
    autopilot.run_channel(fit.id)
    ok("disk is almost full" in srcs(tenant.OWNER, id=fit.id)[0].last_status, "0.5 GB free: the owner's wait too")
    autopilot.disk_free_gb = lambda: 50.0

    # ------------------------------------------------------------ uploads keep the budget
    with tenant.use(tenant.OWNER):
        quota.spend("read", units=quota.DAILY_UNITS - quota.used() - 1700)  # 1,700 units left: one upload needs 1,650
        n0 = len(calls); why = quota.search_block(); youtube.api_search_cc("owner four", set(), 3)
    ok("uploads" in why and len(calls) == n0, "with only an upload's worth left, no topic search is made (uploads first)")
    with tenant.use(T3):
        youtube.api_search_cc("mina two", set(), 3)
    ok(len(calls) == n0, "…for customers on the owner's project too")
    with tenant.use(T2):
        youtube.api_search_cc("own four", set(), 3)
    ok(len(calls) == n0 + 1, "a customer with its own Google project still searches on its own budget")
    quota.SEARCH_BUDGET = 400
    with tenant.use(T2):
        set_setting("quota-search:" + quota._pt_day(), "400")
        why = quota.search_block()
    ok("search budget" in why, "the day's search budget (400 units) is a hard stop as before")

    # ------------------------------------------------------------ follow-up 9 + 10
    with tenant.use(tenant.OWNER):
        set_setting("quota-turns:2000-01-01", "{}")
        with SessionLocal() as s:
            s.query(Setting).filter(Setting.key == "quota-turns:" + quota._pt_day()).delete(); s.commit()
        quota.note_turn(True)
        ok(get_setting("quota-turns:2000-01-01") is None and get_setting("quota-turns:" + quota._pt_day()), "turn counts of earlier days are dropped on the first turn of a day")
    page = open(r"D:\integ-final\static\index.html", encoding="utf-8").read()
    ok("cricket coaching" not in page and "coding for beginners" in page, "the topic example is one with Creative Commons videos")

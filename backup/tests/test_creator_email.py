"""Creator e-mails found by themselves (description -> recent videos -> their own pages), permission mails sent by
themselves (the account's switch), and replies read by themselves for every account. No real network: YouTube, the
web (httpx) and Gmail are fakes. Sections marked [review] cover the fixes after the independent review (2026-11-24)."""
import json, os, sys, tempfile, threading, time
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = os.environ.get("RS_WT", r"D:\creator-email")
sys.path.insert(0, WT)
import httpx
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from app import main, acl, tenant, auth, youtube, telegram, permissions, gmail, quota, comments, scheduler, jobs, backup, competitors
from app import creator_email as ce
from app.db import SessionLocal, Destination, Channel, Setting, set_setting, get_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)

told = []
telegram.notify = lambda text, buttons=None, push_phone=True: told.append((tenant.current(), text, buttons))
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None  # nothing is rendered here

# ---------------------------------------------------------------- fake YouTube
YT_CH, YT_UP, YT_V, calls = {}, {}, {}, []


class _Req:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class _Res:
    def __init__(self, kind): self.kind = kind
    def list(self, **k):
        calls.append((self.kind, k))
        if self.kind == "channels":
            return _Req(lambda: {"items": [YT_CH[i] for i in k["id"].split(",") if i in YT_CH]})
        if self.kind == "playlistItems":
            return _Req(lambda: {"items": [{"contentDetails": {"videoId": v}} for v in YT_UP.get(k["playlistId"], [])]})
        return _Req(lambda: {"items": [{"id": v, "snippet": YT_V[v]} for v in k["id"].split(",") if v in YT_V]})


class FakeYT:
    def channels(self): return _Res("channels")
    def playlistItems(self): return _Res("playlistItems")
    def videos(self): return _Res("videos")


youtube.client = lambda *a, **k: FakeYT()


def channel(cid, desc, title="", handle=""):
    YT_CH[cid] = {"id": cid, "snippet": {"description": desc, "title": title, "customUrl": handle},
                  "brandingSettings": {"channel": {"description": ""}}, "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid}}}


# ---------------------------------------------------------------- fake web
SITES, hits = {}, []
ce._resolve = lambda h: ["10.0.0.5"] if h in ("internal-box.in",) else ["93.184.216.34"]


def handler(req):
    url = str(req.url)
    hits.append((url, req.headers.get("user-agent", "")))
    if url in SITES:
        st, ct, body, hdr = (SITES[url] + ({},))[:4]
        if callable(body):
            return httpx.Response(st, headers={"content-type": ct, **hdr}, content=body())
        return httpx.Response(st, headers={"content-type": ct, **hdr}, content=body if isinstance(body, bytes) else body.encode())
    return httpx.Response(404, headers={"content-type": "text/html"}, content=b"not here")


ce._transport = httpx.MockTransport(handler)
ROBOTS_OK = (200, "text/plain", "User-agent: *\nAllow: /\n")

# ---------------------------------------------------------------- fake Gmail (per account)
SENT, THREADS = [], {}  # THREADS[(tid, thread)] = (from, text)  or  [message dicts]
slow = [0.0]


class Svc:
    def __init__(self, tid): self.tid = tid
    def users(self): return self
    def messages(self): return self
    def threads(self): return self
    def send(self, userId, body):
        time.sleep(slow[0])
        SENT.append((self.tid, body))
        n = len(SENT)
        return _Req(lambda: {"threadId": f"S{self.tid}-{n}", "id": f"M{n}"})
    def get(self, userId, id, **k):
        msgs = [{"id": "m0", "labelIds": ["SENT"], "payload": {"headers": [{"name": "From", "value": "me"}]}}]
        r = THREADS.get((self.tid, id))
        if isinstance(r, list):
            msgs += r
        elif r:
            msgs.append({"id": f"r-{id}", "labelIds": ["INBOX"], "snippet": r[1],
                         "payload": {"headers": [{"name": "From", "value": r[0]}, {"name": "Date", "value": "Mon, 23 Nov 2026"}]}})
        return _Req(lambda: {"messages": msgs})


def msg(mid, frm, text, labels=("INBOX",), **headers):
    hs = [{"name": "From", "value": frm}, {"name": "Date", "value": "Tue, 24 Nov 2026"}] + [{"name": k.replace("_", "-"), "value": v} for k, v in headers.items()]
    return {"id": mid, "labelIds": list(labels), "snippet": text, "payload": {"headers": hs}}


gmail._service = lambda: Svc(tenant.current())


def creator(cid, name, status="new", **kw):
    c = {"channel_id": cid, "name": name, "url": f"https://www.youtube.com/channel/{cid}", "video_id": "vvvvvvvvvvv",
         "video_title": "A video", "views": 1000, "status": status, "added": "2026-11-20", "added_at": utcnow().isoformat() + "Z",
         "subject": "Hi", "message": "Hello there, may I share short clips of your videos with full credit?"}
    if kw.get("email"):
        c["email_trust"] = "channel"
    c.update(kw)
    return c


def put(cs):
    set_setting("creators", json.dumps({c["channel_id"]: c for c in cs}))


def L():
    return json.loads(get_setting("creators") or "{}")


def fill_quota(units):
    set_setting("quota:" + quota._pt_day(), str(units))


# ================================================================ 1. reading addresses (each stage's text)
ok(ce.best("Business: asha [at] creator [dot] in") == "asha@creator.in", "obfuscated [at] [dot] address read")
ok(ce.best("mail: Raj(at)gmail(dot)com") == "raj@gmail.com" and ce.best("ravi {at} site {dot} in") == "ravi@site.in",
   "(at)(dot) and {at}{dot} forms read")
ok(ce.best("contact: neha[@]neha.in") == "neha@neha.in", "[@] form read")
ok(ce.best("look at this dot com") is None, "plain words 'at' / 'dot' are not turned into an address")
ok(ce.best("raj @ gmail.com") is None and ce.addresses("Subscribe @ funny.shorts for more") == []
   and ce.addresses("Follow me @ rahul.vlogs") == [], "[review] '@ name.tv' with spaces is never an address")
bad = "logo@2x.png banner@site.webp noreply@brand.com no-reply@x.in donotreply@y.com a@youtube.com b@google.com " \
      "c@instagram.com d@facebook.com e@linktr.ee f@example.com mailer-daemon@z.com"
ok(ce.addresses(bad) == [], "image names, no-reply and platform / example addresses rejected: " + str(ce.addresses(bad)))
ok(ce.reject_reason("logo@2x.png") == "an image file name" and ce.reject_reason("noreply@brand.com") == "a no-reply address"
   and ce.reject_reason("x@youtube.com") == "an address of the platform itself" and ce.reject_reason("raj@gmail.com") is None,
   "reasons are given")
ok(ce.best("personal: raj@gmail.com\nFor collabs: team@rajfoods.in") == "team@rajfoods.in", "labelled 'collab' address preferred")
ok(ce.best("raj@gmail.com hello@rajfoods.in", {"rajfoods.in"}) == "hello@rajfoods.in", "address on the creator's own domain preferred")
ok(ce.best("first@gmail.com second@gmail.com") == "first@gmail.com", "otherwise the first one")
ok(ce.own_domains(["https://www.rajfoods.in/shop", "https://instagram.com/raj", "https://bit.ly/x", "https://youtube.com/@raj",
                   "https://linktr.ee/raj", "https://amzn.to/abc"], ["Raj Foods"]) == {"rajfoods.in"},
   "own website told apart from social / shops / link pages")
ok(ce.own_domains(["https://nordvpn.com/rahul", "https://rahulmerch.store", "https://www.bhphotovideo.com/x"], ["Rahul Vlogs"])
   == {"rahulmerch.store"}, "[review] a sponsor's / shop's link is never 'their website' (only domains named like the channel)")
ok(ce.own_domains(["https://rajfoods.in"]) == set(), "[review] without the channel's name no domain counts as theirs")
for t in ("This video is sponsored by NordVPN. Partnerships: partners@nordvpn.com", "Music by DJ X - contact@djx.com",
          "Collab with my friend: priya.business@gmail.com"):
    ok(ce.best(t, names=["Rahul Vlogs"]) is None, "[review] somebody else's address is left out: " + t[:40])
ok(ce.best("Sponsored by Acme https://acme.com — support@acme.com. Business: rahulvlogs@gmail.com", names=["Rahul Vlogs"])
   == "rahulvlogs@gmail.com", "[review] a sponsor's labelled address never beats the creator's own")
ok(ce.description_pick("Business enquiries: desk@cooks.in", ["Desk Cooks"]) == ("desk@cooks.in", True),
   "[review] description, labelled -> trusted (may be mailed by itself)")
ok(ce.description_pick("write: someone@gmail.com", ["Desk Cooks"]) == ("someone@gmail.com", False),
   "[review] description, unlabelled and not named like the channel -> please confirm")
ok(ce.description_pick("Business: a@x.in\nContact: b@y.in", ["Zed"])[1] is False, "[review] two labelled addresses -> please confirm")
ok(ce.description_pick("hi rahulvlogs@gmail.com", ["Rahul Vlogs"]) == ("rahulvlogs@gmail.com", True),
   "[review] named like the channel -> trusted")
ok(ce.description_pick("biz: vlog@mail.in", ["Vlogger"]) == ("vlog@mail.in", True) and not ce.GOOD_WORDS.search("showbiz news"),
   "'biz:' is a business label (a whole word only)")
for f in ("app/creator_email.py", "app/permissions.py", "app/gmail.py", "app/backup.py", "static/index.html"):
    ok(b"\x08" not in open(os.path.join(WT, f), "rb").read(), "no backspace byte in " + f)
vids = [{"title": "Sponsored cooking video", "description": "Thanks to our sponsor, write to deals@sponsorco.com"},
        {"title": "Street food tour", "description": "Music: tunes@musicguy.in"}]
ok(ce.from_videos(vids) is None, "an unlabelled address in one video (sponsor / music) is NOT taken")
vids2 = vids + [{"title": "Paneer recipe", "description": "Follow me! chef.raj@gmail.com"},
                {"title": "Biryani at home in 10 minutes flat, the easiest way ever explained", "description": "chef.raj@gmail.com"}]
ok(ce.from_videos(vids2) == ("chef.raj@gmail.com", "Paneer recipe"), "an address in 2+ videos (their footer) is taken")
vids3 = [{"title": "Vlog 12", "description": "For business enquiries: mgmt@rajtalent.in"}]
ok(ce.from_videos(vids3) is None and ce.from_videos(vids3, names=["Raj Talent"]) == ("mgmt@rajtalent.in", "Vlog 12"),
   "[review] one video: taken only when named like the channel")
ok(ce.from_videos([{"title": "v1", "description": "Thanks to our sponsor Acme. For partnership enquiries: hello@acme.com"}]) is None
   and ce.from_videos([{"title": "v", "description": "Guest: Priya. team@priyastudio.com"}]) is None,
   "[review] a sponsor's labelled address / a guest's address is not taken")

# ================================================================ 2. website fetch limits
SITES.update({
    "https://rajfoods.in/robots.txt": ROBOTS_OK,
    "https://rajfoods.in/": (200, "text/html", '<html><a href="https://other-site.com/contact">partner</a>'
                                               '<a href="/contact-us">Contact</a><p>Welcome</p></html>'),
    "https://rajfoods.in/contact-us": (200, "text/html", '<p>Write to us <a href="mailto:hello@rajfoods.in?subject=hi">mail</a></p>'),
    "https://blocked.in/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /\n"),
    "https://blocked.in/": (200, "text/html", "<a href='mailto:x@blocked.in'>x</a>"),
    "https://big.in/robots.txt": ROBOTS_OK,
    "https://big.in/": (200, "text/html", b"<html>" + b"a" * (ce.MAX_BYTES + 200000) + b" big@big.in</html>"),
    "https://jump.in/robots.txt": ROBOTS_OK,
    "https://jump.in/": (302, "text/html", "", {"location": "https://elsewhere.com/"}),
    "https://elsewhere.com/": (200, "text/html", "<a href='mailto:z@elsewhere.com'>z</a>"),
    "https://shy.in/robots.txt": (403, "text/plain", "no"),
    "https://shy.in/": (200, "text/html", "<a href='mailto:s@shy.in'>s</a>"),
    "https://www.instagram.com/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /\n"),
    "https://linktr.ee/robots.txt": ROBOTS_OK,
    "https://linktr.ee/rajkitchen": (200, "text/html", "<p>Business: rajkitchen [at] gmail [dot] com</p>"),
    "https://nordvpn.com/robots.txt": ROBOTS_OK,
    "https://nordvpn.com/": (200, "text/html", '<a href="mailto:support@nordvpn.com">help</a>'),
})
hits.clear()
r = ce.search_links(["https://rajfoods.in"], names=["Raj Foods"])
ok(r == ("hello@rajfoods.in", "their website contact page"), f"website: homepage, then the contact page on the same domain: {r}")
ok(not any("other-site.com" in u for u, _ in hits) and all("rajfoods.in" in u for u, _ in hits), "only the same domain is fetched")
ok(len([u for u, _ in hits if not u.endswith("robots.txt")]) <= 2, "at most the homepage + one contact page")
ua = {a for _, a in hits}
ok(ua == {ce.UA} and "RajiStudio" in ce.UA and "Funny" not in ce.UA and "@" not in ce.UA, "honest, generic User-Agent (no owner details)")
hits.clear()
ok(ce.search_links(["https://nordvpn.com/rahul"], names=["Rahul Vlogs"]) is None and not hits,
   "[review] a sponsor's site is never fetched as 'their website'")
hits.clear()
f = ce.Fetcher()
ok(ce.search_links(["https://blocked.in"], f, ["Blocked"]) is None and not any(u == "https://blocked.in/" for u, _ in hits)
   and any("robots.txt" in n for n in f.notes), "robots.txt 'Disallow' honoured: the page is never fetched")
ok(ce.search_links(["https://shy.in"], ce.Fetcher(), ["Shy"]) is None, "robots.txt cannot be read (403): nothing fetched")
got = ce.Fetcher().page("https://big.in/")
ok(got is not None and len(got[0]) <= ce.MAX_BYTES and "big@big.in" not in got[0], f"size cap: only {ce.MAX_BYTES} bytes are read")
hits.clear()
ok(ce.search_links(["https://jump.in"], names=["Jump"]) is None and not any("elsewhere.com" in u for u, _ in hits),
   "a redirect to another site is not followed")
hits.clear()
ok(ce.search_links(["https://internal-box.in"], names=["Internal Box"]) is None and not hits, "a site on a private address is never fetched")
ok(ce.search_links(["https://linktr.ee/rajkitchen"], names=["Raj Kitchen"]) == ("rajkitchen@gmail.com", "their link page (linktr.ee)"),
   "link page read (its handle is the channel's)")
ok(ce.search_links(["https://linktr.ee/rajkitchen"], names=["Someone Else"]) is None, "[review] somebody else's link page is not read")
hits.clear()
ok(ce.search_links(["https://www.instagram.com/rajkitchen/"], names=["Raj Kitchen"]) is None
   and not any(u.endswith("/rajkitchen/") for u, _ in hits), "Instagram's robots.txt is honoured (not fetched)")
SITES["https://moved.in/robots.txt"] = (301, "text/plain", "", {"location": "https://elsewhere.com/robots.txt"})
SITES["https://moved.in/"] = (200, "text/html", "<a href='mailto:m@moved.in'>m</a>")
ok(ce.search_links(["https://moved.in"], names=["Moved"]) is None, "robots.txt sent to another site: nothing fetched")
f = ce.Fetcher(budget_s=-1)
ok(f.page("https://rajfoods.in/") is None and "time is up" in f.notes[-1], "one creator's page reads have a time budget")
SITES["https://down.in/robots.txt"] = ROBOTS_OK
ok(ce.search_links(["https://down.in"], names=["Down"]) is None, "a site that blocks / errors: just 'not found'")
# [review] hard wall-clock deadline inside the read loop, DNS timeout, no proxy env, IPv4-in-IPv6 addresses


def trickle():
    for _ in range(30):
        time.sleep(0.1)
        yield b"x" * 100


SITES["https://slow.in/robots.txt"] = ROBOTS_OK
SITES["https://slow.in/"] = (200, "text/html", trickle)
t0 = time.monotonic()
f = ce.Fetcher(budget_s=0.6)
ok(f.page("https://slow.in/") is None and time.monotonic() - t0 < 2.0, f"[review] a trickling page is cut at the deadline ({time.monotonic() - t0:.1f} s)")
real_resolve, ce.DNS_TIMEOUT = ce._resolve, 0.3
ce._resolve = lambda h: (time.sleep(2), ["93.184.216.34"])[1]
t0 = time.monotonic()
ok(ce._public_host("hang.in") is False and time.monotonic() - t0 < 1.0, "[review] a name lookup that hangs is given up (timeout)")
ce._resolve, ce.DNS_TIMEOUT = real_resolve, 3.0
with ce._client() as c:
    ok(getattr(c, "trust_env", getattr(c, "_trust_env", None)) is False, "[review] the server's proxy settings / .netrc are not used")
ok(not any(ce._ip_ok(ip) for ip in ["::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::7f00:1", "::127.0.0.1", "100.64.0.1",
                                     "0.0.0.0", "::", "fd00::1", "2002:7f00:1::1", "169.254.169.254", "192.0.0.170"])
   and ce._ip_ok("93.184.216.34"), "[review] private / mapped / NAT64 / 6to4 addresses refused, public ones allowed")

with TestClient(main.app, raise_server_exceptions=False) as owner, TestClient(main.app, raise_server_exceptions=False) as c1, \
        TestClient(main.app, raise_server_exceptions=False) as c2, TestClient(main.app, raise_server_exceptions=False) as anon, \
        TestClient(main.app, raise_server_exceptions=False) as sup:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    sup.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Owner channel", token="x")); s.commit()
    PW = "customer-pass-12345"

    def customer(client, email, name):
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email, "password": PW, "accept_terms": True})
        aid = next(u["id"] for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email)
        tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        client.post("/api/login", json={"username": email, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
            with SessionLocal() as s:
                s.add(Destination(channel_id="UC-" + name, name=name + " channel", token="x")); s.commit()
        return aid, tid

    a1, t1 = customer(c1, "asha@example.com", "Asha")
    a2, t2 = customer(c2, "ravi@example.com", "Ravi")

    # ============================================================ 3. the staged search, labels, retries, quota
    comments.uploads_waiting = lambda: 0
    channel("UCd", "Food vlogs!\nBusiness enquiries: desc [at] cooks [dot] in")
    channel("UCv", "Daily food videos.")
    YT_UP["UUUCv"] = ["v1", "v2"]
    YT_V.update({"v1": {"title": "Paneer tikka at home", "description": "Follow! vlogger.v@gmail.com"},
                 "v2": {"title": "Chole", "description": "vlogger.v@gmail.com"}})
    channel("UCw", "Recipes. Website: https://rajfoods.in")
    channel("UCn", "Nothing to see")
    YT_UP["UUUCw"] = YT_UP["UUUCn"] = ["v3"]
    YT_V["v3"] = {"title": "Just a video", "description": "no mail here"}
    put([creator("UCd", "Desc Cooks"), creator("UCv", "Vlogger V"), creator("UCw", "Raj Foods"), creator("UCn", "Nobody")])
    fill_quota(0)
    calls.clear()
    n = permissions.find_emails()
    cs = L()
    ok(n == 3, f"3 of 4 found ({n})")
    ok(cs["UCd"]["email"] == "desc@cooks.in" and cs["UCd"]["email_from"] == "their channel description"
       and cs["UCd"]["email_trust"] == "channel", "stage 1: channel description (labelled: may be mailed by itself)")
    ok(cs["UCv"]["email"] == "vlogger.v@gmail.com" and cs["UCv"]["email_from"] == "description of video “Paneer tikka at home”"
       and cs["UCv"]["email_trust"] == "check", "stage 2: recent video descriptions, labelled with the video, 'please confirm'")
    ok(cs["UCw"]["email"] == "hello@rajfoods.in" and cs["UCw"]["email_from"] == "their website contact page"
       and cs["UCw"]["email_trust"] == "check", "stage 3: their website contact page, 'please confirm'")
    ok(not cs["UCn"].get("email") and cs["UCn"]["email_search"]["tried"] == ["description", "videos", "website"]
       and cs["UCn"]["email_search"]["tries"] == 1, "nothing found: tried description, videos, website (try 1)")
    ok(sum(1 for k, _ in calls if k == "channels") == 1, "one channels.list for all of them (1 unit)")
    ok(quota.used() == 1 + 2 * 3, f"quota counted: {quota.used()} units (1 + 2 per creator for recent videos)")
    view = {c["channel_id"]: c for c in owner.get("/api/permissions").json()}
    ok(view["UCw"]["search"]["state"] == "found" and view["UCw"]["search"]["from"] == "their website contact page"
       and view["UCw"]["search"]["confirm"] and not view["UCd"]["search"]["confirm"], "found in: shown; 'please confirm' for stages 2-3")
    ok(view["UCn"]["search"]["state"] == "none" and not view["UCn"]["search"]["final"] and view["UCn"]["search"].get("next"),
       "not found yet: next weekly try shown")
    r = owner.post("/api/permissions/UCw/confirm-email")
    ok(r.status_code == 200 and L()["UCw"]["email_trust"] == "hand", "[review] the owner's Confirm makes it usable by itself")
    calls.clear()
    permissions.find_emails()
    ok(not calls and L()["UCn"]["email_search"]["tries"] == 1, "not looked up again before a week has passed")
    for i in (2, 3):
        cs = L(); cs["UCn"]["email_search"]["last"] = (utcnow() - timedelta(days=8)).isoformat() + "Z"; put(cs.values())
        permissions.find_emails()
        ok(L()["UCn"]["email_search"]["tries"] == i, f"weekly retry {i}")
    cs = L(); cs["UCn"]["email_search"]["last"] = (utcnow() - timedelta(days=30)).isoformat() + "Z"; put(cs.values())
    calls.clear()
    permissions.find_emails()
    view = {c["channel_id"]: c for c in owner.get("/api/permissions").json()}
    ok(not calls and L()["UCn"]["email_search"]["tries"] == 3 and view["UCn"]["search"]["final"]
       and view["UCn"]["search"]["tried"] == ["description", "videos", "website"], "max 3 tries, then 'No public e-mail found (tried: …)'")
    channel("UCn", "now: Business: late [at] found [dot] in")
    r = owner.post("/api/permissions/UCn/find-email").json()
    ok(r["found"] == 1 and L()["UCn"]["email"] == "late@found.in", "the owner's 'Find e-mail' still works after the 3 tries")
    cs = L(); cs["UCn"].update(email="", email_from=None); cs["UCn"]["email_search"] = {"state": "looking", "started": utcnow().isoformat() + "Z"}; put(cs.values())
    ok({c["channel_id"]: c for c in owner.get("/api/permissions").json()}["UCn"]["search"]["state"] == "looking", "'Looking for e-mail…' while it runs")
    # quota protection
    put([creator("UCq", "Quota Q")]); channel("UCq", "q [at] q-mail [dot] in")
    quota.exhaust(); calls.clear()
    ok(permissions.find_emails() == 0 and not calls and not L()["UCq"].get("email_search", {}).get("tries"),
       "budget used up: no YouTube call, no try counted")
    fill_quota(quota.DAILY_UNITS - quota.RESERVE // 2 - quota.UPLOAD_COST - 2)  # one waiting upload needs nearly all that is left
    comments.uploads_waiting = lambda: 1
    calls.clear()
    ok(permissions.find_emails() == 0 and not calls, "the budget a waiting upload needs is never used for e-mail lookups")
    comments.uploads_waiting = lambda: 0
    ok(permissions.find_emails() == 1 and L()["UCq"]["email"] == "q@q-mail.in", "with no upload waiting, the same lookup runs")
    put([creator("UCs", "Stage2 S")]); channel("UCs", "nothing"); YT_UP["UUUCs"] = ["s1"]; YT_V["s1"] = {"title": "t", "description": "x"}
    fill_quota(quota.DAILY_UNITS - quota.RESERVE // 2 - 2)
    calls.clear()
    ok(permissions.find_emails() == 0 and not calls, "a search starts only when description + recent videos fit (3 units)")
    fill_quota(0)

    # ============================================================ 4. sending by itself
    set_setting("gmail_email", "owner@gmail.com")
    ok(get_setting(permissions.AUTO_KEY) == "0", "migration: owner's Gmail was not connected at start -> switch OFF")
    with SessionLocal() as s:
        s.query(Setting).filter(Setting.key.in_([permissions.AUTO_KEY, permissions.SINCE_KEY])).delete(synchronize_session=False); s.commit()
    ok(permissions.migrate_auto_send() == "1" and permissions.auto_on() and get_setting(permissions.SINCE_KEY),
       "migration: owner's Gmail connected -> switch ON, from now on only")
    permissions.save_auto(False)
    ok(permissions.migrate_auto_send() == "0", "migration never changes a choice already made")
    with tenant.use(t1):
        ok(not permissions.auto_on() and get_setting(permissions.AUTO_KEY) is None, "customers start OFF")
    old = (utcnow() - timedelta(days=3)).isoformat() + "Z"
    put([creator("OLD", "Backlog B", email="old@backlog.in", added_at=old)])
    SENT.clear(); set_setting(permissions.LAST_KEY, "")
    ok(permissions.auto_send_due() is None and not SENT, "switch OFF: nothing is sent by itself")
    ok(owner.post("/api/settings/perm-auto", json={"auto": True}).json()["auto"], "switch turned ON from the dashboard")
    ok(permissions.auto_send_due() is None and not SENT, "[review] the old backlog (added before the switch went on) is not mailed")
    hold = {c["channel_id"]: c.get("auto_hold") for c in owner.get("/api/permissions").json()}
    ok("before sending by itself was switched on" in (hold["OLD"] or ""), "[review] ... and the page says why")
    put([creator("UA1", "Ready One", email="one@ready.in"), creator("UA2", "Ready Two", email="two@ready.in"),
         creator("UC9", "Confirm Me", email="c9@me.in", email_trust="check")])
    ok(permissions.auto_send_due() == "UA1" and len(SENT) == 1 and L()["UA1"]["sent_by"] == "auto" and L()["UA1"]["status"] == "asked",
       "switch ON: a new creator with a trusted address is mailed by itself")
    rows = acl.activity(limit=20)
    ok(any("by itself" in (r["detail"] or "") and "Ready One" in r["detail"] for r in rows), "the send is in the activity log")
    ok(permissions.auto_send_due() is None and len(SENT) == 1, "2-hour spread: no second mail right away")
    set_setting(permissions.LAST_KEY, (utcnow() - timedelta(minutes=119)).isoformat() + "Z")
    ok(permissions.auto_send_due() is None, "still within 2 hours: nothing")
    set_setting(permissions.LAST_KEY, (utcnow() - timedelta(minutes=121)).isoformat() + "Z")
    ok(permissions.auto_send_due() == "UA2" and len(SENT) == 2, "after 2 hours: the next one")
    set_setting(permissions.LAST_KEY, "")
    ok(permissions.auto_send_due() is None and len(SENT) == 2 and not L()["UC9"].get("mailed"),
       "never twice; [review] an address found in videos / pages is never mailed by itself before Confirm")
    day = "gmail_sent:" + utcnow().strftime("%Y-%m-%d")
    put([creator("UA3", "Ready Three", email="three@ready.in")])
    set_setting(day, "5")
    ok(permissions.auto_send_due() is None and len(SENT) == 2, "daily cap (5) kept")
    set_setting(day, "0")
    never = [creator("N1", "Skipped S", status="skipped", email="n1@a.in"), creator("N2", "Declined D", status="declined", email="n2@a.in"),
             creator("N3", "Mailed M", status="asked", email="n3@a.in", mailed=True, thread_id="T"),
             creator("N4", "Shared A", email="same@a.in"), creator("N5", "Shared B", email="same@a.in", status="skipped"),
             creator("N6", "T-Series Music", email="label@a.in"), creator("N7", "Unsure U", email="n7@a.in", send_unsure=True),
             creator("N8", "Platform P", email="someone@youtube.com"),
             creator("N9", "Happy Vibes", email="info@sonymusic.in"), creator("N0", "Legacy L", email="legacy@a.in", email_trust=None)]
    for c in never:
        if c["channel_id"] == "N0":
            c.pop("email_trust")
    put(never)
    ok(permissions.auto_send_due() is None and len(SENT) == 2,
       "never: skipped, declined, already mailed, shared address, label, unsure, platform address, [review] label domain, old record")
    hold = {c["channel_id"]: c.get("auto_hold") for c in owner.get("/api/permissions").json()}
    ok("also belongs to Shared B" in (hold["N4"] or "") and "labels" in (hold["N6"] or "") and "labels" in (hold["N9"] or ""),
       "why it is not sent by itself is shown (incl. [review] a label's address domain): " + str(hold["N9"]))
    ok(owner.post("/api/permissions/N4/send").status_code == 200 and len(SENT) == 3, "the owner can still press Send for a shared address")
    put([creator("G1", "Gated G", email="g1@a.in")]); set_setting(permissions.LAST_KEY, "")
    real_allowed = acl.allowed
    acl.allowed = lambda k: k != "gmail.send"
    ok(permissions.auto_send_due() is None and len(SENT) == 3, "never when acl.allowed('gmail.send') is false")
    acl.allowed = lambda k: k != "permissions.manage"
    ok(permissions.auto_send_due() is None and len(SENT) == 3, "[review] never when 'Permission inbox' is switched off")
    acl.allowed = lambda k: k != "permissions.auto"
    ok(permissions.auto_send_due() is None and len(SENT) == 3 and not permissions.auto_public()["allowed"],
       "never when the Super Admin's switch 'Permission mails sent by themselves' is off")
    acl.allowed = real_allowed
    set_setting("gmail_email", "")
    ok(permissions.auto_send_due() is None, "never without Gmail connected")
    set_setting("gmail_email", "owner@gmail.com")
    # never twice, even when the owner presses Send while it goes out by itself
    put([creator("R1", "Race R", email="race@a.in")]); SENT.clear(); slow[0] = 0.3; set_setting(permissions.LAST_KEY, "")
    res = []
    th = [threading.Thread(target=lambda: res.append(permissions.auto_send_due())),
          threading.Thread(target=lambda: (time.sleep(0.05), res.append(owner.post("/api/permissions/R1/send").status_code)))]
    [t.start() for t in th]; [t.join() for t in th]
    ok(len(SENT) == 1 and "R1" in res and 400 in res, f"two sends at once -> one mail ({len(SENT)}, {res})")
    # [review] two rounds at once: one mail, the 2-hour mark taken before the send
    put([creator("R2", "Race Two", email="r2@a.in"), creator("R3", "Race Three", email="r3@a.in")]); SENT.clear(); set_setting(permissions.LAST_KEY, "")
    res = []
    th = [threading.Thread(target=lambda: res.append(permissions.auto_send_due())) for _ in range(3)]
    [t.start() for t in th]; time.sleep(0.1)
    mark = get_setting(permissions.LAST_KEY)
    [t.join() for t in th]
    slow[0] = 0.0
    ok(len(SENT) == 1 and sum(1 for x in res if x) == 1 and mark, f"[review] three rounds at once -> one mail, spread taken first ({len(SENT)})")
    # [review] the day's count is taken atomically
    set_setting(day, "0"); SENT.clear()
    out = []

    def safe(i):
        try:
            out.append(gmail.send(f"x{i}@a.in", "body text", "s"))
        except ValueError:
            pass
    th = [threading.Thread(target=safe, args=(i,)) for i in range(9)]
    [t.start() for t in th]; [t.join() for t in th]
    ok(len(SENT) == 5 and get_setting(day) == "5", f"[review] 9 sends at once -> exactly 5 go out ({len(SENT)})")
    set_setting(day, "0")
    # [review] an expired Gmail sign-in: "not sent", the count and the 2-hour mark given back, sending paused, owner told once
    put([creator("E1", "Expired E", email="e1@a.in")]); set_setting(permissions.LAST_KEY, ""); SENT.clear(); told.clear()
    real_service = gmail._service
    gmail._service = lambda: (_ for _ in ()).throw(RuntimeError("invalid_grant: Token has been expired or revoked"))
    ok(permissions.auto_send_due() is None and not SENT, "[review] expired sign-in: nothing sent")
    ok(not L()["E1"].get("send_unsure") and not L()["E1"].get("sending") and get_setting(day) == "0"
       and not get_setting(permissions.LAST_KEY), "[review] ... counted as NOT sent (count and 2-hour mark given back)")
    ok(permissions.paused() and len([t for tid, t, b in told if "reconnect Gmail" in t]) == 1, "[review] ... sending paused, owner told once")
    gmail._service = real_service
    ok(permissions.auto_send_due() is None and not SENT, "[review] paused: nothing more by itself until Gmail is reconnected")
    r = owner.post("/api/permissions/E1/send")
    ok(r.status_code == 200 and len(SENT) == 1, "the owner can still press Send while it is paused")
    ok(len([t for tid, t, b in told if "reconnect Gmail" in t]) == 1, "[review] the pause alert is not repeated")
    permissions.save_auto(True)
    ok(not permissions.paused(), "[review] switching it on again (or reconnecting Gmail) lifts the pause")
    # Gmail failed in the middle: never again by itself, and sending pauses
    put([creator("F1", "Fail F", email="fail@a.in"), creator("F2", "Fail Two", email="f2@a.in")]); set_setting(permissions.LAST_KEY, "")
    real_send = gmail.send
    gmail.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection reset"))
    permissions.auto_send_due()
    gmail.send = real_send
    ok(L()["F1"].get("send_unsure") and permissions.paused() and get_setting(permissions.LAST_KEY),
       "a send that may have gone out: never repeated by itself, [review] sending paused, the 2-hour mark kept")
    set_setting(permissions.LAST_KEY, "")
    ok(permissions.auto_send_due() is None and not L()["F2"].get("mailed"), "[review] after an unsure send nothing more goes out by itself")
    permissions.save_auto(True)
    # [review] one round at a time per account; the pool is bounded
    gate = threading.Event()
    f1 = permissions.submit_once(lambda: gate.wait(5))
    f2 = permissions.submit_once(lambda: None)  # same name '<lambda>' for the same account
    ok(f1 is not None and f2 is None, "[review] a round already queued / running for this account is not queued again")
    gate.set(); f1.result(5)
    ok(permissions.submit_once(lambda: None) is not None, "[review] ... and can be queued again once it finished")
    ok(permissions.MAX_QUEUED <= 100, "[review] the pool queue is bounded")
    # [review] a backup never switches it back on
    ok({"perm_auto_send", "perm_auto_since", "perm_auto_paused", "perm_mail_last"} <= backup.PROTECTED,
       "[review] the switch, its start, a pause and the 2-hour mark are kept on restore")
    # [review] the list is never written by two at once: a mail sent while the inbox is refreshed is not lost
    put([creator("K1", "Keep K", email="k1@a.in")]); set_setting(permissions.LAST_KEY, "")
    real_tr, real_ov = competitors.trending, competitors.overview
    competitors.trending = lambda q: (time.sleep(0.4), [{"channel_id": "UCnew", "channel": "Brand New", "video_id": "nnnnnnnnnnn",
                                                         "title": "a fun video", "views": 5, "cc": False}])[1]
    competitors.overview = lambda: {"outliers": []}
    real_submit = permissions.submit_once
    permissions.submit_once = lambda fn: None
    th = threading.Thread(target=permissions.refresh_inbox); th.start(); time.sleep(0.1)
    owner.post("/api/permissions/K1/send"); th.join()
    competitors.trending, competitors.overview, permissions.submit_once = real_tr, real_ov, real_submit
    ok(L()["K1"].get("mailed") and "UCnew" in L() and L()["UCnew"].get("added_at"), "[review] refresh + send at once: both kept")

    # ============================================================ 5. replies, read by themselves for every account
    def asked(cid, name, email, thread, **kw):
        return creator(cid, name, status="asked", email=email, thread_id=thread, mailed=True, asked_at=utcnow().isoformat() + "Z", **kw)

    told.clear()
    put([asked("Y1", "Yes Yash", "yash@a.in", "TY"), asked("N1", "No Nina", "nina@a.in", "TN"),
         asked("Q1", "Question Q", "q@a.in", "TQ"), asked("M1", "Money M", "m@a.in", "TM"), asked("S1", "Spoof S", "s@a.in", "TS"),
         asked("W1", "Waiting W", "w@a.in", "TW")])
    THREADS.update({(1, "TY"): ("Yash <yash@a.in>", "Haan bilkul, use kar lo!"), (1, "TN"): ("Nina <nina@a.in>", "No, please don't use my videos."),
                    (1, "TQ"): ("Q <q@a.in>", "What channel is this?"), (1, "TM"): ("M <m@a.in>", "Sure, pay 500 rs per clip"),
                    (1, "TS"): ("Evil <x@evil.com>", "Yes sure go ahead")})
    with tenant.use(t1):  # customer Asha: one creator, a yes in HER Gmail; Ravi: connected, nothing yet
        set_setting("gmail_email", "asha@example.com")
        put([asked("A1", "Asha's creator", "ac@a.in", "TA")])
    with tenant.use(t2):
        set_setting("gmail_email", "ravi@example.com")
        put([asked("B1", "Ravi's creator", "rc@a.in", "TA")])  # same thread id as Asha's, in another Gmail
    THREADS[(t1, "TA")] = ("AC <ac@a.in>", "Yes sure, go ahead!")
    scheduler.for_each_customer(scheduler.when_on("gmail.send", permissions.check_replies))()  # the timed job, every account
    cs = L()
    ok(cs["Y1"]["status"] == "granted" and cs["Y1"].get("auto_granted"), "plain yes -> permission source started by itself")
    with SessionLocal() as s:
        src = s.query(Channel).filter(Channel.resolved_url.like("%Y1")).first()
    ok(src and src.mode == "permission" and src.auto and "Haan bilkul" in (src.permission_note or ""), "the reply is the proof of the source")
    ok(cs["N1"]["status"] == "declined" and cs["N1"]["declined_by"] == "reply", "plain no -> 'declined'")
    ok(all(cs[c]["status"] == "replied" for c in ("Q1", "M1", "S1")), "question / money / another address -> 'replied — needs you'")
    ok(cs["W1"]["status"] == "asked", "no reply yet: unchanged")
    view = owner.get("/api/permissions").json()
    ok([c["channel_id"] for c in view[:3]] == sorted(["M1", "Q1", "S1"], key=lambda x: cs[x]["name"]) and view[0]["needs_you"]
       and view[0]["reply"] and view[0]["replied_at"], "'needs you' replies at the top, with the reply text and the date")
    mine = [t for tid, t, b in told if tid == 1 and "need you" in t]
    ok(len(mine) == 1 and "3 creators replied" in mine[0], f"ONE notice for the round ({len(mine)})")
    ok(any("said yes" in t for tid, t, b in told if tid == 1), "the owner is told about the automatic yes (with Undo)")
    rows = acl.activity(limit=50)
    ok(any("said yes by mail" in (r["detail"] or "") for r in rows) and any("said no by mail" in (r["detail"] or "") for r in rows),
       "yes and no are in the activity log")
    with tenant.use(t1):
        ok(L()["A1"]["status"] == "granted", "customer A: their reply (in their own Gmail) started their source")
        with SessionLocal() as s:
            ok(s.query(Channel).filter(Channel.mode == "permission").count() == 1, "customer A: the source is in A's own copy")
    with tenant.use(t2):
        ok(L()["B1"]["status"] == "asked" and "reply" not in L()["B1"], "customer B: a reply in A's Gmail never touches B")
        with SessionLocal() as s:
            ok(s.query(Channel).filter(Channel.mode == "permission").count() == 0, "customer B: no source made")
    ok("A1" not in L() and "B1" not in L(), "the owner's list is not touched by customers' replies")
    ok(any(tid == t1 and "said yes" in t for tid, t, b in told) and not any(tid == t2 for tid, t, b in told),
       "only the account whose creator replied is told")
    acts = acl.activity(account_id=a1, limit=20)
    ok(any("said yes by mail" in (r["detail"] or "") for r in acts) and not any("said yes" in (r["detail"] or "") for r in acl.activity(account_id=a2)),
       "the customer's activity log has their own line only")
    ok(c1.get("/api/permissions").json()[0]["status"] == "granted" and c2.get("/api/permissions").json()[0]["status"] == "asked",
       "each customer's dashboard shows their own updated list")
    sup.post(f"/api/admin/users/{a1}/view")
    v = sup.get("/api/permissions").json()
    ok(len(v) == 1 and v[0]["channel_id"] == "A1" and v[0]["status"] == "granted", "the Super Admin's 'View as' shows the same for that customer")
    ok(permissions.check_replies() == 0, "a reply is handled once")
    set_setting(permissions.LAST_KEY, "")
    ok(permissions.auto_send_due() is None and owner.post("/api/permissions/N1/send").status_code == 400, "a 'no' is never mailed again")
    ok(not permissions.plain_no({"email": "a@a.in"}, {"from": "a@a.in", "snippet": "No problem, go ahead"}), "'No problem, go ahead' is not a no")
    ok(not permissions.plain_no({"email": "a@a.in"}, {"from": "a@a.in", "snippet": "No, unless you pay me"}), "a no with money waits for the owner")
    ok(permissions.plain_no({"email": "a@a.in"}, {"from": "A <a@a.in>", "snippet": "Nahi bhai, mat use karo"}), "a Hinglish no is a no")

    # [review] auto-replies, conditions, quotes, look-alike senders, spam
    c = {"email": "contact@creator.in"}
    for s in ["I am out of the office until Monday with limited access to email. You can reach my manager at m@x.com",
              "Thanks for your email! I am currently travelling. You may contact my team at team@creator.in for anything urgent.",
              "Thank you for reaching out. Our team will get back to you within 48 hours. Okay!",
              "Okay. Our rates for usage start at 5000", "Yes, payment first", "Sure ₹5000 per clip", "ok, charges apply",
              "Yes, conditions apply", "Yes for 2 videos", "Okay noted, our team will revert", "Yes, fees apply",
              "Thanks! Auto-reply: I'm away, you can WhatsApp 98xxxx", "Hi, sure! Who is this?", "Yes. Business enquiries only. Not for fan mail."]:
        ok(not permissions.plain_yes(c, {"from": "Creator <contact@creator.in>", "snippet": s}), "[review] not a plain yes: " + s[:50])
    ok(permissions.plain_yes(c, {"from": "contact@creator.in", "snippet": "Yes. Sent from Outlook From: Funny Gunction Sent: Monday Subject: Quick question Hey Rahul, Would you be okay"}),
       "[review] an Outlook-quoted original is cut (the yes above it counts)")
    ok(permissions.plain_yes(c, {"from": "contact@creator.in", "snippet": "Sure, you can. Please give credit. Sent from my iPhone"}),
       "[review] a phone signature is cut")
    ok(not permissions.plain_yes(c, {"from": "Spoof <xcontact@creator.in.evil.com>", "snippet": "yes"})
       and not permissions.plain_yes(c, {"from": "contact@creator.in.evil.com", "snippet": "yes"}), "[review] exact sender only (no look-alikes)")
    told.clear()
    put([asked("H1", "Header H", "h@a.in", "TH"), asked("X1", "Text OOO", "x@a.in", "TX"), asked("P1", "Spam P", "p@a.in", "TP"),
         asked("U1", "Unconfirmed U", "u@a.in", "TU", email_trust="check"), asked("G1", "Legacy G", "g@a.in", "TG")])
    cs = L(); cs["G1"].pop("email_trust"); put(cs.values())
    THREADS[(1, "TH")] = [msg("h1", "H <h@a.in>", "Yes sure, go ahead", Auto_Submitted="auto-replied")]
    THREADS[(1, "TX")] = ("X <x@a.in>", "I am out of the office until Monday. Okay!")
    THREADS[(1, "TP")] = [msg("p1", "P <p@a.in>", "Yes sure, go ahead", labels=("SPAM",))]
    THREADS[(1, "TU")] = ("U <u@a.in>", "Yes sure, go ahead")
    THREADS[(1, "TG")] = ("G <g@a.in>", "Yes sure, go ahead")
    permissions.check_replies()
    cs = L()
    ok(cs["H1"]["status"] == "asked", "[review] an auto-reply by its headers (Auto-Submitted) is never taken as a yes")
    ok(cs["X1"]["status"] == "asked" and cs["X1"].get("auto_reply"), "[review] an out-of-office text is passed over (noted on the page)")
    ok(cs["P1"]["status"] == "asked", "[review] a message in Spam is ignored")
    ok(cs["U1"]["status"] == "replied" and not cs["U1"].get("auto_granted"), "[review] a yes from an unconfirmed address waits for the owner")
    ok(cs["G1"]["status"] == "granted", "a yes from an address saved before the check (old record) works as before")
    THREADS[(1, "TH")] = [msg("h1", "H <h@a.in>", "Yes sure, go ahead", Auto_Submitted="auto-replied"),
                          msg("h2", "H <h@a.in>", "Haan bilkul, use kar lo")]
    THREADS[(1, "TX")] = [msg("r-TX", "X <x@a.in>", "I am out of the office until Monday. Okay!"), msg("x2", "X <x@a.in>", "Yes go ahead")]
    permissions.check_replies()
    cs = L()
    ok(cs["H1"]["status"] == "granted" and cs["X1"]["status"] == "granted", "[review] the thread keeps being read after an auto-reply")
    for hdr in ({"X_Autoreply": "yes"}, {"X_Autorespond": "1"}, {"Precedence": "bulk"}, {"Precedence": "auto_reply"}, {"Precedence": "list"}):
        ok(gmail.is_auto(msg("z", "a@b.in", "yes", **hdr)), "[review] automatic by header: " + str(hdr))
    ok(not gmail.is_auto(msg("z", "a@b.in", "yes", Auto_Submitted="no")), "[review] Auto-Submitted: no is a person")

    # ============================================================ 6. one daily summary per account
    told.clear()
    set_setting(permissions.SUMMARY_KEY, (utcnow() - timedelta(days=1)).isoformat() + "Z")
    with tenant.use(t2):
        set_setting(permissions.SUMMARY_KEY, (utcnow() - timedelta(minutes=1)).isoformat() + "Z")
    scheduler.for_each_customer(permissions.daily_summary)()
    own = [t for tid, t, b in told if tid == 1]
    ok(len(own) == 1 and "replied" in own[0] and "said yes" in own[0] and "said no" in own[0] and "mailed" in own[0],
       "owner: ONE summary (mailed / replied / yes / no): " + (own[0].replace("\n", " ") if own else ""))
    ok(len([1 for tid, t, b in told if tid == t1]) == 1 and not any(tid == t2 for tid, t, b in told),
       "each account gets its own summary; nothing on a day with nothing to tell")
    told.clear()
    scheduler.for_each_customer(permissions.daily_summary)()
    ok(not told, "not twice for the same news")

    # ============================================================ 7. tenant isolation of the search and the switch
    put([creator("O1", "Owner creator", email="o1@a.in")])
    with tenant.use(t1):
        put([creator("CA", "Customer creator")])
        channel("CA", "Business: ca [at] customer [dot] in")
        ok(permissions.find_emails() == 1 and L()["CA"]["email"] == "ca@customer.in", "customer A's search fills A's list")
        SENT.clear()
        permissions.email_round()
        ok(not SENT, "customer A's switch is OFF: nothing sent by itself for A (owner's switch is ON)")
    ok("CA" not in L(), "the owner's list never gets a customer's creator")
    with tenant.use(t2):
        ok(c2.post("/api/settings/perm-auto", json={"auto": True}).json()["auto"] is True, "a customer can switch it on")
        owner.post(f"/api/admin/users/{a2}/access", json={"perms": {"gmail.send": False}})
        r = c2.post("/api/settings/perm-auto", json={"auto": True})
        ok(r.status_code == 403, "switching ON needs 'Permission mails to creators' for the account")
        ok(c2.post("/api/settings/perm-auto", json={"auto": False}).status_code == 200, "switching OFF is always possible")
        owner.post(f"/api/admin/users/{a2}/access", json={"perms": {"gmail.send": True, "permissions.auto": False}})
        r = c2.post("/api/settings/perm-auto", json={"auto": True})
        ok(r.status_code == 403 and "sent by themselves" in r.text, "the Super Admin's switch 'permissions.auto' stops switching it ON")
        ok(c2.post("/api/settings/perm-auto", json={"auto": False}).status_code == 200, "... OFF still possible")
    keys = [k for k, *_ in acl.PERMS]
    ok(acl.DEFAULT.get("permissions.auto") is True and keys.index("permissions.auto") == keys.index("permissions.manage") + 1,
       "the new switch sits next to the other permission switches, on by default")

    # ============================================================ 8. routes, timed jobs, page
    ok(acl.rule_for("GET", "/api/settings/perm-auto")[0] == "page.ideas" and acl.rule_for("POST", "/api/settings/perm-auto")[0] == "inside:permissions.auto"
       and acl.rule_for("POST", "/api/permissions/UCx/confirm-email")[0] == "permissions.manage", "the new addresses are in acl.ROUTES")
    real = {(m, r.path) for r in main.app.routes if isinstance(r, APIRoute) for m in r.methods if m not in ("HEAD", "OPTIONS")}
    ok(all(acl.rule_for(m, p) is not None for m, p in real), "every route still has a decision in acl.ROUTES")
    import inspect
    src = inspect.getsource(scheduler.start)
    ok('when_on("gmail.send", check_replies)' in src and "add(email_round," in src and "perm_summary" in src
       and "executor.submit(check_replies" not in src and "executor.submit(email_round" not in src,
       "timed jobs: replies (every account), e-mail round, daily summary — in the scheduler's thread, never the render worker")
    html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
    ok("Looking for e-mail…" in html and "No public e-mail found (tried:" in html and "found in: " in html, "page: search states")
    ok('id="pm-auto"' in html and "Send permission mails by themselves" in html, "page: the switch")
    ok("loadPerms().catch(()=>{})},45000)" in html and "Replied — needs you" in html, "page: the inbox updates by itself")
    ok("please confirm" in html and "data-pmconfirm" in html and "Paused — reconnect Gmail" in html, "[review] page: Confirm button and pause note")

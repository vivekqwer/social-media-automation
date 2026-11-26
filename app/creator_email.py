"""Finding a creator's public e-mail address by itself, from public pages only.

Tried in this order (each stage only when the one before found nothing usable):
  1. the channel description                       (YouTube Data API channels.list: 1 unit per 50 channels)
  2. the descriptions of their recent videos       (playlistItems.list + videos.list: 2 units per creator)
  3. the pages their channel description links to  (their own website: the homepage plus one obvious contact page on
                                                    the same domain; a link page such as linktr.ee; their Instagram)

Never: the hidden "business e-mail" on YouTube's About tab (sign-in + CAPTCHA), anybody's cookies, any CAPTCHA.
Website fetches come from the server, with short timeouts, a size cap, an honest User-Agent and robots.txt respected.
A site that blocks the server is simply "not found".

This module only reads and judges text; app/permissions.py decides when to search and stores what was found.
"""
import ipaddress
import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

UA = "RajiStudio-ContactFinder/1.0 (reads public contact pages only; respects robots.txt)"
UA_TOKEN = "RajiStudio-ContactFinder"
TIMEOUT = 5.0  # seconds per request (connect / read)
MAX_BYTES = 300_000  # a page is read up to this size, never more
ROBOTS_MAX = 64_000
MAX_REDIRECTS = 3
BUDGET_S = 20.0  # all page reads for one creator together
RECENT_VIDEOS = 8

STAGES = ("description", "videos", "website")
LABEL_CHANNEL = "their channel description"

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,24}")
URL_RX = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]{}|]+", re.I)
_AT = r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|<\s*at\s*>|\[\s*@\s*\]|\(\s*@\s*\))\s*"
_DOT = r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|<\s*dot\s*>|\[\s*\.\s*\]|\(\s*\.\s*\))\s*"

# the platforms themselves, placeholders, and junk that website code is full of
PLATFORM_WORDS = {"youtube", "youtu", "google", "instagram", "facebook", "fb", "linktr", "linktree", "example",
                  "sentry", "wixpress", "domain", "yourdomain", "email", "mysite", "website", "test"}
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "outlook.com", "hotmail.com",
             "live.com", "icloud.com", "rediffmail.com", "protonmail.com", "proton.me", "zoho.com", "aol.com"}
IMAGE_EXT = ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "avif", "tif", "tiff", "heic", "mp4", "js", "css")
NOREPLY = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer[-_.]?daemon|postmaster|bounce[s]?)\b", re.I)
GOOD_WORDS = re.compile(r"business|\bbiz\b|collab|collaboration|contact|enquir|inquir|booking|partnership|promotion|"
                        r"brand deals?|work with|pr\b|management|manager", re.I)
GOOD_LOCAL = re.compile(r"business|collab|contact|booking|partner|promo|enquir|inquir|work|team|manage", re.I)

LINK_PAGES = {"linktr.ee": "linktr.ee", "beacons.ai": "beacons.ai", "bio.link": "bio.link", "campsite.bio": "campsite.bio",
              "lnk.bio": "lnk.bio", "taplink.cc": "taplink.cc", "solo.to": "solo.to", "linkin.bio": "linkin.bio",
              "carrd.co": "carrd.co", "hoo.be": "hoo.be", "allmylinks.com": "allmylinks.com"}
# sites that are never "their own website" (social networks, shops, link shorteners, stores)
NOT_OWN = ("youtube.com", "youtu.be", "google.com", "goo.gl", "g.co", "facebook.com", "fb.com", "fb.me", "twitter.com",
           "x.com", "t.co", "tiktok.com", "whatsapp.com", "wa.me", "t.me", "telegram.me", "discord.gg", "discord.com",
           "snapchat.com", "threads.net", "pinterest.com", "linkedin.com", "reddit.com", "spotify.com", "apple.com",
           "amazon.in", "amazon.com", "amzn.to", "flipkart.com", "fkrt.it", "myntra.com", "bit.ly", "tinyurl.com",
           "rb.gy", "cutt.ly", "shorturl.at", "is.gd", "ow.ly", "play.google.com", "apps.apple.com", "patreon.com",
           "paypal.me", "paypal.com", "razorpay.me", "instamojo.com", "moj.com", "sharechat.com", "josh.in",
           "twitch.tv", "kick.com", "github.com", "wikipedia.org", "imdb.com", "jiosaavn.com", "gaana.com",
           "wynk.in", "music.apple.com", "soundcloud.com", "medium.com", "blogspot.com", "wordpress.com")
CONTACT_PATH = re.compile(r"/(contact|contact-us|contactus|business|collab|collaborate|work-with-(me|us)|enquir\w*|"
                          r"inquir\w*|about|about-us|impressum)(/|\.html?|$)", re.I)


# words before an address that say it is somebody else's (sponsor, music, guest, shop)
SPONSOR_WORDS = re.compile(r"sponsor\w*|thanks to|use (my )?code|coupon|discount|% ?off|affiliate|music|song|track|"
                           r"guest|\bft\b|\bfeat\w*|friend|camera|gear|powered by|in association|paid promotion|"
                           r"brought to you|partnered with|presented by|courtesy", re.I)
NAME_STOP = {"official", "channel", "vlogs", "vlog", "gaming", "studio", "studios", "india", "indian", "music", "the",
             "tv", "films", "media", "entertainment", "videos", "shorts", "live", "show", "comedy", "kitchen", "food",
             "recipes", "creator", "creations", "productions", "network", "world", "life", "team", "hindi"}


# ---------------------------------------------------------------- reading addresses out of text
def deobfuscate(text: str) -> str:
    """'name [at] domain [dot] com' / 'name (at) domain (dot) com' / 'name[@]domain.com' -> name@domain.com
    (bracketed forms only: "Subscribe @ funny.shorts" or "look at this dot com" never become addresses)"""
    t = unescape(text or "")
    t = re.sub(r"([A-Za-z0-9._%+-])" + _AT + r"([A-Za-z0-9-])", r"\1@\2", t, flags=re.I)
    return re.sub(r"([A-Za-z0-9-])" + _DOT + r"([A-Za-z0-9-])", r"\1.\2", t, flags=re.I)


def reject_reason(email: str) -> str | None:
    """Why an address is never used (None = it may be used)."""
    e = (email or "").strip().lower()
    if "@" not in e:
        return "not an address"
    local, _, dom = e.rpartition("@")
    labels = dom.split(".")
    if labels[-1] in IMAGE_EXT or re.search(r"@\d+x\.", e) or re.search(r"\.(png|jpe?g|gif|webp|svg)$", e):
        return "an image file name"
    if NOREPLY.search(local):
        return "a no-reply address"
    if any(lb in PLATFORM_WORDS for lb in labels[:-1]) or dom in ("linktr.ee",):
        return "an address of the platform itself"
    if len(local) > 64 or not re.fullmatch(r"[a-z]{2,24}", labels[-1]):
        return "not an address"
    if re.fullmatch(r"[0-9a-f]{20,}", local):  # tracking ids in page code
        return "not an address"
    return None


def _domain_of(url: str) -> str:
    h = (urlparse(url if "://" in url else "http://" + url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _same_site(a: str, b: str) -> bool:
    return bool(a) and _domain_of(a) == _domain_of(b)


def _compact(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def name_keys(*names: str) -> list[str]:
    """What a creator is called, squeezed (channel name, @handle, and each long word of them)."""
    keys = []
    for n in names:
        n = (n or "").strip().lstrip("@")
        whole = _compact(n)
        if len(whole) >= 4 and whole not in NAME_STOP:
            keys.append(whole)
        for w in re.split(r"[^A-Za-z0-9]+", n):
            w = w.lower()
            if len(w) >= 4 and w not in NAME_STOP:
                keys.append(w)
    return list(dict.fromkeys(keys))


def matches_name(text: str, keys: list[str]) -> bool:
    s = _compact(text)
    return bool(s) and any(k in s or (len(s) >= 5 and s in k) for k in keys)


def site_label(domain: str) -> str:
    """shop.rajfoods.co.in -> shoprajfoods (the name part, without www / com / co / in ...)"""
    parts = [p for p in domain.lower().split(".") if p not in ("www", "m")]
    while len(parts) > 1 and (len(parts[-1]) <= 3 or parts[-1] in ("store", "shop", "online", "site", "info")):
        parts.pop()
    return "".join(parts)


def own_domains(urls: list[str], names: list[str] | tuple = ()) -> set[str]:
    """The creator's own websites among the links: not social networks, shops, shorteners or link pages, AND named
    like the channel (a sponsor's or a shop's link never counts as their website)."""
    keys = name_keys(*names)
    out = set()
    for u in urls:
        d = _domain_of(u)
        if not d or "." not in d or d in LINK_PAGES or any(d == n or d.endswith("." + n) for n in NOT_OWN) \
                or "instagram.com" in d:
            continue
        if keys and matches_name(site_label(d), keys):
            out.add(d)
    return out


def addresses(text: str) -> list[tuple[str, str]]:
    """[(address, the words before it on its line)] in the order they appear; bad ones already left out."""
    t = deobfuscate(text)
    seen, out, prev = set(), [], 0
    for m in EMAIL_RX.finditer(t):
        e = m.group(0).strip(".").lower()
        before = t[max(prev, m.start() - 60):m.start()].rsplit("\n", 1)[-1]  # its own label only (same line)
        prev = m.end()
        if e in seen or reject_reason(e):
            continue
        seen.add(e)
        out.append((e, before))
    return out


def judge(email: str, before: str, own: set[str], keys: list[str], foreign: set[str] = frozenset()) -> dict:
    """How much an address looks like the creator's own business address."""
    local, _, dom = email.partition("@")
    on_own = any(dom == d or dom.endswith("." + d) for d in own)
    named = matches_name(local, keys) or (dom not in FREE_MAIL and matches_name(site_label(dom), keys))
    labelled = bool(GOOD_WORDS.search(before or ""))
    other = bool(SPONSOR_WORDS.search(before or "")) or any(dom == d or dom.endswith("." + d) for d in foreign)
    sc = 4 * on_own + 5 * named + 2 * labelled + int(bool(GOOD_LOCAL.search(local))) - 10 * other
    return {"email": email, "score": sc, "own": on_own, "named": named, "labelled": labelled, "other": other}


def candidates(text: str, names: list[str] | tuple = (), own: set[str] | None = None) -> list[dict]:
    """Every usable address in a text, best first. Ones that look like someone else's (next to sponsor / music /
    guest words, or on the domain of a linked company) are left out."""
    keys = name_keys(*names)
    urls = links(text)
    own = set(own) if own is not None else own_domains(urls, names)
    foreign = {_domain_of(u) for u in urls} - own
    found = [judge(e, b, own, keys, foreign) for e, b in addresses(text)]
    found = [dict(f, i=i) for i, f in enumerate(found) if not f["other"]]
    return sorted(found, key=lambda f: (-f["score"], f["i"]))


def best(text: str, own: set[str] | None = None, names: list[str] | tuple = ()) -> str | None:
    """The best address in a text (named like the channel / own domain / labelled first, else the first), or None."""
    c = candidates(text, names, own)
    return c[0]["email"] if c else None


def description_pick(text: str, names: list[str] | tuple = ()) -> tuple[str | None, bool]:
    """(address, trusted) from the channel's own description. Trusted = named like the channel or on its own domain,
    or labelled business / collab / contact when it is the only address there. Only a trusted address may be
    mailed by itself; anything else waits for the owner to confirm it."""
    c = candidates(text, names)
    if not c:
        return None, False
    top = c[0]
    return top["email"], bool(top["named"] or top["own"] or (top["labelled"] and len(c) == 1))


def from_videos(videos: list[dict], own: set[str] | None = None, names: list[str] | tuple = ()) -> tuple[str, str] | None:
    """(address, title of the video it is in) from recent video descriptions. Video descriptions also name other
    people (sponsors, music, guests), so an address counts only when it is named like the channel / on its own
    domain, or in two or more of their videos (their usual footer), and never when it stands next to sponsor /
    music / guest words. Found here = please confirm: never mailed by itself."""
    own = own or set()
    keys = name_keys(*names)
    counts: dict[str, int] = {}
    first: dict[str, tuple] = {}
    bad: set[str] = set()
    for v in videos:
        text = v.get("description") or ""
        foreign = {_domain_of(u) for u in links(text)} - own
        for e, before in addresses(text):
            j = judge(e, before, own, keys, foreign)
            if j["other"]:
                bad.add(e)
                continue
            counts[e] = counts.get(e, 0) + 1
            if e not in first or j["score"] > first[e][1]["score"]:
                first[e] = (v.get("title") or "", j, len(first))
    ok = [(e, t, j, i) for e, (t, j, i) in first.items() if e not in bad and (j["named"] or j["own"] or counts[e] >= 2)]
    if not ok:
        return None
    e, t, _j, _i = max(ok, key=lambda x: (x[2]["score"], counts[x[0]], -x[3]))
    return e, t


def short_title(t: str, n: int = 60) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return (t[:n].rsplit(" ", 1)[0] + "…") if len(t) > n else t


def links(text: str) -> list[str]:
    out = []
    for m in URL_RX.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!)")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u not in out:
            out.append(u)
    return out


# ---------------------------------------------------------------- fetching public pages (server side)
_transport = None  # tests put an httpx.MockTransport here; nothing else changes
DNS_TIMEOUT = 3.0
_dns_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns")
_BAD_NETS = [ipaddress.ip_network(n) for n in ("::/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32",
                                              "100.64.0.0/10", "192.0.0.0/24", "198.18.0.0/15")]


def _resolve(host: str) -> list[str]:
    return [a[4][0] for a in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def _ip_ok(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:  # ::ffff:127.0.0.1 is 127.0.0.1
        return _ip_ok(str(a.ipv4_mapped))
    if any(a.version == n.version and a in n for n in _BAD_NETS):  # IPv4 hidden inside IPv6, shared / test ranges
        return False
    return a.is_global and not (a.is_multicast or a.is_reserved or a.is_link_local or a.is_loopback or a.is_private
                                or a.is_unspecified)


def _public_host(host: str) -> bool:
    """Only public internet addresses (never this server, the local network or cloud metadata)."""
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ips = _dns_pool.submit(_resolve, host).result(timeout=DNS_TIMEOUT)
    except Exception:  # noqa: BLE001  (no answer in time, or no such name)
        return False
    return bool(ips) and all(_ip_ok(ip) for ip in ips)


def _client() -> httpx.Client:
    kw = {"timeout": httpx.Timeout(TIMEOUT), "headers": {"User-Agent": UA, "Accept": "text/html,text/plain;q=0.9"},
          "follow_redirects": False, "trust_env": False}  # never the server's proxy settings / .netrc
    if _transport is not None:
        kw["transport"] = _transport
    return httpx.Client(**kw)


def _url_ok(url: str) -> bool:
    u = urlparse(url)
    try:
        port = u.port
    except ValueError:
        return False
    return u.scheme in ("http", "https") and port in (None, 80, 443) and not u.username and not u.password \
        and _public_host((u.hostname or "").lower())


def _get(c: httpx.Client, url: str, cap: int, deadline: float | None = None) -> tuple[int, str, bytes, str]:
    """(status, content type, body up to cap bytes, final url). Redirects are followed only on the same site.
    deadline (time.monotonic) is a hard wall-clock end: a slow page is cut there (status 0 = not read)."""
    start = url
    deadline = deadline or (time.monotonic() + 2 * TIMEOUT)
    for _ in range(MAX_REDIRECTS + 1):
        if not _url_ok(url) or time.monotonic() > deadline:
            return 0, "", b"", url
        with c.stream("GET", url) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                nxt = urljoin(url, r.headers.get("location", ""))
                if not _same_site(nxt, start):
                    return r.status_code, "", b"", url  # never follow a redirect to another site
                url = nxt
                continue
            ctype = r.headers.get("content-type", "").lower()
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) >= cap:
                    del body[cap:]
                    break
                if time.monotonic() > deadline:  # trickling page: stop at the deadline
                    return 0, "", b"", url
            return r.status_code, ctype, bytes(body), url
    return 0, "", b"", url


class Fetcher:
    """One creator's page reads: robots.txt honoured per site, every page under the size cap, all of them within
    one time budget."""

    def __init__(self, budget_s: float = BUDGET_S):
        self.robots: dict[str, RobotFileParser | None] = {}
        self.pages = 0
        self.notes: list[str] = []
        self.t0, self.budget = time.monotonic(), budget_s

    def _deadline(self) -> float:
        return min(self.t0 + self.budget, time.monotonic() + 2 * TIMEOUT)

    def _robots(self, c, url: str) -> RobotFileParser | None:
        u = urlparse(url)
        key = f"{u.scheme}://{u.netloc}"
        if key in self.robots:
            return self.robots[key]
        rp = RobotFileParser()
        try:
            st, _ct, body, _u = _get(c, key + "/robots.txt", ROBOTS_MAX, self._deadline())
        except Exception:  # noqa: BLE001
            st, body = -1, b""
        if st == 200:
            rp.parse(body.decode("utf-8", "replace").splitlines())
        elif 400 <= st < 500 and st not in (401, 403):
            rp.allow_all = True  # no robots.txt: allowed
        else:  # cannot read the rules (refused, server error, sent elsewhere): do not fetch (the careful choice)
            rp.disallow_all = True
        self.robots[key] = rp
        return rp

    def page(self, url: str) -> tuple[str, str] | None:
        """(html, final url) of a public page, or None (blocked, too far, not allowed, not a web page)."""
        if time.monotonic() - self.t0 > self.budget:  # one creator's pages never take longer than this
            self.notes.append(f"{_domain_of(url)}: not read (time is up)")
            return None
        try:
            with _client() as c:
                if not _url_ok(url):
                    self.notes.append(f"{_domain_of(url)}: not a public address")
                    return None
                rp = self._robots(c, url)
                if rp is not None and not rp.can_fetch(UA_TOKEN, url):
                    self.notes.append(f"{_domain_of(url)}: robots.txt does not allow it")
                    return None
                st, ctype, body, final = _get(c, url, MAX_BYTES, self._deadline())
                self.pages += 1
        except Exception as e:  # noqa: BLE001  (timeouts, refused, TLS ...: just "not found")
            self.notes.append(f"{_domain_of(url)}: could not be read ({type(e).__name__})")
            return None
        if st != 200 or not ("html" in ctype or "text/plain" in ctype):
            self.notes.append(f"{_domain_of(url)}: no page ({st or 'blocked'})")
            return None
        return body.decode("utf-8", "replace"), final


def _mailtos(html: str) -> list[str]:
    out = []
    for m in re.finditer(r"""href\s*=\s*["']\s*mailto:([^"'?\s]+)""", html or "", re.I):
        e = unescape(m.group(1)).strip().lower()
        if EMAIL_RX.fullmatch(e) and not reject_reason(e) and e not in out:
            out.append(e)
    return out


def _visible(html: str) -> str:
    h = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", html or "")
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    return unescape(re.sub(r"<[^>]+>", " ", h))


def _page_best(html: str, own: set[str], names) -> str | None:
    keys = name_keys(*names)
    m = [judge(e, "contact", own, keys) for e in _mailtos(html)]
    if m:  # a mailto link is what the site itself offers: the best of those
        return max(enumerate(m), key=lambda x: (x[1]["score"], -x[0]))[1]["email"]
    return best(_visible(html), own, names)


def _contact_link(html: str, base: str) -> str | None:
    for m in re.finditer(r"""<a\b[^>]*?href\s*=\s*["']([^"'#]+)["']""", html or "", re.I):
        u = urljoin(base, unescape(m.group(1)).strip())
        if urlparse(u).scheme in ("http", "https") and _same_site(u, base) and CONTACT_PATH.search(urlparse(u).path or ""):
            return u.split("#")[0]
    return None


def search_links(urls: list[str], fetcher: Fetcher | None = None, names: list[str] | tuple = ()) -> tuple[str, str] | None:
    """Stage 3: (address, where) from the creator's OWN links, or None: websites named like the channel (never a
    sponsor's or a shop's), a link page or Instagram whose handle is the channel's. At most two sites and one link
    page. Found here = please confirm: never mailed by itself."""
    f = fetcher or Fetcher()
    keys = name_keys(*names)
    own = own_domains(urls, names)
    sites, linkpages, insta = [], [], []
    for u in urls:
        d = _domain_of(u)
        handle = (urlparse(u).path or "").strip("/").split("/")[0]
        if d in own and d not in [_domain_of(s) for s in sites]:
            sites.append(u)
        elif d in LINK_PAGES and (matches_name(handle, keys) or matches_name(d.split(".")[0], keys)):
            linkpages.append(u)
        elif d == "instagram.com" and re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle or "") and matches_name(handle, keys):
            insta.append(u)
    for u in sites[:2]:
        p = urlparse(u)
        home = f"{p.scheme}://{p.netloc}/"
        got = f.page(home)
        if not got:
            continue
        html, final = got
        e = _page_best(html, own, names)
        if e:
            return e, "their website homepage"
        nxt = _contact_link(html, final) or urljoin(home, "/contact")
        got = f.page(nxt) if _same_site(nxt, home) else None
        if got:
            e = _page_best(got[0], own, names)
            if e:
                return e, "their website contact page"
    for u in linkpages[:1]:
        got = f.page(u)
        if got:
            e = _page_best(got[0], own, names)
            if e:
                return e, f"their link page ({LINK_PAGES[_domain_of(u)]})"
    for u in insta[:1]:
        got = f.page(u)
        if got:
            m = re.search(r"""<meta[^>]+(?:name|property)=["'](?:og:)?description["'][^>]+content=["']([^"']+)""", got[0], re.I)
            e = best(unescape(m.group(1)) if m else "", own, names)
            if e:
                return e, "their Instagram bio"
    return None

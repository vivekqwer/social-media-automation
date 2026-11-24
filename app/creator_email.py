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
GOOD_WORDS = re.compile(r"business|collab|collaboration|contact|enquir|inquir|booking|partnership|promotion|"
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


# ---------------------------------------------------------------- reading addresses out of text
def deobfuscate(text: str) -> str:
    """'name [at] domain [dot] com' / 'name (at) domain (dot) com' / 'name[@]domain.com' -> name@domain.com"""
    t = unescape(text or "")
    t = re.sub(r"([A-Za-z0-9._%+-])" + _AT + r"([A-Za-z0-9-])", r"\1@\2", t, flags=re.I)
    t = re.sub(r"([A-Za-z0-9-])" + _DOT + r"([A-Za-z0-9-])", r"\1.\2", t, flags=re.I)
    t = re.sub(r"([A-Za-z0-9._%+-])\s+@\s+([A-Za-z0-9-]+\.[A-Za-z]{2,})", r"\1@\2", t)  # "name @ domain.com"
    return t


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


def own_domains(urls: list[str]) -> set[str]:
    """The creator's own websites among the links (not social networks, shops, shorteners or link pages)."""
    out = set()
    for u in urls:
        d = _domain_of(u)
        if not d or "." not in d or d in LINK_PAGES or any(d == n or d.endswith("." + n) for n in NOT_OWN) \
                or "instagram.com" in d:
            continue
        out.add(d)
    return out


def addresses(text: str) -> list[tuple[str, str]]:
    """[(address, the 60 characters before it)] in the order they appear; bad ones already left out."""
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


def score(email: str, before: str, own: set[str]) -> int:
    dom = email.rpartition("@")[2]
    s = 0
    if any(dom == d or dom.endswith("." + d) for d in own):
        s += 4  # on the creator's own domain
    if GOOD_WORDS.search(before or ""):
        s += 2  # labelled "business", "collab", "contact" ...
    if GOOD_LOCAL.search(email.partition("@")[0]):
        s += 2
    return s


def best(text: str, own: set[str] | None = None) -> str | None:
    """The best address in a text (own domain / labelled ones first, else the first one), or None."""
    found = addresses(text)
    if not found:
        return None
    own = own or set()
    return max(enumerate(found), key=lambda x: (score(x[1][0], x[1][1], own), -x[0]))[1][0]


def from_videos(videos: list[dict], own: set[str] | None = None) -> tuple[str, str] | None:
    """(address, title of the video it is in) from recent video descriptions. Video descriptions also name other
    people (sponsors, music, guests), so an address counts only when it is labelled (business / collab / contact),
    on the creator's own domain, or in two or more of their videos (their usual footer)."""
    own = own or set()
    counts: dict[str, int] = {}
    first: dict[str, tuple[str, int, int]] = {}
    for v in videos:
        for e, before in addresses(v.get("description") or ""):
            counts[e] = counts.get(e, 0) + 1
            sc = score(e, before, own)
            if e not in first or sc > first[e][1]:
                first[e] = (v.get("title") or "", sc, len(first))
    ok = [(e, t, sc, i) for e, (t, sc, i) in first.items() if sc >= 2 or counts[e] >= 2]
    if not ok:
        return None
    e, t, _sc, _i = max(ok, key=lambda x: (x[2], counts[x[0]], -x[3]))
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


def _resolve(host: str) -> list[str]:
    return [a[4][0] for a in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def _public_host(host: str) -> bool:
    """Only public internet addresses (never this server, the local network or cloud metadata)."""
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ips = _resolve(host)
    except Exception:  # noqa: BLE001
        return False
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return False
        if not a.is_global or a.is_multicast:
            return False
    return bool(ips)


def _client() -> httpx.Client:
    kw = {"timeout": httpx.Timeout(TIMEOUT), "headers": {"User-Agent": UA, "Accept": "text/html,text/plain;q=0.9"},
          "follow_redirects": False}
    if _transport is not None:
        kw["transport"] = _transport
    return httpx.Client(**kw)


def _url_ok(url: str) -> bool:
    u = urlparse(url)
    return u.scheme in ("http", "https") and u.port in (None, 80, 443) and not u.username and not u.password \
        and _public_host((u.hostname or "").lower())


def _get(c: httpx.Client, url: str, cap: int) -> tuple[int, str, bytes, str]:
    """(status, content type, body up to cap bytes, final url). Redirects are followed only on the same site."""
    start = url
    for _ in range(MAX_REDIRECTS + 1):
        if not _url_ok(url):
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
            return r.status_code, ctype, bytes(body), url
    return 0, "", b"", url


class Fetcher:
    """One creator's page reads: robots.txt honoured per site, every page under the size cap."""

    def __init__(self, budget_s: float = BUDGET_S):
        self.robots: dict[str, RobotFileParser | None] = {}
        self.pages = 0
        self.notes: list[str] = []
        self.t0, self.budget = time.monotonic(), budget_s

    def _robots(self, c, url: str) -> RobotFileParser | None:
        u = urlparse(url)
        key = f"{u.scheme}://{u.netloc}"
        if key in self.robots:
            return self.robots[key]
        rp = RobotFileParser()
        try:
            st, _ct, body, _u = _get(c, key + "/robots.txt", ROBOTS_MAX)
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
                st, ctype, body, final = _get(c, url, MAX_BYTES)
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


def _page_best(html: str, own: set[str]) -> str | None:
    m = _mailtos(html)
    text = _visible(html)
    if m:  # a mailto link is what the site itself offers: the best of those
        return max(enumerate(m), key=lambda x: (score(x[1], "contact", own), -x[0]))[1]
    return best(text, own)


def _contact_link(html: str, base: str) -> str | None:
    for m in re.finditer(r"""<a\b[^>]*?href\s*=\s*["']([^"'#]+)["']""", html or "", re.I):
        u = urljoin(base, unescape(m.group(1)).strip())
        if urlparse(u).scheme in ("http", "https") and _same_site(u, base) and CONTACT_PATH.search(urlparse(u).path or ""):
            return u.split("#")[0]
    return None


def search_links(urls: list[str], fetcher: Fetcher | None = None) -> tuple[str, str] | None:
    """Stage 3: (address, where) from the creator's own links, or None. At most two sites and one link page."""
    f = fetcher or Fetcher()
    own = own_domains(urls)
    sites, linkpages, insta = [], [], []
    for u in urls:
        d = _domain_of(u)
        if d in own and d not in [_domain_of(s) for s in sites]:
            sites.append(u)
        elif d in LINK_PAGES:
            linkpages.append(u)
        elif d == "instagram.com" and re.match(r"^/[A-Za-z0-9._]{1,30}/?$", urlparse(u).path or ""):
            insta.append(u)
    for u in sites[:2]:
        p = urlparse(u)
        home = f"{p.scheme}://{p.netloc}/"
        got = f.page(home)
        if not got:
            continue
        html, final = got
        e = _page_best(html, own)
        if e:
            return e, "their website homepage"
        nxt = _contact_link(html, final) or urljoin(home, "/contact")
        got = f.page(nxt) if _same_site(nxt, home) else None
        if got:
            e = _page_best(got[0], own)
            if e:
                return e, "their website contact page"
    for u in linkpages[:1]:
        got = f.page(u)
        if got:
            e = _page_best(got[0], own)
            if e:
                return e, f"their link page ({LINK_PAGES[_domain_of(u)]})"
    for u in insta[:1]:
        got = f.page(u)
        if got:
            m = re.search(r"""<meta[^>]+(?:name|property)=["'](?:og:)?description["'][^>]+content=["']([^"']+)""", got[0], re.I)
            e = best(unescape(m.group(1)) if m else "", own)
            if e:
                return e, "their Instagram bio"
    return None


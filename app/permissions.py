"""Permission inbox: creators of popular non-Creative-Commons videos, with a ready message asking to use their clips.

The creator's public e-mail is looked up by itself (app/creator_email.py: channel description -> recent video
descriptions -> their own linked pages). The mail goes out when the owner presses Send, or — only when the account
switched on "Send permission mails by themselves" — by itself: at most 5 a day, 1 every 2 hours, never twice, never to
skipped / declined creators, labels or studios, or an address shared with another creator in the list.
Replies are read by themselves for every account: a plain yes starts their source, a plain no marks them declined,
anything else waits for the owner ("replied — needs you"). When a creator says yes, one click (or a Telegram reply)
turns them into a permission source that clips and uploads their videos automatically, with credit.
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta

from . import tenant
from .db import Channel, Destination, SessionLocal, get_setting, set_setting, utcnow, main_destination
from .jobs import _LoggingExecutor

log = logging.getLogger(__name__)
from .alerts import note as _note  # a failure that is only logged also shows in Alerts -> System errors (never raises)
KEY = "creators"
PER_DAY = 5
# music labels, film studios and TV networks: they never give fan pages permission and their Content ID strikes
LABELS = re.compile(r"music|records|songs|films|movies|studios|pictures|t-series|zee|sony|star |colors|tips official|"
                    r"saregama|aditya|lahari|speed records|bhajan|bhakti|aarti|wave|yrf|dharma|netflix|prime video|news|"
                    r"rajshri|eros|shemaroo|ultra|venus|goldmines|pen movies|wynk|jio", re.I)

# Written like a person would write it: short, plain, a little informal, different for each creator,
# naming the video the owner actually liked. No emojis or polished marketing lines.
TEMPLATES = [  # always English (owner's choice); three versions so the mails are not identical
    ("Quick question about your videos",
     "Hey {name},\n\nI've been watching your videos for a while, the \"{video}\" one especially. I run a small channel "
     "where I post {about} Shorts.\n\nWould you be okay with me posting short clips from your videos there? I'd put your name and "
     "a link to the full video in every one.\n\nIf not, no worries at all. And if you ever want something taken down, just "
     "reply here and I'll do it straight away.\n\nThanks,\n{sign}"),
    ("About your video \"{video}\"",
     "Hi {name},\n\nI just watched \"{video}\" and really enjoyed it. I have a small YouTube channel where I post "
     "{about} Shorts.\n\nCould I use short clips (30-40 seconds) from your videos? Every clip would carry your name and a link "
     "to the original video, so people can find your channel.\n\nA simple yes or no is fine. If you say no, I won't use "
     "anything, and you can ask me to remove a clip at any time.\n\nThank you,\n{sign}"),
    ("A small request",
     "Hello {name},\n\nBig fan of your content. \"{video}\" was great. I run a small channel of {about} Shorts and "
     "I'd love to share a few short clips from your videos with full credit: your name and the link to the full video "
     "in each one.\n\nWould that be alright with you? Just reply yes or no. If it's a no, that's completely fine, and "
     "I'll take down anything you ask me to.\n\nThanks a lot,\n{sign}"),
]


def _clean_title(t: str) -> str:
    t = re.split(r"\s[|•/]\s|\s-\s", t or "")[0]  # "Title | Channel name" -> "Title"
    import unicodedata

    t = re.sub(r"#\S+", " ", t)  # no hashtags
    t = "".join(" " if unicodedata.category(ch)[0] == "S" or ch in "️|[]{}<>\"*_~^" or unicodedata.category(ch) in ("Co", "Cs")
                else ch for ch in t)  # no emojis / symbols (Hindi vowel signs are kept)
    t = re.sub(r"\s+", " ", t).strip(" -|,.")
    return (t[:60].rsplit(" ", 1)[0] if len(t) > 60 else t) or "aapki latest video"


def compose(name: str, video_title: str, cid: str) -> tuple[str, str]:
    """(subject, body) — the template is picked from the channel id, so each creator gets one version, always the same."""
    sign, about = mail_identity()
    subj, body = TEMPLATES[sum(map(ord, cid)) % len(TEMPLATES)]
    video = _clean_title(video_title)
    if video == "aapki latest video":  # nothing usable in the title
        video = "your latest video"
    return subj.format(video=video)[:150], body.format(name=(name or "there").strip(), video=video, about=about, sign=sign)


DEFAULT_SIGN, DEFAULT_ABOUT = "Funny Gunction", "funny"


def mail_identity() -> tuple[str, str]:
    """(name the mails are signed with, kind of channel) — set on the dashboard; the channel's own name is not used."""
    return get_setting("mail_sign") or DEFAULT_SIGN, get_setting("mail_about") or DEFAULT_ABOUT


MESSAGE = (
    "Namaste {name} ji! Main aapka bada fan hoon aur aapke videos ke chhote clips (Shorts) ek fan page par post karna chahta hoon — "
    "har clip me aapka naam, channel link aur original video ka link credit me rahega. Kya aap mujhe aapke videos ke clips "
    "use karne ki permission denge? Aap kabhi bhi mana kar sakte hain aur main turant hata dunga. Shukriya! 🙏\n\n"
    "Hi {name}! I'm a big fan and run a fan page. May I post short clips from your videos with full credit "
    "(your name, channel link and the original video link in every clip)? You can say no at any time and I'll remove them. Thank you!"
)


def _load() -> dict:
    raw = get_setting(KEY)
    return json.loads(raw) if raw else {}


def _save(d: dict):
    set_setting(KEY, json.dumps(d))


# ---------------------------------------------------------------- one writer at a time (per account)
_locks: dict[int, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock():
    """One lock per account: every read-change-write of the creators list goes through it, so no change (a sent mail,
    a reply, the owner's decision) is ever lost. YouTube / Gmail / web calls are made outside it."""
    tid = tenant.current()
    with _locks_guard:
        return _locks.setdefault(tid, threading.RLock())


def _update(cid: str, fn) -> dict | None:
    with _lock():
        d = _load()
        c = d.get(cid)
        if c is None:
            return None
        fn(c)
        _save(d)
        return c


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).rstrip("Z")) if v else None
    except ValueError:
        return None


def _now_iso() -> str:
    return utcnow().isoformat() + "Z"


# ---------------------------------------------------------------- the background pool (searches, replies)
lookup_pool = _LoggingExecutor(max_workers=2)  # e-mail searches and reply checks: never on the single render worker
MAX_QUEUED = 60  # at most this many rounds waiting or running, over all accounts
_busy: set = set()
_busy_lock = threading.Lock()


def submit_once(fn):
    """Queue fn for the running account in lookup_pool, unless the same round is already queued or running for it
    (or the pool is full). Returns the future, or None when nothing was queued."""
    key = (tenant.current(), getattr(fn, "__name__", repr(fn)))
    with _busy_lock:
        if key in _busy or len(_busy) >= MAX_QUEUED:
            return None
        _busy.add(key)

    def run():
        try:
            return fn()
        finally:
            with _busy_lock:
                _busy.discard(key)
    run.__name__ = key[1]
    try:
        return lookup_pool.submit(run)
    except Exception:
        with _busy_lock:
            _busy.discard(key)
        raise


def refresh_inbox() -> list[dict]:
    """Add today's creators of popular non-CC videos (max 5 new a day) and tell the owner on Telegram."""
    from . import strikes, youtube
    from .competitors import overview, trending

    candidates = []  # YouTube is read before the list is locked
    try:
        candidates += [v for v in trending("") if not v.get("cc")]
    except Exception as e:  # noqa: BLE001
        log.warning("permission inbox: trending: %s", str(e)[:150])
        _note("Permission inbox: reading trending videos", e)
    candidates += [{**v, "channel_id": v["channel_url"].rsplit("/", 1)[-1]} for v in overview().get("outliers", [])]
    struck = strikes.blocked()  # creators on the never-use list after a strike are never asked again
    new = []
    with _lock():
        d = _load()
        today = utcnow().strftime("%Y-%m-%d")
        added = [c for c in d.values() if c.get("added") == today]
        for v in candidates:
            cid = v.get("channel_id") or ""
            if len(added) + len(new) >= PER_DAY:
                break
            if not cid or cid in d or cid in struck or youtube.RISKY.search(v.get("title", "")) or LABELS.search(v.get("channel", "")):
                continue
            d[cid] = {"channel_id": cid, "name": v.get("channel", ""), "url": f"https://www.youtube.com/channel/{cid}",
                      "video_id": v["video_id"], "video_title": v.get("title", ""), "views": v.get("views", 0),
                      "status": "new", "added": today, "added_at": _now_iso()}
            d[cid]["subject"], d[cid]["message"] = compose(v.get("channel", ""), v.get("title", ""), cid)
            new.append(dict(d[cid]))
        _save(d)
    if new:  # their e-mail is looked up right away, away from the render worker (website reads can be slow)
        submit_once(email_round)
        try:
            from .telegram import esc, notify

            for c in new:
                notify(f"🤝 <b>Ask for permission:</b> {esc(c['name'])}\nTheir video: {esc(c['video_title'][:80])}\n"
                       f"Channel: {c['url']}\n\nMessage to send (copy):\n<code>{esc(c['message'])}</code>",
                       [[{"text": "📧 Add email & send", "callback_data": f"pe:{c['channel_id']}"},
                         {"text": "✅ They said yes", "callback_data": f"py:{c['channel_id']}"}],
                        [{"text": "Asked another way", "callback_data": f"pa:{c['channel_id']}"},
                         {"text": "Skip", "callback_data": f"ps:{c['channel_id']}"}]])
        except Exception:  # noqa: BLE001
            pass
    return new


EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _deobfuscate(t: str) -> str:
    from .creator_email import deobfuscate

    return deobfuscate(t)


def _budget_ok(units: int, waiting: int | None = None) -> bool:
    """A few YouTube read units fit today without touching what waiting uploads need (app/quota.py)."""
    from . import comments, quota

    if waiting is None:
        try:
            waiting = comments.uploads_waiting()
        except Exception:  # noqa: BLE001
            waiting = 0
    return quota.can_read(units, waiting)


def _yt_error(e: Exception, where: str):
    from . import quota

    if "quotaExceeded" in str(e) or "dailyLimitExceeded" in str(e):
        quota.exhaust()
    log.warning("permission inbox: %s: %s", where, str(e)[:150])
    _note("Permission inbox: " + where, e)


def _channel_text(it: dict) -> str:
    return (it.get("snippet") or {}).get("description", "") + "\n" + \
        ((it.get("brandingSettings") or {}).get("channel") or {}).get("description", "")


_desc_trust: dict = {}  # (account, channel id) -> (address, trusted) from the last lookup_emails


def lookup_emails(channel_ids: list[str]) -> dict:
    """{channel_id: public email} from channel descriptions (1 quota unit per 50 channels)."""
    from . import creator_email as ce, quota, youtube

    out = {}
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i:i + 50]
        if not _budget_ok(1):
            return out
        try:
            r = youtube.client().channels().list(part="snippet,brandingSettings", id=",".join(batch), maxResults=50).execute()
            quota.spend("read")
        except Exception as e:  # noqa: BLE001
            _yt_error(e, "looking up creators' emails")
            return out
        for it in r.get("items", []):
            sn = it.get("snippet") or {}
            e, trusted = ce.description_pick(_channel_text(it), [sn.get("title", ""), sn.get("customUrl", "")])
            if e:
                out[it["id"]] = e
                if len(_desc_trust) > 2000:  # only the picks of the moment are needed
                    _desc_trust.clear()
                _desc_trust[(tenant.current(), it["id"])] = (e, trusted, _channel_text(it), sn.get("customUrl", ""))
    return out


def add_creator(cid: str, name: str, video_id: str, video_title: str, views: int, email: str = "") -> dict:
    """Put a creator in the inbox (from the viral picks) with a ready mail about this exact video."""
    from . import creator_email as ce

    known = _desc_trust.pop((tenant.current(), cid), None)
    trust = "check"
    if known and email and known[0] == email.lower():
        e2, t2 = ce.description_pick(known[2], [name, known[3]])  # now with the channel's name from the board too
        trust = "channel" if known[1] or (e2 == known[0] and t2) else "check"
    with _lock():
        d = _load()
        c = d.get(cid)
        if not c:
            c = d[cid] = {"channel_id": cid, "name": name, "url": f"https://www.youtube.com/channel/{cid}", "video_id": video_id,
                          "video_title": video_title, "views": views, "status": "new", "added": utcnow().strftime("%Y-%m-%d"),
                          "added_at": _now_iso(), "email_checked": True}
            c["subject"], c["message"] = compose(name, video_title, cid)
        if email and not c.get("email"):
            c["email"], c["email_from"], c["email_trust"] = email, "their channel description", trust
        _save(d)
        return c


# ---------------------------------------------------------------- finding the e-mail by itself
SEARCH_TRIES = 3  # a creator still without an address is looked up again weekly, 3 times at most
SEARCH_EVERY = timedelta(days=7)
SEARCH_PER_ROUND = 10
LOOKING_STALE = timedelta(minutes=30)
TRUST_AUTO = ("channel", "hand")  # may be mailed by itself; "check" = found in videos / pages: the owner confirms first


def _search_due(c: dict, now: datetime) -> bool:
    if c.get("status") != "new" or c.get("email") or c.get("mailed"):
        return False
    s = c.get("email_search") or {}
    if int(s.get("tries") or 0) >= SEARCH_TRIES:
        return False
    if s.get("state") == "looking" and (_ts(s.get("started")) or now) > now - LOOKING_STALE:
        return False
    last = _ts(s.get("last"))
    return last is None or now - last >= SEARCH_EVERY


def search_view(c: dict) -> dict:
    """What the dashboard shows about the search for this creator's address."""
    s = c.get("email_search") or {}
    tried = s.get("tried") or []
    if c.get("email"):
        return {"state": "found", "from": c.get("email_from") or "added by hand",
                "confirm": c.get("email_trust") == "check" and not c.get("mailed")}
    if s.get("state") == "looking" and (_ts(s.get("started")) or utcnow()) > utcnow() - LOOKING_STALE:
        return {"state": "looking"}
    tries = int(s.get("tries") or 0)
    if tries >= SEARCH_TRIES:
        return {"state": "none", "final": True, "tried": tried, "tries": tries, "notes": s.get("notes") or []}
    if tries:
        nxt = (_ts(s.get("last")) or utcnow()) + SEARCH_EVERY
        return {"state": "none", "final": False, "tried": tried, "tries": tries, "next": nxt.isoformat() + "Z",
                "notes": s.get("notes") or []}
    return {"state": "queued" if c.get("status") == "new" else "idle"}


def _search_one(item: dict, waiting: int, name: str):
    """All stages for one creator: (email | None, where | None, trust, stages tried, notes), or None = put off (the
    YouTube budget is kept for uploads; no try is counted)."""
    from . import creator_email as ce, quota, youtube

    sn = item.get("snippet") or {}
    names = [name, sn.get("title", ""), sn.get("customUrl", "")]
    text = _channel_text(item)
    urls = ce.links(text)
    own = ce.own_domains(urls, names)
    e, trusted = ce.description_pick(text, names)
    if e:
        return e, ce.LABEL_CHANNEL, ("channel" if trusted else "check"), ["description"], []
    tried, notes = ["description"], []
    uploads = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if uploads:
        if not _budget_ok(2, waiting):
            return None
        try:
            pl = youtube.client().playlistItems().list(part="contentDetails", playlistId=uploads,
                                                       maxResults=ce.RECENT_VIDEOS).execute()
            quota.spend("read")
            ids = [x["contentDetails"]["videoId"] for x in pl.get("items", []) if (x.get("contentDetails") or {}).get("videoId")]
            vids = []
            if ids:
                vr = youtube.client().videos().list(part="snippet", id=",".join(ids[:ce.RECENT_VIDEOS])).execute()
                quota.spend("read")
                vids = [{"title": (v.get("snippet") or {}).get("title", ""),
                         "description": (v.get("snippet") or {}).get("description", "")} for v in vr.get("items", [])]
            hit = ce.from_videos(vids, own, names)
            if hit:
                return hit[0], f"description of video “{ce.short_title(hit[1])}”", "check", tried + ["videos"], notes
        except Exception as ex:  # noqa: BLE001
            _yt_error(ex, "reading a creator's recent videos")
            notes.append("their recent videos could not be read")
    else:
        notes.append("no recent videos to read")
    tried += ["videos", "website"]
    if not urls:
        notes.append("no website link on the channel")
        return None, None, "check", tried, notes
    f = ce.Fetcher()
    try:
        hit = ce.search_links(urls, f, names)
    except Exception as ex:  # noqa: BLE001
        hit = None
        notes.append(f"their pages could not be read ({type(ex).__name__})")
    notes += f.notes[:4]
    if hit:
        return hit[0], hit[1], "check", tried, notes
    return None, None, "check", tried, notes


def find_emails(only: str | None = None, deadline_s: float | None = None) -> int:
    """Look for each new creator's public e-mail: channel description, then recent video descriptions, then their own
    linked pages. Runs by itself for every new creator, then weekly (3 tries at most); only=<channel id> is the owner
    pressing "Find e-mail" (any time). Returns how many addresses were found."""
    import time as _time

    from . import comments, quota, youtube

    t0 = _time.monotonic()
    now = utcnow()
    d = _load()
    if only:
        todo = [c for c in d.values() if c["channel_id"] == only and c.get("status") in ("new", "asked")
                and not c.get("email") and not c.get("mailed")]
    else:
        todo = [c for c in d.values() if _search_due(c, now)][:SEARCH_PER_ROUND]
    if not todo:
        return 0
    try:
        waiting = comments.uploads_waiting()
    except Exception:  # noqa: BLE001
        waiting = 0
    if not _budget_ok(3, waiting):  # put off: no try is counted
        return 0
    ids = [c["channel_id"] for c in todo]
    names = {c["channel_id"]: c.get("name") or "" for c in todo}
    stamp = now.isoformat() + "Z"
    for cid in ids:
        _update(cid, lambda c: c.setdefault("email_search", {}).update(state="looking", started=stamp))

    def stop_looking(cid):
        _update(cid, lambda c: (c.setdefault("email_search", {}).update(state="idle")
                                if (c.get("email_search") or {}).get("state") == "looking" else None))

    try:
        r = youtube.client().channels().list(part="snippet,brandingSettings,contentDetails", id=",".join(ids),
                                             maxResults=50).execute()
        quota.spend("read")
    except Exception as e:  # noqa: BLE001
        _yt_error(e, "looking up creators' emails")
        for cid in ids:
            stop_looking(cid)
        return 0
    items = {it.get("id"): it for it in r.get("items", [])}
    found = 0
    for n, cid in enumerate(ids):
        if deadline_s is not None and n and _time.monotonic() - t0 > deadline_s:
            for rest in ids[n:]:  # the timed round does the rest
                stop_looking(rest)
            break
        res = _search_one(items.get(cid) or {}, waiting, names[cid])
        if res is None:
            stop_looking(cid)
            continue
        email, where, trust, tried, notes = res
        at = _now_iso()
        handle = ((items.get(cid) or {}).get("snippet") or {}).get("customUrl") or ""

        def save(c, email=email, where=where, trust=trust, tried=tried, notes=notes, at=at, handle=handle):
            s = c.setdefault("email_search", {})
            s.update(state="found" if email else "none", tries=int(s.get("tries") or 0) + 1, last=at, tried=tried,
                     notes=notes[:5])
            c["email_checked"] = True
            if handle:
                c["handle"] = handle
            if email and not c.get("email") and not c.get("mailed"):
                c.update(email=email, email_from=where, email_found_at=at, email_trust=trust)
        _update(cid, save)
        found += bool(email)
    return found


def confirm_email(cid: str) -> dict:
    """The owner checked an address found in videos / on a website: it may now be mailed by itself, and a plain yes
    from it may start the source by itself."""
    def ok(c):
        if not c.get("email"):
            raise ValueError("There is no address to confirm.")
        c["email_trust"] = "hand"
        c["email_confirmed_at"] = _now_iso()
    with _lock():
        c = _update(cid, ok)
    if c is None:
        raise KeyError(cid)
    return c


def email_round():
    """The timed round (every 30 minutes, every account): look up addresses that are due, then send at most one
    permission mail by itself when the account switched that on."""
    from . import acl

    if acl.feature("permissions.manage"):
        try:
            find_emails()
        except Exception as e:  # noqa: BLE001
            log.warning("email round: %s", str(e)[:150])
            _note("Permission inbox: looking for creators' e-mails", e)
    auto_send_due()


# ---------------------------------------------------------------- sending by itself (the account's switch)
AUTO_KEY = "perm_auto_send"  # "1" on, "0" / missing off (customers start OFF)
SINCE_KEY = "perm_auto_since"  # only creators added after the switch went on are mailed by themselves
PAUSE_KEY = "perm_auto_paused"  # {"at", "why"}: Gmail sign-in failed or a send may have gone out -> paused
LAST_KEY = "perm_mail_last"  # when the last permission mail went out (by hand or by itself)
MAIL_LOG = "perm_mail_log"  # [{at, cid, name, email, by}] (newest 200)
SUMMARY_KEY = "perm_summary_at"
SPREAD = timedelta(hours=2)
SENDING_STALE = timedelta(minutes=15)
_auto_locks: dict[int, threading.Lock] = {}


def auto_on() -> bool:
    return get_setting(AUTO_KEY) == "1"


def paused() -> dict | None:
    try:
        p = json.loads(get_setting(PAUSE_KEY) or "null")
    except ValueError:
        return None
    return p if isinstance(p, dict) and p.get("why") else None


def _pause(why: str):
    """Stop sending by itself for this account until Gmail is reconnected (or the switch is turned on again), and
    tell the owner once."""
    if paused():
        return
    set_setting(PAUSE_KEY, json.dumps({"at": _now_iso(), "why": why[:200]}))
    _activity("gmail.send", "Permission mails by themselves paused: " + why[:200])
    try:
        from .telegram import esc, notify

        notify("⚠️ <b>Permission mails paused — reconnect Gmail</b>\n" + esc(why[:200]) +
               "\n\nNothing more is sent by itself until you reconnect Gmail (My channels → Gmail) or switch "
               "“Send permission mails by themselves” on again.")
    except Exception:  # noqa: BLE001
        pass


def save_auto(on: bool) -> dict:
    was = auto_on()
    set_setting(AUTO_KEY, "1" if on else "0")
    if on:
        set_setting(PAUSE_KEY, "")
        if not was:  # creators already in the list are not mailed by themselves: only the ones that come after
            set_setting(SINCE_KEY, _now_iso())
    return auto_public()


def auto_public() -> dict:
    """What the dashboard's switch row shows."""
    from . import acl, gmail

    off = next((k for k in ("permissions.auto", "gmail.send") if not acl.allowed(k)), None)
    allowed = off is None
    last = _ts(get_setting(LAST_KEY))
    p = paused()
    return {"auto": auto_on(), "gmail": gmail.connected_email() or "", "allowed": allowed, "per_day": gmail.MAX_PER_DAY,
            "sent_today": gmail._sent_today(), "spread_hours": int(SPREAD.total_seconds() // 3600),
            "next_at": ((last + SPREAD).isoformat() + "Z") if last and last + SPREAD > utcnow() else None,
            "since": get_setting(SINCE_KEY) or None, "paused": p["why"] if p else "",
            "note": "" if allowed else acl.message(off)}


def migrate_auto_send() -> str:
    """Once, at start-up, in the owner's copy: the switch starts ON only when the owner's Gmail is already connected
    (and then only for creators added from now on). Customers start OFF (nothing stored = off). A choice that was
    already made is never changed."""
    from . import gmail

    with tenant.use(tenant.OWNER):
        cur = get_setting(AUTO_KEY)
        if cur is None:
            cur = "1" if gmail.connected_email() else "0"
            set_setting(AUTO_KEY, cur)
            if cur == "1":
                set_setting(SINCE_KEY, _now_iso())
            log.info("permission mails by themselves: %s for the owner (Gmail %s)", "ON" if cur == "1" else "OFF",
                     "connected" if cur == "1" else "not connected")
        return cur


def is_label(name: str, email: str = "") -> bool:
    """A music label / film studio / TV network, by the channel name or by the address's domain."""
    from . import creator_email as ce

    dom = (email or "").rpartition("@")[2].lower()
    return bool(LABELS.search(name or "") or (dom and (LABELS.search(ce.site_label(dom)) or LABELS.search(dom.replace(".", " ")))))


def auto_block(c: dict, d: dict | None = None) -> str | None:
    """Why this creator is never mailed by itself (None = they may be)."""
    from . import creator_email as ce, strikes
    from .gmail import EMAIL_RE

    if c.get("status") != "new":
        return "only creators still waiting to be asked are mailed by themselves"
    if c.get("mailed") or c.get("thread_id") or c.get("sending") or c.get("send_unsure"):
        return "a mail was already sent (or may have been sent) to this creator"
    em = (c.get("email") or "").strip().lower()
    if not em:
        return "no address yet"
    if not EMAIL_RE.match(em) or ce.reject_reason(em):
        return "this address cannot be used"
    if c.get("email_trust") not in TRUST_AUTO:
        return "found in their videos or on a website (or not clearly theirs) — confirm it, or press Send yourself"
    since = _ts(get_setting(SINCE_KEY))
    added = _ts(c.get("added_at"))
    if not since or not added or added < since:
        return "was in the list before sending by itself was switched on — press Send yourself"
    others = [o for o in (d if d is not None else _load()).values() if o.get("channel_id") != c.get("channel_id")
              and (o.get("email") or "").strip().lower() == em]
    if others:
        return (f"this address also belongs to {others[0].get('name') or 'another creator'} in the list — "
                "press Send yourself if it is right")
    if is_label(c.get("name") or "", em):
        return "music labels and big studios are never mailed by themselves"
    if strikes.is_blocked(c.get("channel_id")) or strikes.is_blocked(c.get("url")):
        return "on the never-use list after a strike"
    return None


def auto_send_due() -> str | None:
    """Send at most ONE ready mail by itself (switch on, Gmail connected and allowed, inbox allowed, not paused, daily
    cap and 2-hour spread kept). One round at a time per account; the 2-hour mark is taken BEFORE the send and given
    back only when nothing went out. Returns the channel id that was mailed, or None."""
    from . import acl, gmail

    tid = tenant.current()
    with _locks_guard:
        lk = _auto_locks.setdefault(tid, threading.Lock())
    if not lk.acquire(blocking=False):  # another round of this account is sending right now
        return None
    try:
        if not auto_on() or paused() or not acl.allowed("gmail.send") or not acl.allowed("permissions.manage") \
                or not acl.allowed("permissions.auto") \
                or not gmail.connected_email():
            return None
        if gmail._sent_today() >= gmail.MAX_PER_DAY:
            return None
        last = _ts(get_setting(LAST_KEY))
        if last and utcnow() - last < SPREAD:
            return None
        d = _load()
        ready = sorted((c for c in d.values() if auto_block(c, d) is None),
                       key=lambda c: (c.get("added_at") or "", c.get("name") or ""))
        for c in ready:
            before = get_setting(LAST_KEY) or ""
            set_setting(LAST_KEY, _now_iso())  # the spread is taken first: a second round can never slip in
            try:
                send_mail(c["channel_id"], by="auto")
                return c["channel_id"]
            except gmail.SignInError:
                set_setting(LAST_KEY, before)  # nothing went out (send_mail paused sending by itself)
                return None
            except (ValueError, KeyError) as e:
                set_setting(LAST_KEY, before)  # refused before anything left
                log.info("permission mail to %s not sent by itself: %s", c.get("name"), str(e)[:150])
                if gmail._sent_today() >= gmail.MAX_PER_DAY:
                    return None
            except Exception as e:  # noqa: BLE001  (it may or may not have gone out: the mark stays, sending pauses)
                _note("Sending a permission mail by itself", e)
                return None
        return None
    finally:
        lk.release()


def _activity(action: str, detail: str, actor: str = "system"):
    """One line in the activity log (the owner's copy too, so every mail that went out can be found)."""
    try:
        from . import acl

        if tenant.current() == tenant.OWNER:
            acl.record(None, actor, action, detail, tenant.OWNER)
        else:
            acl.record_here(action, detail, actor)
    except Exception:  # noqa: BLE001
        pass


def _log_mail(c: dict, by: str):
    with _lock():
        try:
            entries = json.loads(get_setting(MAIL_LOG) or "[]")
        except ValueError:
            entries = []
        entries.append({"at": _now_iso(), "cid": c["channel_id"], "name": c.get("name"), "email": c.get("email"), "by": by})
        set_setting(MAIL_LOG, json.dumps(entries[-200:]))
    if by != "auto":  # by itself, the mark was already taken before the send
        set_setting(LAST_KEY, _now_iso())


def listing() -> list[dict]:
    d = _load()
    out = []
    for c in d.values():
        v = dict(c)
        v["search"] = search_view(c)
        v["needs_you"] = c.get("status") == "replied"
        if c.get("status") == "new" and c.get("email") and not c.get("mailed"):
            v["auto_hold"] = auto_block(c, d)
        out.append(v)
    return sorted(out, key=lambda c: ({"replied": 0, "new": 1, "asked": 2, "granted": 3, "skipped": 4, "declined": 5}
                                      .get(c["status"], 6), c["name"]))


def set_status(cid: str, status: str) -> dict:
    with _lock():
        d = _load()
        if cid not in d:
            raise KeyError(cid)
        d[cid]["status"] = status
        d[cid][status + "_at"] = _now_iso()
        _save(d)
        return d[cid]


def set_email(cid: str, email: str) -> dict:
    from .gmail import EMAIL_RE

    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise ValueError("That email address does not look right.")
    with _lock():
        d = _load()
        if cid not in d:
            raise KeyError(cid)
        c = d[cid]
        if (c.get("email") or "").strip().lower() != email.lower():
            c.update(email=email, email_from="added by hand", email_trust="hand")
        _save(d)
        return c


def rewrite_unsent() -> int:
    """Give creators who have not been mailed yet the new, natural message (keeps messages the owner edited)."""
    with _lock():
        d = _load()
        n = 0
        for c in d.values():
            if not c.get("mailed") and not c.get("edited") and not c.get("sending") and c["status"] in ("new", "asked"):
                c["subject"], c["message"] = compose(c.get("name", ""), c.get("video_title", ""), c["channel_id"])
                n += 1
        _save(d)
        return n


def set_message(cid: str, subject: str, message: str) -> dict:
    with _lock():
        d = _load()
        if cid not in d:
            raise KeyError(cid)
        if d[cid].get("mailed") or d[cid].get("sending"):
            raise ValueError("Already sent.")
        if len((message or "").strip()) < 30:
            raise ValueError("The message is too short.")
        d[cid].update(subject=(subject or "").strip()[:150] or d[cid].get("subject"), message=message.strip()[:4000], edited=True)
        _save(d)
        return d[cid]


def send_mail(cid: str, by: str = "hand") -> dict:
    """Send the ready message from the account's Gmail. by: "hand" / "dashboard" / "telegram" (the owner pressed Send)
    or "auto" (the account's switch, see auto_send_due). Every way of sending comes through here.
    Never twice: the creator is claimed under a lock before Gmail is called, and a send that may or may not have gone
    out is never repeated by itself. A Gmail sign-in problem, or a send that may have gone out, pauses sending by
    itself for the account (the owner is told once)."""
    from . import acl, gmail

    if not acl.allowed("gmail.send"):  # every way of sending (dashboard, Viral page, Telegram, by itself) comes here
        raise ValueError(acl.message("gmail.send"))
    now = utcnow()
    with _lock():
        d = _load()
        if cid not in d:
            raise KeyError(cid)
        c = d[cid]
        if not c.get("email"):
            raise ValueError("Add the creator's email first (channel About page → business email, or their Instagram bio).")
        if c.get("thread_id") or c.get("mailed"):
            raise ValueError("Already sent to this creator.")
        if c.get("sending") and (_ts(c["sending"]) or now) > now - SENDING_STALE:
            raise ValueError("The mail to this creator is being sent right now.")
        if by == "auto":
            why = auto_block(c, d)
            if why:
                raise ValueError(why)
        c["sending"] = now.isoformat() + "Z"
        _save(d)
        to, body, subject = c["email"], c.get("message") or "", c.get("subject")
    try:
        res = gmail.send(to, body, subject)
    except gmail.SignInError as e:  # nothing went out; the sign-in must be renewed
        _update(cid, lambda c: c.pop("sending", None))
        _pause(str(e))
        raise
    except ValueError:  # refused before anything left (daily limit, bad address): it can be sent later
        _update(cid, lambda c: c.pop("sending", None))
        raise
    except Exception as e:  # it may or may not have gone out: never again by itself
        err = str(e)[:150]
        _update(cid, lambda c: (c.pop("sending", None), c.update(send_unsure=True, send_error=err)))
        _pause(f"A mail to {c.get('name')} may or may not have gone out ({err}). Look in Gmail's Sent folder.")
        raise

    def done(c):
        c.pop("sending", None)
        c.pop("send_unsure", None)
        c.update(status="asked", asked_at=_now_iso(), thread_id=res["thread_id"], mailed=True, sent_by=by)
    c = _update(cid, done)
    _log_mail(c, by)
    if by != "dashboard" or tenant.current() == tenant.OWNER:  # a customer's dashboard press is logged by the server
        _activity("gmail.send", f"Sent a permission mail to {c.get('name')} ({c.get('email')})"
                  + (" by itself" if by == "auto" else ""), "system" if by == "auto" else "user")
    return c


# ---------------------------------------------------------------- replies
YES_RE = re.compile(r"\b(yes|yeah|yep|yup|sure|of course|go ahead|permission granted|granted|you can|you may|allowed|ok|okay|"
                    r"haan|han ji|haa|ji haan|bilkul|theek hai|thik hai|kar sakte|kar sakta|kar lo|kr lo|use kar)\b", re.I)
# anything that is not a plain yes goes to the owner: refusals, conditions, money, legal words, questions, auto-replies
NOT_PLAIN_YES = re.compile(
    r"\b(no|not|don'?t|dont|do not|never|nahi|nahin|nhi|mat|nope|sorry|can'?t|cannot|won'?t|wont|only|but|however|"
    r"if|unless|first|after|condition\w*|terms?|pay\w*|paid|price\w*|pricing|charg\w*|fee\w*|rate\w*|cost\w*|money|"
    r"paise|rupee\w*|rs|inr|usd|dollars?|budget|commercial|license\w*|licence\w*|contract\w*|agreement\w*|invoice\w*|"
    r"remove|delete|strike|copyright|claim\w*|ask|later|wait|think|maybe|shayad|sochke|"
    r"out of (the )?office|away|vacation|holiday|leave|travell?ing|auto(matic)?|automated|reach|contact\w*|revert|"
    r"get back|team will|manager|management|agency|per (clip|video|short|post|reel)s?|"
    r"for \d+ (videos?|clips?|shorts?|reels?|posts?))\b|\?|₹|\$|\d{3,}", re.I)
# an out-of-office / auto-responder text (when the headers did not say so): passed over, the next message is read
AUTO_TEXT = re.compile(
    r"out of (the )?office|\bOOO\b|auto(matic)?[- ]?(reply|response|responder|generated)|this is an automated|"
    r"on (annual |sick )?leave|on vacation|currently (away|travell?ing|unavailable|out)|away from (my )?(desk|e-?mail|office)|"
    r"limited access to (my )?e-?mail|no longer (monitored|in use|active)|not (being )?monitored|"
    r"will (get back|respond|reply) to you|get back to you (shortly|soon|within|as soon)|within \d+ ?(hours|hrs|business|working)|"
    r"thank(s| you) for (your )?(e-?mail|message|mail|reaching out)[.!,]* (i|we)('m| am| are|'re| will| have)", re.I)
QUOTE_CUT = re.compile(
    r"\bOn .{0,160}?wrote:|\bFrom:\s.{0,200}?\b(Sent|Date):\s|-{2,}\s*Original Message\s*-{2,}|^\s*From:\s|^_{5,}|"
    r"\bSent from (my )?\w+|\bGet Outlook for\b|\bNamaste\b|\bHi .{0,40}I'm a big fan|^>", re.I | re.M)


def _own_words(snippet: str) -> str:
    """The creator's new text only (drop the quoted original mail: Gmail, Outlook, phone signatures)."""
    t = QUOTE_CUT.split(snippet or "", maxsplit=1)[0]
    return re.sub(r"\s+", " ", t).strip()


def _sender_ok(c: dict, r: dict) -> bool:
    """The reply comes from exactly the address we wrote to."""
    from email.utils import parseaddr

    want = (c.get("email") or "").strip().lower()
    return bool(want) and parseaddr(r.get("from") or "")[1].strip().lower() == want


def is_auto_text(r: dict) -> bool:
    return bool(AUTO_TEXT.search(_own_words(r.get("snippet", "")) or r.get("snippet", "")))


def plain_yes(c: dict, r: dict) -> bool:
    """A short, unconditional yes from the address we wrote to. Everything else waits for the owner."""
    text = _own_words(r.get("snippet", ""))
    return _sender_ok(c, r) and 0 < len(text) <= 300 and bool(YES_RE.search(text)) and not NOT_PLAIN_YES.search(text) \
        and not AUTO_TEXT.search(text)


# a short, clear refusal from the address we wrote to: marked "declined", nothing more is ever sent to them
NO_RE = re.compile(r"^\W*(no|nope|nah|nahi|nahin|nhi|na)\b|\b(not interested|don'?t use|do not use|please don'?t|"
                   r"not allowed|no permission|permission (is )?denied|i (do not|don'?t) (give|allow|grant)|"
                   r"(can'?t|cannot|won'?t) (allow|give|grant)|not okay|not ok|mat karo|mat use|use mat|nahi chahiye|"
                   r"allow nahi|permission nahi|nahi de sakta|nahi de sakte|nahi dunga|mana hai)\b", re.I)
# ... but not when it is really a yes, a question, a condition or money: those go to the owner
NOT_PLAIN_NO = re.compile(r"\b(no problem|no worries|no issues?|no objection|why not|not a problem|of course|sure|"
                          r"go ahead|happy to|yes|haan|bilkul|pay\w*|paid|price\w*|charg\w*|fee\w*|rate\w*|money|paise|"
                          r"rupee\w*|rs|inr|license\w*|licence\w*|contract\w*|agreement\w*|unless|if|but|only|later|maybe|"
                          r"think|call|whatsapp|contact\w*|reach|manager|team)\b|\?|₹", re.I)


def plain_no(c: dict, r: dict) -> bool:
    """A short, clear no from the address we wrote to (anything less clear waits for the owner)."""
    text = _own_words(r.get("snippet", ""))
    return _sender_ok(c, r) and 0 < len(text) <= 300 and bool(NO_RE.search(text)) and not NOT_PLAIN_NO.search(text) \
        and not AUTO_TEXT.search(text)


def _grant_trusted(c: dict) -> bool:
    """A plain yes may start the source by itself only from an address the owner can stand behind: from the channel's
    own description (clearly theirs), typed or confirmed by the owner, or saved before this check existed."""
    return "email_trust" not in c or c.get("email_trust") in TRUST_AUTO


def revoke(cid: str) -> dict:
    """Undo an automatic yes: stop the source; the reply goes back to the owner to decide."""
    c0 = _load().get(cid)
    if not c0:
        raise KeyError(cid)
    if c0.get("source_id"):
        with SessionLocal() as s:
            ch = s.get(Channel, c0["source_id"])
            if ch:
                ch.auto, ch.mode = False, "cc"  # paused; nothing from this creator is uploaded any more
                s.commit()
    c = _update(cid, lambda c: c.update(status="replied" if c.get("reply") else "asked", auto_granted=False, proof=None))
    if c is None:
        raise KeyError(cid)
    return c


def check_replies() -> int:
    """Look for creators' replies in this account's own Gmail (a timed job for every account with Gmail connected).
    Automatic replies (by their headers, or an out-of-office text) are passed over and the thread keeps being read.
    A plain yes from a trusted address starts their source by itself (with Undo); a plain no marks them declined;
    anything else is "replied — needs you" and the account owner gets ONE notice for the round."""
    from . import gmail

    if not gmail.connected_email():
        return 0
    found, auto, ask, no = 0, [], [], []
    with _lock():
        waiting = [c["channel_id"] for c in _load().values()
                   if c.get("status") == "asked" and c.get("thread_id") and not c.get("reply_id")]
    for cid in waiting:
        c0 = _load().get(cid) or {}
        seen = c0.get("auto_seen") or []
        try:
            r = gmail.reply_in_thread(c0["thread_id"], skip=seen) if seen else gmail.reply_in_thread(c0["thread_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("gmail reply check: %s", str(e)[:150])
            _note("Reading creators' replies in Gmail", e)
            continue
        if not r or (r.get("id") and r.get("id") in seen):
            continue
        if is_auto_text(r):  # out of office / auto-responder: remember it, keep reading the thread
            _update(cid, lambda c, r=r: c.update(auto_seen=(c.get("auto_seen") or [])[-20:] + [r.get("id")],
                                                 auto_reply={"at": _now_iso(), "text": (r.get("snippet") or "")[:300]}))
            continue
        kind = "no" if plain_no(c0, r) else "yes" if plain_yes(c0, r) and _grant_trusted(c0) else "ask"
        at = _now_iso()

        def keep(c, r=r, at=at):
            if c.get("status") != "asked" or c.get("reply_id"):  # changed meanwhile (the owner decided): leave it
                return
            c.update(reply_id=r.get("id"), reply_from=r.get("from"), reply_date=r.get("date"), reply=r.get("snippet") or "",
                     status="replied", replied_at=at)
        c = _update(cid, keep)
        if not c or c.get("replied_at") != at:
            continue
        found += 1
        {"yes": auto, "no": no, "ask": ask}[kind].append(cid)
    if not found:
        return 0
    from .telegram import esc, notify

    for cid in no:  # a clear no: never asked again, never used
        c = _update(cid, lambda c: c.update(status="declined", declined_at=_now_iso(), declined_by="reply"))
        _activity("permissions.manage", f"{c.get('name')} said no by mail — marked “said no”, nothing more is sent")
    for cid in auto:  # clear, unconditional yes from the address we wrote to
        try:
            g = grant_from_reply(cid)
            _update(cid, lambda c: c.update(auto_granted=True))
            _activity("permissions.manage", f"{g.get('name')} said yes by mail — their source was started by itself")
            notify(f"✅ <b>{esc(g['name'])}</b> said yes:\n<i>{esc(_own_words(g['reply'])[:300])}</i>\n\n"
                   "Their new videos are now clipped and uploaded automatically, with credit. Wrong? Press Undo.",
                   [[{"text": "↩️ Undo", "callback_data": f"pu:{cid}"}]])
        except Exception as e:  # noqa: BLE001
            log.warning("auto-grant failed for %s: %s", cid, str(e)[:150])
            _note("Starting clips after a creator said yes", e)
            ask.append(cid)
    d = _load()
    ask = [cid for cid in ask if cid in d]
    if ask:
        for cid in ask:
            _activity("permissions.manage", f"{d[cid].get('name')} replied — waiting for you to decide")
        try:  # ONE notice for the round
            if len(ask) == 1:
                c = d[ask[0]]
                notify(f"📩 <b>{esc(c['name'])}</b> replied — needs you:\n<i>{esc((c.get('reply') or '')[:400])}</i>",
                       [[{"text": "✅ Permission given", "callback_data": f"pg:{ask[0]}"},
                         {"text": "❌ They said no", "callback_data": f"pn:{ask[0]}"}]])
            else:
                notify(f"📩 <b>{len(ask)} creators replied — they need you</b>\n" + "\n".join(
                    f"• <b>{esc(d[cid]['name'])}</b>: <i>{esc((d[cid].get('reply') or '')[:160])}</i>" for cid in ask)
                    + "\n\nRead and decide: Dashboard → Ideas → Permission inbox.")
        except Exception:  # noqa: BLE001
            pass
    return found


def daily_summary() -> str | None:
    """ONE message a day per account (Telegram / phone / own inbox): mailed, replied, said yes, said no since the last
    summary. Nothing is sent on a day with nothing to tell."""
    now = utcnow()
    since = _ts(get_setting(SUMMARY_KEY)) or now - timedelta(days=1)
    try:
        sent = [e for e in json.loads(get_setting(MAIL_LOG) or "[]") if (_ts(e.get("at")) or since) > since]
    except ValueError:
        sent = []
    d = _load()

    def n(field):
        return sum(1 for c in d.values() if (_ts(c.get(field)) or since) > since)
    replied, granted, declined = n("replied_at"), n("granted_at"), n("declined_at")
    needs = sum(1 for c in d.values() if c.get("status") == "replied")
    if not (sent or replied or granted or declined):
        set_setting(SUMMARY_KEY, now.isoformat() + "Z")
        return None
    auto = sum(1 for e in sent if e.get("by") == "auto")
    text = (f"📬 <b>Permission inbox today</b>\n{len(sent)} mailed" + (f" ({auto} by themselves)" if auto else "")
            + f" · {replied} replied · {granted} said yes · {declined} said no"
            + (f"\n{needs} repl{'y' if needs == 1 else 'ies'} need you: Dashboard → Ideas → Permission inbox." if needs else ""))
    from .telegram import notify

    notify(text)
    set_setting(SUMMARY_KEY, now.isoformat() + "Z")
    return text


def grant_from_reply(cid: str) -> dict:
    c = _load().get(cid)
    if not c or not c.get("reply"):
        raise KeyError(cid)
    proof = f"Email reply from {c.get('reply_from', '')} on {c.get('reply_date', '')}: “{c['reply'][:300]}”"
    return grant(cid, proof)


def grant(cid: str, proof: str) -> dict:
    """The creator said yes: save the proof and start a permission source for their channel (automatic uploads)."""
    proof = (proof or "").strip()
    if len(proof) < 20:
        raise ValueError("Write how they said yes — who, how and when (e.g. 'Reply on Instagram from @name on 22 Nov').")
    c = _load().get(cid)
    if c is None:
        raise KeyError(cid)
    from . import acl, strikes

    if strikes.is_blocked(cid) or strikes.is_blocked(c.get("url")):  # on the never-use list after a strike: no source for their videos
        raise ValueError(strikes.blocked_message(c.get("name") or ""))
    if not acl.allowed("source.channel"):  # a yes becomes a channel source: that kind must be allowed for the account
        raise ValueError(acl.message("source.channel"))
    with SessionLocal() as s:
        dest = main_destination(s)
        if not dest:
            raise ValueError("Connect your YouTube channel first.")
        existing = s.query(Channel).filter(Channel.resolved_url == c["url"]).first()
        cap = acl.limit("sources")
        if not existing and cap is not None and acl.source_count() >= cap:
            raise ValueError(f"Your account can have {cap} source(s) and they are all used. Delete one first, "
                             "or ask the person who gave you access for more.")
        if existing:
            existing.mode, existing.permission_note, existing.auto = "permission", proof, True
            src_id = existing.id
        else:
            ch = Channel(kind="channel", query=c["url"], resolved_url=c["url"], name=c["name"], mode="permission",
                         permission_note=proof, auto=True, videos_per_run=2, clips_per_video=1, clip_len=35, aspect="9:16",
                         gap_hours=12, destination_id=dest.id, review_first=False, captions=True, post_times="10:00, 18:00",
                         tz_offset=-330)
            s.add(ch)
            s.flush()
            src_id = ch.id
        s.commit()
    c = _update(cid, lambda c: c.update(status="granted", proof=proof, granted_at=_now_iso(), source_id=src_id))
    try:  # the viral video we asked about is clipped right away
        from .viral import on_granted

        on_granted(cid)
    except Exception as e:  # noqa: BLE001
        log.warning("viral on_granted: %s", str(e)[:150])
        _note("Starting a viral pick after permission was given", e)
    return c

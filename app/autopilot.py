"""Autopilot: channel name/URL in -> latest videos -> hot moments -> clips -> render -> metadata -> scheduled."""
import os
import re
from datetime import timedelta
from urllib.parse import quote

import yt_dlp
from sqlalchemy import func

from .db import Channel, Clip, Destination, Project, SessionLocal, utcnow
from . import acl, quota, youtube
from . import workers
from .captions import fetch_cues, parse_json3
from .hot import fetch_comments, fetch_heatmap, parse_timestamps, rank_moments
from .ingest import _opts, fetch_info, is_cc
from .metadata import auto_metadata
from .render import run_render

MAX_QUEUED_PER_DEST = 30  # don't pile up an unbounded backlog (the usual number; see queue_cap)
QUEUE_CAP_RANGE = (10, 300)
MIN_FREE_GB = 0.7  # the sources wait while the server's disk has less free space than this
OLDER_DEPTH = 300  # a channel source with nothing new goes back through this many of the channel's uploads


def queue_cap() -> int:
    """How many clips may wait for one YouTube channel before the sources pause. The usual number is 30; the account's
    setting "queue_cap" raises or lowers it (e.g. 200 to make a whole batch at once)."""
    from .db import get_setting

    try:
        n = int(get_setting("queue_cap") or 0)
    except (TypeError, ValueError):
        n = 0
    return min(max(n, QUEUE_CAP_RANGE[0]), QUEUE_CAP_RANGE[1]) if n else MAX_QUEUED_PER_DEST


def disk_free_gb() -> float | None:
    import shutil

    from .config import BASE_DATA_DIR

    try:
        return shutil.disk_usage(BASE_DATA_DIR).free / 1e9
    except OSError:
        return None
# clips that already own a publish time (incl. ones still waiting for the helper PC or rendering)
HOLDS_SLOT = ["fetching", "rendering", "draft", "rendered", "queued", "uploading", "uploaded"]


def _resolve_ytdlp(query: str) -> tuple[str, str]:
    """Return (channel_base_url, display_name) from a name, @handle or URL."""
    q = query.strip()
    if q.startswith("http"):
        base = q.split("?")[0].rstrip("/")
        for suffix in ("/videos", "/featured", "/shorts", "/streams"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        return base, base.rsplit("/", 1)[-1]
    if q.startswith("@"):
        return f"https://www.youtube.com/{q}", q
    url = f"https://www.youtube.com/results?search_query={quote(q)}&sp=EgIQAg%3D%3D"  # channels-only filter
    with yt_dlp.YoutubeDL(_opts({"extract_flat": True, "playlistend": 1})) as y:
        info = y.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise ValueError(f"No channel found for '{q}'")
    e = entries[0]
    return e.get("url") or e.get("channel_url"), e.get("title") or e.get("channel") or q


def _list_ytdlp(channel_url: str, n: int) -> list[str]:
    with yt_dlp.YoutubeDL(_opts({"extract_flat": True, "playlistend": n})) as y:
        info = y.extract_info(channel_url.rstrip("/") + "/videos", download=False)
    return [e["id"] for e in (info.get("entries") or []) if e and e.get("id")]


def _slot_from_times(after, post_times: str, tz_offset: int):
    """First daily slot (user's local clock) strictly after `after` (naive UTC). None if no valid times."""
    times = sorted({tuple(int(x) for x in t.strip().split(":")) for t in (post_times or "").split(",")
                    if re.match(r"^\d{1,2}:\d{2}$", t.strip()) and int(t.strip().split(":")[0]) < 24 and int(t.strip().split(":")[1]) < 60})
    if not times:
        return None
    local_after = after - timedelta(minutes=tz_offset or 0)
    for d in range(0, 90):
        day = (local_after + timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        for h, m in times:
            cand = day + timedelta(hours=h, minutes=m)
            if cand > local_after:
                return cand + timedelta(minutes=tz_offset or 0)
    return None


def resolve_channel(query: str) -> tuple[str, str]:
    try:
        return youtube.api_resolve_channel(query)  # official API: not blocked on servers
    except Exception:  # noqa: BLE001
        return _resolve_ytdlp(query)


def list_video_ids(channel_url: str, n: int) -> list[str]:
    try:
        return youtube.api_list_video_ids(channel_url, n)
    except Exception:  # noqa: BLE001
        return _list_ytdlp(channel_url, n)


def _next_slot(s, gap_hours: int, dest_id, post_times: str | None = None, tz_offset: int = 0):
    """Next free publish slot for this destination (only counts clips still holding a future slot)."""
    now = utcnow()
    last = (
        s.query(func.max(Clip.publish_at))
        .filter(
            Clip.destination_id == dest_id,
            Clip.status.in_(HOLDS_SLOT),
            Clip.publish_at.isnot(None),
            Clip.publish_at > now,
        )
        .scalar()
    )
    base = max(last, now) if last else now
    slot = _slot_from_times(base, post_times, tz_offset) if post_times else None
    slot = slot or (base + timedelta(hours=gap_hours))
    # all channels share one daily upload budget: move to the next day while that day is full
    cap = quota.capacity()
    for _ in range(60):
        day = quota.pt_day_of(slot)
        lo, hi = slot - timedelta(hours=36), slot + timedelta(hours=36)
        load = sum(1 for (t,) in s.query(Clip.publish_at).filter(
            Clip.status.in_(HOLDS_SLOT), Clip.publish_at.between(lo, hi)) if quota.pt_day_of(t) == day)
        if load < cap:
            break
        nxt = slot + timedelta(days=1)
        slot = (_slot_from_times(nxt.replace(hour=0, minute=0) - timedelta(minutes=1), post_times, tz_offset) if post_times else None) or nxt
    return slot


def _process_video(s, ch: Channel, vid: str, log: list, mode: str | None = None, note: str | None = None) -> bool:
    """Returns True if the video yielded clips. mode/note override the source's (viral picks: per video)."""
    mode = mode or ch.mode
    info = fetch_info(f"https://www.youtube.com/watch?v={vid}")
    from . import strikes

    if mode != "own" and strikes.is_blocked(info.get("channel_url")):  # every source type comes through here
        log.append(f"skip {vid}: {info.get('channel') or 'this creator'} {strikes.BLOCKED_SKIP}")
        return False
    if mode == "own":  # only videos that really are on one of my connected channels
        mine = {d.channel_id for d in s.query(Destination).all()}
        if not any(cid and cid in (info.get("channel_url") or "") for cid in mine):
            log.append(f"skip {vid}: not on one of your connected channels")
            return False
    elif (mode == "cc" or ch.kind == "topic") and not is_cc(info["license"]):
        log.append(f"skip {vid}: not CC")
        return False
    p = Project(url=f"https://www.youtube.com/watch?v={vid}", mode=mode,
                permission_note=note if note is not None else ch.permission_note, channel_ref=ch.id, **info)
    if not p.heatmap:
        p.heatmap = fetch_heatmap(vid)
    secs = parse_timestamps(fetch_comments(vid), p.duration)
    p.moments = rank_moments(p.duration, p.heatmap, secs, ch.clip_len, ch.clips_per_video)
    s.add(p)
    s.commit()
    if not p.moments and workers.any_recent("analyze"):
        # the server can't see "most replayed" / comments (YouTube blocks it): the helper PC reads them
        p.analysis_pending, p.analysis_since, p.analysis_tries = True, utcnow(), 0
        s.commit()
        log.append(f"{vid}: helper PC is finding the best moment")
        return True
    _make_clips(s, ch, p, log)
    return True


def _fallback_moments(p: Project, length: int, n: int) -> list:
    """No replay/comment signal at all: evenly spread slices from the middle part of the video."""
    dur = float(p.duration or 0)
    if dur <= length:
        return [{"start": 0.0, "end": dur or float(length)}] if dur > 5 else []
    marks = [0.3, 0.5, 0.7, 0.4, 0.6][:max(1, n)]
    return [{"start": round(dur * f, 1), "end": round(min(dur, dur * f + length), 1)} for f in marks]


def make_clips_for_project(pid: int):
    """Continue an autopilot video after the helper PC's analysis (or after giving up on it)."""
    with SessionLocal() as s:
        p = s.get(Project, pid)
        ch = s.get(Channel, p.channel_ref) if p and p.channel_ref else None
        if not ch or s.query(Clip).filter(Clip.project_id == pid).count():
            return
        log = []
        _make_clips(s, ch, p, log)
        ch.last_status = ((ch.last_status or "") + "; " + "; ".join(log))[-1000:]
        s.commit()


def _make_clips(s, ch: Channel, p: Project, log: list):
    moments = p.moments or _fallback_moments(p, ch.clip_len, ch.clips_per_video)
    if not moments:
        log.append(f"skip {p.video_id}: video too short")
        return
    if not p.moments:
        log.append(f"{p.video_id}: no replay data, using the middle of the video")
    # "Automatic upload without review" switched off for this account: every new clip waits for approval,
    # whatever the source says
    review_first = bool(ch.review_first) or not acl.auto_upload()
    for m in moments[:ch.clips_per_video]:
        full = acl.room_for_clip()  # the account's clips-a-day limit and storage
        if full:
            log.append(f"stopped: {full}")
            break
        c = Clip(project_id=p.id, start=m["start"], end=m["end"], aspect=ch.aspect, effects=["fade"],
                 destination_id=ch.destination_id, credit_on=ch.mode != "own")
        s.add(c)
        s.commit()
        if ch.captions:
            try:
                c.captions = parse_json3(p.caption_track, c.start, c.end) if p.caption_track else \
                    fetch_cues(p.video_id, c.start, c.end)
                if c.captions:
                    c.caption_style = {"on": True, "lang": "auto", "pos": "bottom", "size": "M", "color": "#ffffff"}
                s.commit()
            except Exception as e:  # noqa: BLE001
                log.append(f"clip {c.id}: no captions{'' if 'confirm you' in str(e) else ' (' + str(e)[:70] + ')'}")
        run_render(c.id)
        s.refresh(c)
        if c.status == "fetching":  # helper PC will fetch the video; render finishes later
            auto_metadata(c.id)
            s.refresh(c)
            if not review_first:
                c.publish_at = _next_slot(s, ch.gap_hours, ch.destination_id, ch.post_times, ch.tz_offset or 0)
                c.autoqueue = True  # becomes 'queued' as soon as the render completes
                s.commit()
            log.append(f"clip {c.id}: waiting for helper PC")
            continue
        if c.status != "rendered":
            log.append(f"clip {c.id} failed: {c.error}")
            continue
        auto_metadata(c.id)
        s.refresh(c)
        if review_first:
            log.append(f"clip {c.id} ready for review")
            continue
        c.publish_at, c.status, c.autoqueue = _next_slot(s, ch.gap_hours, ch.destination_id, ch.post_times, ch.tz_offset or 0), "queued", True
        s.commit()
        log.append(f"queued clip {c.id} for {c.publish_at}")


def run_channel(channel_id: int):
    with SessionLocal() as s:
        ch = s.get(Channel, channel_id)
        if not ch or ch.kind == "viral":  # viral picks are driven by app/viral.py, never by the source timer
            return
        blocked = acl.source_block(ch.kind, bool(ch.auto_scripts))  # stopped, switched off or limited by the administrator
        if blocked:  # wait quietly; the source carries on by itself when it is allowed again
            ch.last_status, ch.last_run = f"waiting: {blocked}", utcnow()
            s.commit()
            return
        log = []
        try:
            dest = s.get(Destination, ch.destination_id) if ch.destination_id else None
            if not dest:  # its channel was disconnected (or never set): use the main channel, or wait for one
                from .db import main_destination

                dest = main_destination(s)
                if not dest:
                    ch.last_status = "waiting: connect a YouTube channel (My channels) — this source then starts by itself"
                    ch.last_run = utcnow()
                    s.commit()
                    return
                ch.destination_id = dest.id
                s.commit()
            if dest and dest.paused:
                ch.last_status = f"waiting: uploads to {dest.name} are paused (press Resume on the dashboard)"
                ch.last_run = utcnow()
                s.commit()
                return
            if ch.kind == "reel":
                if ch.auto_scripts and not any(not x.get("used") for x in (ch.scripts or [])) and not ch.script_wanted:
                    topic = trending_phrase(ch)  # ask the helper's local AI for a script on today's trend
                    if topic:
                        ch.script_wanted = {"topic": topic, "since": utcnow().isoformat()}
                        log.append(f"asked the local AI for a script about “{topic}”")
                elif ch.script_wanted and ch.script_wanted.get("since", "") < (utcnow() - timedelta(hours=2)).isoformat():
                    log.append("waiting for the local AI on the helper PC (Ollama with qwen2.5:3b)")
                _run_reels(s, ch, log)
                ch.last_status = "; ".join(log) or "no scripts left — add more scripts"
                ch.last_run = utcnow()
                s.commit()
                return
            if ch.kind == "trending":  # pick today's trending topic, then search it like a topic (CC only)
                phrase = trending_phrase(ch)
                if not phrase:
                    ch.last_status, ch.last_run = "no new trending topic right now", utcnow()
                    s.commit()
                    return
                ch.resolved_url = "trending"
                log.append(f"trending topic: “{phrase}”")
            topic = ch.kind in ("topic", "trending")
            search_q = phrase if ch.kind == "trending" else ch.query  # trending keeps its category in ch.query
            if not ch.resolved_url:
                if topic:
                    ch.resolved_url, ch.name = "topic", f"Topic: {ch.query}"
                else:
                    ch.resolved_url, ch.name = resolve_channel(ch.query)
                s.commit()
            from . import strikes

            if not topic and ch.mode != "own" and (strikes.is_blocked(ch.resolved_url) or strikes.is_blocked(ch.query)):
                # this creator is on the never-use list after a strike: the source stops for good (Alerts → Strikes can allow them again)
                ch.auto, ch.last_run = False, utcnow()
                ch.last_status = f"stopped: {ch.name or 'this creator'} {strikes.BLOCKED_SKIP}"
                s.commit()
                return
            backlog = s.query(Clip).filter(Clip.destination_id == ch.destination_id,
                                           Clip.status.in_(["queued", "fetching", "rendering"])).count()
            if backlog >= queue_cap():
                ch.last_status = f"paused: {backlog} clips already queued"
                ch.last_run = utcnow()
                s.commit()
                return
            free = disk_free_gb()
            if free is not None and free < MIN_FREE_GB:  # a large waiting list must never fill the disk
                ch.last_status = f"paused: the server's disk is almost full ({free:.1f} GB free) - new clips wait until uploads free some space"
                ch.last_run = utcnow()
                s.commit()
                return
            seen = set(ch.seen or [])
            # never clip a video twice, whichever source found it first
            used = {v for (v,) in s.query(Project.video_id).filter(Project.video_id.isnot(None))}
            seen |= used
            if topic:  # Creative Commons videos only, found by YouTube search
                ids = youtube.api_search_cc(search_q, seen, ch.videos_per_run * 3)
                if not ids:
                    log.append("no new Creative Commons videos found for this topic" if quota.can_search()
                               else "search skipped: today's YouTube quota is kept for uploads")
            else:
                ids = [i for i in list_video_ids(ch.resolved_url, ch.videos_per_run * 4) if i not in seen]
                if not ids:  # the newest videos are all used: go back through the channel's older uploads
                    ids = [i for i in list_video_ids(ch.resolved_url, OLDER_DEPTH) if i not in seen]
                    if ids:
                        log.append("nothing new: taking older videos of this channel")
            made = 0
            for vid in ids:
                if made >= ch.videos_per_run:
                    break
                waiting = s.query(Clip).filter(Clip.destination_id == ch.destination_id,
                                               Clip.status.in_(["queued", "fetching", "rendering"])).count()
                if waiting >= queue_cap():  # several sources run at once: stop as soon as the list is full
                    log.append(f"paused: {waiting} clips already queued")
                    break
                seen.add(vid)  # mark first, so a failing video is never retried forever or duplicated
                ch.seen = list(seen)
                s.commit()
                try:
                    if _process_video(s, ch, vid, log):
                        made += 1
                    elif not topic and log and strikes.BLOCKED_SKIP in log[-1]:
                        ch.auto = False  # this channel's own videos are all from the struck creator: stop, do not go on
                        log.append("source stopped")
                        break
                except Exception as e:  # noqa: BLE001
                    s.rollback()
                    log.append(f"{vid}: {str(e)[:150]}")
            ch.last_status = "; ".join(log) or "no new videos"
        except Exception as e:  # noqa: BLE001
            s.rollback()
            ch = s.get(Channel, channel_id)
            ch.last_status = f"error: {str(e)[:300]}"
        ch.last_run = utcnow()
        s.commit()


INTERVAL = timedelta(hours=int(os.getenv("AUTOPILOT_INTERVAL_HOURS") or "6"))


def autopilot_tick():
    """Run every source whose interval has passed since its last run (called every 15 minutes)."""
    now = utcnow()
    with SessionLocal() as s:
        ids = [c.id for c in s.query(Channel).filter(Channel.auto.is_(True)).all() if c.kind != "viral"
               and (not c.last_run or c.last_run < now - (timedelta(hours=c.interval_hours) if c.interval_hours else INTERVAL))]
    for i in ids:
        try:
            run_channel(i)
        except Exception:  # noqa: BLE001
            continue


STOP = set("""a an the and or of to in on for with from by at is are was were be this that these those my your our his her
its their i you we they it vs new latest full video official live part episode ep day today 2024 2025 2026 2027 hindi english
shorts short reels reel viral trending best top most watch must how what why when who ki ka ke ko se me mein hai hain
aur bhi ek yeh woh""".split())


def trend_phrase(title: str, tags: list | None = None) -> str:
    """Short search phrase (2-4 words) that describes a trending video's topic."""
    for t in (tags or [])[:5]:
        words = [w for w in re.findall(r"[\w']+", t.lower()) if w not in STOP and not w.isdigit()]
        if 2 <= len(words) <= 4:
            return " ".join(words)
    words = [w for w in re.findall(r"[\w']+", re.split(r"[|#(\[]", title or "")[0].lower()) if w not in STOP and not w.isdigit()]
    return " ".join(words[:3]) if len(words) >= 2 else ""


ANGLES = ["an interesting history fact", "a surprising myth vs truth", "the most famous example in India",
          "a quick tip most people don't know", "how it is made or done", "a famous one from a big Indian city",
          "a record-breaking fact", "a festival or tradition linked to it", "a healthy or smart way to enjoy it",
          "something that changed over the last 50 years"]
GENERIC = {"trends", "trend", "trending", "facts", "fact", "videos", "video", "reels", "reel", "shorts", "short", "ideas", "tips"}


def trending_phrase(ch: Channel) -> str:
    """Next topic for this source's local-AI script, not used before.

    A text subject (e.g. "Food trends"): a trending India video about that subject if there is one, else the subject
    with a fresh angle (history, myth vs truth, ...), so the script always stays on the source's subject.
    A YouTube category id: today's trending videos of that category (songs, films and TV shows are skipped)."""
    from .competitors import trending

    used = set((ch.seen_topics or []))
    q = (ch.query or "").strip()
    keys = [] if q.isdigit() else [w for w in re.findall(r"[\w']+", q.lower()) if w not in GENERIC and w not in STOP]
    for v in trending(q if q.isdigit() else ""):
        if youtube.RISKY.search(v["title"]):
            continue
        text = (v["title"] + " " + " ".join(v.get("tags") or [])).lower()
        if keys and not any(k in text for k in keys):
            continue  # not about this source's subject
        ph = trend_phrase(v["title"], v.get("tags"))
        # Hindi titles split into broken letters with \w (vowel signs are not word characters): plain words only
        if not ph or not re.fullmatch(r"[a-z0-9' ]+", ph) or any(len(w) < 3 for w in ph.split()):
            continue
        if ph not in used:
            ch.seen_topics = list(used | {ph})[-200:]
            return ph
    if keys:
        subject = " ".join(keys)
        for angle in ANGLES:
            ph = f"Indian {subject}: {angle}" if "india" not in subject else f"{subject}: {angle}"
            if ph not in used:
                ch.seen_topics = list(used | {ph})[-200:]
                return ph
        ch.seen_topics = []  # every angle used: start the round again
        return f"Indian {subject}: {ANGLES[0]}"
    return ""


GREETING = re.compile(r"^(hey|hi|hello|namaste|welcome|what'?s up|guys|friends|dosto)[\w ,']{0,25}[.!?]*$", re.I)


def _script_title(text: str) -> str:
    """The reel's title: the first real sentence (greetings like "Hey there!" are skipped; a question is preferred)."""
    sents = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"(?<=[.!?।])\s|\n", text.strip()) if x.strip()]
    good = [x for x in sents if len(x.split()) >= 4 and not GREETING.match(x)]
    pick = next((x for x in good[:3] if x.endswith("?")), good[0] if good else (sents[0] if sents else ""))
    return pick[:90] or "My reel"


def script_problem(text: str, topic: str) -> str:
    """Why a local-AI script is not good enough to become a reel ('' = fine). Bad ones are written again."""
    text = (text or "").strip()
    sents = [x for x in re.split(r"(?<=[.!?।])\s|\n", text) if len(x.split()) >= 3]
    if len(text) < 200 or len(sents) < 4:
        return "too short"
    if len(text) > 1200:
        return "too long"
    words = re.findall(r"[a-z']+", text.lower())
    if words and max(words.count(w) for w in set(words) if len(w) > 4) > max(6, len(words) // 8):
        return "repeats itself"
    keys = [w for w in re.findall(r"[a-z]{4,}", (topic or "").split(":")[0].lower()) if w not in ("indian", "india")]
    if keys and not any(k[:5] in text.lower() for k in keys):
        return "not about the topic"
    return ""


def _run_reels(s, ch: Channel, log: list):
    """Reel source: turn the next unused scripts into reels (made by ReelGen on the helper PC)."""
    scripts = [dict(x) for x in (ch.scripts or [])]
    todo = [i for i, x in enumerate(scripts) if not x.get("used") and (x.get("text") or "").strip()]
    dest = s.get(Destination, ch.destination_id) if ch.destination_id else None
    review_first = bool(ch.review_first) or not acl.auto_upload()  # see _make_clips
    for i in todo[:ch.videos_per_run]:
        full = acl.room_for_clip()
        if full:
            log.append(f"stopped: {full}")
            break
        text = scripts[i]["text"].strip()
        subject = (scripts[i].get("topic") or ch.query or "").split(":")[0].strip().lower()
        p = Project(url="", video_id=None, title=_script_title(text), channel=dest.name if dest else "",
                    channel_url="", license="Original content (my own reel)", description=text,
                    tags=[t for t in dict.fromkeys([subject, (ch.query or "").strip().lower()]) if t][:2],
                    duration=0, heatmap=[], moments=[], mode="original", channel_ref=ch.id)
        s.add(p)
        s.commit()
        c = Clip(project_id=p.id, start=0, end=60, aspect="9:16", effects=[], overlays=[], credit_on=False,
                 destination_id=ch.destination_id, status="fetching", fetch_since=utcnow())
        if not review_first:
            c.publish_at = _next_slot(s, ch.gap_hours, ch.destination_id, ch.post_times, ch.tz_offset or 0)
            c.autoqueue = True  # becomes 'queued' as soon as the reel is made and rendered
        s.add(c)
        scripts[i]["used"] = True
        ch.scripts = scripts
        s.commit()
        auto_metadata(c.id)
        log.append(f"reel {c.id}: “{p.title[:40]}” — helper PC is making it")


def resume_after_analysis(pid: int):
    """The helper PC finished (or gave up) reading a video: continue the Quick upload or the autopilot source."""
    with SessionLocal() as s:
        p = s.get(Project, pid)
        quick = bool(p and p.quick)
    if quick:
        from .quick import quick_run

        quick_run(pid)
    else:
        make_clips_for_project(pid)

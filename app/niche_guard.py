"""One topic per channel (switch "niche.guard"): clips that are clearly off the channel's niche are parked, not posted.

A YouTube channel (Destination) HAS a niche when
  * one of its active sources was made by "What is your channel about?" (Channel.niche = a ready-made niche key from
    app/niches.py, or custom:<typed topic>), or
  * the Super Admin wrote its niche words (POST /api/admin/niche-guard {"dest_id", "words"}; setting niche_words).
A channel without a niche is never touched.

On-topic words = the niche's label, its search phrases, a few plain words of the same subject (EXTRA), the words the
Super Admin wrote, and the channel's typical sources: the search words of its active topic / trending sources, and
creators of whom 3 or more clips already went up on this channel, most of them on topic.

A waiting clip (queued / waiting for the helper / ready for review — never one already on YouTube, never one being
rendered right now, never my own reels or my own videos, never a hand-edited one) is off topic only when its source
video's title and tags share NO topic word with all that (conservative: one shared word keeps it).

Off-topic clips are PARKED (status "draft", error "Parked: off topic for <channel> ..."), never deleted. The daily
un-parking (autopilot.unpark_due) leaves them parked while the guard is on. When parking would take more than
MAX_SHARE of a channel's waiting clips, nothing is parked for that channel: the owner gets one message a day instead.
New videos: autopilot._process_video skips an off-topic video before it becomes a clip (off_topic_video).
"""
import json
import logging

from sqlalchemy import update

from . import keywords as kw
from .db import Channel, Clip, Destination, Project, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

MAX_SHARE = 0.4  # never park more than this share of a channel's waiting clips in one go (report instead)
WAITING = ("queued", "fetching", "rendering", "rendered")  # the channel's waiting line (for the share)
PARKABLE = ("queued", "fetching", "rendered")  # "rendering" is in flight: looked at again next round
TYPICAL_MIN = 3  # uploaded clips of one creator on this channel before the creator counts as a typical source
PARK_NOTE = "Parked: off topic for {dest} (its niche: {niche}). {why} Nothing is deleted: press Render to use it anyway."
WORDS_KEY = "niche_words"  # {dest_id: "words the Super Admin wrote"}
TOLD_KEY = "niche_guard_told"  # {dest_id: "YYYY-MM-DD"} the owner was told today that too much would be parked
LAST_KEY = "niche_guard_last"  # the last round's summary (shown on My channels)

# words that say nothing about a subject (they appear in the ready-made phrases of every niche)
GENERIC = set("""india indian hindi english desi vlog vlogs video videos tutorial tutorials explained explain beginner
    beginners tip tips review reviews best top famous new latest how what life lesson lessons basic basics routine short
    shorts moment moments full official viral trending part episode day today world""".split())
# plain words of the same subject, so "Best Biryani in Hyderabad" is food even though no ready-made phrase says biryani
EXTRA = {
    "food": """food foods street recipe recipes cook cooking cooked kitchen chef eat eating ate taste tasting tasty spicy
        restaurant dhaba thali biryani pizza burger momos momo chaat samosa dosa idli paneer chicken mutton tikka kebab
        paratha roti curry dal sweet sweets mithai dessert cake snack snacks breakfast lunch dinner khana khane nashta
        vada pav pani puri golgappa noodles maggi chai tea coffee juice lassi butter fry fried bbq grill
        chole bhature jalebi gulab jamun kulcha kachori sabzi litti chokha haleem rabri falooda sandwich gol gappe
        gappa egg omelette anda aloo tikki pakora pakode bhaji pav bhaji halwa kheer ladoo laddu barfi rasgulla
        rasmalai kulfi ice cream shake rolls roll kathi shawarma tandoori naan kulche rajma chawal rice pulao khichdi
        upma poha uttapam vadapav misal bhel sev puri dahi papdi lassi thandai sharbat nihari kebabs korma rogan josh
        fish prawn seafood keema handi kadai makhani masala dabeli pakoras chaap soya halwai mithaiwala vendor wala
        छोले भटूरे आलू टिक्की चाट समोसा जलेबी पकोड़े बिरयानी मोमोज़ मोमोज पानीपुरी गोलगप्पे खाना स्वाद मिठाई चाय पराठा डोसा
        नाश्ता कुल्फी लस्सी पनीर चिकन मटन""",
    "tech": """tech technology gadget gadgets phone phones smartphone mobile laptop computer pc unboxing iphone android
        samsung apple camera earbuds headphones watch smartwatch charger app apps ai software hardware""",
    "devops": """devops cloud aws azure gcp kubernetes docker linux programming coding code developer software python
        java javascript terraform ci cd pipeline server""",
    "comedy": """comedy funny fun prank joke jokes laugh laughing standup stand-up sketch meme memes roast hilarious""",
    "motivation": """motivation motivational inspiration inspiring success study discipline mindset speech goals dream
        dreams hustle""",
    "travel": """travel trip tour journey road mountains beach hill hills himachal goa manali kashmir kerala ladakh trek
        trekking adventure explore temple city village""",
    "fitness": """fitness workout gym exercise yoga weight loss muscle cardio abs diet protein running health healthy""",
    "cars": """car cars bike bikes motorcycle ride riding drive driving suv engine royal enfield scooter auto
        automobile racing""",
    "gaming": """game games gaming gameplay gamer minecraft gta pubg bgmi freefire fortnite valorant roblox console
        playstation xbox""",
    "education": """facts fact science knowledge gk general amazing learn learning history space physics chemistry
        biology study""",
    "finance": """finance money invest investing investment stock stocks share market mutual fund funds saving savings
        budget tax income rich""",
    "beauty": """beauty makeup skincare skin hair hairstyle fashion outfit saree style styling look lipstick""",
}


def enabled() -> bool:
    from .hooks import on

    return on("niche.guard")


def _stems(text: str) -> set:
    return {w for w in kw.topic_stems(text or "") if w not in GENERIC and len(w) > 1}


def _admin_words() -> dict:
    try:
        v = json.loads(get_setting(WORDS_KEY) or "{}")
    except (TypeError, ValueError):
        v = {}
    return {str(k): str(x) for k, x in v.items()} if isinstance(v, dict) else {}


def set_words(dest_id: int, words: str) -> dict:
    d = _admin_words()
    words = " ".join(str(words or "").split())[:2000]
    if words:
        d[str(int(dest_id))] = words
    else:
        d.pop(str(int(dest_id)), None)
    set_setting(WORDS_KEY, json.dumps(d, ensure_ascii=False))
    return d


def profile(s, dest_id: int) -> dict | None:
    """The niche of one channel: {"label", "stems", "typical": creators} — None when it has no niche."""
    from . import niches

    labels, text = [], []
    for ch in s.query(Channel).filter(Channel.destination_id == dest_id, Channel.niche.isnot(None),
                                      Channel.niche != "", Channel.auto.is_(True)):
        key = ch.niche or ""
        if key in niches.BY_KEY:
            n = niches.BY_KEY[key]
            labels.append(n["label"])
            text += [n["label"]] + list(n["phrases"]) + [EXTRA.get(key, "")]
        elif key.startswith("custom:"):
            t = key.split(":", 1)[1].replace("-", " ")
            labels.append(t)
            text.append(t)
        text.append(ch.query or "")
    typed = _admin_words().get(str(dest_id))
    if typed:
        labels.append(typed)
        text.append(typed)
        low = typed.lower()
        text += [v for k, v in EXTRA.items() if k in low]  # "food" typed -> the whole food word list (dishes, Hindi)
    if not labels:
        return None
    stems = set()
    for t in text:
        stems |= _stems(t)
    # the channel's typical sources: its own topic searches, and creators already on it with on-topic clips
    for ch in s.query(Channel).filter(Channel.destination_id == dest_id, Channel.auto.is_(True),
                                      Channel.kind.in_(["topic", "trending"])):
        if ch.query and not str(ch.query).isdigit():
            stems |= _stems(ch.query)
    seen: dict[str, list[int]] = {}
    rows = (s.query(Project.channel_url, Project.title, Project.tags).join(Clip, Clip.project_id == Project.id)
            .filter(Clip.destination_id == dest_id, Clip.status == "uploaded", Project.channel_url.isnot(None)).all())
    for url, title, tags in rows:
        on = bool(_video_stems(title, tags) & stems)
        n = seen.setdefault(url, [0, 0])
        n[0] += 1
        n[1] += int(on)
    typical = {u for u, (n, good) in seen.items() if n >= TYPICAL_MIN and good * 2 >= n}
    return {"label": ", ".join(dict.fromkeys(labels))[:120], "stems": stems, "typical": typical}


def _video_stems(title, tags) -> set:
    return _stems(" ".join([str(title or "")] + [str(t) for t in list(tags or [])[:12]]))


def why_off(prof: dict | None, title: str, tags, creator_url: str | None = None, mode: str | None = None) -> str:
    """'' = on topic (or no niche, or my own video). Else the plain reason it is off topic."""
    if not prof or mode in ("original", "own"):
        return ""
    if creator_url and creator_url in prof["typical"]:
        return ""
    words = _video_stems(title, tags)
    if not words:
        return ""  # nothing to judge by: keep it
    if words & prof["stems"]:
        return ""
    return "Its source video (“" + str(title or "")[:70] + "”) shares no topic word with the channel's niche."


def off_topic_video(s, dest_id: int | None, info: dict, mode: str | None = None) -> str:
    """For autopilot: a new video about to become a clip for this channel. '' = fine / guard off / no niche."""
    if not dest_id or not enabled():
        return ""
    try:
        if len([t for t in (info.get("tags") or []) if str(t).strip()]) < 3:
            return ""  # a skipped video is never tried again: only judge one whose tags say what it is about
        return why_off(profile(s, dest_id), info.get("title"), info.get("tags"), info.get("channel_url"), mode)
    except Exception:  # noqa: BLE001  (the guard never stops a source by failing)
        log.exception("niche guard: video check")
        return ""


def still_off(s, c: Clip, cache: dict | None = None) -> bool:
    """For the daily un-parking: is this parked clip still off topic (and the guard on)?"""
    if not enabled() or not (c.error or "").startswith("Parked: off topic"):
        return False
    cache = {} if cache is None else cache
    if c.destination_id not in cache:
        cache[c.destination_id] = profile(s, c.destination_id) if c.destination_id else None
    p = s.get(Project, c.project_id)
    return bool(p and why_off(cache[c.destination_id], p.title, p.tags, p.channel_url, p.mode))


def preview(s=None) -> dict:
    """What the guard would do now, per channel, without changing anything."""
    own = s is None
    s = s or SessionLocal()
    try:
        out = []
        for d in s.query(Destination).order_by(Destination.id):
            prof = profile(s, d.id)
            line = s.query(Clip).filter(Clip.destination_id == d.id, Clip.status.in_(WAITING), Clip.youtube_id.is_(None)).all()
            row = {"dest_id": d.id, "channel": d.name, "niche": prof["label"] if prof else None, "waiting": len(line),
                   "off": [], "action": "no niche: nothing is checked"}
            if prof:
                for c in line:
                    if c.status not in PARKABLE or c.meta_edited or (c.kw or {}).get("niche_parked"):
                        continue  # parked once and brought back by the owner: his choice stays
                    p = s.get(Project, c.project_id)
                    why = why_off(prof, p.title, p.tags, p.channel_url, p.mode) if p else ""
                    if why:
                        row["off"].append({"clip_id": c.id, "title": c.title, "source": p.title, "creator": p.channel,
                                           "status": c.status, "publish_at": c.publish_at.isoformat() + "Z" if c.publish_at else None,
                                           "why": why})
                share = len(row["off"]) / max(1, len(line))
                row["share"] = round(share, 2)
                if not row["off"]:
                    row["action"] = "all on topic"
                elif share > MAX_SHARE:
                    row["action"] = (f"too many ({len(row['off'])} of {len(line)} waiting clips): nothing is parked, "
                                     "the owner is told — check the channel's niche")
                else:
                    row["action"] = f"park {len(row['off'])} of {len(line)}"
                row["refused"] = bool(row["off"]) and share > MAX_SHARE
            out.append(row)
        return {"on": enabled(), "max_share": MAX_SHARE, "channels": out}
    finally:
        if own:
            s.close()


def _tell(dest_name: str, n: int, total: int, did: int):
    """One message a day per channel: parking would empty too much of the line."""
    day = utcnow().strftime("%Y-%m-%d")
    try:
        told = json.loads(get_setting(TOLD_KEY) or "{}")
    except (TypeError, ValueError):
        told = {}
    if told.get(str(did)) == day:
        return False
    told[str(did)] = day
    set_setting(TOLD_KEY, json.dumps(told))
    text = (f"Channel “{dest_name}”: {n} of its {total} waiting clips look off topic for its niche. That is more than "
            f"{int(MAX_SHARE * 100)}%, so nothing was parked. Please check the channel's niche (Sources → What is your "
            "channel about?) or the list in the Super Admin's niche check.")
    try:
        from . import phone

        phone._say(text, title="Raji Studio: off-topic clips", tags="warning", mail=True)
    except Exception as e:  # noqa: BLE001
        log.warning("niche guard message: %s", str(e)[:150])
    try:
        from . import acl

        acl.record_here("niche.guard", text[:480])
    except Exception:  # noqa: BLE001
        pass
    return True


def apply() -> dict:
    """Park the off-topic waiting clips of every channel with a niche (never more than MAX_SHARE of a channel's line)."""
    from .autopilot import SLOT_LOCK

    res = {"parked": 0, "refused": [], "channels": 0}
    if not enabled():
        res["off"] = True
        return res
    with SLOT_LOCK, SessionLocal() as s:
        view = preview(s)
        for row in view["channels"]:
            if not row["off"]:
                continue
            res["channels"] += 1
            if row.get("refused"):
                res["refused"].append({"dest_id": row["dest_id"], "off": len(row["off"]), "waiting": row["waiting"]})
                _tell(row["channel"] or "", len(row["off"]), row["waiting"], row["dest_id"])
                continue
            for o in row["off"]:
                note = PARK_NOTE.format(dest=row["channel"] or "this channel", niche=row["niche"], why=o["why"])[:1000]
                c = s.get(Clip, o["clip_id"])
                mark = dict((c.kw if c is not None else None) or {})
                mark["niche_parked"] = utcnow().isoformat() + "Z"  # parked once: if the owner uses it anyway, it stays
                done = s.execute(update(Clip).where(Clip.id == o["clip_id"], Clip.status == o["status"],
                                                    Clip.youtube_id.is_(None), Clip.meta_edited.isnot(True))
                                 .values(status="draft", autoqueue=False, job_lease=None, fetch_since=None,
                                         publish_at=None, error=note, kw=mark))
                if done.rowcount:
                    res["parked"] += 1
                    log.info("niche guard: parked clip %s (%s)", o["clip_id"], o["source"])
            s.commit()
    if res["parked"]:
        try:
            from . import acl

            acl.record_here("niche.guard", f"Parked {res['parked']} off-topic clip(s) (nothing deleted)")
        except Exception:  # noqa: BLE001
            pass
    set_setting(LAST_KEY, json.dumps({"at": utcnow().isoformat() + "Z", "parked": res["parked"],
                                      "refused": len(res["refused"])}))
    return res


HIDDEN_NOTE = "Kept private: off topic for this channel's niche (not deleted; it stays on YouTube as private)."


def hide_uploaded(limit: int = 12) -> dict:
    """Videos already on YouTube with a future publish time whose source is off topic: set to private with no publish
    time (youtube.keep_private, ~51 units each; nothing is deleted), so the channel shows one topic. Only while the
    day's budget still covers every waiting upload. The freed days are filled by the re-spacing."""
    from datetime import timedelta

    from . import quota, youtube
    from .comments import _uploads_due_here
    from .db import utcnow

    out = {"hidden": 0, "left": 0, "stopped": ""}
    if not enabled():
        return out
    now = utcnow()
    with SessionLocal() as s:
        rows = s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.kept_private.isnot(True),
                                    Clip.removed_at.is_(None), Clip.publish_at.isnot(None),
                                    Clip.publish_at > now + timedelta(hours=1)).order_by(Clip.publish_at).all()
        cache, todo = {}, []
        for c in rows:
            if c.destination_id not in cache:
                cache[c.destination_id] = profile(s, c.destination_id) if c.destination_id else None
            p = s.get(Project, c.project_id)
            if p is not None and why_off(cache[c.destination_id], p.title, p.tags, p.channel_url, p.mode):
                todo.append((c.id, c.destination_id, c.youtube_id))
    out["left"] = len(todo)
    bad = set()
    for cid, did, vid in todo[:limit]:
        if did in bad:
            continue
        if not quota.can_update(_uploads_due_here()) or not quota.shared_room(quota.COST["update"] + quota.COST["read"]):
            out["stopped"] = "today's YouTube budget is kept for uploads"
            break
        try:
            youtube.keep_private(did, vid)
        except LookupError:
            pass  # gone from YouTube: it cannot go public either
        except youtube.NeedsReconnect:
            bad.add(did)
            continue
        except Exception as e:  # noqa: BLE001
            m = str(e)
            log.warning("niche guard: could not keep video %s private: %s", vid, m[:200])
            if "quotaExceeded" in m or "dailyLimitExceeded" in m:
                quota.exhaust()
                out["stopped"] = "YouTube's daily budget is used up"
                break
            continue
        with SessionLocal() as s:
            c = s.get(Clip, cid)
            if c is not None:
                c.kept_private, c.live_noted, c.error = True, True, HIDDEN_NOTE
                if c.comment_id is None:
                    c.comment_state = "off"  # a private video gets no first comment
                s.commit()
        out["hidden"] += 1
        out["left"] -= 1
        try:
            from . import acl

            acl.record_here("niche.guard", f"Kept private (off topic): clip #{cid} — video {vid} stays on YouTube as private")
        except Exception:  # noqa: BLE001
            pass
    if out["hidden"]:
        try:
            from . import tuneups

            out["respaced"] = tuneups.respace_waiting()  # the freed days get waiting clips
        except Exception:  # noqa: BLE001
            log.exception("niche guard: re-spacing after keeping videos private")
    return out


PURGE_KEY = "purge_offtopic"  # the owner asked (26-27 Nov 2026) to delete every off-topic video: runs until done


def purge(limit: int = 20, dry: bool = False, also: list | None = None) -> dict:
    """The owner's explicit request: delete what is off topic for the channel's niche.
    * dashboard: clips parked as off topic (never uploaded) — row and files deleted;
    * YouTube: off-topic videos of the channel that are still PRIVATE (scheduled, never public) — deleted on YouTube
      (youtube.delete_private_video refuses anything public), then the clip row is marked removed.
    Public videos are never touched. Within the day's YouTube budget (uploads first); the rest the next run."""
    import shutil

    from . import quota, youtube
    from .comments import _uploads_due_here
    from .config import DATA_DIR
    from .db import utcnow

    out = {"dashboard": 0, "youtube": 0, "left_youtube": 0, "kept": [], "stopped": "", "dry": dry}
    with SessionLocal() as s:
        parked = [c.id for c in s.query(Clip).filter(Clip.status == "draft", Clip.youtube_id.is_(None),
                                                     Clip.error.like("Parked: off topic%"))]
        cache, yt = {}, []
        for c in s.query(Clip).filter(Clip.youtube_id.isnot(None), Clip.removed_at.is_(None),
                                      Clip.status == "uploaded"):
            if c.destination_id not in cache:
                cache[c.destination_id] = profile(s, c.destination_id) if c.destination_id else None
            p = s.get(Project, c.project_id)
            if p is not None and why_off(cache[c.destination_id], p.title, p.tags, p.channel_url, p.mode):
                if c.publish_at is None or c.publish_at > utcnow() or c.kept_private:
                    yt.append((c.id, c.destination_id, c.youtube_id, c.title or ""))
    out["left_youtube"] = len(yt)
    if dry:
        out["dashboard"], out["youtube"] = len(parked), len(yt)
        out["titles"] = [t[:60] for *_x, t in yt]
        return out
    with SessionLocal() as s:
        for cid in parked:
            c = s.get(Clip, cid)
            if c is not None and c.youtube_id is None and c.status == "draft":
                shutil.rmtree(DATA_DIR / "clips" / str(cid), ignore_errors=True)
                s.delete(c)
                out["dashboard"] += 1
        s.commit()
    bad = set()
    for cid, did, vid, title in yt[:limit]:
        if did in bad:
            continue
        if not quota.can_update(_uploads_due_here()) or not quota.shared_room(quota.COST["update"] + quota.COST["read"]):
            out["stopped"] = "today's YouTube budget is kept for uploads"
            break
        try:
            res = youtube.delete_private_video(did, vid)
        except youtube.NeedsReconnect:
            bad.add(did)
            continue
        except Exception as e:  # noqa: BLE001
            m = str(e)
            log.warning("purge: could not delete %s: %s", vid, m[:200])
            if "quotaExceeded" in m or "dailyLimitExceeded" in m:
                quota.exhaust()
                out["stopped"] = "YouTube's daily budget is used up"
                break
            continue
        if res.startswith("kept:"):
            out["kept"].append(vid)  # public / unlisted: never deleted by this
            continue
        with SessionLocal() as s:
            c = s.get(Clip, cid)
            if c is not None:
                c.removed_at, c.yt_state, c.kept_private = utcnow(), "deleted", True
                c.error = "Deleted from YouTube on the owner's request: off topic for this channel."
                s.commit()
        out["youtube"] += 1
        out["left_youtube"] -= 1
        try:
            from . import acl

            acl.record_here("niche.guard", f"Deleted off-topic private video {vid} (clip #{cid}: {title[:60]})")
        except Exception:  # noqa: BLE001
            pass
    if out["youtube"]:
        try:
            from . import tuneups

            out["respaced"] = tuneups.respace_waiting()  # their days get food clips
        except Exception:  # noqa: BLE001
            log.exception("purge: re-spacing")
    if not out["left_youtube"] and not out["stopped"]:
        set_setting(PURGE_KEY, "")
    return out


def guard_due() -> dict:
    """Timed job (switch "niche.guard", every 6 hours per account)."""
    out = apply()
    try:
        out = dict(out or {})
        if get_setting(PURGE_KEY):
            out["purge"] = purge()  # the owner's delete request, until everything off topic is gone
        out["private"] = hide_uploaded()
    except Exception:  # noqa: BLE001
        log.exception("niche guard: keeping off-topic videos private")
    return out


def last() -> dict:
    try:
        v = json.loads(get_setting(LAST_KEY) or "{}")
    except (TypeError, ValueError):
        v = {}
    return v if isinstance(v, dict) else {}

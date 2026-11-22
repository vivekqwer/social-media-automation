"""First comment: after every upload the channel writes its own first comment under the new video.

YouTube's API can post a comment (commentThreads.insert, 50 quota units, scope youtube.force-ssl) but it has NO way to
pin one. So the system posts it, then tells the owner (Telegram / phone / mail) with the video link: open it, tap ⋮
next to the comment, Pin. The dashboard says this plainly.

Rules:
* Only videos uploaded after this feature exists get a comment: the upload marks the clip "waiting" (or "off").
* A comment is posted only when the video is public: a scheduled video waits until its publish time (job every
  10 minutes; the video's real state is checked first, 1 unit).
* Never twice: comment_id is kept; the claim "waiting -> posting" is atomic; and before posting, the video's newest
  comments are read once (1 unit) — one written by the channel itself is taken as the first comment.
* Uploads come first: a comment waits when the day's YouTube budget is needed for the uploads still to go up — of every
  account on that budget (the owner and each customer using the server's Google project share one).
* Account switch off: nothing is posted; comments already waiting stay waiting (posted if the switch is turned on again
  within 14 days, else quietly "off"); videos uploaded while it is off get none. A clip's own "on" does not override it
  (as with the like & subscribe reminder). The 14 days count from the publish time.
* A login made before comments were added lacks the scope: "no permission", no call to Google; the comment goes out by
  itself after the channel is connected once more (for videos of the last two weeks).
* Network trouble / YouTube busy: tried again (5 times, longer gaps). Comments switched off, made for kids, refused:
  recorded with the reason in plain words, shown on the clip and in Alerts, not tried again.

Account setting "first_comment" (JSON): {"on": bool, "preset": key | "custom", "custom": text}. Each clip has its own
switch (Clip.comment_on) and may have its own words (Clip.comment_text). Placeholders: {title} {channel} {hashtags}.
"""
import json
import logging
import re
import unicodedata
from datetime import timedelta

from sqlalchemy import update

from .db import Clip, Destination, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

MAX_LEN = 500
MAX_TRIES = 5
GIVE_UP_DAYS = 14  # a video that is still not public (or a channel not reconnected) after this gets no comment
PRESETS = [
    ("ask", "Which one would you try first? 👇"),
    ("full", "Full video on the channel ❤️"),
    ("think", "What do you think about {title}? Tell me in the comments 👇"),
    ("sub", "Subscribe to {channel} for more like this 🔔"),
    ("hinglish", "Aapko kaunsa sabse accha laga? Comment mein batao 👇"),
    ("hi", "आपको कौन सा सबसे अच्छा लगा? कमेंट में बताइए 👇"),
]
DEFAULT = "ask"
PLACEHOLDERS = ["{title}", "{channel}", "{hashtags}"]
PIN_HOW = "Open the video → comments → tap ⋮ next to your comment → Pin."
# YouTube's reasons that will not go away by trying again -> what the owner reads
PERMANENT = {
    "commentsDisabled": "comments are turned off on this video (videos marked “made for kids” never allow comments)",
    "forbidden": "YouTube did not allow this channel to comment on the video",
    "ineligibleAccount": "this YouTube account cannot comment yet (it needs a channel with a name)",
    "invalidCommentMetadata": "YouTube refused the words of the comment",
    "commentTextTooLong": "the comment is too long",
    "operationNotSupported": "YouTube does not allow comments here",
    "processingFailure": "YouTube could not process the comment",
}
NO_SCOPE = ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
_MULTI_NL = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]{2,}")
_TAG = re.compile(r"#[^\s#]+")


def clean(text) -> str:
    """Words as typed, made safe: no control characters, at most two empty lines, at most 500 characters."""
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = "".join(ch for ch in t if ch == "\n" or ch >= " ")
    return _MULTI_NL.sub("\n\n", t).strip()[:MAX_LEN].strip()


def settings() -> dict:
    """The account's choice with defaults filled in (a fresh account: on, the first preset)."""
    try:
        raw = json.loads(get_setting("first_comment") or "{}") or {}
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    names = dict(PRESETS)
    custom = clean(raw.get("custom"))
    preset = raw.get("preset") if raw.get("preset") in names or raw.get("preset") == "custom" else DEFAULT
    if preset == "custom" and not custom:
        preset = DEFAULT
    return {"on": raw.get("on") is not False, "preset": preset, "custom": custom,
            "text": custom if preset == "custom" else names[preset],
            "presets": [{"key": k, "text": v} for k, v in PRESETS], "max": MAX_LEN, "placeholders": PLACEHOLDERS,
            "pin_how": PIN_HOW}


def save(on: bool, preset: str, custom: str) -> dict:
    set_setting("first_comment", json.dumps({"on": bool(on), "preset": preset, "custom": clean(custom)}))
    return settings()


def hashtags(clip, n: int = 3) -> str:
    """The clip's hashtags (from its title and description), else made from its first tags."""
    out, seen = [], set()
    for m in _TAG.findall(f"{getattr(clip, 'title', '') or ''}\n{getattr(clip, 'description', '') or ''}"):
        tag = m.rstrip(".,!?;:)]}\"'")
        if len(tag) > 1 and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    if not out:
        for t in getattr(clip, "tags", None) or []:
            word = "".join(ch for ch in str(t) if ch == "_" or unicodedata.category(ch)[0] in "LMN")  # Hindi marks kept
            if word and ("#" + word).lower() not in seen:
                seen.add(("#" + word).lower())
                out.append("#" + word)
    return " ".join(out[:n])


def fill(template: str, title: str = "", channel: str = "", tags: str = "") -> str:
    t = str(template or "").replace("{title}", str(title or "").strip()).replace("{channel}", str(channel or "").strip()) \
        .replace("{hashtags}", str(tags or "").strip())
    return _MULTI_NL.sub("\n\n", "\n".join(_SPACES.sub(" ", x).rstrip() for x in t.split("\n"))).strip()[:1000]


def text_for(clip, channel_name: str = "", cfg: dict | None = None) -> str:
    """The comment this clip gets: its own words, else the account's words; placeholders filled in."""
    own = clean(getattr(clip, "comment_text", None))
    template = own or (cfg or settings())["text"]
    return fill(template, getattr(clip, "title", "") or "", channel_name, hashtags(clip))


def _feature_on() -> bool:
    try:
        from . import acl

        return acl.feature("uploads.comment")
    except Exception:  # noqa: BLE001
        return True


def after_upload(c) -> None:
    """Called when a clip has just been uploaded (before its row is saved): it waits for its comment, or not."""
    try:
        if c.comment_id or c.comment_state:
            return
        if c.comment_on is False or not _feature_on() or not settings()["on"]:
            c.comment_state = "off"
            return
        c.comment_state, c.comment_tries = "waiting", 0
        c.comment_next = c.publish_at if c.publish_at and c.publish_at > utcnow() else None
    except Exception as e:  # noqa: BLE001  (the upload is done; a comment must never undo that)
        log.warning("first comment: %s", str(e)[:200])


def set_for_clip(c, on: bool | None, text: str | None) -> None:
    """The editor: this clip's switch and words. An uploaded video without its comment yet can get one now."""
    if text is not None:
        c.comment_text = clean(text) or None
    if on is not None:
        c.comment_on = bool(on)
    if c.status != "uploaded" or c.comment_id:
        return
    if c.comment_on is False:
        if (c.comment_state or "").startswith(("waiting", "no permission")):
            c.comment_state = "off"
    elif on and (not c.comment_state or c.comment_state == "off" or c.comment_state.startswith(("failed", "no permission"))):
        c.comment_state, c.comment_tries, c.comment_next = "waiting", 0, None


def _reason(e) -> tuple[int, str, str]:
    """(HTTP status, YouTube's reason word, its message) of an API error."""
    status = int(getattr(getattr(e, "resp", None), "status", 0) or getattr(e, "status_code", 0) or 0)
    reason, msg = "", str(e)
    try:
        raw = getattr(e, "content", b"") or b""
        err = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).get("error") or {}
        msg = err.get("message") or msg
        errs = err.get("errors") or []
        reason = (errs[0].get("reason") if errs else "") or ""
        if not reason:
            for d in err.get("details") or []:
                reason = d.get("reason") or reason
    except Exception:  # noqa: BLE001
        pass
    return status, reason, msg


def budget_tenants() -> list[int]:
    """Every copy that spends from the same YouTube budget as the running one (the rule of quota._BudgetSession):
    a customer with their own Google keys has their own budget; the owner and every customer without own keys
    (using the server's project, e.g. "Share my Google project") share the owner's."""
    from . import tenant

    cur = tenant.current()
    if cur != tenant.OWNER and get_setting("google_client_id"):
        return [cur]
    try:
        from .accounts import active_tenants

        ids = active_tenants()
    except Exception:  # noqa: BLE001  (accounts table unreachable: at least the owner and this copy)
        ids = [tenant.OWNER]
    if cur not in ids:
        ids.append(cur)
    out = []
    for tid in ids:
        if tid == tenant.OWNER:
            out.append(tid)
            continue
        try:
            with tenant.use(tid):
                if not get_setting("google_client_id"):
                    out.append(tid)
        except Exception:  # noqa: BLE001  (an unreadable copy: count it, so uploads stay safe)
            out.append(tid)
    return out


def _uploads_due_here() -> int:
    """This copy's uploads that will really go up before the next reset: queued clips due by then on channels that are
    not paused, no more than the account's uploads-a-day limit leaves for today, none while its uploads are paused."""
    from . import acl, quota
    from .config import MAX_UPLOADS_PER_DAY, UPLOAD_LEAD_HOURS
    from .scheduler import _pacific_midnight

    try:
        if acl.upload_block():
            return 0
        cap = acl.uploads_cap(MAX_UPLOADS_PER_DAY)
    except Exception:  # noqa: BLE001
        cap = MAX_UPLOADS_PER_DAY
    now = utcnow()
    with SessionLocal() as s:
        paused = [d.id for d in s.query(Destination.id).filter(Destination.paused.is_(True))]
        until = quota.next_reset() + timedelta(hours=UPLOAD_LEAD_HOURS)
        q = s.query(Clip).filter(Clip.status.in_(["queued", "uploading"]),
                                 (Clip.publish_at.is_(None)) | (Clip.publish_at <= until))
        if paused:
            q = q.filter((Clip.destination_id.is_(None)) | (Clip.destination_id.notin_(paused)))
        due = q.count()
        done_today = s.query(Clip).filter(Clip.attempted_at >= _pacific_midnight(now)).count()
    return min(due, max(0, cap - done_today))


def uploads_waiting() -> int:
    """Uploads that still want today's budget, over every account that shares it (the owner's too when the budget is
    the owner's project)."""
    from . import tenant

    total = 0
    for tid in budget_tenants():
        try:
            with tenant.use(tid):
                total += _uploads_due_here()
        except Exception as e:  # noqa: BLE001
            log.warning("uploads waiting for %s: %s", tid, str(e)[:150])
    return total


def _notify(title: str, video_id: str):
    link = f"https://youtu.be/{video_id}"
    try:
        from . import phone, telegram

        e = lambda t: str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
        msg = (f"💬 Comment posted on <b>{e(title)}</b> — open the video and pin it: ⋮ → Pin\n{link}\n"
               "YouTube does not let apps pin comments, so this one tap is yours.")
        pushed = phone.push(msg, title="Comment posted — pin it", url=link, tags="speech_balloon") if phone.enabled() else False
        if telegram.enabled() or not pushed:
            telegram.notify(msg, push_phone=False)
    except Exception as ex:  # noqa: BLE001  (a message must never break the comment job)
        log.warning("comment notify: %s", str(ex)[:200])


def _set(cid: int, **values):
    with SessionLocal() as s:
        s.execute(update(Clip).where(Clip.id == cid).values(**values))
        s.commit()


def _transient(c, why: str, now):
    tries = int(c.comment_tries or 0) + 1
    if tries >= MAX_TRIES:
        _set(c.id, comment_state=f"failed: tried {tries} times — {why}"[:300], comment_tries=tries)
    else:
        _set(c.id, comment_state=f"waiting: trying again soon ({why})"[:300], comment_tries=tries,
             comment_next=now + timedelta(minutes=10 * 2 ** tries))


def post_due(only: int | None = None) -> dict:
    """The 10-minute job: post every comment that is due. Returns counts (for tests and logs)."""
    from googleapiclient.errors import HttpError

    from . import alerts, quota, youtube
    from .crypto import TokenError

    done = {"posted": 0, "waiting": 0, "no_permission": 0, "failed": 0}
    if not _feature_on():
        return done
    now = utcnow()
    oldest = now - timedelta(days=GIVE_UP_DAYS)
    with SessionLocal() as s:
        q = s.query(Clip).filter(Clip.status == "uploaded", Clip.youtube_id.isnot(None), Clip.comment_id.is_(None),
                                 Clip.removed_at.is_(None),
                                 (Clip.comment_state.like("waiting%")) | (Clip.comment_state == "no permission"))
        if only:
            q = q.filter(Clip.id == only)
        rows = q.order_by(Clip.publish_at, Clip.id).all()
        dests = {d.id: d for d in s.query(Destination)}
    if not rows:
        return done
    cfg = settings()
    by_dest: dict = {}
    for c in rows:
        # 14 days from when the video goes public (a video scheduled weeks ahead must not run out before then)
        start = max(x for x in (c.attempted_at or c.created_at or now, c.publish_at) if x is not None)
        if start < oldest:
            if not cfg["on"]:  # held only because the account switched comments off: simply none for this video
                _set(c.id, comment_state="off")
                continue
            _set(c.id, comment_state="failed: the video did not become public in time, so no comment was posted"
                 if (c.comment_state or "").startswith("waiting") else
                 "failed: the channel was not connected again in time to allow comments")
            done["failed"] += 1
            continue
        if not cfg["on"] or c.publish_at and c.publish_at > now or (c.comment_next and c.comment_next > now):
            # account switch off (My channels → First comment): nothing is posted, the comment keeps waiting and goes
            # out if the switch is turned on again within the 14 days. A clip's own "on" does not override it.
            done["waiting"] += 1
            continue
        by_dest.setdefault(c.destination_id, []).append(c)
    skip_dest = set()
    _need: list = []

    def uploads_need() -> int:  # every account on this budget, counted once per run
        if not _need:
            _need.append(uploads_waiting())
        return _need[0]
    for did, clips in by_dest.items():
        d = dests.get(did)
        if not d:
            continue
        if not youtube.can_comment(did):  # an older login: no call to Google at all
            for c in clips:
                if c.comment_state != "no permission":
                    _set(c.id, comment_state="no permission")
                done["no_permission"] += 1
            continue
        try:  # is each video really public now? (scheduled ones flip at their time; new projects may stay private)
            if not quota.can_comment(uploads_need()):
                done["waiting"] += sum(1 for _ in clips)
                break
            live = youtube.video_status(did, [c.youtube_id for c in clips])
        except (TokenError, youtube.NeedsReconnect) as e:  # login expired: uploads say so already; comments wait
            log.info("comments wait for %s: %s", did, type(e).__name__)
            continue
        except Exception as e:  # noqa: BLE001
            if "RefreshError" in type(e).__name__ or "invalid_grant" in str(e):
                continue
            alerts.note("first comment: checking videos", e)
            continue
        for c in clips:
            if did in skip_dest:
                break
            item = live.get(c.youtube_id)
            if item is None:
                _set(c.id, comment_state="failed: the video is no longer on YouTube")
                done["failed"] += 1
                continue
            st = item.get("status") or {}
            if st.get("uploadStatus") in ("rejected", "failed", "deleted"):
                _set(c.id, comment_state="failed: YouTube did not publish the video")
                done["failed"] += 1
                continue
            if st.get("privacyStatus") != "public":
                _set(c.id, comment_state="waiting: the video is not public yet", comment_next=now + timedelta(minutes=30))
                done["waiting"] += 1
                continue
            if not quota.can_comment(uploads_need()):  # the budget is needed for uploads: try after the reset
                done["waiting"] += 1
                skip_dest.add(did)
                break
            # atomic claim: only one runner may post this clip's comment
            with SessionLocal() as s:
                res = s.execute(update(Clip).where(Clip.id == c.id, Clip.comment_id.is_(None),
                                                   Clip.comment_state == c.comment_state).values(comment_state="posting"))
                s.commit()
            if res.rowcount != 1:
                continue
            text = text_for(c, d.name or "", cfg)
            try:
                yt = youtube.comment_client(did)
                found = None
                r = yt.commentThreads().list(part="snippet", videoId=c.youtube_id, maxResults=50, order="time",
                                             textFormat="plainText").execute()
                quota.spend("read")
                for it in r.get("items") or []:
                    sn = ((it.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
                    if ((sn.get("authorChannelId") or {}).get("value")) == d.channel_id:
                        found = it.get("id")
                        break
                if found:  # the channel already has a comment there (an earlier run, or the owner): never a second one
                    _set(c.id, comment_id=str(found)[:64], comment_state="posted")
                    done["posted"] += 1
                    continue
                if not text:
                    _set(c.id, comment_state="failed: the comment words are empty (My channels → First comment)")
                    done["failed"] += 1
                    continue
                quota.spend("comment")
                resp = yt.commentThreads().insert(part="snippet", body={"snippet": {
                    "videoId": c.youtube_id, "topLevelComment": {"snippet": {"textOriginal": text}}}}).execute()
                _set(c.id, comment_id=str(resp.get("id") or "posted")[:64], comment_state="posted", comment_next=None)
                done["posted"] += 1
                _notify(c.title or f"Clip #{c.id}", c.youtube_id)
            except youtube.NeedsReconnect:
                _set(c.id, comment_state="no permission")
                done["no_permission"] += 1
            except TokenError:
                _set(c.id, comment_state=c.comment_state)  # login problem: back to how it was, try later
                skip_dest.add(did)
            except HttpError as e:
                status, why, msg = _reason(e)
                if why in ("quotaExceeded", "dailyLimitExceeded"):
                    quota.exhaust()
                    _set(c.id, comment_state=c.comment_state)
                    return done
                if why in NO_SCOPE or (status == 403 and "insufficient" in msg.lower() and "scope" in msg.lower()):
                    _set(c.id, comment_state="no permission")
                    done["no_permission"] += 1
                elif why in PERMANENT or status in (400, 403):
                    _set(c.id, comment_state=f"failed: {PERMANENT.get(why) or 'YouTube said: ' + msg[:200]}"[:300])
                    done["failed"] += 1
                else:  # 404 right after publishing, 409, 429, 5xx: YouTube busy -> again later
                    _transient(c, f"YouTube said: {msg[:120]}", now)
                    done["waiting"] += 1
            except Exception as e:  # noqa: BLE001  (network trouble and the like)
                if "RefreshError" in type(e).__name__ or "invalid_grant" in str(e):
                    _set(c.id, comment_state=c.comment_state)
                    skip_dest.add(did)
                    continue
                alerts.note("first comment", e)
                _transient(c, str(e)[:120] or type(e).__name__, now)
                done["waiting"] += 1
    return done


def recover_stuck():
    """A run that stopped between the claim and saving the answer leaves "posting": look again (the list check above
    finds a comment that did go out, so nothing is posted twice)."""
    with SessionLocal() as s:
        s.query(Clip).filter(Clip.comment_state == "posting", Clip.comment_id.is_(None)) \
            .update({"comment_state": "waiting"}, synchronize_session=False)
        s.commit()

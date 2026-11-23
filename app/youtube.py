import threading
import json
import os
import re
import secrets
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import BASE_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, IS_LOCAL
from . import auth, quota
from .crypto import decrypt, encrypt
from .db import Destination, SessionLocal, get_setting, set_setting, main_destination

EMAIL_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"  # watch time, traffic sources, countries
COMMENT_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"  # post the first comment under each new video
# New connections ask for all of these. A stored login keeps the scopes it was made with (a refresh asks for exactly
# those), so older channels go on uploading; they only skip what needs a newer scope until they are connected again.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly",
          COMMENT_SCOPE, ANALYTICS_SCOPE] + EMAIL_SCOPES
REDIRECT = f"{BASE_URL}/auth/callback"

if IS_LOCAL:
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def google_client() -> tuple[str, str]:
    """(client id, secret) of the Google project this copy talks to Google with: its own keys when it has them;
    else the server's — for the owner, and for a customer the Super Admin allowed to share it."""
    from . import tenant

    cid = get_setting("google_client_id")
    if cid:
        try:
            return cid, decrypt(get_setting("google_client_secret") or "")
        except Exception:  # noqa: BLE001
            return cid, ""
    if tenant.current() == tenant.OWNER or get_setting("shared_google") == "1":
        return GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    return "", ""


def configured() -> bool:
    return all(google_client())


def _flow(scopes: list | None = None) -> Flow:
    client_id, client_secret = google_client()
    cfg = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT],
        }
    }
    return Flow.from_client_config(cfg, scopes=scopes or SCOPES, redirect_uri=REDIRECT, autogenerate_code_verifier=False)


def login_url() -> str:
    # select_account lets the user pick a different Google account/channel each time
    url, state = _flow().authorization_url(
        access_type="offline", prompt="consent select_account", include_granted_scopes="true"
    )
    set_setting("oauth_state", state)
    return url


_connect_lock = threading.Lock()


class ChannelLimit(ValueError):
    """The account's plan does not allow another channel."""


def handle_callback(code: str, state: str, channel_limit: int | None = None) -> int:
    """Exchange the code, look up which channel it is, and store it as a destination. Returns destination id.
    channel_limit: how many channels this account may have (None = no limit); reconnecting one is always allowed."""
    with _connect_lock:
        expected = get_setting("oauth_state")
        set_setting("oauth_state", "")  # single use
    if not expected or state != expected:
        raise ValueError("OAuth state mismatch")
    f = _flow()
    f.fetch_token(code=code)
    creds = f.credentials
    granted = _granted_scopes(f)
    email = _email_from(creds)
    if email:  # this Google account may later reset the dashboard password
        auth.remember_email(email)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        raise ValueError("This Google account has no YouTube channel.")
    cid, name = items[0]["id"], items[0]["snippet"]["title"]
    with _connect_lock, SessionLocal() as s:
        d = s.query(Destination).filter(Destination.channel_id == cid).first()
        if d is None:
            have = s.query(Destination).count()
            if channel_limit is not None and have >= channel_limit:
                raise ChannelLimit(f"Your plan allows {channel_limit} YouTube channel{'' if channel_limit == 1 else 's'} and "
                                   f"{have} {'is' if have == 1 else 'are'} already connected. Ask for more channels "
                                   "(My channels → More channels), or disconnect one first.")
            d = Destination(channel_id=cid)
        info = json.loads(creds.to_json())
        if granted:  # what Google really allowed (a box left unticked is missing), not only what was asked for
            info["scopes"] = granted
        d.name, d.token = name, encrypt(json.dumps(info))
        s.add(d)
        s.commit()
        return d.id


def _granted_scopes(f) -> list | None:
    try:
        sc = (f.oauth2session.token or {}).get("scope")
        sc = sc.split() if isinstance(sc, str) else sc
        return [str(x) for x in sc] if sc else None
    except Exception:  # noqa: BLE001
        return None


def login_scopes(dest_id: int) -> list | None:
    """The scopes a channel's stored login has (None = not known: a very old login). Nothing is sent to Google."""
    with SessionLocal() as s:
        d = s.get(Destination, dest_id)
        if not d or not d.token:
            return []
        try:
            sc = json.loads(decrypt(d.token)).get("scopes")
        except Exception:  # noqa: BLE001  (unreadable login: it needs a reconnect anyway)
            return []
    if isinstance(sc, str):
        sc = sc.split()
    return list(sc) if sc is not None else None


def can_comment(dest_id: int) -> bool:
    """May this channel's login post comments? Logins made before comments were added lack the scope: they keep
    uploading, and their comments wait until the channel is connected once more."""
    sc = login_scopes(dest_id)
    return sc is None or COMMENT_SCOPE in sc


def _default_dest_id() -> int | None:
    with SessionLocal() as s:
        d = main_destination(s)
        return d.id if d else None


def token_client_ok(info: dict) -> bool:
    """A customer's stored channel login must belong to the Google project their copy uses now: after the Super Admin
    stops sharing their project, or the customer enters their own keys, the old login must not keep uploading through
    (and using up) somebody else's project. The owner's own copy is never checked."""
    from . import tenant

    if tenant.current() == tenant.OWNER:
        return True
    made_with = (info or {}).get("client_id")
    return not made_with or made_with == google_client()[0]


def _creds(dest_id: int | None):
    dest_id = dest_id or _default_dest_id()
    if not dest_id:
        raise RuntimeError("No YouTube channel connected. Use 'Connect my channel' on the dashboard.")
    with SessionLocal() as s:
        d = s.get(Destination, dest_id)
        if not d:
            raise RuntimeError(f"Destination {dest_id} not found")
        info = json.loads(decrypt(d.token))
        if not token_client_ok(info):
            from .crypto import TokenError

            raise TokenError("This channel was connected through another Google project. Connect it again.")
        # use the scopes this login actually has (older logins have no Analytics scope; asking for it would fail)
        creds = Credentials.from_authorized_user_info(info)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            d.token = encrypt(creds.to_json())
            s.commit()
    return creds


def client(dest_id: int | None = None):
    return build("youtube", "v3", credentials=_creds(dest_id), cache_discovery=False)


class NeedsReconnect(Exception):
    """This channel's login was made before watch-time stats were added: connect it once more."""


def analytics_client(dest_id: int):
    creds = _creds(dest_id)
    if creds.scopes is not None and ANALYTICS_SCOPE not in creds.scopes:
        raise NeedsReconnect()
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def comment_client(dest_id: int):
    creds = _creds(dest_id)
    if creds.scopes is not None and COMMENT_SCOPE not in creds.scopes:
        raise NeedsReconnect()
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(dest_id: int | None, path: str, title: str, description: str, tags: list[str], publish_at=None) -> str:
    body = {
        "snippet": {"title": title[:100], "description": description, "tags": tags, "categoryId": "24"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    if publish_at:
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    media = MediaFileUpload(path, chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    yt = client(dest_id)  # an expired login fails here, before any quota is used
    quota.spend("upload")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk(num_retries=5)
    return resp["id"]


def video_status(dest_id: int, ids: list[str]) -> dict:
    """{video_id: item} for my own videos (owner view, so private/scheduled ones are included).
    A missing id means the video no longer exists on YouTube."""
    yt, out = client(dest_id), {}
    for i in range(0, len(ids), 50):
        r = yt.videos().list(part="status,contentDetails", id=",".join(ids[i:i + 50]), maxResults=50).execute()
        quota.spend("read")
        out.update({it["id"]: it for it in r.get("items", [])})
    return out


KEEP_STATUS = ("embeddable", "license", "publicStatsViewable", "selfDeclaredMadeForKids", "containsSyntheticMedia")


def keep_private(dest_id: int, video_id: str) -> str:
    """Set one of my videos to private with NO publish time (videos.update, part=status; 1 + 50 quota units), so a video
    that was uploaded early and scheduled never goes public by itself. Nothing is deleted. Returns what it was before
    ("private" / "unlisted" / "public"). An update replaces the whole status part, so the other status settings of the
    video are read first and sent back unchanged; leaving publishAt out is what clears the schedule.
    Needs the wider login (youtube.force-ssl): a channel connected before comments were added raises NeedsReconnect."""
    yt = comment_client(dest_id)
    r = yt.videos().list(part="status", id=video_id, maxResults=1).execute()
    quota.spend("read")
    items = r.get("items") or []
    if not items:
        raise LookupError("The video is no longer on YouTube.")
    old = items[0].get("status") or {}
    was = str(old.get("privacyStatus") or "")
    if was == "private" and not old.get("publishAt"):
        return was  # already private with no publish time: nothing to change, no quota used
    status = {k: old[k] for k in KEEP_STATUS if k in old}
    status["privacyStatus"] = "private"
    quota.spend("update")
    yt.videos().update(part="status", body={"id": video_id, "status": status}).execute()
    return was


def _email_from(creds) -> str | None:
    """Verified e-mail of the signed-in Google account, from the OpenID id_token."""
    try:
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(creds.id_token, Request(), google_client()[0] or GOOGLE_CLIENT_ID)
        return claims["email"].lower() if claims.get("email_verified") else None
    except Exception:  # noqa: BLE001
        return None


def reset_url() -> str:
    """Start Google sign-in whose only purpose is proving which account the user owns (no YouTube access)."""
    state = "reset:" + secrets.token_urlsafe(16)
    set_setting("reset_state", f"{state}|{int(time.time()) + 600}")
    url, _ = _flow(EMAIL_SCOPES).authorization_url(state=state, access_type="online", prompt="select_account")
    return url


def handle_reset_callback(code: str, state: str) -> str:
    raw = get_setting("reset_state") or ""
    set_setting("reset_state", "")  # single use
    saved, _, exp = raw.partition("|")
    if not saved or state != saved or int(exp or 0) < time.time():
        raise ValueError("This reset link expired. Start again.")
    f = _flow(EMAIL_SCOPES)
    f.fetch_token(code=code)
    email = _email_from(f.credentials)
    if not email:
        raise ValueError("Could not verify your Google e-mail address.")
    return email


# ---------- read-only helpers via the official API (work from a datacenter IP, no cookies needed) ----------
CC_LICENSE = "Creative Commons Attribution license (reuse allowed)"


def _iso_seconds(d: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    days, h, mi, sec = (int(x or 0) for x in m.groups())
    return days * 86400 + h * 3600 + mi * 60 + sec


def api_video_info(video_id: str) -> dict | None:
    r = client().videos().list(part="snippet,contentDetails,status", id=video_id).execute()
    quota.spend("read")
    items = r.get("items") or []
    if not items:
        return None
    it = items[0]
    sn = it["snippet"]
    return {
        "video_id": video_id,
        "title": sn.get("title", ""),
        "channel": sn.get("channelTitle", ""),
        "channel_url": "https://www.youtube.com/channel/" + sn.get("channelId", ""),
        "license": CC_LICENSE if (it.get("status") or {}).get("license") == "creativeCommon" else None,
        "description": sn.get("description", ""),
        "tags": sn.get("tags") or [],
        "duration": _iso_seconds((it.get("contentDetails") or {}).get("duration", "")),
        "heatmap": [],  # "most replayed" is not in the official API
    }


def api_comments(video_id: str, limit: int = 300) -> list[str]:
    yt = client()
    texts, token = [], None
    while len(texts) < limit:
        r = yt.commentThreads().list(part="snippet", videoId=video_id, maxResults=100, order="relevance",
                                     textFormat="plainText", pageToken=token).execute()
        quota.spend("read")
        texts += [i["snippet"]["topLevelComment"]["snippet"]["textDisplay"] for i in r.get("items", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    return texts


def api_resolve_channel(query: str) -> tuple[str, str]:
    yt = client()
    q = query.strip()
    cid = None
    m = re.search(r"/channel/(UC[\w-]{20,})", q)
    if m:
        cid = m.group(1)
    else:
        h = re.search(r"youtube\.com/(@[\w.\-]+)", q) or (re.match(r"^(@[\w.\-]+)$", q))
        if h:
            items = yt.channels().list(part="id", forHandle=h.group(1)).execute().get("items") or []
            cid = items[0]["id"] if items else None
        if not cid:  # plain name, or an old /c/ /user/ url: search once
            term = q.rstrip("/").rsplit("/", 1)[-1] if q.startswith("http") else q
            items = yt.search().list(part="snippet", q=term, type="channel", maxResults=1).execute().get("items") or []
            quota.spend("search")
            cid = items[0]["snippet"]["channelId"] if items else None
    if not cid:
        raise ValueError(f"No channel found for '{q}'")
    ch = yt.channels().list(part="snippet", id=cid).execute().get("items") or []
    return f"https://www.youtube.com/channel/{cid}", (ch[0]["snippet"]["title"] if ch else q)


def api_list_video_ids(channel_url: str, n: int) -> list[str]:
    m = re.search(r"/channel/(UC[\w-]{20,})", channel_url)
    if not m:
        raise ValueError("not a channel-id url")
    yt, out, token = client(), [], None
    while len(out) < n:  # newest first, 50 a page (1 quota unit each)
        r = yt.playlistItems().list(part="contentDetails", playlistId="UU" + m.group(1)[2:], maxResults=min(n - len(out), 50),
                                    pageToken=token).execute()
        quota.spend("read")
        out += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        token = r.get("nextPageToken")
        if not token or not r.get("items"):
            break
    return out[:n]


# titles that are often re-uploads wrongly marked Creative Commons (music, films, TV): never use them
RISKY = re.compile(r"\b(official (music )?video|lyrics?|full (movie|film|episode)|trailer|songs?|audio|remix|"
                   r"ft|feat|album|movie|episode|serial|t-series|zee|sony|star plus|colors tv|netflix|ipl|wwe)\b", re.I)


def api_search_cc(topic: str, skip: set, n: int, pages: int = 3) -> list[str]:
    """Creative Commons videos (4-20 min, so we clip a moment instead of re-posting a whole Short) that match the
    topic best, skipping ids already used and risky titles."""
    yt, out, token = client(), [], None
    for _ in range(pages):
        if not quota.can_search():
            break
        r = yt.search().list(part="snippet", q=topic, type="video", videoLicense="creativeCommon", order="relevance", videoDuration="medium",
                             safeSearch="moderate", maxResults=50, pageToken=token,
                             regionCode="IN", relevanceLanguage="hi").execute()  # Indian / Hindi videos first
        quota.spend("search")
        quota.note_search()
        for it in r.get("items", []):
            vid = it["id"].get("videoId")
            if vid and vid not in skip and vid not in out and not RISKY.search(it["snippet"].get("title", "")):
                out.append(vid)
        token = r.get("nextPageToken")
        if len(out) >= n or not token:
            break
    return out[:n]


def set_thumbnail(dest_id: int | None, video_id: str, path: str):
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    yt = client(dest_id)
    quota.spend("thumbnail")
    yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(path, mimetype=mime)).execute()


def thumb_error_text(e) -> str:
    m = str(e)
    if "forbidden" in m.lower() or "permission" in m.lower() or "403" in m:
        return ("Waiting: YouTube allows custom thumbnails only after the channel's phone number is verified "
                "(youtube.com/verify). It is sent automatically as soon as that is done.")
    return "Thumbnail not sent yet (retrying automatically): " + m[:200]


def push_thumbnail(c) -> bool:
    """Set the clip's thumbnail on YouTube. On failure the clip is marked pending and retried later."""
    try:
        set_thumbnail(c.destination_id, c.youtube_id, c.thumb_path)
        c.thumb_note, c.thumb_pending = "Thumbnail set on YouTube", False
        return True
    except Exception as e:  # noqa: BLE001
        c.thumb_note, c.thumb_pending = thumb_error_text(e), True
        return False

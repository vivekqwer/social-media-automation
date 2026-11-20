import json
import os
import secrets
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import BASE_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, IS_LOCAL
from . import auth
from .crypto import decrypt, encrypt
from .db import Destination, SessionLocal, get_setting, set_setting

EMAIL_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"] + EMAIL_SCOPES
REDIRECT = f"{BASE_URL}/auth/callback"

if IS_LOCAL:
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _flow(scopes: list | None = None) -> Flow:
    cfg = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
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


def handle_callback(code: str, state: str) -> int:
    """Exchange the code, look up which channel it is, and store it as a destination. Returns destination id."""
    expected = get_setting("oauth_state")
    set_setting("oauth_state", "")  # single use
    if not expected or state != expected:
        raise ValueError("OAuth state mismatch")
    f = _flow()
    f.fetch_token(code=code)
    creds = f.credentials
    email = _email_from(creds)
    if email:  # this Google account may later reset the dashboard password
        auth.remember_email(email)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        raise ValueError("This Google account has no YouTube channel.")
    cid, name = items[0]["id"], items[0]["snippet"]["title"]
    with SessionLocal() as s:
        d = s.query(Destination).filter(Destination.channel_id == cid).first() or Destination(channel_id=cid)
        d.name, d.token = name, encrypt(creds.to_json())
        s.add(d)
        s.commit()
        return d.id


def _default_dest_id() -> int | None:
    with SessionLocal() as s:
        d = s.query(Destination).order_by(Destination.id).first()
        return d.id if d else None


def client(dest_id: int | None = None):
    dest_id = dest_id or _default_dest_id()
    if not dest_id:
        raise RuntimeError("No YouTube channel connected. Use 'Connect my channel' on the dashboard.")
    with SessionLocal() as s:
        d = s.get(Destination, dest_id)
        if not d:
            raise RuntimeError(f"Destination {dest_id} not found")
        creds = Credentials.from_authorized_user_info(json.loads(decrypt(d.token)), SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            d.token = encrypt(creds.to_json())
            s.commit()
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
    req = client(dest_id).videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk(num_retries=5)
    return resp["id"]


def _email_from(creds) -> str | None:
    """Verified e-mail of the signed-in Google account, from the OpenID id_token."""
    try:
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(creds.id_token, Request(), GOOGLE_CLIENT_ID)
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

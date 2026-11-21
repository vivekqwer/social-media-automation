"""Gmail for permission requests: send the owner's message to a creator (one tap) and watch for their reply.

The owner connects their own Gmail once. Mails go out only when the owner presses Send (max 5 a day);
replies are shown on Telegram/dashboard and the owner decides "permission yes / no" with one tap.
"""
import base64
import json
import logging
import re
import secrets
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .crypto import decrypt, encrypt
from .db import get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly",
          "openid", "https://www.googleapis.com/auth/userinfo.email"]
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
MAX_PER_DAY = 5
SUBJECT = "Permission to share short clips of your videos (with full credit)"


def connected_email() -> str | None:
    return get_setting("gmail_email") or None


def login_url() -> str:
    from .youtube import _flow

    state = "gmail:" + secrets.token_urlsafe(24)
    url, _ = _flow(SCOPES).authorization_url(access_type="offline", prompt="consent select_account", state=state)
    set_setting("gmail_state", state)
    return url


def handle_callback(code: str, state: str) -> str:
    from .youtube import _email_from, _flow

    expected = get_setting("gmail_state")
    set_setting("gmail_state", "")
    if not expected or not secrets.compare_digest(state or "", expected):
        raise ValueError("Sign-in check failed. Please try again.")
    f = _flow(SCOPES)
    f.fetch_token(code=code)
    creds = f.credentials
    granted = set(creds.scopes or [])
    if not {"https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"} <= granted:
        raise ValueError("Please tick both Gmail boxes (send and read) on the Google screen.")
    set_setting("gmail_token", encrypt(creds.to_json()))
    email = _email_from(creds) or ""
    set_setting("gmail_email", email)
    return email


def disconnect():
    for k in ("gmail_token", "gmail_email"):
        set_setting(k, "")


def _service():
    raw = get_setting("gmail_token")
    if not raw:
        raise RuntimeError("Gmail is not connected")
    creds = Credentials.from_authorized_user_info(json.loads(decrypt(raw)))
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        set_setting("gmail_token", encrypt(creds.to_json()))
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _sent_today() -> int:
    return int(get_setting("gmail_sent:" + utcnow().strftime("%Y-%m-%d")) or 0)


def send(to: str, body: str) -> dict:
    to = (to or "").strip()
    if not EMAIL_RE.match(to):
        raise ValueError("That email address does not look right.")
    if _sent_today() >= MAX_PER_DAY:
        raise ValueError(f"Already {MAX_PER_DAY} permission mails today — the rest go tomorrow (more would look like spam).")
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"], msg["subject"] = to, SUBJECT
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    res = _service().users().messages().send(userId="me", body={"raw": raw}).execute()
    key = "gmail_sent:" + utcnow().strftime("%Y-%m-%d")
    set_setting(key, str(_sent_today() + 1))
    return {"thread_id": res.get("threadId"), "message_id": res.get("id")}


OWNER_PER_DAY = 40  # alerts to the owner's own inbox (separate from creator mails)


def notify_owner(text: str):
    """An alert to the owner's own Gmail (used when Telegram is not set up)."""
    import html

    from .config import BASE_URL

    me = connected_email()
    if not me:
        return
    key = "gmail_owner:" + utcnow().strftime("%Y-%m-%d")
    n = int(get_setting(key) or 0)
    if n >= OWNER_PER_DAY:
        return
    plain = html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()
    first = plain.splitlines()[0][:90] if plain else "Update"
    msg = MIMEText(plain + f"\n\nOpen Raji Studio: {BASE_URL}/\n(These alerts come from your own Gmail. Connect Telegram in "
                           "My channels to get them as phone messages with buttons instead.)", "plain", "utf-8")
    msg["to"], msg["subject"] = me, "Raji Studio: " + first
    _service().users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).execute()
    set_setting(key, str(n + 1))


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def reply_in_thread(thread_id: str) -> dict | None:
    """The newest message in this thread that is not from me (the creator's reply), or None."""
    me = (connected_email() or "").lower()
    t = _service().users().threads().get(userId="me", id=thread_id, format="metadata",
                                         metadataHeaders=["From", "Date"]).execute()
    for m in reversed(t.get("messages", [])):
        frm = _header(m, "From")
        if me and me in frm.lower():
            continue
        if "SENT" in (m.get("labelIds") or []):
            continue
        return {"from": frm, "date": _header(m, "Date"), "snippet": m.get("snippet", "")[:500], "id": m.get("id")}
    return None

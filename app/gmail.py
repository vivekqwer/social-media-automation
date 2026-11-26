"""Gmail for permission requests: send the owner's message to a creator (one tap) and watch for their reply.

The owner connects their own Gmail once. Mails go out only when the owner presses Send (max 5 a day);
replies are shown on Telegram/dashboard and the owner decides "permission yes / no" with one tap.
"""
import base64
import json
import logging
import re
import secrets
import threading
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
    set_setting("perm_auto_paused", "")  # a fresh sign-in lifts the pause of permission mails sent by themselves
    return email


def disconnect():
    for k in ("gmail_token", "gmail_email", "gmail_broken"):
        set_setting(k, "")


def _service():
    raw = get_setting("gmail_token")
    if not raw:
        raise RuntimeError("Gmail is not connected")
    creds = Credentials.from_authorized_user_info(json.loads(decrypt(raw)))
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "RefreshError" or "invalid_grant" in str(e):
                set_setting("gmail_broken", utcnow().isoformat())  # the login expired: the watchdog tells the owner
            raise
        set_setting("gmail_token", encrypt(creds.to_json()))
        if get_setting("gmail_broken"):
            set_setting("gmail_broken", "")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _sent_today() -> int:
    return int(get_setting("gmail_sent:" + utcnow().strftime("%Y-%m-%d")) or 0)


class SignInError(ValueError):
    """Gmail refused the sign-in (not connected, expired or revoked): nothing was sent."""


_count_lock = threading.Lock()  # one process: the day's count is checked and taken in one step


def _reserve() -> str | None:
    key = "gmail_sent:" + utcnow().strftime("%Y-%m-%d")
    with _count_lock:
        n = int(get_setting(key) or 0)
        if n >= MAX_PER_DAY:
            return None
        set_setting(key, str(n + 1))
    return key


def _unreserve(key: str):
    with _count_lock:
        set_setting(key, str(max(0, int(get_setting(key) or 0) - 1)))


def _signed_in():
    try:
        return _service()
    except Exception as e:  # noqa: BLE001
        raise SignInError(f"Gmail sign-in failed ({type(e).__name__}) — reconnect Gmail in My channels. Nothing was sent.") from e


def _auth_refused(e: Exception) -> bool:
    """The send was refused at sign-in (so nothing left): expired / revoked token."""
    try:
        from google.auth.exceptions import RefreshError

        if isinstance(e, RefreshError):
            return True
    except Exception:  # noqa: BLE001
        pass
    status = getattr(getattr(e, "resp", None), "status", None)
    return status == 401 or str(status) == "401"


def send(to: str, body: str, subject: str | None = None, kind: str = "permission") -> dict:
    """kind "strike": a request to a claimant after a strike (app/strikes.py). It has its own daily limit there and
    does not use up (or wait for) the day's permission mails.
    Raises ValueError when nothing was sent (SignInError for a sign-in problem); any other error may or may not have
    sent the mail."""
    to = (to or "").strip()
    if not EMAIL_RE.match(to):
        raise ValueError("That email address does not look right.")
    if kind == "strike":
        return _deliver(to, body, subject)
    svc = _signed_in()  # an expired sign-in is "not sent", and takes no place of the day
    key = _reserve()
    if not key:
        raise ValueError(f"Already {MAX_PER_DAY} permission mails today — the rest go tomorrow (more would look like spam).")
    try:
        return _deliver(to, body, subject, svc)
    except ValueError:
        _unreserve(key)
        raise
    except Exception as e:  # noqa: BLE001
        if _auth_refused(e):
            _unreserve(key)
            raise SignInError(f"Gmail refused the sign-in — reconnect Gmail in My channels. Nothing was sent.") from e
        raise


def _deliver(to: str, body: str, subject: str | None, svc=None) -> dict:
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"], msg["subject"] = to, (subject or SUBJECT)[:150]
    me = connected_email()
    if me:  # the creator sees the name set on the dashboard (e.g. "Funny Gunction"), not just the address
        from email.utils import formataddr

        from .permissions import mail_identity

        msg["from"] = formataddr((mail_identity()[0], me))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    res = (svc or _signed_in()).users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"thread_id": res.get("threadId"), "message_id": res.get("id")}


OWNER_PER_DAY = 40  # alerts to the owner's own inbox (separate from creator mails)


WATCHDOG_PER_DAY = 15  # the watchdog's "could not fix it by itself" mails: their own count, never crowded out
WEEKLY_PER_DAY = 2  # the Monday report (app/weekly.py): its own small count, never crowded out by alerts


def notify_owner(text: str, kind: str = "alert") -> bool:
    """An alert to the owner's own Gmail (used when Telegram is not set up; the watchdog's alerts always, with kind
    "watchdog"; the Monday report with kind "weekly"). True when it was sent."""
    import html

    from .config import BASE_URL

    me = connected_email()
    if not me:
        return False
    wd, wk = kind == "watchdog", kind == "weekly"
    key = ("gmail_owner_wd:" if wd else "gmail_owner_wk:" if wk else "gmail_owner:") + utcnow().strftime("%Y-%m-%d")
    n = int(get_setting(key) or 0)
    if n >= (WATCHDOG_PER_DAY if wd else WEEKLY_PER_DAY if wk else OWNER_PER_DAY):
        return False
    plain = html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()
    first = plain.splitlines()[0][:90] if plain else "Update"
    tail = ("(The system tried to fix this by itself and could not. This mail comes from your own Gmail.)" if wd else
            "(Your weekly report, every Monday at 9 AM. Switch it off in My channels → Weekly report.)" if wk else
            "(These alerts come from your own Gmail. Connect Telegram in My channels to get them as phone messages with "
            "buttons instead.)")
    msg = MIMEText(plain + f"\n\nOpen Raji Studio: {BASE_URL}/\n" + tail, "plain", "utf-8")
    msg["to"], msg["subject"] = me, "Raji Studio: " + first
    _service().users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).execute()
    set_setting(key, str(n + 1))
    return True


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def search(query: str, limit: int = 30) -> list[str]:
    """Ids of the newest messages that match a Gmail search (read only)."""
    r = _service().users().messages().list(userId="me", q=query, maxResults=max(1, min(int(limit), 100))).execute()
    return [m["id"] for m in r.get("messages") or [] if m.get("id")]


def _part_text(part: dict, want: str) -> str:
    """All text of one MIME type inside a message part (parts can be nested any number of levels)."""
    out = []
    if (part.get("mimeType") or "").lower() == want:
        data = (part.get("body") or {}).get("data")
        if data:
            try:
                out.append(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001  (a damaged part is skipped, never an error)
                pass
    for p in part.get("parts") or []:
        if isinstance(p, dict):
            out.append(_part_text(p, want))
    return "\n".join(x for x in out if x)


def _html_to_text(h: str) -> str:
    import html

    h = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", h or "")
    h = re.sub(r'(?is)<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', r"\2 ( \1 ) ", h)  # keep where links go
    h = re.sub(r"(?i)<\s*(br|/p|/div|/tr|/li|/h\d|/td)\s*/?>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t\r\xa0]+", " ", t)).strip()


def read_message(message_id: str) -> dict:
    """One message as plain text: {id, thread_id, from, subject, date, snippet, text}. Never raises on an odd layout."""
    m = _service().users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = m.get("payload") or {}
    text = _part_text(payload, "text/plain")
    if not text.strip():
        text = _html_to_text(_part_text(payload, "text/html"))
    return {"id": m.get("id") or message_id, "thread_id": m.get("threadId"), "from": _header(m, "From"),
            "subject": _header(m, "Subject"), "date": _header(m, "Date"), "internal_ms": m.get("internalDate"),
            "snippet": (m.get("snippet") or "")[:500], "text": (text or m.get("snippet") or "")[:20000]}


AUTO_HEADERS = ("Auto-Submitted", "X-Autoreply", "X-Autorespond", "Precedence")


def is_auto(msg: dict) -> bool:
    """An automatic message (out of office, auto-responder, mailing list) by its headers."""
    a = _header(msg, "Auto-Submitted").strip().lower()
    if a and a != "no":
        return True
    if _header(msg, "X-Autoreply").strip() or _header(msg, "X-Autorespond").strip():
        return True
    return _header(msg, "Precedence").strip().lower() in ("bulk", "auto_reply", "list", "junk")


def reply_in_thread(thread_id: str, skip: tuple | list | set = ()) -> dict | None:
    """The newest message in this thread written by someone else by hand: not from me, not in Spam / Trash, not an
    automatic reply (headers), not one of `skip`. Automatic replies are passed over, so a later real reply is read."""
    from email.utils import parseaddr

    me = (connected_email() or "").lower()
    t = _service().users().threads().get(userId="me", id=thread_id, format="metadata",
                                         metadataHeaders=["From", "Date", *AUTO_HEADERS]).execute()
    for m in reversed(t.get("messages", [])):
        frm = _header(m, "From")
        labels = set(m.get("labelIds") or [])
        if me and parseaddr(frm)[1].lower() == me:
            continue
        if labels & {"SENT", "SPAM", "TRASH", "DRAFT"} or m.get("id") in skip or is_auto(m):
            continue
        return {"from": frm, "date": _header(m, "Date"), "snippet": m.get("snippet", "")[:500], "id": m.get("id")}
    return None

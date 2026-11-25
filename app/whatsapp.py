"""WhatsApp alerts to the owner's own phone, through the free CallMeBot service (optional).

The owner sends one WhatsApp message to CallMeBot from their own phone, gets an API key back, and types their number
and the key into My channels -> WhatsApp alerts. Both are stored encrypted (app/crypto.py), per account.

Only two kinds of message ever go to that number: the watchdog's "could not fix it by itself" alerts (app/watchdog.py)
and the existing "Needs you" alerts (app/phone.py) — plus the owner's own Test. At most PER_DAY a day.

The number and the key never appear in logs, the activity log, error texts or answers of the server: only the last
3 digits are shown. CallMeBot's free WhatsApp bot is sometimes full for new users; the card says so.
Switched off per account by the Super Admin with "WhatsApp alerts" (acl "whatsapp.use").
"""
import html
import logging
import re

import httpx

from .crypto import decrypt, encrypt
from .db import get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
API = "https://api.callmebot.com/whatsapp.php"
PER_DAY = 10
PHONE_RE = re.compile(r"^\+?[0-9]{8,15}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")


def _secret(key: str) -> str:
    raw = get_setting(key)
    if not raw:
        return ""
    try:
        return decrypt(raw) or ""
    except Exception:  # noqa: BLE001  (an unreadable value counts as not set up)
        return ""


def _allowed() -> bool:
    from . import acl

    return acl.feature("whatsapp.use")


def configured() -> bool:
    return bool(_secret("wa_phone") and _secret("wa_key"))


def enabled() -> bool:
    return configured() and get_setting("wa_on") != "0" and _allowed()


def _tail(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return ("•••" + digits[-3:]) if digits else ""


def _sent_key() -> str:
    return "wa_sent:" + utcnow().strftime("%Y-%m-%d")


def sent_today() -> int:
    try:
        return int(get_setting(_sent_key()) or 0)
    except (TypeError, ValueError):
        return 0


def status() -> dict:
    """What the dashboard may know: never the number or the key, only the last 3 digits."""
    return {"set": configured(), "on": enabled(), "paused": configured() and get_setting("wa_on") == "0",
            "allowed": _allowed(), "phone": _tail(_secret("wa_phone")), "per_day": PER_DAY, "sent_today": sent_today()}


def clean_phone(phone: str) -> str:
    p = re.sub(r"[\s()\-.]", "", str(phone or ""))
    if p.startswith("00"):
        p = "+" + p[2:]
    if not PHONE_RE.match(p):
        raise ValueError("Type the WhatsApp number with the country code, for example +91 followed by the 10 digits.")
    return p if p.startswith("+") else "+" + p


def save(phone: str, apikey: str) -> dict:
    p = clean_phone(phone)
    k = str(apikey or "").strip()
    if not KEY_RE.match(k):
        raise ValueError("The API key looks wrong. Copy it exactly from the WhatsApp message CallMeBot sent you.")
    set_setting("wa_phone", encrypt(p))
    set_setting("wa_key", encrypt(k))
    set_setting("wa_on", "1")
    return status()


def toggle(on: bool) -> dict:
    set_setting("wa_on", "1" if on else "0")
    return status()


def remove() -> dict:
    for k in ("wa_phone", "wa_key", "wa_on"):
        set_setting(k, "")
    return status()


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def send(text: str, test: bool = False) -> bool:
    """One message to the owner's own WhatsApp. Never raises, never logs the number, the key or the address used."""
    if not (configured() and _allowed() and (test or get_setting("wa_on") != "0")):
        return False
    n = sent_today()
    if n >= PER_DAY:
        return False
    body = _plain(text)[:900] or "Raji Studio"
    try:
        r = httpx.get(API, params={"phone": _secret("wa_phone"), "text": body, "apikey": _secret("wa_key")}, timeout=20)
        ok = r.status_code < 300 and "error" not in (r.text or "")[:400].lower()
    except Exception as e:  # noqa: BLE001  (the error text of httpx carries the address with the number and key)
        log.warning("whatsapp: not sent (%s)", type(e).__name__)
        return False
    if not ok:
        log.warning("whatsapp: not sent (answer %s)", r.status_code)
        return False
    set_setting(_sent_key(), str(n + 1))
    return True

"""Dashboard login: signed session cookie, hashed password (scrypt), rate limiting, Google-verified reset."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time

from . import crypto
from .config import APP_PASSWORD, APP_USER
from .db import get_setting, set_setting

SESSION_COOKIE = "cs_session"
RESET_COOKIE = "cs_reset"
SESSION_TTL = 14 * 86400
RESET_TTL = 600
MIN_LEN = 12
COMMON = {"change-me", "changeme", "password", "password123", "admin", "admin123", "12345678", "123456789012", "qwertyuiop12"}

_fails: dict[str, list[float]] = {}


# ---------- secrets / signing ----------
def _secret() -> bytes:
    env = os.getenv("SESSION_SECRET")
    if env:
        return env.encode()
    return hashlib.sha256(b"clipstudio-session|" + crypto._key()).digest()


def _sign(msg: str) -> str:
    return hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()[:40]


def _pack(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _sign(payload)


def _unpack(token: str | None) -> str | None:
    try:
        b, sig = (token or "").rsplit(".", 1)
        payload = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
    except Exception:  # noqa: BLE001
        return None
    return payload if hmac.compare_digest(sig, _sign(payload)) else None


# ---------- password storage ----------
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def _verify_hash(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:  # noqa: BLE001
        return False


def _stored_hash() -> str | None:
    if os.getenv("FORCE_ENV_PASSWORD") == "1":  # escape hatch: ignore the saved password, use APP_PASSWORD
        return None
    return get_setting("admin_pw")


def auth_configured() -> bool:
    return bool(APP_PASSWORD or _stored_hash())


def check_login(user: str, pw: str) -> bool:
    user_ok = hmac.compare_digest((user or "").encode(), APP_USER.encode())
    h = _stored_hash()
    if h:
        pw_ok = _verify_hash(pw or "", h)
    else:
        pw_ok = bool(APP_PASSWORD) and hmac.compare_digest((pw or "").encode(), APP_PASSWORD.encode())
    return user_ok and pw_ok


def validate_new_password(pw: str) -> str | None:
    """Return an error message, or None if the password is acceptable."""
    if len(pw) < MIN_LEN:
        return f"Password must be at least {MIN_LEN} characters."
    if pw.lower() in COMMON or pw.lower() == APP_USER.lower():
        return "That password is too common. Choose another."
    if len(set(pw)) < 5:
        return "Password is too repetitive."
    if not (re.search(r"[A-Za-z]", pw) and re.search(r"[0-9\W_]", pw)):
        return "Use letters plus at least one number or symbol."
    return None


def set_password(new: str):
    set_setting("admin_pw", hash_password(new))


def _pwv() -> str:
    """Changes whenever the password changes, so old sessions stop working."""
    return hashlib.sha256((_stored_hash() or ("env:" + APP_PASSWORD)).encode()).hexdigest()[:10]


# ---------- sessions ----------
def make_session(user: str) -> str:
    return _pack(f"{user}|{int(time.time()) + SESSION_TTL}|{_pwv()}")


def verify_session(token: str | None) -> str | None:
    payload = _unpack(token)
    if not payload:
        return None
    try:
        user, exp, pwv = payload.split("|")
    except ValueError:
        return None
    if int(exp) < time.time() or not hmac.compare_digest(pwv, _pwv()):
        return None
    return user


def request_authenticated(request) -> bool:
    if verify_session(request.cookies.get(SESSION_COOKIE)):
        return True
    hdr = request.headers.get("authorization", "")  # Basic auth still works for scripts/curl
    if hdr.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
            return check_login(user, pw)
        except Exception:  # noqa: BLE001
            return False
    return False


# ---------- reset (verified through Google) ----------
def make_reset_token(email: str) -> str:
    return _pack(f"reset|{email}|{int(time.time()) + RESET_TTL}")


def verify_reset_token(token: str | None) -> str | None:
    payload = _unpack(token)
    if not payload:
        return None
    try:
        kind, email, exp = payload.split("|")
    except ValueError:
        return None
    return email if kind == "reset" and int(exp) >= time.time() else None


def allowed_reset_emails() -> set[str]:
    out = {e.strip().lower() for e in (os.getenv("ADMIN_EMAIL") or "").split(",") if e.strip()}
    out |= {e.strip().lower() for e in (get_setting("admin_emails") or "").split(",") if e.strip()}
    return out


def remember_email(email: str):
    """Google accounts that connected a channel may later reset the dashboard password."""
    emails = {e for e in (get_setting("admin_emails") or "").split(",") if e}
    emails.add(email.lower())
    set_setting("admin_emails", ",".join(sorted(emails)))


# ---------- brute-force limiter ----------
def rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    _fails[ip] = recent
    return len(recent) >= 8


def note_fail(ip: str):
    _fails.setdefault(ip, []).append(time.time())

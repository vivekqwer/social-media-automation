"""Dashboard login: signed session cookie, hashed password (scrypt), rate limiting, Google-verified reset."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time

from . import crypto, tenant
from .config import APP_PASSWORD, APP_USER
from .db import get_setting as _db_get, set_setting as _db_set


def get_setting(key: str):
    """The Super Admin's login data always lives in the owner's copy, whoever the running code is working for."""
    with tenant.use(tenant.OWNER):
        return _db_get(key)


def set_setting(key: str, value: str):
    with tenant.use(tenant.OWNER):
        _db_set(key, value)


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
    """Changes whenever the password changes or the owner logs out, so old sessions stop working.
    Keyed with the server secret, so a stolen cookie reveals nothing about the password."""
    basis = (_stored_hash() or ("env:" + APP_PASSWORD)) + "|" + (get_setting("session_gen") or "0")
    return hmac.new(_secret(), basis.encode(), hashlib.sha256).hexdigest()[:16]


def end_all_sessions():
    set_setting("session_gen", secrets.token_hex(8))


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


AS_COOKIE = "cs_as"  # Super Admin only: "view as this customer"
AS_TTL = 2 * 3600


def identify(token: str | None, as_token: str | None = None) -> dict | None:
    """Who is signed in: the Super Admin (tenant 1) or a customer account (its own tenant). None = nobody."""
    payload = _unpack(token)
    if not payload:
        return None
    if re.match(r"^a\d+\|", payload) and not payload.startswith(APP_USER + "|"):
        from . import accounts

        a = accounts.from_session(payload)
        if not a:
            return None
        return {"role": "user", "tenant": a.tenant_id, "account_id": a.id, "name": a.name, "email": a.email,
                "brand": a.brand, "channel_limit": a.channel_limit or 1, "plan": a.plan}
    if not verify_session(token):
        return None
    me = {"role": "super", "tenant": tenant.OWNER, "account_id": None, "name": APP_USER, "email": None, "brand": None,
          "channel_limit": None, "plan": None}
    seen = _unpack(as_token) if as_token else None
    if seen and seen.startswith("as"):  # looking at a customer's dashboard to help them
        try:
            who, exp = seen[2:].split("|")
            if int(exp) > time.time():
                from . import accounts

                a = accounts.get(int(who))
                if a and a.tenant_id:
                    me.update(tenant=a.tenant_id, viewing={"account_id": a.id, "name": a.name, "email": a.email, "brand": a.brand},
                              brand=a.brand, channel_limit=a.channel_limit or 1)
        except (ValueError, TypeError):
            pass
    return me


def make_view_as(account_id: int) -> str:
    return _pack(f"as{int(account_id)}|{int(time.time()) + AS_TTL}")


def request_identity(request) -> dict | None:
    return identify(request.cookies.get(SESSION_COOKIE), request.cookies.get(AS_COOKIE))


def request_authenticated(request) -> bool:
    return bool(request_identity(request))


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
    """Who may reset the dashboard password: ADMIN_EMAIL when it is set (recommended), otherwise the Google
    accounts of channels that are still connected."""
    admin = {e.strip().lower() for e in (os.getenv("ADMIN_EMAIL") or "").split(",") if e.strip()}
    return admin or {e.strip().lower() for e in (get_setting("admin_emails") or "").split(",") if e.strip()}


def forget_email(email: str):
    emails = {e for e in (get_setting("admin_emails") or "").split(",") if e and e != (email or "").lower()}
    set_setting("admin_emails", ",".join(sorted(emails)))


def remember_email(email: str):
    """Google accounts that connected a channel IN THE OWNER'S OWN COPY may later reset the Super Admin password.
    A customer connecting a channel in their copy must never get onto this list."""
    if tenant.current() != tenant.OWNER:
        return
    emails = {e for e in (get_setting("admin_emails") or "").split(",") if e}
    emails.add(email.lower())
    set_setting("admin_emails", ",".join(sorted(emails)))


# ---------- brute-force limiter ----------
# One bucket per kind of attempt, so customers' sign-ins, registrations and helpers can never use up the
# attempts that protect the Super Admin's own login.   kind: (per address in 10 min, everyone in 1 hour)
LIMITS = {"owner": (8, 60), "customer": (8, 300), "register": (3, 40), "helper": (8, 60)}


def rate_limited(ip: str, kind: str = "owner") -> bool:
    now, (per_ip, per_all) = time.time(), LIMITS.get(kind, LIMITS["owner"])
    if len(_fails) > 5000:  # forget old entries so the table can't grow without end
        for k in [k for k, v in _fails.items() if not v or now - v[-1] > 600]:
            _fails.pop(k, None)
    key = f"{kind}|{ip}"
    recent = [t for t in _fails.get(key, []) if now - t < 600]
    _fails[key] = recent
    everyone = sum(1 for k, v in _fails.items() if k.startswith(kind + "|") for t in v if now - t < 3600)
    return len(recent) >= per_ip or everyone >= per_all  # also a cap for all addresses together


def note_fail(ip: str, kind: str = "owner"):
    _fails.setdefault(f"{kind}|{ip}", []).append(time.time())

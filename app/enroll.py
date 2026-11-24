"""Helper keys: a helper that carries its account's key joins by itself — approved at once, no pairing code, no button.

* One key per account (copy). It looks like  rk<account copy>.<key id>.<signature>  and is made from the server's
  secret, so the database keeps only the key id and a hash of the key (never the key itself).
* The account's admin sees it (behind "Show") inside the one-line install commands and in the helper ZIP; an approved
  helper of the same account is also handed it on its job poll, so the owner's PC can always come back by itself.
* "New key" (rotate) makes the old key useless at once; "Switch off" (revoke) stops every new join until a new key.
* Caps per key: at most MAX_HELPERS helpers joined with it, at most MAX_PER_HOUR joins an hour. A helper removed in
  the dashboard never joins again by itself (its old token's hash is remembered).
* A key is checked only in its own account's copy (the number in it chooses the copy, the signature and the stored
  hash must both match there), so a key of account A can never add a helper to account B.
The key is never written to a log, a URL, or the activity log.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from . import tenant
from .db import SessionLocal, Worker, get_setting, set_setting

SETTING = "helper_enroll"  # {"kid", "hash", "created", "revoked", "uses": [unix times]}
REMOVED = "helper_removed"  # token hashes of helpers removed in the dashboard: they never join again by themselves
MAX_HELPERS = 5  # helpers joined with one key (removed ones do not count)
MAX_PER_HOUR = 10  # joins with one key in an hour
MAX_REMOVED = 200
KEY_RE = re.compile(r"^rk(\d{1,9})\.([0-9a-f]{8})\.([A-Za-z0-9_-]{43})$")


class EnrollError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _sig(tid: int, kid: str) -> str:
    from .auth import _secret

    raw = hmac.new(_secret(), f"helper-enroll:{int(tid)}:{kid}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")  # 43 characters


def make_key(tid: int, kid: str) -> str:
    return f"rk{int(tid)}.{kid}.{_sig(tid, kid)}"


def key_hash(key: str) -> str:
    return hashlib.sha256(("helper-key:" + (key or "")).encode()).hexdigest()


def tenant_of_key(key: str) -> int | None:
    """The account copy a key belongs to (only its shape is checked here; the copy itself checks the key)."""
    m = KEY_RE.match((key or "").strip())
    return int(m.group(1)) if m else None


def kid_of(key: str) -> str:
    m = KEY_RE.match((key or "").strip())
    return m.group(2) if m else ""


def _state() -> dict | None:
    try:
        st = json.loads(get_setting(SETTING) or "null")
    except ValueError:
        return None
    return st if isinstance(st, dict) and st.get("kid") else None


def _save(st: dict):
    set_setting(SETTING, json.dumps(st))


def rotate() -> str:
    """A new key for this copy; the old one stops working at once."""
    tid, kid = tenant.current(), secrets.token_hex(4)
    key = make_key(tid, kid)
    _save({"kid": kid, "hash": key_hash(key), "created": int(time.time()), "revoked": False, "uses": []})
    return key


def revoke():
    st = _state() or {"kid": "00000000", "hash": "", "created": int(time.time()), "uses": []}
    st["revoked"] = True
    _save(st)


def current_key(create: bool = True) -> str | None:
    """This copy's key, made the first time it is needed. None when switched off (or none yet and create=False)."""
    st = _state()
    if st is None:
        return rotate() if create else None
    if st.get("revoked"):
        return None
    key = make_key(tenant.current(), st["kid"])
    if not hmac.compare_digest(key_hash(key), st.get("hash") or ""):
        return rotate() if create else None  # the server's secret changed: the old key cannot be shown, make a new one
    return key


def _joined(s, kid: str) -> int:
    return s.query(Worker).filter(Worker.enrolled == kid).count()


def check(key: str, old_hash: str = "") -> str:
    """The key id if `key` may add a helper to this copy now; else EnrollError (403 not valid, 429 over a cap)."""
    key, tid = (key or "").strip(), tenant.current()
    m = KEY_RE.match(key)
    st = _state()
    if not m or int(m.group(1)) != tid or not st or st.get("revoked") \
            or not hmac.compare_digest(key_hash(key), st.get("hash") or ""):
        raise EnrollError(403, "This helper key is not valid any more. Copy the command again from the dashboard "
                               "(My channels -> Helper PC -> Show).")
    if old_hash and old_hash in removed():
        raise EnrollError(403, "This helper was removed in the dashboard, so it does not join by itself again.")
    with SessionLocal() as s:
        if _joined(s, st["kid"]) >= MAX_HELPERS:
            raise EnrollError(429, f"{MAX_HELPERS} helpers already joined with this key. Remove one in the dashboard "
                                   "or make a new key.")
    now = time.time()
    if len([t for t in st.get("uses") or [] if now - t < 3600]) >= MAX_PER_HOUR:
        raise EnrollError(429, "Too many helpers joined with this key in the last hour. Try again later.")
    return st["kid"]


def note_use():
    st = _state()
    if st:
        now = time.time()
        st["uses"] = [t for t in st.get("uses") or [] if now - t < 3600] + [now]
        _save(st)


def removed() -> list[str]:
    try:
        v = json.loads(get_setting(REMOVED) or "[]")
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def remember_removed(token_hash: str):
    if token_hash:
        set_setting(REMOVED, json.dumps((removed() + [token_hash])[-MAX_REMOVED:]))


def log_join(name: str, ip: str):
    """One line in the activity log (never the key)."""
    from . import acl

    tid = tenant.current()
    acl.record(acl._account_of(tid), "system", "helper.enroll",
               f"A helper joined by itself with the helper key: {name[:60]} from {ip[:60]}", tid)


def summary() -> dict:
    """What the Helper card shows (never the key)."""
    st = _state()
    now = time.time()
    with SessionLocal() as s:
        rows = s.query(Worker).filter(Worker.enrolled.isnot(None)).order_by(Worker.id).all()
        kid = st["kid"] if st else None
        joined = [{"id": w.id, "name": w.name, "current": w.enrolled == kid,
                   "created_at": (w.created_at.isoformat() + "Z") if w.created_at else None} for w in rows]
    return {"state": "none" if not st else "off" if st.get("revoked") else "on",
            "created_at": st.get("created") if st else None, "joined": joined,
            "used_last_hour": len([t for t in (st or {}).get("uses") or [] if now - t < 3600]),
            "max_helpers": MAX_HELPERS, "max_per_hour": MAX_PER_HOUR}


def commands(key: str, base: str) -> dict:
    """The one-line install commands, the key inside (shown only to the account's admin)."""
    base = base.rstrip("/")
    tls = "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
    laptop = (f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{tls}& ([scriptblock]::Create((iwr -UseBasicParsing '
              f"'{base}/helper/install.ps1').Content)) -Server '{base}' -Key '{key}'\"")
    phone = f"curl -fsSL {base}/helper/setup.sh | RAJI_KEY='{key}' bash -s -- {base}"
    return {"key": key, "laptop": laptop, "phone": phone}


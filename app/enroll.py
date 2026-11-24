"""Helper keys: a helper that carries its account's key joins by itself — approved at once, no pairing code, no button.

* One key per account (copy). It looks like  rk<account copy>.<key id>.<signature>  and is made from the server's
  secret, so the database keeps only the key id and a hash of the key (never the key itself).
* The account's admin sees it (behind "Show") and copies it into the installer's prompt (never onto a command line);
  the helper ZIP carries it. An approved helper is also handed it on its job poll — but only one a person approved, or
  one that joined with the current key (or the key just replaced): a helper that joined with an older key gets nothing.
* "New key" (rotate) makes the old key useless at once; "Switch off" (revoke) stops every new join until a new key;
  "Key leaked" removes every helper that joined by key, then makes a new key.
* Caps per key: at most MAX_HELPERS helpers joined with it, at most MAX_PER_HOUR joins an hour.
* A helper removed in the dashboard never joins again by itself: its install id (a random id the helper keeps in a
  file and sends when it registers) and its token's hash are remembered, and a key join from either is refused until
  a person approves that helper again with its pairing code (which takes it off the list).
* A key is checked only in its own account's copy (the number in it chooses the copy, the signature and the stored
  hash must both match there), so a key of account A can never add a helper to account B. An account whose
  "Helper PC" switch is off gets no key joins, no key hand-over and no package.
The key is never written to a log, a URL, or the activity log.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

from . import tenant
from .db import SessionLocal, Worker, get_setting, set_setting

SETTING = "helper_enroll"  # {"kid", "hash", "created", "revoked", "prev", "uses": [unix times]}
REMOVED = "helper_removed"  # {"ids": [install ids], "tokens": [token hashes]} of helpers removed in the dashboard
MAX_HELPERS = 5  # helpers joined with one key (removed ones do not count)
MAX_PER_HOUR = 10  # joins with one key in an hour
MAX_REMOVED = 500
KEY_RE = re.compile(r"^rk(\d{1,9})\.([0-9a-f]{8})\.([A-Za-z0-9_-]{43})$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
INVALID = "This helper key is not valid. Copy it again from the dashboard (My channels -> Helper PC -> Show)."
REMOVED_MSG = ("This helper was removed in the dashboard, so it does not join by itself again. "
               "To bring it back, approve it with its pairing code.")
_lock = threading.Lock()  # every read-modify-write of the two settings (one server process)


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


def allowed() -> bool:
    """The account's "Helper PC" switch (acl helper.manage); the owner's copy always."""
    from . import acl

    tid = tenant.current()
    if tid == tenant.OWNER:
        return True
    try:
        aid = acl._account_of(tid)
        eff = acl.effective(aid) if aid else None
        return eff is None or eff["perms"].get("helper.manage", True)
    except Exception:  # noqa: BLE001  (unsure: no key joins)
        return False


def _rotate_locked() -> str:
    old = _state()
    tid, kid = tenant.current(), secrets.token_hex(4)
    key = make_key(tid, kid)
    prev = old["kid"] if old and not old.get("revoked") else None  # helpers of the key being replaced still get the new one
    _save({"kid": kid, "hash": key_hash(key), "created": int(time.time()), "revoked": False, "prev": prev, "uses": []})
    return key


def rotate() -> str:
    """A new key for this copy; the old one stops working at once."""
    with _lock:
        return _rotate_locked()


def revoke():
    with _lock:
        st = _state() or {"kid": "00000000", "hash": "", "created": int(time.time()), "uses": []}
        st["revoked"], st["prev"] = True, None
        _save(st)


def current_key(create: bool = True) -> str | None:
    """This copy's key, made the first time it is needed. None when switched off (or none yet and create=False)."""
    with _lock:  # two first calls at once make one key, not two
        st = _state()
        if st is None:
            return _rotate_locked() if create else None
        if st.get("revoked"):
            return None
        key = make_key(tenant.current(), st["kid"])
        if not hmac.compare_digest(key_hash(key), st.get("hash") or ""):
            return _rotate_locked() if create else None  # the server's secret changed: make a new one
        return key


def _joined(s, kid: str) -> int:
    return s.query(Worker).filter(Worker.enrolled == kid).count()


def check(key: str, install_id: str = "", old_hash: str = "") -> str:
    """The key id if `key` may add a helper to this copy now; else EnrollError (403 / 429)."""
    key, tid = (key or "").strip(), tenant.current()
    m = KEY_RE.match(key)
    st = _state()
    if not m or int(m.group(1)) != tid or not st or st.get("revoked") \
            or not hmac.compare_digest(key_hash(key), st.get("hash") or "") or not allowed():
        raise EnrollError(403, INVALID)
    # the install id is what makes a removal stick (our helpers always send it); leaving it out gains nothing,
    # since anyone holding the key could send a fresh one — that is what New key / Key leaked are for
    gone = removed()
    if (install_id and install_id in gone["ids"]) or (old_hash and old_hash in gone["tokens"]):
        raise EnrollError(403, REMOVED_MSG)
    with SessionLocal() as s:
        if _joined(s, st["kid"]) >= MAX_HELPERS:
            raise EnrollError(429, f"{MAX_HELPERS} helpers already joined with this key. Remove one in the dashboard "
                                   "or make a new key.")
    now = time.time()
    if len([t for t in st.get("uses") or [] if now - t < 3600]) >= MAX_PER_HOUR:
        raise EnrollError(429, "Too many helpers joined with this key in the last hour. Try again later.")
    return st["kid"]


def note_use(kid: str):
    with _lock:
        st = _state()
        if st and st["kid"] == kid:  # a rotation in between starts a fresh count
            now = time.time()
            st["uses"] = [t for t in st.get("uses") or [] if now - t < 3600] + [now]
            _save(st)


def hand_over(w: Worker) -> str | None:
    """The key for an approved helper that asks, if it may have it: approved by a person (joined without a key), or
    joined with the current key or the key just replaced (then it is moved to the current one). Older keys: nothing."""
    if not allowed():
        return None
    key = current_key()
    if not key:
        return None
    st = _state() or {}
    if w.enrolled is None or w.enrolled == st.get("kid"):
        return key
    if st.get("prev") and w.enrolled == st["prev"]:
        w.enrolled = st["kid"]  # it now counts as joined with the current key (the caller commits)
        return key
    return None


def removed() -> dict:
    try:
        v = json.loads(get_setting(REMOVED) or "{}")
    except ValueError:
        v = {}
    if isinstance(v, list):  # first version: token hashes only
        v = {"tokens": v}
    return {"ids": list(v.get("ids") or []), "tokens": list(v.get("tokens") or [])}


def remember_removed(token_hash: str, install_id: str | None):
    with _lock:
        v = removed()
        if install_id and install_id not in v["ids"]:
            v["ids"] = (v["ids"] + [install_id])[-MAX_REMOVED:]
        if token_hash and token_hash not in v["tokens"]:
            v["tokens"] = (v["tokens"] + [token_hash])[-MAX_REMOVED:]
        set_setting(REMOVED, json.dumps(v))


def forget_removed(install_id: str | None):
    """A person approved this helper again (pairing code): it may join by key again later."""
    if not install_id:
        return
    with _lock:
        v = removed()
        if install_id in v["ids"]:
            v["ids"].remove(install_id)
            set_setting(REMOVED, json.dumps(v))


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
    """The key (shown only to the account's admin) and the install commands. The commands never contain the key:
    the installer asks for it (Windows: it also takes it from the clipboard), so it is never in a shell history."""
    base = base.rstrip("/")
    tls = "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
    laptop = (f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{tls}& ([scriptblock]::Create((iwr -UseBasicParsing '
              f"'{base}/helper/install.ps1').Content)) -Server '{base}'\"")
    phone = f"curl -fsSL {base}/helper/setup.sh | bash -s -- {base}"
    return {"key": key, "laptop": laptop, "phone": phone}

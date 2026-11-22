"""Accounts: who may use this server, and which private copy (tenant) each of them works in.

* The Super Admin is the original login (APP_USER + password) and owns tenant 1 — nothing about it changes.
* Everybody else registers here. A new account is "pending" until the Super Admin approves it; an approved account
  gets its own tenant (database part + data folder), its own system name ("brand") and plan limits
  (channels: 1 until the Super Admin raises it, e.g. after a payment).

These tables are shared (not per customer): they live in the default schema next to tenant 1's tables.
"""
import hashlib
import hmac
import re
import secrets
import time

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import declarative_base, sessionmaker

from . import tenant
from .db import IS_SQLITE, engine, ensure_tenant, migrate, utcnow

CoreBase = declarative_base()
CoreSession = sessionmaker(bind=engine, expire_on_commit=False)  # always the shared tables, whatever the customer
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
DEFAULT_CHANNELS = 1
SELF_TEST_TENANT = 2_000_000_000  # a throw-away copy for the isolation check; real copies are far below this
MAX_PENDING = 50  # registrations waiting for approval; more are refused until the Super Admin clears the list


class Account(CoreBase):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    email = Column(String(200), unique=True, nullable=False)  # the login
    name = Column(String(120))  # the person
    brand = Column(String(60))  # what they call their system (shown in their dashboard)
    pw_hash = Column(String(200), nullable=False)
    status = Column(String(12), default="pending")  # pending | active | blocked
    tenant_id = Column(Integer, unique=True)  # their private copy (2, 3, ...)
    channel_limit = Column(Integer, default=DEFAULT_CHANNELS)  # how many YouTube channels they may connect
    plan = Column(String(40), default="starter")  # only the name shown to the customer; the plan that counts is in `access`
    note = Column(Text)  # the Super Admin's own notes (payment reference, phone, ...)
    upgrade = Column(JSON)  # the customer's open request: {"channels": 3, "message": "...", "at": "..."}
    # what the Super Admin set for this account (see app/acl.py): {"plan": name, "perms": {key: bool}, "limits": {key: n},
    # "controls": {"uploads_paused": bool, "sources_stopped": bool}}. Empty/missing = the plan and the defaults decide.
    access = Column(JSON)
    plan_expires = Column(DateTime)  # a paid plan with a period ends here (app/payments.py); empty = it does not end
    expiry_warned = Column(DateTime)  # the end date the customer was already warned about (3 days before)
    paid_plan = Column(String(40))  # the plan the running payment is for; the end only acts while the account is still on it
    limit_before_pay = Column(Integer)  # channel limit before the first payment of this paid period (given back at the end)
    limit_by_hand = Column(Boolean)  # the Super Admin set the channel limit by hand: a payment's end leaves it alone
    session_gen = Column(String(20), default="0")  # changes on logout / password change: old cookies stop working
    terms_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)
    approved_at = Column(DateTime)
    last_login = Column(DateTime)


def init():
    from . import acl, payments  # noqa: F401  (their shared tables must be known before the tables are made)

    CoreBase.metadata.create_all(engine)
    migrate(tenant.OWNER, CoreBase.metadata)
    with CoreSession() as s:  # every active customer's tables exist and are up to date
        ids = [a.tenant_id for a in s.query(Account).filter(Account.status == "active", Account.tenant_id.isnot(None))]
    for tid in ids:
        try:
            ensure_tenant(tid)
        except Exception:  # noqa: BLE001  (one broken customer must not stop the server from starting)
            import logging

            logging.getLogger(__name__).exception("tenant %s could not be prepared", tid)


def public(a: Account) -> dict:
    return {"id": a.id, "email": a.email, "name": a.name, "brand": a.brand, "status": a.status, "tenant_id": a.tenant_id,
            "channel_limit": a.channel_limit or DEFAULT_CHANNELS, "plan": a.plan, "note": a.note or "", "upgrade": a.upgrade,
            "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
            "approved_at": a.approved_at.isoformat() + "Z" if a.approved_at else None,
            "last_login": a.last_login.isoformat() + "Z" if a.last_login else None,
            "plan_expires": a.plan_expires.isoformat() + "Z" if a.plan_expires else None}


def clean_brand(text: str) -> str:
    t = re.sub(r"[<>\"'`&\\]", "", text or "")
    return re.sub(r"\s+", " ", t).strip()[:60]


def register(email: str, name: str, brand: str, password: str, accepted_terms: bool) -> Account:
    from .auth import hash_password, validate_new_password

    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("That email address does not look right.")
    if not accepted_terms:
        raise ValueError("Please read and accept the Terms to create an account.")
    name, brand = clean_brand(name), clean_brand(brand)
    if len(name) < 2:
        raise ValueError("Tell us your name.")
    if len(brand) < 2:
        raise ValueError("Give your system a name (for example “Raji Studio”).")
    err = validate_new_password(password or "")
    if err:
        raise ValueError(err)
    with CoreSession() as s:
        if s.query(Account).filter(Account.email == email).first():
            raise ValueError("An account with this email already exists. Sign in instead.")
        if s.query(Account).filter(Account.status == "pending").count() >= MAX_PENDING:
            raise ValueError("Registration is paused for the moment. Please try again later.")
        a = Account(email=email, name=name, brand=brand, pw_hash=hash_password(password), status="pending",
                    channel_limit=DEFAULT_CHANNELS, terms_at=utcnow())
        try:  # the plan new accounts start on (Users → Plans & defaults); without one the default switches apply
            from . import acl

            start = acl.new_account_plan()
            if start:
                a.plan, a.access = start, {"plan": start}
                a.channel_limit = acl.config()["plans"][start]["limits"].get("channels") or DEFAULT_CHANNELS
        except Exception:  # noqa: BLE001  (a problem with plans must never stop a registration)
            pass
        s.add(a)
        s.commit()
        return a


def by_email(email: str) -> Account | None:
    with CoreSession() as s:
        return s.query(Account).filter(Account.email == (email or "").strip().lower()).first()


def get(account_id: int) -> Account | None:
    with CoreSession() as s:
        return s.get(Account, int(account_id))


def check_login(email: str, password: str) -> tuple[Account | None, str]:
    """(account, problem). The account is returned only when the login is fully allowed."""
    from .auth import _verify_hash

    a = by_email(email)
    if not a or not _verify_hash(password or "", a.pw_hash):
        return None, "Wrong email or password."
    if a.status == "pending":
        return None, "Your account is waiting for approval. You will be able to sign in as soon as it is approved."
    if a.status != "active":
        return None, "This account is blocked. Contact the person who gave you access."
    with CoreSession() as s:
        row = s.get(Account, a.id)
        row.last_login = utcnow()
        s.commit()
    return a, ""


# ---------- sessions (same signed-cookie scheme as the Super Admin's) ----------
def _gen(a: Account) -> str:
    from .auth import _secret

    return hmac.new(_secret(), f"{a.pw_hash}|{a.session_gen}".encode(), hashlib.sha256).hexdigest()[:16]


def make_session(a: Account) -> str:
    from .auth import SESSION_TTL, _pack

    return _pack(f"a{a.id}|{int(time.time()) + SESSION_TTL}|{_gen(a)}")


def from_session(payload: str) -> Account | None:
    """The active account a cookie payload belongs to (None: expired, logged out, blocked, unknown)."""
    try:
        who, exp, gen = payload.split("|")
        if not who.startswith("a") or int(exp) < time.time():
            return None
        a = get(int(who[1:]))
    except (ValueError, TypeError):
        return None
    if not a or a.status != "active" or not a.tenant_id or not hmac.compare_digest(gen, _gen(a)):
        return None
    return a


def end_sessions(account_id: int):
    with CoreSession() as s:
        a = s.get(Account, account_id)
        if a:
            a.session_gen = secrets.token_hex(8)
            s.commit()


def set_password(account_id: int, new: str):
    from .auth import hash_password

    with CoreSession() as s:
        a = s.get(Account, account_id)
        a.pw_hash, a.session_gen = hash_password(new), secrets.token_hex(8)
        s.commit()


# ---------- Super Admin actions ----------
def _changed():
    """An account row changed: what it may do (app/acl.py keeps a short-lived copy) is read again at once."""
    try:
        from . import acl

        acl.invalidate()
    except Exception:  # noqa: BLE001
        pass


def listing() -> list[dict]:
    with CoreSession() as s:
        order = {"pending": 0, "active": 1, "blocked": 2}
        return sorted((public(a) for a in s.query(Account).all()),
                      key=lambda a: (0 if a["upgrade"] else 1, order.get(a["status"], 3), a["id"]))


def approve(account_id: int) -> dict:
    """Give the account its private copy and let it sign in."""
    with CoreSession() as s:
        a = s.get(Account, account_id)
        if not a:
            raise KeyError(account_id)
        if not a.tenant_id:
            if a.id + 1 >= SELF_TEST_TENANT:
                raise ValueError("No more accounts can be added.")
            a.tenant_id = a.id + 1  # tenant 1 is the Super Admin's own system
        s.commit()
        tid = a.tenant_id
    ensure_tenant(tid)
    with CoreSession() as s:
        a = s.get(Account, account_id)
        a.status, a.approved_at = "active", a.approved_at or utcnow()
        s.commit()
        _changed()
        return public(a)


def update(account_id: int, **changes) -> dict:
    """status (active/blocked), channel_limit, plan, note, brand, name; upgrade=None closes the request."""
    with CoreSession() as s:
        a = s.get(Account, account_id)
        if not a:
            raise KeyError(account_id)
        if changes.get("status") == "blocked":
            a.session_gen = secrets.token_hex(8)  # signed-in browsers are logged out at once
        for k in ("status", "channel_limit", "plan", "note"):
            if changes.get(k) is not None:
                setattr(a, k, changes[k])
        for k in ("brand", "name"):
            if changes.get(k) is not None:
                v = clean_brand(changes[k])
                if len(v) < 2:
                    raise ValueError("The name is too short.")
                setattr(a, k, v)
        if "upgrade" in changes:
            a.upgrade = changes["upgrade"]
        s.commit()
        _changed()
        return public(a)


def reset_password(account_id: int) -> str:
    """A one-time password the Super Admin passes on; the customer changes it after signing in."""
    temp = secrets.token_urlsafe(9) + "7a"
    set_password(account_id, temp)
    return temp


def delete(account_id: int):
    """Remove a pending or blocked account's login. Its private data (if any) is left in place on purpose."""
    with CoreSession() as s:
        a = s.get(Account, account_id)
        if not a:
            raise KeyError(account_id)
        if a.status == "active":
            raise ValueError("Block the account first.")
        s.delete(a)
        s.commit()
    _changed()


def active_tenants() -> list[int]:
    """Every private copy that background work should run for: the owner's and each active customer's."""
    with CoreSession() as s:
        ids = [a.tenant_id for a in s.query(Account).filter(Account.status == "active", Account.tenant_id.isnot(None))]
    return [tenant.OWNER] + sorted(ids)


def self_test() -> dict:
    """Prove on THIS database that a customer's copy is separate: write in a throw-away copy, look from the owner's."""
    from .db import SessionLocal, Setting, _engines, _makers, _ready

    tid, marker = SELF_TEST_TENANT, "isolation-" + secrets.token_hex(4)
    out = {"database": "sqlite" if IS_SQLITE else "postgresql"}
    try:
        with tenant.use(tid):
            with SessionLocal() as s:
                s.merge(Setting(key="self_test", value=marker))
                s.commit()
            with SessionLocal() as s:
                row = s.get(Setting, "self_test")
                out["written_in_copy"] = bool(row and row.value == marker)
        with SessionLocal() as s:  # the owner's copy
            row = s.get(Setting, "self_test")
            out["invisible_to_owner"] = not (row and row.value == marker)
        out["ok"] = out["written_in_copy"] and out["invisible_to_owner"]
    finally:
        try:
            eng = _engines.pop(tid, None)
            _makers.pop(tid, None)
            _ready.discard(tid)
            if IS_SQLITE:
                import shutil

                if eng is not None:
                    eng.dispose()
                shutil.rmtree(tenant.root(tid), ignore_errors=True)
            else:
                from sqlalchemy import text

                with engine.begin() as conn:
                    conn.execute(text(f'DROP SCHEMA IF EXISTS "{tenant.schema(tid)}" CASCADE'))
        except Exception as e:  # noqa: BLE001
            out["cleanup"] = str(e)[:120]
    return out

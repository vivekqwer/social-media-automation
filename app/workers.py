"""Helper PCs ("workers"): run on the owner's own internet connection, where YouTube does not bot-block,
and fetch video sections / voice-overs for the server. Each helper must be approved in the dashboard."""
import hashlib
from datetime import timedelta

from .db import SessionLocal, Worker, utcnow

ONLINE_WINDOW = timedelta(minutes=10)  # a slow home connection can spend several minutes on one download without calling in


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def pair_code(token: str) -> str:
    """6-digit code the helper shows; the owner types it when approving, so a stranger's helper can't be approved by mistake."""
    return str(int(hashlib.sha256(("pair:" + token).encode()).hexdigest(), 16) % 1_000_000).zfill(6)


def tenant_of(token: str) -> int:
    """Which customer a helper belongs to. Tokens of customers' helpers start with "t<number>."; the owner's do not."""
    import re

    m = re.match(r"^t(\d{1,9})\.", token or "")
    return int(m.group(1)) if m else 1


def claim_code(tenant_id: int) -> str:
    """What a customer types into a new helper so it attaches to THEIR copy (shown in their dashboard)."""
    import hmac

    from .auth import _secret

    sig = hmac.new(_secret(), f"helper-claim:{int(tenant_id)}".encode(), hashlib.sha256).hexdigest()[:10]
    return f"{int(tenant_id)}-{sig}"


def tenant_from_claim(code: str) -> int | None:
    import hmac

    try:
        tid = int((code or "").split("-", 1)[0])
    except ValueError:
        return None
    return tid if tid > 1 and hmac.compare_digest(code, claim_code(tid)) else None


def authenticate(token: str) -> tuple[int, bool] | None:
    """(worker_id, approved) for a valid token, and record that the helper is alive. None if unknown."""
    if not token:
        return None
    with SessionLocal() as s:
        w = s.query(Worker).filter(Worker.token_hash == token_hash(token)).first()
        if not w:
            return None
        w.last_seen = utcnow()
        s.commit()
        return w.id, bool(w.approved)


def any_approved() -> bool:
    with SessionLocal() as s:
        return s.query(Worker).filter(Worker.approved.is_(True)).count() > 0


RECENT_WINDOW = timedelta(days=2)


def any_recent(kind: str = "source") -> bool:
    """An approved helper that was online lately — only then is it worth waiting for it. A customer without a helper
    of their own is also served by the Super Admin's central helper, if that one can do this kind of job."""
    with SessionLocal() as s:
        if s.query(Worker).filter(Worker.approved.is_(True), Worker.last_seen >= utcnow() - RECENT_WINDOW).count() > 0:
            return True
    return central_online(kind, RECENT_WINDOW)


# ---------------------------------------------------------------- central helper (one helper for every account)
KINDS = ("source", "transcribe", "analyze", "reel", "tts", "script", "setup_ai")
TICKET_TTL = 6 * 3600  # seconds a central helper has to deliver a job it was given (a slow phone upload included)
# which delivery addresses (/api/worker/<name>/<id>) a job's ticket opens
TICKET_PATHS = {"source": ("source", "captions", "fail"), "reel": ("source", "fail"), "tts": ("voice", "fail"),
                "analyze": ("analysis", "fail"), "script": ("script", "fail")}


def parse_can(header: str | None) -> frozenset | None:
    """What a helper says it can do (header x-worker-can). None = an older helper: everything, but no job tickets."""
    if header is None:
        return None
    words = {w.strip().lower() for w in header.split(",") if w.strip()}
    return frozenset(KINDS) if "all" in words else frozenset(words & set(KINDS))


def can_do(can, kind: str) -> bool:
    """`can` is a set from parse_can, the text stored in Worker.can, or None (an older helper: everything)."""
    if can is None:
        return True
    if isinstance(can, str):
        can = parse_can(can)
    return kind in can


def can_text(can: frozenset | None) -> str | None:
    if can is None:
        return None
    return "all" if can >= set(KINDS) else ",".join(k for k in KINDS if k in can)


def own_online() -> bool:
    """This copy has its own approved helper that called in lately (it is preferred over the central one)."""
    with SessionLocal() as s:
        return s.query(Worker).filter(Worker.approved.is_(True), Worker.last_seen >= utcnow() - ONLINE_WINDOW).count() > 0


def central_online(kind: str = "source", window: timedelta = ONLINE_WINDOW) -> bool:
    """For a customer's copy: is the Super Admin's central helper around, and can it do `kind`?"""
    from . import tenant

    if tenant.current() == tenant.OWNER:
        return False  # the owner's own helpers are counted as they are
    try:
        with tenant.use(tenant.OWNER), SessionLocal() as s:
            rows = s.query(Worker).filter(Worker.central.is_(True), Worker.approved.is_(True), Worker.can.isnot(None),
                                          Worker.last_seen >= utcnow() - window).all()
            return any(can_do(w.can, kind) for w in rows)
    except Exception:  # noqa: BLE001  (never let this question break a page)
        return False


def is_central(wid: int) -> bool:
    """Is this helper (of the owner's copy) marked central and speaking the ticket protocol?"""
    from . import tenant

    with tenant.use(tenant.OWNER), SessionLocal() as s:
        w = s.get(Worker, wid)
        return bool(w and w.approved and w.central and w.can is not None)


def _b64(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _ticket_sig(payload: str) -> str:
    import hmac

    from .auth import _secret

    return hmac.new(_secret(), ("job-ticket:" + payload).encode(), hashlib.sha256).hexdigest()


def make_ticket(tenant_id: int, kind: str, obj_id: int, worker_id: int, ttl: int = TICKET_TTL) -> str:
    """A signed, opaque note that goes with one job handed to the central helper: which copy, which job, which helper.
    The helper sends it back with the result; the server reads the copy from here, never from the helper."""
    import time

    payload = f"{int(tenant_id)}|{kind}|{int(obj_id)}|{int(worker_id)}|{int(time.time()) + int(ttl)}"
    return _b64(payload.encode()) + "." + _ticket_sig(payload)


def read_ticket(ticket: str) -> dict | None:
    """The ticket's contents if it was signed here, is unchanged and has not run out; else None."""
    import base64
    import hmac
    import time

    try:
        body, sig = (ticket or "").strip().rsplit(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        if not hmac.compare_digest(sig, _ticket_sig(payload)):
            return None
        tid, kind, oid, wid, exp = payload.split("|")
        if kind not in TICKET_PATHS or int(exp) < time.time():
            return None
        return {"tid": int(tid), "kind": kind, "id": int(oid), "wid": int(wid)}
    except Exception:  # noqa: BLE001  (anything malformed is simply not a ticket)
        return None


def as_dict(w: Worker) -> dict:
    online = bool(w.last_seen and utcnow() - w.last_seen < ONLINE_WINDOW)
    return {
        "id": w.id,
        "name": w.name,
        "approved": bool(w.approved),
        "ip": w.ip,
        "online": online,
        "central": bool(w.central),
        "can": w.can,
        "last_seen": (w.last_seen.isoformat() + "Z") if w.last_seen else None,
        "created_at": (w.created_at.isoformat() + "Z") if w.created_at else None,
    }

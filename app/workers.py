"""Helper PCs ("workers"): run on the owner's own internet connection, where YouTube does not bot-block,
and fetch video sections / voice-overs for the server. Each helper must be approved in the dashboard."""
import hashlib
from datetime import timedelta

from .db import SessionLocal, Worker, utcnow

ONLINE_WINDOW = timedelta(minutes=2)


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


def any_recent() -> bool:
    """An approved helper that was online lately — only then is it worth waiting for it."""
    with SessionLocal() as s:
        return s.query(Worker).filter(Worker.approved.is_(True), Worker.last_seen >= utcnow() - RECENT_WINDOW).count() > 0


def as_dict(w: Worker) -> dict:
    online = bool(w.last_seen and utcnow() - w.last_seen < ONLINE_WINDOW)
    return {
        "id": w.id,
        "name": w.name,
        "approved": bool(w.approved),
        "ip": w.ip,
        "online": online,
        "last_seen": (w.last_seen.isoformat() + "Z") if w.last_seen else None,
        "created_at": (w.created_at.isoformat() + "Z") if w.created_at else None,
    }

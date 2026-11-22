"""Online payments with Razorpay: a customer picks a plan, pays, and the plan is put on their account by itself.

* The Super Admin pastes the Razorpay keys once (Users → Payments). They are kept in the owner's settings; the two
  secret parts are encrypted (app/crypto.py) and never sent back to a browser. Test or live is decided by the key.
* Every plan can have a price (₹), a billing period (monthly / yearly / one-time) and a short description.
* Pay: the server makes the Razorpay order (the amount comes from the saved price, never from the browser), the
  browser opens Razorpay Checkout, and the payment is confirmed twice, both safe to repeat:
  1. the browser sends Razorpay's signature back (HMAC-SHA256 of "order_id|payment_id" with the key secret);
  2. Razorpay calls our webhook (HMAC-SHA256 of the raw body with the webhook secret), so a closed browser still
     gets its plan. The same payment is applied only once (one atomic status change from "created" to "paid").
* A plan with a period ends: 3 days before, the customer is told; after the end the account goes back to the plan
  for free accounts with 1 channel. Nothing is deleted — extra channels simply stop uploading (see acl.channel_cap).
* No keys saved: customers see the manual UPI steps and the "I have paid" request exactly as before.

The `payments` table is shared (next to `accounts`), like the activity log.
"""
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from datetime import timedelta

import httpx
from sqlalchemy import Column, DateTime, Integer, String, update

from . import tenant
from .accounts import Account, CoreBase, CoreSession
from .crypto import TokenError, decrypt, encrypt
from .db import get_setting, set_setting, utcnow

log = logging.getLogger(__name__)
API = "https://api.razorpay.com/v1"
KEYS = "razorpay"  # owner setting: {"key_id": ..., "key_secret": enc, "webhook_secret": enc}
PRICES = "plan_prices"  # owner setting: {plan name: {"price": rupees, "period": "monthly", "description": "..."}}
PERIODS = {"monthly": ("per month", 30), "yearly": ("per year", 365), "once": ("one-time", None)}
KEY_ID_RE = re.compile(r"^rzp_(test|live)_[A-Za-z0-9]{6,40}$")
MAX_PRICE = 1_000_000  # ₹ 10 lakh: anything above is a typing mistake
ORDERS_PER_HOUR = 10  # per account
WARN_DAYS = 3


class Payment(CoreBase):
    """One Razorpay order and what became of it (shared: next to the accounts)."""

    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, index=True)
    plan = Column(String(40))
    amount = Column(Integer)  # paise (₹1 = 100), exactly what Razorpay was asked for
    currency = Column(String(3), default="INR")
    period = Column(String(10))
    order_id = Column(String(60), unique=True)
    status = Column(String(10), default="created")  # created | paid | failed
    payment_id = Column(String(60))
    via = Column(String(10))  # checkout | webhook (which confirmation came first)
    note = Column(String(300))  # e.g. "the plan was deleted before the payment — apply it by hand"
    created_at = Column(DateTime, default=utcnow, index=True)
    paid_at = Column(DateTime)


# ---------------------------------------------------------------- keys (owner's settings)
def _owner_get(key: str) -> str | None:
    with tenant.use(tenant.OWNER):
        return get_setting(key)


def _owner_set(key: str, value: str):
    with tenant.use(tenant.OWNER):
        set_setting(key, value)


def _raw_keys() -> dict:
    try:
        return json.loads(_owner_get(KEYS) or "{}")
    except ValueError:
        return {}


def _keys() -> tuple[str, str, str]:
    """(key id, key secret, webhook secret) in plain text — only for talking to Razorpay, never for a browser."""
    raw = _raw_keys()

    def plain(v):
        try:
            return decrypt(v) if v else ""
        except TokenError:
            return ""

    return raw.get("key_id") or "", plain(raw.get("key_secret")), plain(raw.get("webhook_secret"))


def mode(key_id: str | None = None) -> str:
    kid = _keys()[0] if key_id is None else key_id
    return "live" if kid.startswith("rzp_live_") else "test" if kid.startswith("rzp_test_") else ""


def enabled() -> bool:
    """Online payment is possible: the key id and the key secret are saved."""
    kid, secret, _w = _keys()
    return bool(kid and secret)


def keys_public(webhook_url: str = "") -> dict:
    """What the Super Admin's Payments box shows: never a secret, only that it is saved."""
    kid, secret, wh = _keys()
    return {"key_set": bool(kid), "key_end": kid[-4:] if kid else "", "secret_set": bool(secret), "webhook_set": bool(wh),
            "mode": mode(kid), "enabled": bool(kid and secret), "webhook_url": webhook_url}


def save_keys(key_id: str = "", key_secret: str = "", webhook_secret: str = "", clear: bool = False) -> None:
    """An empty field keeps what is saved. `clear` removes everything (customers then see the manual UPI steps)."""
    if clear:
        _owner_set(KEYS, "{}")
        return
    raw = _raw_keys()
    key_id, key_secret, webhook_secret = (key_id or "").strip(), (key_secret or "").strip(), (webhook_secret or "").strip()
    if key_id:
        if not KEY_ID_RE.match(key_id):
            raise ValueError("The Key ID starts with rzp_test_ or rzp_live_. Copy it again from Razorpay → Account & Settings → API Keys.")
        if raw.get("key_id") and mode(raw["key_id"]) != mode(key_id) and not key_secret:
            raise ValueError("You switched between test and live keys: paste the Key Secret that belongs to the new Key ID too.")
        raw["key_id"] = key_id
    for name, v in (("key_secret", key_secret), ("webhook_secret", webhook_secret)):
        if v:
            if len(v) > 200 or not re.match(r"^[\x21-\x7e]+$", v):
                raise ValueError("That secret does not look right. Copy it again (no spaces).")
            raw[name] = encrypt(v)
    _owner_set(KEYS, json.dumps(raw))


def check_keys() -> dict:
    """Ask Razorpay whether the saved key id + secret work (one harmless read: the newest order)."""
    kid, secret, _w = _keys()
    if not (kid and secret):
        return {"ok": False, "message": "Save the Key ID and the Key Secret first."}
    try:
        r = httpx.get(API + "/orders", params={"count": 1}, auth=(kid, secret), timeout=15)
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"Razorpay could not be reached ({type(e).__name__}). Try again in a minute."}
    if r.status_code == 200:
        return {"ok": True, "mode": mode(kid), "message": f"The keys work ({mode(kid)} mode)."}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "Razorpay says the Key ID or the Key Secret is wrong. Copy both again (a new secret "
                                        "is shown only once, when you generate the key)."}
    return {"ok": False, "message": f"Razorpay answered {r.status_code}. Try again in a minute."}


# ---------------------------------------------------------------- plan prices (owner's settings)
def prices() -> dict:
    try:
        raw = json.loads(_owner_get(PRICES) or "{}")
    except ValueError:
        raw = {}
    return {str(n): p for n, p in raw.items() if isinstance(p, dict)}


def price_of(plan: str) -> dict | None:
    """{"price", "period", "description"} of a plan, when it is for sale (price above 0)."""
    want = (plan or "").strip().lower()
    hit = next((p for n, p in prices().items() if n.lower() == want), None)
    return hit if hit and int(hit.get("price") or 0) > 0 else None


def set_price(plan: str, price: int | None, period: str = "monthly", description: str = ""):
    """price None or 0 = not for sale (customers do not see the plan on their Plans card)."""
    if period not in PERIODS:
        raise ValueError("Choose monthly, yearly or one-time.")
    if price is not None and not 0 <= int(price) <= MAX_PRICE:
        raise ValueError(f"The price must be between ₹0 and ₹{MAX_PRICE:,}.")
    p = prices()
    for n in [n for n in p if n.lower() == plan.lower()]:
        p.pop(n)
    if price:
        p[plan] = {"price": int(price), "period": period, "description": re.sub(r"\s+", " ", description or "").strip()[:200]}
    _owner_set(PRICES, json.dumps(p))


def drop_price(plan: str):
    p = prices()
    for n in [n for n in p if n.lower() == (plan or "").lower()]:
        p.pop(n)
    _owner_set(PRICES, json.dumps(p))


def with_prices(overview: dict) -> dict:
    """acl.overview() plus each plan's price (for the Super Admin's Plans & defaults list)."""
    p = {n.lower(): v for n, v in prices().items()}
    for plan in overview.get("plans") or []:
        plan["price"] = p.get(plan["name"].lower()) or None
    overview["online_pay"] = enabled()
    return overview


def _includes(plan: str) -> dict:
    """What a plan gives, from its limits (empty = no limit)."""
    from . import acl

    cfg = acl.config()
    eff = acl._resolve({"plan": plan}, cfg)
    ch = (cfg["plans"].get(plan) or {"limits": {}})["limits"].get("channels")
    return {"channels": ch or 1, **{k: eff["limits"].get(k) for k in acl.LIMIT_KEYS}}


def catalog(account_id: int | None) -> dict:
    """The customer's Plans card: every plan for sale, what it includes, the current plan and when it ends."""
    from . import acl

    cfg = acl.config()
    p = {n.lower(): v for n, v in prices().items()}
    rows = []
    for name in sorted(cfg["plans"], key=str.lower):
        pr = p.get(name.lower())
        if not pr or int(pr.get("price") or 0) <= 0:
            continue
        rows.append({"name": name, "price": int(pr["price"]), "period": pr.get("period") or "monthly",
                     "period_text": PERIODS.get(pr.get("period") or "monthly", PERIODS["monthly"])[0],
                     "description": pr.get("description") or "", "includes": _includes(name)})
    rows.sort(key=lambda r: r["price"])
    out = {"plans": rows, "online": enabled(), "mode": mode(), "current": "", "expires": None, "channel_limit": None}
    if account_id:
        a = _account(account_id)
        if a:
            out.update(current=acl._plan_name(cfg, acl._plan_of(a)) or "", channel_limit=a.channel_limit or 1,
                       expires=a.plan_expires.isoformat() + "Z" if a.plan_expires else None)
    return out


# ---------------------------------------------------------------- paying
_orders: dict[int, list[float]] = {}
_olock = threading.Lock()


def _rate_limited(account_id: int) -> bool:
    now = time.time()
    with _olock:
        recent = [t for t in _orders.get(account_id, []) if now - t < 3600]
        if len(recent) >= ORDERS_PER_HOUR:
            _orders[account_id] = recent
            return True
        _orders[account_id] = recent + [now]
        return False


def _account(account_id: int) -> Account | None:
    with CoreSession() as s:
        return s.get(Account, int(account_id))


def create_order(account_id: int, plan: str) -> dict:
    """Make the Razorpay order for one plan. The amount is the saved price — whatever the browser says."""
    from . import acl

    kid, secret, _w = _keys()
    if not (kid and secret):
        raise ValueError("Online payment is not set up yet. Use the UPI steps below.")
    a = _account(account_id)
    if not a or a.status != "active":
        raise ValueError("This account cannot pay right now.")
    name = acl._plan_name(acl.config(), plan)
    pr = price_of(name or "")
    if not name or not pr:
        raise ValueError("This plan is not for sale. Choose another one.")
    if _rate_limited(account_id):
        raise PermissionError("Too many payment attempts. Try again in an hour.")
    amount = int(pr["price"]) * 100
    body = {"amount": amount, "currency": "INR", "receipt": f"a{a.id}-{int(time.time())}",
            "notes": {"account_id": str(a.id), "plan": name, "email": a.email}}
    try:
        r = httpx.post(API + "/orders", json=body, auth=(kid, secret), timeout=20)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Razorpay could not be reached ({type(e).__name__}). Try again in a minute.") from e
    if r.status_code != 200:
        log.warning("razorpay order: %s %s", r.status_code, r.text[:200])
        raise RuntimeError("Razorpay did not accept the payment request. Try again later, or use the UPI steps.")
    order = r.json()
    oid = str(order.get("id") or "")
    if not oid.startswith("order_") or int(order.get("amount") or 0) != amount:
        raise RuntimeError("Razorpay answered something unexpected. Try again later.")
    with CoreSession() as s:
        s.add(Payment(account_id=a.id, plan=name, amount=amount, currency="INR", period=pr.get("period") or "monthly",
                      order_id=oid, status="created", created_at=utcnow()))
        s.commit()
    return {"order_id": oid, "amount": amount, "currency": "INR", "key_id": kid, "plan": name,
            "name": a.brand or "Raji Studio", "description": f"Plan “{name}” — {PERIODS[pr.get('period') or 'monthly'][0]}",
            "prefill": {"name": a.name or "", "email": a.email or ""}}


def _sign(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_checkout(account_id: int, order_id: str, payment_id: str, signature: str) -> dict:
    """The browser's confirmation after Checkout. Only the account that made the order can confirm it."""
    _kid, secret, _w = _keys()
    if not secret:
        raise ValueError("Online payment is not set up.")
    with CoreSession() as s:
        row = s.query(Payment).filter(Payment.order_id == order_id).first()
    if not row or row.account_id != int(account_id):
        raise ValueError("This payment is not known. If money was taken, it is applied by itself within minutes.")
    good = _sign(secret, f"{order_id}|{payment_id}".encode())
    if not hmac.compare_digest(good, (signature or "").strip().lower()):
        raise ValueError("The payment could not be confirmed. If money was taken, it is applied by itself within minutes.")
    return mark_paid(order_id, payment_id, "checkout")


def webhook(raw: bytes, signature: str) -> dict:
    """Razorpay's own call (no sign-in). Checked with the webhook secret over the exact bytes received."""
    _kid, _secret, wh = _keys()
    if not wh:
        raise PermissionError("No webhook secret saved.")
    if not hmac.compare_digest(_sign(wh, raw), (signature or "").strip().lower()):
        raise PermissionError("Wrong signature.")
    try:
        ev = json.loads(raw)
    except ValueError:
        raise PermissionError("Not JSON.")
    kind = str(ev.get("event") or "")
    pay = (((ev.get("payload") or {}).get("payment") or {}).get("entity")) or {}
    order = (((ev.get("payload") or {}).get("order") or {}).get("entity")) or {}
    oid = str(pay.get("order_id") or order.get("id") or "")
    pid = str(pay.get("id") or "")
    if not oid:
        return {"ok": True, "ignored": "no order"}
    with CoreSession() as s:
        row = s.query(Payment).filter(Payment.order_id == oid).first()
    if not row:
        return {"ok": True, "ignored": "unknown order"}  # not ours (e.g. another app on the same Razorpay account)
    if kind == "payment.failed":
        with CoreSession() as s:
            s.execute(update(Payment).where(Payment.order_id == oid, Payment.status == "created").values(status="failed", payment_id=pid))
            s.commit()
        return {"ok": True, "status": "failed"}
    if kind not in ("payment.captured", "order.paid"):
        return {"ok": True, "ignored": kind}
    paid = int(pay.get("amount") or order.get("amount_paid") or 0)
    if paid != row.amount or str(pay.get("currency") or order.get("currency") or "INR") != row.currency:
        log.warning("razorpay webhook: amount %s for order %s (expected %s)", paid, oid, row.amount)
        return {"ok": True, "ignored": "amount differs"}
    return mark_paid(oid, pid, "webhook")


def _expiry(a: Account, plan: str, period: str):
    days = PERIODS.get(period or "monthly", PERIODS["monthly"])[1]
    if days is None:
        return None  # one-time: the plan does not end
    from . import acl

    now = utcnow()
    same = (acl._plan_of(a) or "").lower() == plan.lower()
    start = a.plan_expires if same and a.plan_expires and a.plan_expires > now else now  # renewing adds to what is left
    return start + timedelta(days=days)


def mark_paid(order_id: str, payment_id: str, via: str) -> dict:
    """Mark an order paid and put its plan on the account — once, however often this is called."""
    from . import acl

    now = utcnow()
    with CoreSession() as s:
        res = s.execute(update(Payment).where(Payment.order_id == order_id, Payment.status != "paid")
                        .values(status="paid", payment_id=payment_id or None, paid_at=now, via=via))
        s.commit()
        row = s.query(Payment).filter(Payment.order_id == order_id).first()
    if res.rowcount != 1:
        return {"ok": True, "already": True, "plan": row.plan if row else None}
    a = _account(row.account_id)
    if not a:
        _note(order_id, "the account is gone — refund it in Razorpay")
        _tell_owner(f"💰 ₹{row.amount / 100:,.0f} received for an account that no longer exists (order {order_id}). Refund it in Razorpay.")
        return {"ok": True, "applied": False}
    expires = _expiry(a, row.plan, row.period)
    try:
        acl.apply_plan(a.id, row.plan)
    except (ValueError, KeyError):
        _note(order_id, "the plan was deleted before the payment — put the customer on a plan by hand")
        _tell_owner(f"💰 ₹{row.amount / 100:,.0f} received from {a.brand or a.name} ({a.email}), but the plan “{row.plan}” "
                    "no longer exists. Put them on a plan by hand (Users → Access).")
        return {"ok": True, "applied": False}
    with CoreSession() as s:
        acc = s.get(Account, a.id)
        acc.plan_expires, acc.expiry_warned = expires, None
        if acc.upgrade:
            acc.upgrade = None  # an open "I have paid" request is answered by this payment
        s.commit()
    acl.invalidate()
    until = f" until {expires:%d %b %Y}" if expires else ""
    acl.record(a.id, "system", "payment", f"Paid ₹{row.amount / 100:,.0f} online — plan “{row.plan}”{until}", a.tenant_id)
    _tell_owner(f"💰 ₹{row.amount / 100:,.0f} received from {_e(a.brand or a.name)} — plan “{_e(row.plan)}” applied{until}.")
    _tell_customer(a, f"✅ Payment received: ₹{row.amount / 100:,.0f}. Your plan “{_e(row.plan)}” is active{until}. Thank you!")
    return {"ok": True, "applied": True, "plan": row.plan, "expires": expires.isoformat() + "Z" if expires else None}


def _note(order_id: str, text: str):
    with CoreSession() as s:
        s.execute(update(Payment).where(Payment.order_id == order_id).values(note=text[:300]))
        s.commit()


def _e(t) -> str:
    return str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tell_owner(text: str):
    try:
        from .telegram import notify

        with tenant.use(tenant.OWNER):
            notify(text)
    except Exception:  # noqa: BLE001
        pass


def _tell_customer(a: Account, text: str):
    """The customer's own alerts (their Telegram / phone app / Gmail, whatever they set up). Never raises."""
    if not a.tenant_id:
        return
    try:
        from .db import tenant_exists
        from .telegram import notify

        if tenant_exists(a.tenant_id):
            with tenant.use(a.tenant_id):
                notify(text)
    except Exception:  # noqa: BLE001
        pass


def listing(limit: int = 200) -> list[dict]:
    """The Super Admin's payments list, newest first."""
    with CoreSession() as s:
        rows = s.query(Payment).order_by(Payment.id.desc()).limit(max(1, min(int(limit), 1000))).all()
        who = {a.id: a for a in s.query(Account).filter(Account.id.in_({r.account_id for r in rows}))} if rows else {}
    return [{"id": r.id, "account_id": r.account_id, "name": (who.get(r.account_id).name if who.get(r.account_id) else "(deleted)"),
             "email": who.get(r.account_id).email if who.get(r.account_id) else "", "brand": who.get(r.account_id).brand if who.get(r.account_id) else "",
             "plan": r.plan, "amount": r.amount / 100, "status": r.status, "order_id": r.order_id, "payment_id": r.payment_id or "",
             "via": r.via or "", "note": r.note or "", "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
             "paid_at": r.paid_at.isoformat() + "Z" if r.paid_at else None} for r in rows]


# ---------------------------------------------------------------- plans that end (daily, once, as the owner)
def _free_plan() -> str:
    """Where an account goes when its paid plan ends: the plan new accounts start on, unless that one costs money."""
    from . import acl

    start = acl.new_account_plan()
    return "" if not start or price_of(start) else start


def expire_due() -> dict:
    """Warn 3 days before a plan ends; after the end put the account back on the free plan with 1 channel.
    Nothing is deleted: extra channels stay connected, their uploads just wait (acl.channel_cap)."""
    from . import acl

    now, warned, ended = utcnow(), 0, 0
    with CoreSession() as s:
        rows = [(a.id, a.plan_expires, a.expiry_warned) for a in
                s.query(Account).filter(Account.plan_expires.isnot(None), Account.status == "active")]
    for aid, exp, warned_for in rows:
        try:
            a = _account(aid)
            if exp <= now:
                free = _free_plan()
                old = acl._plan_of(a)
                acl.apply_plan(aid, free)
                with CoreSession() as s:
                    acc = s.get(Account, aid)
                    acc.channel_limit, acc.plan_expires, acc.expiry_warned = 1, None, None
                    s.commit()
                acl.invalidate()
                acl.record(aid, "system", "payment", f"The plan “{old}” ended — back to {('the plan “' + free + '”') if free else 'the free switches'} with 1 channel", a.tenant_id)
                _tell_customer(a, f"⏰ Your plan “{_e(old)}” has ended. Your account is back on the free plan with 1 YouTube channel. "
                                  "Nothing was deleted — renew on My channels → Your plan to switch everything on again.")
                _tell_owner(f"⏰ The plan “{_e(old)}” of {_e(a.brand or a.name)} ({_e(a.email)}) ended — moved back to the free plan.")
                ended += 1
            elif exp - now <= timedelta(days=WARN_DAYS) and warned_for != exp:
                _tell_customer(a, f"⏰ Your plan “{_e(acl._plan_of(a))}” ends on {exp:%d %b %Y}. Renew it on My channels → Your plan "
                                  "so nothing stops.")
                with CoreSession() as s:
                    s.get(Account, aid).expiry_warned = exp
                    s.commit()
                warned += 1
        except Exception:  # noqa: BLE001  (one account's problem must not stop the others)
            log.exception("plan expiry failed for account %s", aid)
    return {"warned": warned, "ended": ended}

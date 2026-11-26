"""Online payments with Razorpay: a customer picks a plan, pays, and the plan is put on their account by itself.

* The Super Admin pastes the Razorpay keys once (Users → Payments). They are kept in the owner's settings; the two
  secret parts are encrypted (app/crypto.py) and never sent back to a browser. Test or live is decided by the key.
* Every plan can have a price (₹), a billing period (monthly / yearly / one-time) and a short description.
* Pay: the server makes the Razorpay order (the amount comes from the saved price, never from the browser), the
  browser opens Razorpay Checkout, and the payment is confirmed twice, both safe to repeat:
  1. the browser sends Razorpay's signature back (HMAC-SHA256 of "order_id|payment_id" with the key secret);
  2. Razorpay calls our webhook (HMAC-SHA256 of the raw body with the webhook secret), so a closed browser still
     gets its plan. The same payment is applied only once (one atomic status change from "created" to "paid").
* A plan with a period ends: 3 days before, the customer is told; after the end the account goes back to the one-time
  plan it owns (Account.owned_plan), else to the plan for free accounts, with the channel limit it had before paying.
  Nothing is deleted — extra channels simply stop uploading (see acl.channel_cap).
* Putting a plan on is one transaction per account (acl._write_lock + a row lock): plan, end date and the payment's
  applied mark are saved together, from the row as it is then. A "paid" row without the mark (the server stopped half
  way) is finished by recover_unapplied() (every 10 minutes) or by Razorpay's next webhook retry — once.
* The last webhook call (time, ok / failed: why) is kept in the owner's settings and shown on the Razorpay card.
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
    mode = Column(String(4))  # test | live — which Razorpay keys made the order (test = no real money)
    note = Column(String(300))  # e.g. "the plan was deleted before the payment — apply it by hand"
    created_at = Column(DateTime, default=utcnow, index=True)
    paid_at = Column(DateTime)
    # handled: the plan was put on (in the same transaction as the account's plan and end date), or the owner was told to
    # do it by hand (see note). A "paid" row without it was cut off half way: recover_unapplied() finishes it.
    applied_at = Column(DateTime)


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
            "mode": mode(kid), "enabled": bool(kid and secret), "webhook_url": webhook_url, "last_webhook": webhook_last()}


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
        raise ValueError(f"The price must be between ₹0 and {inr(MAX_PRICE)}.")
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


def inr(rupees) -> str:
    """₹ in the Indian way (₹2,499 · ₹1,00,000), the same as the page's inr()."""
    n = round(float(rupees or 0), 2)
    whole, paise = int(n), round((n - int(n)) * 100)
    d = str(whole)
    head, tail = d[:-3], d[-3:]
    groups = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return "₹" + "".join(g + "," for g in groups) + tail + (f".{paise:02d}" if paise else "")


def _includes(plan: str) -> dict:
    """What a plan gives, from its limits (empty = no limit). channels None = the plan leaves the account's channel
    limit as it is (acl.apply_plan only sets it when the plan has one)."""
    from . import acl

    cfg = acl.config()
    eff = acl._resolve({"plan": plan}, cfg)
    ch = (cfg["plans"].get(plan) or {"limits": {}})["limits"].get("channels")
    return {"channels": ch or None, **{k: eff["limits"].get(k) for k in acl.LIMIT_KEYS}}


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
                       expires=a.plan_expires.isoformat() + "Z" if a.plan_expires else None,
                       owned=a.owned_plan or (a.paid_plan if a.paid_plan and not a.plan_expires else None))
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
                      order_id=oid, status="created", mode=mode(kid), created_at=utcnow()))
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
    if row.status == "paid":
        return {"ok": True, "already": True, "plan": row.plan}
    # the signature proves Razorpay made this payment id; whether the money is really taken is asked from Razorpay
    pay = _captured(payment_id, row)
    if pay is None:  # Razorpay not reachable now: the row stays "created", the webhook finishes it
        return {"ok": True, "pending": True, "plan": row.plan,
                "message": "Payment received — your plan will switch on in a minute."}
    if not pay:
        raise ValueError("Razorpay has not confirmed this payment. If money was taken, it is applied by itself within minutes, "
                         "or refunded by Razorpay.")
    return mark_paid(order_id, payment_id, "checkout")


def _captured(payment_id: str, row: "Payment") -> bool | None:
    """Ask Razorpay about the payment: True = captured for exactly this order, amount and currency (an authorized one is
    captured here first); False = not (failed, other order, other amount); None = Razorpay could not be asked."""
    kid, secret, _w = _keys()
    if not re.match(r"^pay_[A-Za-z0-9]{6,40}$", payment_id or ""):
        return False
    try:
        r = httpx.get(f"{API}/payments/{payment_id}", auth=(kid, secret), timeout=15)
        if r.status_code >= 500 or r.status_code == 429:
            return None
        if r.status_code != 200:
            return False
        p = r.json()
        if p.get("order_id") != row.order_id or int(p.get("amount") or 0) != row.amount or p.get("currency") != "INR":
            log.warning("razorpay payment %s does not match order %s", payment_id, row.order_id)
            return False
        if p.get("status") == "authorized":
            r = httpx.post(f"{API}/payments/{payment_id}/capture", json={"amount": row.amount, "currency": "INR"},
                           auth=(kid, secret), timeout=20)
            if r.status_code >= 500 or r.status_code == 429:
                return None
            if r.status_code != 200:
                return False
            p = r.json()
        return p.get("status") == "captured" and p.get("order_id") == row.order_id and int(p.get("amount") or 0) == row.amount
    except (httpx.HTTPError, ValueError):
        return None


WEBHOOK_LAST = "razorpay_webhook_last"  # owner setting: {"at", "ok", "reason"} of the last call to the webhook
_wh_last = {"at": 0.0, "ok": None}


def _webhook_seen(ok: bool, reason: str = ""):
    """Remember the last webhook call for the Razorpay card. A wrong call (anybody can send one) is written at most once
    a minute, so a flood cannot keep the database busy; a good one always."""
    now = time.time()
    if not ok and _wh_last["ok"] is False and now - _wh_last["at"] < 60:
        return
    _wh_last.update(at=now, ok=ok)
    try:
        _owner_set(WEBHOOK_LAST, json.dumps({"at": utcnow().isoformat() + "Z", "ok": ok, "reason": reason[:120]}))
    except Exception:  # noqa: BLE001
        log.warning("could not save the webhook status")


def webhook_last() -> dict | None:
    try:
        v = json.loads(_owner_get(WEBHOOK_LAST) or "null")
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def webhook(raw: bytes, signature: str) -> dict:
    """Razorpay's own call (no sign-in); its outcome is remembered for the Razorpay card."""
    try:
        out = _webhook(raw, signature)
    except PermissionError as e:
        _webhook_seen(False, {"No webhook secret saved.": "no webhook secret saved here",
                              "Wrong signature.": "wrong signature (the webhook secret differs)",
                              "Not JSON.": "not a Razorpay message"}.get(str(e), str(e)))
        raise
    except Exception as e:
        _webhook_seen(False, f"error on this server ({type(e).__name__})")
        raise
    _webhook_seen(True)
    return out


def _webhook(raw: bytes, signature: str) -> dict:
    """Checked with the webhook secret over the exact bytes received."""
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
    if kind not in ("payment.captured", "order.paid"):  # the only events that put a plan on an account
        return {"ok": True, "ignored": kind}
    if (kind == "payment.captured" and pay.get("status", "captured") != "captured") or \
            (kind == "order.paid" and order and order.get("status", "paid") != "paid"):
        return {"ok": True, "ignored": "not captured"}
    paid = int(pay.get("amount") or order.get("amount_paid") or 0)
    if paid != row.amount or str(pay.get("currency") or order.get("currency") or "INR") != row.currency:
        log.warning("razorpay webhook: amount %s for order %s (expected %s)", paid, oid, row.amount)
        return {"ok": True, "ignored": "amount differs"}
    return mark_paid(oid, pid, "webhook")


def _expiry(a: Account, plan: str, period: str, now=None):
    """The end date of a payment for `plan`, read from the account row the caller holds locked (so two payments at the
    same moment both count: the second one starts from the first one's end)."""
    days = PERIODS.get(period or "monthly", PERIODS["monthly"])[1]
    if days is None:
        return None  # one-time: the plan does not end
    from . import acl

    now = now or utcnow()
    same = (a.paid_plan or "").lower() == plan.lower() and (acl._plan_of(a) or "").lower() == plan.lower()
    start = a.plan_expires if same and a.plan_expires and a.plan_expires > now else now  # renewing adds to what is left
    return start + timedelta(days=days)


STALE = 60  # seconds: a "paid" row this old without the applied mark was cut off half way (a running apply takes < 1 s)
MARK_KEY = "payments_applied_mark"  # owner setting: rows from before the mark existed were marked once
_marked = [False]


def mark_old_applied():
    """Once per server: every payment that was already "paid" before the applied mark existed was applied by the old
    code — mark it, so recover_unapplied() never puts it on a second time."""
    if _marked[0]:
        return
    try:
        if _owner_get(MARK_KEY):
            _marked[0] = True
            return
        with CoreSession() as s:
            s.execute(update(Payment).where(Payment.status == "paid", Payment.applied_at.is_(None))
                      .values(applied_at=Payment.paid_at))
            s.execute(update(Payment).where(Payment.status == "paid", Payment.applied_at.is_(None))
                      .values(applied_at=Payment.created_at))
            s.commit()
        _owner_set(MARK_KEY, utcnow().isoformat())
        _marked[0] = True
    except Exception:  # noqa: BLE001  (tried again on the next call)
        log.exception("marking old payments failed")


def mark_paid(order_id: str, payment_id: str, via: str) -> dict:
    """Mark an order paid and put its plan on the account — once, however often this is called. A repeated call
    (Razorpay retries its webhook) finishes a payment whose plan was not put on yet (the server stopped half way)."""
    mark_old_applied()
    now = utcnow()
    with CoreSession() as s:
        row = s.query(Payment).filter(Payment.order_id == order_id).first()
    if row is not None and row.status != "paid" and row.mode == "test" and mode() != "test":
        # made while test keys were saved, paid with a test card after going live: no real money, so no plan
        with CoreSession() as s:
            res = s.execute(update(Payment).where(Payment.order_id == order_id, Payment.status.notin_(["paid", "refused"]))
                            .values(status="refused", payment_id=payment_id or None, via=via,
                                    note="test-mode order paid after live keys were saved - no plan was put on"))
            s.commit()
        if res.rowcount == 1:
            _tell_owner(f"⚠️ A TEST payment (order {order_id}) arrived after you went live. No real money was paid, so no "
                        "plan was put on. Switch off the test-mode webhook in Razorpay.")
        return {"ok": True, "applied": False, "ignored": "test order in live mode"}
    with CoreSession() as s:
        res = s.execute(update(Payment).where(Payment.order_id == order_id, Payment.status.notin_(["paid", "refused"]))
                        .values(status="paid", payment_id=payment_id or None, paid_at=now, via=via))
        s.commit()
        row = s.query(Payment).filter(Payment.order_id == order_id).first()
    if res.rowcount != 1:
        if row is not None and row.status == "paid" and row.applied_at is None and row.paid_at \
                and row.paid_at <= now - timedelta(seconds=STALE):
            return _apply_row(row, late=True)  # paid earlier, but the plan never went on: only now, once
        return {"ok": True, "already": True, "plan": row.plan if row else None}
    return _apply_row(row)


def _handled(row_id: int, note: str) -> bool:
    """Mark a paid row as handled without a plan (the owner is told to act by hand). True only for the one call that
    marked it, so the owner is told once."""
    with CoreSession() as s:
        res = s.execute(update(Payment).where(Payment.id == row_id, Payment.applied_at.is_(None))
                        .values(applied_at=utcnow(), note=note[:300]))
        s.commit()
    return res.rowcount == 1


def _apply_row(row: "Payment", late: bool = False) -> dict:
    a = _account(row.account_id)
    if not a:
        if _handled(row.id, "the account is gone — refund it in Razorpay"):
            _tell_owner(f"💰 {inr(row.amount / 100)} received for an account that no longer exists (order {row.order_id}). "
                        "Refund it in Razorpay.")
        return {"ok": True, "applied": False}
    test = "[TEST — no real money] " if row.mode == "test" else ""
    return _apply_paid(a, row.plan, row.period, row.amount, test, "online", payment_row=row.id, late=late)


def _apply_paid(a: Account, plan: str, period: str, amount: int, test: str, how: str, payment_row: int | None = None,
                manual: dict | None = None, late: bool = False) -> dict:
    """Put a paid plan on an account (online payment or a UPI payment the Super Admin allowed): what the Super Admin
    restricted stays, the end date and the channel limit to give back at the end are remembered, the open request is
    answered, everyone is told.
    One transaction, one account at a time: the plan, the end date, the payment's "applied" mark (or, for a UPI payment,
    its payments row) are saved together, read from the account row as it is at that moment — so a crash leaves either
    nothing or everything, and two payments at the same moment both count."""
    from . import acl

    try:
        pname, change, kept = acl.plan_change(plan, keep_restrictions=True)  # what the Super Admin switched off stays off
    except ValueError:
        if manual is not None:
            raise ValueError(f"The plan “{plan}” no longer exists. Put the customer on a plan by hand (Access).")
        if payment_row is None or _handled(payment_row, "the plan was deleted before the payment — put the customer on a plan by hand"):
            _tell_owner(f"💰 {test}{inr(amount / 100)} received from {_e(a.brand or a.name)} ({_e(a.email)}), but the plan “{_e(plan)}” "
                        "no longer exists. Put them on a plan by hand (Users → Access).")
        return {"ok": True, "applied": False}
    now = utcnow()
    with acl._write_lock, CoreSession() as s:  # the same lock as every other change of an account's switches
        acc = s.query(Account).filter(Account.id == a.id).with_for_update().first()
        if acc is None:
            return {"ok": True, "applied": False}
        pay = None
        if payment_row is not None:
            pay = s.query(Payment).filter(Payment.id == payment_row).with_for_update().first()
            if pay is None or pay.applied_at is not None:
                return {"ok": True, "already": True, "plan": pname}  # put on by the other confirmation meanwhile
        if manual is not None:  # "Paid — allow": only while the request is still open (a double click finds it answered)
            req = acc.upgrade or {}
            if str(req.get("plan") or "").lower() != str(manual["plan"]).lower():
                raise ValueError("There is no open plan request for this account.")
        expires = _expiry(acc, pname, period, now)
        legacy_owned = acc.paid_plan if acc.paid_plan and acc.plan_expires is None else None  # one-time, saved by older code
        owned = acc.owned_plan or legacy_owned
        # the channel limit given back when this paid period ends: the one from before the FIRST payment of the period
        # (paid_plan stays set until the end is carried out by expire_due, also when the date has passed: a renewal made
        # between the end date and the daily job must not remember the PAID channel limit as the one to give back)
        running = bool(acc.paid_plan and acc.plan_expires is not None)
        before = acc.limit_before_pay if running and acc.limit_before_pay else (acc.channel_limit or 1)
        acl.apply_in(acc, change)
        if expires is None:  # one-time: the account owns this plan; nothing ends
            acc.owned_plan = pname
            acc.plan_expires = acc.expiry_warned = acc.paid_plan = acc.limit_before_pay = None
        else:
            acc.owned_plan = owned  # kept: the plan the account falls back to when this period ends
            acc.plan_expires, acc.expiry_warned = expires, None
            acc.paid_plan, acc.limit_before_pay = pname, before
        if acc.upgrade:
            acc.upgrade = None  # an open "I have paid" request is answered by this payment
        if pay is not None:
            pay.applied_at = now
        if manual is not None:
            s.add(Payment(account_id=acc.id, plan=pname, amount=amount, currency="INR", period=period,
                          order_id=f"manual_{acc.id}_{int(now.timestamp() * 1000)}", status="paid", via="manual",
                          note=manual["note"][:300], created_at=now, paid_at=now, applied_at=now))
        s.commit()
    acl.invalidate()
    acl.record_plan(a.id, pname, kept, True, a.tenant_id)
    until = f" until {expires:%d %b %Y}" if expires else ""
    acl.record(a.id, "system" if how == "online" else "super", "payment",
               f"{test}Paid {inr(amount / 100)} {how} — plan “{pname}”{until}", a.tenant_id)
    if how == "online":
        _tell_owner(f"💰 {test}{inr(amount / 100)} received from {_e(a.brand or a.name)} — plan “{_e(pname)}” applied{until}."
                    + (" (Applied late: the server stopped before it was finished — nothing to do.)" if late else ""))
    _tell_customer(a, f"✅ Payment received: {inr(amount / 100)}. Your plan “{_e(pname)}” is active{until}. Thank you!")
    return {"ok": True, "applied": True, "plan": pname, "expires": expires.isoformat() + "Z" if expires else None}


def recover_unapplied() -> dict:
    """Timed (owner's copy, every 10 minutes): a payment that is "paid" but whose plan was never put on (the server
    stopped between the two steps) is put on now — once, the owner is told once. Safe to repeat."""
    mark_old_applied()
    cut = utcnow() - timedelta(seconds=STALE)
    with CoreSession() as s:
        rows = s.query(Payment).filter(Payment.status == "paid", Payment.applied_at.is_(None), Payment.paid_at <= cut).all()
    done = 0
    for row in rows:
        try:
            if _apply_row(row, late=True).get("applied"):
                done += 1
        except Exception:  # noqa: BLE001  (one payment's problem must not stop the others)
            log.exception("late payment %s", row.order_id)
    return {"applied": done, "seen": len(rows)}


def allow_manual(account_id: int, expected: str | None = None) -> dict:
    """The Super Admin checked a customer's UPI payment for the plan they asked for ("I have paid ₹X"): the plan is put
    on exactly like an online payment (restrictions kept, ends on time) and a row is added to the payments list.
    A double click: the second press finds the request answered (one row, one set of messages)."""
    a = _account(account_id)
    req = (a.upgrade or {}) if a else {}
    if not a or not req.get("plan"):
        raise ValueError("There is no open plan request for this account.")
    if expected and str(req["plan"]).lower() != str(expected).lower():
        raise ValueError(f"The customer changed the request to the plan “{req['plan']}” meanwhile. Check the payment for "
                         "that plan, then press again.")
    from . import acl

    name = acl._plan_name(acl.config(), req["plan"])
    if not name:
        raise ValueError(f"The plan “{req['plan']}” no longer exists. Put the customer on a plan by hand (Access).")
    pr = price_of(name) or {}
    period = req.get("period") or pr.get("period") or "monthly"
    amount = int(req.get("price") or pr.get("price") or 0) * 100
    out = _apply_paid(a, name, period, amount, "", "by UPI",
                      manual={"plan": req["plan"], "note": "UPI: " + (req.get("message") or "no reference")})
    if not out.get("applied"):
        raise ValueError("The plan could not be put on. Put the customer on a plan by hand (Access).")
    return out


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
             "via": r.via or "", "note": r.note or "", "mode": r.mode or "", "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
             "paid_at": r.paid_at.isoformat() + "Z" if r.paid_at else None,
             "applied": bool(r.applied_at) if r.status == "paid" else None} for r in rows]


# ---------------------------------------------------------------- plans that end (daily, once, as the owner)
def _free_plan() -> str:
    """Where an account goes when its paid plan ends: the plan new accounts start on, unless that one costs money."""
    from . import acl

    start = acl.new_account_plan()
    return "" if not start or price_of(start) else start


def _clear_paid(s, acc: Account):
    acc.plan_expires = acc.expiry_warned = acc.paid_plan = acc.limit_before_pay = None


def taken_over(account_id: int):
    """The Super Admin put the account on a plan by hand: a running paid plan no longer ends by itself."""
    with CoreSession() as s:
        acc = s.get(Account, int(account_id))
        if acc:
            _clear_paid(s, acc)
            acc.limit_by_hand = False  # the plan they chose decides the channel limit
            s.commit()


def limit_set_by_hand(account_id: int):
    """The Super Admin changed the channel limit by hand: a paid plan's end leaves it exactly as they set it."""
    with CoreSession() as s:
        acc = s.get(Account, int(account_id))
        if acc:
            acc.limit_by_hand = True
            s.commit()


def expire_due() -> dict:
    """Warn 3 days before a paid plan ends; after the end put the account back on the free plan and give back the
    channel limit it had before paying (a limit the Super Admin set by hand stays as they set it).
    Acts only while the account is still on the plan that was paid for. Nothing is deleted: channels over the limit
    stay connected, their uploads just wait (acl.over_cap_channels). What the Super Admin switched off stays off."""
    from . import acl

    now, warned, ended = utcnow(), 0, 0
    with CoreSession() as s:
        rows = [(a.id, a.plan_expires, a.expiry_warned) for a in
                s.query(Account).filter(Account.plan_expires.isnot(None), Account.status == "active")]
    for aid, exp, warned_for in rows:
        try:
            a = _account(aid)
            if a is None or a.plan_expires is None:
                continue  # ended or taken over meanwhile
            exp, warned_for = a.plan_expires, a.expiry_warned  # the fresh row: a payment may have arrived since the list was read
            old = acl._plan_of(a)
            if not a.paid_plan or old.lower() != a.paid_plan.lower():
                with CoreSession() as s:  # the account is on another plan now (changed by hand): not ours to end
                    _clear_paid(s, s.get(Account, aid))
                    s.commit()
                continue
            if exp <= now:
                # back to the one-time plan the account owns (bought before this monthly / yearly one), else the free plan
                owned = acl._plan_name(acl.config(), a.owned_plan) if a.owned_plan else None
                target = owned or _free_plan()
                pname, change, kept = acl.plan_change(target, keep_restrictions=True)
                with acl._write_lock, CoreSession() as s:  # one transaction, the same lock as a payment
                    acc = s.query(Account).filter(Account.id == aid).with_for_update().first()
                    if acc is None or acc.plan_expires != exp or not acc.paid_plan                             or acl._plan_of(acc).lower() != acc.paid_plan.lower():
                        continue  # renewed (or changed by hand) this very moment: the new state stands
                    by_hand, before, lim_now = bool(acc.limit_by_hand), acc.limit_before_pay or 1, acc.channel_limit
                    acl.apply_in(acc, change)
                    if by_hand:
                        acc.channel_limit, acc.limit_by_hand = lim_now, True  # exactly as the Super Admin set it
                    else:
                        acc.channel_limit = before
                    _clear_paid(s, acc)
                    s.commit()
                    lim = acc.channel_limit or 1
                acl.invalidate()
                acl.record_plan(aid, pname, kept, True, a.tenant_id)
                where = f"the plan “{owned}” you own" if owned else ("the plan “" + pname + "”" if pname else "the free switches")
                acl.record(aid, "system", "payment", f"The plan “{old}” ended — back to {where} with {lim} channel(s)", a.tenant_id)
                back = f"back on your plan “{_e(owned)}”" if owned else "back on the free plan"
                _tell_customer(a, f"⏰ Your plan “{_e(old)}” has ended. Your account is {back} with {lim} YouTube "
                                  f"channel{'' if lim == 1 else 's'}. Nothing was deleted — renew on My channels → Your plan to switch "
                                  "everything on again.")
                _tell_owner(f"⏰ The plan “{_e(old)}” of {_e(a.brand or a.name)} ({_e(a.email)}) ended — moved {back.replace('your', 'their')}.")
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

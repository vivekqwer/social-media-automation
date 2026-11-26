"""Razorpay gaps: a one-time plan is kept under a later monthly plan, paid-but-not-applied after a crash is finished
once (webhook retry / timed job), two payments at the same moment both count, a double click on 'Paid - allow' gives
one row and one set of messages, and the Razorpay card shows the last webhook."""
import hashlib, hmac, json, os, sys, tempfile, threading, time
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = r"D:\integ-final"
sys.path.insert(0, WT)
from fastapi.testclient import TestClient
from app import main, acl, accounts, tenant, auth, telegram, jobs, payments
from app.accounts import Account, CoreSession
from app.payments import Payment
from app.db import SessionLocal, Destination, set_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
told = []
telegram.notify = lambda text, *a, **k: told.append((tenant.current(), text))
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
KID, SECRET, WH = "rzp_test_FAKEKEY1234", "fake-key-secret-for-tests", "fake-webhook-secret-xyz"


class R:
    def __init__(self, code, data=None):
        self.status_code, self._d, self.text = code, data or {}, json.dumps(data or {})

    def json(self):
        return self._d


n_order = [0]


def fake_post(url, json=None, auth=None, timeout=None, **k):  # no real network: Razorpay is faked
    n_order[0] += 1
    return R(200, {"id": f"order_GAP{n_order[0]:06d}", "amount": json["amount"], "currency": "INR", "status": "created"})


payments.httpx.post = fake_post
payments.httpx.get = lambda url, **k: R(200, {"items": []})
sign = lambda secret, msg: hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)


def event(oid, pid, amount):
    return json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": pid, "order_id": oid, "amount": amount, "currency": "INR", "status": "captured"}}}}).encode()


def row_of(oid):
    with CoreSession() as s:
        return s.query(Payment).filter(Payment.order_id == oid).first()


def age(oid, seconds=120):  # the payment was marked paid that long ago
    with CoreSession() as s:
        s.query(Payment).filter(Payment.order_id == oid).first().paid_at = utcnow() - timedelta(seconds=seconds); s.commit()


def end_now(aid):
    with CoreSession() as s:
        s.get(Account, aid).plan_expires = utcnow() - timedelta(minutes=1); s.commit()


acc = lambda aid: accounts.get(aid)
with mk() as owner, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    clients = []

    def customer(email, name):
        c = mk(); c.__enter__(); clients.append(c)
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email, "password": PW, "accept_terms": True})
        aid = next(u for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email)["id"]
        tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        c.post("/api/login", json={"username": email, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
            with SessionLocal() as s:
                s.add(Destination(channel_id="UC-" + name, name=name + " main", token="x")); s.commit()
        return c, aid, tid

    owner.post("/api/admin/plans", json={"name": "Free", "limits": {"channels": 1}, "perms": {}})
    owner.post("/api/admin/plans", json={"name": "Pro", "limits": {"channels": 5}, "perms": {}, "price": 499, "period": "monthly"})
    owner.post("/api/admin/plans", json={"name": "Life", "limits": {"channels": 3}, "perms": {}, "price": 2999, "period": "once"})
    owner.post("/api/admin/acl/default", json={"perms": {}, "limits": {}, "new_plan": "Free", "apply": True})
    owner.post("/api/admin/razorpay", json={"key_id": KID, "key_secret": SECRET, "webhook_secret": WH})

    # ---------- 5. the Razorpay card: last webhook received
    k = owner.get("/api/admin/razorpay").json()
    ok("last_webhook" in k and k["last_webhook"] is None, "no webhook yet: the card says none")

    def pay(c, plan, tag):
        o = c.post("/api/pay/order", json={"plan": plan}).json()
        body = event(o["order_id"], f"pay_{tag}{o['order_id'][-6:]}", o["amount"])
        return o, anon.post("/api/pay/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": sign(WH, body)})

    c1, a1, t1 = customer("asha@example.com", "Asha")
    o, r = pay(c1, "Life", "A")
    lw = owner.get("/api/admin/razorpay").json()["last_webhook"]
    ok(r.status_code == 200 and lw and lw["ok"] is True and lw["at"].endswith("Z"), f"a good webhook: 'ok' with its time ({lw})")
    bad = event(o["order_id"], "pay_X", 1)
    anon.post("/api/pay/razorpay/webhook", content=bad, headers={"X-Razorpay-Signature": sign(b"other-secret".decode(), bad)})
    lw = owner.get("/api/admin/razorpay").json()["last_webhook"]
    ok(lw["ok"] is False and "wrong signature" in lw["reason"] and WH not in json.dumps(lw), f"a webhook with the wrong secret: 'failed: wrong signature' ({lw})")
    for _ in range(5):
        anon.post("/api/pay/razorpay/webhook", content=bad, headers={"X-Razorpay-Signature": "00"})
    ok(owner.get("/api/admin/razorpay").json()["last_webhook"]["at"] == lw["at"], "a flood of wrong calls is written at most once a minute")
    html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
    ok('id="rz-wh-last"' in html and "Last webhook received:" in html and "'failed: '+esc(lw.reason" in html, "the Razorpay card has the 'Last webhook received' line")

    # ---------- 1. a one-time plan is kept under a later monthly plan
    a = acc(a1)
    ok(acl.effective(a1)["plan"] == "Life" and a.channel_limit == 3 and a.owned_plan == "Life" and a.plan_expires is None and a.paid_plan is None,
       f"Life (one-time): on, 3 channels, owned, never ends ({a.owned_plan}, {a.paid_plan})")
    ok(c1.get("/api/plans").json().get("owned") == "Life", "the Plans card knows the plan the account owns")
    pay(c1, "Pro", "B")
    a = acc(a1)
    ok(acl.effective(a1)["plan"] == "Pro" and a.channel_limit == 5 and a.owned_plan == "Life" and a.paid_plan == "Pro" and a.limit_before_pay == 3,
       f"then Pro monthly: 5 channels; Life still owned; 3 channels to give back ({a.limit_before_pay})")
    pay(c1, "Pro", "C")
    ok(acc(a1).limit_before_pay == 3 and acc(a1).owned_plan == "Life", "renewing Pro keeps both")
    end_now(a1)
    told.clear()
    payments.expire_due()
    a = acc(a1)
    ok(acl.effective(a1)["plan"] == "Life" and a.channel_limit == 3 and a.owned_plan == "Life" and a.paid_plan is None and a.plan_expires is None,
       f"Pro ends: back on Life with 3 channels, not the free plan ({acl.effective(a1)['plan']}, {a.channel_limit})")
    ok(any(t == t1 and "back on your plan “Life”" in x for t, x in told), "the customer is told they are back on their own plan")
    ok(payments.expire_due()["ended"] == 0 and acl.effective(a1)["plan"] == "Life", "the job again: nothing more happens (Life never ends)")

    # an account that bought the one-time plan with the old code (paid_plan set, no end date, no owned_plan)
    c2, a2, t2 = customer("ravi@example.com", "Ravi")
    acl.apply_plan(a2, "Life", keep_restrictions=True)
    with CoreSession() as s:
        x = s.get(Account, a2); x.paid_plan, x.plan_expires, x.limit_before_pay, x.owned_plan = "Life", None, 1, None; s.commit()
    pay(c2, "Pro", "D")
    ok(acc(a2).owned_plan == "Life" and acc(a2).limit_before_pay == 3, f"old one-time purchase: recognised when Pro is bought ({acc(a2).owned_plan}, {acc(a2).limit_before_pay})")
    end_now(a2); payments.expire_due()
    ok(acl.effective(a2)["plan"] == "Life" and acc(a2).channel_limit == 3, "…and Pro's end goes back to Life with 3 channels")

    # no one-time plan: the free plan with the limit from before, as before
    c3, a3, t3 = customer("mira@example.com", "Mira")
    pay(c3, "Pro", "E")
    end_now(a3); payments.expire_due()
    ok(acl.effective(a3)["plan"] == "Free" and acc(a3).channel_limit == 1 and acc(a3).owned_plan is None, "no one-time plan owned: back on the free plan with 1 channel")
    # Pro first, Life bought during it: Life is on for good, nothing ends any more
    pay(c3, "Pro", "F")
    pay(c3, "Life", "G")
    a = acc(a3)
    ok(acl.effective(a3)["plan"] == "Life" and a.plan_expires is None and a.paid_plan is None and a.owned_plan == "Life" and a.channel_limit == 3,
       "Life bought while Pro runs: Life is on, nothing ends")
    ok(payments.expire_due()["ended"] == 0 and acl.effective(a3)["plan"] == "Life", "…and the daily job leaves it")

    # ---------- 2. paid but not applied after a crash
    c4, a4, t4 = customer("neel@example.com", "Neel")
    real_apply_in = acl.apply_in

    def crash(*a, **k):
        raise RuntimeError("power cut")

    o4 = c4.post("/api/pay/order", json={"plan": "Pro"}).json()
    body = event(o4["order_id"], "pay_CRASH0001", 49900)
    acl.apply_in = crash
    try:
        payments.webhook(body, sign(WH, body))
        crashed = False
    except RuntimeError:
        crashed = True
    acl.apply_in = real_apply_in
    row = row_of(o4["order_id"])
    ok(crashed and row.status == "paid" and row.applied_at is None, "a crash between 'paid' and the plan: the row is paid, not applied")
    ok(acl.effective(a4)["plan"] == "Free" and acc(a4).plan_expires is None and acc(a4).paid_plan is None,
       "…and nothing half-done on the account (plan, end date and mark are one transaction)")
    told.clear()
    r = payments.webhook(body, sign(WH, body))
    ok(r.get("already") and row_of(o4["order_id"]).applied_at is None, "a retry within a minute: left to the running apply (not applied twice)")
    age(o4["order_id"])
    r = payments.webhook(body, sign(WH, body))
    a = acc(a4)
    ok(r.get("applied") and acl.effective(a4)["plan"] == "Pro" and a.channel_limit == 5 and row_of(o4["order_id"]).applied_at is not None
       and abs((a.plan_expires - utcnow()) - timedelta(days=30)) < timedelta(minutes=5), "Razorpay's webhook retry: the plan is put on now, 30 days")
    late = [x for t, x in told if t == tenant.OWNER and "Applied late" in x]
    ok(len(late) == 1 and sum(1 for t, x in told if t == t4 and "Payment received" in x) == 1, "the owner is told once (applied late), the customer once")
    exp = acc(a4).plan_expires
    r = payments.webhook(body, sign(WH, body))
    ok(r.get("already") and acc(a4).plan_expires == exp and len([x for t, x in told if "Applied late" in x]) == 1, "another retry: nothing changes, no new message")
    ok(payments.recover_unapplied() == {"applied": 0, "seen": 0}, "the timed job finds nothing to do")

    # the timed job finishes a crashed renewal (the webhook never comes back)
    o5 = c4.post("/api/pay/order", json={"plan": "Pro"}).json()
    body5 = event(o5["order_id"], "pay_CRASH0002", 49900)
    acl.apply_in = crash
    try:
        payments.webhook(body5, sign(WH, body5))
    except RuntimeError:
        pass
    acl.apply_in = real_apply_in
    ok(payments.recover_unapplied()["applied"] == 0 and acc(a4).plan_expires == exp, "the timed job waits a minute (a running apply is not raced)")
    age(o5["order_id"])
    told.clear()
    r = payments.recover_unapplied()
    ok(r == {"applied": 1, "seen": 1} and abs((acc(a4).plan_expires - exp) - timedelta(days=30)) < timedelta(minutes=1),
       f"the timed job puts the crashed renewal on: +30 days from the current end ({r})")
    ok(len([x for t, x in told if t == tenant.OWNER and "Applied late" in x]) == 1, "…the owner is told once")
    ok(payments.recover_unapplied()["applied"] == 0 and len([x for t, x in told if "Applied late" in x]) == 1, "…and only once")
    js = owner.get("/api/admin/payments").json()["payments"]
    ok(all(p["applied"] is True for p in js if p["status"] == "paid"), "the payments list says every paid row is applied")
    # rows paid before the mark existed are never applied again
    with CoreSession() as s:
        s.add(Payment(account_id=a4, plan="Pro", amount=49900, currency="INR", period="monthly", order_id="order_OLDROW0001",
                      status="paid", via="webhook", mode="test", created_at=utcnow() - timedelta(days=40), paid_at=utcnow() - timedelta(days=40)))
        s.commit()
    with tenant.use(tenant.OWNER):
        set_setting(payments.MARK_KEY, "")
    payments._marked[0] = False
    payments.mark_old_applied()
    ok(row_of("order_OLDROW0001").applied_at is not None and payments.recover_unapplied()["applied"] == 0 and acc(a4).plan_expires and
       abs((acc(a4).plan_expires - exp) - timedelta(days=30)) < timedelta(minutes=1), "payments from before the mark (old code applied them) are marked once, never applied again")

    # ---------- 3. two payments for one account at the same moment: both periods count
    c5, a5, t5 = customer("uma@example.com", "Uma")
    oa = c5.post("/api/pay/order", json={"plan": "Pro"}).json()
    ob = c5.post("/api/pay/order", json={"plan": "Pro"}).json()
    real_exp = payments._expiry

    def slow_expiry(*a, **k):
        out = real_exp(*a, **k)
        time.sleep(0.4)  # both payments would read the same account row here without the lock
        return out

    payments._expiry = slow_expiry
    res = []
    th = [threading.Thread(target=lambda o=o, p=p: res.append(payments.mark_paid(o["order_id"], p, "webhook")))
          for o, p in ((oa, "pay_SAMEA00001"), (ob, "pay_SAMEB00001"))]
    for t in th:
        t.start()
    for t in th:
        t.join()
    payments._expiry = real_exp
    a = acc(a5)
    ok(len(res) == 2 and all(x.get("applied") for x in res), "both payments are applied")
    ok(a.plan_expires and abs((a.plan_expires - utcnow()) - timedelta(days=60)) < timedelta(minutes=5),
       f"two payments in the same moment: 60 days, not 30 ({a.plan_expires - utcnow() if a.plan_expires else None})")
    ok(a.limit_before_pay == 1 and a.channel_limit == 5, f"…and the limit to give back is the one from before the first ({a.limit_before_pay})")

    # ---------- 4. double click on 'Paid — allow' (manual UPI)
    owner.post("/api/admin/razorpay", json={"clear": True})
    c6, a6, t6 = customer("kiran@example.com", "Kiran")
    r = c6.post("/api/account/upgrade", json={"plan": "Pro", "message": "UPI 4455"})
    ok(r.status_code == 200, f"the customer says they paid by UPI ({r.status_code})")

    def slow_apply(*a, **k):
        time.sleep(0.4)
        return real_apply_in(*a, **k)

    acl.apply_in = slow_apply
    told.clear()
    out, errs = [], []

    def press():
        try:
            out.append(payments.allow_manual(a6, "Pro"))
        except ValueError as e:
            errs.append(str(e))

    th = [threading.Thread(target=press) for _ in range(2)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    acl.apply_in = real_apply_in
    rows = [p for p in owner.get("/api/admin/payments").json()["payments"] if p["account_id"] == a6]
    ok(len(out) == 1 and len(errs) == 1 and "no open plan request" in errs[0], f"double click: one press applies, the other finds the request answered ({errs})")
    ok(len(rows) == 1 and rows[0]["via"] == "manual" and rows[0]["applied"] is True and "UPI 4455" in rows[0]["note"], f"…one payments row ({len(rows)})")
    ok(sum(1 for t, x in told if t == t6 and "Payment received" in x) == 1, "…one message to the customer")
    a = acc(a6)
    ok(acl.effective(a6)["plan"] == "Pro" and abs((a.plan_expires - utcnow()) - timedelta(days=30)) < timedelta(minutes=5) and a.upgrade is None,
       "…and one period (30 days)")
    r = owner.post(f"/api/admin/users/{a6}/allow-plan", json={"plan": "Pro"})
    ok(r.status_code == 400 and acc(a6).plan_expires == a.plan_expires, "a third press later: plain 400, nothing applied again")
    for c in clients:
        c.__exit__(None, None, None)
for f in ("payments.py", "acl.py", "weekly.py"):
    ok(b"\x08" not in open(os.path.join(WT, "app", f), "rb").read(), f"no backspace byte in app/{f}")

"""Razorpay payments: keys (never shown), plan prices, order amount from the server, signature checks, webhook
(idempotent), plan + expiry applied, renewal extends, daily expiry job, channel cap, Super Admin only routes."""
import hashlib, hmac, json, os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = r"D:\integ-final"
sys.path.insert(0, WT)
from fastapi.testclient import TestClient
from app import main, acl, accounts, tenant, auth, telegram, jobs, payments
from app.accounts import Account, CoreSession
from app.db import SessionLocal, Destination, set_setting, get_setting, utcnow
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


calls, n_order = [], [0]
get_code = [200]
PAYS = {}  # payment id -> what Razorpay says about it (GET /v1/payments/{id})
down = [False]  # Razorpay unreachable for payment look-ups


def fake_post(url, json=None, auth=None, timeout=None, **k):
    calls.append(("POST", url, json, auth))
    if "/payments/" in url and url.endswith("/capture"):
        pid = url.split("/payments/")[1].split("/")[0]
        p = PAYS[pid]
        if json["amount"] != p["amount"]:
            return R(400, {"error": "amount"})
        p["status"] = "captured"
        return R(200, dict(p))
    n_order[0] += 1
    return R(200, {"id": f"order_TEST{n_order[0]:06d}", "amount": json["amount"], "currency": "INR", "status": "created"})


def fake_get(url, params=None, auth=None, timeout=None, **k):
    calls.append(("GET", url, params, auth))
    if "/payments/" in url:
        if down[0]:
            raise payments.httpx.ConnectError("down")
        pid = url.rsplit("/", 1)[1]
        return R(200, dict(PAYS[pid])) if pid in PAYS else R(400, {"error": "not found"})
    return R(get_code[0], {"items": []})


def razorpay_has(pid, oid, amount=49900, status="captured", currency="INR"):
    PAYS[pid] = {"id": pid, "order_id": oid, "amount": amount, "currency": currency, "status": status}


payments.httpx.post, payments.httpx.get = fake_post, fake_get
sign = lambda secret, msg: hmac.new(secret.encode(), msg.encode() if isinstance(msg, str) else msg, hashlib.sha256).hexdigest()
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
with mk() as owner, mk() as c1, mk() as c2, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})

    def customer(client, email, name):
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email, "password": PW, "accept_terms": True})
        aid = next(u for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email)["id"]
        tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        client.post("/api/login", json={"username": email, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
            with SessionLocal() as s:
                s.add(Destination(channel_id="UC-" + name, name=name + " main", token="x")); s.add(Destination(channel_id="UC2-" + name, name=name + " second", token="x")); s.commit()
        return aid, tid

    a1, t1 = customer(c1, "asha@example.com", "Asha")
    a2, t2 = customer(c2, "ravi@example.com", "Ravi")
    acc = lambda aid: accounts.get(aid)

    # ---------- plan prices (Super Admin, on the existing plan editor)
    r = owner.post("/api/admin/plans", json={"name": "Pro", "limits": {"channels": 3, "clips_per_day": 20, "storage_mb": 2048}, "perms": {},
                                              "price": 499, "period": "monthly", "description": "For growing channels"})
    ok(r.status_code == 200 and next(p for p in r.json()["plans"] if p["name"] == "Pro")["price"] == {"price": 499, "period": "monthly", "description": "For growing channels"}, "a plan gets a price, period and description")
    owner.post("/api/admin/plans", json={"name": "Free", "limits": {"channels": 1}, "perms": {}})
    owner.post("/api/admin/plans", json={"name": "Lifetime", "limits": {"channels": 5}, "perms": {}, "price": 9999, "period": "once"})
    ok(owner.post("/api/admin/plans", json={"name": "Bad", "perms": {}, "limits": {}, "price": -5}).status_code == 422
       and owner.post("/api/admin/plans", json={"name": "Bad", "perms": {}, "limits": {}, "price": 5, "period": "weekly"}).status_code == 422, "bad price / period refused")
    p = c1.get("/api/plans").json()
    ok([x["name"] for x in p["plans"]] == ["Pro", "Lifetime"] and p["online"] is False, "customers see only plans for sale (cheapest first); no keys = not online")
    pro = p["plans"][0]
    ok(pro["includes"] == {"channels": 3, "sources": None, "clips_per_day": 20, "uploads_per_day": None, "storage_mb": 2048} and pro["price"] == 499 and pro["period_text"] == "per month", f"what a plan includes comes from its limits ({pro['includes']})")
    ok("pay" in p, "the manual UPI instructions come along (shown when online payment is not set up)")
    ok(c1.post("/api/pay/order", json={"plan": "Pro"}).status_code == 400, "no keys: Pay is refused (the UPI steps are used)")

    # ---------- keys
    ok(owner.post("/api/admin/razorpay", json={"key_id": "pk_live_nope"}).status_code == 400, "a key id that is not rzp_test_/rzp_live_ is refused")
    r = owner.post("/api/admin/razorpay", json={"key_id": KID, "key_secret": SECRET, "webhook_secret": WH})
    k = r.json()
    ok(r.status_code == 200 and k["key_end"] == "1234" and k["secret_set"] and k["webhook_set"] and k["mode"] == "test" and k["enabled"], "keys saved: only 'saved', the last 4 characters and test mode come back")
    txt = owner.get("/api/admin/razorpay").text + owner.get("/api/admin/acl").text + owner.get("/api/admin/users").text + c1.get("/api/plans").text + c1.get("/api/me").text
    ok(SECRET not in txt and WH not in txt and KID not in owner.get("/api/admin/razorpay").text, "no secret (and not the whole key id) is ever sent to the Super Admin's browser")
    with tenant.use(tenant.OWNER):
        raw = get_setting("razorpay")
    ok(SECRET not in raw and WH not in raw and "enc:v1:" in raw, "the secrets are stored encrypted")
    ok(owner.get("/api/admin/razorpay").json()["webhook_url"].endswith("/api/pay/razorpay/webhook"), "the webhook address is shown")
    owner.post("/api/admin/razorpay", json={"key_id": KID})
    ok(payments._keys() == (KID, SECRET, WH), "saving with empty secret fields keeps the saved secrets")
    ok(owner.post("/api/admin/razorpay", json={"key_id": "rzp_live_OTHERKEY99"}).status_code == 400, "switching test -> live without the new secret is refused")
    get_code[0] = 200
    ok(owner.post("/api/admin/razorpay/check").json()["ok"] is True and calls[-1][1] == "https://api.razorpay.com/v1/orders" and calls[-1][2] == {"count": 1} and calls[-1][3] == (KID, SECRET), "Check keys: one read of Razorpay's orders with the keys -> OK")
    get_code[0] = 401
    r = owner.post("/api/admin/razorpay/check").json()
    ok(r["ok"] is False and "wrong" in r["message"], "Check keys: wrong keys are reported")

    # ---------- customers cannot reach the Super Admin's payment routes
    ok(all(c1.request(m, u, json={} if m == "POST" else None).status_code == 403 for m, u in
           [("GET", "/api/admin/razorpay"), ("POST", "/api/admin/razorpay"), ("POST", "/api/admin/razorpay/check"), ("GET", "/api/admin/payments")]), "customers get 403 on keys / check / payments list")
    ok(acl.check_request("GET", "/api/admin/payments", {"role": "user", "account_id": a1, "tenant": t1}) == "Only the Super Admin can do this.", "…in the access table too")
    ok(owner.post("/api/pay/order", json={"plan": "Pro"}).status_code == 400, "the Super Admin cannot pay (only customers)")

    # ---------- order: the amount comes from the server
    r = c1.post("/api/pay/order", json={"plan": "pro", "amount": 1, "price": 1})
    o = r.json()
    ok(r.status_code == 200 and calls[-1][2]["amount"] == 49900 and o["amount"] == 49900 and calls[-1][2]["currency"] == "INR", "order amount = saved price x 100 (paise), whatever the browser sends")
    ok(calls[-1][2]["notes"] == {"account_id": str(a1), "plan": "Pro", "email": "asha@example.com"} and o["key_id"] == KID and calls[-1][3] == (KID, SECRET), "order notes: account id + plan; Checkout gets the (public) key id")
    ok(c1.post("/api/pay/order", json={"plan": "Free"}).status_code == 400 and c1.post("/api/pay/order", json={"plan": "Gold"}).status_code == 400, "plans not for sale / unknown cannot be ordered")
    with CoreSession() as s:
        row = s.query(payments.Payment).filter_by(order_id=o["order_id"]).first()
    ok(row and row.status == "created" and row.account_id == a1 and row.amount == 49900 and row.plan == "Pro", "a payments row is stored (created)")

    # ---------- checkout signature
    oid, pid = o["order_id"], "pay_TESTAAA0001"
    bad = c1.post("/api/pay/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": pid, "razorpay_signature": sign("wrong-secret", f"{oid}|{pid}")})
    ok(bad.status_code == 400 and acc(a1).plan_expires is None and acl.effective(a1)["plan"] == "", "a wrong signature is refused; nothing applied")
    good = {"razorpay_order_id": oid, "razorpay_payment_id": pid, "razorpay_signature": sign(SECRET, f"{oid}|{pid}")}
    ok(c2.post("/api/pay/verify", json=good).status_code == 400, "another customer cannot confirm someone else's order")
    # (review 3) a good signature alone is not enough: Razorpay is asked whether the money is really captured
    down[0] = True
    r = c1.post("/api/pay/verify", json=good)
    with CoreSession() as s:
        st = s.query(payments.Payment).filter_by(order_id=oid).first().status
    ok(r.status_code == 200 and r.json().get("pending") and "switch on in a minute" in r.json()["message"] and st == "created"
       and acc(a1).plan_expires is None and acl.effective(a1)["plan"] == "", "Razorpay unreachable: row stays 'created', nothing applied, the customer is told it switches on in a minute")
    down[0] = False
    for bad_pay, why in (({"status": "failed"}, "a failed payment"), ({"order_id": "order_OTHER0001"}, "a payment of another order"),
                         ({"amount": 100}, "a different amount"), ({"currency": "USD"}, "another currency")):
        razorpay_has(pid, oid); PAYS[pid].update(bad_pay)
        r = c1.post("/api/pay/verify", json=good)
        ok(r.status_code == 400 and acl.effective(a1)["plan"] == "", f"checkout with a valid signature but {why}: refused, nothing applied")
    razorpay_has(pid, oid, status="authorized")
    told.clear()
    r = c1.post("/api/pay/verify", json=good)
    ok(any(c[0] == "POST" and c[1].endswith(f"/payments/{pid}/capture") and c[2] == {"amount": 49900, "currency": "INR"} for c in calls),
       "an 'authorized' payment is captured for the order's amount before the plan is applied")
    a = acc(a1)
    exp1 = a.plan_expires
    ok(r.status_code == 200 and r.json()["applied"] and acl.effective(a1)["plan"] == "Pro" and a.channel_limit == 3, "a good signature: paid, plan + channel limit applied")
    ok(exp1 and abs((exp1 - utcnow()) - timedelta(days=30)) < timedelta(minutes=5), "monthly plan ends in 30 days")
    ok(any(t == tenant.OWNER and "₹499 received from Asha Studio" in x and "Pro" in x for t, x in told), "the owner is told (₹ + brand + plan)")
    ok(any(t == tenant.OWNER and "[TEST" in x and "received" in x for t, x in told), "(review 4) test-mode money is tagged TEST in the owner's message")
    ok(any(t == t1 and "Payment received" in x for t, x in told), "the customer is told in their own copy")
    n = len(told)
    r = c1.post("/api/pay/verify", json=good)
    ok(r.status_code == 200 and r.json().get("already") and acc(a1).plan_expires == exp1 and len(told) == n, "the same payment twice: applied once")
    ok(c1.get("/api/plans").json()["current"] == "Pro" and c1.get("/api/plans").json()["expires"], "the Plans card shows the current plan and its end")
    ok(c1.get("/api/me").json()["plan_expires"], "/api/me tells the end date")

    # ---------- webhook (no sign-in, raw body signature, idempotent), renewal extends
    o2 = c1.post("/api/pay/order", json={"plan": "Pro"}).json()

    def event(oid, pid, amount=49900, kind="payment.captured"):
        return json.dumps({"event": kind, "payload": {"payment": {"entity": {"id": pid, "order_id": oid, "amount": amount, "currency": "INR", "status": "captured"}}}}).encode()

    body = event(o2["order_id"], "pay_TESTBBB0002")
    ok(anon.post("/api/pay/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": sign("nope", body)}).status_code == 400, "webhook: a wrong signature is refused")
    ok(anon.post("/api/pay/razorpay/webhook", content=body).status_code == 400, "webhook: no signature is refused")
    with CoreSession() as s:
        ok(s.query(payments.Payment).filter_by(order_id=o2["order_id"]).first().status == "created", "…and nothing changed")
    wrong_amt = event(o2["order_id"], "pay_TESTBBB0002", amount=100)
    r = anon.post("/api/pay/razorpay/webhook", content=wrong_amt, headers={"X-Razorpay-Signature": sign(WH, wrong_amt)})
    ok(r.status_code == 200 and r.json().get("ignored") == "amount differs" and acc(a1).plan_expires == exp1, "webhook: a different amount is not applied")
    r = anon.post("/api/pay/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": sign(WH, body)})
    exp2 = acc(a1).plan_expires
    ok(r.status_code == 200 and r.json()["applied"] and abs((exp2 - exp1) - timedelta(days=30)) < timedelta(minutes=1), "webhook (no sign-in) applies the payment; renewing extends from the current end (+30 days)")
    paid_evt = json.dumps({"event": "order.paid", "payload": {"payment": {"entity": {"id": "pay_TESTBBB0002", "order_id": o2["order_id"], "amount": 49900, "currency": "INR"}},
                                                             "order": {"entity": {"id": o2["order_id"], "amount_paid": 49900}}}}).encode()
    r = anon.post("/api/pay/razorpay/webhook", content=paid_evt, headers={"X-Razorpay-Signature": sign(WH, paid_evt)})
    r2 = anon.post("/api/pay/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": sign(WH, body)})
    r3 = c1.post("/api/pay/verify", json={"razorpay_order_id": o2["order_id"], "razorpay_payment_id": "pay_TESTBBB0002", "razorpay_signature": sign(SECRET, o2["order_id"] + "|pay_TESTBBB0002")})
    ok(r.json().get("already") and r2.json().get("already") and r3.json().get("already") and acc(a1).plan_expires == exp2, "order.paid + repeated webhook + late browser confirmation: applied once")
    other = event("order_NOTOURS", "pay_X")
    ok(anon.post("/api/pay/razorpay/webhook", content=other, headers={"X-Razorpay-Signature": sign(WH, other)}).json().get("ignored") == "unknown order", "webhook: unknown orders are ignored with 200")
    o3 = c2.post("/api/pay/order", json={"plan": "Pro"}).json()
    f = event(o3["order_id"], "pay_FAILED1", kind="payment.failed")
    anon.post("/api/pay/razorpay/webhook", content=f, headers={"X-Razorpay-Signature": sign(WH, f)})
    pays = owner.get("/api/admin/payments").json()["payments"]
    ok(len(pays) == 3 and {x["status"] for x in pays} == {"paid", "failed"} and pays[-1]["name"] == "Asha" and pays[-1]["amount"] == 499 and pays[-1]["plan"] == "Pro", "Super Admin's payments list: who, plan, amount, status, date")
    ok(all(x["mode"] == "test" for x in pays), "(review 4) every payment row says test/live (here: test)")
    hl = owner.get("/api/health").json()
    hl = hl if isinstance(hl, list) else hl.get("items", [])
    ok(any(i.get("key") == "pay_test_mode" and i["level"] == "warn" and "TEST mode" in i["text"] for i in hl), "(review 4) the owner's Problems list warns while test keys are saved")
    hc = c1.get("/api/health").json()
    hc = hc if isinstance(hc, list) else hc.get("items", [])
    ok(not any(i.get("key") == "pay_test_mode" for i in hc), "…customers do not see that warning")
    unc = event(o3["order_id"], "pay_AUTHONLY1", kind="payment.captured").replace(b'"captured"}', b'"authorized"}')
    r = anon.post("/api/pay/razorpay/webhook", content=unc, headers={"X-Razorpay-Signature": sign(WH, unc)})
    ok(r.json().get("ignored") == "not captured" and acl.effective(a2)["plan"] == "", "(review 3) webhook: a payment.captured event whose payment is not captured is not applied")
    for k in ("payment.authorized", "refund.created"):
        e = event(o3["order_id"], "pay_OTHEREVT1", kind=k)
        ok(anon.post("/api/pay/razorpay/webhook", content=e, headers={"X-Razorpay-Signature": sign(WH, e)}).json().get("ignored") == k, f"webhook: {k} does not apply a plan")
    # a one-time plan never ends; a different plan starts from now
    o4 = c2.post("/api/pay/order", json={"plan": "Lifetime"}).json()
    razorpay_has("pay_LIFE00001", o4["order_id"], amount=999900)
    c2.post("/api/pay/verify", json={"razorpay_order_id": o4["order_id"], "razorpay_payment_id": "pay_LIFE00001", "razorpay_signature": sign(SECRET, o4["order_id"] + "|pay_LIFE00001")})
    ok(acl.effective(a2)["plan"] == "Lifetime" and acc(a2).plan_expires is None and acc(a2).channel_limit == 5, "one-time plan: applied, no end date")

    # ---------- channel cap (least destructive): channels beyond the limit only stop uploading
    accounts.update(a2, channel_limit=1)
    with tenant.use(t2), SessionLocal() as s:
        over = acl.over_cap_channels(s)
        names = {x.name for x in s.query(Destination).filter(Destination.id.in_(over))}
    ok(names == {"Ravi second"}, "over the channel limit: only the extra channel's uploads wait (the main one keeps going)")
    with tenant.use(t2):
        items = acl.health_items()
    ok(any(i["key"] == "acl_channels_over" for i in items), "…and the customer's problems list says so")
    with tenant.use(tenant.OWNER), SessionLocal() as s:
        ok(acl.over_cap_channels(s) == set(), "the owner's own copy is never capped")

    # ---------- the daily expiry job
    with CoreSession() as s:
        s.get(Account, a1).plan_expires = utcnow() + timedelta(days=2); s.commit()
    told.clear()
    r = payments.expire_due()
    r2 = payments.expire_due()
    ok(r["warned"] == 1 and r2["warned"] == 0 and any(t == t1 and "ends on" in x for t, x in told), "3 days before the end: the customer is told, once")
    with CoreSession() as s:
        s.get(Account, a1).plan_expires = utcnow() - timedelta(minutes=1); s.commit()
    owner.post("/api/admin/acl/default", json={"perms": {}, "limits": {}, "new_plan": "Free", "apply": True})
    told.clear()
    r = payments.expire_due()
    a = acc(a1)
    with tenant.use(t1), SessionLocal() as s:
        kept = s.query(Destination).count()
    ok(r["ended"] == 1 and acl.effective(a1)["plan"] == "Free" and a.channel_limit == 1 and a.plan_expires is None, "after the end: back to the free plan with 1 channel")
    ok(kept == 2 and a.status == "active", "nothing is deleted (both channels stay connected, the account stays active)")
    ok(any(t == t1 and "has ended" in x for t, x in told) and any(t == tenant.OWNER and "ended" in x for t, x in told), "customer and owner are told")
    ok(payments.expire_due()["ended"] == 0, "the job is safe to run again")

    # ---------- (review 2) a payment never lifts what the Super Admin restricted; a plan put on by hand works as before
    payments._orders.clear()
    owner.post("/api/admin/razorpay", json={"key_id": KID, "key_secret": SECRET, "webhook_secret": WH})  # (unchanged keys)
    with mk() as c3, mk() as c4:
        a3, t3 = customer(c3, "mira@example.com", "Mira")
        a4, t4 = customer(c4, "neel@example.com", "Neel")

        def pay(client, aid, plan="Pro", amount=49900, tag="A"):
            o = client.post("/api/pay/order", json={"plan": plan}).json()
            body = event(o["order_id"], f"pay_REG{tag}{o['order_id'][-6:]}", amount=amount)
            r = anon.post("/api/pay/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": sign(WH, body)}).json()
            return r

        owner.post(f"/api/admin/users/{a3}/access", json={"perms": {"page.viral": False, "source.reel": True},
                                                          "limits": {"sources": 2, "clips_per_day": 50, "storage_mb": 0}})
        owner.post(f"/api/admin/users/{a3}", json={"channel_limit": 2})
        owner.post(f"/api/admin/users/{a3}/control", json={"action": "pause_uploads"})
        r = pay(c3, a3)
        e3, x3 = acl.effective(a3), acl.public_access(a3)
        ok(r.get("applied") and e3["plan"] == "Pro", "restricted customer pays: on the plan")
        ok(e3["perms"]["page.viral"] is False and "source.reel" not in x3["own_perms"], "…a switch the Super Admin set OFF stays off (an own ON switch is left to the plan)")
        ok(e3["limits"]["sources"] == 2 and e3["limits"]["clips_per_day"] == 20 and e3["limits"]["storage_mb"] == 2048,
           f"…own limits stricter than the plan stay, looser ones and 'no limit' give way to the plan ({e3['limits']})")
        ok(acc(a3).channel_limit == 3 and acc(a3).limit_before_pay == 2 and e3["controls"]["uploads_paused"] is True and acc(a3).status == "active",
           "…a channel limit the Super Admin RAISED by hand (1 to 2) is a gift: the paid plan gives its 3; 'uploads paused' stays")
        # expiry: the Super Admin's by-hand channel limit stays exactly as set; what they switched off stays off
        with CoreSession() as s:
            s.get(Account, a3).plan_expires = utcnow() - timedelta(minutes=1); s.commit()
        payments.expire_due()
        e3 = acl.effective(a3)
        ok(e3["plan"] == "Free" and acc(a3).channel_limit == 2 and e3["perms"]["page.viral"] is False and e3["limits"]["sources"] == 2
           and e3["controls"]["uploads_paused"] is True, "(review 1) at the end: free plan, by-hand channel limit (2) kept, restrictions kept")
        # by hand (Super Admin) still clears everything, as before
        owner.post(f"/api/admin/users/{a3}/plan", json={"plan": "Pro"})
        ok(acl.public_access(a3)["own_perms"] == {} and acl.effective(a3)["perms"]["page.viral"] is True, "a plan put on BY HAND still clears the account's own switches")

        # ---------- (review 1) channel limit before paying is given back, not 1
        owner.post("/api/admin/plans", json={"name": "Duo", "limits": {"channels": 2}, "perms": {}})  # not for sale
        owner.post(f"/api/admin/users/{a4}/plan", json={"plan": "Duo"})  # 2 channels before paying
        pay(c4, a4, tag="B")
        ok(acc(a4).channel_limit == 3 and acc(a4).limit_before_pay == 2 and acc(a4).paid_plan == "Pro", "paying raises the limit to the plan's (3); the limit before (2) is remembered")
        pay(c4, a4, tag="C")
        ok(acc(a4).limit_before_pay == 2, "renewing keeps the limit from before the FIRST payment")
        with CoreSession() as s:
            s.get(Account, a4).plan_expires = utcnow() - timedelta(minutes=1); s.commit()
        payments.expire_due()
        ok(acc(a4).channel_limit == 2 and acl.effective(a4)["plan"] == "Free" and acc(a4).paid_plan is None, "at the end the limit goes back to what it was before paying (2), not to 1")
        with tenant.use(t4), SessionLocal() as s:
            ok(s.query(Destination).count() == 2, "…nothing deleted")
        # changed by hand during the paid period -> left exactly as set
        pay(c4, a4, tag="D")
        owner.post(f"/api/admin/users/{a4}", json={"channel_limit": 7})
        with CoreSession() as s:
            s.get(Account, a4).plan_expires = utcnow() - timedelta(minutes=1); s.commit()
        payments.expire_due()
        ok(acc(a4).channel_limit == 7 and acl.effective(a4)["plan"] == "Free", "a limit the Super Admin changed by hand during the paid period stays exactly as set (7)")
        owner.post(f"/api/admin/users/{a4}/access", json={"limits": {"channels": 1}})
        pay(c4, a4, tag="E")
        owner.post(f"/api/admin/users/{a4}/access", json={"limits": {"channels": 6}})  # by hand through the Access panel
        with CoreSession() as s:
            s.get(Account, a4).plan_expires = utcnow() - timedelta(minutes=1); s.commit()
        payments.expire_due()
        ok(acc(a4).channel_limit == 6, "…also when set in the Access panel")
        # the Super Admin puts the account on a plan by hand -> they have taken over; the payment's end no longer acts
        pay(c4, a4, tag="F")
        owner.post(f"/api/admin/users/{a4}/plan", json={"plan": "Pro"})
        a = acc(a4)
        ok(a.plan_expires is None and a.expiry_warned is None and a.paid_plan is None, "a plan put on by hand clears the paid end date (Super Admin took over)")
        ok(payments.expire_due()["ended"] == 0 and acl.effective(a4)["plan"] == "Pro" and acc(a4).channel_limit == 3, "…and nothing is moved back later")
        # the account is on another plan by some other path -> not ended, just forgotten
        pay(c4, a4, tag="G")
        acl.apply_plan(a4, "Free")
        with CoreSession() as s:
            s.get(Account, a4).plan_expires = utcnow() - timedelta(minutes=1); s.commit()
        lim = acc(a4).channel_limit
        r = payments.expire_due()
        ok(r["ended"] == 0 and acl.effective(a4)["plan"] == "Free" and acc(a4).channel_limit == lim and acc(a4).plan_expires is None,
           "expiry acts only while the account is still on the plan that was paid for")

    # ---------- (review 5) the expiry job: an hour of grace after a missed 3:30, and once 5 minutes after start-up
    from app import scheduler as schmod
    jobs_added = []

    class FakeSch:
        def __init__(self, *a, **k): pass
        def add_job(self, fn, *a, **k): jobs_added.append((getattr(fn, "__name__", ""), a, k))
        def start(self): pass
    real = schmod.BackgroundScheduler
    schmod.BackgroundScheduler = FakeSch
    try:
        schmod.start()
    finally:
        schmod.BackgroundScheduler = real
    pe = [j for j in jobs_added if j[0] == "plans_ending"]
    ok(any(j[1][:1] == ("cron",) and j[2].get("misfire_grace_time") == 3600 and j[2].get("hour") == 3 for j in pe)
       and any(j[1][:1] == ("date",) and timedelta(minutes=4) < j[2]["run_date"] - __import__("datetime").datetime.now(__import__("datetime").timezone.utc) <= timedelta(minutes=5) for j in pe),
       f"expiry job: daily 3:30 UTC with 1 hour misfire grace + once ~5 minutes after start ({len(pe)} jobs)")

    # ---------- rate limit on order creation
    payments._orders.clear()
    codes = [c1.post("/api/pay/order", json={"plan": "Pro"}).status_code for _ in range(11)]
    ok(codes[:10] == [200] * 10 and codes[10] == 429, "order creation is rate limited (10 an hour per account)")

    # ---------- deleting a plan removes its price; removing keys = manual UPI again
    owner.delete("/api/admin/plans/Lifetime")
    ok("Lifetime" not in payments.prices(), "deleting a plan removes its price")
    owner.post("/api/admin/razorpay", json={"clear": True})
    p = c1.get("/api/plans").json()
    ok(p["online"] is False and c1.post("/api/pay/order", json={"plan": "Pro"}).status_code == 400, "keys removed: customers are back on the UPI steps")
    ok(c1.post("/api/account/upgrade", json={"channels": 3, "message": "paid by UPI ref 123"}).status_code == 200, "the manual 'I have paid' request still works")

    # ---------- (UI check 1) no keys: "I have paid ₹X" per plan; Super Admin's "Paid — allow" puts that plan on
    with mk() as c5:
        a5, t5 = customer(c5, "tara@example.com", "Tara")
        r = c5.post("/api/account/upgrade", json={"channels": 1, "message": ""})
        ok(r.status_code == 400 and "already allows 1 channel" in r.json()["detail"], f"channels wanted = 1 (old form): a plain 400 sentence, not a 422 ({r.status_code})")
        ok(c5.post("/api/account/upgrade", json={"message": "x"}).status_code == 400, "neither a plan nor channels: plain 400")
        ok(c5.post("/api/account/upgrade", json={"plan": "Duo"}).status_code == 400 and c5.post("/api/account/upgrade", json={"plan": "Gold"}).status_code == 400,
           "a plan not for sale / unknown cannot be claimed")
        owner.post(f"/api/admin/users/{a5}/access", json={"perms": {"page.viral": False}, "limits": {}})
        told.clear()
        r = c5.post("/api/account/upgrade", json={"plan": "pro", "message": "UPI 4123"})
        up = acc(a5).upgrade
        ok(r.status_code == 200 and up["plan"] == "Pro" and up["price"] == 499 and up["period"] == "monthly" and up["channels"] == 3 and up["message"] == "UPI 4123",
           f"'I have paid ₹499' stores the plan, its price and channels from the plan ({up})")
        ok(any(t == tenant.OWNER and "Pro" in x and "₹499" in x for t, x in told), "the owner is told which plan and price")
        u = next(x for x in owner.get("/api/admin/users").json()["users"] if x["id"] == a5)
        ok(u["upgrade"]["plan"] == "Pro", "the Super Admin's request list shows the plan")
        ok(owner.post(f"/api/admin/users/{a2}/allow-plan").status_code == 400, "allow-plan without an open plan request: plain 400")
        ok(c5.post(f"/api/admin/users/{a5}/allow-plan").status_code == 403, "customers cannot allow their own plan")
        told.clear()
        r = owner.post(f"/api/admin/users/{a5}/allow-plan")
        a = acc(a5)
        e5 = acl.effective(a5)
        ok(r.status_code == 200 and r.json()["applied"] and e5["plan"] == "Pro" and a.channel_limit == 3 and a.upgrade is None, "Paid — allow: the plan is on, request answered")
        ok(e5["perms"]["page.viral"] is False, "…with keep_restrictions (what the Super Admin switched off stays off)")
        ok(a.paid_plan == "Pro" and a.limit_before_pay == 1 and a.plan_expires and abs((a.plan_expires - utcnow()) - timedelta(days=30)) < timedelta(minutes=5),
           "…same bookkeeping as an online payment (paid plan, limit before, ends in 30 days)")
        ok(any(t == t5 and "Payment received: ₹499" in x for t, x in told), "…and the customer is told")
        m = next(x for x in owner.get("/api/admin/payments").json()["payments"] if x["account_id"] == a5)
        ok(m["via"] == "manual" and m["status"] == "paid" and m["amount"] == 499 and "UPI 4123" in m["note"], "…and it is in the payments list (UPI, with the reference)")
        with CoreSession() as s:
            s.get(Account, a5).plan_expires = utcnow() - timedelta(minutes=1); s.commit()
        payments.expire_due()
        ok(acl.effective(a5)["plan"] == "Free" and acc(a5).channel_limit == 1 and acl.effective(a5)["perms"]["page.viral"] is False, "…and it ends on time like a paid one")

        # ---------- (UI check 2) a plan with an empty channels limit: the tile and the effect match (limit unchanged)
        owner.post("/api/admin/plans", json={"name": "Lite", "limits": {"clips_per_day": 5}, "perms": {}, "price": 99, "period": "monthly"})
        lite = next(p for p in c5.get("/api/plans").json()["plans"] if p["name"] == "Lite")
        ok(lite["includes"]["channels"] is None, "an empty channels limit is not shown as '1 channel' (None = the account keeps its own number)")
        owner.post(f"/api/admin/users/{a5}", json={"channel_limit": 4})
        c5.post("/api/account/upgrade", json={"plan": "Lite", "message": "UPI 5"})
        ok(acc(a5).upgrade["channels"] == 4, "…the request records the account's own number")
        owner.post(f"/api/admin/users/{a5}/allow-plan")
        ok(acl.effective(a5)["plan"] == "Lite" and acc(a5).channel_limit == 4, "…and paying for it leaves the channel limit as it was (what the tile says)")
        ok(owner.post(f"/api/admin/users/{a5}/allow-plan").status_code == 400, "pressing 'Paid — allow' twice: the second time is a plain 400, nothing applied again")
        n_pay = len([x for x in owner.get("/api/admin/payments").json()["payments"] if x["account_id"] == a5])
        ok(n_pay == 2, f"…one payments row per allowed request ({n_pay})")
        ok(acl.rule_for("POST", f"/api/admin/users/{a5}/allow-plan")[0] == acl.SUPER, "the allow-plan address is Super Admin only in the access table")
        # a plan with 1 channel can be claimed by a customer who has 1 channel (the old form refused anything below 2)
        with mk() as c6:
            a6, t6 = customer(c6, "uma@example.com", "Uma")
            owner.post("/api/admin/plans", json={"name": "Solo", "limits": {"channels": 1}, "perms": {}, "price": 2499, "period": "yearly"})
            told.clear()
            r = c6.post("/api/account/upgrade", json={"plan": "Solo"})
            up = acc(a6).upgrade or {}
            ok(r.status_code == 200 and up.get("channels") == 1 and up.get("price") == 2499 and up.get("period") == "yearly", f"a 1-channel plan can be claimed ({r.status_code} {up})")
            ok(any(t == tenant.OWNER and "₹2,499 per year" in x and "no payment reference" in x for t, x in told), "the owner's message shows the price the Indian way (₹2,499 per year)")
            # "Not paid — close request": nothing is applied
            r = owner.post(f"/api/admin/users/{a6}", json={"close_request": True})
            ok(r.status_code == 200 and acc(a6).upgrade is None and acl.effective(a6)["plan"] == "Free" and acc(a6).paid_plan is None and acc(a6).plan_expires is None, "Not paid — close request: request gone, no plan put on")
            ok(owner.post(f"/api/admin/users/{a6}/allow-plan").status_code == 400, "…and it cannot be allowed afterwards")
            # raising the channel limit by hand does not silently answer a PLAN request
            c6.post("/api/account/upgrade", json={"plan": "Solo", "message": "ref 77"})
            owner.post(f"/api/admin/users/{a6}", json={"channel_limit": 2})
            ok((acc(a6).upgrade or {}).get("plan") == "Solo", "changing the channel limit by hand leaves an open plan request open")
            r = owner.post(f"/api/admin/users/{a6}/allow-plan")
            a = acc(a6)
            ok(r.json().get("applied") and a.channel_limit == 1 and a.limit_before_pay == 2 and abs((a.plan_expires - utcnow()) - timedelta(days=365)) < timedelta(minutes=5),
               f"allowed: the plan's 1 channel is applied, the 2 from before is remembered, yearly ends in 365 days ({a.channel_limit}, {a.limit_before_pay})")
            bad422 = c6.post("/api/account/upgrade", json={"channels": 0}).json()["detail"]

    # ---------- (UI check 4) one Indian price format everywhere
    ok([payments.inr(x) for x in (0, 499, 2499, 100000, 1234567, 49.5)] == ["₹0", "₹499", "₹2,499", "₹1,00,000", "₹12,34,567", "₹49.50"], "payments.inr: Indian grouping")
    html = open(os.path.join(WT, "static", "index.html"), encoding="utf-8").read()
    ok("₹${" not in html and "toLocaleString('en-IN'))" not in html.replace("Math.round(m/60).toLocaleString('en-IN'))", ""), "the page shows every price through inr()")
    import re as _re
    import subprocess
    fn = _re.search(r"function plainErr\(x\)\{.*?is not valid\.`\}", html, _re.S).group(0)
    cases = [{"loc": ["body", "channels"], "msg": "Input should be greater than or equal to 2"},
             {"loc": ["body", "message"], "msg": "String should have at most 500 characters"},
             {"loc": ["body", "plan"], "msg": "Field required"}, {"loc": ["body", "price"], "msg": "Input should be a valid integer"}]
    outp = subprocess.run(["node", "-e", fn + ";console.log(JSON.stringify(" + json.dumps(cases) + ".map(plainErr)))"],
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()
    ok(json.loads(outp or "[]") == ["The channel count must be at least 2.", "The message is too long (at most 500 characters).", "The plan is missing.", "The price must be a number."],
       f"(UI check 1) a 422 reaches the toast as a plain sentence ({outp})")
    node = lambda js: subprocess.run(["node", "-e", js], capture_output=True, text=True, encoding="utf-8").stdout.strip()
    outp = node(fn + ";console.log(JSON.stringify(" + json.dumps(bad422) + ".map(plainErr)))")
    ok(isinstance(bad422, list) and json.loads(outp or "[]") == ["The channel count must be at least 1."], f"…also the server's real 422 for 'channels: 0' ({outp})")
    outp = node(fn + ";console.log(JSON.stringify(" + json.dumps([{"loc": ["body", "some_new_field", 0], "msg": "Value error, strange"}, {"loc": ["path", "aid"], "msg": "x"}, {}]) + ".map(plainErr)))")
    ok(json.loads(outp or "[]") == ["Some new field is not valid.", "Aid is not valid.", "This value is not valid."] and "body" not in outp and "Value error" not in outp,
       f"…an unknown field or message still gives a sentence without the raw text ({outp})")
    ok("(x.loc||[]).join('.')" not in html and "j.detail||'Upload failed'" not in html, "the page no longer builds a toast from the raw validation list")
    jinr = _re.search(r"const inr=n=>\{.*?\};", html).group(0)
    nums = [0, 499, 2499, 100000, 1234567, 49.5, 1000000]
    outp = node(jinr + "console.log(JSON.stringify(" + json.dumps(nums) + ".map(inr)))")
    ok(json.loads(outp or "[]") == [payments.inr(x) for x in nums], f"(UI check 4) the page's inr() and the server's inr() write every price the same way ({outp})")
    ok("₹${esc(p.price.price)}" not in html and "${esc(inr(p.price.price))}" in html, "…the Super Admin's plan row uses it too (was ₹2499)")
    ok("YouTube channel${lim===1?'':'s'}, the same as you have now" in html and "placeholder=\"${l.key==='channels'?'not changed':'no limit'}\"" in html,
       "(UI check 2) the tile and the plan editor say what an empty channel number does")
    ok('<div class="ch" style="flex-wrap:wrap"><div style="flex:1 1 240px;min-width:0"><h3>Online payment (Razorpay)</h3>' in html, "(UI check 3) the Razorpay card's header lets the badge wrap under the title")
    src = open(os.path.join(WT, "app", "payments.py"), encoding="utf-8").read()
    ok("₹{" not in src, "the server's messages use inr() too")
    # CSP lets Razorpay Checkout load
    csp = c1.get("/").headers.get("content-security-policy", "")
    ok("https://checkout.razorpay.com" in csp and "https://api.razorpay.com" in csp, "the page's security policy allows Razorpay Checkout")
for f in os.listdir(os.path.join(WT, "app")):
    if f.endswith(".py") and b"\x08" in open(os.path.join(WT, "app", f), "rb").read():
        ok(False, f"backspace byte in app/{f}")
ok(b"\x08" not in open(os.path.join(WT, "static", "index.html"), "rb").read(), "no backspace bytes")

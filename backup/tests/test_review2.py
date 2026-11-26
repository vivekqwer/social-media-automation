"""Regression tests for the last review before the payments deploy: renewal after the end date, test orders after going
live, what a paying customer gets (restrictions vs values kept from a deleted plan), the expiry job racing a renewal,
'Paid - allow' for the plan that was shown, one customer backup unpacked at a time."""
import hashlib, hmac, json, os, sys, tempfile
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
WT = r"D:\integ-final"
sys.path.insert(0, WT)
from fastapi.testclient import TestClient
from app import main, acl, accounts, tenant, auth, telegram, jobs, payments, backup
from app.accounts import Account, CoreSession
from app.payments import Payment
from app.db import SessionLocal, Destination, set_setting, utcnow
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
told = []
telegram.notify = lambda text, *a, **k: told.append((tenant.current(), text))
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
KID, SECRET, WH = "rzp_test_FAKEKEY1234", "fake-key-secret-for-tests", "fake-webhook-secret-xyz"
LIVE = "rzp_live_FAKEKEY1234"


class R:
    def __init__(self, code, data=None):
        self.status_code, self._d, self.text = code, data or {}, json.dumps(data or {})

    def json(self):
        return self._d


n_order = [0]


def fake_post(url, json=None, auth=None, timeout=None, **k):
    n_order[0] += 1
    return R(200, {"id": f"order_TEST{n_order[0]:06d}", "amount": json["amount"], "currency": "INR", "status": "created"})


payments.httpx.post = fake_post
payments.httpx.get = lambda url, **k: R(200, {"items": []})
sign = lambda secret, msg: hmac.new(secret.encode(), msg.encode() if isinstance(msg, str) else msg, hashlib.sha256).hexdigest()
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)


def event(oid, pid, amount):
    return json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": pid, "order_id": oid, "amount": amount, "currency": "INR", "status": "captured"}}}})


with mk() as owner, mk() as anon, mk() as c1, mk() as c2, mk() as c3, mk() as c4, mk() as c5, mk() as c6:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})

    def customer(client, email, name):
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email, "password": PW, "accept_terms": True})
        aid = next(u for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email)["id"]
        tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        client.post("/api/login", json={"username": email, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
            with SessionLocal() as s:
                s.add(Destination(channel_id="UC-" + name, name=name + " main", token="x")); s.commit()
        return aid, tid

    acc = lambda aid: accounts.get(aid)
    FREE = payments._free_plan()  # where an account goes when its paid plan ends
    owner.post("/api/admin/plans", json={"name": "Pro", "limits": {"channels": 5, "uploads_per_day": 10}, "perms": {},
                                         "price": 499, "period": "monthly"})
    owner.post("/api/admin/plans", json={"name": "Basic", "limits": {"channels": 2}, "perms": {}, "price": 199, "period": "monthly"})
    owner.post("/api/admin/plans", json={"name": "Free", "limits": {"channels": 1}, "perms": {}})
    owner.post("/api/admin/plans", json={"name": "Duo", "limits": {"channels": 2}, "perms": {}})
    owner.post("/api/admin/plans", json={"name": "Starter", "limits": {"channels": 1, "uploads_per_day": 1}, "perms": {"page.viral": False}})
    owner.post("/api/admin/razorpay", json={"key_id": KID, "key_secret": SECRET, "webhook_secret": WH})

    def pay(client, plan="Pro", amount=49900, tag="A"):
        o = client.post("/api/pay/order", json={"plan": plan}).json()
        body = event(o["order_id"], f"pay_{tag}{o['order_id'][-6:]}", amount)
        return anon.post("/api/pay/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": sign(WH, body)}).json()

    def end_now(aid):
        with CoreSession() as s:
            s.get(Account, aid).plan_expires = utcnow() - timedelta(minutes=1); s.commit()

    # ---------- 1. a renewal made after the end date, before the daily job, does not keep the paid channel limit
    a1, t1 = customer(c1, "asha@example.com", "Asha")
    owner.post(f"/api/admin/users/{a1}/plan", json={"plan": "Duo"})  # 2 channels before paying
    pay(c1, tag="A")
    ok(acc(a1).channel_limit == 5 and acc(a1).limit_before_pay == 2, "paid: 5 channels, the 2 from before are remembered")
    end_now(a1)  # the end date passed, the daily job has not run yet
    r = pay(c1, tag="B")
    ok(r.get("applied") and acc(a1).limit_before_pay == 2, f"renewed after the end date: still 2 to give back ({acc(a1).limit_before_pay})")
    end_now(a1)
    payments.expire_due()
    ok(acc(a1).channel_limit == 2 and acl.effective(a1)["plan"] == FREE and acc(a1).paid_plan is None,
       f"at the next end: free plan with 2 channels, not the paid 5 ({acc(a1).channel_limit})")

    # ---------- 3. what a paying customer gets
    # (a) values an account only kept from a deleted plan are replaced by the plan that was paid for
    a2, t2 = customer(c2, "ravi@example.com", "Ravi")
    owner.post(f"/api/admin/users/{a2}/plan", json={"plan": "Starter"})
    owner.delete("/api/admin/plans/Starter")
    e = acl.effective(a2)
    ok(e["perms"]["page.viral"] is False and e["limits"]["uploads_per_day"] == 1, "the deleted plan's values stay with the account (as before)")
    r = pay(c2, tag="C")
    e = acl.effective(a2)
    ok(r.get("applied") and e["plan"] == "Pro" and e["perms"]["page.viral"] is True and e["limits"]["uploads_per_day"] == 10 and acc(a2).channel_limit == 5,
       f"paying for Pro gives what Pro promises: switch on, 10 uploads a day, 5 channels ({e['perms']['page.viral']}, {e['limits']['uploads_per_day']}, {acc(a2).channel_limit})")
    # (b) a channel limit RAISED by hand is a gift: the paid plan goes beyond it, and it comes back at the end
    a3, t3 = customer(c3, "mira@example.com", "Mira")
    owner.post(f"/api/admin/users/{a3}", json={"channel_limit": 3})
    r = pay(c3, tag="D")
    ok(r.get("applied") and acc(a3).channel_limit == 5 and acc(a3).limit_before_pay == 3, f"raised to 3 by hand, pays for Pro: 5 channels ({acc(a3).channel_limit})")
    end_now(a3)
    payments.expire_due()
    ok(acc(a3).channel_limit == 3, f"…and back to the 3 the Super Admin gave when the plan ends ({acc(a3).channel_limit})")
    # (c) a channel limit LOWERED by hand, a switch set OFF and a limit set in the Access panel are restrictions: they stay
    a4, t4 = customer(c4, "neel@example.com", "Neel")
    owner.post(f"/api/admin/users/{a4}/plan", json={"plan": "Duo"})
    owner.post(f"/api/admin/users/{a4}", json={"channel_limit": 1})  # lowered from 2 to 1
    owner.post(f"/api/admin/users/{a4}/access", json={"perms": {"page.viral": False}, "limits": {"uploads_per_day": 3}})
    r = pay(c4, tag="E")
    e = acl.effective(a4)
    ok(r.get("applied") and acc(a4).channel_limit == 1 and e["perms"]["page.viral"] is False and e["limits"]["uploads_per_day"] == 3,
       f"restrictions set by hand survive the payment ({acc(a4).channel_limit}, {e['perms']['page.viral']}, {e['limits']['uploads_per_day']})")
    end_now(a4)
    payments.expire_due()
    e = acl.effective(a4)
    ok(acc(a4).channel_limit == 1 and e["perms"]["page.viral"] is False and e["limits"]["uploads_per_day"] == 3, "…and the plan's end")
    r = pay(c4, tag="F")
    ok(acc(a4).channel_limit == 1 and acl.effective(a4)["perms"]["page.viral"] is False, "…and the next payment (the mark is not lost)")
    owner.post(f"/api/admin/users/{a4}/access", json={"perms": {"page.viral": True}, "limits": {"uploads_per_day": -1}})
    owner.post(f"/api/admin/users/{a4}", json={"channel_limit": 2})  # raised again by hand: no longer a restriction
    end_now(a4); payments.expire_due()
    r = pay(c4, tag="G")
    e = acl.effective(a4)
    ok(acc(a4).channel_limit == 5 and e["perms"]["page.viral"] is True and e["limits"]["uploads_per_day"] == 10,
       f"lifted by the Super Admin: the next payment gives the full plan ({acc(a4).channel_limit}, {e['limits']['uploads_per_day']})")
    # (d) an account saved before restrictions were marked: every own OFF switch counts as one (the safe reading)
    a5, t5 = customer(c5, "tara@example.com", "Tara")
    with CoreSession() as s:
        row = s.get(Account, a5); row.access = {"plan": "", "perms": {"page.viral": False}, "limits": {"uploads_per_day": 2}, "controls": {}}; s.commit()
    acl.invalidate()
    pay(c5, tag="H")
    e = acl.effective(a5)
    ok(e["perms"]["page.viral"] is False and e["limits"]["uploads_per_day"] == 2, "an account from before this change keeps its own OFF switch and limit on payment")

    # ---------- 4. the daily job does not end a plan that was renewed while the job was running
    a6, t6 = customer(c6, "kiran@example.com", "Kiran")
    pay(c6, tag="I")
    end_now(a5); end_now(a6)
    real_tell = payments._tell_customer
    fired = []

    def tell(a, text):
        if not fired and a.id == a5:  # while account 5 is being told, account 6's payment lands
            fired.append(1)
            pay(c6, tag="J")
        return real_tell(a, text)

    payments._tell_customer = tell
    out = payments.expire_due()
    payments._tell_customer = real_tell
    x = acc(a6)
    ok(fired and acl.effective(a6)["plan"] == "Pro" and x.paid_plan == "Pro" and x.plan_expires and x.plan_expires > utcnow() + timedelta(days=25),
       f"renewed during the job: still on Pro with the new end date ({acl.effective(a6)['plan']}, {x.plan_expires})")
    ok(acl.effective(a5)["plan"] == FREE and out["ended"] == 1, f"…the account that really ended is on the free plan ({out})")

    # ---------- 2. an order made with test keys puts no plan on after live keys are saved
    end_now(a1); payments.expire_due()  # a1 is on the free plan again
    o = c1.post("/api/pay/order", json={"plan": "Pro"}).json()
    owner.post("/api/admin/razorpay", json={"key_id": LIVE, "key_secret": SECRET, "webhook_secret": WH})
    ok(payments.mode() == "live", "live keys saved")
    told.clear()
    body = event(o["order_id"], "pay_TESTAFTERLIVE", 49900)
    r = anon.post("/api/pay/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": sign(WH, body)}).json()
    with CoreSession() as s:
        row = s.query(Payment).filter(Payment.order_id == o["order_id"]).first()
        st, note = row.status, row.note or ""
    ok(r.get("applied") is False and acl.effective(a1)["plan"] == FREE and acc(a1).paid_plan is None and st == "refused" and "test" in note,
       f"a test payment after going live: no plan, the row says why ({r}, {st})")
    ok(sum("TEST payment" in t for _, t in told) == 1, "the owner is told once")
    anon.post("/api/pay/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": sign(WH, body)})
    ok(sum("TEST payment" in t for _, t in told) == 1 and acl.effective(a1)["plan"] == FREE, "Razorpay's retry changes nothing and does not message again")
    lst = owner.get("/api/admin/payments").json()["payments"]
    ok(any(p.get("status") == "refused" for p in lst), "the payments list shows it as not applied")
    r = pay(c1, tag="K")  # a live order in live mode still works
    ok(r.get("applied") and acl.effective(a1)["plan"] == "Pro", "a live order is applied as before")

    # ---------- 'Paid - allow' puts on the plan that was shown on the button
    owner.post("/api/admin/razorpay", json={"key_id": "", "key_secret": "", "webhook_secret": ""})
    r = c2.post("/api/account/upgrade", json={"plan": "Basic", "message": "UPI ref 111"})
    if r.status_code == 200:
        c2.post("/api/account/upgrade", json={"plan": "Pro", "message": "UPI ref 222"})  # replaced meanwhile
        r = owner.post(f"/api/admin/users/{a2}/allow-plan", json={"plan": "Basic"})
        ok(r.status_code == 400 and "changed the request" in r.json().get("detail", ""), f"the request changed since the page was loaded: refused in plain words ({r.status_code})")
        r = owner.post(f"/api/admin/users/{a2}/allow-plan", json={"plan": "Pro"})
        ok(r.status_code == 200 and r.json().get("applied"), "the plan that is shown is put on")
    else:
        ok(False, f"could not send a plan request ({r.status_code} {r.text[:120]})")

# ---------- one customer backup is unpacked at a time
class Busy:
    def acquire(self, timeout=None):
        return False

    def release(self):
        pass


real = backup._unpack_one
backup._unpack_one = Busy()
try:
    backup.unpack(b'{"format": "x"}', is_super=False)
    ok(False, "a second customer file at the same moment waits and is then refused")
except backup.BackupError as e:
    ok("busy" in str(e), "a second customer file at the same moment is told the system is busy")
backup._unpack_one = real
try:
    backup.unpack(b"not a backup", is_super=False)
    ok(False, "garbage refused")
except backup.BackupError:
    ok(real.acquire(timeout=1), "the turn is given back after a refused file")
    real.release()

"""Every address of the server has a decision in app/acl.py ROUTES. A new route without one makes this fail."""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, r"D:\integ-final")
from fastapi.routing import APIRoute
from app import main, acl
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)

real = set()
for r in main.app.routes:
    if isinstance(r, APIRoute):
        for m in r.methods:
            if m not in ("HEAD", "OPTIONS"):
                real.add((m, r.path))
table = {(m, p) for m, p, _rule, _text in acl.ROUTES if m != "*"}
wild = [(m, p) for m, p, _rule, _text in acl.ROUTES if m == "*"]

missing = sorted(x for x in real if acl.rule_for(*x) is None)
ok(not missing, f"every route of app/main.py has a decision in acl.ROUTES ({len(real)} routes)" + (f" — MISSING: {missing}" if missing else ""))
exact_missing = sorted(real - table)
covered_by_wild = [x for x in exact_missing if acl.rule_for(*x) is not None]
ok(exact_missing == covered_by_wild, f"routes not written out one by one are covered by a wildcard line only: {exact_missing}")
stale = sorted(table - real)
ok(not stale, "no line in ROUTES points at an address that no longer exists" + (f" — STALE: {stale}" if stale else ""))
ok(len(acl.ROUTES) == len({(m, p) for m, p, *_ in acl.ROUTES}), "no address is decided twice")

known = set(acl.DEFAULT) | {acl.ANYONE, acl.SIGNED, acl.SUPER, acl.HELPER}
bad = [(m, p, rule) for m, p, rule, _t in acl.ROUTES if rule.replace("inside:", "") not in known]
ok(not bad, "every decision is a real switch or one of anyone / signed-in / super / helper" + (f" — BAD: {bad}" if bad else ""))

# the kinds of decision
kinds = {"anyone": 0, "signed-in": 0, "super": 0, "helper": 0, "switch": 0}
for m, p, rule, _t in acl.ROUTES:
    kinds[rule if rule in kinds else "switch"] += 1
print("INFO", len(acl.ROUTES), "lines:", kinds, flush=True)
ok(all(rule == acl.SUPER for m, p, rule, _t in acl.ROUTES if p.startswith("/api/admin/")) and
   all(p.startswith("/api/admin/") for m, p, rule, _t in acl.ROUTES if rule == acl.SUPER), "/api/admin/* is Super Admin only, and nothing else is")
ok(all(rule == acl.HELPER for m, p, rule, _t in acl.ROUTES if p.startswith("/api/worker/")), "/api/worker/* belongs to the helper (its own token)")
changing = [(m, p, rule) for m, p, rule, _t in acl.ROUTES if m in ("POST", "PUT", "DELETE", "PATCH") and rule == acl.SIGNED]
ok(sorted(p for _m, p, _r in changing) == ["/api/change-password", "/api/onboarding/ack", "/api/pay/order", "/api/pay/verify"],
   f"the only changing addresses open to every signed-in account: {[p for _m, p, _r in changing]}")
ok({p for p in main.PUBLIC_PATHS} <= {p for m, p, rule, _t in acl.ROUTES if rule == acl.ANYONE} | {"/login", "/forgot", "/reset", "/register"},
   "every public path of the middleware is marked 'anyone' here")
used = {rule.replace("inside:", "") for _m, _p, rule, _t in acl.ROUTES}
inline_only = {"source.topic", "source.channel", "source.trending", "source.reel", "uploads.youtube", "uploads.auto"}
unused = sorted(set(acl.DEFAULT) - used - inline_only)
ok(not unused, "every switch guards at least one address (or is enforced inside a route / in background work)" + (f" — UNUSED: {unused}" if unused else ""))

# matching real paths
ok(acl.rule_for("POST", "/api/clips/12/render")[0] == "clips.rerender" and acl.rule_for("DELETE", "/api/clips/12")[0] == "clips.discard", "numbers in the address are matched")
ok(acl.rule_for("POST", "/api/permissions/find-emails")[0] == "permissions.manage" and acl.rule_for("POST", "/api/permissions/UCabc/send")[0] == "gmail.send", "fixed words win over {placeholders}")
ok(acl.rule_for("GET", "/api/reports/weekly-2026-01-04.pdf")[0] == "page.analytics" and acl.rule_for("POST", "/api/reports/weekly")[0] == "page.analytics", "reports")
ok(acl.rule_for("POST", "/api/alerts/anything/new")[0] == "page.alerts" and acl.rule_for("GET", "/api/alerts/x")[0] == "page.alerts", "anything new under /api/alerts/ belongs to the Alerts page")
ok(acl.rule_for("POST", "/api/not-there") is None and acl.rule_for("POST", "/api/clips/1/render/extra") is None, "unknown addresses are unknown")
user = {"role": "user", "account_id": 10**9, "tenant": 5}
ok(acl.check_request("POST", "/api/not-there", user) == acl.UNKNOWN and acl.check_request("GET", "/api/not-there", user) is None
   and acl.check_request("GET", "/index.html", user) is None, "a customer cannot CHANGE anything through an address nobody decided about")
ok(acl.check_request("POST", "/api/not-there", {"role": "super", "tenant": 1}) is None, "the Super Admin is not stopped")
ok(acl.check_request("GET", "/api/admin/users", user) == "Only the Super Admin can do this.", "admin addresses refuse customers here too")
for f in os.listdir(os.path.join(r"D:\integ-final", "app")):
    if f.endswith(".py"):
        raw = open(os.path.join(r"D:\integ-final", "app", f), "rb").read()
        if b"\x08" in raw:
            ok(False, f"backspace byte in app/{f}")
ok(b"\x08" not in open(os.path.join(r"D:\integ-final", "static", "index.html"), "rb").read(), "no backspace bytes in the changed files")

"""Access control: plans, the default for accounts without a plan, what a new account gets, and deleting an
account's whole data (typed e-mail, never the owner's copy, nobody else's data touched)."""
import os, sys, tempfile
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, r"D:\integ-final")
from fastapi.testclient import TestClient
from app import main, acl, accounts, tenant, auth, telegram, jobs
from app.db import SessionLocal, Destination, Channel, Clip, Project, set_setting, get_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
telegram.notify = lambda *a, **k: None
auth.rate_limited = lambda ip, kind="owner": False
jobs.executor.submit = lambda fn, *a, **k: None
PW = "customer-pass-12345"
mk = lambda: TestClient(main.app, raise_server_exceptions=False)
with mk() as owner, mk() as c1, mk() as c2, mk() as c3, mk() as anon:
    owner.post("/api/login", json={"username": "admin", "password": "owner-test-pass-88"})
    with SessionLocal() as s:
        s.add(Destination(channel_id="UC-owner", name="Owner channel", token="x")); s.commit()
    (Path(d) / "owner-file.txt").write_text("the owner's own file")

    def register(email, name):
        anon.post("/api/register", json={"name": name, "brand": name + " Studio", "email": email, "password": PW, "accept_terms": True})
        return next(u for u in owner.get("/api/admin/users").json()["users"] if u["email"] == email)

    def customer(client, email, name):
        aid = register(email, name)["id"]
        tid = owner.post(f"/api/admin/users/{aid}/approve").json()["tenant_id"]
        client.post("/api/login", json={"username": email, "password": PW})
        with tenant.use(tid):
            set_setting("onboarding", '{"finished": true}')
            with SessionLocal() as s:
                s.add(Destination(channel_id="UC-" + name, name=name + " channel", token="x")); s.commit()
        return aid, tid

    a1, t1 = customer(c1, "asha@example.com", "Asha")
    a2, t2 = customer(c2, "ravi@example.com", "Ravi")
    perms = lambda c: c.get("/api/me").json()["access"]["perms"]
    limits = lambda c: c.get("/api/me").json()["access"]["limits"]
    acc = lambda aid: owner.get(f"/api/admin/users/{aid}/access").json()
    plan = lambda name, apply=True, limits=None, **off: owner.post("/api/admin/plans", json={
        "name": name, "apply": apply, "limits": limits or {}, "perms": {k.replace("__", "."): v for k, v in off.items()}})

    # ---------- plans
    r = plan("Starter", limits={"sources": 2, "channels": 2, "clips_per_day": 5}, page__viral=False, source__reel=False)
    ov = r.json()
    ok(r.status_code == 200 and [p["name"] for p in ov["plans"]] == ["Starter"] and ov["plans"][0]["users"] == 0 and ov["no_plan_users"] == 2, "a plan is made; nobody is on it yet")
    ok(plan("x" * 41).status_code == 422 and plan("<b>bad</b>").status_code == 400, "bad plan names are refused")
    ok(perms(c2)["page.viral"] is True, "making a plan changes nobody")
    r = owner.post(f"/api/admin/users/{a2}/plan", json={"plan": "starter"})
    ok(r.status_code == 200 and r.json()["plan"] == "Starter" and perms(c2)["page.viral"] is False and perms(c2)["source.reel"] is False
       and perms(c2)["page.clips"] is True and limits(c2) == {"sources": 2, "clips_per_day": 5, "uploads_per_day": None, "storage_mb": None, "channels": 2},
       f"one click puts a customer on the plan: its switches and limits apply at once ({limits(c2)})")
    ok(perms(c1)["page.viral"] is True and limits(c1)["sources"] is None, "…only that customer")
    r = c2.get("/api/viral")
    ok(r.status_code == 403 and "Viral today page" in r.json()["detail"], "…and the server enforces the plan")
    ok(owner.post(f"/api/admin/users/{a2}/plan", json={"plan": "Gold"}).status_code == 400, "an unknown plan cannot be applied")
    # the customer's own switch on top of the plan
    owner.post(f"/api/admin/users/{a2}/access", json={"perms": {"source.reel": True}, "limits": {"sources": 9}})
    ok(perms(c2)["source.reel"] is True and limits(c2)["sources"] == 9 and acc(a2)["own_perms"] == {"source.reel": True}, "a switch set for one customer wins over their plan")
    # change the plan: offered to apply, or not
    r = plan("Starter", apply=False, limits={"sources": 2, "channels": 2, "clips_per_day": 5}, page__viral=False, source__reel=False, clips__discard=False)
    ok(r.json()["accounts"] == 1 and perms(c2)["clips.discard"] is True, "plan changed with “keep them as they are”: the customer on it keeps what they had")
    plan("Pro", limits={"channels": 5}, page__ideas=False)
    a3, t3 = customer(c3, "meena@example.com", "Meena")
    owner.post(f"/api/admin/users/{a3}/plan", json={"plan": "Starter"})
    ok(perms(c3)["clips.discard"] is False and perms(c3)["page.viral"] is False, "…a customer put on the plan afterwards gets the new plan")
    r = plan("Starter", apply=True, limits={"sources": 3, "channels": 4, "clips_per_day": 5}, page__viral=False, source__reel=False, clips__discard=False, page__alerts=False)
    ok(r.json()["accounts"] == 2 and perms(c2)["page.alerts"] is False and perms(c3)["page.alerts"] is False and limits(c3)["sources"] == 3 and limits(c3)["channels"] == 4,
       "plan changed with “apply to the customers on it”: they all follow, at once")
    ok(limits(c2)["sources"] == 3 and perms(c2)["source.reel"] is True and perms(c2)["clips.discard"] is True and perms(c1)["page.alerts"] is True,
       "…a changed value replaces the customer's own; what was not changed stays; customers not on the plan are untouched")
    ov = owner.get("/api/admin/acl").json()
    ok({p["name"]: p["users"] for p in ov["plans"]} == {"Pro": 0, "Starter": 2} and ov["no_plan_users"] == 1, "the plans card shows how many customers are on each plan")
    r = owner.post(f"/api/admin/users/{a2}/plan", json={"plan": "Starter"})
    ok(acc(a2)["own_perms"] == {} and perms(c2)["source.reel"] is False and perms(c2)["clips.discard"] is False, "applying the plan again clears the customer's own switches")

    # ---------- the default (accounts without a plan) and what a NEW account gets
    r = owner.post("/api/admin/acl/default", json={"perms": {"page.ideas": False}, "limits": {"storage_mb": 500}, "new_plan": "Starter", "apply": True})
    ok(r.status_code == 200 and perms(c1)["page.ideas"] is False and limits(c1)["storage_mb"] == 500, "the default set applies to customers without a plan")
    ok(perms(c2)["page.ideas"] is False and limits(c2)["storage_mb"] == 500 and perms(c2)["page.viral"] is False, "…and fills what a plan does not say")
    ok(owner.post("/api/admin/acl/default", json={"perms": {}, "limits": {}, "new_plan": "Nope"}).status_code == 400, "an unknown plan cannot be the plan for new accounts")
    new = register("nitin@example.com", "Nitin")
    ok(new["plan"] == "Starter" and new["channel_limit"] == 4 and acc(new["id"])["perms"]["page.viral"] is False, "a NEW account starts on the chosen plan (switches, limits and channels)")
    r = owner.post("/api/admin/acl/default", json={"perms": {"page.ideas": True, "page.guide": False}, "limits": {}, "new_plan": "", "apply": False})
    ok(perms(c1)["page.ideas"] is False and perms(c1)["page.guide"] is True and limits(c1)["storage_mb"] == 500, "default changed with “keep them as they are”: existing customers keep theirs")
    new2 = register("om@example.com", "Om")
    a5 = acc(new2["id"])
    ok(new2["plan"] in ("starter", "") and a5["plan"] == "" and a5["perms"]["page.guide"] is False and a5["perms"]["page.ideas"] is True and a5["limits"]["storage_mb"] is None,
       "…a new account without a plan gets the new default")
    ok(owner.post(f"/api/admin/users/{a1}/plan", json={"plan": ""}).status_code == 200 and perms(c1)["page.guide"] is False, "“no plan” = the default switches")
    # deleting a plan
    r = owner.delete("/api/admin/plans/Starter")
    ok(r.status_code == 200 and r.json()["accounts"] == 3 and perms(c2)["page.viral"] is False and limits(c2)["sources"] == 3 and acc(a2)["plan"] == "",
       "a deleted plan leaves its customers exactly as they were")
    ok(owner.delete("/api/admin/plans/Starter").status_code == 404, "…and is gone")
    log = [r["detail"] for r in owner.get("/api/admin/activity?actor=super&limit=500").json()["rows"]]
    ok(any(x.startswith("Made the plan “Starter”") for x in log) and any("Changed the plan “Starter” — applied to the 2 account(s)" in x for x in log)
       and "Put on the plan “Starter”" in log and any(x.startswith("Deleted the plan “Starter”") for x in log) and any(x.startswith("Changed the default switches") for x in log),
       "plan and default changes are in the activity log")
    # PostgreSQL keeps an empty JSON column as JSON null, and old rows have no column value at all
    with acl.CoreSession() as s:
        row = s.get(acl.Account, a1); row.access = None; row.plan = None; s.commit()
    acl.invalidate()
    ok(perms(c1)["page.clips"] is True and owner.post(f"/api/admin/users/{a1}/access", json={"perms": {"page.clips": False}}).status_code == 200
       and perms(c1)["page.clips"] is False, "an account whose switches were never saved (empty column) works and can be changed")

    # ---------- delete the whole data of one account
    for t in (t1, t2):
        with tenant.use(t):
            with SessionLocal() as s:
                p = Project(url="u", video_id="abcdefghijk", title="V", channel="C", mode="cc", duration=60); s.add(p); s.commit()
                s.add(Clip(project_id=p.id, start=0, end=30, status="rendered", title="clip")); s.add(Channel(kind="topic", query="q", mode="cc")); s.commit()
            f = acl.tenant_folder(t) / "clips" / "1" / "out.mp4"; f.parent.mkdir(parents=True, exist_ok=True); f.write_bytes(b"video")
    folder1, folder2 = acl.tenant_folder(t1), acl.tenant_folder(t2)
    ok(c1.post(f"/api/admin/users/{a1}/delete-data", json={"email": "asha@example.com"}).status_code == 403, "a customer cannot delete anything this way")
    r = owner.post(f"/api/admin/users/{a2}/delete-data", json={"email": "asha@example.com"})
    ok(r.status_code == 400 and "Nothing was deleted" in r.json()["detail"] and (folder2 / "app.db").exists() and c2.get("/api/overview").status_code == 200,
       "the wrong e-mail typed: refused, nothing deleted, the customer still signed in")
    ok(owner.post(f"/api/admin/users/{a2}/delete-data", json={"email": ""}).status_code == 400, "…an empty one too")
    r = owner.post(f"/api/admin/users/{a2}/delete-data", json={"email": " Ravi@Example.com "})
    j = r.json()
    ok(r.status_code == 200 and j["deleted"] and j["tenant_id"] == t2 and not folder2.exists(), f"the right e-mail: the data folder and its database are gone ({j})")
    ok(c2.get("/api/overview").status_code == 401 and anon.post("/api/login", json={"username": "ravi@example.com", "password": PW}).status_code == 403,
       "the account was blocked and signed out first")
    ok(t2 not in accounts.active_tenants() and t1 in accounts.active_tenants(), "background work no longer runs for that copy")
    ok((folder1 / "clips" / "1" / "out.mp4").exists() and c1.get("/api/clips/1").json().get("title") == "clip" and len(c1.get("/api/destinations").json()) == 1,
       "another customer's files and data are untouched")
    ok((Path(d) / "owner-file.txt").exists() and (Path(d) / "t.db").exists() and [x["name"] for x in owner.get("/api/destinations").json()] == ["Owner channel"],
       "the owner's own data is untouched")
    rows = owner.get(f"/api/admin/activity?account_id={a2}").json()["rows"]
    ok(any(x["action"] == "admin.delete_data" and "ravi@example.com" in x["detail"] and x["actor"] == "super" for x in rows), "the deletion is in the activity log")
    ok(owner.post(f"/api/admin/users/{a2}/delete-data", json={"email": "ravi@example.com"}).json().get("deleted") is True, "pressing it again is harmless")
    # the owner's copy can never be deleted
    for bad in (1, 0, -3):
        try:
            acl.tenant_folder(bad); raised = False
        except ValueError:
            raised = True
        ok(raised, f"copy {bad} is never a customer folder")
    with acl.CoreSession() as s:
        fake = acl.Account(email="evil@example.com", name="Evil", brand="E", pw_hash="x", status="blocked", tenant_id=1); s.add(fake); s.commit(); fid = fake.id
    acl.invalidate()
    r = owner.post(f"/api/admin/users/{fid}/delete-data", json={"email": "evil@example.com"})
    ok(r.status_code == 400 and "cannot be deleted" in r.json()["detail"] and (Path(d) / "t.db").exists() and (Path(d) / "owner-file.txt").exists()
       and owner.get("/api/overview").status_code == 200, "even an account row pointing at copy 1 cannot delete the owner's data")
    pend = register("zed@example.com", "Zed")
    ok(owner.post(f"/api/admin/users/{pend['id']}/delete-data", json={"email": "zed@example.com"}).status_code == 400, "an account that was never approved has nothing to delete")
    base = acl.tenants_dir()
    ok(all(not str(acl.tenant_folder(n)).startswith(str(base) + os.sep + "..") and acl.tenant_folder(n).parent == base for n in (2, 77)),
       "a customer folder is always exactly <data>/tenants/<number>")
    # unblocked later: a fresh, empty copy
    owner.post(f"/api/admin/users/{a2}", json={"status": "active"})
    ok(c2.post("/api/login", json={"username": "ravi@example.com", "password": PW}).status_code == 200 and c2.get("/api/overview").json()["destinations"] == []
       and c2.get("/api/clips/1").status_code == 404 and c2.get("/api/me").json()["onboarding"]["active"], "unblocked later: the customer starts again with an empty copy")
    ok(c1.get("/api/clips/1").json().get("title") == "clip", "…and still nobody else's data is touched")
print("DONE", flush=True)

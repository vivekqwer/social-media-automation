import os, sys, tempfile
from pathlib import Path
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "tenant-test-pass-5"
sys.path.insert(0, r"D:\integ-final")
from app import tenant, config, jobs
from app.config import DATA_DIR, BASE_DATA_DIR, COOKIES_PATH
from app.db import Base, engine, SessionLocal, Destination, Clip, Project, get_setting, set_setting, ensure_tenant, migrate
from app import faces
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
Base.metadata.create_all(engine); migrate()

# files
ok(Path(DATA_DIR) == BASE_DATA_DIR and Path(DATA_DIR / "clips" / "5") == BASE_DATA_DIR / "clips" / "5", "tenant 1 uses the original data folder")
with tenant.use(2):
    p2 = Path(DATA_DIR / "clips" / "5")
    ok(p2 == BASE_DATA_DIR / "tenants" / "2" / "clips" / "5", f"tenant 2 has its own folder: {p2.relative_to(BASE_DATA_DIR)}")
    ok(Path(COOKIES_PATH) == BASE_DATA_DIR / "tenants" / "2" / "cookies.txt" and Path(faces.DEFAULT_FACE).parts[-4:-2] == ("tenants", "2"),
       "module-level constants (cookies, default face) follow the customer too")
    w = DATA_DIR / "clips" / "7"
    w.mkdir(parents=True, exist_ok=True); (w / "a.txt").write_text("two");
    ok((w / "a.txt").exists() and open(w / "a.txt").read() == "two" and [f.name for f in w.glob("*.txt")] == ["a.txt"] and w.name == "7", "mkdir / open / glob / name work on the lazy path")
ok(not (DATA_DIR / "clips" / "7" / "a.txt").exists(), "tenant 1 does not see tenant 2's file")
ok(Path(COOKIES_PATH) == BASE_DATA_DIR / "cookies.txt", "back in tenant 1 the same constant points to tenant 1")
import subprocess
with tenant.use(2):
    r = subprocess.run([sys.executable, "-c", "import sys,os;print(os.path.exists(sys.argv[1]))", DATA_DIR / "clips" / "7" / "a.txt"], capture_output=True, text=True, cwd=DATA_DIR / "clips" / "7")
    ok(r.stdout.strip() == "True", "the lazy path can be passed to a program (argument and working folder)")

# database
with SessionLocal() as s:
    s.add(Destination(channel_id="UC-owner", name="Owner channel", token="x")); s.commit()
set_setting("mail_sign", "Owner")
with tenant.use(2):
    with SessionLocal() as s:
        ok(s.query(Destination).count() == 0, "tenant 2 starts empty (does not see the owner's channel)")
        s.add(Destination(channel_id="UC-two", name="Customer channel", token="x"))
        p = Project(url="u", title="T2", mode="cc"); s.add(p); s.commit()
        s.add(Clip(project_id=p.id, start=0, end=10, aspect="9:16", status="draft")); s.commit()
    ok(get_setting("mail_sign") is None, "settings are private too")
    set_setting("mail_sign", "Customer")
with tenant.use(3):
    with SessionLocal() as s:
        ok(s.query(Destination).count() == 0 and s.query(Clip).count() == 0, "tenant 3 sees neither")
with SessionLocal() as s:
    ok([x.name for x in s.query(Destination)] == ["Owner channel"] and s.query(Clip).count() == 0 and get_setting("mail_sign") == "Owner", "the owner sees only the owner's data")
with tenant.use(2):
    with SessionLocal() as s:
        ok([x.name for x in s.query(Destination)] == ["Customer channel"] and s.query(Clip).count() == 1 and get_setting("mail_sign") == "Customer", "tenant 2 sees only its own")
ok((BASE_DATA_DIR / "tenants" / "2" / "app.db").exists(), "tenant 2's database is its own file (SQLite) / schema (PostgreSQL)")

# background jobs keep the customer
seen = []
def job():
    with SessionLocal() as s:
        seen.append((tenant.current(), [x.name for x in s.query(Destination)]))
with tenant.use(2):
    f2 = jobs.executor.submit(job)
f1 = jobs.executor.submit(job)
f2.result(); f1.result()
ok(seen == [(2, ["Customer channel"]), (1, ["Owner channel"])], f"a queued job runs as the customer who queued it: {seen}")
ok(tenant.current() == 1, "outside a customer block the code is the owner again")
# nested / error safety
try:
    with tenant.use(2):
        with tenant.use(3):
            assert tenant.current() == 3
        assert tenant.current() == 2
        raise ValueError("boom")
except ValueError:
    pass
ok(tenant.current() == 1, "the customer is restored after an error")
# threads started elsewhere default to the owner, never to a random customer
import threading
got = []
with tenant.use(2):
    t = threading.Thread(target=lambda: got.append(tenant.current())); t.start(); t.join()
ok(got == [1], "a brand-new thread starts as the owner (never inherits a customer by accident)")

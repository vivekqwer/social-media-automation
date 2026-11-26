"""Google's own temporary errors while reading watch time never become an alert (only after ~12 hours in a row)."""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"
sys.path.insert(0, r"D:/integ-final")
from app import stats
from app.db import Base, engine, migrate, SessionLocal, Destination, get_setting, set_setting
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
Base.metadata.create_all(engine); migrate()
with SessionLocal() as s:
    s.add(Destination(channel_id="UC1", name="Foody", token="x")); s.commit()
notes = []; stats._note = lambda what, e: notes.append(what)
set_setting("watchtime-status:1", "ok")
def boom(did): raise Exception('<HttpError 500 when requesting https://x/v2/reports returned "Internal error encountered.". Details: "[{\'reason\': \'backendError\'}]">')
stats.sync_watch_time = boom
for i in range(3): stats.sync_all_watch_time()
ok(not notes and get_setting("watchtime-status:1") == "ok", "3 Google hiccups in a row: no alert, status stays ok")
stats.sync_all_watch_time()
ok(len(notes) == 1 and get_setting("watchtime-status:1").startswith("error"), "4th in a row: shown once it really lasts")
stats.sync_watch_time = lambda did: {}
stats.sync_all_watch_time()
ok(get_setting("watchtime-status:1") == "ok" and get_setting("watchtime-fails:1") == "0", "works again: counter back to 0")
def other(did): raise Exception("KeyError: 'day'")
stats.sync_watch_time = other; notes.clear(); stats.sync_all_watch_time()
ok(len(notes) == 1, "a real error of ours is still shown at once")

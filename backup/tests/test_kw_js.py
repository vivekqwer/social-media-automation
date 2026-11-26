"""Dashboard side of the keyword helper: script syntax, and the editor's counters against the server's."""
import json, os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
d = __import__("tempfile").mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "kw-js-pass-12"
sys.path.insert(0, r"D:\integ-final")
from app import metadata as M
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
r = subprocess.run([sys.executable, os.path.join(here, "_extract_js.py")], capture_output=True, text=True)
ok("ID PROBLEM" not in r.stdout and "none" in r.stdout, "every keyword element exists once in the page; no stray bytes: " + r.stdout.strip().replace("\n", " | ")[:120])
n = subprocess.run(["node", "--check", os.path.join(here, "dash_c.js")], capture_output=True, text=True)
ok(n.returncode == 0, "dashboard script has no syntax error " + n.stderr[:200])
lists = [[], ["a"], ["a b", "c"], ["butter chicken recipe", "shorts", "Chef Asha"], ["बटर चिकन", "रेसिपी"], ["x" * 30, "y z " * 5 + "q"]]
json.dump([{"tags": t, "n": M.tag_chars(t)} for t in lists], open(os.path.join(here, "parity_cases.json"), "w", encoding="utf-8"), ensure_ascii=False)
j = subprocess.run(["node", os.path.join(here, "js_parity.js")], capture_output=True, text=True, encoding="utf-8")
print(j.stdout.strip() or ("FAIL the page functions could not be run: " + j.stderr[-400:]), flush=True)

"""The Android phone helper (worker/phone/phone.py) and what the server hands it."""
import ast, os, shutil, subprocess, sys, tempfile, types
from datetime import timedelta
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-phone-pass-73"
ROOT = r"D:\phone-helper"
sys.path.insert(0, ROOT)
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
PHONE = os.path.join(ROOT, "worker", "phone", "phone.py")
SETUP = os.path.join(ROOT, "worker", "phone", "setup.sh")
FF = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")

# ---------------------------------------------------------------- the file itself: small, few imports, no server code
src = open(PHONE, encoding="utf-8").read()
mods = set()
for n in ast.walk(ast.parse(src)):
    if isinstance(n, ast.Import):
        mods |= {a.name.split(".")[0] for a in n.names}
    elif isinstance(n, ast.ImportFrom):
        mods.add((n.module or "").split(".")[0])
extra = sorted(m for m in mods if m not in sys.stdlib_module_names)
ok(extra == ["httpx", "yt_dlp"], f"phone.py needs only the standard library + httpx + yt-dlp ({extra})")
ok(not mods & {"app", "numpy", "whisper", "faster_whisper", "sqlalchemy", "fastapi"}, "no server code, numpy, Whisper or SQLAlchemy")
ok(os.path.getsize(PHONE) < 30 * 1024, f"one small file ({os.path.getsize(PHONE) // 1024} KB)")

# ---------------------------------------------------------------- server side: what a phone is given
from fastapi.testclient import TestClient
from app import main, workers, acl
from app.db import SessionLocal, Clip, Project, Channel, Worker, set_setting, utcnow
main.executor.submit = lambda *a, **k: None
PT, LT = "phone-token-" + "p" * 40, "laptop-token-" + "l" * 40
with TestClient(main.app, raise_server_exceptions=False) as c:
    with SessionLocal() as s:
        s.add(Worker(name="Android phone", token_hash=workers.token_hash(PT), approved=True, last_seen=utcnow()))
        s.add(Worker(name="laptop", token_hash=workers.token_hash(LT), approved=True, last_seen=utcnow() - timedelta(hours=1)))
        ch = Channel(kind="reel", query="q", auto_scripts=True, script_wanted={"topic": "street food tips"}); s.add(ch); s.commit()
        reel = Project(url="u", video_id="", title="reel", mode="original", description="a script " * 10, channel_ref=ch.id, duration=30)
        vid = Project(url="u", video_id="abcdefghijk", title="t", mode="cc", duration=600, quick={"captions": True})
        ana = Project(url="u", video_id="analyzeme01", title="a", mode="cc", duration=600, analysis_pending=True)
        s.add_all([reel, vid, ana]); s.commit()
        s.add(Clip(project_id=reel.id, start=0, end=30, aspect="9:16", status="fetching"))
        s.add(Clip(project_id=vid.id, start=10, end=40, aspect="9:16", status="fetching"))  # no captions: would want Whisper
        s.add(Clip(project_id=vid.id, start=100, end=130, aspect="9:16", status="draft", tts_pending='{"text": "hi", "voice": "x"}'))
        s.commit()
    set_setting("ai_wanted", "1")
    H = {"x-worker-token": PT, "x-worker-can": "source,analyze", "x-worker-slots": "3", "x-worker-llm": "1", "x-worker-ai": "-"}
    jobs = c.get("/api/worker/jobs", headers=H).json()["jobs"]
    kinds = sorted(j["type"] for j in jobs)
    ok(kinds == ["analyze", "source"], f"the phone gets only source + analyze, never reel / tts / script / setup_ai ({kinds})")
    srcj = next(j for j in jobs if j["type"] == "source")
    ok("transcribe" not in srcj and "hint" not in srcj, "helper PC not seen for an hour: the phone takes it without transcribe (goes up without captions)")
    with SessionLocal() as s:
        w = s.query(Worker).filter(Worker.name == "Android phone").first()
        ok(w.can == "source,analyze", f"the server remembers what the phone can do ({w.can})")
        rc = s.query(Clip).filter(Clip.project_id == reel.id).first()
        ok(rc.fetch_tries in (None, 0) and rc.job_lease is None, "the reel it cannot make is not leased and uses up no try")
        for cl in s.query(Clip): cl.job_lease = None
        s.query(Project).filter(Project.id == ana.id).first().analysis_lease = None
        ch2 = s.get(Channel, ch.id); ch2.script_wanted = {"topic": "street food tips"}
        s.commit()
    with SessionLocal() as s:  # the helper PC (which writes captions) was seen 5 minutes ago
        s.query(Worker).filter(Worker.name == "laptop").first().last_seen = utcnow() - timedelta(minutes=5)
        s.add(Clip(project_id=vid.id, start=200, end=230, aspect="9:16", status="fetching", captions=[{"s": 0, "e": 1, "t": "x"}]))
        s.commit()
    pj = c.get("/api/worker/jobs", headers=H).json()["jobs"]
    ok([(j["type"], j.get("start")) for j in pj] == [("source", 200)],
       f"helper PC seen lately: the phone leaves clips and analyses that want captions to it, takes the one that has them ({[(j['type'], j.get('start')) for j in pj]})")
    with SessionLocal() as s:
        ok(s.query(Clip).filter(Clip.start == 10).first().job_lease is None, "…without leasing them or using a try")
        for cl in s.query(Clip): cl.job_lease = None
        s.query(Clip).filter(Clip.start == 200).first().status = "failed"
        s.commit()
    lj = c.get("/api/worker/jobs", headers={"x-worker-token": LT, "x-worker-llm": "1", "x-worker-ai": "-"}).json()["jobs"]
    lk = sorted(j["type"] for j in lj)
    ok(lk == ["analyze", "reel", "script", "source", "tts"] and next(j for j in lj if j["type"] == "source").get("transcribe"),
       f"the Windows helper (no x-worker-can) still gets everything, captions included ({lk})")
    with SessionLocal() as s:
        ok(s.query(Worker).filter(Worker.name == "laptop").first().can is None, "an older helper is remembered as 'can do all, no tickets'")
    ok(sorted(j["type"] for j in c.get("/api/worker/jobs", headers={**H, "x-worker-slots": "0"}).json()["jobs"]) == [], "slots 0: a heartbeat only")

    # public, read-only file routes serve the repository files byte for byte
    r1, r2 = c.get("/helper/phone.py"), c.get("/helper/setup.sh")
    ok(r1.status_code == 200 and r1.content == open(PHONE, "rb").read(), "GET /helper/phone.py serves the repo file byte-identical, no sign-in")
    ok(r2.status_code == 200 and r2.content == open(SETUP, "rb").read(), "GET /helper/setup.sh serves the repo file byte-identical")
    ok(c.post("/helper/phone.py").status_code in (404, 405), "read-only")
    ok(all(c.get(u, follow_redirects=False).status_code != 200 for u in ("/helper/../app/config.py", "/helper/%2e%2e/app/config.py",
                                                                         "/helper/x.py", "/helper/README.md")), "no other files are served")

# routes are classified
R = {(m, p): rule for m, p, rule, _t in acl.ROUTES}
ok(R.get(("GET", "/helper/phone.py")) == acl.ANYONE and R.get(("GET", "/helper/setup.sh")) == acl.ANYONE, "the two files are 'anyone' in acl.ROUTES")
ok(R.get(("POST", "/api/admin/workers/{wid}/central")) == acl.SUPER, "marking central is Super Admin only in acl.ROUTES")
ok({"/helper/phone.py", "/helper/setup.sh"} <= main.PUBLIC_PATHS, "…and public in the middleware")

# ---------------------------------------------------------------- setup.sh
sh = open(SETUP, "rb").read()
ok(b"\r" not in sh and sh.startswith(b"#!"), "setup.sh has Unix line ends (bash on the phone)")
t = sh.decode()
for need in ("pkg install -y python ffmpeg", "pip install --upgrade --quiet httpx yt-dlp", "termux-wake-lock", ".termux/boot",
             "start-helper.sh", "/helper/phone.py", "Unrestricted" if False else "battery"):
    ok(need in t, f"setup.sh: {need}")
bash = shutil.which("bash")
if bash:
    r = subprocess.run([bash, "-n", SETUP], capture_output=True, text=True)
    ok(r.returncode == 0, "bash -n setup.sh: no syntax errors " + r.stderr[:200])
    r = subprocess.run([bash, SETUP, "http://evil.example"], capture_output=True, text=True, timeout=30)
    ok(r.returncode == 1 and "https://" in r.stdout, "setup.sh refuses a server that is not https, with a plain message")
ok('"${2' not in t and "/dev/tty" in t and "helper_code.txt" in t, "the helper code is typed at a prompt (or a file), never on the command line")
ok("1800" in t and "wait_s * 2" in t and "last_pip" in t, "start.sh: longer waits after repeated crashes (max 30 min); no yt-dlp update on a quick restart")
readme = open(os.path.join(ROOT, "worker", "phone", "README.md"), encoding="utf-8").read()
ok("F-Droid" in readme and "Termux:Boot" in readme and "battery" in readme.lower() and "charger" in readme, "README: F-Droid, Termux:Boot, battery, charger")
page = open(os.path.join(ROOT, "static", "index.html"), "rb").read()
ok(b"Android phone" in page and b"/helper/setup.sh" in page and b"\x08" not in page, "dashboard: Android phone tab with the command; no 0x08 bytes")

# ---------------------------------------------------------------- the phone helper itself, yt-dlp and httpx faked
home = tempfile.mkdtemp()
os.environ["RAJI_HELPER_HOME"] = home; os.environ["RAJI_SERVER"] = "https://srv.example"
sys.argv = ["phone.py"]
import importlib.util
spec = importlib.util.spec_from_file_location("phone", PHONE); phone = importlib.util.module_from_spec(spec); spec.loader.exec_module(phone)
ok(phone.SERVER == "https://srv.example" and phone.CAN == "source,analyze" and phone.PARALLEL == 2, "server from settings; tells the server source,analyze; 2 downloads side by side")
ok(phone.pair_code("abc") == workers.pair_code("abc"), "same pairing code as the server expects")

SAMPLE = os.path.join(home, "sample.mp4")
subprocess.run([FF, "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-f", "lavfi", "-i", "sine=frequency=440",
                "-t", "32", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", SAMPLE], capture_output=True)
SHORT = os.path.join(home, "short.mp4")
subprocess.run([FF, "-y", "-i", SAMPLE, "-t", "5", "-c", "copy", SHORT], capture_output=True)
ok(phone.stream_seconds(phone.pathlib.Path(SAMPLE), "v:0") > 30, "real ffmpeg measures the sample")

calls, posts = [], []


class FakeYDL:
    mode = "ok"

    def __init__(self, opts): self.o = opts; calls.append(opts)
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def extract_info(self, url, download=False):
        if not download:
            return {"heatmap": [{"start_time": 0, "end_time": 10, "value": 1.0}], "comments": [{"text": "1:23 wow"}, {"text": "nice"}]}
        if FakeYDL.mode == "block":
            raise Exception("ERROR: Sign in to confirm you're not a bot")
        out = self.o["outtmpl"].replace("%(ext)s", "mp4")
        shutil.copy(SHORT if FakeYDL.mode == "short" else SAMPLE, out)
        return {"requested_downloads": [{"filepath": out}], "duration": 600}


class Resp:
    def __init__(self, code=200, data=None): self.status_code, self._d = code, data or {"ok": True}; self.content = b""; self.headers = {}
    def json(self): return self._d
    def raise_for_status(self):
        if self.status_code >= 400: raise phone.httpx.HTTPStatusError("x", request=None, response=self)


def fake_post(url, headers=None, **kw):
    f = (kw.get("files") or {}).get("file")
    posts.append({"url": url, "headers": dict(headers or {}), "json": kw.get("json"), "bytes": f[1].read() if f else None})
    if url.endswith("/api/worker/register"):
        return Resp(200, {"token": "new-phone-token-" + "z" * 40})
    return Resp()


phone.yt_dlp = types.SimpleNamespace(YoutubeDL=FakeYDL)
phone.httpx = types.SimpleNamespace(post=fake_post, get=None, HTTPError=Exception, TransportError=ConnectionError,
                                    HTTPStatusError=type("HSE", (Exception,), {"__init__": lambda self, m, request=None, response=None: setattr(self, "response", response)}))
hdr = {"x-worker-token": "tok"}
phone.handle({"type": "source", "clip_id": 7, "video_id": "abcdefghijk", "start": 10.0, "end": 40.0, "ticket": "TICKET-A"}, hdr)
p = [x for x in posts if "/source/" in x["url"]]
ok(len(p) == 1 and p[0]["url"] == "https://srv.example/api/worker/source/7", "download, then POST to /api/worker/source/7")
ok(p and p[0]["headers"].get("x-job-ticket") == "TICKET-A" and p[0]["headers"].get("x-worker-token") == "tok", "the ticket goes back unchanged with the token")
ok(p and p[0]["bytes"] == open(SAMPLE, "rb").read(), "the downloaded section is what is sent")
o = calls[-1]
ok(o["format"] == "bv*[height<=1080]+ba/b[height<=1080]/b" and o["force_keyframes_at_cuts"] and o["merge_output_format"] == "mp4"
   and o["allowed_extractors"] == ["(?i)youtube.*"] and "download_ranges" in o and o["ffmpeg_location"] == FF, "same yt-dlp options as the server's download_section")
ok(not os.listdir(os.path.join(home, "tmp")), "nothing is left on the phone after sending")

posts.clear(); FakeYDL.mode = "short"
phone.handle({"type": "source", "clip_id": 8, "video_id": "abcdefghijk", "start": 10.0, "end": 40.0}, hdr)
ok(not [x for x in posts if "/source/" in x["url"]] and [x for x in posts if x["url"].endswith("/api/worker/fail/8")], "a cut-short download is never sent; the server is told it failed")
f = [x for x in posts if x["url"].endswith("/fail/8")]
ok(f and f[0]["json"]["kind"] == "source" and "incompletely" in f[0]["json"]["error"], "…with a clear reason")

posts.clear(); FakeYDL.mode = "block"
phone.handle({"type": "source", "clip_id": 9, "video_id": "abcdefghijk", "start": 10.0, "end": 40.0, "ticket": "T9"}, hdr)
f = [x for x in posts if x["url"].endswith("/fail/9")]
ok(f and "not a bot" in f[0]["json"]["error"] and f[0]["headers"].get("x-job-ticket") == "T9", "a YouTube block is reported (with the ticket)")

posts.clear(); FakeYDL.mode = "ok"
phone.handle({"type": "analyze", "project_id": 3, "video_id": "analyzeme01", "captions": True, "ticket": "T3"}, hdr)
a = [x for x in posts if x["url"].endswith("/api/worker/analysis/3")]
ok(a and a[0]["json"]["heatmap"] and a[0]["json"]["comments"] == ["1:23 wow", "nice"] and a[0]["headers"]["x-job-ticket"] == "T3", "analyze: replays + comments in one call, posted back")
posts.clear()
for kind in ("reel", "tts", "script", "setup_ai"):
    phone.handle({"type": kind, "clip_id": 1}, hdr)
ok(posts == [], "jobs it cannot do are never attempted")

# the loop: registers, tells what it can do and how many places are free, restarts for an update when idle
gets = []


def fake_get(url, headers=None, **kw):
    gets.append({"url": url, "headers": dict(headers or {})})
    if len(gets) == 1:
        return Resp(200, {"jobs": [], "version": "v1"})
    return Resp(200, {"jobs": [], "version": "v2"})


phone.httpx.get = fake_get
phone.POLL_SECONDS = 0.01
phone.LOCK_PORT = 47699
code = None
try:
    phone.main()
except SystemExit as e:
    code = e.code
tokf = os.path.join(home, ".worker_token")
ok(os.path.exists(tokf) and open(tokf).read().startswith("new-phone-token-"), "first start registers and keeps the token")
ok(gets and gets[0]["headers"].get("x-worker-can") == "source,analyze" and gets[0]["headers"].get("x-worker-slots") == "2", "asks for jobs with x-worker-can source,analyze and 2 free places")
ok(code == phone.EXIT_REFRESH == 4, f"server on new code and nothing running: exits 4 so start.sh fetches the newest phone.py ({code})")
log = open(os.path.join(home, "helper.log"), encoding="utf-8").read()
ok("pairing code: " + phone.pair_code(open(tokf).read().strip()) in log, "the pairing code is in the rotating log")
for h in list(phone.log.handlers):
    phone.log.removeHandler(h); h.close()

"""Raji Studio helper PC.

Runs on your own computer (home internet, which YouTube does not bot-block) and does the two jobs the
server cannot: download the needed section of a video, and generate voice-overs. Rendering and uploading
still happen on the server.

Start it with run_worker.bat (Windows) or:  python worker/clipstudio_worker.py [SERVER_URL]
The first start registers this PC. With the account's helper key (worker/enroll_key.txt, written by the dashboard's
install command or the helper ZIP, or handed over by the server to an approved helper) it joins by itself, nothing to
approve; without it, approve it in the dashboard under My channels -> Helper PC with its pairing code.
"""
import errno
import hashlib
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", str(ROOT / "data" / "worker"))

import httpx  # noqa: E402

from app.ingest import download_section, friendly_error  # noqa: E402
from app.tts import synth  # noqa: E402
from app.captions import fetch_track  # noqa: E402
from app.hot import _comments_ytdlp, fetch_heatmap  # noqa: E402

DEFAULT_SERVER = "https://social-media-automation-production-f5543fa3.up.railway.app"
_server_file = pathlib.Path(__file__).with_name("server.txt")  # written into a downloaded helper by its dashboard
if _server_file.exists():
    DEFAULT_SERVER = _server_file.read_text(encoding="utf-8-sig").strip() or DEFAULT_SERVER
SERVER = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("CLIPSTUDIO_URL", DEFAULT_SERVER)).rstrip("/")
if not (SERVER.startswith("https://") or re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", SERVER)):
    sys.exit("The server address must start with https:// (the helper sends videos and its key there).")
TOKEN_FILE = pathlib.Path(__file__).with_name(".worker_token")
ENROLL_FILE = pathlib.Path(__file__).with_name("enroll_key.txt")  # the account's helper key: join without approval
HELPER_KEY_RE = re.compile(r"rk\d{1,9}\.[0-9a-f]{8}\.[A-Za-z0-9_-]{43}")
# ReelGen (text -> reel engine) lives in its own project folder on this PC
REELGEN_DIR = pathlib.Path(os.getenv("CLIPSTUDIO_REELGEN", r"D:\video generator"))
POLL_SECONDS = 8
MAX_BACKOFF = 300  # the server answers with an error: wait longer each time, up to 5 minutes
NET_PROBE = 30  # no connection (offline, asleep, Wi-Fi changed): look every 30 s and call the server as soon as it is back
PARALLEL = 3  # video downloads at the same time (YouTube slows each single download down to about playing speed)
UPDATE_EVERY = 600  # seconds between "is the server on newer code?" checks
EXPECTED_ORIGIN = "github.com/vivekqwer/social-media-automation"  # only ever update from this repository
EXIT_UPDATED, EXIT_ALREADY_RUNNING = 4, 5  # the launchers restart on 4 and stop on 5
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}  # never wait for a hidden prompt


def log(msg: str):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(line.encode(enc, "replace").decode(enc), flush=True)  # Hindi text must never crash a job on a cp1252 console


def single_instance():
    """Only one helper per account on a PC (two would download the same clip twice). Helpers of different
    accounts (each has its own code in helper_code.txt) may run side by side."""
    port = 47613
    code_file = pathlib.Path(__file__).with_name("helper_code.txt")
    try:
        if code_file.exists():
            port += 1 + int(code_file.read_text(encoding="utf-8-sig").strip().split("-", 1)[0]) % 1000  # 47614-48613
    except (ValueError, OSError):
        pass
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", port))
    except OSError as e:
        if e.errno == errno.EADDRINUSE or getattr(e, "winerror", 0) == 10048:
            log("Another Raji Studio helper is already running on this PC. Closing this one.")
            sys.exit(EXIT_ALREADY_RUNNING)
        log(f"Could not take the single-helper lock ({e}); continuing.")  # e.g. Windows reserved that port today
    return lock


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=120, env=GIT_ENV)


try:  # the code version this process started with
    STARTED_AT = _git("rev-parse", "HEAD").stdout.strip()
except Exception:  # noqa: BLE001
    STARTED_AT = ""


ZIP_ALLOWED = re.compile(r"(app/[a-z_]+\.py|worker/[a-z_]+\.(py|bat|txt)|helper_version\.txt|Start helper\.bat)")
ZIP_KEEP = {"worker/helper_code.txt", "worker/server.txt", "worker/.worker_token", "worker/enroll_key.txt"}  # this PC's own settings
WIN_DEVICES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
MAX_UPDATE_BYTES = 5 * 1024 * 1024


def update_from_server(server_version: str):
    """A downloaded helper (no Git): fetch the current helper files from its own server, then restart (exit 4)."""
    vfile = ROOT / "helper_version.txt"
    have = vfile.read_text(encoding="utf-8-sig").strip() if vfile.exists() else ""
    if have == server_version:
        return
    import io
    import zipfile

    try:
        r = httpx.get(f"{SERVER}/api/worker/update", headers={"x-worker-token": get_token()}, timeout=120)
        if r.status_code != 200 or len(r.content) > MAX_UPDATE_BYTES:
            return
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            names = [i.filename for i in infos]
            bad = (not names or any(not ZIP_ALLOWED.fullmatch(n) for n in names)
                   or any(n.rsplit("/", 1)[-1].split(".")[0].lower() in WIN_DEVICES for n in names)
                   or sum(i.file_size for i in infos) > MAX_UPDATE_BYTES)
            if bad:
                log("Update skipped: the server sent unexpected files.")
                return
            names.sort(key=lambda n: n == "helper_version.txt")  # the version file last: it marks a complete update
            for n in names:
                if n.lower() in ZIP_KEEP:
                    continue  # this PC's own settings stay as they are
                dest = ROOT / n
                if n.endswith(".bat") and dest.exists():
                    continue  # a start script that is running right now must not be swapped under it
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".new")
                tmp.write_bytes(z.read(n))
                os.replace(tmp, dest)
        log("Updated the helper from the server. Restarting...")
        sys.exit(EXIT_UPDATED)
    except (httpx.HTTPError, OSError, ValueError, zipfile.BadZipFile) as e:
        log(f"Update check failed ({str(e)[:80]}); trying again later.")


def maybe_update(server_version: str):
    """Move the helper's code to exactly the commit the server runs (fast-forward only), then restart (exit 4).
    The code always comes from GitHub over HTTPS; the server only says which commit."""
    if not re.fullmatch(r"[0-9a-f]{40}", server_version or ""):
        return
    if not (ROOT / ".git").exists():
        return update_from_server(server_version)
    try:
        if EXPECTED_ORIGIN not in _git("remote", "get-url", "origin").stdout:
            return
        head = _git("rev-parse", "HEAD").stdout.strip()
        if head == server_version:
            if STARTED_AT and head != STARTED_AT:  # the folder already has the new code, but this process runs the old
                log("New helper code is in the folder. Restarting...")
                sys.exit(EXIT_UPDATED)
            return  # up to date
        if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            return  # local edits we must not touch
        if _git("fetch", "--quiet", "origin", "main").returncode:
            return
        ahead = _git("merge-base", "--is-ancestor", "HEAD", server_version).returncode == 0
        published = _git("merge-base", "--is-ancestor", server_version, "origin/main").returncode == 0
        if not (ahead and published):
            return  # only move forward, and only to commits that are on GitHub's main branch
        if _git("merge", "--ff-only", "--quiet", server_version).returncode or \
                _git("rev-parse", "HEAD").stdout.strip() != server_version:
            log("Update skipped (could not move to the server's version).")
            return
        log("Updated the helper to the server's version. Restarting...")
        sys.exit(EXIT_UPDATED)  # the launcher installs any new requirements, then starts it again
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return


def pair_code(token: str) -> str:
    return str(int(hashlib.sha256(("pair:" + token).encode()).hexdigest(), 16) % 1_000_000).zfill(6)


def enroll_key() -> str:
    """The account's helper key (CLIPSTUDIO_KEY or worker/enroll_key.txt), or "" when there is none."""
    key = os.getenv("CLIPSTUDIO_KEY", "").strip()
    if not key and ENROLL_FILE.exists():
        try:
            key = ENROLL_FILE.read_text(encoding="utf-8-sig").strip()
        except OSError:
            key = ""
    return key if HELPER_KEY_RE.fullmatch(key) else ""


def key_id() -> str:
    """The public part of the key this PC has (the server hands over a newer key when it differs)."""
    key = enroll_key()
    return key.split(".")[1] if key else "none"


def save_enroll_key(key: str):
    """Keep the key the server handed over (never written to the log)."""
    if not HELPER_KEY_RE.fullmatch(key or "") or key == enroll_key():
        return
    try:
        tmp = ENROLL_FILE.with_suffix(".tmp")
        tmp.write_text(key, encoding="utf-8")
        os.replace(tmp, ENROLL_FILE)
        log("Saved this account's helper key: if the server ever forgets this PC, it joins again by itself.")
    except OSError as e:
        log(f"Could not save the helper key ({str(e)[:80]}).")


def get_token(force_new: bool = False) -> str:
    if TOKEN_FILE.exists() and not force_new:
        tok = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        if len(tok) >= 40:
            return tok
    old = TOKEN_FILE.read_text(encoding="utf-8-sig").strip() if TOKEN_FILE.exists() else ""
    # A customer's helper carries the code from their dashboard (My channels -> Helper PC), in the file
    # worker\helper_code.txt or the CLIPSTUDIO_CLAIM setting; the system owner's own helper needs none.
    claim = os.getenv("CLIPSTUDIO_CLAIM", "").strip()
    code_file = pathlib.Path(__file__).with_name("helper_code.txt")
    if not claim and code_file.exists():
        claim = code_file.read_text(encoding="utf-8-sig").strip()
    body, r, key = {"name": socket.gethostname()}, None, enroll_key()
    if key:  # the helper key: approved at once. The old token's hash lets the server refuse a helper it removed.
        h = {"x-helper-key": key}
        if old:
            h["x-old-helper"] = hashlib.sha256(old.encode()).hexdigest()
        r = httpx.post(f"{SERVER}/api/worker/register", json=body, timeout=30, headers=h)
        if r.status_code in (401, 403):
            try:
                why = str(r.json().get("detail", ""))[:200]
            except Exception:  # noqa: BLE001
                why = ""
            log(f"The helper key was not accepted ({why or r.status_code}). Registering with a pairing code instead.")
            r = None
    if r is None:
        r = httpx.post(f"{SERVER}/api/worker/register", json=body, timeout=30,
                       headers={"x-helper-claim": claim} if claim else {})
        if r.status_code == 403:
            sys.exit("The helper code is not valid. Copy it again from the dashboard (My channels -> Helper PC) into "
                     "worker/helper_code.txt and start the helper again.")
    r.raise_for_status()
    data = r.json()
    tok = data["token"]
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(tok, encoding="utf-8")
    os.replace(tmp, TOKEN_FILE)
    if data.get("approved"):
        log("Connected this PC with the helper key - nothing to approve.")
    else:
        log("Registered this PC. Approve it in the dashboard: My channels -> Helper PC -> Approve, "
            f"and type this pairing code: {pair_code(tok)}")
    return tok


def network_up() -> bool:
    """A cheap check that the server can be reached at all (name lookup + connect, nothing sent)."""
    from urllib.parse import urlparse

    u = urlparse(SERVER)
    try:
        socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=5).close()
        return True
    except OSError:
        return False


def is_offline(e: Exception) -> bool:
    """No answer at all (no network, PC just woke up, server unreachable), as opposed to an answer with an error."""
    return isinstance(e, (getattr(httpx, "TransportError", OSError), OSError))


def pause(seconds: float, offline: bool = False):
    """Wait before the next call. While offline, look at the network every NET_PROBE seconds and return as soon as
    it is back, so after sleep / wake or a network change the helper calls in within about 30 s, not after the back-off.
    A jump of the clock (the PC slept) counts as time waited."""
    if not offline:
        time.sleep(seconds)
        return
    waited = 0.0
    while waited < seconds:
        step = min(NET_PROBE, seconds - waited)
        t0 = time.time()
        time.sleep(step)
        waited += max(step, time.time() - t0)
        if network_up():
            return


def _post(url: str, headers: dict, tries: int = 3, **kw):
    """POST with a few retries; any refusal raises, so the server hears about the failure."""
    for i in range(tries):
        try:
            for f in (kw.get("files") or {}).values():
                f[1].seek(0)
            r = httpx.post(url, headers=headers, **kw)
            if r.status_code == 409:  # the server no longer needs it (done elsewhere / cancelled)
                return r
            r.raise_for_status()
            return r
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500 or i == tries - 1:
                raise
            time.sleep(10 * (i + 1))


def analyze(job: dict, headers: dict):
    pid, vid = job["project_id"], job["video_id"]
    log(f"video {vid}: reading replays, comments and captions")
    try:
        heat = fetch_heatmap(vid)
        try:
            comments = _comments_ytdlp(vid, 300)
        except Exception:  # noqa: BLE001
            comments = []
        track = None
        if job.get("captions"):
            try:
                track = fetch_track(vid)
            except Exception:  # noqa: BLE001  (video has no captions)
                track = None
        r = _post(f"{SERVER}/api/worker/analysis/{pid}", headers, timeout=120,
                  json={"heatmap": heat, "comments": comments[:3000], "caption_track": track})
        log(f"video {vid}: {len(heat)} replay points, {len(comments)} comments, captions {'yes' if track else 'no'} -> {r.status_code}")
    except Exception as e:  # noqa: BLE001
        log(f"video {vid}: analysis failed: {str(e)[:150]}")
        _tell_failed(pid, "analyze", str(e)[:300], headers)


LLM_URL = os.getenv("CLIPSTUDIO_LLM_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.getenv("CLIPSTUDIO_LLM_MODEL", "qwen2.5:3b")


def llm_ready() -> bool:
    """Is a local AI (Ollama) with the model running on this PC?"""
    try:
        tags = httpx.get(f"{LLM_URL}/api/tags", timeout=3).json().get("models", [])
        return any(m.get("name", "").startswith(LLM_MODEL.split(":")[0]) for m in tags)
    except Exception:  # noqa: BLE001
        return False


AI_STATUS = ""  # sent to the server on every poll (ASCII only: it travels in a header)
_ai_thread = None
_ai_last_fail = 0.0


def _ai(msg: str):
    global AI_STATUS
    AI_STATUS = msg.encode("ascii", "ignore").decode()[:190]
    log("local AI: " + AI_STATUS)


def _ollama_exe() -> str | None:
    import shutil

    p = shutil.which("ollama")
    if p:
        return p
    for base in (os.getenv("LOCALAPPDATA", ""), os.getenv("ProgramFiles", "")):
        c = pathlib.Path(base) / "Programs" / "Ollama" / "ollama.exe" if base else None
        if c and c.exists():
            return str(c)
        c = pathlib.Path(base) / "Ollama" / "ollama.exe" if base else None
        if c and c.exists():
            return str(c)
    return None


def _winget_busy() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq winget.exe"], capture_output=True, text=True, timeout=20).stdout
        return "winget.exe" in out.lower()
    except Exception:  # noqa: BLE001
        return False


OLLAMA_SETUP_URL = "https://github.com/ollama/ollama/releases/latest/download/OllamaSetup.exe"  # official release only


def _download_ollama() -> pathlib.Path:
    """Download the official installer in pieces; a dropped connection carries on where it stopped (slow internet)."""
    dest = ROOT / "data" / "worker" / "OllamaSetup.exe.part"
    dest.parent.mkdir(parents=True, exist_ok=True)
    total, fails, last = 0, 0, 0.0
    while True:
        have = dest.stat().st_size if dest.exists() else 0
        if total and have >= total:
            break
        try:
            hdr = {"Range": f"bytes={have}-"} if have else {}
            with httpx.stream("GET", OLLAMA_SETUP_URL, headers=hdr, follow_redirects=True,
                              timeout=httpx.Timeout(60, read=120)) as r:
                if r.status_code == 416:  # already complete
                    break
                r.raise_for_status()
                if r.status_code == 200 and have:  # server ignored the range: start again
                    have = 0
                    dest.unlink(missing_ok=True)
                size = int(r.headers.get("content-length", 0))
                total = have + size if size else total
                if not str(r.url).startswith(("https://github.com/", "https://objects.githubusercontent.com/",
                                              "https://release-assets.githubusercontent.com/")):
                    raise RuntimeError("unexpected download location")
                with open(dest, "ab") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
                        have += len(chunk)
                        if total and time.time() - last > 20:
                            last = time.time()
                            _ai(f"Downloading Ollama: {have * 100 // total}% ({have >> 20} MB of {total >> 20} MB)")
            fails = 0
            if not total:
                break
        except Exception as e:  # noqa: BLE001
            fails += 1
            if fails > 60:
                raise RuntimeError(f"download keeps failing: {str(e)[:80]}")
            _ai(f"Internet dropped while downloading Ollama ({have >> 20} MB saved) - carrying on in 30 s")
            time.sleep(30)
    final = dest.with_suffix("")  # OllamaSetup.exe
    final.unlink(missing_ok=True)
    dest.rename(final)
    return final


def _setup_ai():
    """Install Ollama (winget) and download the model, step by step, so the dashboard can show progress."""
    global _ai_last_fail
    import json as _json

    hidden = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
    try:
        exe = _ollama_exe()
        if not exe:
            if os.name != "nt":
                raise RuntimeError("install Ollama from ollama.com on this PC")
            waited = 0
            while _winget_busy() and not _ollama_exe() and waited < 7200:
                _ai(f"Windows is installing Ollama already ({waited // 60} min so far, slow internet takes longer)")
                time.sleep(60)
                waited += 60
            if not _ollama_exe():
                setup = _download_ollama()
                _ai("Installing Ollama...")
                subprocess.run([str(setup), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], timeout=1800, **hidden)
                for _ in range(30):
                    if _ollama_exe():
                        break
                    time.sleep(2)
                if not _ollama_exe():
                    raise RuntimeError("the Ollama installer did not finish")
                setup.unlink(missing_ok=True)
            exe = _ollama_exe()
        try:
            httpx.get(f"{LLM_URL}/api/tags", timeout=3)
        except Exception:  # noqa: BLE001
            _ai("Starting Ollama...")
            subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden)
            for _ in range(30):
                time.sleep(2)
                try:
                    httpx.get(f"{LLM_URL}/api/tags", timeout=3)
                    break
                except Exception:  # noqa: BLE001
                    pass
        fails = 0
        while not llm_ready():  # Ollama keeps the downloaded part, so each retry carries on where it stopped
            _ai(f"Downloading the AI model {LLM_MODEL} (about 2 GB)...")
            try:
                with httpx.stream("POST", f"{LLM_URL}/api/pull", json={"name": LLM_MODEL, "stream": True},
                                  timeout=httpx.Timeout(60, read=600)) as r:
                    last = 0.0
                    for line in r.iter_lines():
                        if not line:
                            continue
                        ev = _json.loads(line)
                        if ev.get("error"):
                            raise RuntimeError(ev["error"])
                        if ev.get("total") and time.time() - last > 20:
                            last = time.time()
                            _ai(f"Downloading the AI model: {ev.get('completed', 0) * 100 // ev['total']}% "
                                f"({ev.get('completed', 0) >> 20} MB of {ev['total'] >> 20} MB)")
            except Exception as e:  # noqa: BLE001
                fails += 1
                if fails > 60:
                    raise
                _ai(f"Internet dropped while downloading the AI model ({str(e)[:60]}) - carrying on in 30 s")
                time.sleep(30)
        if not llm_ready():
            raise RuntimeError("the model did not load")
        _ai("ready")
    except Exception as e:  # noqa: BLE001
        _ai_last_fail = time.time()
        _ai(f"Install problem: {str(e)[:120]} (tries again in 30 min)")


def start_ai_setup():
    global _ai_thread
    import threading

    if (_ai_thread and _ai_thread.is_alive()) or time.time() - _ai_last_fail < 1800:
        return
    _ai_thread = threading.Thread(target=_setup_ai, daemon=True)
    _ai_thread.start()


HINGLISH_EXAMPLE = (
    "Kya aap jaante ho ki samosa asal mein India ka nahi hai? Ye Central Asia se aaya tha, jahan isse sambosa kehte the. "
    "Pehle isme aloo nahi, keema bhara jaata tha. Aloo wala samosa tab bana jab Portuguese aloo India laaye. "
    "Aaj India mein roz lakhon samose bikte hain. Aapko samosa chutney ke saath pasand hai ya bina? Comment karo aur follow karo!")


def _script_prompt(topic: str, lang: str) -> str:
    rules = ("6 to 8 short, simple sentences. No greeting: the very first sentence is a surprising question about the "
             "topic. Give 3 facts that are well known and "
             "certainly true (if unsure, give a simple tip instead of a fact). End by asking viewers to comment and follow. "
             "No labels like 'Fact one', no hashtags, emojis, quotes or scene directions. Output only the script.")
    if lang == "hi":
        return ("You write YouTube Shorts voice-overs in Hinglish: Hindi words written in English (Roman) letters. "
                "Never use Devanagari script.\n\nExample script about samosa:\n" + HINGLISH_EXAMPLE
                + f"\n\nNow write a new script in the same style about: {topic}.\nRules: " + rules)
    return f"Write a 30-second YouTube Shorts voice-over in simple English about: {topic}.\nRules: " + rules


def _clean_script(raw: str) -> str:
    text = " ".join(line.strip().lstrip("-*0123456789.) ") for line in (raw or "").splitlines() if line.strip())
    text = re.sub(r"(?i)\b(true )?fact (one|two|three|1|2|3)[:.,]\s*|\b(first|second|third|final)(ly)?,\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?i)^(hey|hi|hello|namaste)( [a-z]+){0,2}[!.,]+\s*", "", text)  # no greeting ("Hey you!", "Hi guys,")
    text = re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)  # after removed "First," etc.
    return text[:1].upper() + text[1:]


def write_script(job: dict, headers: dict):
    topic, lang = job["topic"], job.get("lang") or "hi"
    log(f"local AI: writing a script about “{topic}”")
    text = ""
    for attempt in range(2):
        r = httpx.post(f"{LLM_URL}/api/generate", json={"model": LLM_MODEL, "prompt": _script_prompt(topic, lang), "stream": False,
                                                         "options": {"temperature": 0.6, "num_predict": 350}}, timeout=900)
        r.raise_for_status()
        text = _clean_script(r.json().get("response", ""))
        if len(text) >= 80 and not re.search(r"[\u0900-\u097F]", text) or lang != "hi":
            break
        log("local AI: script was not in Roman letters, trying again")
    if len(text) < 40:
        raise RuntimeError("the local AI returned too little text")
    r = _post(f"{SERVER}/api/worker/script/{job['source_id']}", headers, json={"topic": topic, "script": text[:3000]}, timeout=60)
    verdict = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    log(f"local AI: script sent ({len(text)} characters)" + ("" if verdict.get("ok", True) else f" - not used: {verdict.get('why')}"))


def _archive_dir() -> pathlib.Path:
    """Finished videos of uploaded clips (the server deletes its copy once this PC has it). Outside the project folder,
    so updating, re-installing or cleaning the project never touches them. Another place: set RAJI_ARCHIVE_DIR, or
    write the folder into worker\\archive_dir.txt."""
    if os.environ.get("RAJI_ARCHIVE_DIR"):
        return pathlib.Path(os.environ["RAJI_ARCHIVE_DIR"]).expanduser()
    try:
        txt = (ROOT / "worker" / "archive_dir.txt").read_text(encoding="utf-8").strip()
        if txt:
            return pathlib.Path(txt).expanduser()
    except OSError:
        pass
    return pathlib.Path(os.environ.get("USERPROFILE") or pathlib.Path.home()) / "RajiStudioArchive"


ARCHIVE_DIR = _archive_dir()
OLD_ARCHIVE_DIR = ROOT / "archive"  # where the first version kept them: still read (never written)
KEY_RE = re.compile(r"t\d{1,9}-c\d{1,12}")
SAVE_KINDS = ("inventory", "restore", "archive")  # run after every other job of a poll, one at a time


def archive_header() -> str:
    from urllib.parse import quote

    return quote(str(ARCHIVE_DIR))  # headers are ASCII only


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _saved_name(key: str, name: str, sha: str = "") -> str:
    tail = ".source.mp4" if name == "source" else ".mp4"
    return f"{key}-{sha[:12]}{tail}" if sha else f"{key}{tail}"


def _find_saved(key: str, name: str, sha: str) -> pathlib.Path | None:
    """The saved file with exactly this sha256 (a file is never overwritten, so there may be more than one)."""
    for d in (ARCHIVE_DIR, OLD_ARCHIVE_DIR):
        for p in (d / _saved_name(key, name), d / _saved_name(key, name, sha)):
            if p.exists() and _sha256(p) == sha:
                return p
    return None


def send_inventory(headers: dict):
    """Once a day (the server asks): the names of the saved videos this PC still has. Nothing is deleted anywhere."""
    names = set()
    for d in (ARCHIVE_DIR, OLD_ARCHIVE_DIR):
        if d.is_dir():
            names |= {p.name for p in d.iterdir() if p.is_file() and p.suffix == ".mp4" and KEY_RE.match(p.name)}
    r = _post(f"{SERVER}/api/worker/archive-inventory", headers, json={"names": sorted(names)}, timeout=120)
    log(f"archive folder checked: {len(names)} saved video file(s) -> {r.status_code}")


def archive_clip(job: dict, headers: dict):
    """Download an uploaded clip's finished video (and my own original file) into the archive folder, check, confirm."""
    import json

    cid, key = job["clip_id"], job["key"]
    if not KEY_RE.fullmatch(key):
        raise RuntimeError("unexpected clip name")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    shas = {}
    for f in job.get("files") or []:
        name, sha = f.get("name"), str(f.get("sha") or "").lower()
        if name not in ("video", "source") or not re.fullmatch(r"[0-9a-f]{64}", sha):
            continue
        if _find_saved(key, name, sha):
            shas[name] = sha
            continue
        dest = ARCHIVE_DIR / _saved_name(key, name)
        if dest.exists():  # a different file has this name: never overwrite a saved video
            dest = ARCHIVE_DIR / _saved_name(key, name, sha)
        tmp = dest.with_name(dest.name + ".part")
        h = hashlib.sha256()
        with httpx.stream("GET", f"{SERVER}/api/worker/archive/{cid}/{name}", headers=headers,
                          timeout=httpx.Timeout(60, read=300)) as r:
            r.raise_for_status()
            with open(tmp, "wb") as out:
                for chunk in r.iter_bytes(1 << 20):
                    out.write(chunk)
                    h.update(chunk)
        if h.hexdigest() != sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"the {name} file arrived damaged (sha256 differs); it is tried again later")
        os.replace(tmp, dest)
        shas[name] = sha
    info = {**(job.get("info") or {}), "files": {n: {"sha256": s, "file": _find_saved(key, n, s).name} for n, s in shas.items()},
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    side = ARCHIVE_DIR / f"{key}.json"
    tmp = side.with_name(side.name + ".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, side)
    r = _post(f"{SERVER}/api/worker/archived/{cid}", headers, json={"files": shas}, timeout=120)
    log(f"clip {cid}: video saved on this PC ({ARCHIVE_DIR.name}\\{_saved_name(key, 'video')}) -> {r.status_code}")


def restore_clip(job: dict, headers: dict):
    """Send a saved video back to the server ("Get the video back from the PC" in the dashboard)."""
    cid, key = job["clip_id"], job["key"]
    if not KEY_RE.fullmatch(key):
        raise RuntimeError("unexpected clip name")
    found = {}
    for f in job.get("files") or []:
        p = _find_saved(key, f.get("name"), str(f.get("sha") or "").lower())
        if not p:
            raise RuntimeError(f"the saved {f.get('name')} file of this clip is not in {ARCHIVE_DIR}")
        found[f["name"]] = p
    for name, p in found.items():  # one file per request, as the raw body (the server counts it as it arrives)
        for i in range(3):
            try:
                with open(p, "rb") as fh:
                    r = httpx.post(f"{SERVER}/api/worker/restore/{cid}/{name}", content=fh, timeout=900,
                                   headers={**headers, "content-type": "application/octet-stream",
                                            "content-length": str(p.stat().st_size)})
                if r.status_code == 409:
                    break  # no longer needed (or refused): the server says why on the clip
                r.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500 or i == 2:
                    raise
                time.sleep(10 * (i + 1))
        log(f"clip {cid}: saved {name} file sent back to the server -> {r.status_code}")


def handle(job: dict, headers: dict):
    if job["type"] == "setup_ai":
        return start_ai_setup()
    if job["type"] in SAVE_KINDS:
        try:
            if job["type"] == "inventory":
                return send_inventory(headers)
            return archive_clip(job, headers) if job["type"] == "archive" else restore_clip(job, headers)
        except Exception as e:  # noqa: BLE001
            log(f"clip {job.get('clip_id')}: {job['type']} failed: {str(e)[:150]}")
            if job.get("clip_id"):  # (the folder list has no clip: the server asks again tomorrow)
                _tell_failed(job["clip_id"], job["type"], str(e)[:300], headers)
            return None
    if job["type"] not in ("analyze", "script", "source", "reel", "tts"):
        return log(f"unknown job type {job['type']!r} (the helper updates itself soon)")
    if job["type"] == "analyze":
        return analyze(job, headers)
    if job["type"] == "script":
        try:
            return write_script(job, headers)
        except Exception as e:  # noqa: BLE001
            log(f"local AI failed: {str(e)[:150]}")
            return None
    cid = job["clip_id"]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        try:
            if job["type"] == "source":
                log(f"clip {cid}: downloading {job['video_id']} {job['start']:.0f}-{job['end']:.0f}s")
                path = download_section(job["video_id"], job["start"], job["end"], pathlib.Path(d))
                if job.get("transcribe"):  # the video has no subtitles: write captions here (offline Whisper)
                    with _HEAVY:  # one at a time: it uses the whole processor
                        send_captions(job, path, headers)
                with open(path, "rb") as f:
                    r = _post(f"{SERVER}/api/worker/source/{cid}", headers,
                              files={"file": ("source.mp4", f, "video/mp4")}, timeout=900)
                log(f"clip {cid}: video sent ({path.stat().st_size // 1024} KB) -> {r.status_code}; server is rendering")
            elif job["type"] == "reel":
                out = pathlib.Path(d) / "reel.mp4"
                make_reel(job["script"], job.get("voice") or "", out, job.get("pexels_key") or "", job.get("photo_query") or "")
                with open(out, "rb") as f:
                    r = _post(f"{SERVER}/api/worker/source/{cid}", headers,
                              files={"file": ("reel.mp4", f, "video/mp4")}, timeout=900)
                log(f"clip {cid}: reel made and sent ({out.stat().st_size // 1024} KB) -> {r.status_code}")
            elif job["type"] == "tts":
                p = pathlib.Path(d) / "voice.mp3"
                synth(job["text"], job["voice"], str(p))
                with open(p, "rb") as f:
                    r = _post(f"{SERVER}/api/worker/voice/{cid}", headers,
                              files={"file": ("voice.mp3", f, "audio/mpeg")}, timeout=120)
                log(f"clip {cid}: voice-over sent -> {r.status_code}")
        except Exception as e:  # noqa: BLE001
            log(f"clip {cid}: failed: {str(e)[:200]}")
            _tell_failed(cid, job["type"], friendly_error(e), headers)


def send_captions(job: dict, path: pathlib.Path, headers: dict):
    try:
        from app.config import ffmpeg_bin
        from transcribe import cues_for

        lead = job["start"] - int(job["start"])  # the section starts at the whole second before the clip
        log(f"clip {job['clip_id']}: writing captions (offline Whisper)")
        cues, lang = cues_for(path, ffmpeg_bin(), lead, job["end"] - job["start"], job.get("hint", ""))
        r = _post(f"{SERVER}/api/worker/captions/{job['clip_id']}", headers, json={"cues": cues, "lang": lang}, timeout=60)
        log(f"clip {job['clip_id']}: {len(cues)} caption lines ({lang}) -> {r.status_code}")
    except Exception as e:  # noqa: BLE001  (captions are a bonus; the video still goes up)
        log(f"clip {job['clip_id']}: captions skipped: {str(e)[:150]}")


BACKUP_DIR = ROOT / "backups"


def backup_daily(headers: dict):
    """Once a day: save the dashboard's data (not the videos) to backups/ on this PC; keep 14 days."""
    import gzip
    import json

    day = time.strftime("%Y-%m-%d")
    out = BACKUP_DIR / f"clipstudio-{day}.json.gz"
    if out.exists():
        return
    try:
        r = httpx.get(f"{SERVER}/api/worker/backup", headers=headers, timeout=120)
        r.raise_for_status()
        BACKUP_DIR.mkdir(exist_ok=True)
        tmp = out.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(r.json(), f)
        os.replace(tmp, out)
        for old in sorted(BACKUP_DIR.glob("clipstudio-*.json.gz"))[:-14]:
            old.unlink(missing_ok=True)
        log(f"backup saved: {out.name}")
    except Exception as e:  # noqa: BLE001
        log(f"backup skipped: {str(e)[:120]}")


def make_reel(script: str, voice: str, out: pathlib.Path, pexels_key: str = "", photo_query: str = ""):
    """Run ReelGen (the user's text -> 9:16 reel engine) in its own folder."""
    main = REELGEN_DIR / "main.py"
    if not main.exists():
        raise RuntimeError(f"ReelGen not found at {REELGEN_DIR}. Set CLIPSTUDIO_REELGEN to its folder.")
    cmd = [sys.executable, str(main), "--text", script, "-o", str(out)]
    if voice:
        cmd += ["--voice", voice]
    env = {**os.environ, "PYTHONUTF8": "1"}
    if pexels_key and photo_query:  # real stock photos for the backgrounds
        cmd += ["--query", photo_query[:80]]
        env["PEXELS_API_KEY"] = pexels_key
    log(f"making a reel ({len(script)} characters of script)")
    r = subprocess.run(cmd, cwd=REELGEN_DIR, capture_output=True, text=True, timeout=1800, env=env)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError("ReelGen failed: " + (r.stderr or r.stdout)[-300:])


def _tell_failed(id_: int, kind: str, error: str, headers: dict):
    try:
        httpx.post(f"{SERVER}/api/worker/fail/{id_}", headers=headers, json={"kind": kind, "error": error}, timeout=30)
    except httpx.HTTPError:
        pass  # the server re-offers the job after its lease runs out


import threading  # noqa: E402

_HEAVY = threading.Lock()


def handle_all(jobs: list, headers: dict):
    """Video downloads run side by side (they wait on YouTube, not on this PC); everything else one after another."""
    from concurrent.futures import ThreadPoolExecutor

    def one(job):
        try:  # a job of another account (this is the central helper) carries the server's ticket: sent back with the result
            handle(job, {**headers, "x-job-ticket": job["ticket"]} if job.get("ticket") else headers)
        except Exception as e:  # noqa: BLE001  (one job's problem must never stop the others)
            log(f"job failed: {str(e)[:150]}")

    downloads = [j for j in jobs if j.get("type") == "source"]
    others = [j for j in jobs if j.get("type") != "source" and j.get("type") not in SAVE_KINDS]
    others += sorted((j for j in jobs if j.get("type") in SAVE_KINDS), key=lambda j: SAVE_KINDS.index(j["type"]))  # last
    if len(downloads) > 1:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            list(pool.map(one, downloads))
    else:
        for j in downloads:
            one(j)
    for j in others:
        one(j)


def first_token() -> str:
    """The token, registering first if needed; a PC that starts without internet waits for it instead of stopping."""
    delay = POLL_SECONDS
    while True:
        try:
            return get_token()
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            delay = min(delay * 2, MAX_BACKOFF)
            log(f"Could not register yet: {str(e)[:150]} (trying again)")
            pause(delay, offline=is_offline(e))


def main():
    _lock = single_instance()  # noqa: F841  (held for the life of the process)
    tok = first_token()
    headers = {"x-worker-token": tok}
    last_update_check = time.time()  # first check after a while: a fresh start never loops straight into an update
    log(f"Raji Studio helper running for {SERVER}. Keep this window open.")
    waiting, delay, offline = False, POLL_SECONDS, False
    while True:
        try:
            r = httpx.get(f"{SERVER}/api/worker/jobs", headers={**headers, "x-worker-llm": "1" if llm_ready() else "0", "x-worker-ai": AI_STATUS or "-",
                                                                "x-worker-archive": archive_header(), "x-worker-can": "all",
                                                                "x-worker-enroll": key_id()},
                          timeout=30)
            if offline:
                log("Connected again.")
            delay, offline = POLL_SECONDS, False
            if r.status_code == 403:
                if not waiting:
                    log(f"Waiting for approval in the dashboard (My channels -> Helper PC). Pairing code: {pair_code(tok)}")
                    waiting = True
            elif r.status_code == 401:
                log("The server does not know this helper (removed in the dashboard, or its data was restored). Registering again...")
                tok = get_token(force_new=True)
                headers = {"x-worker-token": tok}
                waiting = False
            else:
                r.raise_for_status()
                if waiting:
                    log("Approved. Waiting for jobs...")
                    waiting = False
                data = r.json()
                if data.get("enroll_key"):
                    save_enroll_key(data["enroll_key"])
                handle_all(data.get("jobs", []), headers)
                backup_daily(headers)
                if time.time() - last_update_check > UPDATE_EVERY:
                    last_update_check = time.time()
                    maybe_update(data.get("version", ""))
        except Exception as e:  # noqa: BLE001
            delay = min(delay * 2, MAX_BACKOFF)  # wait longer each time, up to 5 minutes...
            offline = is_offline(e)  # ...but with no connection at all, look every 30 s and call in as soon as it is back
            log(f"connection problem: {str(e)[:150]} (next try in {delay} s"
                + (f", sooner when the network is back)" if offline else ")"))
        pause(delay, offline)


if __name__ == "__main__":
    main()

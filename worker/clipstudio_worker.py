"""Raji Studio helper PC.

Runs on your own computer (home internet, which YouTube does not bot-block) and does the two jobs the
server cannot: download the needed section of a video, and generate voice-overs. Rendering and uploading
still happen on the server.

Start it with run_worker.bat (Windows) or:  python worker/clipstudio_worker.py [SERVER_URL]
The first start registers this PC; approve it in the dashboard under My channels -> Helper PC.
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
SERVER = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("CLIPSTUDIO_URL", DEFAULT_SERVER)).rstrip("/")
if not (SERVER.startswith("https://") or re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", SERVER)):
    sys.exit("The server address must start with https:// (the helper sends videos and its key there).")
TOKEN_FILE = pathlib.Path(__file__).with_name(".worker_token")
# ReelGen (text -> reel engine) lives in its own project folder on this PC
REELGEN_DIR = pathlib.Path(os.getenv("CLIPSTUDIO_REELGEN", r"D:\video generator"))
POLL_SECONDS = 8
UPDATE_EVERY = 600  # seconds between "is the server on newer code?" checks
EXPECTED_ORIGIN = "github.com/vivekqwer/social-media-automation"  # only ever update from this repository
EXIT_UPDATED, EXIT_ALREADY_RUNNING = 4, 5  # the launchers restart on 4 and stop on 5
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}  # never wait for a hidden prompt


def log(msg: str):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(line.encode(enc, "replace").decode(enc), flush=True)  # Hindi text must never crash a job on a cp1252 console


def single_instance():
    """Only one helper per PC (two would download the same clip twice)."""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 47613))
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


def maybe_update(server_version: str):
    """Move the helper's code to exactly the commit the server runs (fast-forward only), then restart (exit 4).
    The code always comes from GitHub over HTTPS; the server only says which commit."""
    if not re.fullmatch(r"[0-9a-f]{40}", server_version or "") or not (ROOT / ".git").exists():
        return
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


def get_token(force_new: bool = False) -> str:
    if TOKEN_FILE.exists() and not force_new:
        tok = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        if len(tok) >= 40:
            return tok
    r = httpx.post(f"{SERVER}/api/worker/register", json={"name": socket.gethostname()}, timeout=30)
    r.raise_for_status()
    tok = r.json()["token"]
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(tok, encoding="utf-8")
    os.replace(tmp, TOKEN_FILE)
    log("Registered this PC. Approve it in the dashboard: My channels -> Helper PC -> Approve, "
        f"and type this pairing code: {pair_code(tok)}")
    return tok


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


def handle(job: dict, headers: dict):
    if job["type"] == "setup_ai":
        return start_ai_setup()
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


def main():
    _lock = single_instance()  # noqa: F841  (held for the life of the process)
    tok = get_token()
    headers = {"x-worker-token": tok}
    last_update_check = time.time()  # first check after a while: a fresh start never loops straight into an update
    log(f"Raji Studio helper running for {SERVER}. Keep this window open.")
    waiting, delay = False, POLL_SECONDS
    while True:
        try:
            r = httpx.get(f"{SERVER}/api/worker/jobs", headers={**headers, "x-worker-llm": "1" if llm_ready() else "0", "x-worker-ai": AI_STATUS or "-"},
                          timeout=30)
            delay = POLL_SECONDS
            if r.status_code == 403:
                if not waiting:
                    log(f"Waiting for approval in the dashboard (My channels -> Helper PC). Pairing code: {pair_code(tok)}")
                    waiting = True
            elif r.status_code == 401:
                log("The server does not know this helper (removed in the dashboard?). Registering again...")
                tok = get_token(force_new=True)
                headers = {"x-worker-token": tok}
                waiting = False
            else:
                r.raise_for_status()
                if waiting:
                    log("Approved. Waiting for jobs...")
                    waiting = False
                data = r.json()
                for job in data.get("jobs", []):
                    handle(job, headers)
                backup_daily(headers)
                if time.time() - last_update_check > UPDATE_EVERY:
                    last_update_check = time.time()
                    maybe_update(data.get("version", ""))
        except Exception as e:  # noqa: BLE001
            delay = min(delay * 2, 300)  # server down: wait longer each time, up to 5 minutes
            log(f"connection problem: {str(e)[:150]} (next try in {delay} s)")
        time.sleep(delay)


if __name__ == "__main__":
    main()

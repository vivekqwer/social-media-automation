"""Raji Studio phone helper: a tiny helper for an Android phone (Termux), always on at home.

YouTube blocks downloads from the server, so a helper on a home connection downloads the needed part of each video
and sends it to the server. This file is the whole helper: Python's standard library + httpx + yt-dlp, and Termux's
ffmpeg. It does two kinds of job: "source" (download a section, send it) and "analyze" (most-replayed graph and
comments). Reels, voice-overs, captions and AI scripts stay with the Windows helper PC.

Start:  python phone.py [SERVER_URL]       (worker/phone/setup.sh sets everything up and starts it on boot)
The first start registers the phone. With the account's helper key (enroll_key.txt, written by the dashboard's setup
command, or handed over by the server once the phone is approved) it joins by itself; without it, approve it in the
dashboard (My channels -> Helper PC) with the pairing code it prints. The Super Admin can then mark it "central": it then downloads for every account without a helper.
"""
import hashlib
import logging
import logging.handlers
import os
import pathlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import yt_dlp
from yt_dlp.utils import download_range_func

HOME = pathlib.Path(os.getenv("RAJI_HELPER_HOME") or pathlib.Path(__file__).resolve().parent)
DEFAULT_SERVER = "https://social-media-automation-production-f5543fa3.up.railway.app"
CAN = "source,analyze"  # told to the server, which then never sends reels, voice-overs, captions or AI scripts
PARALLEL = 2  # downloads side by side
POLL_SECONDS = 20  # polite: one short call every 20 s while idle
BUSY_POLL = 60  # while both places are busy: a call a minute only to say "alive" (it asks for 0 jobs)
MAX_BACKOFF = 300  # the server answers with an error: wait longer each time, up to 5 minutes
MAX_REREGISTER = 1800  # registering again keeps failing: wait longer each time, up to 30 minutes
KEY_RETRY = 1800  # a key that could not be used (too many joins) is tried again after 30 minutes
NET_PROBE = 30  # no connection at all (Wi-Fi off, phone woke up): look every 30 s, call in as soon as it is back
REFRESH_EVERY = 24 * 3600  # once a day (and whenever the server gets new code) start.sh updates yt-dlp and this file
LOCK_PORT = 47623  # one phone helper at a time
EXIT_REFRESH, EXIT_RUNNING, EXIT_STOP = 4, 5, 6  # start.sh: 4 = update and start again, 5 and 6 = stop


def _server() -> str:
    f = HOME / "server.txt"
    saved = f.read_text(encoding="utf-8-sig").strip() if f.exists() else ""
    return (sys.argv[1] if len(sys.argv) > 1 else os.getenv("RAJI_SERVER") or saved or DEFAULT_SERVER).rstrip("/")


SERVER = _server()
TOKEN_FILE = HOME / ".worker_token"
ENROLL_FILE = HOME / "enroll_key.txt"  # the account's helper key: join without approval
REFUSED_FILE = HOME / "enroll_key.refused"  # a key the server refused, set aside
ID_FILE = HOME / "helper_id.txt"  # this install's random id (stays when the token changes)
KEY_RE = re.compile(r"rk\d{1,9}\.[0-9a-f]{8}\.[A-Za-z0-9_-]{43}")

log = logging.getLogger("raji-phone")


def setup_log():
    """A small rotating log next to this file (helper.log, at most about 1.5 MB in all) and the screen."""
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(HOME / "helper.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        log.addHandler(h)


# ---------------------------------------------------------------- downloading (same yt-dlp options as app/ingest.py)
def ffmpeg_bin() -> str:
    exe = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is missing. In Termux run: pkg install ffmpeg")
    return exe


def _js_runtimes() -> dict | None:
    """yt-dlp needs a JavaScript runtime for YouTube's full formats; Termux has node (pkg install nodejs-lts)."""
    if shutil.which("deno"):
        return None  # yt-dlp's default
    if shutil.which("node"):
        return {"node": {}}
    if shutil.which("qjs"):
        return {"quickjs": {}}
    return None


def _opts(extra: dict | None = None) -> dict:
    o = {"quiet": True, "no_warnings": True, "ffmpeg_location": ffmpeg_bin(), "allowed_extractors": ["(?i)youtube.*"],
         "socket_timeout": 30}
    js = _js_runtimes()
    if js:
        o["js_runtimes"] = js
    if extra:
        o.update(extra)
    return o


def stream_seconds(path: pathlib.Path, sel: str) -> float:
    """How many seconds of one stream (e.g. 'a:0', 'v:0') are actually in the file. 0 if absent."""
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path), "-map", "0:" + sel, "-c", "copy", "-f", "null", "-"],
                       capture_output=True, text=True)
    t = re.findall(r"time=(\d+):(\d+):([\d.]+)", r.stderr)
    if not t:
        return 0.0
    h, m, sec = t[-1]
    return int(h) * 3600 + int(m) * 60 + float(sec)


def is_bot_block(err) -> bool:
    m = str(err)
    return "Sign in to confirm" in m or "not a bot" in m


def friendly_error(msg) -> str:
    m = str(msg)
    if is_bot_block(m):
        return "YouTube asked the phone helper to confirm it is not a bot. Restart the phone's Wi-Fi router or try later."
    if "Private video" in m or "Video unavailable" in m or "members-only" in m.lower():
        return "This video is private, removed or members-only, so it can't be used."
    if "Requested format is not available" in m:
        return "YouTube did not offer a downloadable format for this video right now. Try again later."
    return m[-600:]


def download_section(video_id: str, start: float, end: float, out_dir: pathlib.Path) -> pathlib.Path:
    """Download only [start, end] of the video; a cut-short file is never sent (same checks as the server's)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    s, e = int(start), int(end) + 1
    cached = out_dir / f"src_{s}_{e}.mp4"
    if cached.exists():
        return cached
    opts = _opts({
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / f"src_{s}_{e}.%(ext)s"),
        "download_ranges": download_range_func(None, [(s, e)]),
        "force_keyframes_at_cuts": True,
        "external_downloader_args": {"ffmpeg_i": ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                                                  "-rw_timeout", "30000000"]},
    })
    url = f"https://www.youtube.com/watch?v={video_id}"
    info, want = {}, max(1, e - s)
    for attempt in (1, 2, 3):
        if attempt < 3:
            try:
                with yt_dlp.YoutubeDL(opts) as y:
                    info = y.extract_info(url, download=True)
            except Exception as err:  # noqa: BLE001  (short network glitch: try again; real blocks fail at once)
                m = str(err)
                if is_bot_block(err) or "Private video" in m or "Video unavailable" in m or attempt == 2:
                    raise
                continue
            path = pathlib.Path(info["requested_downloads"][0]["filepath"])
            if path != cached:
                path.replace(cached)
        else:  # last resort: whole video, then cut the section here (only for videos up to 30 minutes)
            if not info or int(info.get("duration") or 0) > 1800:
                raise RuntimeError("The video section downloaded incompletely. Please try again.")
            _download_whole_and_cut(url, cached, s, e, out_dir)
        want = max(1, min(e, int(info.get("duration") or e)) - s)  # the clip may run past the end of the video
        if stream_seconds(cached, "v:0") >= want * 0.9:
            break
        cached.unlink(missing_ok=True)  # never send a cut-short clip
        if attempt == 3:
            raise RuntimeError("The video section downloaded incompletely. Please try again.")
    if 0 < stream_seconds(cached, "a:0") < want * 0.9:  # ranged audio downloads sometimes stop early
        try:
            _repair_audio(video_id, cached, s, want, out_dir)
        except Exception:
            cached.unlink(missing_ok=True)
            raise
    return cached


def _download_whole_and_cut(url: str, cached: pathlib.Path, s: int, e: int, out_dir: pathlib.Path):
    opts = _opts({"format": "bv*[height<=1080]+ba/b[height<=1080]/b", "merge_output_format": "mp4",
                  "outtmpl": str(out_dir / "whole.%(ext)s")})
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
        whole = pathlib.Path(info["requested_downloads"][0]["filepath"])
        subprocess.run([ffmpeg_bin(), "-y", "-ss", str(s), "-i", str(whole), "-t", str(e - s), "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(cached)],
                       capture_output=True, text=True)
    finally:  # never leave a whole video on the phone
        for leftover in out_dir.glob("whole.*"):
            leftover.unlink(missing_ok=True)


def _repair_audio(video_id: str, clip_file: pathlib.Path, start: int, length: int, out_dir: pathlib.Path):
    """Fetch the whole audio track (small) and put the right part back under the video."""
    opts = _opts({"format": "ba[ext=m4a]/ba", "outtmpl": str(out_dir / "audio_full.%(ext)s"),
                  "match_filter": lambda i, *_: "too long for an audio repair" if (i.get("duration") or 0) > 3 * 3600 else None})
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        if not info.get("requested_downloads"):
            raise RuntimeError("The sound of this clip downloaded incompletely. Please press Re-download video.")
        audio = pathlib.Path(info["requested_downloads"][0]["filepath"])
        fixed = out_dir / "fixed.mp4"
        r = subprocess.run([ffmpeg_bin(), "-y", "-i", str(clip_file), "-ss", str(start), "-t", str(length), "-i", str(audio),
                            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
                            str(fixed)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("The sound of this clip downloaded incompletely. Please press Re-download video.")
        fixed.replace(clip_file)
    finally:
        for leftover in out_dir.glob("audio_full.*"):
            leftover.unlink(missing_ok=True)


def read_video(video_id: str, limit: int = 300) -> tuple[list, list[str]]:
    """'Most replayed' graph and top comments in one yt-dlp call (comments off or blocked: the graph alone)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        o = _opts({"skip_download": True, "getcomments": True,
                   "extractor_args": {"youtube": {"max_comments": [str(limit), str(limit), "0", "0"], "comment_sort": ["top"]}}})
        with yt_dlp.YoutubeDL(o) as y:
            info = y.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        with yt_dlp.YoutubeDL(_opts({"skip_download": True})) as y:
            info = y.extract_info(url, download=False)
    return info.get("heatmap") or [], [c.get("text", "") for c in (info.get("comments") or [])]


# ---------------------------------------------------------------- talking to the server
def pair_code(token: str) -> str:
    """The 6-digit code typed into the dashboard when approving (same as the helper PC's)."""
    return str(int(hashlib.sha256(("pair:" + token).encode()).hexdigest(), 16) % 1_000_000).zfill(6)


def enroll_key() -> str:
    """The account's helper key (enroll_key.txt, written by setup.sh), or "" when there is none."""
    try:
        key = ENROLL_FILE.read_text(encoding="utf-8-sig").strip() if ENROLL_FILE.exists() else ""
    except OSError:
        key = ""
    return key if KEY_RE.fullmatch(key) else ""


def key_id() -> str:
    key = enroll_key()
    return key.split(".")[1] if key else "none"


def install_id() -> str:
    """A random id for this install (helper_id.txt): a phone removed in the dashboard never joins again by itself."""
    try:
        v = ID_FILE.read_text(encoding="utf-8-sig").strip().lower() if ID_FILE.exists() else ""
    except OSError:
        v = ""
    if not re.fullmatch(r"[0-9a-f]{32}", v):
        v = secrets.token_hex(16)
        try:
            ID_FILE.write_text(v, encoding="utf-8")
        except OSError:
            pass
    return v


def save_enroll_key(key: str):
    """Keep the key the server handed over (never written to the log); only this user may read the file."""
    if not KEY_RE.fullmatch(key or "") or key == enroll_key():
        return
    try:
        ENROLL_FILE.write_text(key, encoding="utf-8")
        try:
            ENROLL_FILE.chmod(0o600)
        except OSError:
            pass
        log.info("Saved this account's helper key: if the server ever forgets this phone, it joins again by itself.")
    except OSError as e:
        log.info("Could not save the helper key (%s).", str(e)[:80])


_KEY_TRY = {"key": "", "at": 0.0}


def key_worth_trying() -> bool:
    key = enroll_key()
    return bool(key) and (key != _KEY_TRY["key"] or time.time() - _KEY_TRY["at"] > KEY_RETRY)


def _detail(r) -> str:
    try:
        return str(r.json().get("detail", ""))[:200]
    except Exception:  # noqa: BLE001
        return ""


def get_token(force_new: bool = False) -> str:
    if TOKEN_FILE.exists() and not force_new:
        tok = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        if len(tok) >= 40:
            return tok
    old = TOKEN_FILE.read_text(encoding="utf-8-sig").strip() if TOKEN_FILE.exists() else ""
    # a customer's phone carries the code from their dashboard (helper_code.txt); the Super Admin's own needs none
    claim = os.getenv("RAJI_HELPER_CODE", "").strip()
    code_file = HOME / "helper_code.txt"
    if not claim and code_file.exists():
        claim = code_file.read_text(encoding="utf-8-sig").strip()
    body, r, key, ident = {"name": "Android phone"}, None, enroll_key(), {"x-helper-id": install_id()}
    if key:  # the helper key: approved at once
        _KEY_TRY.update(key=key, at=time.time())
        h = {**ident, "x-helper-key": key}
        if old:
            h["x-old-helper"] = hashlib.sha256(old.encode()).hexdigest()
        r = httpx.post(f"{SERVER}/api/worker/register", json=body, timeout=30, headers=h)
        customer = not key.startswith("rk1.")
        if r.status_code in (401, 403):  # refused for good: set the key aside
            try:
                os.replace(ENROLL_FILE, REFUSED_FILE)
            except OSError:
                pass
            log.info("The helper key was not accepted (%s). It is set aside (enroll_key.refused); this phone now needs "
                     "approval by a person.", _detail(r) or r.status_code)
            if customer and not claim:  # never fall back into the system owner's copy
                log.info("This key belongs to a customer account and the phone has no helper code. Copy the setup "
                         "command and the key again from the dashboard (My channels -> Helper PC) and run it again.")
                sys.exit(EXIT_STOP)
            r = None
        elif r.status_code == 429:
            log.info("The helper key cannot be used right now (%s). Registering with a pairing code instead; the key is "
                     "tried again later.", _detail(r) or "too many helpers")
            if customer and not claim:
                raise RuntimeError("the helper key cannot be used right now; trying again later")
            r = None
    if r is None:
        r = httpx.post(f"{SERVER}/api/worker/register", json=body, timeout=30,
                       headers={**ident, "x-helper-claim": claim} if claim else ident)
        if r.status_code == 403:
            log.info("The helper code is not valid. Copy it again from the dashboard (My channels -> Helper PC) and run the "
                     "setup command again.")
            sys.exit(EXIT_STOP)
    r.raise_for_status()
    data = r.json()
    tok = data["token"]
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(tok, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, TOKEN_FILE)
    if data.get("approved"):
        log.info("Connected this phone with the helper key - nothing to approve. Waiting for jobs...")
    else:
        log.info("Registered this phone. In the dashboard open My channels -> Helper PC, press Approve and type "
                 "this pairing code: %s", pair_code(tok))
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
    """No answer at all (no network, phone just woke up), as opposed to an answer with an error."""
    return isinstance(e, (getattr(httpx, "TransportError", OSError), OSError))


def _post(url: str, headers: dict, tries: int = 3, **kw):
    """POST with a few retries; a refusal raises, so the server hears about the failure."""
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


def tell_failed(id_: int, kind: str, error: str, headers: dict):
    try:
        httpx.post(f"{SERVER}/api/worker/fail/{id_}", headers=headers, json={"kind": kind, "error": error[:900]}, timeout=30)
    except httpx.HTTPError:
        pass  # the server offers the job again after its lease runs out


def handle(job: dict, headers: dict):
    """One job. A job of another account carries the server's ticket; it goes back with the result, unchanged."""
    if job.get("ticket"):
        headers = {**headers, "x-job-ticket": job["ticket"]}
    kind = job.get("type")
    if kind == "analyze":
        pid, vid = job["project_id"], job["video_id"]
        log.info("video %s: reading replays and comments", vid)
        try:
            heat, comments = read_video(vid)
            r = _post(f"{SERVER}/api/worker/analysis/{pid}", headers, timeout=120,
                      json={"heatmap": heat[:2000], "comments": comments[:3000], "caption_track": None})
            log.info("video %s: %d replay points, %d comments -> %s", vid, len(heat), len(comments), r.status_code)
        except Exception as e:  # noqa: BLE001
            log.info("video %s: analysis failed: %s", vid, str(e)[:150])
            tell_failed(pid, "analyze", str(e)[:300], headers)
        return
    if kind != "source":
        log.info("job type %r is not done on the phone (skipped)", kind)
        return
    cid = job["clip_id"]
    (HOME / "tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=HOME / "tmp") as d:
        try:
            log.info("clip %s: downloading %s %.0f-%.0fs", cid, job["video_id"], job["start"], job["end"])
            path = download_section(job["video_id"], job["start"], job["end"], pathlib.Path(d))
            with open(path, "rb") as f:  # no captions on the phone: the clip goes up without them
                r = _post(f"{SERVER}/api/worker/source/{cid}", headers,
                          files={"file": ("source.mp4", f, "video/mp4")}, timeout=900)
            log.info("clip %s: video sent (%d KB) -> %s", cid, path.stat().st_size // 1024, r.status_code)
        except Exception as e:  # noqa: BLE001
            log.info("clip %s: failed: %s", cid, str(e)[:200])
            tell_failed(cid, "source", friendly_error(e), headers)


def single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        print("The phone helper is already running.")
        sys.exit(EXIT_RUNNING)
    return s


def main():
    if not (SERVER.startswith("https://") or re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", SERVER)):
        print("The server address must start with https://")
        sys.exit(EXIT_STOP)
    setup_log()
    _lock = single_instance()  # noqa: F841  (held for the life of the process)
    (HOME / "tmp").mkdir(exist_ok=True)
    for old in (HOME / "tmp").iterdir():  # parts left by a crash or a power cut
        shutil.rmtree(old, ignore_errors=True)
    ffmpeg_bin()
    delay = POLL_SECONDS
    while True:  # a phone that starts without internet waits for it instead of stopping
        try:
            tok = get_token()
            break
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            delay = min(delay * 2, MAX_BACKOFF)
            log.info("Could not register yet: %s (trying again)", str(e)[:150])
            _sleep_until(delay, set(), is_offline(e))
    headers = {"x-worker-token": tok}
    log.info("Raji Studio phone helper running for %s (keep Termux open, phone on charger and Wi-Fi).", SERVER)
    pool, running = ThreadPoolExecutor(max_workers=PARALLEL), set()
    waiting, delay, last_call, version, started, offline = False, POLL_SECONDS, 0.0, None, time.time(), False
    reg_delay = 0
    while True:
        running = {f for f in running if not f.done()}
        free = PARALLEL - len(running)
        if free == 0 and time.time() - last_call < BUSY_POLL:
            time.sleep(3)
            continue
        try:
            last_call = time.time()
            r = httpx.get(f"{SERVER}/api/worker/jobs", timeout=30,
                          headers={**headers, "x-worker-can": CAN, "x-worker-slots": str(free), "x-worker-enroll": key_id()})
            if offline:
                log.info("Connected again.")
            offline = False
            if r.status_code == 401 or (r.status_code == 403 and key_worth_trying()):
                log.info("The server does not know this phone (removed in the dashboard?). Registering again..."
                         if r.status_code == 401 else "A helper key was saved for this phone. Joining with it...")
                try:
                    tok = get_token(force_new=True)
                    headers, waiting, reg_delay, delay = {"x-worker-token": tok}, False, 0, POLL_SECONDS
                except SystemExit:
                    raise
                except Exception as e:  # noqa: BLE001  (keeps failing: 30 s, 1, 2, 4 ... up to 30 minutes)
                    reg_delay = min(max(reg_delay * 2, 30), MAX_REREGISTER)
                    delay, offline = reg_delay, is_offline(e)
                    log.info("Could not register again: %s (next try in %d s)", str(e)[:150], delay)
            elif r.status_code == 403:
                delay = POLL_SECONDS
                if not waiting:
                    log.info("Waiting for approval in the dashboard (My channels -> Helper PC). Pairing code: %s", pair_code(tok))
                    waiting = True
            else:
                r.raise_for_status()
                delay = POLL_SECONDS  # only an answer without an error ends the back-off
                if waiting:
                    log.info("Approved. Waiting for jobs...")
                    waiting = False
                data = r.json()
                if data.get("enroll_key"):
                    save_enroll_key(data["enroll_key"])
                for job in data.get("jobs", [])[:max(free, 0)]:
                    running.add(pool.submit(_safe, job, headers))
                v = data.get("version") or ""
                if not running and ((version is not None and v != version) or time.time() - started > REFRESH_EVERY):
                    log.info("Restarting to fetch the newest helper and yt-dlp...")
                    pool.shutdown(wait=True)
                    sys.exit(EXIT_REFRESH)  # start.sh fetches phone.py from the server, updates yt-dlp, starts again
                version = v
        except Exception as e:  # noqa: BLE001
            delay = min(delay * 2, MAX_BACKOFF)
            offline = is_offline(e)  # no connection at all: look every 30 s, call in as soon as it is back
            log.info("connection problem: %s (next try in %d s%s)", str(e)[:150], delay,
                     ", sooner when the network is back" if offline else "")
        _sleep_until(delay, running, offline)


def _safe(job: dict, headers: dict):
    try:
        handle(job, headers)
    except Exception as e:  # noqa: BLE001  (one job's problem must never stop the helper)
        log.info("job failed: %s", str(e)[:150])


def _sleep_until(seconds: float, running: set, offline: bool = False):
    """Sleep, but come back early when a download finishes (a place is free again), or - while offline - when the
    network is back (looked at every NET_PROBE seconds; a jump of the clock after the phone slept counts as waited)."""
    end, busy, probe = time.time() + seconds, len(running), time.time() + NET_PROBE
    while time.time() < end:
        time.sleep(1)
        if busy and sum(1 for f in running if not f.done()) < busy:
            return
        if offline and time.time() >= probe:
            if network_up():
                return
            probe = time.time() + NET_PROBE


if __name__ == "__main__":
    main()

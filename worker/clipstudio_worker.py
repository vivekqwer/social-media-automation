"""Clip Studio helper PC.

Runs on your own computer (home internet, which YouTube does not bot-block) and does the two jobs the
server cannot: download the needed section of a video, and generate voice-overs. Rendering and uploading
still happen on the server.

Start it with run_worker.bat (Windows) or:  python worker/clipstudio_worker.py [SERVER_URL]
The first start registers this PC; approve it in the dashboard under My channels -> Helper PC.
"""
import os
import pathlib
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
TOKEN_FILE = pathlib.Path(__file__).with_name(".worker_token")
POLL_SECONDS = 8
UPDATE_EVERY = 600  # seconds between "is the server on newer code?" checks
EXIT_REMOVED, EXIT_UPDATED = 3, 4  # the launchers stop on 3 and restart on 4


def log(msg: str):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def single_instance():
    """Only one helper per PC (two would download the same clip twice)."""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 47613))
    except OSError:
        log("Another Clip Studio helper is already running on this PC. Closing this one.")
        sys.exit(0)
    return lock


def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=120).stdout.strip()


def maybe_update(server_version: str):
    """Pull the new helper code when the server runs a newer version, then restart (exit code 4)."""
    if not server_version or not (ROOT / ".git").exists():
        return
    try:
        head = _git("rev-parse", "HEAD")
        if not head or head == server_version or _git("status", "--porcelain", "--untracked-files=no"):
            return  # up to date, or local edits we must not touch
        _git("fetch", "--quiet", "origin")
        if subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", server_version], cwd=ROOT,
                          capture_output=True).returncode != 0:
            return  # the server is not ahead of us (or that version is unknown here)
        before = (ROOT / "requirements.txt").read_bytes()
        if subprocess.run(["git", "pull", "--ff-only", "--quiet"], cwd=ROOT).returncode != 0:
            return
        if (ROOT / "requirements.txt").read_bytes() != before:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "worker" / "requirements.txt")], cwd=ROOT / "worker")
        log("Updated the helper to the server's version. Restarting...")
        sys.exit(EXIT_UPDATED)
    except subprocess.TimeoutExpired:
        return


def get_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    r = httpx.post(f"{SERVER}/api/worker/register", json={"name": socket.gethostname()}, timeout=30)
    r.raise_for_status()
    tok = r.json()["token"]
    TOKEN_FILE.write_text(tok)
    log("Registered this PC. Approve it in the dashboard: My channels -> Helper PC.")
    return tok


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
        r = httpx.post(f"{SERVER}/api/worker/analysis/{pid}", headers=headers, timeout=120,
                       json={"heatmap": heat, "comments": comments[:3000], "caption_track": track})
        log(f"video {vid}: {len(heat)} replay points, {len(comments)} comments, captions {'yes' if track else 'no'} -> {r.status_code}")
    except Exception as e:  # noqa: BLE001
        log(f"video {vid}: analysis failed: {str(e)[:150]}")
        httpx.post(f"{SERVER}/api/worker/fail/{pid}", headers=headers, json={"kind": "analyze", "error": str(e)[:300]}, timeout=30)


def handle(job: dict, headers: dict):
    if job["type"] == "analyze":
        return analyze(job, headers)
    cid = job["clip_id"]
    with tempfile.TemporaryDirectory() as d:
        try:
            if job["type"] == "source":
                log(f"clip {cid}: downloading {job['video_id']} {job['start']:.0f}-{job['end']:.0f}s")
                path = download_section(job["video_id"], job["start"], job["end"], pathlib.Path(d))
                with open(path, "rb") as f:
                    r = httpx.post(f"{SERVER}/api/worker/source/{cid}", headers=headers,
                                   files={"file": ("source.mp4", f, "video/mp4")}, timeout=900)
                log(f"clip {cid}: video sent ({path.stat().st_size // 1024} KB) -> {r.status_code}; server is rendering")
            elif job["type"] == "tts":
                p = pathlib.Path(d) / "voice.mp3"
                synth(job["text"], job["voice"], str(p))
                with open(p, "rb") as f:
                    r = httpx.post(f"{SERVER}/api/worker/voice/{cid}", headers=headers,
                                   files={"file": ("voice.mp3", f, "audio/mpeg")}, timeout=120)
                log(f"clip {cid}: voice-over sent -> {r.status_code}")
        except Exception as e:  # noqa: BLE001
            log(f"clip {cid}: failed: {str(e)[:200]}")
            httpx.post(f"{SERVER}/api/worker/fail/{cid}", headers=headers,
                       json={"kind": job["type"], "error": friendly_error(e)}, timeout=30)


def main():
    _lock = single_instance()  # noqa: F841  (held for the life of the process)
    headers = {"x-worker-token": get_token()}
    last_update_check = 0.0
    log(f"Clip Studio helper running for {SERVER}. Keep this window open.")
    waiting = False
    while True:
        try:
            r = httpx.get(f"{SERVER}/api/worker/jobs", headers=headers, timeout=30)
            if r.status_code == 403:
                if not waiting:
                    log("Waiting for approval in the dashboard (My channels -> Helper PC)...")
                    waiting = True
            elif r.status_code == 401:
                log("This helper was removed in the dashboard. Delete worker/.worker_token and start again.")
                sys.exit(EXIT_REMOVED)
            else:
                r.raise_for_status()
                if waiting:
                    log("Approved. Waiting for jobs...")
                    waiting = False
                data = r.json()
                for job in data.get("jobs", []):
                    handle(job, headers)
                if time.time() - last_update_check > UPDATE_EVERY:
                    last_update_check = time.time()
                    maybe_update(data.get("version", ""))
        except Exception as e:  # noqa: BLE001
            log(f"connection problem: {str(e)[:150]}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

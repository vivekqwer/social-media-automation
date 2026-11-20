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
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", str(ROOT / "data" / "worker"))

import httpx  # noqa: E402

from app.ingest import download_section, friendly_error  # noqa: E402
from app.tts import synth  # noqa: E402

DEFAULT_SERVER = "https://social-media-automation-production-f5543fa3.up.railway.app"
SERVER = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("CLIPSTUDIO_URL", DEFAULT_SERVER)).rstrip("/")
TOKEN_FILE = pathlib.Path(__file__).with_name(".worker_token")
POLL_SECONDS = 8


def log(msg: str):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def get_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    r = httpx.post(f"{SERVER}/api/worker/register", json={"name": socket.gethostname()}, timeout=30)
    r.raise_for_status()
    tok = r.json()["token"]
    TOKEN_FILE.write_text(tok)
    log("Registered this PC. Approve it in the dashboard: My channels -> Helper PC.")
    return tok


def handle(job: dict, headers: dict):
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
    headers = {"x-worker-token": get_token()}
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
                return
            else:
                r.raise_for_status()
                if waiting:
                    log("Approved. Waiting for jobs...")
                    waiting = False
                for job in r.json().get("jobs", []):
                    handle(job, headers)
        except Exception as e:  # noqa: BLE001
            log(f"connection problem: {str(e)[:150]}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

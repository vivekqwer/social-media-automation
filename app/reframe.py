"""Smart reframe for Shorts: find where the people are in the source video, so the 9:16 crop follows them
instead of always cutting the centre. Free YuNet face detector (OpenCV), runs on the server CPU.
Standalone: if OpenCV or the model is missing, the clip is simply centre-cropped as before."""
import logging
import statistics
import subprocess
from pathlib import Path

from .config import BASE_DATA_DIR, ffmpeg_bin
from .ingest import stream_seconds

log = logging.getLogger(__name__)
MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
_detector = None


def _model() -> Path:
    baked = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet.onnx"  # put there by the Dockerfile
    if baked.exists():
        return baked
    p = BASE_DATA_DIR / "models" / "face_detection_yunet.onnx"
    if not p.exists():
        import httpx

        p.parent.mkdir(parents=True, exist_ok=True)
        r = httpx.get(MODEL_URL, follow_redirects=True, timeout=60)
        r.raise_for_status()
        p.write_bytes(r.content)
    return p


def subject_x(src: Path, samples: int = 8) -> float | None:
    """Where the main person is, as a fraction of the frame width (0 = left, 1 = right). None = no clear subject."""
    global _detector
    try:
        import cv2
        import numpy as np

        if _detector is None:
            _detector = cv2.FaceDetectorYN.create(str(_model()), "", (320, 320), 0.6)
        dur = stream_seconds(src, "v:0") or 0
        if dur <= 0:
            return None
        xs = []
        for i in range(samples):
            t = dur * (i + 0.5) / samples
            raw = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                                  "-vf", "scale=480:-2", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
            if not raw:
                continue
            w = 480
            h = len(raw) // (w * 3)
            if h <= 0:
                continue
            img = np.frombuffer(raw[: w * h * 3], np.uint8).reshape(h, w, 3)
            _detector.setInputSize((w, h))
            _, faces = _detector.detect(img)
            if faces is None or not len(faces):
                continue
            best = max(faces, key=lambda f: f[2] * f[3] * f[-1])  # biggest, surest face = the main person
            xs.append(float((best[0] + best[2] / 2) / w))
        if len(xs) < max(2, samples // 3):
            return None  # no person in most of the clip: keep the centre
        return round(min(1.0, max(0.0, statistics.median(xs))), 3)
    except Exception as e:  # noqa: BLE001
        log.warning("reframe: %s", str(e)[:200])
        return None

"""My face for thumbnails, one per connected channel.

The photo's background is removed once at upload with the free U2-Net human-segmentation model
(runs on the CPU, no AI service). A PNG that already has a transparent background is used as is.
"""
import io
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageOps

from .config import DATA_DIR

MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net_human_seg.onnx"
_session = None


def face_path(dest_id) -> Path:
    return DATA_DIR / "faces" / f"{int(dest_id)}.png"


DEFAULT_FACE = DATA_DIR / "faces" / "default.png"  # used by every channel that has no face of its own


def face_for(dest_id) -> Path | None:
    for p in ((face_path(dest_id),) if dest_id else ()) + (DEFAULT_FACE,):
        if p.exists():
            return p
    return None


def _model() -> Path:
    baked = Path(__file__).resolve().parent.parent / "models" / "u2net_human_seg.onnx"  # put there by the Dockerfile
    if baked.exists():
        return baked
    p = DATA_DIR / "models" / "u2net_human_seg.onnx"
    if not p.exists():  # one-time download (~170 MB) onto the data volume
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.replace(p)
    return p


def _mask(img: Image.Image) -> Image.Image:
    global _session
    if _session is None:
        import onnxruntime as ort

        _session = ort.InferenceSession(str(_model()), providers=["CPUExecutionProvider"])
    x = np.asarray(img.convert("RGB").resize((320, 320), Image.LANCZOS)).astype(np.float32)
    x = x / max(1.0, float(x.max()))
    x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    out = _session.run(None, {_session.get_inputs()[0].name: x.transpose(2, 0, 1)[None].astype(np.float32)})[0][0, 0]
    m = (out - out.min()) / (out.max() - out.min() + 1e-8)
    mask = Image.fromarray((m * 255).astype(np.uint8)).resize(img.size, Image.LANCZOS)
    return mask.point(lambda v: 0 if v < 25 else 255 if v > 230 else v)  # crisp edges, no grey halo


def save_face(dest_id, data: bytes) -> Path:
    """dest_id=None saves the default face (all channels without their own)."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    img.thumbnail((1400, 1400))
    if img.mode in ("RGBA", "LA", "P") and img.convert("RGBA").getextrema()[3][0] < 250:
        cut = img.convert("RGBA")  # already transparent
    else:
        cut = img.convert("RGBA")
        cut.putalpha(_mask(img))
    box = cut.getchannel("A").point(lambda v: 255 if v > 40 else 0).getbbox()
    if not box or (box[2] - box[0]) * (box[3] - box[1]) < 0.04 * cut.width * cut.height:
        raise ValueError("No person found in this photo. Use a clear photo of your face and shoulders.")
    out = face_path(dest_id) if dest_id else DEFAULT_FACE
    out.parent.mkdir(parents=True, exist_ok=True)
    cut.crop(box).save(out)
    if not DEFAULT_FACE.exists():  # the first face also covers channels connected later
        cut.crop(box).save(DEFAULT_FACE)
    return out

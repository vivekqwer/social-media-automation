"""Automatic thumbnails: a frame from the clip + my face (if uploaded for the channel) + big, bold text.

Phone-safe layout: text is huge (few words, max 3 lines) and kept inside the area every YouTube surface
shows — away from the edges, the duration stamp (bottom-right of 16:9) and the cropped/overlaid parts of
Shorts thumbnails (top and bottom of 9:16). Text is drawn by libass so Hindi is shaped correctly.
"""
import re
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageFont

from .config import FONTS_DIR, ffmpeg_bin
from .faces import face_for
from .ingest import stream_seconds
from .render import DEVA, _esc, ass_text

LATIN_FONT = ("Anton", FONTS_DIR / "Anton-Regular.ttf")
HINDI_FONT = ("Noto Sans Devanagari", FONTS_DIR / "NotoSansDevanagari-Bold.ttf")


def default_text(title: str) -> str:
    t = re.sub(r"\s+", " ", re.split(r"[|:\-–(\[]", title or "")[0]).strip()
    return " ".join(t.split()[:5]).upper()[:34]


def _wrap(words: list[str], font, maxw: float, max_lines: int) -> list[str] | None:
    lines, cur = [], ""
    for w in words:
        nxt = (cur + " " + w).strip()
        if font.getlength(nxt) <= maxw:
            cur = nxt
            continue
        if not cur or font.getlength(w) > maxw:
            return None
        lines.append(cur)
        cur = w
    lines.append(cur)
    return lines if len(lines) <= max_lines else None


def _fit(text: str, font_file: Path, box_w: float, box_h: float, max_lines: int, biggest: int, smallest: int):
    """Largest font size at which the text fits the box. -> (size, lines, line height)."""
    words = text.split()
    for size in range(biggest, smallest - 1, -4):
        f = ImageFont.truetype(str(font_file), size)
        lines = _wrap(words, f, box_w, max_lines)
        if lines:
            lh = sum(f.getmetrics())
            ink = lh if "Devanagari" in font_file.name else size * 1.2  # Anton's glyphs are much shorter than its line box
            if (size * 1.06 * (len(lines) - 1) + ink) <= box_h:
                return size, lines, lh
    f = ImageFont.truetype(str(font_file), smallest)
    return smallest, [" ".join(words)], sum(f.getmetrics())


def _score(rgb: np.ndarray) -> float:
    """How good a frame looks as a thumbnail: colourful, sharp, not too dark or washed out."""
    r, g, b = (rgb[..., i].astype(np.float32) for i in range(3))
    rg, yb = r - g, 0.5 * (r + g) - b
    colour = np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean())
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    sharp = np.sqrt(np.abs(np.diff(gray, 2, axis=0)).mean() + np.abs(np.diff(gray, 2, axis=1)).mean())
    light = gray.mean()
    s = colour + 6 * sharp
    return s * (0.3 if light < 45 or light > 215 else 1.0)


def _best_time(src: Path, dur: float, rank: int = 0) -> float:
    scored = []
    for f in (0.12, 0.22, 0.32, 0.42, 0.52, 0.62, 0.72, 0.85):
        t = dur * f
        r = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                            "-vf", "scale=192:108", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
        if r.returncode or len(r.stdout) != 192 * 108 * 3:
            continue
        scored.append((_score(np.frombuffer(r.stdout, np.uint8).reshape(108, 192, 3)), t))
    if not scored:
        return dur * 0.35
    scored.sort(reverse=True)
    return scored[min(rank, len(scored) - 1)][1]  # rank 1 = second-best frame (for the auto-swap variant)


def _frame(src: Path, W: int, H: int, out: Path, variant: int = 0):
    dur = stream_seconds(src, "v:0") or 10.0
    at = _best_time(src, dur, rank=2 if variant else 0)
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},eq=contrast=1.12:saturation=1.35"
    r = subprocess.run([ffmpeg_bin(), "-y", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1", "-vf", vf, str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("thumbnail frame failed: " + r.stderr[-300:])


def _gradient(W: int, H: int, horizontal: bool, reach: float, strength: int) -> Image.Image:
    """Dark fade behind the text so it stays readable on any frame."""
    n = W if horizontal else H
    col = Image.new("L", (n, 1) if horizontal else (1, n))
    col.putdata([int(strength * max(0.0, 1 - i / (n * reach)) ** 1.4) for i in range(n)])
    a = col.resize((W, H))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    layer.putalpha(a)
    return layer


def _paste_face(base: Image.Image, face: Image.Image, x: int, y: int, stroke: int):
    a = face.getchannel("A")
    pad = stroke * 4
    big = Image.new("L", (face.width + 2 * pad, face.height + 2 * pad))
    big.paste(a, (pad, pad))
    ring = big.filter(ImageFilter.GaussianBlur(stroke)).point(lambda v: 255 if v > 8 else 0)
    shadow = big.filter(ImageFilter.GaussianBlur(stroke * 3)).point(lambda v: int(v * 0.6))
    base.paste(Image.new("RGBA", big.size, (0, 0, 0, 255)), (x - pad + stroke, y - pad + stroke), shadow)
    base.paste(Image.new("RGBA", big.size, (255, 255, 255, 255)), (x - pad, y - pad), ring)
    base.paste(face, (x, y), face)


def make_thumbnail(clip, project) -> Path:
    workdir = Path(clip.output_path).parent
    src = workdir / "body.mp4" if (workdir / "body.mp4").exists() else Path(clip.output_path)  # without the Shorts cover
    portrait = clip.aspect == "9:16"
    W, H = (1080, 1920) if portrait else (1280, 720)
    variant = int(getattr(clip, "thumb_variant", 0) or 0)
    _frame(src, W, H, workdir / "thumb_frame.png", variant)
    base = Image.open(workdir / "thumb_frame.png").convert("RGBA")

    face = None
    fp = face_for(clip.destination_id)
    if fp and clip.thumb_face is not False:
        face = Image.open(fp).convert("RGBA")

    if portrait:  # Shorts: keep everything inside the centre 4:5 area, clear of the top bar and bottom title
        base.alpha_composite(_gradient(W, H, False, 0.55, 200))
        if face:
            s = min(H * 0.54 / face.height, W * 0.9 / face.width)
            face = face.resize((int(face.width * s), int(face.height * s)), Image.LANCZOS)
            _paste_face(base, face, (W - face.width) // 2, H - face.height, int(W * 0.012))
            box = (W * 0.07, H * 0.16, W * 0.93, H * 0.46)
        else:
            box = (W * 0.07, H * 0.24, W * 0.93, H * 0.62)
        anchor = 5
    else:  # 16:9: face on the right, text on the left (duration stamp sits bottom-right)
        base.alpha_composite(_gradient(W, H, True, 0.72, 215))
        if face:
            s = min(H * 0.97 / face.height, W * 0.46 / face.width)
            face = face.resize((int(face.width * s), int(face.height * s)), Image.LANCZOS)
            fx = W - face.width - int(W * 0.015)
            _paste_face(base, face, fx, H - face.height, int(H * 0.012))
            box = (W * 0.05, H * 0.09, min(W * 0.62, fx - W * 0.02), H * 0.88)
        else:
            box = (W * 0.05, H * 0.09, W * 0.80, H * 0.88)
        anchor = 4
    base.convert("RGB").save(workdir / "thumb_base.png")

    text = ass_text(clip.thumb_text or default_text(clip.title or project.title)).replace("\\N", " ")
    fname, ffile = HINDI_FONT if DEVA.search(text) else LATIN_FONT
    bw, bh = box[2] - box[0], box[3] - box[1]
    size, lines, lh = _fit(text, ffile, bw, bh, 3, int(H * (0.105 if portrait else 0.22)), int(H * (0.04 if portrait else 0.09)))
    fs = lh  # libass sizes a font by its full line height; Pillow by the em
    yellow = (0 if variant else len(lines) - 1) if len(lines) > 1 else -1  # variant highlights the first line
    step = lh * 0.92 if fname == HINDI_FONT[0] else size * 1.06  # tight lines (Anton's own line gap is huge)
    x = int(box[0]) if anchor == 4 else W // 2
    y0 = (box[1] + box[3]) / 2 - step * (len(lines) - 1) / 2
    events = [f"Dialogue: 0,0:00:00.00,0:00:10.00,T,,0,0,0,,{{\\an{anchor}\\pos({x},{int(y0 + i * step)})"
              f"\\c&H{'00D7FF' if i == yellow else 'FFFFFF'}&}}{ln}" for i, ln in enumerate(lines)]
    ass = "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: T,{fname},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{max(4, size // 11)},{max(3, size // 22)},{anchor},0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, "",
    ])
    (workdir / "thumb.ass").write_text(ass, encoding="utf-8")
    r = subprocess.run([ffmpeg_bin(), "-y", "-i", "thumb_base.png", "-vf", f"ass=thumb.ass:fontsdir='{_esc(FONTS_DIR)}'",
                        "-frames:v", "1", "-q:v", "2", "thumb.jpg"], cwd=workdir, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("thumbnail failed: " + r.stderr[-300:])
    for q in ("5", "8", "12"):  # YouTube refuses thumbnails over 2 MB (busy frames can exceed it)
        if (workdir / "thumb.jpg").stat().st_size <= 1_900_000:
            break
        subprocess.run([ffmpeg_bin(), "-y", "-i", "thumb.jpg", "-q:v", q, "thumb_small.jpg"], cwd=workdir, capture_output=True)
        (workdir / "thumb_small.jpg").replace(workdir / "thumb.jpg")
    for f in ("thumb_frame.png", "thumb_base.png"):
        (workdir / f).unlink(missing_ok=True)
    return workdir / "thumb.jpg"

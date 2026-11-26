"""Thumbnail designer: strong Shorts / YouTube thumbnails made from the clip itself — classical image processing only
(Pillow, numpy, ffmpeg, libass; OpenCV face finding when it is installed). No AI, no outside service, nothing paid.

1. Frame: 16 frames of the clip's own section are scored (colour, sharpness, faces, a clear subject); black, washed
   out, blurred and transition frames and frames with burned-in text in the middle lose. Variant 1 (the A/B test's
   second thumbnail) takes the best frame that is far enough in time from the first one.
2. Crop: towards the subject (the biggest face, else the peak of a saliency map: spectral residual + colour
   contrast + a centre prior) with a slight punch-in zoom, the subject placed where the layout leaves room for it.
3. Grade: levels, contrast, vibrance (skin tones boosted less), unsharp mask, soft vignette.
4. Words: 2-4 big words from the title (channel names, "| Best Moment", "Reaction", emojis, years, 4K... dropped; the
   strongest few words kept, one highlighted); the owner's own thumbnail text is always used as typed. Text is drawn
   by libass (Devanagari shaped correctly), thick outline + soft shadow, inside the area every surface shows.
5. Four layouts (top text + accent bar, bottom box, slanted band, arrow + circle on the subject), picked by the clip
   id; variant 1 uses the next layout. The owner can cycle them ("Another design" -> thumb_style "d1:<n>").
6. Output: 1080x1920 (9:16) or 1280x720 (16:9) JPEG of at most 1.9 MB.

Super Admin switch "thumbs.designer" (on by default); off = the old thumbnail (app/thumbs.py). A slow timed job
(`refresh_old`) re-makes, within a small daily number per account, the thumbnails that the old code made for clips
that are still waiting (the Short's opening frame is made again too) or uploaded but not public yet (sent to YouTube
again — the video file on YouTube cannot change).
"""
import io
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .config import FONTS_DIR, ffmpeg_bin

log = logging.getLogger(__name__)

VERSION = "d1"  # clip.thumb_style of a designer thumbnail ("d1" or "d1:<layout chosen by the owner>")
CLASSIC = "classic"
LAYOUTS = ["top", "box", "band", "arrow"]
MAX_BYTES = 1_900_000
PER_DAY = int(os.getenv("THUMB_REDESIGN_PER_DAY") or "15")  # re-made old thumbnails a day, per account
PER_RUN = 5

YELLOW, RED, WHITE, BLACK = (255, 214, 10), (236, 32, 40), (255, 255, 255), (0, 0, 0)
DEVA = re.compile(r"[ऀ-ॿ]")


# ---------------------------------------------------------------- switch, layout, marker
def enabled() -> bool:
    try:
        from . import acl

        return acl.feature("thumbs.designer")
    except Exception:  # noqa: BLE001  (no account context, e.g. a tool on the PC: the designer)
        return True


def owner_layout(clip) -> int | None:
    m = re.match(r"^d\d+:(\d)$", str(getattr(clip, "thumb_style", None) or ""))
    return int(m.group(1)) % len(LAYOUTS) if m else None


def layout_for(clip_id, variant: int = 0, chosen: int | None = None) -> int:
    """The same clip always gets the same layout; the A/B variant the next one."""
    base = chosen if chosen is not None else (int(clip_id or 0) * 3 + 1) % len(LAYOUTS)
    return (base + (1 if variant else 0)) % len(LAYOUTS)


def style_after(clip) -> str:
    o = owner_layout(clip)
    return VERSION + (f":{o}" if o is not None else "")


def next_style(clip) -> str:
    """'Another design': the next layout after the one shown now, kept for later re-makes."""
    o = owner_layout(clip)
    cur = o if o is not None else layout_for(clip.id)
    return f"{VERSION}:{(cur + 1) % len(LAYOUTS)}"


# ---------------------------------------------------------------- words
STOP = set("""a an the of for to in on at by is are was were be been with and or but from as it its this that these those
my your our their his her you we they i me so just very about into over than then too can will do does did has have had
not no vs. vs ka ki ke ko se me mein hai hain tha thi the ye yeh woh wo ek aur bhi to toh par pe na hi""".split()) - {"vs"}
POWER = set("""best worst secret secrets shocking insane crazy clean dirty vs never first last biggest cheapest costliest
most only real fake viral world's huge giant tiny new old hidden truth exposed amazing unbelievable epic perfect
famous legendary richest poorest fastest dangerous deadly spicy sweet free cheap expensive rare banned crispy
hosh ud jayenge jaoge pagal kamaal zabardast dhamaka gazab jhakaas mast desi asli nakli sabse wow omg""".split())
JUNK = re.compile(r"(?i)\b(best\s+moments?|reaction|reacts?|reacting|official|full\s+video|full\s+movie|teaser|shorts?|"
                  r"4k|8k|hd|uhd|fhd|hdr|1080p|720p|2160p|\d+\s?fps|lyrics?|lyrical|music\s+video|ft\.?|feat\.?|"
                  r"subscribe|subscirbe|like|share|vlog|episode|ep\s?\d+|part\s?\d+|trailer|l)\b")
YEAR = re.compile(r"\b(19|20)\d\d\b")


def _clean(text: str) -> str:
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text or "")  # (Official Video), [4K]
    t = re.sub(r"#\S+", " ", t)
    t = re.sub(r"[^\wऀ-ॿ\s'!?&%₹+\-:|]", " ", t)  # emojis and symbols
    return re.sub(r"\s+", " ", t).strip()


def hook_words(title: str, channel: str | None = None) -> list[str]:
    """2-4 strong words from a title: the hook, not the channel or the "| Best Moment" tail."""
    t = _clean(title)
    if channel:
        t = re.sub(re.escape(channel.strip()), " ", t, flags=re.I) if channel.strip() else t
    segs = [s for s in re.split(r"\s*\|\s*", t) if s.strip()]
    good = []
    for sg in segs:
        sg = YEAR.sub(" ", JUNK.sub(" ", sg))
        sg = re.sub(r"\s+", " ", sg).strip(" -:")
        if len(sg.split()) >= 1:
            good.append(sg)
    t = max(good[:2], key=lambda x: len(x.split())) if good else ""
    if good and len(good[0].split()) >= 2:
        t = good[0]
    toks = [w.strip("-:'") for w in re.split(r"\s+|\s[-:]\s|[:]", t) if w.strip("-:'")]
    toks = [w for w in toks if not re.fullmatch(r"[\-:|&+]+", w)]
    if len(toks) <= 4:
        return toks
    title_caps = sum(1 for w in toks if w.isupper() and len(w) > 2) < len(toks) * 0.6  # a title in all caps says nothing

    def score(i: int) -> float:
        w = toks[i]
        lw = w.lower().strip("!?")
        if lw in STOP:
            return -1.5
        s = 1.0
        if lw in POWER:
            s += 3
        if title_caps and w.isupper() and len(w) > 2:
            s += 2
        if re.search(r"\d", w):
            s += 2
        if DEVA.search(w):
            s += 0.5
        if len(lw) >= 5:
            s += 0.6
        if w.endswith(("!", "?")):
            s += 1
        return s

    sc = [score(i) for i in range(len(toks))]
    best, best_s = (0, min(4, len(toks))), -1e9
    for n in (2, 3, 4):
        for i in range(0, len(toks) - n + 1):
            if sc[i] < 0 or sc[i + n - 1] < 0:
                continue  # never start or end on "the", "ke", "of"...
            s = sum(sc[i:i + n]) + 0.4 * n - 0.08 * i
            if s > best_s:
                best, best_s = (i, n), s
    i, n = best
    return toks[i:i + n]


def key_index(words: list[str], edge_only: bool = False) -> int:
    """The word to highlight: the strongest (power word, number, longest); edge_only = first or last word."""
    if not words:
        return -1

    def s(i):
        w = words[i].lower().strip("!?")
        return (w in POWER) * 3 + bool(re.search(r"\d", w)) * 2 + (w not in STOP) + len(w) / 20 + i / 100

    idx = [0, len(words) - 1] if edge_only else range(len(words))
    return max(idx, key=s)


def words_for(clip, project) -> tuple[list[str], bool]:
    """(words, typed by the owner). Latin words in capitals."""
    own = (getattr(clip, "thumb_text", None) or "").strip()
    if own:
        ws = _clean(own).replace("|", " ").split()
        return [w if DEVA.search(w) else w.upper() for w in ws][:8], True
    title = getattr(clip, "title", None) or getattr(project, "title", None) or ""
    ws = hook_words(title, getattr(project, "channel", None))
    if not ws:
        ws = hook_words(getattr(project, "title", None) or "", None)
    return [w if DEVA.search(w) else w.upper() for w in ws], False


# ---------------------------------------------------------------- frames
_det_lock = threading.Lock()
_det = {"kind": None, "obj": None}


def _detector():
    """YuNet (when its model is already on disk) or OpenCV's Haar cascade; None when neither is there."""
    if _det["kind"] is not None:
        return _det["kind"], _det["obj"]
    kind, obj = "none", None
    try:
        import cv2

        from .config import BASE_DATA_DIR

        for p in (Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet.onnx",
                  BASE_DATA_DIR / "models" / "face_detection_yunet.onnx"):
            if p.exists():
                kind, obj = "yunet", cv2.FaceDetectorYN.create(str(p), "", (320, 320), 0.6)
                break
        if obj is None:
            casc = Path(getattr(getattr(cv2, "data", None), "haarcascades", "") or "") / "haarcascade_frontalface_default.xml"
            if casc.exists():
                c = cv2.CascadeClassifier(str(casc))
                if not c.empty():
                    kind, obj = "haar", c
    except Exception as e:  # noqa: BLE001
        log.info("thumb designer: no face finder (%s)", str(e)[:120])
    _det.update(kind=kind, obj=obj)
    return kind, obj


def faces(rgb: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Faces as (x, y, w, h) fractions of the picture, biggest first."""
    try:
        with _det_lock:
            kind, d = _detector()
            if d is None:
                return []
            import cv2

            h, w = rgb.shape[:2]
            s = 480 / max(w, h)
            img = np.ascontiguousarray(rgb[..., ::-1])
            if s < 1:
                img = cv2.resize(img, (int(w * s), int(h * s)))
            ih, iw = img.shape[:2]
            if kind == "yunet":
                d.setInputSize((iw, ih))
                _, f = d.detect(img)
                boxes = [] if f is None else [(float(a[0]), float(a[1]), float(a[2]), float(a[3])) for a in f]
            else:
                g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                boxes = [tuple(map(float, b)) for b in d.detectMultiScale(g, 1.15, 5, minSize=(max(16, iw // 25),) * 2)]
        out = [(x / iw, y / ih, bw / iw, bh / ih) for x, y, bw, bh in boxes if bw > iw * 0.03]
        return sorted(out, key=lambda b: -b[2] * b[3])
    except Exception:  # noqa: BLE001
        return []


def _grab(src: Path, t: float, width: int | None) -> Image.Image | None:
    vf = f"scale={width}:-2" if width else "scale='min(1920,iw)':-2"
    r = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", vf, "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if r.returncode or not r.stdout:
        return None
    try:
        return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None


def _gray(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def text_in_middle(rgb: np.ndarray) -> float:
    """Share of the middle of the frame covered by burned-in text (bright letters with dark edges). Cheap guess."""
    h, w = rgb.shape[:2]
    band = rgb[int(h * 0.3):int(h * 0.85), int(w * 0.1):int(w * 0.9)].astype(np.int16)
    lo = band.min(axis=2)
    bright = (lo > 215) | ((band[..., 0] > 220) & (band[..., 1] > 190) & (band[..., 2] < 90))  # white or yellow letters
    g = _gray(band.astype(np.float32))
    edge = np.zeros_like(bright)
    edge[:, 1:] |= np.abs(np.diff(g, axis=1)) > 110
    edge[1:, :] |= np.abs(np.diff(g, axis=0)) > 110
    return float((bright & edge).mean())


def frame_score(img: Image.Image) -> dict:
    rgb = np.asarray(img, np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    colour = float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))
    gray = _gray(rgb)
    lap = gray[1:-1, 1:-1] * 4 - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    sharp = float(np.sqrt(lap.var()))
    light, spread = float(gray.mean()), float(gray.std())
    fc = faces(np.asarray(img))
    face = max((f[2] * f[3] for f in fc), default=0.0)
    sal = saliency(img)
    peak = float(np.percentile(sal, 97) / (sal.mean() + 1e-6))
    txt = text_in_middle(np.asarray(img))
    return {"colour": colour, "sharp": sharp, "light": light, "spread": spread, "face": face, "faces": len(fc),
            "peak": peak, "text": txt}


def _total(m: dict, sharp_median: float) -> float:
    s = m["colour"] + 2.2 * m["sharp"] + 4 * min(m["peak"], 6)
    if m["face"]:
        s *= 1.25 + min(1.0, 4 * m["face"] ** 0.5)  # a clear face makes people click; bigger is better
    if m["light"] < 40 or m["light"] > 220:
        s *= 0.25  # black / washed out
    if m["spread"] < 18:
        s *= 0.2  # flat: a fade, a title card, a transition
    if sharp_median and m["sharp"] < 0.6 * sharp_median:
        s *= 0.55  # blurred (motion, cross-fade)
    if m["text"] > 0.012:
        s *= max(0.35, 1 - 18 * m["text"])  # captions / burned-in text in the middle
    return s


_scored_cache: dict = {}


def _small_frames(src: Path, dur: float, n: int) -> list[tuple[float, Image.Image]]:
    """n small frames spread over the clip in ONE decoding pass (one seek per frame is much slower on long GOPs)."""
    from .ingest import video_size

    size = video_size(src) or (1080, 1920)
    w = 320
    h = max(2, int(round(size[1] * w / size[0] / 2)) * 2)
    t0, span = dur * 0.05, dur * 0.9
    r = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{t0:.2f}", "-i", str(src), "-t", f"{span:.2f}",
                        "-vf", f"fps={n / max(0.5, span):.4f},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True)
    fs = w * h * 3
    k = len(r.stdout) // fs
    out = []
    for i in range(min(k, n * 2)):
        a = np.frombuffer(r.stdout[i * fs:(i + 1) * fs], np.uint8).reshape(h, w, 3)
        out.append((t0 + (i + 0.5) * span / max(1, k), Image.fromarray(a)))
    return out


def pick_times(src: Path, dur: float, n: int = 16) -> list[tuple[float, float]]:
    """[(score, time)] best first, for n frames spread over the clip."""
    try:
        key = (str(src), Path(src).stat().st_mtime, round(dur, 2), n)
    except OSError:
        key = None
    if key and key in _scored_cache:
        return _scored_cache[key]
    got = [(t, frame_score(im)) for t, im in _small_frames(src, dur, n)]
    if not got:  # one seek per frame as a fallback
        for i in range(n // 2):
            t = dur * (0.05 + 0.9 * i / max(1, n // 2 - 1))
            im = _grab(src, t, 320)
            if im is not None:
                got.append((t, frame_score(im)))
    if not got:
        return []
    med = float(np.median([m["sharp"] for _t, m in got]))
    res = sorted(((_total(m, med), t) for t, m in got), reverse=True)
    if key:
        if len(_scored_cache) > 32:
            _scored_cache.clear()
        _scored_cache[key] = res
    return res


def choose_time(scored: list[tuple[float, float]], dur: float, variant: int) -> float:
    if not scored:
        return dur * 0.35
    if not variant:
        return scored[0][1]
    first = scored[0][1]
    for _s, t in scored[1:]:
        if abs(t - first) >= max(1.5, dur * 0.18):
            return t  # the second thumbnail shows another moment
    return scored[min(1, len(scored) - 1)][1]


# ---------------------------------------------------------------- saliency and crop
def _box_blur(a: np.ndarray, k: int) -> np.ndarray:
    p = np.pad(a, k, mode="edge")
    c = p.cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)))
    n = 2 * k + 1
    return (c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]) / (n * n)


def saliency(img: Image.Image, size: int = 96) -> np.ndarray:
    """0..1 map (size wide): spectral residual + colour contrast + a soft centre prior."""
    w, h = img.size
    sw, sh = size, max(8, int(size * h / w))
    small = np.asarray(img.resize((sw, sh), Image.BILINEAR), np.float32)
    g = np.asarray(Image.fromarray(small.astype(np.uint8)).convert("L").resize((64, 64)), np.float32)
    F = np.fft.fft2(g)
    logA = np.log(np.abs(F) + 1e-6)
    res = logA - _box_blur(logA, 1)
    sr = np.abs(np.fft.ifft2(np.exp(res + 1j * np.angle(F)))) ** 2
    sr = np.asarray(Image.fromarray((sr / (sr.max() + 1e-9) * 255).astype(np.uint8)).resize((sw, sh), Image.BILINEAR)
                    .filter(ImageFilter.GaussianBlur(2)), np.float32) / 255
    blur = np.asarray(Image.fromarray(small.astype(np.uint8)).filter(ImageFilter.GaussianBlur(2)), np.float32)
    ft = np.sqrt(((blur - blur.reshape(-1, 3).mean(0)) ** 2).sum(2))
    ft /= ft.max() + 1e-6
    yy, xx = np.mgrid[0:sh, 0:sw]
    centre = np.exp(-(((xx / sw - 0.5) / 0.38) ** 2 + ((yy / sh - 0.5) / 0.38) ** 2))
    s = (0.45 * sr / (sr.max() + 1e-6) + 0.55 * ft) * (0.55 + 0.45 * centre)
    return s / (s.max() + 1e-6)


def subject(img: Image.Image) -> tuple[float, float, float, bool]:
    """(x, y, radius) of the main subject as fractions of the width/height/width, and whether it is a face."""
    fc = faces(np.asarray(img))
    if fc:
        x, y, w, h = fc[0]
        return x + w / 2, y + h / 2, max(w, h * img.height / img.width) * 0.85, True
    s = saliency(img)
    hh, ww = s.shape
    # people draw the eye: skin-coloured areas (YCbCr range) count extra when no face finder is available
    ycc = np.asarray(img.convert("YCbCr").resize((ww, hh), Image.BILINEAR), np.float32)
    skin = ((ycc[..., 1] > 80) & (ycc[..., 1] < 125) & (ycc[..., 2] > 138) & (ycc[..., 2] < 170)
            & (ycc[..., 0] > 60)).astype(np.float32)
    if 0.01 < skin.mean() < 0.35:  # some skin, not a whole orange / brown frame
        s = s * 0.65 + _box_blur(skin, 1) * 0.45
    yy, xx = np.mgrid[0:hh, 0:ww]
    edge = np.minimum(np.minimum(xx / ww, 1 - xx / ww), np.minimum(yy / hh, 1 - yy / hh))
    s = s * np.clip(edge / 0.12, 0, 1)  # the subject is never at the very edge (and the map is noisy there)
    sm = _box_blur(s, max(2, s.shape[1] // 28))
    iy, ix = np.unravel_index(int(sm.argmax()), sm.shape)
    area = float((sm > 0.62 * sm.max()).mean())
    rad = min(0.32, max(0.12, (area / np.pi) ** 0.5 * 0.9))
    return (ix + 0.5) / s.shape[1], (iy + 0.5) / s.shape[0], rad, False


def crop_to(img: Image.Image, W: int, H: int, subj, target: tuple[float, float], zoom: float) -> tuple[Image.Image, tuple]:
    """Crop img to W:H around the subject, the subject at `target` (fractions) of the result; slight zoom.
    Returns (picture W x H, subject (x, y, r) in the result as fractions)."""
    sw, sh = img.size
    A = W / H
    cw, ch = (sh * A, sh) if sw / sh > A else (sw, sw / A)
    # never zoom so far that the picture gets soft (at most ~2.2x enlargement)
    zoom = max(1.0, min(zoom, cw / (W / 2.2)))
    cw, ch = cw / zoom, ch / zoom
    sx, sy, sr, _isface = subj
    x0 = min(max(0.0, sx * sw - target[0] * cw), sw - cw)
    y0 = min(max(0.0, sy * sh - target[1] * ch), sh - ch)
    out = img.crop((int(x0), int(y0), int(x0 + cw), int(y0 + ch))).resize((W, H), Image.LANCZOS)
    return out, ((sx * sw - x0) / cw, (sy * sh - y0) / ch, sr * sw / cw)


# ---------------------------------------------------------------- grade
def grade(img: Image.Image) -> Image.Image:
    a = np.asarray(img, np.float32)
    lum = _gray(a)
    lo, hi = np.percentile(lum, 1), np.percentile(lum, 99)
    if hi - lo > 40:  # gentle auto levels
        st = (a - lo) * (255.0 / (hi - lo))
        a = a * 0.4 + st * 0.6
    a = np.clip(a, 0, 255)
    im = Image.fromarray(a.astype(np.uint8))
    im = ImageEnhance.Contrast(im).enhance(1.12)
    # vibrance: dull colours gain more than strong ones, skin tones less (no orange faces)
    hsv = np.asarray(im.convert("HSV"), np.float32)
    ycc = np.asarray(im.convert("YCbCr"), np.float32)
    skin = (ycc[..., 1] > 77) & (ycc[..., 1] < 127) & (ycc[..., 2] > 133) & (ycc[..., 2] < 173)
    sat = hsv[..., 1] / 255
    boost = 0.38 * (1 - sat) * np.where(skin, 0.35, 1.0)
    hsv[..., 1] = np.clip((sat * (1 + boost) + 0.04 * (~skin)) * 255, 0, 255)
    im = Image.fromarray(hsv.astype(np.uint8), "HSV").convert("RGB")
    im = im.filter(ImageFilter.UnsharpMask(radius=2.2, percent=85, threshold=3))
    W, H = im.size  # soft vignette (made small, then enlarged: smooth anyway)
    yy, xx = np.mgrid[0:128, 0:128].astype(np.float32)
    d = np.sqrt(((xx - 63.5) / 64) ** 2 + ((yy - 63.5) / 64) ** 2) / 1.4142
    v = Image.fromarray(((1 - 0.38 * np.clip(d, 0, 1) ** 2.4) * 255).astype(np.uint8)).resize((W, H), Image.BILINEAR)
    from PIL import ImageChops

    return ImageChops.multiply(im, Image.merge("RGB", (v, v, v)))


# ---------------------------------------------------------------- text layout
LATIN_FONT = ("Anton", FONTS_DIR / "Anton-Regular.ttf")
HINDI_FONT = ("Noto Sans Devanagari", FONTS_DIR / "NotoSansDevanagari-Bold.ttf")


def _fsize(text: str, ffile: Path, maxw: float, biggest: int, smallest: int) -> int:
    lo, hi = smallest, biggest
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ImageFont.truetype(str(ffile), mid).getlength(text) <= maxw:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _split(words: list[str], key: int, boxed: bool, ffile: Path, maxw: float, maxh: float, own: bool) -> list[list[str]]:
    """Words -> 1 or 2 lines (3 only for long text the owner typed): the split that allows the biggest letters."""
    n = len(words)
    if n <= 1:
        return [words]
    if boxed and key in (0, n - 1):  # the key word alone on its line
        rest = words[:key] if key == n - 1 else words[1:]
        parts = [rest]
        if len(rest) > 3:  # long text (the owner's own): the other words on two balanced lines
            f = ImageFont.truetype(str(ffile), 100)
            cut = min(range(1, len(rest)), key=lambda i: max(f.getlength(" ".join(rest[:i])), f.getlength(" ".join(rest[i:]))))
            parts = [rest[:cut], rest[cut:]]
        return parts + [[words[key]]] if key == n - 1 else [[words[key]]] + parts
    rng = [()] + [(i,) for i in range(1, n)]
    if own and n > 4:
        rng += [(i, j) for i in range(1, n) for j in range(i + 1, n)]
    f = ImageFont.truetype(str(ffile), 100)
    lh = 125 if DEVA.search(" ".join(words)) else 112
    best, best_v, best_w = [words], -1.0, 1e9
    for cut in rng:
        parts = [words[a:b] for a, b in zip((0,) + cut, cut + (n,))]
        widest = max(f.getlength(" ".join(p)) for p in parts)
        v = min(maxw / widest, maxh / (len(parts) * lh)) * (1.0 if len(parts) < 3 else 0.9)
        if len(parts) > len(best):
            better = v > best_v * 1.04  # fewer lines unless more give clearly bigger letters
        else:
            better = v > best_v * 1.001 or (v >= best_v * 0.999 and widest < best_w)  # same size: the balanced split
        if better:
            best, best_v, best_w = parts, v, widest
    return best


def _ass_colour(rgb, alpha: int = 0) -> str:
    return f"&H{alpha:02X}{rgb[2]:02X}{rgb[1]:02X}{rgb[0]:02X}&"


class _Text:
    """Lines placed in a box: sizes, positions and which word is highlighted."""

    def __init__(self, words, key, own, box, H, boxed, portrait, align):
        from .render import ass_text

        words = [ass_text(w).replace("\\N", " ") for w in words if w]
        self.hindi = any(DEVA.search(w) for w in words)
        self.fname, self.ffile = HINDI_FONT if self.hindi else LATIN_FONT
        self.box, self.align, self.boxed = box, align, boxed
        bw, bh = box[2] - box[0], box[3] - box[1]
        if boxed:
            bw *= 0.86  # room for the box around the key word
        lines = _split(words, key, boxed, self.ffile, bw, bh, own)
        if boxed and len(lines) < 2:
            self.boxed = boxed = False
        self.lines = [" ".join(l) for l in lines]
        # which line / word is the key
        self.key_line, self.key_word = -1, ""
        if 0 <= key < len(words):
            self.key_word = words[key]
            count = 0
            for i, l in enumerate(lines):
                if count <= key < count + len(l):
                    self.key_line = i
                count += len(l)
        biggest = int(H * (0.14 if portrait else 0.25))
        smallest = int(H * (0.035 if portrait else 0.08))
        sizes = [_fsize(l, self.ffile, bw, biggest, smallest) for l in self.lines]
        if len(sizes) > 1:  # stacked words, but not one tiny and one giant line
            cap = int(min(sizes) * 1.45)
            sizes = [min(s, cap) for s in sizes]
        if self.boxed and self.key_line >= 0 and len(sizes) > 1:
            sizes[self.key_line] = min(sizes[self.key_line], int(max(sizes) * 1.1))
        gap = 1.04 if not self.hindi else 0.98

        def heights(sz):
            return [s * (1.0 if not self.hindi else 1.25) for s in sz]

        tot = sum(heights(sizes)) * gap + (0.25 * max(sizes) if boxed else 0)
        if tot > bh:
            k = bh / tot
            sizes = [max(smallest, int(s * k)) for s in sizes]
        self.sizes = sizes
        hs = heights(sizes)
        extra = 0.22 * max(sizes) if boxed else 0
        total = sum(hs) * gap + extra
        y = box[1] + (bh - total) / 2
        self.ys, self.rects = [], []
        for i, (l, s, h) in enumerate(zip(self.lines, sizes, hs)):
            if boxed and i == self.key_line:
                y += extra / 2
            cy = y + h * gap / 2
            self.ys.append(cy)
            f = ImageFont.truetype(str(self.ffile), s)
            w = f.getlength(l)
            x = (box[0] + box[2]) / 2 if align == "c" else box[0]
            x0 = x - w / 2 if align == "c" else x
            ink = s * (0.92 if not self.hindi else 0.8)  # capital height of Anton / Devanagari letters
            self.rects.append((x0, cy - ink / 2, x0 + w, cy + ink / 2))
            y += h * gap + (extra / 2 if boxed and i == self.key_line else 0)
        self.xs = [(box[0] + box[2]) / 2 if align == "c" else box[0] for _ in self.lines]

    def block(self):
        return (min(r[0] for r in self.rects), min(r[1] for r in self.rects), max(r[2] for r in self.rects),
                max(r[3] for r in self.rects))

    def events(self, key_colour) -> list[str]:
        an = 5 if self.align == "c" else 4
        ev = []
        for i, (l, s, x, y) in enumerate(zip(self.lines, self.sizes, self.xs, self.ys)):
            lh = sum(ImageFont.truetype(str(self.ffile), s).getmetrics())  # libass sizes a font by its line height
            body = l
            colour = WHITE
            if i == self.key_line and self.boxed:
                colour = BLACK if key_colour == YELLOW else WHITE
            elif i == self.key_line and self.key_word:
                hl = "{\\c" + _ass_colour(key_colour) + "}" + self.key_word + "{\\c" + _ass_colour(WHITE) + "}"
                body = re.sub(r"(?<!\S)" + re.escape(self.key_word) + r"(?!\S)", lambda _m: hl, l, count=1)
            bord, shad = max(4, s // 10), max(3, s // 16)
            pos = f"\\an{an}\\pos({int(x)},{int(y)})\\fs{lh}"
            if not (i == self.key_line and self.boxed):  # soft dark glow behind the letters (readable on anything)
                ev.append(f"Dialogue: 0,0:00:00.00,0:00:10.00,T,,0,0,0,,{{{pos}\\bord{bord * 3}\\blur{max(6, s // 7)}"
                          f"\\1a&HFF&\\3c&H000000&\\3a&H70&\\shad0}}{l}")
                ev.append(f"Dialogue: 1,0:00:00.00,0:00:10.00,T,,0,0,0,,{{{pos}\\bord{bord}\\shad{shad}\\c{_ass_colour(colour)}"
                          f"\\3c&H000000&\\4c&H000000&\\4a&H40&}}{body}")
            else:
                ev.append(f"Dialogue: 1,0:00:00.00,0:00:10.00,T,,0,0,0,,{{{pos}\\bord0\\shad0\\c{_ass_colour(colour)}}}{body}")
        return ev


# ---------------------------------------------------------------- drawing
def _gradient(W, H, side: str, reach: float, strength: int) -> Image.Image:
    n = W if side in ("left", "right") else H
    ramp = np.clip(1 - np.arange(n) / (n * reach), 0, 1) ** 1.5 * strength
    if side in ("bottom", "right"):
        ramp = ramp[::-1]
    a = np.tile(ramp[None, :], (H, 1)) if side in ("left", "right") else np.tile(ramp[:, None], (1, W))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    layer.putalpha(Image.fromarray(a.astype(np.uint8)))
    return layer


def _arrow(d: ImageDraw.ImageDraw, a, b, width: float, fill, outline):
    a, b = np.array(a, float), np.array(b, float)
    v = b - a
    L = float(np.hypot(*v))
    if L < width * 3:
        return
    u = v / L
    nrm = np.array([-u[1], u[0]])
    head = min(L * 0.45, width * 2.6)
    neck = b - u * head
    pts = [a + nrm * width / 2, neck + nrm * width / 2, neck + nrm * width * 1.35, b, neck - nrm * width * 1.35,
           neck - nrm * width / 2, a - nrm * width / 2]
    d.polygon([tuple(p) for p in pts], fill=fill, outline=outline, width=max(3, int(width * 0.22)))


def _ring(d: ImageDraw.ImageDraw, cx, cy, r, width, colour):
    d.ellipse((cx - r - width * 0.35, cy - r - width * 0.35, cx + r + width * 0.35, cy + r + width * 0.35),
              outline=(0, 0, 0, 200), width=int(width * 1.7))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=colour + (255,), width=int(width))


def _rounded(d, rect, r, fill):
    d.rounded_rectangle(tuple(int(v) for v in rect), radius=int(r), fill=fill)


# ---------------------------------------------------------------- the thumbnail
def source_image(clip, project, variant: int) -> Image.Image:
    """The best frame (full size) of the clip's own section, or the frame kept when the video went to the PC."""
    from .ingest import stream_seconds
    from .thumbs import _thumb_source

    workdir, src, _W, _H = _thumb_source(clip, project)
    kept = workdir / f"thumb_frame_v{variant}.png"
    if not src.exists() and kept.exists():
        return Image.open(kept).convert("RGB")
    if not src.exists():
        raise RuntimeError("the video is not on the server and no frame was kept for this")
    dur = stream_seconds(src, "v:0") or 10.0
    t = choose_time(pick_times(src, dur), dur, variant)
    img = _grab(src, t, None)
    if img is None:
        img = _grab(src, dur * 0.35, None)
    if img is None:
        raise RuntimeError("no frame could be read from the video")
    return img


def compose(img: Image.Image, W: int, H: int, words: list[str], own: bool, layout: int, face: Image.Image | None = None,
            face_scale: float = 1.0, workdir: Path | None = None, out: Path | None = None) -> Path:
    """One designer thumbnail from a source picture. Pure (no database): used by make() and by the samples."""
    from .render import _esc
    from .thumbs import _paste_face

    portrait = H > W
    name = LAYOUTS[layout % len(LAYOUTS)]
    workdir = Path(workdir or ".")
    subj = subject(img)
    # where the subject goes, so the words never sit on it
    if portrait:
        target = {"top": (0.5, 0.64), "box": (0.5, 0.40), "band": (0.5, 0.64), "arrow": (0.5, 0.38)}[name]
        if face is not None:
            target = (0.5, 0.42)
    else:
        target = (0.70, 0.5) if face is None else (0.40, 0.45)
    zoom = 1.25 if (subj[3] and subj[2] < 0.12) else 1.15
    base, (sx, sy, sr) = crop_to(img, W, H, subj, target, zoom)
    base = grade(base).convert("RGBA")

    # the area every surface shows (Shorts: centre, clear of the top bar and the title at the bottom;
    # 16:9: left part, clear of the duration stamp at the bottom right)
    fimg = None
    if portrait:
        avail = [W * 0.07, H * 0.15, W * 0.93, H * 0.74]
        if face is not None:
            s = min(H * 0.54 / face.height, W * 0.9 / face.width) * face_scale
            fimg = face.resize((max(1, int(face.width * s)), max(1, int(face.height * s))), Image.LANCZOS)
            avail[3] = max(H * 0.46, min(H * 0.66, H - fimg.height - H * 0.04))
    else:
        avail = [W * 0.05, H * 0.08, W * 0.60, H * 0.90]
        if face is not None:
            s = min(H * 0.97 / face.height, W * 0.46 / face.width) * face_scale
            fimg = face.resize((max(1, int(face.width * s)), max(1, int(face.height * s))), Image.LANCZOS)
            avail[2] = min(W * 0.62, W - fimg.width - W * 0.035)
    ah = avail[3] - avail[1]
    if portrait:
        zone = {"top": (avail[0], avail[1], avail[2], avail[1] + ah * 0.48),
                "box": (avail[0], avail[3] - ah * 0.50, avail[2], avail[3]),
                "band": (avail[0], avail[1] + ah * 0.04, avail[2], avail[1] + ah * 0.44),
                "arrow": (avail[0], avail[3] - ah * 0.46, avail[2], avail[3])}[name]
        align = "c"
    else:
        zone = {"top": (avail[0], avail[1], avail[2], avail[1] + ah * 0.60),
                "box": (avail[0], avail[3] - ah * 0.62, avail[2], avail[3]),
                "band": (avail[0], avail[1] + ah * 0.12, avail[2] - W * 0.04, avail[3] - ah * 0.12),
                "arrow": (avail[0], avail[1] + ah * 0.18, avail[2] - W * 0.08, avail[3] - ah * 0.18)}[name]
        align = "c" if name == "band" else "l"
    boxed = name == "box" and len(words) > 1
    key = key_index(words, edge_only=boxed)
    txt = _Text(words, key, own, zone, H, boxed, portrait, align)
    key_colour = YELLOW if name in ("top", "box", "arrow") else RED
    if name == "band":
        key_colour = YELLOW

    # darkening behind the words
    if portrait:
        if name in ("top", "band"):
            base.alpha_composite(_gradient(W, H, "top", 0.55, 190))
        else:
            base.alpha_composite(_gradient(W, H, "bottom", 0.55, 200))
    else:
        base.alpha_composite(_gradient(W, H, "left", 0.75, 215))

    # graphics: drawn twice as large, then reduced (smooth edges)
    S = 2
    over = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    bx0, by0, bx1, by1 = txt.block()
    if name == "top":  # accent bar under the words
        bw = max(W * 0.18, (bx1 - bx0) * 0.45)
        cx = (bx0 + bx1) / 2 if align == "c" else bx0 + bw / 2
        y = by1 + max(H * (0.022 if portrait else 0.04), txt.sizes[-1] * 0.3)
        _rounded(d, ((cx - bw / 2) * S, y * S, (cx + bw / 2) * S, (y + H * (0.011 if portrait else 0.02)) * S),
                 H * 0.006 * S, RED + (255,))
    elif name == "box" and txt.boxed and txt.key_line >= 0:  # the key word in a tilted yellow box
        x0, y0, x1, y1 = txt.rects[txt.key_line]
        pad = txt.sizes[txt.key_line] * 0.16
        rect = (x0 - pad * 1.3, y0 - pad, x1 + pad * 1.3, y1 + pad)
        m = int(pad * 2 + 24)  # a small layer around the box only (turning the whole picture is slow)
        ox, oy = int(rect[0]) - m, int(rect[1]) - m
        box = Image.new("RGBA", ((int(rect[2] - rect[0]) + 2 * m) * S, (int(rect[3] - rect[1]) + 2 * m) * S), (0, 0, 0, 0))
        bd = ImageDraw.Draw(box)
        r0 = (rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy)
        _rounded(bd, tuple(v * S for v in (r0[0] + 8, r0[1] + 10, r0[2] + 8, r0[3] + 10)), pad * S * 0.6, (0, 0, 0, 150))
        _rounded(bd, tuple(v * S for v in r0), pad * S * 0.6, YELLOW + (255,))
        box = box.rotate(2.0, resample=Image.BICUBIC)
        over.alpha_composite(box, (ox * S, oy * S)) if ox >= 0 and oy >= 0 else over.paste(box, (ox * S, oy * S), box)
        d = ImageDraw.Draw(over)
    elif name == "box":  # one word: a bar under it instead
        _rounded(d, (bx0 * S, (by1 + H * 0.02) * S, bx1 * S, (by1 + H * 0.032) * S), H * 0.006 * S, YELLOW + (255,))
    elif name == "band":  # slanted dark band with yellow edges across the words
        tilt = H * (0.025 if portrait else 0.05)
        pad = (by1 - by0) * 0.28 + H * 0.02
        if portrait:
            top, bot = by0 - pad, by1 + pad
            poly = [(0, top + tilt), (W, top - tilt), (W, bot - tilt), (0, bot + tilt)]
        else:
            left, right = 0, bx1 + W * 0.06
            poly = [(left, 0), (right + tilt, 0), (right - tilt, H), (left, H)]
        d.polygon([(x * S, y * S) for x, y in poly], fill=(10, 10, 14, 175))
        e = int(H * (0.008 if portrait else 0.012) * S)
        if portrait:
            d.line([(0, (top + tilt) * S), (W * S, (top - tilt) * S)], fill=YELLOW + (255,), width=e)
            d.line([(0, (bot + tilt) * S), (W * S, (bot - tilt) * S)], fill=YELLOW + (255,), width=e)
        else:
            d.line([((right + tilt) * S, 0), ((right - tilt) * S, H * S)], fill=YELLOW + (255,), width=e)
    elif name == "arrow":  # a ring on the subject and an arrow from the words to it
        cx, cy = sx * W, sy * H
        r = min(max(sr * W * 0.8, W * (0.10 if portrait else 0.07)), W * (0.20 if portrait else 0.14))
        clear = not (bx0 - r * 0.5 < cx < bx1 + r * 0.5 and by0 - r * 0.6 < cy < by1 + r * 0.6)
        if fimg is not None:  # never circle something hidden behind the face photo
            fx0 = (W - fimg.width) / 2 if portrait else W - fimg.width - W * 0.015
            fy0 = H - fimg.height
            clear = clear and not (fx0 < cx < fx0 + fimg.width and cy > fy0)
        if clear and 0.05 * W < cx < 0.95 * W and 0.08 * H < cy < 0.92 * H:
            _ring(d, cx * S, cy * S, r * S, max(8, W * 0.012) * S, RED)
            if portrait:
                a = ((bx0 + bx1) / 2 + (W * 0.12 if cx > W / 2 else -W * 0.12), by0 - H * 0.015)
            else:
                a = (bx1 + W * 0.01, (by0 + by1) / 2)
            v = np.array([cx - a[0], cy - a[1]])
            L = float(np.hypot(*v)) or 1
            b = (cx - v[0] / L * (r + W * 0.025), cy - v[1] / L * (r + W * 0.025))
            if np.hypot(b[0] - a[0], b[1] - a[1]) > W * 0.06:
                _arrow(d, (a[0] * S, a[1] * S), (b[0] * S, b[1] * S), W * (0.028 if portrait else 0.026) * S,
                       YELLOW + (255,), (0, 0, 0, 255))
        else:  # nothing clear to point at: an accent bar instead
            aw = max(W * 0.16, (bx1 - bx0) * 0.35)
            ax0 = (bx0 + bx1) / 2 - aw / 2 if align == "c" else bx0
            _rounded(d, (ax0 * S, (by0 - H * 0.03) * S, (ax0 + aw) * S, (by0 - H * 0.019) * S),
                     H * 0.006 * S, RED + (255,))
    base.alpha_composite(over.resize((W, H), Image.LANCZOS))

    if fimg is not None:
        if portrait:
            _paste_face(base, fimg, (W - fimg.width) // 2, H - fimg.height, int(W * 0.012))
        else:
            _paste_face(base, fimg, W - fimg.width - int(W * 0.015), H - fimg.height, int(H * 0.012))

    tag = f"td{os.getpid()}_{threading.get_ident() % 100000}"
    basef, assf, outf = workdir / f"{tag}_base.bmp", workdir / f"{tag}.ass", workdir / f"{tag}_out.bmp"
    base.convert("RGB").save(basef)
    ass = "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: T,{txt.fname},80,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,6,3,5,0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *txt.events(key_colour), "",
    ])
    if name == "box" and txt.key_line >= 0 and txt.boxed:  # the key word turns with its box
        ass = ass.replace(f"\\pos({int(txt.xs[txt.key_line])},{int(txt.ys[txt.key_line])})",
                          f"\\pos({int(txt.xs[txt.key_line])},{int(txt.ys[txt.key_line])})\\frz2", 1)
    assf.write_text(ass, encoding="utf-8")
    try:
        r = subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-i", basef.name,
                            "-vf", f"ass={assf.name}:fontsdir='{_esc(FONTS_DIR)}'", "-frames:v", "1", outf.name],
                           cwd=workdir, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("thumbnail text failed: " + r.stderr[-300:])
        final = Image.open(outf).convert("RGB")
        out = Path(out or workdir / "thumb.jpg")
        for q in (92, 86, 80, 72, 64, 55):  # YouTube refuses thumbnails over 2 MB
            buf = io.BytesIO()
            final.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
            if buf.tell() <= MAX_BYTES:
                break
        tmp = out.with_suffix(".tmp.jpg")
        tmp.write_bytes(buf.getvalue())
        try:
            tmp.replace(out)
        except OSError:  # Windows: the old picture is open somewhere (a viewer) — write over it instead
            out.write_bytes(buf.getvalue())
            tmp.unlink(missing_ok=True)
        return out
    finally:
        for f in (basef, assf, outf):
            f.unlink(missing_ok=True)


def make(clip, project) -> Path:
    """The designer thumbnail of a clip (thumb.jpg next to its video)."""
    from .faces import face_for
    from .thumbs import _thumb_source

    workdir, _src, W, H = _thumb_source(clip, project)
    variant = int(getattr(clip, "thumb_variant", 0) or 0)
    img = source_image(clip, project, variant)
    words, own = words_for(clip, project)
    face, fscale = None, 1.0
    fp = face_for(clip.destination_id)
    if fp and getattr(clip, "thumb_face", None) is not False:
        from .branding import face_scale

        face, fscale = Image.open(fp).convert("RGBA"), face_scale()
    lay = layout_for(clip.id, variant, owner_layout(clip))
    return compose(img, W, H, words, own, lay, face, fscale, workdir, workdir / "thumb.jpg")


def keep_frame(clip, project, variant: int) -> Path:
    """For the PC archive: the full-size best frame of this variant, so a later thumbnail can still be designed."""
    from .thumbs import _thumb_source

    workdir, _src, _W, _H = _thumb_source(clip, project)
    p = workdir / f"thumb_frame_v{variant}.png"
    source_image(clip, project, variant).save(p)
    return p


# ---------------------------------------------------------------- the slow timed job
def _today_key() -> str:
    from .db import utcnow

    return "thumb_redesign:" + utcnow().strftime("%Y-%m-%d")


def done_today() -> int:
    from .db import get_setting

    return int(get_setting(_today_key()) or 0)


def _count():
    from .db import set_setting

    set_setting(_today_key(), str(done_today() + 1))


def candidates(s, now):
    """Clips whose automatic thumbnail the old code made and that can still change: waiting for review, queued and not
    going public within 2 hours, or uploaded and not public yet. Soonest first. (A queued video may still be sent
    to YouTube meanwhile: the new opening frame replaces the file in one step, so an upload reads either the old or
    the new file, never half of one; its thumbnail then goes up with it.)"""
    from datetime import timedelta

    from .db import Clip

    soon = now + timedelta(hours=2)
    q = s.query(Clip).filter(Clip.thumb_custom.isnot(True), Clip.removed_at.is_(None), Clip.thumb_test.is_(None),
                             (Clip.thumb_style.is_(None)) | (Clip.thumb_style == CLASSIC),
                             Clip.status.in_(["rendered", "queued", "uploaded"]),
                             (Clip.status == "rendered") | (Clip.publish_at > soon) |
                             ((Clip.status == "uploaded") & (Clip.publish_at > now + timedelta(minutes=30))))
    out = [c for c in q.all() if c.status != "uploaded" or c.youtube_id]
    order = {"uploaded": 0, "queued": 1, "rendered": 2}
    return sorted(out, key=lambda c: (order[c.status], c.publish_at or now + timedelta(days=3650), c.id))


def refresh_old(max_run: int = PER_RUN) -> dict:
    """Timed job (per account, "thumbs.designer"): re-make old-style thumbnails, at most PER_DAY a day. Clips not on
    YouTube yet: new thumbnail + the Short's opening frame made again. Uploaded, not public: only the new thumbnail
    is sent (one thumbnails.set, 50 units), with room for 2 uploads left in the day's allowance."""
    from pathlib import Path as _P

    from . import quota
    from .db import Clip, Project, SessionLocal, utcnow

    res = {"remade": 0, "pushed": 0, "skipped": 0, "left_today": max(0, PER_DAY - done_today())}
    if res["left_today"] <= 0:
        return res
    now = utcnow()
    with SessionLocal() as s:
        for c in candidates(s, now):
            if res["remade"] + res["skipped"] >= max_run or done_today() >= PER_DAY:
                break
            p = s.get(Project, c.project_id)
            if p is None or not c.output_path:
                continue
            from .thumbs import frame_ready, make_thumbnail

            if not frame_ready(c, p):
                continue  # the video is on the PC only and no frame was kept: nothing to make it from
            uploaded = c.status == "uploaded"
            if uploaded and not (quota.DAILY_UNITS - quota.used() >= 2 * quota.UPLOAD_COST + quota.COST["thumbnail"]
                                 and quota.shared_room(quota.COST["thumbnail"])):
                break  # the day's allowance is for uploads first
            _count()
            try:
                c.thumb_path = str(make_thumbnail(c, p))
                c.thumb_note = None if not uploaded else c.thumb_note
                if (c.thumb_style or "").startswith("d"):
                    res["remade"] += 1
                else:
                    c.thumb_style = "x" + VERSION  # the designer failed for this clip: not tried again every day
                    res["skipped"] += 1
                s.commit()
            except Exception as e:  # noqa: BLE001
                s.rollback()
                c = s.get(Clip, c.id)
                c.thumb_style = "x" + VERSION
                s.commit()
                res["skipped"] += 1
                log.info("thumb redesign %s: %s", c.id, str(e)[:200])
                continue
            if uploaded:
                from . import youtube

                youtube.push_thumbnail(c)  # a refusal is retried by retry_thumbnails()
                res["pushed"] += 1
                s.commit()
            elif not c.pc_item_id:
                s.refresh(c)
                if c.status in ("rendered", "queued") and (_P(c.output_path).parent / "body.mp4").exists():
                    from .render import apply_short_cover

                    apply_short_cover(c)  # the Short's opening frame shows the new thumbnail
    res["left_today"] = max(0, PER_DAY - done_today())
    if res["remade"] or res["skipped"]:
        log.info("thumb redesign: %s", res)
    return res

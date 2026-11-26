"""Animated "Like + Subscribe + Bell" sticker at the top of every video (YouTube style).

Everything is drawn here with Pillow (shapes and the fonts in FONTS_DIR): no downloaded pictures, no AI. A short
clip with a transparent background (3.6 s, 25 frames a second) is made ONCE per size and kept on disk; a render only
lays that clip over the video, so it costs very little time per video.

What happens in the 3.6 s: the three buttons pop in -> a small white hand taps LIKE (the thumb turns blue and filled)
-> taps SUBSCRIBE (the red button turns grey "SUBSCRIBED") -> taps the bell (it rings) -> everything fades away.

Where it sits (see plan()):
* Shorts / square: at the top, right under the title band with the owner's face (never over the face badge or the
  title words), inside YouTube's safe area: not the top 8 %, not the right 15 %, not the bottom 22 %.
  Without a title band: right under the top 8 %.
* 16:9 videos: the top-right corner.
When: once from about 2 s (after the Short's cover moment), and a second time near the end when the clip is 25 s or
longer. Never at the same time as the small "like and subscribe" reminder pill (app/reminder.py), which stays as it is.

It is burned in by the last step of a render (render.apply_short_cover): body.mp4 stays clean, so a thumbnail made
from a frame of the video never shows the sticker. Clip.sub_anim_v says what a clip's finished video carries:
None = made before this existed, 0 = made without it (off / too short), VERSION = has it, -1 = adding it failed.

Account setting "sub_anim" (JSON): {"on": bool, "pos": "top" | "auto"}; each clip has its own switch
(Clip.sub_anim_on; empty = on); the Super Admin's switch is "clips.subscribe_anim".

Waiting clips (queued / waiting for review, not on YouTube yet) that were made without it get it by themselves, one
at a time (remake_tick, a timed job): the sticker is laid over the finished video (out.mp4, or body.mp4 when it is
still there), so title, thumbnail, publish time, credit, face, reminder and everything else stay exactly the same,
and no download or helper PC is needed. Only a clip without any video file on the server is rendered again through
the normal path (run_render: a missing source goes to the helper PC as always).
"""
import functools
import json
import logging
import math
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

VERSION = 1  # change when the look changes: waiting clips are then made again by themselves
FPS = 25
DUR = 3.6  # seconds the sticker is on screen
MIN_CLIP = 5.5  # shorter clips get none
FIRST_AT = 2.0  # first showing (the Short's cover is over by 0.7 s)
NOT_BEFORE = 1.0
SECOND_FROM = 25.0  # clips this long show it a second time near the end
GAP = 0.4  # seconds between the sticker and the reminder pill
RED = (255, 0, 0)
BLUE = (6, 95, 212)
INK = (15, 15, 15)
GREY_BG = (229, 229, 229)
GREY_TXT = (96, 96, 96)
POSITIONS = ("top", "auto")

# ---------------------------------------------------------------- settings
KEY = "sub_anim"


def settings() -> dict:
    """The account's choice (a fresh account: on, at the top)."""
    from .db import get_setting

    try:
        raw = json.loads(get_setting(KEY) or "{}") or {}
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {"on": raw.get("on") is not False, "pos": raw.get("pos") if raw.get("pos") in POSITIONS else "top"}


def save(on: bool, pos: str) -> dict:
    from .db import set_setting

    set_setting(KEY, json.dumps({"on": bool(on), "pos": pos if pos in POSITIONS else "top"}))
    return settings()


def wanted(clip, cfg: dict | None = None) -> bool:
    """Should this clip carry the sticker (its own switch, the Super Admin's switch, the account's switch)?"""
    if getattr(clip, "sub_anim_on", None) is False:
        return False
    from . import acl

    if not acl.feature("clips.subscribe_anim"):
        return False
    return bool((cfg or settings()).get("on"))


# ---------------------------------------------------------------- drawing
@functools.lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont:
    from .config import FONTS_DIR

    for name in ("NotoSans-Bold.ttf", "Anton-Regular.ttf"):
        try:
            return ImageFont.truetype(str(FONTS_DIR / name), max(8, int(size)))
        except OSError:
            continue
    return ImageFont.load_default()


SS = 4  # everything is drawn 4x bigger, then made smaller: smooth edges


def _measure(h: int) -> dict:
    """Sizes of the three buttons for a button height of h pixels."""
    f = _font(h * 0.40 * SS)
    tw = lambda t: f.getlength(t) / SS  # noqa: E731
    like_w = h * 0.30 + h * 0.58 + h * 0.17 + tw("LIKE") + h * 0.40
    sub_w = max(tw("SUBSCRIBE"), tw("SUBSCRIBED")) + h * 0.80
    gap = h * 0.16
    row_w = like_w + gap + sub_w + gap + h
    m = h * 0.30  # room for the pop, the shadow and the bell's swing
    return {"h": h, "like_w": like_w, "sub_w": sub_w, "bell_w": float(h), "gap": gap, "row_w": row_w,
            "pad_x": m, "pad_top": h * 0.42, "pad_bottom": h * 1.15,
            "W": int(math.ceil(row_w + 2 * m)) // 2 * 2 + 2, "H": int(math.ceil(h * (1 + 0.42 + 1.15))) // 2 * 2 + 2}


def geometry(h: int) -> dict:
    """Public: canvas size and where the button row is inside it (for placement and tests)."""
    g = _measure(h)
    return {"canvas": (g["W"], g["H"]), "row": (g["pad_x"], g["pad_top"], g["pad_x"] + g["row_w"], g["pad_top"] + h)}


def _thumb_mask(n: int) -> Image.Image:
    """A thumbs-up hand as a mask n x n (drawn in a 100 x 100 design box)."""
    k = n / 100.0
    m = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(m)
    R = lambda *v: [x * k for x in v]  # noqa: E731
    d.rounded_rectangle(R(6, 44, 25, 94), radius=5 * k, fill=255)  # the cuff
    d.rounded_rectangle(R(31, 42, 91, 94), radius=13 * k, fill=255)  # the palm and fingers
    # the thumb: a thick rounded stroke going up and to the right
    for (x0, y0), (x1, y1), w in (((40, 50), (52, 14), 19),):
        d.line(R(x0, y0, x1, y1), fill=255, width=int(w * k))
        r = w * k / 2
        d.ellipse((x1 * k - r, y1 * k - r, x1 * k + r, y1 * k + r), fill=255)
    d.polygon(R(31, 54, 40, 36, 60, 40, 60, 52), fill=255)
    m = m.filter(ImageFilter.GaussianBlur(2.2 * k)).point(lambda v: 255 if v > 128 else 0)
    # the lines between the fingers on the right
    for y in (58, 71, 83):
        d2 = ImageDraw.Draw(m)
        d2.line(R(70, y, 92, y), fill=0, width=max(1, int(3.2 * k)))
    return m


def _bell_mask(n: int) -> Image.Image:
    k = n / 100.0
    m = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(m)
    R = lambda *v: [x * k for x in v]  # noqa: E731
    d.ellipse(R(44, 6, 56, 18), fill=255)  # the knob on top
    d.ellipse(R(23, 14, 77, 74), fill=255)  # the dome
    d.rectangle(R(23, 44, 77, 72), fill=255)
    d.polygon(R(23, 60, 77, 60, 88, 76, 12, 76), fill=255)  # the flare
    d.rounded_rectangle(R(10, 70, 90, 80), radius=5 * k, fill=255)  # the rim
    d.ellipse(R(41, 79, 59, 95), fill=255)  # the clapper
    return m.filter(ImageFilter.GaussianBlur(1.5 * k)).point(lambda v: 255 if v > 128 else 0)


def _hand_mask(n: int) -> Image.Image:
    """A pointing hand (index finger up), tip at (36 %, 4 %) of the box."""
    k = n / 100.0
    m = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(m)
    R = lambda *v: [x * k for x in v]  # noqa: E731

    def stroke(x0, y0, x1, y1, w):
        d.line(R(x0, y0, x1, y1), fill=255, width=int(w * k))
        for x, y in ((x0, y0), (x1, y1)):
            d.ellipse(R(x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=255)

    stroke(36, 12, 36, 58, 17)  # index finger
    stroke(52, 46, 52, 60, 15)  # the folded fingers
    stroke(66, 49, 66, 62, 14)
    stroke(79, 54, 79, 66, 13)
    stroke(16, 64, 28, 76, 14)  # the thumb
    d.rounded_rectangle(R(24, 52, 86, 96), radius=16 * k, fill=255)  # palm
    return m.filter(ImageFilter.GaussianBlur(1.2 * k)).point(lambda v: 255 if v > 128 else 0)


def _outline(mask: Image.Image, px: int) -> Image.Image:
    """The ring of a shape: the mask minus the mask made smaller by px."""
    size = max(3, px * 2 + 1)
    inner = mask.filter(ImageFilter.MinFilter(size if size % 2 else size + 1))
    return ImageChops.subtract(mask, inner)


def _grow(mask: Image.Image, px: int) -> Image.Image:
    size = max(3, px * 2 + 1)
    return mask.filter(ImageFilter.MaxFilter(size if size % 2 else size + 1))


def _shift(mask: Image.Image, dx: int, dy: int) -> Image.Image:
    """The mask moved by (dx, dy); what leaves the picture is gone (no wrapping round)."""
    out = Image.new("L", mask.size, 0)
    out.paste(mask, (dx, dy))
    return out


def _down(img: Image.Image, w: int, h: int) -> Image.Image:
    return img.resize((max(1, int(round(w))), max(1, int(round(h)))), Image.LANCZOS)


def _pill(w: float, h: float, radius: float, color) -> Image.Image:
    img = Image.new("RGBA", (int(w * SS), int(h * SS)), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=radius * SS, fill=color + (255,))
    return img


def _text(img: Image.Image, text: str, cx: float, cy: float, size: float, color):
    """Words centred on (cx, cy) of a 4x image (cx, cy in 1x pixels)."""
    f = _font(size * SS)
    d = ImageDraw.Draw(img)
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    d.text((cx * SS - (l + r) / 2, cy * SS - (t + b) / 2), text, font=f, fill=color + (255,))


def _icon(mask_fn, n: float, color, filled: bool, stroke: float) -> Image.Image:
    big = int(n * SS)
    m = mask_fn(big)
    if not filled:
        m = _outline(m, int(stroke * SS))
    img = Image.new("RGBA", (big, big), color + (0,))
    img.putalpha(m)
    return img


def _parts(h: int) -> dict:
    """Every button in each of its looks, at 2x the final size (scaled per frame for the pop)."""
    g = _measure(h)
    Z = 2  # parts are kept at 2x; a frame scales them to its own size
    out = {}
    for liked in (False, True):
        w = g["like_w"]
        img = _pill(w, h, h / 2, (255, 255, 255))
        ic = h * 0.58
        icon = _icon(_thumb_mask, ic, BLUE if liked else INK, liked, h * 0.055)
        img.alpha_composite(icon, (int(h * 0.30 * SS), int((h - ic) / 2 * SS - h * 0.03 * SS)))
        tx0 = h * 0.30 + ic + h * 0.17
        tw = _font(h * 0.40 * SS).getlength("LIKE") / SS
        _text(img, "LIKE", tx0 + tw / 2, h * 0.52, h * 0.40, BLUE if liked else INK)
        out[f"like{int(liked)}"] = _down(img, w * Z, h * Z)
    for subbed in (False, True):
        w = g["sub_w"]
        img = _pill(w, h, h * 0.24, GREY_BG if subbed else RED)
        _text(img, "SUBSCRIBED" if subbed else "SUBSCRIBE", w / 2, h * 0.52, h * 0.40, GREY_TXT if subbed else (255, 255, 255))
        out[f"sub{int(subbed)}"] = _down(img, w * Z, h * Z)
    for on in (False, True):
        img = Image.new("RGBA", (int(h * SS), int(h * SS)), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((0, 0, img.width - 1, img.height - 1), fill=(255, 255, 255, 255))
        ic = h * 0.60
        bell = _icon(_bell_mask, ic, INK, on, h * 0.06)
        img.alpha_composite(bell, (int((h - ic) / 2 * SS), int((h - ic) / 2 * SS)))
        out[f"bell{int(on)}"] = _down(img, h * Z, h * Z)
    hs = h * 0.95  # the hand
    big = int(hs * SS)
    m = _hand_mask(big)
    hand = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    edge = _grow(m, int(h * 0.035 * SS))
    hand.paste((30, 30, 30, 255), (0, 0), edge)
    white = Image.new("RGBA", (big, big), (255, 255, 255, 255))
    hand.paste(white, (0, 0), m)
    out["hand"] = _down(hand, hs * Z, hs * Z)
    out["hand_tip"] = (0.36 * hs, 0.04 * hs)
    out["Z"] = Z
    return out


def _ease_out_back(x: float) -> float:
    x = min(1.0, max(0.0, x))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def _ease_io(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def _lerp(a, b, x):
    return a + (b - a) * x


# the moments of the animation (seconds from its start)
T_LIKE, T_SUB, T_BELL = 0.85, 1.50, 2.12
T_OUT = 3.10


def _state(t: float) -> dict:
    """What the sticker looks like at time t."""
    st = {}
    for i, name in enumerate(("like", "sub", "bell")):
        t0 = 0.06 * i
        pop = _ease_out_back((t - t0) / 0.38)
        alpha = min(1.0, max(0.0, (t - t0) / 0.16))
        out = _ease_io((t - T_OUT - 0.05 * i) / 0.40)
        st[name] = {"s": max(0.01, pop * (1 - 0.25 * out)), "a": alpha * (1 - out), "dy": -out * 0.35}
    for name, at in (("like", T_LIKE), ("sub", T_SUB), ("bell", T_BELL)):
        press = 1 - 0.08 * math.sin(math.pi * min(1.0, max(0.0, (t - at + 0.05) / 0.18)))
        st[name]["s"] *= press
    st["liked"] = t >= T_LIKE
    st["subbed"] = t >= T_SUB
    st["bell_on"] = t >= T_BELL
    tb = t - T_BELL
    st["bell_angle"] = 16 * math.sin(2 * math.pi * 5.0 * tb) * math.exp(-2.6 * tb) if tb >= 0 else 0.0
    st["burst"] = (t - T_LIKE) / 0.40 if 0 <= t - T_LIKE <= 0.40 else None
    st["rings"] = tb if 0 <= tb <= 0.9 else None
    return st


def _hand_path(t: float, g: dict) -> tuple | None:
    """Where the hand's fingertip is at time t (x, y in canvas pixels, scale, alpha); None = not shown."""
    h, x0, y0 = g["h"], g["pad_x"], g["pad_top"]
    like = (x0 + g["like_w"] * 0.46, y0 + h * 0.62)
    sub = (x0 + g["like_w"] + g["gap"] + g["sub_w"] * 0.55, y0 + h * 0.62)
    bell = (x0 + g["like_w"] + g["gap"] * 2 + g["sub_w"] + h * 0.58, y0 + h * 0.80)
    start = (like[0] + h * 0.7, y0 + h * 1.12)
    leave = (bell[0] - h * 0.25, y0 + h * 1.12)
    keys = [(0.40, start), (T_LIKE - 0.05, like), (T_LIKE + 0.15, like), (T_SUB - 0.05, sub), (T_SUB + 0.15, sub),
            (T_BELL - 0.05, bell), (T_BELL + 0.30, bell), (T_BELL + 0.80, leave)]
    if t < keys[0][0] or t > keys[-1][0]:
        return None
    for (ta, pa), (tb, pb) in zip(keys, keys[1:]):
        if ta <= t <= tb:
            x = _ease_io((t - ta) / max(1e-6, tb - ta))
            pos = (_lerp(pa[0], pb[0], x), _lerp(pa[1], pb[1], x))
            break
    alpha = min(1.0, (t - 0.40) / 0.15) * (1 - _ease_io((t - (T_BELL + 0.45)) / 0.35))
    press = min(abs(t - T_LIKE), abs(t - T_SUB), abs(t - T_BELL))
    scale = 0.86 if press < 0.07 else 1.0
    return pos[0], pos[1], scale, max(0.0, alpha)


def _paste(canvas: Image.Image, part: Image.Image, cx: float, cy: float, scale: float, alpha: float, Z: int,
           angle: float = 0.0, pivot: tuple | None = None):
    if alpha <= 0.01 or scale <= 0.02:
        return
    img = part
    if angle:
        pv = pivot or (img.width / 2, img.height / 2)
        img = img.rotate(angle, resample=Image.BICUBIC, center=pv)
    w, h = img.width * scale / Z, img.height * scale / Z
    img = img.resize((max(1, int(round(w))), max(1, int(round(h)))), Image.LANCZOS)
    if alpha < 0.999:
        a = img.getchannel("A").point(lambda v: int(v * alpha))
        img.putalpha(a)
    canvas.alpha_composite(img, (int(round(cx - img.width / 2)), int(round(cy - img.height / 2))))


def render_frame(h: int, t: float, parts: dict | None = None) -> Image.Image:
    """One frame of the sticker (transparent background), button height h, at time t."""
    g = _measure(h)
    P = parts or _parts(h)
    Z = P["Z"]
    st = _state(t)
    layer = Image.new("RGBA", (g["W"], g["H"]), (0, 0, 0, 0))
    x, y = g["pad_x"], g["pad_top"]
    centers = {"like": (x + g["like_w"] / 2, y + h / 2),
               "sub": (x + g["like_w"] + g["gap"] + g["sub_w"] / 2, y + h / 2),
               "bell": (x + g["like_w"] + 2 * g["gap"] + g["sub_w"] + h / 2, y + h / 2)}
    for name, key in (("like", f"like{int(st['liked'])}"), ("sub", f"sub{int(st['subbed'])}"), ("bell", f"bell{int(st['bell_on'])}")):
        e = st[name]
        cx, cy = centers[name]
        cy += e["dy"] * h
        if name == "bell":
            _paste(layer, P[key], cx, cy, e["s"], e["a"], Z, st["bell_angle"], (P[key].width / 2, P[key].height * 0.18))
        else:
            _paste(layer, P[key], cx, cy, e["s"], e["a"], Z)
    d = ImageDraw.Draw(layer)
    if st["burst"] is not None:  # a small blue ring around LIKE when it is tapped
        k = st["burst"]
        cx = x + h * 0.30 + h * 0.29
        r = h * (0.30 + 0.16 * k)
        a = int(200 * (1 - k))
        d.ellipse((cx - r, y + h / 2 - r, cx + r, y + h / 2 + r), outline=BLUE + (a,), width=max(2, int(h * 0.035)))
    if st["rings"] is not None and st["bell"]["a"] > 0.5:  # little lines beside the ringing bell
        k = st["rings"]
        a = int(255 * min(1.0, k / 0.1) * max(0.0, 1 - k / 0.9))
        cx, cy = centers["bell"]
        for side in (-1, 1):
            for j, rr in enumerate((0.62, 0.78)):
                r = h * rr
                box = (cx - r, cy - r, cx + r, cy + r)
                start = -40 if side > 0 else 180 - 10
                d.arc(box, start=start, end=start + 50, fill=(255, 255, 255, a), width=max(2, int(h * 0.055)))
    # soft shadow under everything, so white buttons stay clear on a bright picture
    sh = layer.getchannel("A").filter(ImageFilter.GaussianBlur(h * 0.07)).point(lambda v: int(v * 0.55))
    base = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    base.putalpha(_shift(sh, 0, int(h * 0.05)))
    base.alpha_composite(layer)
    hp = _hand_path(t, g)
    if hp:
        hx, hy, hs, ha = hp
        hand = P["hand"]
        tipx, tipy = P["hand_tip"]
        w = hand.width / Z * hs
        # the fingertip sits on (hx, hy)
        cx = hx - tipx * hs + w / 2
        cy = hy - tipy * hs + w / 2
        hand_sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
        _paste(hand_sh, hand, cx, cy, hs, ha, Z)
        s2 = hand_sh.getchannel("A").filter(ImageFilter.GaussianBlur(h * 0.05)).point(lambda v: int(v * 0.5))
        shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
        shadow.putalpha(_shift(s2, int(h * 0.04), int(h * 0.06)))
        base.alpha_composite(shadow)
        base.alpha_composite(hand_sh)
    return base


# ---------------------------------------------------------------- the cached clip
_build_lock = threading.Lock()


def cache_dir() -> Path:
    from .config import BASE_DATA_DIR

    d = BASE_DATA_DIR / "cache" / "subscribe"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build(h: int) -> Path:
    """The sticker as a short clip with a transparent background (made once per size, then reused)."""
    from .config import ffmpeg_bin

    h = int(h)
    path = cache_dir() / f"sub_v{VERSION}_yt_h{h}.mov"
    if path.exists() and path.stat().st_size > 1000:
        return path
    with _build_lock:
        if path.exists() and path.stat().st_size > 1000:
            return path
        g = _measure(h)
        parts = _parts(h)
        tmp = path.with_suffix(f".{int(time.time() * 1000)}.tmp.mov")
        p = subprocess.Popen([ffmpeg_bin(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{g['W']}x{g['H']}",
                              "-r", str(FPS), "-i", "-", "-c:v", "png", "-pix_fmt", "rgba", "-f", "mov", str(tmp)],
                             stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for i in range(int(round(DUR * FPS))):
                p.stdin.write(render_frame(h, i / FPS, parts).tobytes())
            p.stdin.close()
            err = p.stderr.read().decode("utf-8", "replace")
            if p.wait() != 0 or not tmp.exists():
                raise RuntimeError("making the subscribe sticker failed: " + err[-300:])
        except BaseException:
            p.kill()
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(path)
        return path


def preview(h: int = 96, t: float = T_BELL + 0.12) -> bytes:
    """One still picture of the sticker for the dashboard (liked, subscribed, the bell ringing)."""
    import io

    cached = cache_dir() / f"preview_v{VERSION}_h{int(h)}_{int(t * 100)}.png"
    if cached.exists():
        return cached.read_bytes()
    buf = io.BytesIO()
    render_frame(int(h), t).save(buf, "PNG")
    data = buf.getvalue()
    try:
        cached.write_bytes(data)
    except OSError:
        pass
    return data


# ---------------------------------------------------------------- where and when
def _band_bottom(clip, W: int, H: int) -> float | None:
    """Lower edge of the title band with the face badge on a Short/square (branding.make_banner), None = no band."""
    if getattr(clip, "aspect", "9:16") == "16:9" or getattr(clip, "frame_title", None) is False:
        return None
    top, band_h = H * (0.085 if H > W else 0.04), H * (0.19 if H > W else 0.22)
    return top + band_h


def face_box(W: int, H: int) -> tuple:
    """Where branding.make_banner puts the round face badge (for the tests: the sticker must never cover it)."""
    top, band_h = int(H * (0.085 if H > W else 0.04)), int(H * (0.19 if H > W else 0.22))
    d = int(min(band_h * 0.55, W * 0.19)) + 10
    x = int(W * 0.045)
    y = top + (band_h - d) // 2
    return (x, y, x + d, y + d)


def _windows(dur: float, cta: dict | None) -> list[float]:
    """Start times of the sticker: once early, and once near the end of a long clip; never during the reminder."""
    busy = [(float(cta["start"]) - GAP, float(cta["end"]) + GAP)] if cta else []
    clear = lambda s: s >= NOT_BEFORE - 1e-6 and s + DUR <= dur - 0.3 + 1e-6 and all(s + DUR <= a or s >= b for a, b in busy)  # noqa: E731
    first = None
    for s in (FIRST_AT, max(NOT_BEFORE, dur - 0.3 - DUR) if dur < FIRST_AT + DUR + 0.3 else None,
              (busy[0][0] - DUR) if busy else None, (busy[0][1]) if busy else None):
        if s is not None and clear(round(s, 2)):
            first = round(s, 2)
            break
    if first is None:
        return []
    out = [first]
    if dur >= SECOND_FROM:
        s2 = (busy[0][0] - DUR) if busy else dur - 0.6 - DUR
        s2 = round(s2, 2)
        if s2 >= first + DUR + 4.0 and clear(s2):
            out.append(s2)
    return out


def plan(clip, W: int, H: int, dur: float, cfg: dict | None = None, cta="auto") -> dict | None:
    """Where and when the sticker goes on this clip. None = no sticker (switched off, or the clip is too short).
    cta: the reminder's plan (reminder.plan) — "auto" works it out here."""
    cfg = cfg or settings()
    if dur < MIN_CLIP or not wanted(clip, cfg):
        return None
    if cta == "auto":
        try:
            from .reminder import plan as rplan

            cta = rplan(clip, W, H, dur)
        except Exception:  # noqa: BLE001  (the reminder is drawn by its own code; here only its time matters)
            cta = None
    times = _windows(float(dur), cta)
    if not times:
        return None
    wide = W > H
    if wide:
        h = int(round(H * 0.075))
    else:
        h = int(round(min(W * 0.098, H * 0.075)))  # a Short: about as big as YouTube's own Subscribe button
    g = _measure(h)
    cw, ch = g["W"], g["H"]
    if wide:
        avail = W * 0.55
        if cw > avail:
            h = int(h * avail / cw)
            g = _measure(h)
            cw, ch = g["W"], g["H"]
        busy_top = cfg.get("pos") == "auto" and _top_busy(clip, times)
        rx = W * 0.975  # right edge of the button row
        x = rx - g["pad_x"] - g["row_w"]
        y = (H * 0.80 - h - g["pad_top"]) if busy_top else (H * 0.05 - g["pad_top"])
        if busy_top:
            x = W * 0.025 - g["pad_x"]
    else:
        left, right = W * 0.045, W * 0.85  # the right 15 % is under YouTube's buttons
        if g["row_w"] + 2 * g["pad_x"] > right - left + 2 * g["pad_x"] or cw > W * 0.85:
            h = int(h * (right - left) / g["row_w"])
            g = _measure(h)
            cw, ch = g["W"], g["H"]
        band = _band_bottom(clip, W, H)
        row_top = (band + H * 0.012) if band is not None else H * 0.09
        if cfg.get("pos") == "auto" and _top_busy(clip, times):
            row_top = H * 0.56
        x = left - g["pad_x"]
        y = row_top - g["pad_top"]
        # never into YouTube's bottom text area with the hand
        y = min(y, H * 0.78 - ch)
    x, y = int(round(x)), int(round(y))
    return {"h": h, "x": x, "y": y, "w": cw, "hh": ch, "times": times, "dur": DUR,
            "row": (x + g["pad_x"], y + g["pad_top"], x + g["pad_x"] + g["row_w"], y + g["pad_top"] + h)}


def _top_busy(clip, times: list) -> bool:
    try:
        from .reminder import _top_is_busy

        return any(_top_is_busy(clip, t, t + DUR) for t in times)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- burning it in
def ffmpeg_parts(p: dict, first_idx: int, src: str) -> tuple[list, str, str]:
    """Inputs and filter text that lay the sticker over the video stream `src` (a label like "[0:v]").
    Returns (extra input arguments, filter text ending in a label, that label)."""
    path = build(p["h"])
    args, fc, cur = [], "", src
    for i, t in enumerate(p["times"]):
        idx = first_idx + i
        args += ["-i", str(path)]
        lab = f"[sa{i}]"
        out = f"[sv{i}]"
        fc += (f"[{idx}:v]format=rgba,setpts=PTS-STARTPTS+{t:.2f}/TB{lab};"
               f"{cur}{lab}overlay={p['x']}:{p['y']}:eof_action=pass:format=auto{out};")
        cur = out
    return args, fc, cur


def video_wh(path: Path) -> tuple:
    try:
        from .ingest import video_size

        s = video_size(path)
        if s and s[0] and s[1]:
            return int(s[0]), int(s[1])
    except Exception:  # noqa: BLE001
        pass
    return None


def plan_for(clip, path: Path) -> dict | None:
    """The plan for the video file `path` of this clip (its real size), or None. Never raises."""
    try:
        wh = video_wh(path)
        if not wh:
            from .render import DIMS

            wh = DIMS.get(clip.aspect, DIMS["16:9"])
        dur = float((clip.end or 0) - (clip.start or 0))
        return plan(clip, wh[0], wh[1], dur)
    except Exception as e:  # noqa: BLE001
        from .render import _note

        _note("Putting the like & subscribe animation on a video", e)
        return None


def _final(fc: str, lab: str) -> str:
    """The filter text with its last label renamed to [v] (the stream that is written)."""
    return fc[:-1].rsplit(lab, 1)[0] + "[v]"


def with_cover(p: dict, cover_fc: str) -> tuple:
    """render.apply_short_cover: its cover filter (ending in [v]) followed by the sticker. Inputs 0 and 1 are the
    video and the thumbnail. (None, None) = the sticker could not be made (noted; the Short is made without it)."""
    try:
        args, part, lab = ffmpeg_parts(p, 2, "[vc]")
        return args, cover_fc[:-len("[v]")] + "[vc];" + _final(part, lab)
    except Exception as e:  # noqa: BLE001
        from .render import _note

        _note("Putting the like & subscribe animation on a video", e)
        return None, None


def burn(workdir: Path, src_name: str, p: dict, out_name: str = "out.mp4") -> bool:
    """Lay the sticker over workdir/src_name and write workdir/out_name (atomically). False = it did not work.
    Never raises (the sticker is an extra: a video is never lost or stopped because of it)."""
    from .config import ffmpeg_bin
    from .render import _note

    tmp = workdir / "out.sub.tmp.mp4"
    try:
        args, fc, lab = ffmpeg_parts(p, 1, "[0:v]")
        r = subprocess.run([ffmpeg_bin(), "-y", "-i", src_name, *args, "-filter_complex", _final(fc, lab),
                            "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", tmp.name],
                           cwd=workdir, capture_output=True, text=True)
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        _note("Putting the like & subscribe animation on a video", e)
        return False
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        _note("Putting the like & subscribe animation on a video", RuntimeError("ffmpeg: " + r.stderr[-300:]))
        return False
    tmp.replace(workdir / out_name)
    return True


# ---------------------------------------------------------------- waiting clips get it too
REMAKE_KEY = "sub_anim_remake"
WAITING = ("queued", "rendered")
STOP_AFTER = 3  # failures in a row
SECONDS_EACH = 25  # about how long laying it over one finished Short takes on the server (one encode pass)
SECONDS_FULL = 150  # a clip without any video file: a full render (download + render)
TICK_SECONDS = 120  # remake_tick's interval: the next clip starts at once when the worker is free, else on the next tick
SOON = timedelta(minutes=10)  # a clip going public (or, without a file, going to YouTube) sooner than this is left alone
PENDING_MAX = timedelta(hours=30)  # a full render that never ends (helper never came): given up, counted as failed
_state_lock = threading.RLock()  # every read-change-write of the state below (one server process)


def _rstate() -> dict:
    from .db import get_setting

    try:
        st = json.loads(get_setting(REMAKE_KEY) or "{}") or {}
    except (ValueError, TypeError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("row", 0)
    st.setdefault("done", 0)
    st.setdefault("failed", 0)
    st.setdefault("stopped", None)
    st.setdefault("pending", {})  # clips handed to a full render: id -> {"aq", "was", "at"}
    st.setdefault("active", {})  # the clip being re-made now: id -> {"was", "aq", "out"} (a restart gives it back)
    return st


def _rsave(st: dict):
    from .db import set_setting

    set_setting(REMAKE_KEY, json.dumps(st))


def _rmod(fn):
    """Read the state, change it with fn(st), save it — under one lock. Returns what fn returns."""
    with _state_lock:
        st = _rstate()
        out = fn(st)
        _rsave(st)
        return out


def _files(clip) -> tuple:
    from .config import DATA_DIR

    d = DATA_DIR / "clips" / str(clip.id)
    out = Path(clip.output_path) if clip.output_path else d / "out.mp4"
    return d, (d / "body.mp4") if (d / "body.mp4").exists() else None, out if out.exists() else None


def _needs(clip, cfg: dict, rcfg: dict | None = None) -> bool:
    """A waiting clip whose finished video does not carry the current sticker, and would get one if made now."""
    v = getattr(clip, "sub_anim_v", None)
    if v is not None and (v < 0 or v >= VERSION):
        return False  # has it, or adding it failed (the button tries those again)
    if clip.youtube_id or clip.pc_item_id or clip.status not in WAITING:
        return False
    if v is not None and v > 0 and _files(clip)[1] is None:
        return False  # an older sticker is burned into out.mp4 and the clean video is gone: never two stickers
    dur = float((clip.end or 0) - (clip.start or 0))
    if dur < MIN_CLIP or not wanted(clip, cfg):
        return False
    from .render import DIMS
    from .reminder import plan as rplan, settings as rsettings

    W, H = DIMS.get(clip.aspect, DIMS["16:9"])
    try:
        cta = rplan(clip, W, H, dur, rcfg or rsettings())
    except Exception:  # noqa: BLE001
        cta = None
    return bool(_windows(dur, cta))


def busy_windows(clip, dur: float | None = None, pad: float = 0.15) -> list:
    """[(start, end) seconds] where the FINISHED video (out.mp4) of this clip shows the sticker; [] when it has none.
    For picking thumbnail frames from out.mp4 (body.mp4 never has it)."""
    v = getattr(clip, "sub_anim_v", None)
    if v is None or v <= 0:
        return []
    dur = float(dur if dur is not None else (clip.end or 0) - (clip.start or 0))
    try:
        from .render import DIMS
        from .reminder import plan as rplan

        W, H = DIMS.get(getattr(clip, "aspect", "9:16"), DIMS["16:9"])
        cta = rplan(clip, W, H, dur)
    except Exception:  # noqa: BLE001
        cta = None
    return [(max(0.0, t - pad), t + DUR + pad) for t in _windows(dur, cta)]


def waiting(s=None) -> list:
    """Clips not on YouTube yet that do not carry the sticker and should (earliest publish time first)."""
    from .db import Clip, SessionLocal
    from .reminder import settings as rsettings

    cfg, rcfg = settings(), rsettings()
    own = s is None
    s = s or SessionLocal()
    try:
        rows = (s.query(Clip).filter(Clip.status.in_(WAITING), Clip.youtube_id.is_(None), Clip.pc_item_id.is_(None))
                .order_by(Clip.publish_at.is_(None), Clip.publish_at, Clip.id).all())
        return [c for c in rows if _needs(c, cfg, rcfg)]
    finally:
        if own:
            s.close()


def status() -> dict:
    """For the dashboard: how many waiting clips still need it, and about how long making them takes."""
    from . import acl

    st = _rstate()
    cfg = settings()
    on = cfg["on"] and acl.feature("clips.subscribe_anim")
    rows = waiting() if on else []
    full = sum(1 for c in rows if not (_files(c)[1] or _files(c)[2]))
    # one at a time; the next starts as soon as the render worker is free (new clips always go first). When the worker
    # is busy all the time, only the 2-minute tick starts one: minutes_if_busy
    secs = (len(rows) - full) * SECONDS_EACH + full * SECONDS_FULL
    return {"on": on, "waiting": len(rows), "need_full_render": full, "minutes": int(math.ceil(secs / 60)) if rows else 0,
            "minutes_if_busy": int(math.ceil(len(rows) * max(SECONDS_EACH, TICK_SECONDS) / 60)) if rows else 0,
            "done": st["done"], "failed": st["failed"], "stopped": st["stopped"], "full_render_waiting": len(st["pending"]),
            "version": VERSION}


def restart() -> dict:
    """The "Re-make all waiting clips" button: clear a stop, try again clips where it failed, start at once."""
    from sqlalchemy import update

    from .db import Clip, SessionLocal

    with SessionLocal() as s:
        s.execute(update(Clip).where(Clip.sub_anim_v == -1, Clip.status.in_(WAITING), Clip.youtube_id.is_(None))
                  .values(sub_anim_v=None))
        s.commit()
    _rmod(lambda st: st.update(stopped=None, row=0))
    _start_next()
    return status()


def _lead():
    try:
        from . import quota

        return timedelta(hours=quota.upload_lead_hours())
    except Exception:  # noqa: BLE001
        from .config import UPLOAD_LEAD_HOURS

        return timedelta(hours=UPLOAD_LEAD_HOURS)


def _tell(text: str):
    try:
        from .watchdog import deliver

        deliver(text)
    except Exception:  # noqa: BLE001
        log.warning("subscribe animation: could not tell the owner")
    try:
        from .alerts import note

        note("Adding the like & subscribe animation to waiting clips", RuntimeError(text))
    except Exception:  # noqa: BLE001
        pass


def recover_remakes(only: set | None = None) -> int:
    """A clip left "rendering" by a re-make that never finished (a redeploy / restart in the middle): give it back its
    status, autoqueue and video. Its out.mp4 is untouched until the new one replaces it in one step, so the finished
    video is always whole. Called by scheduler.recover() before it re-renders stuck clips, and by the watchdog."""
    from sqlalchemy import update

    from .db import Clip, SessionLocal

    def take(st):
        keys = [k for k in st["active"] if only is None or int(k) in only]
        return {k: st["active"].pop(k) for k in keys}

    with _state_lock:
        items = _rmod(take)
        n = 0
        if items:
            with SessionLocal() as s:
                for k, info in items.items():
                    res = s.execute(update(Clip).where(Clip.id == int(k), Clip.status == "rendering")
                                    .values(status=info.get("was") or "queued", autoqueue=info.get("aq"),
                                            output_path=info.get("out"), error=None))
                    n += res.rowcount
                s.commit()
    return n


def _worker_idle(skip: tuple | None = None) -> bool:
    """No job of ANY account waits or runs on the single render worker (jobs.executor), except `skip` (itself)."""
    from . import jobs

    with jobs._rlock:
        return not any(n > 0 for k, n in jobs._queued.items() if k != skip)


def _pick():
    """The next clip to re-make (earliest publish time first), or None."""
    from .db import utcnow

    now = utcnow()
    lead = _lead()
    for c in waiting():
        if c.publish_at and c.publish_at < now + SOON:
            continue  # goes public within minutes: never held up, not even for a minute
        d, body, out = _files(c)
        if not (body or out) and c.status == "queued" and c.publish_at and c.publish_at - lead < now + SOON:
            continue  # no file and its upload is due: a full render now would make it late
        return c.id
    return None


def _start_next(skip: tuple | None = None):
    """Hand the next waiting clip to the render worker if it is free. Returns its id or None."""
    from . import acl, jobs

    if not acl.feature("clips.subscribe_anim") or not settings()["on"]:
        return None
    st = _rstate()
    if st["stopped"] or st["active"] or not _worker_idle(skip):
        return None  # stopped, one is being made, or new clips / other work go first
    cid = _pick()
    if cid is not None:
        jobs.executor.submit(remake_one, cid)
    return cid


def remake_tick():
    """Timed job (every 2 minutes, per account): give ONE waiting clip the sticker when the render worker is free.
    Each re-make starts the next one itself while the worker stays free, so this tick is only the fallback."""
    from . import acl, jobs, tenant

    if not acl.feature("clips.subscribe_anim") or not settings()["on"]:
        return None
    if _rstate()["active"] and not jobs.queued("remake_one", tenant.current()):
        recover_remakes()  # a re-make that died without giving its clip back
    if _rstate()["pending"] and _rmod(_follow):  # full renders started earlier: see how they ended
        return None
    return _start_next()


def remake_one(cid: int) -> str:
    """Give one waiting clip the sticker. Everything else about the clip stays as it is. Then start the next one."""
    from . import tenant

    result = "skip"
    try:
        result = _remake(cid)
        return result
    finally:
        if result != "skip":
            try:
                _start_next(skip=(tenant.current(), "remake_one", cid))
            except Exception:  # noqa: BLE001
                log.warning("subscribe animation: could not start the next clip")


def _remake(cid: int) -> str:
    from sqlalchemy import update

    from .db import Clip, SessionLocal, utcnow

    with SessionLocal() as s:
        c = s.get(Clip, cid)
        if c is None or not _needs(c, settings()):
            return "skip"
        was, aq, out_path = c.status, c.autoqueue, c.output_path
        _rmod(lambda st: st["active"].__setitem__(str(cid), {"was": was, "aq": aq, "out": out_path}))
        res = s.execute(update(Clip).where(Clip.id == cid, Clip.status == was, Clip.youtube_id.is_(None))
                        .values(status="rendering", error=None, autoqueue=(was == "queued")))
        s.commit()
        if res.rowcount != 1:
            _rmod(lambda st: st["active"].pop(str(cid), None))
            return "skip"  # it changed meanwhile (uploading, edited...)
        s.refresh(c)
        d, body, out = _files(c)
        result = "fail"
        try:
            if body is not None:  # the clean video is still here: rebuild the finished one (Short cover + sticker)
                from .render import apply_short_cover

                apply_short_cover(c)
                result = "fail" if c.sub_anim_v == -1 else "ok"
            elif out is not None and c.sub_anim_v in (None, 0):  # only the finished video, no sticker in it yet
                p = plan_for(c, out)
                if p is None:
                    c.sub_anim_v, result = 0, "ok"
                elif burn(out.parent, out.name, p, out.name):
                    c.sub_anim_v, result = VERSION, "ok"
            elif out is None:  # no video file at all on the server: the normal render (a missing source -> helper PC)
                s.execute(update(Clip).where(Clip.id == cid).values(status="draft"))
                s.commit()
                _rmod(lambda st: (st["active"].pop(str(cid), None),
                                  st["pending"].__setitem__(str(cid), {"aq": aq, "was": was, "at": utcnow().isoformat()})))
                from .render import run_render

                try:
                    run_render(cid)
                finally:
                    with SessionLocal() as s2:  # stopped before it even started: back as it was
                        s2.execute(update(Clip).where(Clip.id == cid, Clip.status == "draft").values(status=was, autoqueue=aq))
                        s2.commit()
                    _rmod(_follow)
                return "full"
        except Exception as e:  # noqa: BLE001
            from .render import _note

            _note("Putting the like & subscribe animation on a waiting clip", e)
            result = "fail"
        vals = {"status": was, "autoqueue": aq, "output_path": out_path,
                "sub_anim_v": c.sub_anim_v if result == "ok" else -1}
        s.execute(update(Clip).where(Clip.id == cid, Clip.status == "rendering").values(**vals))
        s.commit()
    _rmod(lambda st: (st["active"].pop(str(cid), None), _count(st, result == "ok", cid)))
    return result


def _follow(st: dict) -> bool:
    """Full renders started by remake_one: done, still waiting for the helper PC, or failed.
    True = one is being made right now (the next clip waits; one waiting for the helper does not hold the others up)."""
    from sqlalchemy import update

    from .db import Clip, SessionLocal, utcnow

    busy = False
    for key, info in list(st["pending"].items()):
        with SessionLocal() as s:
            c = s.get(Clip, int(key))
            if c is None:
                st["pending"].pop(key, None)
                continue
            try:
                since = datetime.fromisoformat(info.get("at") or "")
            except (TypeError, ValueError):
                since = utcnow()
            if c.status in ("rendering", "draft", "fetching") and utcnow() - since < PENDING_MAX:
                busy = busy or c.status == "rendering"
                continue  # still being made (the helper PC may be fetching its video)
            ok = c.status in WAITING and c.sub_anim_v is not None and c.sub_anim_v >= 0
            if c.status in WAITING:
                s.execute(update(Clip).where(Clip.id == c.id).values(autoqueue=info.get("aq")))
            elif not ok:
                s.execute(update(Clip).where(Clip.id == c.id).values(sub_anim_v=-1))
            s.commit()
        st["pending"].pop(key, None)
        _count(st, ok, int(key))
    return busy


def _count(st: dict, ok: bool, cid):
    if ok:
        st["done"] = int(st["done"]) + 1
        st["row"] = 0
        return
    st["failed"] = int(st["failed"]) + 1
    st["row"] = int(st["row"]) + 1
    if st["row"] >= STOP_AFTER and not st["stopped"]:
        st["stopped"] = (f"Stopped after {st['row']} failures in a row (last: clip #{cid}). The clips keep their "
                         "videos and upload as they are. Press “Re-make all waiting clips” to try again.")
        _tell("Adding the like & subscribe animation to waiting clips stopped: " + st["stopped"])


# ---------------------------------------------------------------- the dashboard's addresses
def router():
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import Response
    from pydantic import BaseModel

    r = APIRouter()

    class SubAnimIn(BaseModel):
        on: bool = True
        pos: str = "top"

    @r.get("/api/settings/subscribe-anim")
    def sub_anim_get():
        return {**settings(), "remake": status()}

    @r.post("/api/settings/subscribe-anim")
    def sub_anim_set(body: SubAnimIn):
        from . import acl

        acl.need("clips.edit")
        if body.pos not in POSITIONS:
            raise HTTPException(400, "Choose top or automatic.")
        return {**save(body.on, body.pos), "remake": status()}

    @r.get("/api/settings/subscribe-anim/preview.png")
    def sub_anim_preview():
        return Response(preview(), media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    @r.post("/api/subscribe-anim/remake")
    def sub_anim_remake():
        from . import acl

        acl.need("clips.rerender")  # the address needs the animation switch; making clips again needs this one
        if not acl.feature("clips.subscribe_anim") or not settings()["on"]:
            raise HTTPException(409, "Switch the like & subscribe animation on first.")
        return restart()

    return r

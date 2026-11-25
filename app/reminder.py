"""A small, polite "like and subscribe" reminder on the video.

One short line in a soft rounded pill, shown once for about three seconds near the end of the clip, fading in and
out. No sound, no blinking, no bouncing. It is drawn by the same subtitle renderer as the captions, so Hindi is
shaped correctly. Emoji are not possible (the fonts here have none), so the wording is plain words only.

Where it sits:
* Shorts / square: under the picture, left of YouTube's own buttons — never in the bottom 22 % or the right 15 % of
  the frame, never on the title band at the top, and moved up when captions would reach it.
* 16:9 videos: a small pill in the top-right corner (bottom-left instead when captions or a text are at the top).

Account setting "reminder" (JSON): {"on": bool, "preset": key | "custom", "custom": text}. Each clip has its own
switch (Clip.cta_on; empty = on).
"""
import json
import math
import re
import struct

MAX_LEN = 40
MIN_CLIP = 8.0  # shorter clips get no reminder
FIRST_AT = 5.0  # never in the first seconds
SHOW_FOR = 3.2
FADE_MS = 400
PRESETS = [
    ("share", "Like, share and subscribe for more"),
    ("share_hinglish", "Like, share aur subscribe karein"),
    ("share_hi", "लाइक, शेयर और सब्सक्राइब करें"),
    ("en", "Like and subscribe for more"),
    ("en2", "Enjoyed it? Like and subscribe"),
    ("hinglish", "Pasand aaya? Like aur subscribe karein"),
    ("hi", "पसंद आया? लाइक और सब्सक्राइब करें"),
]
DEFAULT = "share"  # a fresh account, or one that never chose: like, share and subscribe
# letters (English, accented, Hindi), digits and simple punctuation; everything else (emoji, symbols) cannot be drawn
_NOT_ALLOWED = re.compile("[^0-9A-Za-zÀ-ɏऀ-ॿ‌‍ .,!?'&:-]")
_SPACES = re.compile(" {2,}")


def clean(text) -> str:
    """Custom wording made safe to draw: one line, plain letters and simple punctuation, at most 40 characters."""
    t = str(text or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    t = _NOT_ALLOWED.sub("", t.replace("’", "'"))
    return _SPACES.sub(" ", t).strip()[:MAX_LEN].strip()


def settings() -> dict:
    """The account's choice, with defaults filled in (a fresh account: on, English, "like, share and subscribe")."""
    from .db import get_setting

    try:
        raw = json.loads(get_setting("reminder") or "{}") or {}
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    names = dict(PRESETS)
    custom = clean(raw.get("custom"))
    preset = raw.get("preset") if raw.get("preset") in names or raw.get("preset") == "custom" else DEFAULT
    if preset == "custom" and not custom:
        preset = DEFAULT
    return {"on": raw.get("on") is not False, "preset": preset, "custom": custom,
            "text": custom if preset == "custom" else names[preset],
            "presets": [{"key": k, "text": v} for k, v in PRESETS], "max": MAX_LEN}


def save(on: bool, preset: str, custom: str) -> dict:
    from .db import set_setting

    set_setting("reminder", json.dumps({"on": bool(on), "preset": preset, "custom": clean(custom)}))
    return settings()


_RATIO: dict[str, float] = {}  # font file -> letter size for subtitle size 1 (a fact about the font, same for everyone)


def _font_path(hindi: bool) -> str:
    from .config import FONTS_DIR

    return str(FONTS_DIR / ("NotoSansDevanagari-Bold.ttf" if hindi else "NotoSans-Bold.ttf"))


def em_ratio(hindi: bool) -> float:
    """The subtitle renderer makes the font's Windows height (winAscent + winDescent) equal to the size it is given,
    so the real letter size (em) is smaller, and differs per font. Read it from the font file once."""
    path = _font_path(hindi)
    if path not in _RATIO:
        try:
            with open(path, "rb") as f:
                b = f.read()
            tabs = {}
            for i in range(struct.unpack(">H", b[4:6])[0]):
                tag, _, off, _ = struct.unpack(">4sIII", b[12 + 16 * i: 28 + 16 * i])
                tabs[tag] = off
            upm = struct.unpack(">H", b[tabs[b"head"] + 18: tabs[b"head"] + 20])[0]
            win_a, win_d = struct.unpack(">HH", b[tabs[b"OS/2"] + 74: tabs[b"OS/2"] + 78])
            _RATIO[path] = upm / float(win_a + win_d)
        except Exception:  # noqa: BLE001  (font missing or unusual: the usual value for these fonts)
            _RATIO[path] = 0.525 if hindi else 0.658
    return _RATIO[path]


def text_width(text: str, em: float, hindi: bool) -> float:
    """Width in pixels of one line at letter size `em` (Hindi is measured unshaped: a little too wide, never too narrow)."""
    try:
        from PIL import ImageFont

        return float(ImageFont.truetype(_font_path(hindi), 200).getlength(text)) * em / 200.0
    except Exception:  # noqa: BLE001  (font file missing: a safe estimate, the pill is only a little too wide)
        return len(text) * em * 0.62


def _captions_top(clip, W: int, H: int, t0: float, t1: float) -> float | None:
    """Top edge (y) of bottom captions shown while the reminder is visible; None = no captions down there."""
    st = getattr(clip, "caption_style", None) or {}
    if not st.get("on") or (st.get("pos") or "bottom") != "bottom":
        return None
    from .render import DEVA

    fs = int(H * {"S": 0.034, "M": 0.044, "L": 0.056}.get(st.get("size"), 0.044))
    room = W * 0.86
    lines = 0
    for c in getattr(clip, "captions", None) or []:
        try:
            s, e, t = float(c.get("s", 0)), float(c.get("e", 0)), str(c.get("t", ""))
        except (TypeError, ValueError, AttributeError):
            continue
        if e < t0 - 0.2 or s > t1 + 0.2 or not t.strip():
            continue
        hindi = bool(DEVA.search(t))
        n = sum(max(1, math.ceil(text_width(part, fs * em_ratio(hindi), hindi) * 1.04 / room)) for part in t.split("\n"))
        lines = max(lines, n)
    if not lines:
        return None
    return H - int(H * 0.09) - lines * fs * 1.0


def _top_is_busy(clip, t0: float, t1: float) -> bool:
    st = getattr(clip, "caption_style", None) or {}
    if st.get("on") and getattr(clip, "captions", None) and st.get("pos") == "top":
        return True
    for o in getattr(clip, "overlays", None) or []:
        try:
            s = float(o.get("start", 0))
            e = float(o.get("end", s + 3))
        except (TypeError, ValueError, AttributeError):
            continue
        if o.get("pos") == "top" and str(o.get("text", "")).strip() and s < t1 and e > t0:
            return True
    return False


def plan(clip, W: int, H: int, dur: float, cfg: dict | None = None) -> dict | None:
    """Where and when the reminder goes for this clip. None = no reminder (switched off, or the clip is too short)."""
    from .render import DEVA

    if getattr(clip, "cta_on", None) is False or dur < MIN_CLIP:
        return None
    from . import acl

    if not acl.feature("clips.reminder"):  # switched off for this account by the Super Admin: no reminder is drawn
        return None
    cfg = cfg or settings()
    text = clean(cfg.get("text"))
    if not cfg.get("on") or not text:
        return None
    start = max(FIRST_AT, dur - 4.5)
    end = min(start + SHOW_FOR, dur - 0.4)
    if end - start < 2.0:
        return None
    hindi = bool(DEVA.search(text))
    wide = W > H
    em = min(W, H) * (0.0285 if hindi else 0.0265)  # letter size: small, but readable on a phone
    limit = W * (0.5 if wide else 0.74)
    while True:
        pad, h = em * 1.25, em * 2.2
        w = text_width(text, em, hindi) + 2 * pad
        if w <= limit or em <= min(W, H) * 0.017:
            break
        em *= 0.96  # long wording: slightly smaller letters, never cut
    w = min(w, limit)
    fs = max(8, int(round(em / em_ratio(hindi))))
    if wide:
        if _top_is_busy(clip, start, end):
            cx, cy = W * 0.03 + w / 2, H * 0.78
        else:
            cx, cy = W * 0.975 - w / 2, H * 0.045 + h / 2
    else:
        cx, cy = W * 0.46, H * 0.705
        cap = _captions_top(clip, W, H, start, end)
        if cap is not None and cy + h / 2 > cap - h * 0.35:
            cy = max(H * 0.56, cap - h * 0.35 - h / 2)  # captions are tall here: sit just above them
        cy = min(cy, H * 0.78 - h / 2)  # never in the part YouTube's own text covers
        cx = min(cx, W * 0.85 - w / 2)  # ... nor under its buttons on the right
    return {"text": text, "start": round(start, 2), "end": round(end, 2), "fs": fs, "w": int(round(w)), "h": int(round(h)),
            "cx": int(round(cx)), "cy": int(round(cy)), "font": "Noto Sans Devanagari" if hindi else "Noto Sans"}


def ass_events(p: dict) -> list[str]:
    """The two subtitle lines: the rounded pill (a drawing) and the words on it. Both fade in and out together."""
    from .render import _ass_time, ass_text

    w, h = int(p["w"]), int(p["h"])
    r = h / 2.0
    k = r * 0.5523  # circle made of curves
    n = lambda v: f"{v:.1f}"  # noqa: E731
    shape = (f"m {n(r)} 0 l {n(w - r)} 0 b {n(w - r + k)} 0 {n(w)} {n(r - k)} {n(w)} {n(r)} "
             f"b {n(w)} {n(r + k)} {n(w - r + k)} {n(h)} {n(w - r)} {n(h)} l {n(r)} {n(h)} "
             f"b {n(r - k)} {n(h)} 0 {n(r + k)} 0 {n(r)} b 0 {n(r - k)} {n(r - k)} 0 {n(r)} 0")
    t0, t1 = _ass_time(float(p["start"])), _ass_time(float(p["end"]))
    x0, y0 = int(p["cx"] - w / 2), int(p["cy"] - h / 2)
    fade = f"\\fad({FADE_MS},{FADE_MS})"
    words = ass_text(p["text"]).replace("\\N", " ")
    return [
        f"Dialogue: 4,{t0},{t1},Default,,0,0,0,,{{\\an7\\pos({x0},{y0})\\bord0\\shad0\\1c&H1A1A1A&\\1a&H59&{fade}\\p1}}{shape}{{\\p0}}",
        f"Dialogue: 5,{t0},{t1},Default,,0,0,0,,{{\\an5\\pos({int(p['cx'])},{int(p['cy'])})\\fn{p['font']}\\fs{int(p['fs'])}"
        f"\\b1\\bord0\\shad0\\q2\\c&HFFFFFF&\\1a&H08&{fade}}}{words}",
    ]

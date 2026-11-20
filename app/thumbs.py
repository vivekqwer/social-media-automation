"""Automatic thumbnails: a strong frame from the rendered clip + big, readable text (Hindi works too)."""
import re
import subprocess
from pathlib import Path

from .config import FONTS_DIR, ffmpeg_bin
from .ingest import stream_seconds
from .render import _esc


def default_text(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").split("|")[0]).strip()
    return " ".join(t.split()[:6]).upper()[:42]


def _two_lines(text: str) -> tuple[str, str]:
    words = text.split()
    if len(words) < 2:
        return text, ""
    best, cut = None, 1
    for i in range(1, len(words)):  # most balanced split
        diff = abs(len(" ".join(words[:i])) - len(" ".join(words[i:])))
        if best is None or diff < best:
            best, cut = diff, i
    return " ".join(words[:cut]), " ".join(words[cut:])


def make_thumbnail(clip, project) -> Path:
    src = Path(clip.output_path)
    workdir = src.parent
    portrait = clip.aspect == "9:16"
    W, H = (1080, 1920) if portrait else (1280, 720)
    text = (clip.thumb_text or default_text(clip.title or project.title)).replace("{", "(").replace("}", ")").strip()
    l1, l2 = _two_lines(text)
    fs = int(W * (0.115 if portrait else 0.088))
    align, mv = (8, int(H * 0.12)) if portrait else (2, int(H * 0.07))
    lr = int(W * 0.05)
    body = l1 + (("\\N{\\c&H00D7FF&}" + l2) if l2 else "")  # second line in yellow
    ass = "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: T,Noto Sans,{fs},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{max(4, fs // 12)},{max(2, fs // 25)},{align},{lr},{lr},{mv},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 0,0:00:00.00,0:00:10.00,T,,0,0,0,,{body}", "",
    ])
    (workdir / "thumb.ass").write_text(ass, encoding="utf-8")
    dur = stream_seconds(src, "v:0") or 10.0
    at = max(0.0, dur * 0.35)  # past the intro, before the end
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"eq=contrast=1.12:saturation=1.35,vignette=PI/5,ass=thumb.ass:fontsdir='{_esc(FONTS_DIR)}'")
    r = subprocess.run([ffmpeg_bin(), "-y", "-ss", f"{at:.2f}", "-i", src.name, "-frames:v", "1", "-vf", vf, "-q:v", "3", "thumb.jpg"],
                       cwd=workdir, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("thumbnail failed: " + r.stderr[-300:])
    return workdir / "thumb.jpg"

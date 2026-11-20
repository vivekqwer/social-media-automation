import re
import subprocess
import textwrap
from pathlib import Path

from .config import DATA_DIR, FONT_PATH, FONTS_DIR, ffmpeg_bin
from .db import Clip, Project, SessionLocal
from . import workers
from .ingest import download_section, friendly_error, is_bot_block
from .policy import check_edit

DIMS = {"16:9": (1280, 720), "9:16": (1080, 1920), "1:1": (1080, 1080)}
COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
COLOR_NAMES = {"white", "black", "red", "yellow", "green", "blue", "orange", "pink", "cyan", "magenta", "gray"}


DEVA = re.compile(r"[\u0900-\u097F]")


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(cues: list, style: dict, W: int, H: int, path: Path, overlays: list | None = None):
    size = {"S": 0.034, "M": 0.044, "L": 0.056}.get((style or {}).get("size"), 0.044)
    fs = int(H * size)
    m = COLOR_RE.match((style or {}).get("color") or "#ffffff")
    rgb = m.group(1) if m else "ffffff"
    bgr = rgb[4:6] + rgb[2:4] + rgb[0:2]
    pos = (style or {}).get("pos", "bottom")
    align, mv = {"top": (8, int(H * 0.08)), "center": (5, 0)}.get(pos, (2, int(H * 0.09)))
    out = max(2, int(fs * 0.09))
    lr = int(W * 0.07)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Noto Sans,{fs},&H00{bgr},&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{out},0,{align},{lr},{lr},{mv},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for c in cues:
        text = str(c.get("t", "")).replace("{", "(").replace("}", ")").replace(chr(10), "\\N").strip()
        if text:
            lines.append(f"Dialogue: 0,{_ass_time(float(c['s']))},{_ass_time(float(c['e']))},Default,,0,0,0,,{text}")
    for o in overlays or []:  # text overlays that need font fallback (Hindi + English in one line)
        text = str(o.get("text", "")).replace("{", "(").replace("}", ")").replace(chr(10), "\\N").strip()
        if not text:
            continue
        t0 = max(0.0, float(o.get("start", 0)))
        t1 = max(t0 + 0.2, float(o.get("end", t0 + 3)))
        osz = max(16, min(200, int(o.get("size") or 56)))
        mo = COLOR_RE.match(str(o.get("color") or "#ffffff"))
        orgb = mo.group(1) if mo else "ffffff"
        obgr = orgb[4:6] + orgb[2:4] + orgb[0:2]
        an, y0 = {"top": (8, int(H * 0.08)), "center": (5, H // 2)}.get(o.get("pos"), (2, H - int(H * 0.08)))
        anim = o.get("anim", "fade")
        tags = f"\\an{an}\\fs{osz}\\c&H{obgr}&\\bord3"
        if anim == "slide_up":
            tags += f"\\move({W // 2},{y0 + 60},{W // 2},{y0},0,400)\\fad(300,300)"
        else:
            tags += f"\\pos({W // 2},{y0})" + ("\\fad(300,300)" if anim == "fade" else "")
        lines.append(f"Dialogue: 1,{_ass_time(t0)},{_ass_time(t1)},Default,,0,0,0,,{{{tags}}}{text}")
    path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")


def _esc(p) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:")


def _color(c: str) -> str:
    c = (c or "white").strip()
    m = COLOR_RE.match(c)
    if m:
        return "0x" + m.group(1)
    return c.lower() if c.lower() in COLOR_NAMES else "white"


def _drawtext(i: int, o: dict, W: int, workdir: Path) -> str:
    size = max(16, min(200, int(o.get("size") or 56)))
    s = max(0.0, float(o.get("start", 0)))
    e = max(s + 0.2, float(o.get("end", s + 3)))
    chars = max(8, int(W * 0.85 / (size * 0.55)))
    raw = str(o.get("text", ""))
    (workdir / f"ov_{i}.txt").write_text(textwrap.fill(raw, width=chars), encoding="utf-8")
    font = FONT_PATH
    base = {"top": "h*0.08", "center": "(h-text_h)/2"}.get(o.get("pos"), "h-text_h-h*0.08")
    anim = o.get("anim", "fade")
    y, alpha = base, "1"
    if anim in ("fade", "slide_up"):
        alpha = f"if(lt(t,{s + 0.3}),(t-{s})/0.3,if(gt(t,{e - 0.3}),({e}-t)/0.3,1))"
    if anim == "slide_up":
        y = f"({base})+(1-min(max((t-{s})/0.4,0),1))*60"
    return (
        f"drawtext=fontfile='{_esc(font)}':textfile=ov_{i}.txt:expansion=none:fontsize={size}"
        f":fontcolor={_color(o.get('color'))}:borderw=3:bordercolor=black:line_spacing=8"
        f":x=(w-text_w)/2:y='{y}':alpha='{alpha}':enable='between(t,{s},{e})'"
    )


def _has_audio(path) -> bool:
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    return "Audio:" in r.stderr


def build_filter(clip: Clip, W: int, H: int, dur: float, workdir: Path, has_voice: bool, has_audio: bool = True) -> str:
    if clip.aspect == "16:9":
        chain = [f"scale={W}:{H}:force_original_aspect_ratio=decrease", f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"]
    else:
        chain = [f"scale={W}:{H}:force_original_aspect_ratio=increase", f"crop={W}:{H}"]
    fx = clip.effects or []
    if "punch_in" in fx:
        chain += ["scale=trunc(iw*1.15/2)*2:trunc(ih*1.15/2)*2", f"crop={W}:{H}"]
    if "shake" in fx:
        chain += [
            "scale=trunc(iw*1.08/2)*2:trunc(ih*1.08/2)*2",
            f"crop={W}:{H}:x='(iw-ow)/2+sin(t*35)*(iw-ow)/2*0.8':y='(ih-oh)/2+cos(t*31)*(ih-oh)/2*0.8'",
        ]
    if "bw" in fx:
        chain.append("hue=s=0")
    if "fade" in fx:
        chain += ["fade=t=in:st=0:d=0.4", f"fade=t=out:st={max(dur - 0.4, 0):.2f}:d=0.4"]
    ass_ovs = []
    for i, o in enumerate(clip.overlays or []):
        txt = str(o.get("text", "")).strip()
        if not txt:
            continue
        if DEVA.search(txt):
            ass_ovs.append(o)  # Hindi text: drawtext cannot fall back between fonts, libass can
        else:
            chain.append(_drawtext(i, o, W, workdir))
    st = clip.caption_style or {}
    cues = clip.captions if (st.get("on") and clip.captions) else []
    if cues or ass_ovs:
        build_ass(cues, st, W, H, workdir / "captions.ass", ass_ovs)
        chain.append(f"ass=captions.ass:fontsdir='{_esc(FONTS_DIR)}'")
    vol = max(0.0, min(3.0, float(clip.orig_volume if clip.orig_volume is not None else 1.0)))
    fc = f"[0:v]{','.join(chain)}[v];"
    if has_audio:
        fc += f"[0:a]volume={vol},apad[a0];"  # pad: a short source audio track must not cut the clip short
    else:
        fc += f"anullsrc=r=44100:cl=stereo,atrim=duration={dur:.2f}[a0];"
    if has_voice:
        ms = int(max(0.0, float(clip.voice_offset or 0)) * 1000)
        fc += f"[1:a]adelay={ms}|{ms}[a1];"
        fc += "[a0][a1]amix=inputs=2:duration=first:dropout_transition=0,volume=2[a]"
    else:
        fc += "[a0]anull[a]"
    return fc


def render_clip(project: Project, clip: Clip) -> Path:
    workdir = DATA_DIR / "clips" / str(clip.id)
    workdir.mkdir(parents=True, exist_ok=True)
    src = download_section(project.video_id, clip.start, clip.end, workdir / "src")
    W, H = DIMS.get(clip.aspect, DIMS["16:9"])
    dur = clip.end - clip.start
    has_voice = bool(clip.voice_path) and Path(clip.voice_path).exists()
    has_audio = _has_audio(src)
    lead = max(0.0, clip.start - int(clip.start))  # section is downloaded from floor(start)
    cmd = [ffmpeg_bin(), "-y", "-ss", f"{lead:.2f}", "-i", str(src)]
    if has_voice:
        cmd += ["-i", str(clip.voice_path)]
    cmd += [
        "-filter_complex", build_filter(clip, W, H, dur, workdir, has_voice, has_audio),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", f"{dur:.2f}", "out.mp4",
    ]
    r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr[-600:])
    return workdir / "out.mp4"


def run_render(clip_id: int):
    """Background job: policy check -> render -> update status."""
    with SessionLocal() as s:
        clip = s.get(Clip, clip_id)
        project = s.get(Project, clip.project_id)
        errs = check_edit(project, clip)
        if errs:
            clip.status, clip.error = "failed", "; ".join(errs)
            s.commit()
            return
        clip.status, clip.error = "rendering", None
        s.commit()
        try:
            clip.output_path = str(render_clip(project, clip))
            clip.status = "queued" if (clip.autoqueue and clip.title and clip.description) else "rendered"
        except Exception as e:  # noqa: BLE001
            if is_bot_block(e) and workers.any_approved():
                clip.status, clip.error = "fetching", None  # the helper PC downloads the section, then we render
            else:
                clip.status, clip.error = "failed", friendly_error(e)
        s.commit()

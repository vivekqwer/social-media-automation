"""Quality check: every clip is looked at after it is made and right before it goes to YouTube. A clip without real
content (black or frozen picture, silence, mostly "subscribe" talk, the end screen, a repeat of a moment already used)
is never queued and never uploaded: it becomes a draft whose note starts with "Quality check:" (nothing is deleted;
"Use anyway" on the dashboard puts it back, and that is written in the activity log).

No AI service and nothing paid: ffmpeg reads the finished video once (small grey pictures, 2 a second, and the sound),
numpy measures them; the caption track and the "most replayed" graph the app already has are used when they exist.

Score 0-100 = 100 minus the penalties below (each with its plain-language reason). Below the threshold (setting
"quality_min", default 40, Super Admin 0-100) = rejected. The thresholds are on purpose conservative: one weak signal
alone never rejects a clip; it takes a clearly broken picture/sound, or several problems together.

  picture  black frames (>=90% of the picture dark)   share 15% -> 0 ... 60% -> 70 points
           dark screen with a little text (75-90% dark: credits, a title card)  30% -> 0 ... 80% -> 30 points
           frozen picture (no part of frames 0.5 s apart changes)  50% -> 0 ... 90% -> 40 points (+ up to 10 for
                                                       "barely moves" overall)
           very blurry (sharpness, a picture with contrast) on 80% of frames   10 points
  sound    no sound track                              65 points
           silence (below -50 dB for 0.6 s or more)    share 30% -> 0 ... 90% -> 65 points
           very quiet (mean below -42 dB)              10 points
  words    (full check, when captions exist) almost no talking in a talky video  15 points
           promotion talk ("subscribe", "sponsor", "link in the description", ...) 25% -> 20 ... 60% -> 40 points
           cut mid-sentence (only when the captions have punctuation)              5 points per end
  interest (full check) fewer replays than the video's average (most-replayed graph)  up to 15 points
           the last 20 s of a video (end screen / outro)    25 points;  only the first seconds (intro)  10 points
  repeat   the same moment of the same video as an older clip for the same channel (made, waiting or on YouTube): 0

Own videos (Clips -> Upload my video, original reels, videos from the folder on the PC) get only the technical checks
(black / frozen / silence) with a softer frozen-picture rule (a photo slide show with a voice is fine).

If the check itself cannot run (no ffmpeg, a broken file, the video is on the PC), the clip is NOT rejected for it.
"""
import json
import logging
import re
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import update

from .db import Channel, Clip, PcItem, Project, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

VERSION = 1  # a stored score from another version of the check is made again
PERM = "clips.quality"
NOTE = "Quality check:"  # every rejected clip's note starts with this (like "Parked:")
REJECTED = NOTE + "%"
DEFAULT_MIN = 40
SWEEP_PER_RUN = 3  # queued clips checked by the slow timed job per round
SWEEP_PER_DAY = 30  # ... and per day (light on the small server)
SAFE_SHARE = 0.30  # the timed job never rejects more than this share of a channel's waiting clips: it tells the owner
FPS = 2.0  # small grey pictures per second of video
SIDE = 160  # their size (square; the shape does not matter for these measures)
MAX_FRAMES = 240  # a long video is sampled more thinly
SKIP_START = 1.0  # the first second of a finished Short shows the thumbnail (render.apply_short_cover) and fades in

PROMO = re.compile(r"\b(subscrib\w*|like and subscribe|smash (?:that|the) like|hit the bell|bell icon|notification bell|"
                   r"sponsor\w*|patreon|link in (?:the )?(?:description|bio)|links? below|discount code|promo code|use code|"
                   r"merch|thanks? for watching|thank you for watching|see you (?:in the )?next (?:video|time)|"
                   r"don'?t forget to|intro|outro|channel ko|subscribe karo|like karo|bell icon dabao)\b", re.I)

_lock = threading.Lock()


# ---------------------------------------------------------------- switches and settings (one copy = one account)
def allowed() -> bool:
    """The Super Admin's per-account switch "Quality check" (always allowed in the owner's own copy)."""
    try:
        from . import acl

        return acl.feature(PERM)
    except Exception:  # noqa: BLE001
        return True


def enabled() -> bool:
    try:
        return allowed() and (get_setting("quality_on") or "1") != "0"
    except Exception:  # noqa: BLE001  (database unreachable: behave as before the check existed)
        return False


def threshold() -> int:
    try:
        v = int(get_setting("quality_min") or DEFAULT_MIN)
    except (TypeError, ValueError):
        v = DEFAULT_MIN
    return min(max(v, 0), 100)


def _stats() -> dict:
    try:
        v = json.loads(get_setting("quality_stats") or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _count(key: str, n: int = 1):
    """Daily counters (checked / passed / rejected / used anyway), the last 21 days."""
    try:
        with _lock:
            st = _stats()
            day = utcnow().strftime("%Y-%m-%d")
            row = st.setdefault(day, {})
            row[key] = int(row.get(key, 0)) + n
            for k in sorted(st)[:-21]:
                st.pop(k, None)
            set_setting("quality_stats", json.dumps(st))
    except Exception:  # noqa: BLE001  (a counter must never stop a clip)
        pass


def week_counts(now=None) -> dict:
    now = now or utcnow()
    days = {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
    out = {"checked": 0, "passed": 0, "rejected": 0, "used_anyway": 0}
    for day, row in _stats().items():
        if day in days:
            for k in out:
                out[k] += int((row or {}).get(k, 0))
    return out


# ---------------------------------------------------------------- reading the video (ffmpeg + numpy)
def _ffmpeg() -> str:
    from .config import ffmpeg_bin

    return ffmpeg_bin()


def _ramp(x: float, a: float, b: float) -> float:
    return 0.0 if x <= a else 1.0 if x >= b else (x - a) / (b - a)


def file_sig(path) -> str | None:
    try:
        st = Path(path).stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except (OSError, TypeError):
        return None


def probe(path: str) -> dict:
    """Length, picture and sound streams, from ffmpeg's own description of the file (no ffprobe needed)."""
    r = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=30,
                       encoding="utf-8", errors="replace")
    err = r.stderr or ""
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return {"duration": dur, "video": bool(re.search(r"Stream #\S+.*: Video:", err)),
            "audio": bool(re.search(r"Stream #\S+.*: Audio:", err))}


def measure(path: str) -> dict:
    """Picture and sound measures of one video file. One ffmpeg run (plus a quick look at its streams)."""
    import numpy as np

    t0 = time.time()
    info = probe(path)
    dur = info["duration"]
    if not info["video"]:
        raise ValueError("the file has no picture")
    skip = SKIP_START if dur > 6 else 0.0
    span = max(dur - skip - 0.4, 0.5)  # the last moment fades out
    fps = min(FPS, MAX_FRAMES / span)
    # the decoder skips what these measures do not need (frames nothing refers to, the de-blocking filter): about half
    # the work of a full decode, so a 30 s Short takes a few seconds on the small server
    cmd = [_ffmpeg(), "-hide_banner", "-nostats", "-threads", "2", "-skip_frame:v", "noref", "-skip_loop_filter:v", "all",
           "-flags2:v", "fast", "-ss", f"{skip:.2f}", "-t", f"{span:.2f}", "-i", str(path),
           "-map", "0:v:0", "-vf", f"fps={fps:.4f},scale={SIDE}:{SIDE}:flags=area,format=gray", "-f", "rawvideo",
           "-pix_fmt", "gray", "pipe:1"]
    if info["audio"]:
        cmd += ["-map", "0:a:0", "-af", "silencedetect=noise=-50dB:d=0.6,volumedetect", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    err = (r.stderr or b"").decode("utf-8", "replace")
    raw = r.stdout or b""
    n = len(raw) // (SIDE * SIDE)
    if r.returncode != 0 and n == 0:
        raise RuntimeError("ffmpeg could not read the video: " + err[-200:])
    if n == 0:
        raise ValueError("no pictures could be read from the video")
    fr = np.frombuffer(raw[: n * SIDE * SIDE], dtype=np.uint8).reshape(n, SIDE, SIDE).astype(np.float32)
    dark = (fr < 30).mean(axis=(1, 2))
    black = float((dark >= 0.90).mean())
    dim = float(((dark >= 0.75) & (dark < 0.90)).mean())  # black screen with a little text: credits, a title card
    if n > 1:
        delta = np.abs(fr[1:] - fr[:-1])
        diffs = delta.mean(axis=(1, 2))
        # frozen = no part of the picture changes (a talking face in a still room still moves its own blocks)
        blocks = delta.reshape(n - 1, 8, SIDE // 8, 8, SIDE // 8).mean(axis=(2, 4)).max(axis=(1, 2))
        frozen = float((blocks < 2.0).mean())
        still = float((diffs < 1.5).mean())
        motion = float(np.median(diffs))
    else:
        frozen = still = 1.0
        motion = 0.0
    lap = np.abs(4 * fr[:, 1:-1, 1:-1] - fr[:, :-2, 1:-1] - fr[:, 2:, 1:-1] - fr[:, 1:-1, :-2] - fr[:, 1:-1, 2:])
    sharp = lap.var(axis=(1, 2))
    busy = fr.std(axis=(1, 2)) >= 15  # a plain one-colour card is empty, not blurry
    blurry = float(((sharp < 12) & busy & (dark < 0.90)).mean())
    out = {"duration": round(dur, 2), "frames": int(n), "black": round(black, 3), "dim": round(dim, 3), "frozen": round(frozen, 3),
           "still": round(still, 3), "motion": round(motion, 2), "blurry": round(blurry, 3), "audio": info["audio"]}
    if info["audio"]:
        sil = 0.0
        for a, b in re.findall(r"silence_start:\s*(-?[\d.]+)[\s\S]*?silence_end:\s*(-?[\d.]+)", err):
            sil += max(0.0, float(b) - max(float(a), 0.0))
        if re.search(r"silence_start:\s*(-?[\d.]+)(?![\s\S]*silence_end)", err):  # silent until the end
            last = re.findall(r"silence_start:\s*(-?[\d.]+)", err)[-1]
            sil += max(0.0, span - max(float(last), 0.0))
        m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", err)
        mean = -91.0 if not m or m.group(1) == "-inf" else float(m.group(1))
        out.update(silence=round(min(sil / span, 1.0), 3), mean_db=round(mean, 1))
    out["secs"] = round(time.time() - t0, 2)
    return out


# ---------------------------------------------------------------- words and interest (data the app already has)
def _words_in(track: dict | None, start: float, end: float) -> tuple[str, int, int, bool]:
    """Text of the caption track inside [start, end] (seconds of the source video), its words, all words of the track
    and whether the track has sentence punctuation at all."""
    if not track:
        return "", 0, 0, False
    from .captions import NOISE

    parts, total, punct = [], 0, 0
    for ev in track.get("events", []) or []:
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        text = NOISE.sub("", "".join(sg.get("utf8", "") for sg in segs)).replace("\n", " ").strip()
        if not text:
            continue
        total += len(text.split())
        punct += len(re.findall(r"[.!?।]", text))
        t0 = ev["tStartMs"] / 1000
        t1 = t0 + (ev.get("dDurationMs") or 0) / 1000
        if t1 > start and t0 < end:
            parts.append(text)
    txt = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return txt, len(txt.split()), total, punct >= 3


def _section_text(c: Clip, p: Project | None) -> tuple[str, int, int, bool]:
    if p is not None and p.caption_track:
        return _words_in(p.caption_track, float(c.start or 0), float(c.end or 0))
    caps = [str(x.get("t", "")) for x in (c.captions or []) if isinstance(x, dict)]
    txt = re.sub(r"\s+", " ", " ".join(caps)).strip()
    return txt, len(txt.split()), 0, bool(re.search(r"[.!?]", txt))


def _heat(p: Project | None, start: float, end: float) -> float | None:
    """The section's "most replayed" value compared with the video's average (1.0 = average). None = no graph."""
    hm = (p.heatmap if p is not None else None) or []
    vals, inside = [], []
    for seg in hm:
        try:
            a, b, v = float(seg["start_time"]), float(seg["end_time"]), float(seg["value"])
        except (KeyError, TypeError, ValueError):
            continue
        vals.append(v)
        if b > start and a < end:
            inside.append(v)
    if not vals or not inside or max(vals) <= 0:
        return None
    avg = sum(vals) / len(vals)
    return (sum(inside) / len(inside)) / avg if avg > 0 else None


def promo_share(text: str) -> tuple[float, int]:
    words = len(text.split())
    if not words:
        return 0.0, 0
    hits = PROMO.findall(text)
    used = sum(len(h.split()) for h in hits)
    return min(used * 3 / words, 1.0), len(hits)  # a promo phrase stands for the sentence around it (~3x its words)


def judge(m: dict | None, *, kind: str = "full", text: str = "", words: int = 0, track_words: int = 0,
          punct: bool = False, src_duration: float = 0, start: float = 0, end: float = 0, heat: float | None = None,
          repeat_of: int | None = None) -> tuple[int, list[str], list[list]]:
    """Score 0-100 and the reasons, from the measures. Pure: easy to test and to tune."""
    pen: list[list] = []  # [points, reason]

    def add(points: float, why: str):
        if points >= 1:
            pen.append([int(round(points)), why])

    full = kind == "full"
    if m:
        b = m.get("black", 0)
        add(70 * _ramp(b, 0.15, 0.60), f"{round(b * 100)}% black picture")
        dm = m.get("dim", 0)
        add(30 * _ramp(dm, 0.30, 0.80), f"{round(dm * 100)}% a dark screen with a little text (credits / title card)")
        f, st = m.get("frozen", 0), m.get("still", 0)
        if full:
            add(min(40 * _ramp(f, 0.50, 0.90) + 10 * _ramp(st, 0.80, 1.0), 45),
                "the picture does not move (a still image / slide)" if f >= 0.9 else "the screen barely moves")
        else:  # a slide show with a voice is a normal own video: only a completely still picture counts
            add(25 * _ramp(f, 0.85, 1.0), "the picture does not move at all")
        if m.get("blurry", 0) >= 0.8:
            add(10, "the picture is very blurry")
        if not m.get("audio"):
            add(65, "no sound at all")
        else:
            s = m.get("silence", 0)
            add(65 * _ramp(s, 0.30, 0.90), f"{round(s * 100)}% silence")
            if s < 0.3 and m.get("mean_db", 0) < -42:
                add(10, f"very quiet sound ({m.get('mean_db')} dB)")
    if full:
        length = max(end - start, 1.0)
        if track_words and src_duration > 0:
            wps_all, wps = track_words / src_duration, words / length
            if wps_all >= 1.0 and wps < 0.25 * wps_all:
                add(15, f"almost no talking in this part ({words} words)")
        share, hits = promo_share(text)
        if words >= 8 and hits >= 2 and share >= 0.25:
            add(max(20.0, 40 * _ramp(share, 0.25, 0.60)), "mostly 'subscribe' / promotion talk")
        if punct and text:
            if text[:1].islower():
                add(5, "starts in the middle of a sentence")
            if not re.search(r"[.!?।\"')]\s*$", text):
                add(5, "ends in the middle of a sentence")
        if heat is not None and heat < 0.7:
            add(15 * _ramp(0.7 - heat, 0.0, 0.4), f"fewer replays than the rest of the video ({heat:.1f}x the average)")
        if src_duration > 60 and start >= src_duration - 20:
            add(25, "the very end of the video (end screen / outro)")
        elif src_duration > 120 and end <= 12:
            add(10, "only the first seconds of the video (intro)")
    if repeat_of:
        pen.append([100, f"repeats the moment of clip #{repeat_of} (same video, same part)"])
    score = max(0, 100 - sum(p for p, _w in pen))
    pen.sort(key=lambda x: -x[0])
    return int(score), [w for _p, w in pen], pen


# ---------------------------------------------------------------- one clip
def kind_of(c: Clip, p: Project | None) -> str:
    """'tech' = only the technical checks: my own videos (upload, reels, the folder on the PC)."""
    return "tech" if c.pc_item_id or (p is not None and p.mode == "original") else "full"


def repeat_of(s, c: Clip) -> int | None:
    """An older clip of the same source video for the same channel covering the same moment (made, waiting or on
    YouTube). The older one keeps its place; a re-upload of a deleted video is not a repeat."""
    from .autopilot import LIVE_STATES

    if not c.project_id or c.start is None or c.end is None:
        return None
    q = s.query(Clip).filter(Clip.project_id == c.project_id, Clip.id != c.id, Clip.status.in_(LIVE_STATES),
                             Clip.removed_at.is_(None), Clip.start < c.end, Clip.end > c.start)
    for o in q.order_by(Clip.id).all():
        if o.reuploaded_as == c.id or c.reuploaded_as == o.id:
            continue
        if (o.destination_id or 0) != (c.destination_id or 0):
            continue
        if o.status in ("uploaded", "uploading") or o.id < c.id:
            return o.id
    return None


def fresh(c: Clip) -> bool:
    q = c.quality or {}
    return q.get("v") == VERSION and q.get("sig") is not None and q.get("sig") == file_sig(c.output_path)


def score(s, c: Clip, p: Project | None = None) -> dict:
    """Measure and score one clip (its finished video). Never raises: a check that cannot run gives score None."""
    p = p if p is not None else (s.get(Project, c.project_id) if c.project_id else None)
    kind = kind_of(c, p)
    q = {"v": VERSION, "kind": kind, "at": utcnow().isoformat() + "Z", "sig": file_sig(c.output_path)}
    if not c.output_path or q["sig"] is None:
        return {**q, "score": None, "reasons": [], "error": "the video is not on the server (it may be saved on the PC)"}
    try:
        m = measure(c.output_path)
    except Exception as e:  # noqa: BLE001
        log.warning("quality check of clip %s: %s", c.id, str(e)[:200])
        return {**q, "score": None, "reasons": [], "error": f"could not be checked: {str(e)[:150]}"}
    extra = {}
    if kind == "full":
        text, words, track_words, punct = _section_text(c, p)
        extra = dict(text=text, words=words, track_words=track_words, punct=punct,
                     src_duration=float((p.duration if p is not None else 0) or 0), start=float(c.start or 0),
                     end=float(c.end or 0), heat=_heat(p, float(c.start or 0), float(c.end or 0)))
        m["words"] = words
        if extra["heat"] is not None:
            m["heat"] = round(extra["heat"], 2)
    try:
        rep = repeat_of(s, c) if kind == "full" else None
    except Exception:  # noqa: BLE001
        rep = None
    sc, reasons, pen = judge(m, kind=kind, repeat_of=rep, **extra)
    return {**q, "score": sc, "reasons": reasons, "points": pen, "signals": m}


def _keep(old: dict | None, new: dict) -> dict:
    """A new score keeps what the owner decided (Use anyway) and what the clip was before it was rejected."""
    old = old or {}
    return {**new, **{k: old[k] for k in ("override", "was", "rejected_at", "by") if k in old}}


def failing(c: Clip, minimum: int | None = None) -> bool:
    q = c.quality or {}
    if q.get("override") or q.get("score") is None:
        return False
    return int(q["score"]) < (threshold() if minimum is None else minimum)


def note_for(q: dict) -> str:
    why = "; ".join(q.get("reasons") or []) or "too little real content"
    return (f"{NOTE} score {q.get('score')}/100 — {why}. It was not queued and will not go to YouTube. "
            "Open it to see why; press Use anyway if it is fine.")[:900]


def reject(s, c: Clip, by: str) -> bool:
    """Not queued, never uploaded: a draft with a plain note (nothing deleted). Only while it waits — never a clip that
    is being uploaded or is on YouTube. True when it was rejected now."""
    q = dict(c.quality or {})
    was = {"status": c.status, "publish_at": c.publish_at.isoformat() if c.publish_at else None, "autoqueue": bool(c.autoqueue)}
    q.update(was=was, rejected_at=utcnow().isoformat() + "Z", by=by)
    res = s.execute(update(Clip).where(Clip.id == c.id, Clip.status.in_(["queued", "rendered", "failed"]),
                                       Clip.youtube_id.is_(None))
                    .values(status="draft", autoqueue=False, publish_at=None, error=note_for(q), quality=q))
    s.commit()
    if res.rowcount != 1:
        return False
    s.refresh(c)
    if c.pc_item_id:
        try:
            row = s.get(PcItem, c.pc_item_id)
            if row is not None:
                row.note = (f"{NOTE} not uploaded (score {q.get('score')}/100: "
                            + "; ".join(q.get("reasons") or [])[:200] + "). See Clips → Rejected.")[:400]
                s.commit()
        except Exception:  # noqa: BLE001
            s.rollback()
    _count("rejected")
    try:
        from . import acl

        acl.record_here(PERM, f"Quality check rejected clip #{c.id} (score {q.get('score')}): "
                        + "; ".join(q.get("reasons") or [])[:300])
    except Exception:  # noqa: BLE001
        pass
    return True


def check(s, c: Clip, p: Project | None = None, force: bool = False) -> dict:
    """Score the clip when its stored score is missing or old (or `force`), save it, count it. Returns the score."""
    if not force and fresh(c):
        return c.quality
    q = _keep(c.quality, score(s, c, p))
    c.quality = q
    s.commit()
    if q.get("score") is not None:
        _count("checked")
        if q["score"] >= threshold():
            _count("passed")
    return q


# ---------------------------------------------------------------- the gates (every way a clip goes to YouTube)
def after_render(clip_id: int) -> bool | None:
    """render.run_render: a finished clip is checked before it can wait in the queue or for review.
    False = rejected now; None = not checked (switched off, or not waiting)."""
    if not enabled():
        return None
    try:
        with SessionLocal() as s:
            c = s.get(Clip, clip_id)
            if c is None or c.status not in ("queued", "rendered") or c.youtube_id:
                return None
            check(s, c)
            if failing(c) and reject(s, c, "render"):
                return False
            return True
    except Exception as e:  # noqa: BLE001  (the check must never break making clips)
        log.warning("quality check after render of clip %s: %s", clip_id, str(e)[:200])
        return None


def upload_ok(s, c: Clip, p: Project | None = None) -> bool:
    """scheduler.process_queue, right before the upload: the last gate. False = rejected now (not uploaded)."""
    if not enabled():
        return True
    q = c.quality or {}
    if q.get("override"):
        return True
    try:
        check(s, c, p)
    except Exception as e:  # noqa: BLE001
        log.warning("quality check before upload of clip %s: %s", c.id, str(e)[:200])
        return True
    if failing(c):
        reject(s, c, "upload")
        _rate_alert()
        return False
    return True


def need_ok(s, c: Clip, p: Project | None = None):
    """Approve / Upload now: a clip that fails the check is refused with the reasons (Use anyway lets it go)."""
    if not enabled() or (c.quality or {}).get("override"):
        return
    try:
        check(s, c, p)
    except Exception:  # noqa: BLE001
        return
    if failing(c):
        q = c.quality or {}
        raise HTTPException(409, f"{NOTE} score {q.get('score')}/100 — " + "; ".join(q.get("reasons") or [])
                            + ". It will not go to YouTube. Press Use anyway in the clip if it is fine.")


# ---------------------------------------------------------------- moments that would obviously fail are not taken
def weak_moment(p: Project, m: dict) -> str:
    start, end = float(m.get("start") or 0), float(m.get("end") or 0)
    dur = float(p.duration or 0)
    if dur > 60 and start >= dur - 20:
        return "end screen"
    if p.caption_track:
        text, words, total, _punct = _words_in(p.caption_track, start, end)
        if dur > 0 and total / dur >= 1.0 and words / max(end - start, 1.0) < 0.2 * (total / dur):
            return "almost no talking"
        share, hits = promo_share(text)
        if words >= 8 and hits >= 2 and share >= 0.35:
            return "promotion talk"
    return ""


def pick_moments(p: Project, moments: list) -> list:
    """autopilot._make_clips: leave out moments that are the end screen, nearly silent or mostly "subscribe" talk —
    as long as at least one other moment is left (the check after the render still looks at every clip)."""
    if not moments or not enabled():
        return moments
    try:
        good = [m for m in moments if not weak_moment(p, m)]
    except Exception:  # noqa: BLE001
        return moments
    return good or moments


# ---------------------------------------------------------------- queued clips made before the check existed
def _alert(text: str, key: str):
    """Once a day per kind: the owner's phone app / Telegram / Gmail (the usual alert path)."""
    day = utcnow().strftime("%Y-%m-%d")
    k = f"quality_alert:{key}"
    if get_setting(k) == day:
        return
    set_setting(k, day)
    try:
        from . import phone

        phone._say(text, title="Quality check", tags="warning", mail=True)
    except Exception as e:  # noqa: BLE001
        log.warning("quality alert: %s", str(e)[:150])


def _rate_alert():
    w = week_counts()
    if w["rejected"] >= 5 and w["rejected"] > 0.5 * max(w["checked"], 1):
        _alert(f"Quality check: {w['rejected']} of {w['checked']} clips checked this week were rejected (not uploaded). "
               "If good clips are being stopped, open Clips → Rejected by quality check and press Use anyway, or lower "
               "the threshold.", "rate")


def sweep(now=None) -> dict:
    """Timed job (per account): a few queued clips a round (at most 30 a day) are checked; clips that fail are rejected.
    Safety: when a channel would lose more than 30% of its waiting clips, nothing of it is rejected and the owner is
    told instead. A clip whose video is not on the server (saved on the PC) is skipped quietly. Never a clip that is
    on YouTube."""
    out = {"checked": 0, "rejected": 0, "held_back": 0, "skipped": 0}
    if not enabled():
        return out
    now = now or utcnow()
    day = now.strftime("%Y-%m-%d")
    try:
        used = json.loads(get_setting("quality_sweep") or "{}")
    except (TypeError, ValueError):
        used = {}
    n_today = int(used.get("n", 0)) if used.get("day") == day else 0
    budget = max(0, min(SWEEP_PER_RUN, SWEEP_PER_DAY - n_today))
    minimum = threshold()
    with SessionLocal() as s:
        rows = (s.query(Clip).filter(Clip.status == "queued", Clip.youtube_id.is_(None))
                .order_by(Clip.publish_at.is_(None), Clip.publish_at, Clip.id).all())
        for c in rows:
            if budget <= 0:
                break
            if fresh(c) or (c.quality or {}).get("override"):
                continue
            sig = file_sig(c.output_path)
            if sig is None:  # on the PC (archive / PC folder): checked right before its upload instead
                if (c.quality or {}).get("missing") != (c.output_path or ""):
                    c.quality = {**(c.quality or {}), "missing": c.output_path or ""}
                    s.commit()
                out["skipped"] += 1
                continue
            check(s, c)
            budget -= 1
            n_today += 1
            out["checked"] += 1
        set_setting("quality_sweep", json.dumps({"day": day, "n": n_today}))
        # reject per channel, unless it would take too much of that channel's line at once
        recent = (now - timedelta(days=7)).isoformat()
        by_dest: dict = {}
        for c in s.query(Clip).filter(Clip.status == "queued", Clip.youtube_id.is_(None)).all():
            by_dest.setdefault(c.destination_id, []).append(c)
        for did, clips in by_dest.items():
            bad = [c for c in clips if fresh(c) and failing(c, minimum)]
            if not bad:
                continue
            gone = [c for c in s.query(Clip).filter(Clip.status == "draft", Clip.error.like(REJECTED),
                                                    Clip.destination_id == did).all()
                    if (c.quality or {}).get("by") == "sweep" and (c.quality or {}).get("rejected_at", "") >= recent]
            share = (len(bad) + len(gone)) / max(len(clips) + len(gone), 1)
            if share > SAFE_SHARE:
                out["held_back"] += len(bad)
                from .db import Destination

                d = s.get(Destination, did) if did else None
                _alert(f"Quality check: {len(bad)} of {len(clips)} clips waiting for {d.name if d else 'your channel'} "
                       f"score below {minimum}. That is too many to remove at once, so none were removed by the daily "
                       "check — please look at them (Clips → Queued, the score is on each clip). Each one is still "
                       "checked again right before its upload.", f"safe:{did}")
                continue
            for c in bad:
                if reject(s, c, "sweep"):
                    out["rejected"] += 1
    return out


# ---------------------------------------------------------------- owner actions
def _slot(s, c: Clip, p: Project | None):
    from .autopilot import SLOT_LOCK, _next_slot

    ch = s.get(Channel, p.channel_ref) if p is not None and p.channel_ref else None
    with SLOT_LOCK:
        return _next_slot(s, ch.gap_hours if ch else 12, c.destination_id, ch.post_times if ch else None,
                          (ch.tz_offset or 0) if ch else 0)


def _put_back(s, c: Clip, p: Project | None):
    """A rejected clip goes back to where it was: queued (its old time if still ahead, else the next free slot) or
    ready for review. Without its video on the server it goes back to Drafts (Render makes it again)."""
    was = (c.quality or {}).get("was") or {}
    has_file = file_sig(c.output_path) is not None
    c.error = None
    if not has_file and not c.pc_item_id:
        c.status = "draft"
        return
    if was.get("status") == "queued" or c.pc_item_id:
        from datetime import datetime

        old = None
        try:
            old = datetime.fromisoformat(was["publish_at"]) if was.get("publish_at") else None
        except ValueError:
            old = None
        if c.pc_item_id:  # a video from the PC folder keeps its day; a day that passed is moved by pcfolder.due
            c.publish_at = old or _slot(s, c, p)
        else:
            c.publish_at = old if old and old > utcnow() + timedelta(hours=2) else _slot(s, c, p)
        c.status, c.autoqueue = "queued", not c.pc_item_id
    else:
        c.status = "rendered"


def use_anyway(cid: int, actor: str = "user") -> dict:
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        if c is None:
            raise HTTPException(404, "Not found")
        if c.status in ("uploading", "uploaded") or c.youtube_id:
            raise HTTPException(409, "This clip is already on YouTube.")
        p = s.get(Project, c.project_id) if c.project_id else None
        q = dict(c.quality or {})
        q["override"] = {"at": utcnow().isoformat() + "Z", "by": actor, "score": q.get("score")}
        c.quality = q
        if c.status == "draft" and (c.error or "").startswith(NOTE):
            _put_back(s, c, p)
        s.commit()
        _count("used_anyway")
        from . import acl, tenant

        tid = tenant.current()
        acl.record(acl._account_of(tid), actor, PERM,
                   f"Used a clip anyway after the quality check (score {q.get('score')}): {(c.title or '')[:100]} (#{c.id})", tid)
        return {"id": c.id, "status": c.status, "publish_at": c.publish_at.isoformat() + "Z" if c.publish_at else None,
                "quality": c.quality}


def check_again(cid: int) -> dict:
    with SessionLocal() as s:
        c = s.get(Clip, cid)
        if c is None:
            raise HTTPException(404, "Not found")
        if c.status in ("uploading", "uploaded") or c.youtube_id:
            raise HTTPException(409, "This clip is already on YouTube.")
        p = s.get(Project, c.project_id) if c.project_id else None
        q = check(s, c, p, force=True)
        if q.get("score") is None:
            raise HTTPException(409, "The clip could not be checked: " + (q.get("error") or "unknown reason"))
        rejected = c.status == "draft" and (c.error or "").startswith(NOTE)
        if rejected and not failing(c):
            _put_back(s, c, p)
            s.commit()
        elif rejected:  # still too weak: the note shows the new score
            c.error = note_for(c.quality)
            s.commit()
        elif failing(c) and c.status in ("queued", "rendered"):
            reject(s, c, "check")
        return {"id": c.id, "status": c.status, "quality": c.quality, "threshold": threshold()}


def recheck_all() -> dict:
    """After the threshold changed: rejected clips whose stored score now passes go back (no new measuring)."""
    back = 0
    with SessionLocal() as s:
        for c in s.query(Clip).filter(Clip.status == "draft", Clip.error.like(REJECTED)).all():
            if not failing(c) and (c.quality or {}).get("score") is not None:
                p = s.get(Project, c.project_id) if c.project_id else None
                _put_back(s, c, p)
                back += 1
        s.commit()
    return {"back": back}


def summary() -> dict:
    with SessionLocal() as s:
        rejected = s.query(Clip).filter(Clip.status == "draft", Clip.error.like(REJECTED), Clip.removed_at.is_(None)).count()
    return {"on": enabled(), "allowed": allowed(), "threshold": threshold(), "default": DEFAULT_MIN,
            "week": week_counts(), "rejected_now": rejected, "per_day": SWEEP_PER_DAY}


# ---------------------------------------------------------------- addresses (app/acl.py ROUTES has each one)
router = APIRouter()


def _actor(request: Request) -> str:
    who = getattr(request.state, "who", None) or {}
    return "user" if who.get("role") == "user" else "super"


@router.get("/api/quality")
def api_summary():
    return summary()


@router.get("/api/quality/rejected")
def api_rejected():
    from .main import _cards

    with SessionLocal() as s:
        rows = (s.query(Clip).filter(Clip.status == "draft", Clip.error.like(REJECTED), Clip.removed_at.is_(None))
                .order_by(Clip.id.desc()).limit(200).all())
        return _cards(s, rows)


@router.post("/api/clips/{cid}/quality/use")
def api_use(cid: int, request: Request):
    return use_anyway(cid, _actor(request))


@router.post("/api/clips/{cid}/quality/check")
def api_check(cid: int):
    if not allowed():
        from . import acl

        raise HTTPException(403, acl.message(PERM))
    return check_again(cid)


@router.post("/api/quality/recheck")
def api_recheck():
    return recheck_all()


class QualityIn(BaseModel):
    on: bool = True
    threshold: int = Field(DEFAULT_MIN, ge=0, le=100)


@router.post("/api/admin/quality")
def api_admin(body: QualityIn):
    """Super Admin: the quality check on/off for the copy being viewed, and its threshold (0-100)."""
    set_setting("quality_on", "1" if body.on else "0")
    set_setting("quality_min", str(body.threshold))
    return summary()

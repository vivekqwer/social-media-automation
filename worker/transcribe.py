"""Offline captions on the helper PC (faster-whisper, CPU). Used when a video has no subtitles of its own."""
import re
import subprocess

import numpy as np

_model = None
MODEL = "small"  # good Hindi in Devanagari on a normal CPU (~1.5x the clip length)
HINDI_HINT = re.compile(r"[ऀ-ॿ]|\b(hai|hain|kya|nahi|mein|aur|ka|ki|ke|bhai|yaar)\b", re.I)


def _audio(path, ffmpeg: str) -> np.ndarray:
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def cues_for(path, ffmpeg: str, lead: float, length: float, hint_text: str = "") -> tuple[list[dict], str]:
    """Captions for [lead, lead+length] of the file, with times relative to the clip start."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        _model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    audio = _audio(path, ffmpeg)
    lang = "hi" if HINDI_HINT.search(hint_text or "") else None  # Whisper mistakes Hindi speech for English without a hint
    segs, info = _model.transcribe(audio, language=lang, vad_filter=True, word_timestamps=True)
    words = [w for s in segs for w in (s.words or [])]
    cues, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= 6 or w.word.strip().endswith((".", "?", "!", "।")):
            cues.append(cur)
            cur = []
    if cur:
        cues.append(cur)
    out = []
    for group in cues:
        s, e = group[0].start - lead, group[-1].end - lead
        if e <= 0 or s >= length:
            continue
        text = "".join(w.word for w in group).strip()
        if text:
            out.append({"s": round(max(0.0, s), 2), "e": round(min(length, max(e, s + 0.4)), 2), "t": text})
    return out, (lang or info.language or "")

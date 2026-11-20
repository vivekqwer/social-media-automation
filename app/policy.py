"""Source-mode gates. Checked before render, before scheduling and before upload."""
from pathlib import Path

from .ingest import is_cc

MAX_COMMENTARY_SECONDS = 90


def check_edit(project, clip) -> list[str]:
    errs = []
    dur = (clip.end or 0) - (clip.start or 0)
    if dur < 2:
        errs.append("Clip must be at least 2 seconds.")
    if project.mode == "cc" and not is_cc(project.license):
        errs.append("CC mode: source video is not licensed Creative Commons (reuse allowed).")
    if project.mode == "permission" and not (project.permission_note or "").strip():
        errs.append("Permission mode: add proof/note of the creator's permission.")
    if project.mode == "commentary":
        has_voice = bool(clip.voice_path) and Path(clip.voice_path).exists()
        if not clip.overlays and not has_voice:
            errs.append("Commentary mode: add a text overlay or voice-over (transformative edit required).")
        if dur > MAX_COMMENTARY_SECONDS:
            errs.append(f"Commentary mode: keep clips <= {MAX_COMMENTARY_SECONDS}s.")
    return errs

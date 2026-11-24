"""Remake a clip's automatic thumbnail (used by the auto-swap)."""
from pathlib import Path

from .db import Project


def remake_thumbnail(s, c) -> bool:
    variant = int(c.thumb_variant or 0)
    kept = Path(c.output_path).parent / f"thumb_frame_v{variant}.png" if c.output_path else None
    if not c.output_path or not (Path(c.output_path).exists() or (kept and kept.exists())):
        return False  # the video is on the PC only and no frame was kept for this
    from .thumbs import make_thumbnail

    c.thumb_path = str(make_thumbnail(c, s.get(Project, c.project_id)))
    return True

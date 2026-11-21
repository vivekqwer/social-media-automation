"""Remake a clip's automatic thumbnail (used by the auto-swap)."""
from pathlib import Path

from .db import Project


def remake_thumbnail(s, c) -> bool:
    if not c.output_path or not Path(c.output_path).exists():
        return False
    from .thumbs import make_thumbnail

    c.thumb_path = str(make_thumbnail(c, s.get(Project, c.project_id)))
    return True

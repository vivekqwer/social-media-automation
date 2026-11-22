"""Title + small face on every frame of a Short.

YouTube does not show a custom thumbnail in the Shorts feed or on the channel's Shorts tab: it shows a frame of
the video that it picks itself, and the API cannot choose it. So the things that make a thumbnail work — the short
title and the owner's face — are put on the video itself, as a slim band at the top for the whole clip. Whatever
frame YouTube picks then carries them.

The band (soft dark fade + round face badge) is a transparent PNG laid over the picture; the words are drawn by
the same subtitle renderer as the captions, so Hindi is shaped correctly.
"""
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

FACE_SIZES = {"S": 0.55, "M": 0.78, "L": 1.0}  # thumbnail face size relative to the largest ("L" = the old size)


def face_scale() -> float:
    from .db import get_setting

    return FACE_SIZES.get(get_setting("thumb_face_size") or "S", FACE_SIZES["S"])


def headline(clip, project) -> str:
    """The few words shown on the thumbnail and on the band (the thumbnail text if the owner typed one)."""
    from .thumbs import default_text

    return (getattr(clip, "thumb_text", None) or default_text(getattr(project, "title", None) or getattr(clip, "title", None) or "")).strip()


def make_banner(clip, project, W: int, H: int, workdir: Path) -> dict | None:
    """Write banner.png into workdir and say where the band and its words go. None = nothing to show."""
    from .faces import face_for
    from .render import DEVA

    text = headline(clip, project)
    fp = face_for(clip.destination_id) if getattr(clip, "thumb_face", None) is not False else None
    if not text and not fp:
        return None
    top, band_h = int(H * (0.085 if H > W else 0.04)), int(H * (0.19 if H > W else 0.22))
    band = Image.new("RGBA", (W, band_h + 2 * int(band_h * 0.25)), (0, 0, 0, 0))
    # soft dark fade so white words stay readable on any picture
    grad = Image.new("L", (1, band.height))
    for y in range(band.height):
        edge = min(y, band.height - 1 - y) / (band.height * 0.25)
        grad.putpixel((0, y), int(150 * min(1.0, edge)))
    band.putalpha(grad.resize(band.size))
    pad = (band.height - band_h) // 2
    x = int(W * 0.045)
    if fp:
        d = int(min(band_h * 0.55, W * 0.19))  # a small badge: the words are the main thing
        face = Image.open(fp).convert("RGBA")
        side = min(face.width, face.height)
        face = face.crop(((face.width - side) // 2, 0, (face.width - side) // 2 + side, side)).resize((d, d), Image.LANCZOS)  # head and shoulders
        ring = 5
        badge = Image.new("RGBA", (d + 2 * ring, d + 2 * ring), (0, 0, 0, 0))
        dr = ImageDraw.Draw(badge)
        dr.ellipse((0, 0, badge.width - 1, badge.height - 1), fill=(255, 255, 255, 255))
        dr.ellipse((ring, ring, ring + d - 1, ring + d - 1), fill=(38, 38, 46, 255))
        mask = Image.new("L", (d, d), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, d - 1, d - 1), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(0.6))
        badge.paste(face, (ring, ring), Image.composite(face.getchannel("A"), Image.new("L", (d, d), 0), mask))
        band.alpha_composite(badge, (x, pad + (band_h - badge.height) // 2))
        x += badge.width + int(W * 0.03)
    band.save(workdir / "banner.png")
    hindi = bool(DEVA.search(text))
    width = int(W * 0.955) - x
    fs = int(H * (0.046 if hindi else 0.064)) if H > W else int(H * 0.085)  # big, bold display letters like a thumbnail
    wide = 0.62 if hindi else 0.40  # average letter width of the font, as a share of its size
    lines = textwrap.wrap(text, max(6, int(width / (fs * wide))))
    while len(lines) > 2 and fs > int(H * 0.03):  # too long for two lines: slightly smaller letters, never cut words
        fs = int(fs * 0.9)
        lines = textwrap.wrap(text, max(6, int(width / (fs * wide))))
    lines = lines[:2]
    return {"path": "banner.png", "y": top - pad, "lines": lines, "x": x, "cy": top + band_h // 2, "fs": fs,
            "font": "Noto Sans Devanagari" if hindi else "Anton"}

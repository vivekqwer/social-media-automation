from .db import Clip, Project, SessionLocal
from .keywords import suggest

LICENSE_LINE = {
    "cc": "License: Creative Commons Attribution (CC BY). Changes made: clipped and edited.",
    "permission": "Used with the creator's permission.",
    "commentary": "Edited with original commentary/transformation.",
}


def _clean(s: str) -> str:
    return (s or "").replace("<", "").replace(">", "")


def build(project: Project, clip: Clip, keywords: list[str]) -> dict:
    base = _clean(project.title)
    title = f"{base} | Best Moment"
    if len(title) > 100:
        title = base[:100]
    hashtags = " ".join("#" + k.replace(" ", "") for k in keywords[:3])
    src = f"https://youtu.be/{project.video_id}?t={int(clip.start)}"
    parts = [
        hashtags,
        "",
        f"Original video: {base}",
        f"Credit: {_clean(project.channel)} - {project.channel_url}",
        f"Source: {src}",
        LICENSE_LINE.get(project.mode, ""),
        "",
        "--- Original description ---",
        _clean(project.description)[:1500],
    ]
    description = "\n".join(parts).strip()[:4900]

    tags, total = [], 0
    for t in list(project.tags or []) + keywords:
        t = _clean(t).strip()
        if not t or t.lower() in (x.lower() for x in tags):
            continue
        if total + len(t) + 3 > 480:
            break
        tags.append(t)
        total += len(t) + 3
    return {"title": title, "description": description, "tags": tags}


def auto_metadata(clip_id: int, extra_keywords: list[str] | None = None) -> dict:
    with SessionLocal() as s:
        clip = s.get(Clip, clip_id)
        project = s.get(Project, clip.project_id)
        seed = " ".join(project.title.split()[:6])
        kws = list(extra_keywords or []) + suggest(seed, 20)
        meta = build(project, clip, kws)
        clip.title, clip.description, clip.tags = meta["title"], meta["description"], meta["tags"]
        s.commit()
        return meta

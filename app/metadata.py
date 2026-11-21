import re

from .db import Clip, Project, SessionLocal
from .keywords import suggest

LICENSE_LINE = {
    "cc": "License: Creative Commons Attribution (CC BY). Changes made: clipped and edited.",
    "permission": "Used with the creator's permission.",
    "commentary": "Edited with original commentary/transformation.",
}


def affiliate_lines(text: str) -> list[str]:
    """Owner's affiliate rules (keywords -> line) that match this video's words."""
    import json as _json

    from .db import get_setting

    try:
        rules = _json.loads(get_setting("affiliate_rules") or "[]")
    except ValueError:
        return []
    words = set(re.findall(r"\w+", (text or "").lower()))
    out = []
    for r in rules:
        keys = [k.strip().lower() for k in (r.get("keywords") or "").split(",") if k.strip()]
        if r.get("text") and any((set(k.split()) <= words) for k in keys):
            out.append(r["text"].strip())
    return out[:5]


def _with_affiliates(meta: dict, text: str) -> dict:
    lines = affiliate_lines(text)
    if lines:
        meta["description"] = _cut(meta["description"] + "\n\n" + "\n".join(lines)
                                   + "\n(Some links are affiliate links — I may earn a small commission.)", 4900)
    return meta


def _cut(s: str, n: int) -> str:
    return s.encode()[:n].decode(errors="ignore")  # YouTube limits description by bytes


def _clean(s: str) -> str:
    return (s or "").replace("<", "").replace(">", "")


def _build_original(project: Project, clip: Clip, keywords: list[str]) -> dict:
    """My own reel: no credit block, the script itself is the description."""
    title = _clean(project.title)[:100]
    shorts = clip.aspect == "9:16"
    hashtags = " ".join((["#shorts"] if shorts else []) + ["#" + re.sub(r"\W", "", k) for k in keywords[:3] if k])
    description = _cut((hashtags + "\n\n" + _clean(project.description)).strip(), 4900)
    tags, total = [], 0
    for t in keywords + (["shorts"] if shorts else []):
        t = _clean(t).strip()
        if t and t.lower() not in (x.lower() for x in tags) and total + len(t) + 3 <= 480:
            tags.append(t)
            total += len(t) + 3
    return {"title": title, "description": description, "tags": tags}


def build(project: Project, clip: Clip, keywords: list[str]) -> dict:
    words = " ".join([project.title or "", " ".join(project.tags or []), " ".join(keywords)])
    if project.mode == "original":
        return _with_affiliates(_build_original(project, clip, keywords), words)
    if project.mode == "own":  # my own long video -> Short: link back to the full video, no credit block
        meta = _build_original(project, clip, keywords)
        meta["title"] = (_clean(project.title) + " | Best Moment")[:100]
        meta["description"] = _cut(meta["description"].split("\n\n")[0] + f"\n\nFull video: https://youtu.be/{project.video_id}"
                                    + "\n\n" + _clean(project.description)[:1500], 4900)
        return _with_affiliates(meta, words)
    return _with_affiliates(_build(project, clip, keywords), words)


def _build(project: Project, clip: Clip, keywords: list[str]) -> dict:
    base = _clean(project.title)
    title = f"{base} | Best Moment"
    if len(title) > 100:
        title = base[:100]
    shorts = clip.aspect == "9:16"
    tag_src = [t for t in (list(project.tags or [])[:3] + keywords) if t][:3]
    hashtags = " ".join((["#shorts"] if shorts else []) + ["#" + re.sub(r"\W", "", t) for t in tag_src])
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
    description = _cut("\n".join(parts).strip(), 4900)

    tags, total = [], 0
    extra = [project.channel] + (["shorts"] if shorts else [])
    for t in list(project.tags or []) + keywords + extra:
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
        # what people actually type: title words, the original's top tags and the channel name
        seeds = [" ".join(project.title.split()[:5])] + list(project.tags or [])[:2] + \
            ([] if project.mode == "original" else [project.channel])
        found = [suggest(seeds[0], 15)] + [suggest(sd, 6, expand=2) for sd in seeds[1:] if sd]
        kws, seen = [], set()
        for k in list(extra_keywords or []) + [k for group in zip(*[g + [None] * (15 - len(g)) for g in found]) for k in group if k]:
            if k.lower() not in seen:
                seen.add(k.lower())
                kws.append(k)
        # keep only suggestions about this video's topic (drops e.g. other games from the channel-name search)
        vocab = {w for w in re.findall(r"\w+", (project.title + " " + " ".join(project.tags or [])).lower()) if len(w) > 2}
        kws = [k for k in kws if k in (extra_keywords or []) or set(re.findall(r"\w+", k.lower())) & vocab]
        meta = build(project, clip, kws)
        clip.title, clip.description, clip.tags = meta["title"], meta["description"], meta["tags"]
        s.commit()
        return meta

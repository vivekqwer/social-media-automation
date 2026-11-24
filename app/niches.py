"""Ready-made niches ("What is your channel about?"): search phrases for Creative Commons topic sources.

Data only (plus two small pure helpers). The sources themselves are made by app/niche_sources.py.

Every phrase becomes one topic source: YouTube search, Creative Commons videos only, Indian / Hindi results first
(app/youtube.py api_search_cc). Phrases mix Hindi and English the way Indian viewers search. The same phrase in many
accounts is searched once a day and shared (app/search_cache.py), so ready-made phrases cost the least.

trending: a YouTube category id from main.TREND_CATS for one "Trending (CC clips)" source, or None where no
category fits. low_cc: few Creative Commons videos exist for it -> the page says so plainly.
"""
import re

NICHES = [
    {"key": "food", "label": "Food and street food", "trending": "26", "phrases": [
        "indian street food", "street food india vlog", "desi khana recipe", "स्ट्रीट फूड", "famous food of delhi"]},
    {"key": "tech", "label": "Tech and gadgets", "trending": "28", "phrases": [
        "tech gadgets review hindi", "smartphone tips hindi", "new gadgets unboxing", "best budget gadgets india",
        "टेक्नोलॉजी जानकारी"]},
    {"key": "devops", "label": "DevOps, cloud and programming", "trending": "28", "phrases": [
        "devops tutorial", "cloud computing explained", "kubernetes tutorial hindi", "programming tutorial hindi",
        "aws tutorial for beginners"]},
    {"key": "comedy", "label": "Comedy", "trending": "23", "low_cc": True, "phrases": [
        "funny video hindi", "comedy sketch india", "standup comedy hindi", "मज़ेदार वीडियो"]},
    {"key": "motivation", "label": "Motivation", "trending": "22", "phrases": [
        "motivational speech hindi", "success motivation", "मोटिवेशनल वीडियो", "life lessons hindi", "study motivation"]},
    {"key": "travel", "label": "Travel", "trending": None, "phrases": [
        "india travel vlog", "himachal travel vlog", "travel vlog hindi", "goa trip vlog", "भारत यात्रा"]},
    {"key": "fitness", "label": "Fitness", "trending": None, "phrases": [
        "home workout hindi", "yoga for beginners", "fitness tips hindi", "gym workout", "weight loss exercise"]},
    {"key": "cars", "label": "Cars and bikes", "trending": None, "phrases": [
        "car review hindi", "bike review hindi", "motorcycle ride india", "car vlog india", "बाइक राइड"]},
    {"key": "gaming", "label": "Gaming", "trending": "20", "phrases": [
        "gameplay hindi", "mobile gaming india", "gaming highlights", "minecraft hindi", "pc gaming"]},
    {"key": "education", "label": "Education and facts", "trending": None, "phrases": [
        "amazing facts hindi", "science facts", "general knowledge hindi", "रोचक तथ्य", "how it works explained"]},
    {"key": "finance", "label": "Finance basics", "trending": None, "phrases": [
        "personal finance hindi", "share market basics hindi", "money saving tips", "investment for beginners india",
        "पैसे कैसे बचाएं"]},
    {"key": "beauty", "label": "Beauty and fashion", "trending": "26", "phrases": [
        "makeup tutorial hindi", "skincare routine", "fashion tips india", "hairstyle tutorial", "saree draping"]},
]
BY_KEY = {n["key"]: n for n in NICHES}

LOW_CC_NOTE = ("Very few videos about this are Creative Commons (free to reuse), so expect few or no clips. "
               "Videos are only ever taken from Creative Commons videos or creators who gave you permission.")
# subjects that are almost never Creative Commons (music labels, films, TV, sport broadcasts, news)
LOW_CC_WORDS = re.compile(r"\b(?:music|songs?|gaana|gane|lyrics?|bhajan|films?|movies?|bollywood|tollywood|cinema|trailers?|"
                          r"web ?series|serial|tv shows?|anime|cartoons?|cricket|ipl|football|wwe|news)\b"
                          r"|गाना|गाने|फिल्म|मूवी|न्यूज़", re.I)  # (Hindi vowel signs are not word characters: no \b there)
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
MAX_TOPIC = 60


def clean_topic(text: str) -> str:
    """A typed topic as one short search phrase ('' = nothing usable). Links are not topics."""
    t = " ".join(str(text or "").replace("#", " ").split())[:MAX_TOPIC].strip(" ,.;:-")
    if not t or t.lower().startswith(("http", "www.")) or "/" in t:
        return ""
    return t


def phrases_for_topic(text: str) -> list[str]:
    """3-4 search phrases for a topic the user typed: the topic, "… shorts", "… best moments" and a Hindi variant."""
    base = clean_topic(text)
    if not base:
        return []
    low = base.lower()
    out = [base, f"{base} shorts", f"{base} best moments"]
    if not DEVANAGARI.search(base) and "hindi" not in low:
        out.append(f"{base} hindi")
    seen, res = set(), []
    for p in out:
        if p.lower() not in seen:
            seen.add(p.lower())
            res.append(p)
    return res


def low_cc(text: str) -> bool:
    return bool(LOW_CC_WORDS.search(str(text or "")))


def custom_key(text: str) -> str:
    """The niche key a typed topic is kept under (so it can be removed again as one)."""
    slug = re.sub(r"\s+", "-", clean_topic(text).lower())
    return ("custom:" + slug)[:40]

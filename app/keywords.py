"""Keyword research from real search demand (YouTube autocomplete). Not a virality guarantee."""
import httpx

URL = "https://suggestqueries.google.com/complete/search"


def suggest(seed: str, limit: int = 25) -> list[str]:
    seed = (seed or "").strip()
    if not seed:
        return []
    queries = [seed] + [f"{seed} {c}" for c in "abcdefgh"]
    counts: dict[str, int] = {}
    order: list[str] = []
    for q in queries:
        try:
            r = httpx.get(URL, params={"client": "firefox", "ds": "yt", "q": q}, timeout=8)
            items = r.json()[1]
        except Exception:
            continue
        for rank, it in enumerate(items):
            key = it.lower().strip()
            if key not in counts:
                order.append(key)
                counts[key] = 0
            counts[key] += 10 - rank  # earlier suggestion = stronger demand signal
    order.sort(key=lambda k: counts[k], reverse=True)
    return order[:limit]

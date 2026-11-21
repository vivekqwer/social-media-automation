"""Telegram remote control: alerts on the phone and commands (/status, /review, /pause, /resume, /stats).

Standalone module: everything here is wrapped so a Telegram problem never affects uploads or rendering.
Setup from the dashboard: paste the bot token from @BotFather, then send the shown /start code to the bot.
"""
import json
import logging
import secrets
import threading

import httpx

from .crypto import decrypt, encrypt
from .db import get_setting, set_setting

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
_lock = threading.Lock()


def _token() -> str | None:
    raw = get_setting("tg_token")
    try:
        return decrypt(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def chat_id() -> str | None:
    return get_setting("tg_chat")


def enabled() -> bool:
    return bool(_token() and chat_id() and get_setting("tg_on") != "0")


def _call(method: str, **params):
    tok = _token()
    if not tok:
        raise RuntimeError("Telegram is not set up")
    r = httpx.post(API.format(token=tok, method=method), json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"Telegram error {r.status_code}")
    return data.get("result")


def setup(token: str) -> dict:
    """Check the token with Telegram, store it encrypted and create a one-time link code."""
    token = token.strip()
    r = httpx.get(API.format(token=token, method="getMe"), timeout=20).json()
    if not r.get("ok"):
        raise ValueError("Telegram did not accept this token. Copy it again from @BotFather.")
    set_setting("tg_token", encrypt(token))
    set_setting("tg_chat", "")
    set_setting("tg_offset", "0")
    code = secrets.token_hex(3).upper()
    set_setting("tg_link_code", code)
    set_setting("tg_on", "1")
    set_setting("tg_bot", r["result"].get("username") or "")
    return {"bot": r["result"].get("username"), "code": code}


def status() -> dict:
    return {"configured": bool(_token()), "linked": bool(chat_id()), "on": get_setting("tg_on") != "0",
            "code": None if chat_id() else get_setting("tg_link_code"), "bot": get_setting("tg_bot")}


def disconnect():
    for k in ("tg_token", "tg_chat", "tg_link_code", "tg_bot"):
        set_setting(k, "")


def notify(text: str, buttons: list | None = None):
    """Send a message to the owner. Never raises."""
    if not enabled():
        return
    try:
        kw = {"chat_id": chat_id(), "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            kw["reply_markup"] = {"inline_keyboard": buttons}
        _call("sendMessage", **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram: %s", str(e)[:200])


def notify_once(key: str, text: str, every_minutes: int = 60):
    """Same message at most once per period (e.g. 'helper offline')."""
    from datetime import datetime, timedelta, timezone

    last = get_setting("tg_once:" + key)
    now = datetime.now(timezone.utc)
    if last and datetime.fromisoformat(last) > now - timedelta(minutes=every_minutes):
        return
    set_setting("tg_once:" + key, now.isoformat())
    notify(text)


def send_document(path: str, caption: str = ""):
    if not enabled():
        return
    try:
        tok = _token()
        with open(path, "rb") as f:
            httpx.post(API.format(token=tok, method="sendDocument"), data={"chat_id": chat_id(), "caption": caption[:1000]},
                       files={"document": (path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1], f)}, timeout=120)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram document: %s", str(e)[:200])


def esc(t) -> str:
    return str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------- commands ----------------
def _cmd_status() -> str:
    from .main import _helper_state
    from . import quota
    from .db import Clip, Destination, SessionLocal

    with SessionLocal() as s:
        cnt = {k: s.query(Clip).filter(Clip.status == k).count() for k in ("rendered", "queued", "fetching", "failed", "uploaded")}
        dests = s.query(Destination).all()
        hp = _helper_state(s)
    q = quota.summary()
    lines = ["<b>Raji Studio</b>",
             f"Ready to review: {cnt['rendered']} · Queued: {cnt['queued']} · Waiting for helper: {cnt['fetching']}",
             f"Failed: {cnt['failed']} · Uploaded: {cnt['uploaded']}",
             f"Uploads left today: {q['uploads_left']}",
             "Helper PC: " + ("online" if hp["online"] else "OFFLINE")]
    for d in dests:
        lines.append(f"{esc(d.name)}: " + ("uploads PAUSED — " + esc(d.paused_reason or "") if d.paused else "uploads on"))
    return "\n".join(lines)


def _cmd_stats() -> str:
    from .stats import overview

    o = overview()
    out = ["<b>Channel numbers</b>"]
    for c in o["channels"]:
        w = c.get("watch") or {}
        out.append(f"<b>{esc(c['name'])}</b>: {c['subscribers'] if c['subscribers'] is not None else '—'} subscribers "
                   f"({_sign(c['subs_24h'])} today), {c['views'] if c['views'] is not None else '—'} views ({_sign(c['views_24h'])} today), "
                   f"watch time 28 days: {round((w.get('minutes_28d') or 0) / 60, 1)} h")
    top = sorted(o["videos"], key=lambda v: -(v["new_views"] or 0))[:3]
    if top:
        out.append("Most new views: " + "; ".join(f"{esc(v['title'][:40])} (+{v['new_views'] or 0})" for v in top))
    return "\n".join(out)


def _sign(n):
    return "—" if n is None else (f"+{n}" if n > 0 else str(n))


def _review_list():
    from .db import Clip, SessionLocal

    with SessionLocal() as s:
        rows = s.query(Clip).filter(Clip.status == "rendered").order_by(Clip.id).limit(5).all()
        return [(c.id, c.title or f"Clip #{c.id}") for c in rows]


def _set_pause(paused: bool) -> str:
    from .db import Destination, SessionLocal, utcnow

    with SessionLocal() as s:
        for d in s.query(Destination).all():
            d.paused = paused
            d.paused_at = utcnow() if paused else d.paused_at
            d.paused_reason = "Paused from Telegram" if paused else None
        s.commit()
    return "All uploads are paused." if paused else "Uploads resumed."


def _handle_text(text: str):
    cmd = (text or "").strip().split()[0].lower().split("@")[0] if text else ""
    if cmd in ("/start", "/help"):
        return notify("Commands:\n/status — what is happening\n/review — clips waiting for approval\n/stats — subscribers, views, "
                      "watch time\n/pause — pause all uploads\n/resume — resume uploads")
    if cmd == "/status":
        return notify(_cmd_status())
    if cmd == "/stats":
        return notify(_cmd_stats())
    if cmd == "/pause":
        return notify(_set_pause(True))
    if cmd == "/resume":
        return notify(_set_pause(False))
    if cmd == "/review":
        items = _review_list()
        if not items:
            return notify("Nothing is waiting for review.")
        for cid, title in items:
            notify(f"Ready: <b>{esc(title)}</b>", [[{"text": "✅ Approve", "callback_data": f"ap:{cid}"},
                                                     {"text": "🗑 Discard", "callback_data": f"dc:{cid}"}]])
        return None
    return notify("Unknown command. Send /help")


def _handle_callback(data: str) -> str:
    from fastapi import HTTPException

    from . import main

    kind, _, cid = (data or "").partition(":")
    try:
        if kind == "ap":
            c = main.approve_clip(int(cid), main.ApproveIn())
            return f"Approved — goes live {c.get('publish_at') or 'soon'} (UTC)."
        if kind == "dc":
            main.discard_clip(int(cid))
            return "Discarded."
    except HTTPException as e:
        return f"Could not do that: {e.detail}"
    return "Unknown action"


def poll():
    """Read new messages/button presses (called every 20 seconds by the scheduler)."""
    if not _token() or get_setting("tg_on") == "0":
        return
    if not _lock.acquire(blocking=False):
        return
    try:
        offset = int(get_setting("tg_offset") or 0)
        updates = _call("getUpdates", offset=offset, timeout=0, allowed_updates=["message", "callback_query"])
        for u in updates or []:
            offset = max(offset, u["update_id"] + 1)
            set_setting("tg_offset", str(offset))
            if "message" in u:
                m = u["message"]
                cid, text = str(m["chat"]["id"]), m.get("text") or ""
                if not chat_id():  # linking: only the one-time code from the dashboard links a chat
                    code = (get_setting("tg_link_code") or "").upper()
                    if code and code in text.upper():
                        set_setting("tg_chat", cid)
                        set_setting("tg_link_code", "")
                        notify("✅ Linked to Raji Studio. You will get alerts here.\nSend /help to see what you can do.")
                    continue
                if cid != chat_id():
                    continue  # strangers are ignored
                _handle_text(text)
            elif "callback_query" in u:
                q = u["callback_query"]
                if str(q["message"]["chat"]["id"]) != chat_id():
                    continue
                msg = _handle_callback(q.get("data", ""))
                try:
                    _call("answerCallbackQuery", callback_query_id=q["id"], text=msg[:190])
                except Exception:  # noqa: BLE001
                    pass
                notify(msg)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram poll: %s", str(e)[:200])
    finally:
        _lock.release()


def daily_summary():
    if not enabled():
        return
    try:
        notify("🌙 <b>Daily summary</b>\n" + _cmd_status() + "\n\n" + _cmd_stats())
    except Exception as e:  # noqa: BLE001
        log.warning("telegram summary: %s", str(e)[:200])

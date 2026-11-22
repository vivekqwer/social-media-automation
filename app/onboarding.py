"""Locked, step-by-step onboarding for a customer's copy.

A new customer sees only this until it is finished: each step opens when the one before it is done, with exact
instructions, so a person who cannot code can set everything up from the dashboard. What is "done" is checked on
the real system (keys saved, channel connected, helper approved, ...), never just ticked — except the two things
the system cannot see (phone verification) or that are allowed to be skipped (face photo, Gmail).

The owner's own copy (tenant 1) has no onboarding: it is already running.
"""
import json
import re

from . import tenant
from .config import BASE_URL
from .db import Channel, Destination, SessionLocal, Worker, get_setting, set_setting

CLIENT_ID_RE = re.compile(r"^[0-9]{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com$")
REDIRECT = f"{BASE_URL}/auth/callback"

STEPS = [  # key, title, why, can it be skipped
    ("google", "Your Google project", "Gives your system its own YouTube upload allowance (about 6 uploads a day) that nobody else uses.", False),
    ("channel", "Connect your YouTube channel", "Clips are uploaded to this channel.", False),
    ("verify", "Verify the channel's phone number", "YouTube only accepts custom thumbnails on phone-verified channels.", False),
    ("helper", "Your helper PC", "A small program on a Windows PC at home. It downloads the videos (YouTube blocks downloads from servers).", False),
    ("face", "Your face for thumbnails", "Put on every thumbnail, next to a frame of the video.", True),
    ("gmail", "Gmail for alerts and permission mails", "Alerts come to your inbox; permission mails to creators are sent from it when you press Send.", True),
    ("source", "Your first source", "Where clips come from. It runs by itself after this.", False),
]

HOW = {
    "google": [
        "Open console.cloud.google.com and sign in with the Google account that owns your YouTube channel.",
        "At the top press the project menu → “New project”. Name it (for example your system's name) → Create. Make sure it is selected.",
        "Menu ☰ → “APIs & Services” → “Library”. Search and press Enable for each of these three: “YouTube Data API v3”, “YouTube Analytics API”, “Gmail API”.",
        "Menu ☰ → “APIs & Services” → “OAuth consent screen” → Get started. App name: your system's name. Support email: yours. Audience: External. Contact email: yours. Tick “I agree to the Google API Services: User Data Policy”. Finish / Create.",
        "Still in the consent screen: open “Audience” and press “Publish app” → Confirm. (If you skip this, Google logs your system out every 7 days.)",
        "Open “Clients” → “Create client”. Application type: “Web application”. Under “Authorized redirect URIs” press Add URI and paste the address shown below (use the Copy button). Create.",
        "Google shows a “Client ID” and a “Client secret” (the secret is shown in full only now — copy it straight away). Paste each one into the two boxes below and press Save. They are checked with Google. Keep them private — they are like a password.",
        "Good to know: Google treats a brand-new project as “unverified”. If your uploaded videos stay locked as Private on YouTube, Google wants an “API compliance audit” for your project (a free form: search “YouTube API Services - Audit and Quota Extension Form”). The dashboard tells you if this happens (Dashboard → Problems).",
    ],
    "channel": [
        "Press “Connect YouTube channel”.",
        "Choose the Google account of your channel. If it has several channels, pick the one you want to upload to.",
        "Google says “Google hasn't verified this app” — that is normal for your own project: press Advanced → “Go to …”.",
        "Tick every permission box and press Continue. You come back here and this step turns green.",
        "If Google says “Access blocked”: in Google Cloud check that you pressed “Publish app”. If it says “redirect_uri_mismatch”: the redirect address in your Google client is not exactly the one shown in step 1 — copy it again.",
        "Wrong keys saved? Open My channels → “Google project keys” and save the right ones.",
    ],
    "verify": [
        "Signed in to the Google account of your channel, open youtube.com/verify.",
        "Enter a phone number, then type the code YouTube sends by SMS or call.",
        "Come back and press “I have verified it”. (Already verified? Just press it.)",
    ],
    "helper": [
        "On a Windows PC that is usually on (at home), press “Download my helper” below and save the ZIP file.",
        "Open the folder where it was saved, right-click the ZIP → “Extract All…” → Extract. Open the new folder.",
        "Double-click “Start helper”. If Windows shows a blue “Windows protected your PC” box, press “More info” → “Run anyway” (it is your own helper). The first time it installs what it needs; if it says Python was installed, close the window and double-click “Start helper” once more.",
        "The window shows a 6-digit pairing code. Type that code below and press Approve.",
        "To make it start by itself with Windows: open the folder “worker” and double-click “install_autostart”.",
    ],
    "face": [
        "Choose a clear photo of your face (looking at the camera, good light) and upload it. The background is removed automatically.",
        "No photo now? Press Skip — thumbnails are then made without a face. You can add it later in My channels.",
    ],
    "gmail": [
        "Press “Connect Gmail” and choose the Google account the mails should come from.",
        "On “Google hasn't verified this app” press Advanced → “Go to …”, tick both Gmail boxes and press Continue.",
        "Not now? Press Skip — alerts are then only shown in the dashboard and permission mails cannot be sent. You can connect it later in My channels.",
    ],
    "source": [
        "Press “Add my first source”.",
        "Easiest start: Source type “Topic”, write what your channel is about (for example “indian street food”). Only videos whose owners allow reuse (Creative Commons) are used.",
        "Leave “Review first” on: the first clips then wait for your approval in Clips.",
        "Press Add. The system now finds videos, cuts the best moments and prepares them by itself.",
    ],
}


def _ack() -> dict:
    try:
        return json.loads(get_setting("onboarding") or "{}")
    except ValueError:
        return {}


def acknowledge(key: str):
    """Steps the system cannot check itself (verify) or that may be skipped (face, gmail)."""
    if key not in ("verify", "face", "gmail"):
        raise ValueError("This step is checked automatically.")
    a = _ack()
    a[key] = True
    set_setting("onboarding", json.dumps(a))


def google_keys() -> dict:
    """What this copy uses to talk to Google (never the secret itself)."""
    cid = get_setting("google_client_id") or ""
    shared = tenant.current() == tenant.OWNER or get_setting("shared_google") == "1"
    return {"own": bool(cid), "client_id": cid, "shared": shared and not cid, "redirect_uri": REDIRECT}


def save_google_keys(client_id: str, client_secret: str):
    from .crypto import encrypt

    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not CLIENT_ID_RE.match(client_id):
        raise ValueError("That does not look like a Client ID. It ends with “.apps.googleusercontent.com” — copy it again from Google.")
    if len(client_secret) < 20 or " " in client_secret or client_secret == client_id:
        raise ValueError("That does not look like a Client secret. Copy the “Client secret” (not the Client ID) from Google.")
    problem = check_with_google(client_id, client_secret)
    if problem:
        raise ValueError(problem)
    set_setting("google_client_id", client_id)
    set_setting("google_client_secret", encrypt(client_secret))


def check_with_google(client_id: str, client_secret: str) -> str:
    """Ask Google whether this Client ID + secret pair is real ('' = yes). It is done by redeeming a made-up code:
    Google answers "invalid_client" for wrong keys and "invalid_grant" for right keys with a wrong code."""
    import httpx

    try:
        r = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
            "client_id": client_id, "client_secret": client_secret, "code": "check-only", "grant_type": "authorization_code",
            "redirect_uri": REDIRECT})
        err = (r.json() or {}).get("error", "")
    except Exception:  # noqa: BLE001  (Google unreachable: do not block the customer; connecting the channel will tell)
        return ""
    if err == "invalid_client":
        return ("Google does not know this Client ID with this Client secret. Copy both again from Google "
                "(APIs & Services → Clients → your client). The secret is shown in full only when it is created — "
                "if you no longer have it, press “Add secret” there and use the new one.")
    if err == "redirect_uri_mismatch":
        return "The keys are right, but the redirect address is not in your Google client yet. Add the address shown above under “Authorized redirect URIs” and press Save in Google, then try again."
    return ""


def state() -> dict:
    """Every step with done / locked, the current step, and whether the dashboard is still locked."""
    from . import faces, gmail

    if tenant.current() == tenant.OWNER:
        return {"active": False, "steps": [], "current": None, "done": 0, "total": 0}
    ack, keys = _ack(), google_keys()
    with SessionLocal() as s:
        has_channel = s.query(Destination).count() > 0
        helper_ok = s.query(Worker).filter(Worker.approved.is_(True)).count() > 0
        helper_waiting = s.query(Worker).filter(Worker.approved.is_(False)).count()
        has_source = s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "viral")).count() > 0
    if get_setting("onboarding") is None and has_channel and has_source:
        ack = {"finished": True, "verify": True}  # it was set up before this page existed
        set_setting("onboarding", json.dumps(ack))
    done = {"google": keys["own"] or keys["shared"], "channel": has_channel, "verify": bool(ack.get("verify")),
            "helper": helper_ok, "face": faces.DEFAULT_FACE.exists() or bool(ack.get("face")),
            "gmail": bool(gmail.connected_email()) or bool(ack.get("gmail")), "source": has_source}
    steps, locked, current = [], False, None
    for key, title, why, skippable in STEPS:
        ok = bool(done[key]) and not locked  # a later step only counts once everything before it is done
        st = {"key": key, "title": title, "why": why, "how": HOW[key], "done": ok, "locked": locked and not ok, "skippable": skippable,
              "skipped": key in ("face", "gmail") and bool(ack.get(key)) and not (faces.DEFAULT_FACE.exists() if key == "face" else gmail.connected_email())}
        if not ok and current is None:
            current = key
            locked = True  # everything after the first unfinished step stays closed
        steps.append(st)
    finished = current is None
    if finished and not ack.get("finished"):
        ack["finished"] = True
        set_setting("onboarding", json.dumps(ack))
    return {"active": not (finished or ack.get("finished")), "steps": steps, "current": current,
            "done": sum(1 for x in steps if x["done"]), "total": len(steps), "google": keys,
            "helper_waiting": helper_waiting}


def allows(step: str) -> bool:
    """May this copy do what belongs to `step` now? (Server-side lock: the dashboard alone is not trusted.)"""
    if tenant.current() == tenant.OWNER:
        return True
    st = state()
    if not st["active"]:
        return True
    order = [k for k, *_ in STEPS]
    cur = order.index(st["current"]) if st["current"] in order else len(order)
    return order.index(step) <= cur

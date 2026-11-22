"""Setup guide: everything a new admin needs, in plain words, inside the dashboard.

* HOW: step-by-step instructions for every line of the checklist (/api/setup adds them to its items)
* HELP: "if this happens -> what the system does by itself -> what you do" for every problem we know
* record_error / recent_errors: the last server problems, so a non-coder can press "Copy details" and send them on
No coding and no server access is needed for anything in here.
"""
import json
import re
import traceback

from .db import get_setting, set_setting, utcnow

HOW = {
    "youtube": [
        "Open My channels and press “Connect a YouTube channel”.",
        "Choose the Google account that owns the channel. If the account has several channels (brand accounts), Google asks which one — pick the channel you want to upload to.",
        "If Google shows “Google hasn't verified this app”: press Advanced, then “Go to Raji Studio”.",
        "Tick every permission box and press Allow / Continue. You come back to the dashboard and the channel is in the list.",
        "The box “See, edit, and permanently delete your YouTube videos, ratings, comments and captions” is only used to post the first comment under each new video. Channels connected before this was added do not have it yet (My channels says so): connect them once more (same channel) to allow comments.",
        "If Google says “Access blocked”: in the Google Cloud project that your keys come from, open APIs & Services → OAuth consent screen → Audience and press “Publish app” (or add this Google address under Test users). Then try again.",
        "Customers: your own Google project's keys can be changed in My channels → “Google project keys”.",
        "To add a second channel, do the same again with the other channel — the first one stays connected.",
    ],
    "main": [
        "My channels shows every connected channel. One has the badge “Main”: it is chosen first when you add a source or a quick upload, and your own videos and viral picks go there.",
        "Press “Make main” on another channel to change that.",
        "Press “Move everything here” to switch channels completely: all sources and all clips that are not uploaded yet move to that channel.",
        "One source can also use a different channel: Sources → the channel menu in that source's row.",
        "Videos that are already on YouTube always stay on the channel they were uploaded to.",
    ],
    "verify": [
        "YouTube only allows custom thumbnails on channels with a verified phone number.",
        "On the Google account of the channel, open youtube.com/verify, enter a phone number and type the code YouTube sends.",
        "Nothing else to do: thumbnails that were refused are sent again by themselves within 30 minutes.",
    ],
    "face": [
        "Open My channels → “Your face for thumbnails”.",
        "Upload a clear photo of the face (looking at the camera, good light). The background is removed automatically.",
        "It is used on every thumbnail. Each channel can also have its own face: press “Own face” in that channel's row.",
    ],
    "helper": [
        "The helper is a small program on a Windows PC at home. It downloads videos, makes reels, captions and voices, because YouTube blocks downloads from servers.",
        "On the PC that already has it: nothing to do — it starts by itself when Windows starts. If it shows offline, switch the PC on and check the internet.",
        "On a new PC: 1) install Python 3.11+ from python.org (tick “Add to PATH”) and Git from git-scm.com; 2) copy the project folder to the PC (or download it from the project's GitHub page: Code → Download ZIP, and unzip it); 3) double-click worker\\run_worker.bat — the first start installs what it needs; 4) the window shows a pairing code; open My channels → Helper PC, type that code and press Approve; 5) double-click worker\\install_autostart.bat once so it starts with Windows.",
        "The helper updates itself when the dashboard gets a new version.",
    ],
    "gmail": [
        "Open My channels → Gmail and press “Connect Gmail”.",
        "Choose the Google account the mails should come from. On “Google hasn't verified this app” press Advanced → Go to Raji Studio.",
        "Tick both Gmail boxes (send, read) and press Allow.",
        "Set “Name at the end of mails” in the same card. Mails to creators are only sent when you press Send; replies are read automatically.",
    ],
    "source": [
        "Press “Add source” (top right).",
        "Choose what the system should make clips from: a topic (Creative Commons videos), a YouTube channel whose owner allowed it, trending topics, or your own reel scripts.",
        "Choose the channel it uploads to, “Videos per run” and “Clips per video”, and the upload times. Leave “Review first” on until you trust the results.",
        "Press Add. The source runs by itself from now on; “Run” starts it immediately.",
        "To change it later: Sources → Edit (upload times, clip length, videos per run, review first, channel).",
    ],
    "perm": [
        "Open Ideas → Permission inbox (or Viral today → Today's 5 picks).",
        "For each creator: check the ready mail (“See / edit the mail”), make sure the email address is right, press Send. “Send all ready mails” sends every ready one.",
        "If a creator has no public email: open their channel's About page, press “View email address”, paste it into the box. Or press Skip.",
        "A clear “yes” by mail starts their clips automatically (you get a message with Undo). Any other reply waits here for your decision.",
    ],
    "viral": [
        "Open Viral today. The list is saved every 2 hours with the date; choose an older day at the top.",
        "Five videos are picked every day. Creative Commons videos and creators who said yes are clipped and scheduled by themselves.",
        "For the others press “Send all ready mails” once a day.",
        "Press “Pick” on any video in the list to add it yourself.",
    ],
    "review": [
        "Open Clips → “Ready to review”.",
        "Open a clip, watch it, change anything you like (text, sound, captions, thumbnail), then press Approve. It is uploaded at the next free time.",
        "Press Discard to throw a clip away.",
        "To upload without checking: Sources → tick “auto upload” for that source.",
    ],
    "failed": [
        "Press “Retry all failed”. The clips are made again.",
        "If the same clip fails again, open it: the reason is shown in red at the top, with what to do.",
    ],
    "ai": [
        "Press “Install now”. The helper PC downloads the free local AI (about 3 GB in total) and shows the progress here. A slow or broken internet connection only makes it take longer — it carries on by itself.",
        "When it is ready, reel sources with “AI scripts” write their own scripts. Keep “Review first” on: a small free AI makes mistakes.",
    ],
    "telegram": [
        "In Telegram, open @BotFather, send /newbot, choose a name. BotFather gives a long token.",
        "Open My channels → Telegram, paste the token and press Save. (Only you can paste it — it is a password.)",
        "Open your new bot in Telegram and send the /start code the dashboard shows. Alerts and Approve buttons now come to your phone.",
    ],
    "pexels": [
        "Open pexels.com/api, sign up (free) and copy “Your API key”.",
        "Open My channels → “Stock photos for reels (Pexels)”, paste the key, press Save. Reels now use real photos.",
    ],
    "meta": [
        "Instagram: switch the account to Professional and link it to a Facebook Page.",
        "Open developers.facebook.com → Graph API Explorer → your app → Get User Access Token with: pages_show_list, pages_read_engagement, pages_manage_posts, instagram_basic, instagram_content_publish.",
        "Open My channels → Instagram + Facebook, paste the token and press Connect.",
    ],
}

# (what you see, what the system does by itself, what you do)
HELP = [
    ("A page or a button is missing, or a message says “switched off for your account”",
     "The person who runs the system decides what each account can use: pages, buttons, kinds of sources, uploads and daily limits. What is off is hidden, and work that is not allowed waits quietly. Nothing of yours is deleted.",
     "Dashboard → Problems & fixes shows what is paused or limited. To get something switched on, ask the person who gave you access. Super Admin: Users → Access on that account — every switch, limit and the pause buttons are there; changes apply at once."),
    ("I want to use a different YouTube channel",
     "Sources and waiting clips can move to another channel in one press. Uploaded videos stay where they are.",
     "My channels → Connect a YouTube channel (the old one stays connected). Then on the new channel press “Move everything here”. If you no longer want the old one, press Disconnect on it."),
    ("I want to keep this channel and add another one",
     "Each source uploads to the channel chosen for it; the Main channel gets everything new.",
     "My channels → Connect a YouTube channel. Then Sources → choose the channel in each source's row, or press “Make main” on the channel that should get new work."),
    ("I disconnected the only channel",
     "Nothing is lost: sources and clips wait. The moment any channel is connected they attach to it and carry on.",
     "My channels → Connect a YouTube channel."),
    ("“Reconnect needed” on a channel / uploads say the login expired",
     "Uploads to that channel wait; nothing is deleted.",
     "Press the red “Reconnect needed” badge (or Connect a YouTube channel) and choose the same channel again. Uploads that were paused only because of the login start again by themselves."),
    ("Uploads are paused",
     "After a copyright strike or a removed video the system stops uploading to protect the channel.",
     "Read the reason on the Dashboard. Remove risky sources, then press Resume. Three strikes close a channel — use only Creative Commons videos or creators who gave permission."),
    ("Queued clips are not going up",
     "YouTube allows about 5–6 uploads a day through the API. The rest wait and go up after the daily reset (1:30 PM India time).",
     "Nothing. To upload fewer clips a day, lower the number in the source."),
    ("The helper PC is offline",
     "Jobs wait for it (up to 3 tries each). The dashboard tells you when it has been away for a while.",
     "Switch the PC on and check its internet. If it still shows offline, double-click worker\\run_worker.bat. Moving to a new PC: see “Helper PC” in the steps above."),
    ("A clip failed",
     "The reason is saved with the clip. Many failures are retried automatically.",
     "Dashboard → Problems & fixes → “Retry all failed”. If it fails again, open the clip and read the red line."),
    ("The thumbnail is not on YouTube",
     "A refused thumbnail is sent again every 30 minutes.",
     "Verify the channel's phone number at youtube.com/verify (needed once per channel)."),
    ("Where do I see everything that went wrong?",
     "Every problem is collected on the Alerts page, in tabs: Deleted, Strikes & copyright, Not working and System errors. "
     "The red number in the menu counts the new serious ones.",
     "Open Alerts, choose a tab and press the button on the item (Open clip, Retry, Resume uploads, Reconnect…). "
     "Press Dismiss when you have seen it — it stays in the list as history."),
    ("I want to change or remove “Like and subscribe” on my videos",
     "A small line is shown once, for about 3 seconds near the end of every new video. It has no sound and does not blink.",
     "My channels → “Like & subscribe reminder”: switch it off or choose other words. For one clip only: open it → Trim & look. "
     "Clips that are already made keep their look until you press Save & re-render (or Clips → “Make all waiting clips again”)."),
    ("The first comment is not pinned / was not posted",
     "After every upload the channel posts its own first comment once the video is public (scheduled videos: at their "
     "publish time, within about 10 minutes). YouTube does not let any app pin a comment, so you get a message with the "
     "video link. Uploads always come first: when today's YouTube allowance is needed for uploads, comments wait.",
     "To pin: open the link → comments → tap ⋮ next to your comment → Pin. If a clip says “no permission”: My channels → "
     "Connect a YouTube channel → choose the same channel and tick every box; waiting comments then go out by themselves. "
     "Words: My channels → “First comment”; one clip: open it → Details."),
    ("My thumbnail does not show on my Shorts",
     "YouTube shows a frame of the video for Shorts (on the channel's Shorts tab and in the feed), not the uploaded thumbnail — nobody can choose it through the API. So every Short carries the title and your small face at the top of every frame, and the thumbnail itself is shown for the first moment of the video.",
     "Nothing. To choose the exact picture yourself: YouTube app on your phone → the Short → Edit → thumbnail → pick the first frame. To switch the title band off for one clip: open it → Trim & look. Face size: My channels → “Size on thumbnails”."),
    ("Shorts look cut / zoomed in",
     "New Shorts show the whole picture on a blurred background.",
     "Open the clip → Trim & look → Picture: “Whole video — nothing cut”, then Save & re-render. “Fill the screen” is the zoomed look."),
    ("The voice and the video do not end together",
     "The Voice tab measures both and shows the difference.",
     "Open the clip → Voice → “Make the clip as long as the voice” or “Fit the voice's speed to the clip”, then Save & re-render."),
    ("I want to change a source (upload times, clip length, how many clips)",
     "New clips from that source use the new settings; clips that are already made stay as they are.",
     "Sources → Edit in that source's row → Save changes."),
    ("I changed the look and want the waiting clips made again",
     "Every clip in “Ready to review” and “Queued” is rendered again; upload times are kept.",
     "Clips → “Make all waiting clips again”."),
    ("An AI reel is bad",
     "The reel is thrown away and the local AI writes a new one on another topic.",
     "Open the reel → “Reject — write a new one”."),
    ("How are keywords chosen?",
     "For every clip the system looks up what people really type into YouTube search about that video (YouTube's own "
     "search suggestions, most typed first), adds the original video's own tags, and marks what is on today's trending "
     "lists. The strongest phrase leads the title, the next ones stand in the first two lines of the description, "
     "then 3–5 hashtags and the tags (YouTube allows 500 characters). Credit lines are never changed. Clips that wait "
     "more than a day get fresh keywords before they go up. YouTube does not publish search numbers, so none are shown.",
     "Nothing. To look or change: open a clip → Details → Keywords. Click a keyword to add or remove it, or press "
     "“Find better keywords” → “Use these”. If you change the title, description or tags yourself, the system "
     "leaves that clip alone. To research a topic: Ideas → Keyword research."),
    ("I want to upload my own video file",
     "It becomes a clip with every tool: sound, text, captions, thumbnail, schedule.",
     "Clips → “Upload my video” (MP4 or MOV, up to 300 MB). A wide video is uploaded as a full video, a tall one as a Short."),
    ("A creator answered my mail",
     "A plain yes starts their clips by itself. Everything else waits for you.",
     "Ideas → Permission inbox: read the reply and press “Permission given” or “They said no”. Press Undo if an automatic yes was wrong."),
    ("Mails are not sent",
     "At most 5 permission mails a day are sent; the rest wait for the next day.",
     "My channels → Gmail must show Connected. If not, press Connect Gmail."),
    ("AI reels are off-topic or strange",
     "Bad scripts are thrown away and written again automatically.",
     "Keep “Review first” on for AI sources, choose English in Sources (the small free AI writes it better), and discard reels you do not like."),
    ("A video was removed from YouTube or I deleted it",
     "The Alerts page shows it within 2 hours.",
     "Open the clip and press “Upload again” if it was removed by mistake. Do not upload a video again that got a copyright complaint."),
    ("I forgot the dashboard password",
     "A customer's password is reset by the person who runs the system (Users → Reset password): you get a temporary one and change it after signing in. The Super Admin resets their own through the Google account that connected their channel.",
     "Customers: ask the person who gave you access. Super Admin: on the sign-in page press “Forgot password” and sign in with that Google account."),
    ("A red message says something went wrong on the server",
     "The problem is saved in the list below with the time.",
     "Try again after a minute. If it keeps happening, press “Copy details” below and send them to the person who looks after the system."),
]


SECRET_RE = re.compile(r"[A-Za-z0-9_\-\.]{24,}|[\w.+-]+@[\w-]+\.[\w.-]+|\[parameters: .*", re.S)


def scrub(text: str) -> str:
    """Error text that is safe to show and copy: long tokens, e-mail addresses and SQL parameters are cut out."""
    return SECRET_RE.sub("[hidden]", text or "")


def record_error(where: str, exc: BaseException):
    """Remember the last 30 unexpected problems (no secrets: type, short message, where)."""
    try:
        rows = json.loads(get_setting("guide_errors") or "[]")
        last = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        line = f"{last.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{last.lineno}" if last else ""
        text, now = scrub(str(exc))[:300], utcnow().isoformat() + "Z"
        for r in rows[-10:]:  # the same fault seen again (e.g. by every poll of the dashboard): count it, don't repeat it
            if (r["type"], r["line"], r["text"]) == (type(exc).__name__, line, text):
                r["count"], r["at"] = int(r.get("count") or 1) + 1, now
                break
        else:
            rows.append({"at": now, "where": str(where)[:120], "type": type(exc).__name__, "text": text, "line": line, "count": 1})
        set_setting("guide_errors", json.dumps(rows[-30:]))
    except Exception:  # noqa: BLE001  (the error log must never cause an error)
        pass


def recent_errors() -> list:
    try:
        return list(reversed(json.loads(get_setting("guide_errors") or "[]")))
    except Exception:  # noqa: BLE001
        return []


def clear_errors():
    set_setting("guide_errors", "[]")

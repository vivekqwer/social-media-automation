"""Access control: what each customer account may see and do. The Super Admin decides it; the server enforces it.

Everything about it is in this one file:

* PERMS     every switch (key, group, the words shown for it, on/off when nobody decided)
* LIMITS    the numbers per account (sources, clips a day, uploads a day, storage)
* ROUTES    every address of the server and the switch it needs — checked for every request in the HTTP middleware
* the answer for one account: its own switches -> its plan -> the default for everybody -> the catalogue
* the helpers background work uses (uploads paused? sources stopped? today's limits? storage full?)
* plans, the default for new accounts, the activity log, and deleting an account's data

Rules that never change:
* The Super Admin is never restricted — not in the owner's own copy (tenant 1) and not while viewing a customer.
* A switch that is off stops quietly in background work (no errors), and the customer's dashboard says why.
* What an account may do is read fresh from the shared `accounts` table (a small cache, emptied on every change),
  so a change by the Super Admin applies at once, without a new sign-in.
"""
import json
import logging
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Column, DateTime, Integer, String, Text

from . import accounts, tenant
from .accounts import Account, CoreBase, CoreSession
from .db import get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- the catalogue
GROUPS = [("pages", "Pages they can open"), ("channels", "YouTube channels"), ("sources", "Sources"), ("clips", "Clips"),
          ("uploads", "Uploads"), ("extras", "Extras")]

# key, group, what it is called (shown to the Super Admin, and to the customer in the "switched off" message), default
PERMS = [
    ("page.dashboard", "pages", "Dashboard page", True),
    ("page.clips", "pages", "Clips page", True),
    ("page.sources", "pages", "Sources page", True),
    ("page.analytics", "pages", "Analytics page", True),
    ("page.viral", "pages", "Viral today page", True),
    ("page.ideas", "pages", "Ideas page", True),
    ("page.alerts", "pages", "Alerts page", True),
    ("page.settings", "pages", "My channels page", True),
    ("page.guide", "pages", "Setup guide page", True),
    ("page.quick", "pages", "Quick upload", True),

    ("channels.connect", "channels", "Connect a YouTube channel", True),
    ("channels.disconnect", "channels", "Disconnect a YouTube channel", True),
    ("channels.main", "channels", "Change the main channel / move work between channels", True),
    ("channels.pause", "channels", "Pause and resume uploads to a channel", True),
    ("channels.language", "channels", "Title language of a channel (English / Hinglish / Hindi)", True),

    ("sources.add", "sources", "Add sources", True),
    ("sources.edit", "sources", "Change sources (settings, start / stop, scripts)", True),
    ("sources.delete", "sources", "Delete sources", True),
    ("sources.run", "sources", "Run a source now", True),
    ("source.topic", "sources", "Source type: topic (Creative Commons videos)", True),
    ("source.channel", "sources", "Source type: a channel (Creative Commons, with permission, or their own)", True),
    ("source.trending", "sources", "Source type: trending topics", True),
    ("source.reel", "sources", "Source type: original reels from scripts", True),
    ("source.viral", "sources", "Viral picks (5 a day)", True),
    ("ideas.autotune", "sources", "Auto-tune: sources make more of what works (Ideas → What works)", True),
    ("niche.guard", "sources", "One topic per channel: off-topic waiting clips are parked, off-topic videos skipped", True),

    ("clips.create", "clips", "Make a clip from a video link", True),
    ("clips.own_video", "clips", "Upload their own video file", True),
    ("clips.edit", "clips", "Edit clips (trim, text, look, title, description)", True),
    ("clips.approve", "clips", "Approve clips for upload", True),
    ("clips.discard", "clips", "Discard clips", True),
    ("clips.rerender", "clips", "Make clips again (re-render, re-download, retry failed)", True),
    ("clips.upload_now", "clips", "Upload now / choose the upload time", True),
    ("clips.thumbnail", "clips", "Thumbnails (make, upload, send to YouTube)", True),
    ("clips.face", "clips", "Face photo for thumbnails", True),
    ("clips.captions", "clips", "Captions from YouTube", True),
    ("clips.voice", "clips", "Voice-over (make or upload)", True),
    ("clips.reminder", "clips", "Like, share & subscribe reminder on their videos", True),
    ("clips.subscribe_anim", "clips", "Animated like + subscribe + bell at the top of their videos", True),
    ("clips.pc_folder", "clips", "Videos from a folder on their PC (Clips → From my PC)", True),
    ("clips.quality", "clips", "Quality check (clips without real content never go to YouTube)", True),
    ("titles.hook", "clips", "Hook titles (short curiosity titles instead of “video title | Best Moment”)", True),
    ("clips.short", "clips", "Clips end at a natural pause in the speech (±3 s, when captions are there)", True),

    ("uploads.youtube", "uploads", "Upload to YouTube at all", True),
    ("uploads.auto", "uploads", "Automatic upload without review", True),
    ("uploads.comment", "uploads", "First comment under new videos", True),
    ("titles.retitle", "uploads", "Hook titles for videos already on YouTube that are not public yet (20 a day, uploads first)", True),

    ("gmail.connect", "extras", "Connect / disconnect Gmail", True),
    ("gmail.send", "extras", "Permission mails to creators (send, and read their replies)", True),
    ("permissions.manage", "extras", "Permission inbox (find creators, record yes / no)", True),
    ("permissions.auto", "extras", "Permission mails sent by themselves (the account still switches it on)", True),
    ("ideas.competitors", "extras", "Competitor tracker", True),
    ("meta.use", "extras", "Instagram + Facebook", True),
    ("telegram.use", "extras", "Telegram", True),
    ("phone.use", "extras", "Phone notifications (ntfy app) and news alerts (video live, views, subscribers)", True),
    ("report.weekly", "extras", "Weekly report (Monday 9 AM: views, subscribers, top videos — by e-mail and phone app)", True),
    ("keywords.use", "extras", "Keyword helper (research, better keywords, keywords refreshed before upload)", True),
    ("backup.use", "extras", "Backup & restore (download, check and restore a backup; daily backups always run)", True),
    ("strikes.reply", "extras", "Strike responder: the request to the creator to take a complaint back", True),
    ("whatsapp.use", "extras", "WhatsApp alerts", True),
    ("watchdog.repair", "extras", "Self-repair (the system fixes known problems by itself)", True),
    ("pexels.use", "extras", "Pexels stock photos", True),
    ("ai.reels", "extras", "AI reels (local AI on the helper PC)", True),
    ("helper.manage", "extras", "Helper PC (download, helper key, approve, remove)", True),
    ("google.keys", "extras", "Change their own Google project keys", True),
    ("cookies.manage", "extras", "YouTube cookies for the server", True),
    ("affiliate.use", "extras", "Affiliate links", True),
    ("account.rename", "extras", "Rename their system", True),
    ("account.upgrade", "extras", "Ask for more channels", True),
]
LABEL = {k: label for k, _g, label, _d in PERMS}
DEFAULT = {k: d for k, _g, _l, d in PERMS}
PAGES = ["dashboard", "clips", "sources", "analytics", "viral", "ideas", "alerts", "settings", "guide"]  # nav order
SOURCE_KIND = {"topic": "source.topic", "channel": "source.channel", "trending": "source.trending", "reel": "source.reel",
               "viral": "source.viral"}

# key, what it is called, a hint for the Super Admin. Empty = no limit. "channels" lives in accounts.channel_limit.
LIMITS = [
    ("channels", "YouTube channels", "How many channels they may connect."),
    ("sources", "Sources", "How many sources they may have (viral picks do not count)."),
    ("clips_per_day", "Clips made per day", "New clips a day, by hand and by sources together."),
    ("uploads_per_day", "Uploads per day", "Videos sent to YouTube a day (never more than YouTube's own daily allowance)."),
    ("storage_mb", "Storage (MB)", "Size of their data folder. When it is full no new clips are made; nothing is deleted."),
]
LIMIT_KEYS = [k for k, _l, _h in LIMITS if k != "channels"]
CONTROLS = {"uploads_paused": "All uploads paused", "sources_stopped": "All sources stopped"}


def message(key: str) -> str:
    return f"This is switched off for your account: {LABEL.get(key, key)}. Ask the person who gave you access."


ASK = "ask the person who gave you access"

# ---------------------------------------------------------------- every address and what it needs
ANYONE = "anyone"  # no sign-in needed (sign-in pages, public pages, signed links)
SIGNED = "signed-in"  # any signed-in account: its own data, needed by pages that are allowed
SUPER = "super"  # the Super Admin only
HELPER = "helper"  # the helper program: it signs in with its own token, not with an account


def inside(key: str) -> str:
    """The address is open to signed-in accounts, and the switch is checked inside the route itself
    (a browser page that must answer with a readable redirect, or a rule that depends on what is sent)."""
    return "inside:" + key


# method, address (written exactly as in app/main.py), what it needs, what the activity log says (None = not logged)
ROUTES = [
    # --- signing in, public pages
    ("GET", "/login", ANYONE, None),
    ("GET", "/forgot", ANYONE, None),
    ("GET", "/reset", ANYONE, None),
    ("GET", "/register", ANYONE, None),
    ("POST", "/api/login", ANYONE, None),
    ("POST", "/api/logout", ANYONE, None),
    ("POST", "/api/register", ANYONE, None),
    ("POST", "/api/reset-password", ANYONE, None),
    ("GET", "/auth/reset/start", ANYONE, None),
    ("GET", "/auth/callback", ANYONE, None),  # Google sends people back here; the switches are checked inside
    ("GET", "/privacy", ANYONE, None),
    ("GET", "/terms", ANYONE, None),
    ("GET", "/about", ANYONE, None),
    ("GET", "/welcome", ANYONE, None),  # the public home page (visitors who are not signed in also get it at /)
    ("GET", "/api/public/plans", ANYONE, None),  # read-only: plan names and prices for the home page, nothing else
    ("GET", "/healthz", ANYONE, None),
    ("GET", "/helper/phone.py", ANYONE, None),  # the Android phone helper: a public file of the repository, no secrets
    ("GET", "/helper/setup.sh", ANYONE, None),
    ("GET", "/helper/install.ps1", ANYONE, None),  # the Windows helper's one-time setup: a public file, the key comes on the command line
    ("GET", "/media/{cid}/{exp}/{sig}.mp4", ANYONE, None),  # signed, short-lived link for Instagram / Facebook
    ("POST", "/api/pay/razorpay/webhook", ANYONE, None),  # Razorpay's own call, proven by its signature (app/payments.py)
    # --- the account itself
    ("GET", "/api/me", SIGNED, None),
    ("POST", "/api/change-password", SIGNED, "Changed the password"),
    ("POST", "/api/account/upgrade", "account.upgrade", "Asked for more channels"),
    ("POST", "/api/account/brand", "account.rename", "Renamed the system"),
    ("GET", "/api/account/helper-code", "helper.manage", None),
    ("POST", "/api/account/shared-helper", "helper.manage", "Changed whether the shared helper may download"),
    # --- plans and online payment (any customer may see the plans and pay; the amount always comes from the server)
    ("GET", "/api/plans", SIGNED, None),
    ("POST", "/api/pay/order", SIGNED, "Started an online payment"),
    ("POST", "/api/pay/verify", SIGNED, "Paid for a plan online"),
    # --- step-by-step setup
    ("GET", "/api/onboarding", SIGNED, None),
    ("POST", "/api/onboarding/ack", SIGNED, "Confirmed a setup step"),
    ("POST", "/api/onboarding/google", "google.keys", "Saved Google project keys"),
    # --- Super Admin
    ("GET", "/api/admin/users", SUPER, None),
    ("POST", "/api/admin/users/{aid}/approve", SUPER, None),
    ("POST", "/api/admin/users/{aid}", SUPER, None),
    ("POST", "/api/admin/users/{aid}/reset-password", SUPER, None),
    ("DELETE", "/api/admin/users/{aid}", SUPER, None),
    ("POST", "/api/admin/users/{aid}/view", SUPER, None),
    ("POST", "/api/admin/view/exit", SUPER, None),
    ("POST", "/api/admin/users/{aid}/shared-google", SUPER, None),
    ("GET", "/api/admin/pay", SUPER, None),
    ("POST", "/api/admin/pay", SUPER, None),
    ("GET", "/api/admin/selftest", SUPER, None),
    ("GET", "/api/admin/razorpay", SUPER, None),
    ("POST", "/api/admin/razorpay", SUPER, None),
    ("POST", "/api/admin/razorpay/check", SUPER, None),
    ("GET", "/api/admin/payments", SUPER, None),
    ("GET", "/api/admin/acl", SUPER, None),
    ("POST", "/api/admin/acl/default", SUPER, None),
    ("POST", "/api/admin/plans", SUPER, None),
    ("DELETE", "/api/admin/plans/{name}", SUPER, None),
    ("GET", "/api/admin/activity", SUPER, None),
    ("GET", "/api/admin/users/{aid}/access", SUPER, None),
    ("POST", "/api/admin/users/{aid}/access", SUPER, None),
    ("POST", "/api/admin/users/{aid}/plan", SUPER, None),
    ("POST", "/api/admin/users/{aid}/allow-plan", SUPER, None),
    ("POST", "/api/admin/users/{aid}/control", SUPER, None),
    ("DELETE", "/api/admin/users/{aid}/channels/{did}", SUPER, None),
    ("POST", "/api/admin/users/{aid}/delete-data", SUPER, None),
    ("POST", "/api/admin/users/{aid}/trial", SUPER, None),  # extend / end a free trial
    ("GET", "/api/admin/signup", SUPER, None),  # new accounts: approve by hand or start at once; the free trial
    ("POST", "/api/admin/signup", SUPER, None),
    ("POST", "/api/admin/workers/{wid}/central", SUPER, None),  # a helper of the owner's copy serves every account
    # --- pages: what each one reads
    ("GET", "/api/overview", SIGNED, None),  # counts and channel list every page needs
    ("GET", "/api/stats", SIGNED, None),
    ("GET", "/api/health", "page.dashboard", None),
    ("GET", "/api/setup", "page.dashboard", None),
    ("GET", "/api/clips", "page.clips", None),
    ("GET", "/api/channels", "page.sources", None),
    ("GET", "/api/analytics", "page.analytics", None),
    ("POST", "/api/analytics/sync", "page.analytics", None),
    ("GET", "/api/reports", "page.analytics", None),
    ("GET", "/api/thumb-tests", "page.analytics", None),  # thumbnail A/B tests: which one won
    ("GET", "/api/reports/{name}", "page.analytics", None),
    ("POST", "/api/reports/weekly", "page.analytics", "Made a weekly report"),
    ("GET", "/api/viral", "page.viral", None),
    ("POST", "/api/viral/refresh", "page.viral", None),
    ("GET", "/api/ideas", "page.ideas", None),
    ("GET", "/api/permissions", "page.ideas", None),
    ("GET", "/api/insights", "page.ideas", None),  # Ideas -> What works (read only)
    ("GET", "/api/alerts", "page.alerts", None),
    ("POST", "/api/alerts/seen", "page.alerts", None),
    ("POST", "/api/alerts/check", "page.alerts", None),
    ("GET", "/api/alerts/board", "page.alerts", None),
    ("POST", "/api/alerts/board/seen", "page.alerts", None),
    ("*", "/api/alerts/{rest:path}", "page.alerts", None),  # anything else the Alerts page gets later
    # strike responder (Alerts → Strikes; app/strikes.py): each account only ever sees and acts on its own cases
    ("GET", "/api/strikes", "page.alerts", None),
    ("GET", "/api/settings/strike-reply", SIGNED, None),
    ("POST", "/api/settings/strike-reply", "page.alerts", "Changed automatic sending of the request after a strike"),  # ON also needs gmail.send (inside)
    ("POST", "/api/strikes/{sid}/send", "strikes.reply", "Sent a request to a creator to retract a complaint"),  # gmail.send inside
    ("POST", "/api/strikes/{sid}/solved", "page.alerts", "Marked a strike case as solved"),
    ("POST", "/api/strikes/{sid}/release", "sources.edit", "Allowed a creator again after a strike"),
    ("POST", "/api/strikes/{sid}/block", "sources.edit", "Stopped using a creator after a strike"),
    ("POST", "/api/strikes/{sid}/hide-public", "channels.pause", "Set a struck creator's other videos to private"),
    ("GET", "/api/destinations", "page.settings", None),
    ("GET", "/api/backup", "page.settings", None),  # Backup & restore (My channels): only ever this account's own copy
    # the backup itself (download, check, restore) has its own switch; "My channels page" is checked inside as well
    ("GET", "/api/backup/download", "backup.use", "Downloaded a backup"),
    ("GET", "/api/backup/files/{name}", "backup.use", None),
    ("POST", "/api/backup/check", "backup.use", None),  # reads the file and shows what is in it; nothing changes
    ("POST", "/api/backup/files/{name}/check", "backup.use", None),
    ("POST", "/api/backup/restore", "backup.use", "Restored a backup (all data of this copy replaced)"),
    ("GET", "/api/guide", "page.guide", None),
    ("POST", "/api/guide/errors/clear", "page.guide", None),
    ("POST", "/api/quick", "page.quick", "Started a quick upload"),
    ("GET", "/api/quick", "page.quick", None),
    ("GET", "/api/quick/{pid}", "page.quick", None),
    # --- YouTube channels
    ("GET", "/auth/login", inside("channels.connect"), None),  # renewing an expired login stays possible
    ("GET", "/api/youtube/status", SIGNED, None),
    ("DELETE", "/api/destinations/{did}", "channels.disconnect", "Disconnected a YouTube channel"),
    ("POST", "/api/destinations/{did}/main", "channels.main", "Changed the main channel"),
    ("POST", "/api/destinations/{did}/take-over", "channels.main", "Moved all work to another channel"),
    ("POST", "/api/destinations/{did}/pause", "channels.pause", "Paused uploads to a channel"),
    ("POST", "/api/destinations/{did}/resume", "channels.pause", "Resumed uploads to a channel"),
    ("POST", "/api/destinations/{did}/language", "channels.language", "Changed the title language of a channel"),
    # --- sources (the source type and the number of sources are checked inside POST /api/channels)
    ("POST", "/api/channels", "sources.add", "Added a source"),
    ("PUT", "/api/channels/{chid}", "sources.edit", "Changed a source"),
    ("POST", "/api/channels/{chid}/scripts", "sources.edit", "Added scripts to a source"),
    ("POST", "/api/channels/{chid}/toggle", "sources.edit", "Started / stopped a source"),
    ("POST", "/api/channels/{chid}/review", "sources.edit", "Changed a source's automatic upload"),
    ("POST", "/api/channels/{chid}/language", "sources.edit", None),
    ("POST", "/api/channels/{chid}/destination", "sources.edit", "Changed which channel a source uploads to"),
    ("POST", "/api/channels/{chid}/run", "sources.run", "Ran a source"),
    ("DELETE", "/api/channels/{chid}", "sources.delete", "Deleted a source"),
    # "What is your channel about?" (Get started and Sources): niches -> topic sources (topic switch, source limit inside)
    ("GET", "/api/niches", SIGNED, None),
    ("POST", "/api/niches", "sources.add", "Picked niches (topic sources made for them)"),
    ("POST", "/api/niches/remove", "sources.edit", "Removed a niche (its sources switched off)"),
    ("POST", "/api/insights/autotune", "ideas.autotune", "Switched auto-tune on / off"),  # sources.edit inside (it changes sources)
    ("POST", "/api/insights/undo", "sources.edit", "Undid auto-tune (sources back to their own settings)"),
    ("POST", "/api/viral/pick", "source.viral", "Picked a viral video"),
    # --- clips
    ("POST", "/api/projects", "clips.create", "Opened a video to make a clip"),
    ("GET", "/api/projects", SIGNED, None),
    ("GET", "/api/projects/{pid}", SIGNED, None),
    ("POST", "/api/projects/{pid}/analyze", "clips.create", None),
    ("POST", "/api/projects/{pid}/clips", "clips.create", "Made a clip"),
    ("POST", "/api/clips/{cid}/reupload", "clips.create", "Made a new copy of an uploaded clip"),
    ("POST", "/api/clips/{cid}/get-back", "clips.create", "Asked the helper PC to send a saved video back"),
    ("POST", "/api/upload-video", "clips.own_video", "Uploaded their own video file"),
    # videos in the folder on the PC (Clips -> From my PC, app/pcfolder.py)
    ("GET", "/api/pc-items", "clips.pc_folder", None),  # page.clips inside; the other pc-items routes check clips.pc_folder inside
    ("GET", "/api/pc-items/{iid}/thumb", "clips.pc_folder", None),
    ("POST", "/api/pc-items/{iid}/approve", "clips.approve", "Approved a video from the PC folder"),
    ("POST", "/api/pc-items/approve-all", "clips.approve", "Approved every waiting video from the PC folder"),
    ("POST", "/api/pc-items/{iid}/skip", "clips.discard", "Skipped a video from the PC folder"),
    ("POST", "/api/pc-items/{iid}/unskip", "clips.discard", None),
    ("POST", "/api/pc-items/auto", "uploads.auto", "Switched PC-folder videos going up by themselves on / off"),
    ("GET", "/api/clips/{cid}", SIGNED, None),
    ("GET", "/api/clips/{cid}/sync", SIGNED, None),
    ("GET", "/api/clips/{cid}/video", SIGNED, None),
    ("GET", "/api/clips/{cid}/thumbnail", SIGNED, None),
    ("GET", "/api/queue", SIGNED, None),
    ("GET", "/api/keywords", "page.ideas", None),
    ("POST", "/api/keywords/clip/{cid}", "keywords.use", None),  # a preview only: nothing is saved (clips.edit inside)
    ("POST", "/api/clips/{cid}/remove", "clips.discard", "Removed a deleted video from the dashboard"),
    ("POST", "/api/clips/{cid}/restore", "clips.discard", "Showed a removed video again"),
    ("GET", "/api/voices", SIGNED, None),
    ("PUT", "/api/clips/{cid}", "clips.edit", None),
    ("POST", "/api/clips/{cid}/sync", "clips.edit", None),
    ("POST", "/api/clips/{cid}/metadata", "clips.edit", None),
    ("POST", "/api/clips/{cid}/approve", "clips.approve", "Approved a clip for upload"),
    ("POST", "/api/clips/{cid}/unschedule", "clips.approve", "Took a clip out of the upload queue"),
    ("DELETE", "/api/clips/{cid}", "clips.discard", "Discarded a clip"),
    ("POST", "/api/clips/{cid}/rewrite", "clips.discard", "Rejected an AI reel"),
    ("POST", "/api/clips/{cid}/render", "clips.rerender", "Re-rendered a clip"),
    ("POST", "/api/clips/{cid}/refetch", "clips.rerender", "Downloaded a clip's video again"),
    ("POST", "/api/fix/retry-failed", "clips.rerender", "Retried all failed clips"),
    ("POST", "/api/fix/rerender-waiting", "clips.rerender", "Made all waiting clips again"),
    ("POST", "/api/clips/{cid}/schedule", "clips.upload_now", "Sent a clip to upload (now or at a chosen time)"),
    ("POST", "/api/clips/{cid}/thumbnail", "clips.thumbnail", None),
    ("POST", "/api/clips/{cid}/thumbnail-upload", "clips.thumbnail", "Uploaded a thumbnail"),
    ("POST", "/api/clips/{cid}/thumbnail/push", "clips.thumbnail", None),
    ("POST", "/api/faces/{did}", "clips.face", "Changed the face photo"),
    ("DELETE", "/api/faces/{did}", "clips.face", "Removed the face photo"),
    ("GET", "/api/faces/{did}", SIGNED, None),
    ("GET", "/api/settings/face-size", SIGNED, None),
    ("POST", "/api/settings/face-size", "clips.face", None),
    ("GET", "/api/settings/queue-cap", SIGNED, None),
    ("GET", "/api/settings/upload-lead", SIGNED, None),
    ("POST", "/api/admin/upload-lead", SUPER, "Changed how early clips are sent to YouTube"),
    ("POST", "/api/admin/park-waiting", SUPER, "Parked clips that were waiting to be made"),
    ("POST", "/api/admin/unpark", SUPER, "Parked clips go back into the line a few a day"),
    ("POST", "/api/admin/queue-cap", SUPER, "Changed how many clips may wait before the sources pause"),
    # growth fixes (Nov 2026): hook titles, retitles of scheduled videos, one topic per channel, shorter clips, posts a day
    ("GET", "/api/settings/growth", SIGNED, None),  # read-only: what is on, for the My channels card
    ("POST", "/api/clips/{cid}/hook-title", "titles.hook", None),  # a suggestion only: nothing is saved (clips.edit inside)
    ("GET", "/api/retitle", "titles.retitle", None),
    ("GET", "/api/niche-guard", "niche.guard", None),  # read-only preview of this account's own channels
    ("GET", "/api/settings/short-clips", "clips.short", None),
    ("GET", "/api/settings/posts-per-day", SIGNED, None),
    ("POST", "/api/admin/posts-per-day", SUPER, "Changed public posts per day"),
    ("GET", "/api/admin/niche-guard", SUPER, None),  # dry run: what would be parked
    ("POST", "/api/admin/niche-guard", SUPER, "Ran the niche check / changed a channel's niche words"),
    ("POST", "/api/admin/growth-fixes", SUPER, "Switched a growth fix on / off for this copy"),
    ("POST", "/api/admin/retitle", SUPER, "Ran the retitle of scheduled videos now"),
    ("GET", "/api/settings/reminder", SIGNED, None),
    ("GET", "/api/phone", SIGNED, None),
    ("POST", "/api/phone", "phone.use", "Changed phone notifications"),
    ("POST", "/api/phone/test", "phone.use", None),
    ("GET", "/api/settings/weekly-report", SIGNED, None),
    ("POST", "/api/settings/weekly-report", "report.weekly", "Changed the weekly report"),
    ("GET", "/api/whatsapp", SIGNED, None),
    ("POST", "/api/whatsapp", "whatsapp.use", "Changed WhatsApp alerts"),  # never the number or the key in the log
    ("POST", "/api/whatsapp/test", "whatsapp.use", None),
    ("DELETE", "/api/whatsapp", "whatsapp.use", "Removed WhatsApp alerts"),
    ("GET", "/api/watchdog", "page.alerts", None),
    ("POST", "/api/watchdog/check", "watchdog.repair", None),
    ("POST", "/api/admin/watchdog", SUPER, "Changed self-repair"),
    # quality check (app/quality.py): "Use anyway" writes its own line (also in the owner's copy), so None here
    ("GET", "/api/quality", "page.clips", None),
    ("GET", "/api/quality/rejected", "page.clips", None),
    ("POST", "/api/clips/{cid}/quality/use", "clips.approve", None),
    ("POST", "/api/clips/{cid}/quality/check", "clips.quality", None),
    ("POST", "/api/quality/recheck", "clips.quality", "Checked the rejected clips again"),
    ("POST", "/api/admin/quality", SUPER, "Changed the quality check"),
    ("POST", "/api/settings/reminder", "clips.reminder", "Changed the like & subscribe reminder"),  # clips.edit inside
    ("GET", "/api/settings/subscribe-anim", SIGNED, None),
    ("GET", "/api/settings/subscribe-anim/preview.png", SIGNED, None),
    ("POST", "/api/settings/subscribe-anim", "clips.subscribe_anim", "Changed the like & subscribe animation"),  # clips.edit inside
    ("POST", "/api/subscribe-anim/remake", "clips.subscribe_anim", "Re-made waiting clips with the like & subscribe animation"),  # clips.rerender inside
    ("GET", "/api/settings/comment", SIGNED, None),
    ("POST", "/api/settings/comment", "uploads.comment", "Changed the first comment"),
    ("POST", "/api/clips/{cid}/comment", "uploads.comment", None),
    ("POST", "/api/clips/{cid}/captions", "clips.captions", None),
    ("POST", "/api/clips/{cid}/tts", "clips.voice", None),
    ("POST", "/api/clips/{cid}/voice-upload", "clips.voice", None),
    # --- extras
    ("GET", "/api/gmail", SIGNED, None),
    ("GET", "/auth/gmail", inside("gmail.connect"), None),
    ("DELETE", "/api/gmail", "gmail.connect", "Disconnected Gmail"),
    ("POST", "/api/permissions/{cid}/send", "gmail.send", "Sent a permission mail"),
    ("POST", "/api/viral/send", "gmail.send", "Sent the ready permission mails"),
    ("POST", "/api/permissions/refresh", "permissions.manage", None),
    ("POST", "/api/permissions/rewrite", "permissions.manage", None),
    ("POST", "/api/permissions/find-emails", "permissions.manage", None),
    ("POST", "/api/permissions/{cid}/find-email", "permissions.manage", None),
    ("POST", "/api/permissions/{cid}/mark/{status}", "permissions.manage", None),
    ("POST", "/api/permissions/{cid}/message", "permissions.manage", None),
    ("POST", "/api/permissions/{cid}/email", "permissions.manage", None),
    ("POST", "/api/permissions/{cid}/confirm-email", "permissions.manage", "Confirmed a creator's e-mail address"),
    ("POST", "/api/permissions/{cid}/grant", "permissions.manage", "Recorded a creator's yes"),
    ("POST", "/api/permissions/{cid}/grant-reply", "permissions.manage", "Recorded a creator's yes"),
    ("POST", "/api/permissions/{cid}/revoke", "permissions.manage", "Took back a creator's permission"),
    ("GET", "/api/settings/mail", SIGNED, None),
    ("POST", "/api/settings/mail", "permissions.manage", None),
    ("GET", "/api/settings/perm-auto", "page.ideas", None),
    ("POST", "/api/settings/perm-auto", inside("permissions.auto"), "Changed: send permission mails by themselves"),  # OFF always possible; ON needs permissions.auto + gmail.send
    ("POST", "/api/ideas/competitors", "ideas.competitors", "Added a channel to the competitor tracker"),
    ("DELETE", "/api/ideas/competitors", "ideas.competitors", None),
    ("POST", "/api/ideas/scan", "ideas.competitors", None),
    ("GET", "/api/meta", SIGNED, None),
    ("POST", "/api/meta/setup", "meta.use", "Connected Instagram + Facebook"),
    ("POST", "/api/meta/toggle", "meta.use", None),
    ("POST", "/api/meta/page", "meta.use", "Chose the Facebook Page for Instagram + Facebook"),
    ("POST", "/api/meta/targets", "meta.use", "Changed where Shorts are cross-posted"),
    ("POST", "/api/meta/app", "meta.use", "Saved the Meta app for Instagram + Facebook"),  # never the secret in the log
    ("DELETE", "/api/meta", "meta.use", "Disconnected Instagram + Facebook"),
    ("POST", "/api/clips/{cid}/crosspost", "meta.use", "Posted a clip to Instagram / Facebook"),
    ("GET", "/api/telegram", SIGNED, None),
    ("POST", "/api/telegram/setup", "telegram.use", "Set up Telegram"),
    ("POST", "/api/telegram/toggle", "telegram.use", None),
    ("POST", "/api/telegram/test", "telegram.use", None),
    ("DELETE", "/api/telegram", "telegram.use", "Disconnected Telegram"),
    ("GET", "/api/settings/pexels", SIGNED, None),
    ("POST", "/api/settings/pexels", "pexels.use", "Changed the Pexels key"),
    ("POST", "/api/ai/setup", "ai.reels", "Asked for the local AI to be installed"),
    ("GET", "/api/settings/affiliate", SIGNED, None),
    ("POST", "/api/settings/affiliate", "affiliate.use", "Changed the affiliate links"),
    ("GET", "/api/settings/cookies", SIGNED, None),
    ("POST", "/api/settings/cookies", "cookies.manage", "Saved YouTube cookies"),
    ("DELETE", "/api/settings/cookies", "cookies.manage", "Removed the YouTube cookies"),
    # --- helper PC
    ("GET", "/api/helper/download", "helper.manage", "Downloaded the helper program"),
    ("GET", "/api/workers", SIGNED, None),
    ("POST", "/api/workers/{wid}/approve", "helper.manage", "Approved a helper PC"),
    ("DELETE", "/api/workers/{wid}", "helper.manage", "Removed a helper PC"),
    ("POST", "/api/workers/{wid}/pc-folder", "helper.manage", "Changed the folder of videos a helper PC watches"),
    # helper key (app/enroll.py): helpers that carry it join by themselves; only the account's admin sees it
    ("GET", "/api/helper/key", "helper.manage", None),
    ("POST", "/api/helper/key/show", "helper.manage", "Looked at the helper key (install commands)"),
    ("POST", "/api/helper/key/rotate", "helper.manage", "Made a new helper key (the old one stopped)"),
    ("POST", "/api/helper/key/revoke", "helper.manage", "Switched the helper key off"),
    ("POST", "/api/worker/register", HELPER, None),
    ("GET", "/api/worker/package", HELPER, None),  # the helper program for the install command (needs the helper key)
    ("GET", "/api/worker/jobs", HELPER, None),
    ("GET", "/api/worker/backup", HELPER, None),
    ("GET", "/api/worker/update", HELPER, None),
    ("POST", "/api/worker/script/{chid}", HELPER, None),
    ("POST", "/api/worker/captions/{cid}", HELPER, None),
    ("POST", "/api/worker/source/{cid}", HELPER, None),
    ("POST", "/api/worker/voice/{cid}", HELPER, None),
    ("GET", "/api/worker/archive/{cid}/{name}", HELPER, None),  # an uploaded clip's video, for the copy on the PC
    ("POST", "/api/worker/archived/{cid}", HELPER, None),
    ("POST", "/api/worker/restore/{cid}/{name}", HELPER, None),
    ("POST", "/api/worker/archive-inventory", HELPER, None),
    ("POST", "/api/worker/pc-items", HELPER, None),  # the folder of videos on the PC: details only, never the video
    ("POST", "/api/worker/pc-thumb/{iid}", HELPER, None),
    ("POST", "/api/worker/pc-file/{iid}", HELPER, None),  # the video itself, only when the server asked for it
    ("POST", "/api/worker/fail/{cid}", HELPER, None),
    ("POST", "/api/worker/analysis/{pid}", HELPER, None),
]


def _to_regex(template: str):
    out, pos = "", 0
    for m in re.finditer(r"\{([^}:]+)(:path)?\}", template):
        out += re.escape(template[pos:m.start()]) + (".+" if m.group(2) else "[^/]+")
        pos = m.end()
    return re.compile("^" + out + re.escape(template[pos:]) + "$")


_EXACT: dict[tuple[str, str], tuple] = {}
_PATTERNS: list[tuple] = []  # (method, regex, rule, text, template) — the most specific address first
for _m, _p, _rule, _text in ROUTES:
    if "{" in _p:
        _PATTERNS.append((_m, _to_regex(_p), _rule, _text, _p))
    else:
        _EXACT[(_m, _p)] = (_rule, _text, _p)
_PATTERNS.sort(key=lambda r: (r[0] == "*", ":path}" in r[4], -len(re.sub(r"\{[^}]+\}", "", r[4]))))
_found: dict[tuple[str, str], tuple | None] = {}


def rule_for(method: str, path: str) -> tuple | None:
    """(what it needs, activity text, address as written in ROUTES) for a request, or None when it is not in ROUTES."""
    method = "GET" if method == "HEAD" else method.upper()
    key = (method, path)
    if key in _EXACT:
        return _EXACT[key]
    if key in _found:
        return _found[key]
    hit = None
    for m, rx, rule, text, template in _PATTERNS:
        if m in (method, "*") and rx.match(path):
            hit = (rule, text, template)
            break
    if len(_found) > 4000:
        _found.clear()
    _found[key] = hit
    return hit


# ---------------------------------------------------------------- the activity log (shared table)
class Activity(CoreBase):
    """What customers did and what the Super Admin changed for them (shared: next to the accounts)."""

    __tablename__ = "activity"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=utcnow, index=True)
    account_id = Column(Integer, index=True)  # whose account it is about (empty: plans and defaults)
    tenant_id = Column(Integer)
    actor = Column(String(10))  # user | super | system
    action = Column(String(60), index=True)  # short key, e.g. "sources.add" or "admin.switch"
    detail = Column(Text)  # one readable line


MAX_ACTIVITY = 20000  # the oldest lines are dropped beyond this
_log_count = 0


def record(account_id: int | None, actor: str, action: str, detail: str, tenant_id: int | None = None):
    """Add one line to the activity log. Never raises: the log must not break what it describes."""
    global _log_count
    try:
        with CoreSession() as s:
            s.add(Activity(account_id=account_id, tenant_id=tenant_id, actor=actor[:10], action=action[:60],
                           detail=(detail or "")[:500], at=utcnow()))
            s.commit()
            _log_count += 1
            if _log_count % 200 == 0:
                top = s.query(Activity.id).order_by(Activity.id.desc()).first()
                if top and top[0] > MAX_ACTIVITY:
                    s.query(Activity).filter(Activity.id <= top[0] - MAX_ACTIVITY).delete(synchronize_session=False)
                    s.commit()
    except Exception:  # noqa: BLE001
        log.warning("activity log failed", exc_info=True)


def record_here(action: str, detail: str, actor: str = "system"):
    """A line for the account whose copy the running code works in (background work). Nothing for the owner's copy."""
    tid = tenant.current()
    aid = _account_of(tid)
    if aid:
        record(aid, actor, action, detail, tid)


def activity(account_id: int | None = None, actor: str = "", q: str = "", limit: int = 100, before: int | None = None) -> list[dict]:
    with CoreSession() as s:
        rows = s.query(Activity)
        if account_id is not None:
            rows = rows.filter(Activity.account_id == account_id)
        if actor in ("user", "super", "system"):
            rows = rows.filter(Activity.actor == actor)
        if before:
            rows = rows.filter(Activity.id < before)
        if q.strip():
            like = "%" + q.strip().replace("%", "").replace("_", "")[:60] + "%"
            rows = rows.filter(Activity.detail.ilike(like) | Activity.action.ilike(like))
        rows = rows.order_by(Activity.id.desc()).limit(max(1, min(int(limit), 500))).all()
        names = {a.id: (a.name, a.email) for a in s.query(Account).filter(Account.id.in_({r.account_id for r in rows if r.account_id}))} \
            if rows else {}
        return [{"id": r.id, "at": r.at.isoformat() + "Z" if r.at else None, "account_id": r.account_id,
                 "name": (names.get(r.account_id) or ("", ""))[0], "email": (names.get(r.account_id) or ("", ""))[1],
                 "actor": r.actor, "action": r.action, "detail": r.detail} for r in rows]


# ---------------------------------------------------------------- plans and the default for everybody (owner's settings)
PLANS_KEY = "acl_plans"
_lock = threading.Lock()
TTL = 15.0  # seconds a cached answer may be used; every change empties the cache at once
_cache: dict = {"plans": None, "accounts": {}, "tenants": {}}
PLAN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _+\-]{0,39}$")


def invalidate():
    with _lock:
        _cache["plans"], _cache["accounts"], _cache["tenants"] = None, {}, {}


def _clean_set(data: dict | None) -> dict:
    """{"perms": {key: bool}, "limits": {key: int}} with only keys that exist.
    A limit that is present decides: a number is the limit, 0 means "no limit". A limit that is absent is decided by
    the next layer (plan, then the default). "channels" is only kept in plans (an account's own is a column)."""
    data = data or {}
    perms = {k: bool(v) for k, v in (data.get("perms") or {}).items() if k in DEFAULT and isinstance(v, bool)}
    limits = {}
    for k, v in (data.get("limits") or {}).items():
        n = _int(v)
        if k in LIMIT_KEYS and n is not None:
            limits[k] = max(0, min(n, MAX_LIMIT))
        elif k == "channels" and n:
            limits[k] = max(1, min(n, 50))
    return {"perms": perms, "limits": limits}


MAX_LIMIT = 10_000_000


def _int(v) -> int | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def config() -> dict:
    """{"default": set, "plans": {name: set}, "new_plan": name} — what the Super Admin saved under Plans & defaults."""
    now = time.time()
    hit = _cache["plans"]
    if hit and hit[0] > now:
        return hit[1]
    try:
        with tenant.use(tenant.OWNER):
            raw = json.loads(get_setting(PLANS_KEY) or "{}")
    except ValueError:
        raw = {}
    cfg = {"default": _clean_set(raw.get("default")), "new_plan": str(raw.get("new_plan") or ""),
           "plans": {str(n): _clean_set(p) for n, p in (raw.get("plans") or {}).items() if PLAN_NAME_RE.match(str(n))}}
    if cfg["new_plan"] and _plan_name(cfg, cfg["new_plan"]) is None:
        cfg["new_plan"] = ""
    with _lock:
        _cache["plans"] = (now + TTL, cfg)
    return cfg


def _save_config(cfg: dict):
    with tenant.use(tenant.OWNER):
        set_setting(PLANS_KEY, json.dumps(cfg))
    invalidate()


def _plan_name(cfg: dict, name: str | None) -> str | None:
    """The saved plan this name means (capital letters do not matter), or None."""
    want = (name or "").strip().lower()
    return next((n for n in cfg["plans"] if n.lower() == want), None) if want else None


def new_account_plan() -> str:
    """The plan a new registration starts on ('' = the default set)."""
    try:
        return config()["new_plan"] or ""
    except Exception:  # noqa: BLE001
        return ""


def _plan_of(a: Account) -> str:
    """The plan the Super Admin put this account on. It is kept inside `access`, not in the old `plan` column:
    that column says "starter" for every account made before plans existed, and must not put them on a plan."""
    return str((a.access or {}).get("plan") or "")


def _resolve(access: dict | None, cfg: dict) -> dict:
    """What one account may do: its own switches -> its plan -> the default for everybody -> the catalogue."""
    own = _clean_set(access)
    pname = _plan_name(cfg, (access or {}).get("plan"))
    layers = [own, cfg["plans"].get(pname) or {"perms": {}, "limits": {}}, cfg["default"]]
    perms = {k: next((lay["perms"][k] for lay in layers if k in lay["perms"]), d) for k, d in DEFAULT.items()}
    limits = {k: next((lay["limits"][k] for lay in layers if k in lay["limits"]), 0) or None for k in LIMIT_KEYS}
    controls = {k: bool(((access or {}).get("controls") or {}).get(k)) for k in CONTROLS}
    return {"perms": perms, "limits": limits, "controls": controls, "plan": pname or "", "own": own}


def _eff_of(a: Account) -> dict:
    eff = _resolve(a.access or {}, config())
    eff["limits"]["channels"] = a.channel_limit or accounts.DEFAULT_CHANNELS
    eff["account_id"], eff["tenant_id"] = a.id, a.tenant_id
    return eff


def effective(account_id: int) -> dict | None:
    """The answer for one account (cached for a few seconds; emptied on every change)."""
    now = time.time()
    hit = _cache["accounts"].get(account_id)
    if hit and hit[0] > now:
        return hit[1]
    a = accounts.get(account_id)
    if not a:
        return None
    eff = _eff_of(a)
    with _lock:
        if len(_cache["accounts"]) > 5000:
            _cache["accounts"].clear()
        _cache["accounts"][account_id] = (now + TTL, eff)
    return eff


def _account_of(tid: int) -> int | None:
    """The account that owns a private copy (None for the owner's copy and for copies without an account)."""
    if tid == tenant.OWNER:
        return None
    now = time.time()
    hit = _cache["tenants"].get(tid)
    if hit and hit[0] > now:
        return hit[1]
    with CoreSession() as s:
        row = s.query(Account.id).filter(Account.tenant_id == tid).first()
    aid = row[0] if row else None
    with _lock:
        _cache["tenants"][tid] = (now + TTL, aid)
    return aid


def state(tenant_id: int | None = None) -> dict | None:
    """The switches of the copy the running code works in. None = no restrictions (the owner's own copy)."""
    tid = tenant.current() if tenant_id is None else int(tenant_id)
    aid = _account_of(tid)
    return effective(aid) if aid else None


# ---------------------------------------------------------------- "is it allowed?" — used by routes and background work
_super: ContextVar[bool] = ContextVar("acl_super", default=False)


@contextmanager
def as_super():
    """The Super Admin's own request (also while viewing a customer's dashboard): nothing is restricted."""
    token = _super.set(True)
    try:
        yield
    finally:
        _super.reset(token)


def allowed(key: str) -> bool:
    if _super.get():
        return True
    st = state()
    return True if st is None else bool(st["perms"].get(key, DEFAULT.get(key, True)))


def need(key: str):
    """For a route: stop with the clear 403 when this switch is off for the signed-in account."""
    if not allowed(key):
        from fastapi import HTTPException

        raise HTTPException(403, message(key))


def control(name: str) -> bool:
    """A direct control of the Super Admin (uploads paused / sources stopped). Not lifted by who is asking."""
    st = state()
    return bool(st and st["controls"].get(name))


def limit(name: str) -> int | None:
    """A limit of the current account (None = no limit)."""
    if _super.get():
        return None
    st = state()
    return None if st is None else st["limits"].get(name)


PT = ZoneInfo("America/Los_Angeles")  # the same "day" as YouTube's own daily allowance (it restarts about 1:30 PM India time)


def day_start(now: datetime | None = None) -> datetime:
    pt = (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(PT)
    return pt.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)


def clips_today() -> int:
    from .db import Clip, SessionLocal

    with SessionLocal() as s:
        return s.query(Clip).filter(Clip.created_at >= day_start()).count()


def uploads_today() -> int:
    from .db import Clip, SessionLocal

    with SessionLocal() as s:
        return s.query(Clip).filter(Clip.attempted_at >= day_start()).count()


def source_count() -> int:
    from .db import Channel, SessionLocal

    with SessionLocal() as s:
        return s.query(Channel).filter((Channel.kind.is_(None)) | (Channel.kind != "viral")).count()


# --- storage: the size of the customer's data folder, measured rarely and remembered
STORAGE_TTL = 600.0
_storage: dict[int, tuple[float, int]] = {}


def tenants_dir() -> Path:
    from .config import BASE_DATA_DIR

    return (Path(BASE_DATA_DIR) / "tenants").resolve()


def tenant_folder(tid: int) -> Path:
    """The data folder of a customer WITHOUT creating it. Never the owner's folder."""
    tid = int(tid)
    if tid <= tenant.OWNER:
        raise ValueError("The owner's own data folder is not a customer folder.")
    base = tenants_dir()
    p = (base / str(tid)).resolve()
    if p.parent != base or p.name != str(tid):
        raise ValueError("Not a customer folder.")
    return p


def _folder_bytes(path: Path) -> int:
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def storage_bytes(tenant_id: int | None = None, fresh: bool = False) -> int:
    """Size of a customer's data folder. Measured at most every 10 minutes (never on every request)."""
    tid = tenant.current() if tenant_id is None else int(tenant_id)
    if tid <= tenant.OWNER:
        return 0
    now = time.time()
    hit = _storage.get(tid)
    if hit and not fresh and hit[0] > now:
        return hit[1]
    try:
        folder = tenant_folder(tid)
        size = _folder_bytes(folder) if folder.exists() else 0
    except ValueError:
        size = 0
    _storage[tid] = (now + STORAGE_TTL, size)
    return size


def storage_block() -> str:
    """'' when new files may be saved, else the reason in plain words."""
    cap = limit("storage_mb")
    if cap is None:
        return ""
    used = storage_bytes() / (1024 * 1024)
    if used < cap:
        return ""
    return (f"Your storage is full ({used:.0f} MB of {cap} MB). Discard clips you no longer need, or {ASK} for more space. "
            "Nothing was deleted.")


def clip_block(n: int = 1) -> str:
    """'' when `n` more clips may be made now, else the reason in plain words."""
    cap = limit("clips_per_day")
    if cap is not None:
        made = clips_today()
        if made + n > cap:
            return (f"Your account can make {cap} clip{'' if cap == 1 else 's'} a day and {made} "
                    f"{'was' if made == 1 else 'were'} made today. More can be made tomorrow, or {ASK} for a higher limit.")
    return storage_block()


def _stop(text: str):
    from fastapi import HTTPException

    raise HTTPException(403, text)


def need_clip_room(n: int = 1):
    why = clip_block(n)
    if why:
        _stop(why)


def need_storage():
    why = storage_block()
    if why:
        _stop(why)


def need_source_room():
    cap = limit("sources")
    if cap is not None:
        have = source_count()
        if have >= cap:
            _stop(f"Your account can have {cap} source{'' if cap == 1 else 's'} and {have} "
                  f"{'exists' if have == 1 else 'exist'} already. Delete one first, or {ASK} for more.")


def source_block(kind: str | None, auto_scripts: bool = False) -> str:
    """Why a source must not run now ('' = it may). Used by the source timer and by Run now."""
    st = state()
    if st is None:
        return ""
    if st["controls"].get("sources_stopped"):
        return "all sources are stopped by the administrator"
    key = SOURCE_KIND.get(kind or "channel", "source.channel")
    if not st["perms"].get(key, True):
        return f"switched off for your account ({LABEL[key]})"
    if kind == "reel" and auto_scripts and not st["perms"].get("ai.reels", True):
        return f"switched off for your account ({LABEL['ai.reels']})"
    cap = st["limits"].get("clips_per_day")
    if cap is not None and clips_today() >= cap:
        return f"today's limit of {cap} clip{'' if cap == 1 else 's'} for your account is reached"
    cap = st["limits"].get("storage_mb")
    if cap is not None and storage_bytes() / (1024 * 1024) >= cap:
        return "your storage is full"
    return ""


def room_for_clip() -> str:
    """Background work, before each new clip: '' or why no more clips are made now."""
    st = state()
    if st is None:
        return ""
    cap = st["limits"].get("clips_per_day")
    if cap is not None and clips_today() >= cap:
        return f"today's limit of {cap} clip{'' if cap == 1 else 's'} for your account is reached"
    cap = st["limits"].get("storage_mb")
    if cap is not None and storage_bytes() / (1024 * 1024) >= cap:
        return "your storage is full"
    return ""


def auto_upload() -> bool:
    """May new clips of this account go to YouTube without being approved first?"""
    st = state()
    return True if st is None else bool(st["perms"].get("uploads.auto", True))


def upload_block() -> str:
    """Why the upload queue must not run for this account now ('' = it may)."""
    st = state()
    if st is None:
        return ""
    if st["controls"].get("uploads_paused"):
        return "Uploads are paused by the administrator."
    if not st["perms"].get("uploads.youtube", True):
        return "Uploading to YouTube is switched off for your account."
    return ""


def channel_cap() -> int | None:
    """How many of this account's YouTube channels may upload (None = all of them: the owner's own copy)."""
    st = state()
    return None if st is None else int(st["limits"].get("channels") or accounts.DEFAULT_CHANNELS)


def over_cap_channels(s) -> set[int]:
    """Channels beyond the plan's channel limit (e.g. after a paid plan ended). They stay connected and keep their
    clips; only their uploads wait. The main channel is always the first one allowed, then the oldest."""
    cap = channel_cap()
    if cap is None:
        return set()
    from .db import Destination, main_destination

    ids = [d.id for d in s.query(Destination.id).order_by(Destination.id)]
    if len(ids) <= cap:
        return set()
    main = main_destination(s)
    order = ([main.id] if main else []) + [i for i in ids if not main or i != main.id]
    return set(order[cap:])


def uploads_cap(server_cap: int) -> int:
    st = state()
    cap = None if st is None else st["limits"].get("uploads_per_day")
    return server_cap if cap is None else min(server_cap, cap)


def feature(key: str) -> bool:
    """Background work: is this switch on for the account whose copy is being worked on?"""
    st = state()
    return True if st is None else bool(st["perms"].get(key, True))


def health_items() -> list[dict]:
    """Lines for the customer's "Problems & fixes" list, so they can see what the administrator switched off."""
    st = state()
    if st is None:
        return []
    out, tail = [], " Ask the person who gave you access."
    if st["controls"].get("uploads_paused"):
        out.append({"key": "acl_uploads_paused", "level": "bad", "text": "Uploads are paused by the administrator. Your clips stay queued; nothing is lost." + tail, "fix": None})
    elif not st["perms"].get("uploads.youtube", True):
        out.append({"key": "acl_uploads_off", "level": "bad", "text": "Uploading to YouTube is switched off for your account. Your clips stay queued." + tail, "fix": None})
    if st["controls"].get("sources_stopped"):
        out.append({"key": "acl_sources_stopped", "level": "bad", "text": "All sources are stopped by the administrator. No new clips are made until they are started again." + tail, "fix": None})
    if not st["perms"].get("uploads.auto", True):
        out.append({"key": "acl_auto_off", "level": "info", "text": "Automatic upload is switched off for your account: every new clip waits in Clips → Ready to review until you approve it.", "fix": None})
    lim = st["limits"]
    try:
        from .db import Destination, SessionLocal

        with SessionLocal() as s:
            over = over_cap_channels(s)
            names = [d.name for d in s.query(Destination).filter(Destination.id.in_(over))] if over else []
        if names:
            out.append({"key": "acl_channels_over", "level": "bad", "text": f"Your plan allows {lim.get('channels') or 1} YouTube channel(s). "
                        f"Uploads to {', '.join(names)} wait (nothing is deleted). Renew or change your plan on My channels → Your plan, "
                        "or disconnect a channel.", "fix": None})
    except Exception:  # noqa: BLE001
        pass
    if lim.get("uploads_per_day") is not None and uploads_today() >= lim["uploads_per_day"]:
        out.append({"key": "acl_uploads_limit", "level": "info", "text": f"Today's limit of {lim['uploads_per_day']} upload(s) for your account is reached. "
                                             "The rest go up tomorrow by themselves.", "fix": None})
    if lim.get("clips_per_day") is not None and clips_today() >= lim["clips_per_day"]:
        out.append({"key": "acl_clips_limit", "level": "info", "text": f"Today's limit of {lim['clips_per_day']} new clip(s) for your account is reached. "
                                             "Sources carry on tomorrow by themselves.", "fix": None})
    if lim.get("storage_mb") is not None:
        used = storage_bytes() / (1024 * 1024)
        if used >= lim["storage_mb"]:
            out.append({"key": "acl_storage", "level": "bad", "text": f"Your storage is full ({used:.0f} MB of {lim['storage_mb']} MB). No new clips are made. "
                                                "Discard clips you no longer need, or ask for more space. Nothing is deleted by itself.", "fix": None})
    return out


def for_me(who: dict) -> dict | None:
    """What /api/me tells the dashboard, so it can hide what is off (customers only)."""
    if who.get("role") != "user" or not who.get("account_id"):
        return None
    st = effective(who["account_id"])
    if not st:
        return None
    return {"perms": st["perms"], "limits": st["limits"], "controls": st["controls"], "plan": st["plan"]}


# ---------------------------------------------------------------- the HTTP middleware's part
UNKNOWN = "This action is not open for customer accounts yet. Ask the person who gave you access."


def check_request(method: str, path: str, who: dict) -> str | None:
    """None when the request may go on, else the message for the 403. Only customers are ever stopped here."""
    if who.get("role") != "user":
        return None
    hit = rule_for(method, path)
    if hit is None:
        # an address nobody decided about: reading is harmless (every copy is private), changing is refused
        return UNKNOWN if path.startswith("/api/") and method.upper() not in ("GET", "HEAD", "OPTIONS") else None
    rule = hit[0]
    if rule in (ANYONE, SIGNED, HELPER) or rule.startswith("inside:"):
        return None
    if rule == SUPER:
        return "Only the Super Admin can do this."
    st = effective(who["account_id"])
    if st is None or st["perms"].get(rule, True):
        return None
    return message(rule)


def log_request(who: dict, method: str, path: str, status: int):
    """After a request that changed something: one line in the activity log (customers, and the Super Admin while
    working inside a customer's dashboard)."""
    try:
        if status >= 400:
            return
        hit = rule_for(method, path)
        if not hit or not hit[1]:  # ROUTES gives the words; an address without words is not worth a line
            return
        if who.get("role") == "user":
            aid, actor = who.get("account_id"), "user"
        elif who.get("viewing"):
            aid, actor = who["viewing"].get("account_id"), "super"
        else:
            return
        ids = re.findall(r"/(\d+)(?=/|$)", path)
        key = hit[0].replace("inside:", "")
        record(aid, actor, "account" if key in (SIGNED, ANYONE) else key, hit[1] + (f" (#{ids[0]})" if ids else ""),
               who.get("tenant"))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- Super Admin: one account
def catalog() -> dict:
    return {"groups": [{"id": g, "title": t} for g, t in GROUPS],
            "perms": [{"key": k, "group": g, "label": lab, "default": d} for k, g, lab, d in PERMS],
            "limits": [{"key": k, "label": lab, "hint": h} for k, lab, h in LIMITS],
            "controls": CONTROLS}


def _account(aid: int) -> Account:
    a = accounts.get(aid)
    if not a:
        raise KeyError(aid)
    return a


def _write_access(aid: int, change) -> dict:
    """Read-change-write of one account's own switches (the JSON column; an empty one is None on PostgreSQL).
    One at a time: two switches pressed quickly after each other must both be kept."""
    with _write_lock, CoreSession() as s:
        a = s.query(Account).filter(Account.id == int(aid)).with_for_update().first()
        if not a:
            raise KeyError(aid)
        acc = _own(a)
        change(acc, a)
        a.access = acc
        s.commit()
    invalidate()
    return acc


_write_lock = threading.Lock()


def _own(a: Account) -> dict:
    """A fresh copy of what is saved for one account, with every part present."""
    acc = a.access or {}
    perms, limits = dict(acc.get("perms") or {}), dict(acc.get("limits") or {})
    held = acc.get("held")
    if isinstance(held, dict):
        held = {"perms": [k for k in held.get("perms") or [] if k in DEFAULT],
                "limits": [k for k in held.get("limits") or [] if k in LIMIT_KEYS or k == "channels"]}
    else:  # saved before restrictions were marked: every own OFF switch and own limit counts as one (the safe reading)
        held = {"perms": [k for k, v in perms.items() if v is False and k in DEFAULT],
                "limits": [k for k, v in limits.items() if k in LIMIT_KEYS and _int(v)]}
    return {"plan": str(acc.get("plan") or ""), "perms": perms, "limits": limits,
            "controls": dict(acc.get("controls") or {}), "held": held}


def _hold(acc: dict, kind: str, key: str, on: bool):
    """Mark (or unmark) one switch or limit as restricted by the Super Admin for this account. Only these survive a
    payment or a paid plan's end; values an account merely kept from a deleted or edited plan do not."""
    keys = acc["held"][kind]
    if on and key not in keys:
        keys.append(key)
    elif not on and key in keys:
        keys.remove(key)


def set_access(aid: int, perms: dict | None = None, limits: dict | None = None) -> list[str]:
    """Switches and limits of one account.
    A switch: True / False, or None to remove the account's own setting (it follows its plan again).
    A limit: a number, None or 0 for "no limit for this account", -1 to follow its plan again.
    Returns the readable changes (also written to the activity log)."""
    a = _account(aid)
    before = _eff_of(a)
    bad = [k for k in (perms or {}) if k not in DEFAULT] + [k for k in (limits or {}) if k not in LIMIT_KEYS and k != "channels"]
    if bad:
        raise ValueError("Unknown setting: " + ", ".join(bad))

    def change(acc, row):
        for k, v in (perms or {}).items():
            if v is None:
                acc["perms"].pop(k, None)
                _hold(acc, "perms", k, False)
            else:
                if bool(v):
                    _hold(acc, "perms", k, False)
                elif acc["perms"].get(k) is not False:
                    _hold(acc, "perms", k, True)  # switched OFF here, by hand: a payment does not switch it on again
                acc["perms"][k] = bool(v)
        for k, v in (limits or {}).items():
            n = _int(v)
            if k == "channels":
                if n is not None and n >= 1:
                    was, new = row.channel_limit or accounts.DEFAULT_CHANNELS, min(n, 50)
                    if new < was:
                        _hold(acc, "limits", "channels", True)  # lowered by hand: a payment does not raise it
                    elif new > was:
                        _hold(acc, "limits", "channels", False)  # raised by hand: a gift, not a restriction
                    row.channel_limit, row.limit_by_hand = new, True  # a paid plan's end leaves it alone
            elif n is not None and n < 0:
                acc["limits"].pop(k, None)  # follow the plan again
                _hold(acc, "limits", k, False)
            else:
                new = min(n or 0, MAX_LIMIT)  # 0 = no limit for this account, whatever its plan says
                if not new:
                    _hold(acc, "limits", k, False)
                elif _int(acc["limits"].get(k)) != new:
                    _hold(acc, "limits", k, True)  # a number set here, by hand
                acc["limits"][k] = new

    _write_access(aid, change)
    a = _account(aid)
    after = _eff_of(a)
    done = []
    for k in DEFAULT:
        if before["perms"][k] != after["perms"][k]:
            done.append(f"{LABEL[k]}: switched {'on' if after['perms'][k] else 'off'}")
    names = {k: lab for k, lab, _h in LIMITS}
    for k in ["channels"] + LIMIT_KEYS:
        if before["limits"].get(k) != after["limits"].get(k):
            done.append(f"{names[k]}: {_show(before['limits'].get(k))} → {_show(after['limits'].get(k))}")
    for line in done:
        record(aid, "super", "admin.switch", line, a.tenant_id)
    if limits and "storage_mb" in limits and a.tenant_id:
        _storage.pop(a.tenant_id, None)
    return done


def _show(v) -> str:
    return "no limit" if v is None else str(v)


def channels_by_hand(aid: int, was: int, new: int):
    """The Super Admin changed the channel limit on the Users screen: lowering it is a restriction a payment keeps,
    raising it is a gift the paid plan may go beyond."""
    if new != was:
        _write_access(aid, lambda acc, row: _hold(acc, "limits", "channels", new < was))


def set_control(aid: int, name: str, on: bool) -> dict:
    if name not in CONTROLS:
        raise ValueError("Unknown control.")
    a = _account(aid)
    _write_access(aid, lambda acc, row: acc["controls"].__setitem__(name, bool(on)))
    words = {("uploads_paused", True): "Paused all uploads", ("uploads_paused", False): "Resumed uploads",
             ("sources_stopped", True): "Stopped all sources", ("sources_stopped", False): "Started the sources again"}
    record(aid, "super", "admin.control", words[(name, bool(on))], a.tenant_id)
    return _eff_of(_account(aid))["controls"]


def apply_plan(aid: int, name: str, keep_restrictions: bool = False) -> dict:
    """Put an account on a plan in one step: its own switches and limits are cleared, so the plan decides everything.
    '' = no plan (the default for everybody).
    keep_restrictions (a plan put on by a payment or by a paid plan's end, not by the Super Admin): every switch the
    Super Admin set OFF for this account in the Access panel, and every limit they set there that is stricter than the
    plan's, stay. A channel limit they LOWERED by hand stays when it is lower than the plan's. Values an account only
    kept from a deleted or edited plan are not restrictions: the plan that was paid for replaces them. Direct controls
    are never touched here."""
    pname, change, kept = plan_change(name, keep_restrictions)
    a = _account(aid)
    _write_access(aid, change)
    record_plan(aid, pname, kept, keep_restrictions, a.tenant_id)
    return public_access(aid)


def record_plan(aid: int, pname: str, kept: list, keep_restrictions: bool, tenant_id: int | None):
    if keep_restrictions:
        record(aid, "system", "admin.plan", (f"Put on the plan “{pname}”" if pname else "Taken off every plan (default switches)")
               + (f" — kept what the Super Admin restricted: {', '.join(kept)}" if kept else ""), tenant_id)
    else:
        record(aid, "super", "admin.plan", f"Put on the plan “{pname}”" if pname else "Taken off every plan (default switches)", tenant_id)


def apply_in(row: Account, change):
    """The change of plan_change() made on an account row inside the caller's own transaction (app/payments.py: the
    plan, the end date and the payment's "applied" mark are saved together). The caller holds _write_lock and calls
    invalidate() after its commit."""
    acc = _own(row)
    change(acc, row)
    row.access = acc


def plan_change(name: str, keep_restrictions: bool = False):
    """(plan name, change(access, row), list filled with what was kept): what apply_plan writes, without writing it."""
    cfg = config()
    pname = _plan_name(cfg, name) if name else ""
    if name and pname is None:
        raise ValueError("There is no plan with this name.")
    plan_set = cfg["plans"].get(pname) or {"limits": {}}
    plan_limits = _resolve({"plan": pname}, cfg)["limits"]
    kept = []

    def change(acc, row):
        perms, limits, held = {}, {}, {"perms": [], "limits": []}
        was = acc["held"]
        if keep_restrictions:
            perms = {k: False for k in was["perms"] if acc["perms"].get(k) is False and k in DEFAULT}
            for k in was["limits"]:
                n, cap = _int(acc["limits"].get(k)), plan_limits.get(k)
                if k in LIMIT_KEYS and n and (cap is None or n < cap):  # 0 = "no limit": never stricter
                    limits[k] = n
            kept.extend([LABEL[k] for k in perms] + [k for k in limits])
            held = {"perms": list(perms), "limits": list(limits)}
        acc["perms"], acc["limits"], acc["plan"] = perms, limits, pname or ""
        row.plan = pname or ""  # the old column is only the name shown under the customer's name
        ch = plan_set["limits"].get("channels")
        ch_held = keep_restrictions and "channels" in was["limits"]
        if ch:
            if ch_held and row.limit_by_hand and (row.channel_limit or accounts.DEFAULT_CHANNELS) < ch:
                kept.append("channels")  # the channel limit the Super Admin lowered stays
                held["limits"].append("channels")
            else:
                row.channel_limit = ch
                if keep_restrictions:
                    row.limit_by_hand = False  # the plan decides the channel limit now
        elif ch_held:
            held["limits"].append("channels")  # this plan leaves the channel limit alone: the mark stays with it
        acc["held"] = held

    return pname or "", change, kept


def usage(tid: int | None) -> dict:
    """What the account uses right now, next to its limits."""
    if not tid:
        return {}
    try:
        from .db import tenant_exists

        if not tenant_exists(tid):  # a deleted copy is not opened (opening would create it again)
            return {"channels": 0, "channel_list": [], "sources": 0, "clips_per_day": 0, "uploads_per_day": 0, "storage_mb": 0}
        with tenant.use(tid):
            from .db import Destination, SessionLocal

            with SessionLocal() as s:
                chans = [{"id": d.id, "name": d.name, "paused": bool(d.paused)} for d in s.query(Destination).order_by(Destination.id)]
            return {"channels": len(chans), "channel_list": chans, "sources": source_count(), "clips_per_day": clips_today(),
                    "uploads_per_day": uploads_today(), "storage_mb": round(storage_bytes(tid) / (1024 * 1024), 1)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def public_access(aid: int, with_usage: bool = False) -> dict:
    """Everything the Super Admin's Access panel shows for one account."""
    a = _account(aid)
    eff = _eff_of(a)
    out = {"account": accounts.public(a), "perms": eff["perms"], "own_perms": eff["own"]["perms"], "limits": eff["limits"],
           "own_limits": eff["own"]["limits"], "controls": eff["controls"], "plan": eff["plan"]}
    if with_usage:
        out["usage"] = usage(a.tenant_id) if a.status != "pending" else {}
        out["activity"] = activity(account_id=aid, limit=40)
    return out


# ---------------------------------------------------------------- Super Admin: plans and the default
def overview() -> dict:
    cfg = config()
    with CoreSession() as s:
        rows = [_plan_of(a) for a in s.query(Account).all()]
    on_plan = {n: 0 for n in cfg["plans"]}
    none = 0
    for plan in rows:
        p = _plan_name(cfg, plan)
        if p:
            on_plan[p] += 1
        else:
            none += 1
    return {"catalog": catalog(), "default": cfg["default"], "new_plan": cfg["new_plan"], "no_plan_users": none,
            "plans": [{"name": n, **cfg["plans"][n], "users": on_plan[n]} for n in sorted(cfg["plans"], key=str.lower)]}


def _freeze_or_follow(old_cfg: dict, new_cfg: dict, on_plan, apply: bool) -> int:
    """After a plan (or the default) changed: either the accounts that use it follow the new values (their own differing
    switches for the changed keys are removed), or they keep exactly what they have now (the old value is written into
    the account). Returns how many accounts were touched."""
    touched = 0

    def plan_channels(cfg, a):
        return (cfg["plans"].get(_plan_name(cfg, _plan_of(a))) or {"limits": {}})["limits"].get("channels")

    with _write_lock, CoreSession() as s:
        for a in s.query(Account).all():
            if not on_plan(a):
                continue
            old, new = _resolve({"plan": _plan_of(a)}, old_cfg), _resolve({"plan": _plan_of(a)}, new_cfg)
            p_keys = [k for k in DEFAULT if old["perms"][k] != new["perms"][k]]
            l_keys = [k for k in LIMIT_KEYS if old["limits"][k] != new["limits"][k]]
            ch_old, ch_new = plan_channels(old_cfg, a), plan_channels(new_cfg, a)
            if not p_keys and not l_keys and ch_old == ch_new:
                continue  # nothing this account gets from the plan changed
            touched += 1
            acc = _own(a)
            perms, limits = acc["perms"], acc["limits"]
            for k in p_keys:
                if apply:
                    perms.pop(k, None)
                else:
                    perms.setdefault(k, old["perms"][k])
            for k in l_keys:
                if apply:
                    limits.pop(k, None)
                else:
                    limits.setdefault(k, old["limits"][k] or 0)
            if apply and ch_new and ch_new != ch_old:
                a.channel_limit = ch_new
            a.access = acc
        s.commit()
    invalidate()
    return touched


def save_plan(name: str, perms: dict, limits: dict, apply: bool = True) -> dict:
    """Create or change a plan. `apply`: accounts on the plan follow the change (True) or keep what they have (False)."""
    name = re.sub(r"\s+", " ", name or "").strip()
    if not PLAN_NAME_RE.match(name):
        raise ValueError("Give the plan a short name with letters and numbers (for example “Starter” or “Pro”).")
    old = config()
    existing = _plan_name(old, name)
    new = json.loads(json.dumps(old))
    new["plans"].pop(existing, None)
    new["plans"][existing or name] = _clean_set({"perms": perms, "limits": limits})
    final = existing or name
    _save_config(new)
    n = _freeze_or_follow(old, config(), lambda a: (_plan_name(old, _plan_of(a)) or "") == final, apply) if existing else 0
    record(None, "super", "admin.plan", f"{'Changed' if existing else 'Made'} the plan “{final}”"
           + (f" — {'applied to' if apply else 'kept away from'} the {n} account(s) on it" if existing and n else ""))
    return {"name": final, "accounts": n}


def delete_plan(name: str) -> dict:
    old = config()
    existing = _plan_name(old, name)
    if not existing:
        raise KeyError(name)
    new = json.loads(json.dumps(old))
    new["plans"].pop(existing)
    if new.get("new_plan") == existing:
        new["new_plan"] = ""
    _save_config(new)
    with _write_lock, CoreSession() as s:  # accounts that were on it keep what they had, as their own switches
        rows = [a for a in s.query(Account).all() if _plan_name(old, _plan_of(a)) == existing]
        for a in rows:
            was = _resolve(a.access or {}, old)
            acc = _own(a)
            acc.update(plan="", perms=dict(was["perms"]), limits={k: v or 0 for k, v in was["limits"].items()})
            a.access, a.plan = acc, ""
        s.commit()
        n = len(rows)
    invalidate()
    record(None, "super", "admin.plan", f"Deleted the plan “{existing}”" + (f" — {n} account(s) keep their switches" if n else ""))
    return {"deleted": existing, "accounts": n}


def save_default(perms: dict, limits: dict, new_plan: str | None = None, apply: bool = True) -> dict:
    """The switches for accounts without a plan, and which plan a NEW account starts on."""
    old = config()
    new = json.loads(json.dumps(old))
    new["default"] = _clean_set({"perms": perms, "limits": {k: v for k, v in (limits or {}).items() if k != "channels"}})
    if new_plan is not None:
        if new_plan and _plan_name(old, new_plan) is None:
            raise ValueError("There is no plan with this name.")
        new["new_plan"] = _plan_name(old, new_plan) or ""
    _save_config(new)
    n = _freeze_or_follow(old, config(), lambda a: True, apply)
    record(None, "super", "admin.plan", "Changed the default switches" + (f" (new accounts start on “{new['new_plan']}”)" if new["new_plan"] else "")
           + (f" — {'applied to' if apply else 'kept away from'} {n} account(s)" if n else ""))
    return {"accounts": n}


# ---------------------------------------------------------------- Super Admin: delete everything of one account
def delete_account_data(aid: int, typed_email: str) -> dict:
    """Remove a customer's whole private copy: database part and data folder. The login stays (blocked).
    The owner's copy (tenant 1) can never be removed here."""
    from .db import IS_SQLITE, _engines, _makers, _ready, engine

    a = _account(aid)
    if (typed_email or "").strip().lower() != (a.email or "").lower():
        raise ValueError("The e-mail address you typed is not this account's. Nothing was deleted.")
    tid = a.tenant_id
    if not tid:
        raise ValueError("This account has no data yet.")
    tid = int(tid)
    if tid <= tenant.OWNER or tid >= accounts.SELF_TEST_TENANT:
        raise ValueError("This copy cannot be deleted.")
    folder = tenant_folder(tid)  # raises unless it is exactly <data>/tenants/<number>
    accounts.update(aid, status="blocked")  # signed out at once; background work stops for this copy
    accounts.end_sessions(aid)
    invalidate()
    problems = []
    eng = _engines.pop(tid, None)
    _makers.pop(tid, None)
    _ready.discard(tid)
    try:
        if IS_SQLITE:
            if eng is not None:
                eng.dispose()
        else:
            from sqlalchemy import text

            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{tenant.schema(tid)}" CASCADE'))
    except Exception as e:  # noqa: BLE001
        problems.append("database: " + str(e)[:150])
    try:
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
            if folder.exists() and any(folder.iterdir()):
                problems.append("some files are still in use and could not be removed — press the button again in a minute")
    except Exception as e:  # noqa: BLE001
        problems.append("files: " + str(e)[:150])
    _storage.pop(tid, None)
    with _write_lock, CoreSession() as s:
        row = s.get(Account, aid)
        if row:
            acc = dict(row.access or {})
            acc["controls"] = {}
            row.access, row.upgrade = acc, None
            s.commit()
    invalidate()
    record(aid, "super", "admin.delete_data", f"Deleted all data of {a.email} (copy #{tid})"
           + (" — " + "; ".join(problems) if problems else ""), tid)
    return {"deleted": not problems, "problems": problems, "tenant_id": tid}

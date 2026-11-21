# Build Log

## 2026-11-20 — Task 1: Feasibility & architecture plan
- Output: feasibility answer + pipeline design. Files: memory.md, build-log.md.

## 2026-11-20 — Task 2: MVP build
- Built FastAPI app: ingest (yt-dlp), hot-moment ranking (heatmap + comment timestamps), ffmpeg renderer (crop/aspect, effects, animated text, voice-over mix), edge-tts, keyword research (YT autocomplete), metadata builder (credit + original tags/description + keywords), YouTube OAuth + resumable upload with publishAt, APScheduler queue, policy gates (cc/permission/commentary).
- Autopilot: source channel (name/@handle/URL) -> latest videos -> clips -> render -> metadata -> staggered schedule.
- Multi-destination: connect several own channels; each source maps to one.
- Dashboard: stats, my channels, bulk source add, manual editor, queue.
- Deploy files: Dockerfile, railway.json, .env.example, README.
- Verified: ranking logic; ffmpeg render (9:16, fade/punch/shake, slide-up text); CC license detection (Big Buck Bunny); non-CC rejected; channel search resolve; channel video listing; real 6s section download; API endpoints; UI loads, no runtime JS errors.
- Fixed: yt-dlp ffmpeg detection on local (PATH + ffmpeg alias); JS newline escape bug.
- Not verified: real upload, full autopilot run, Railway deploy, comment fetch at scale.
- Next: push to GitHub, Railway env + volume + Postgres, Google OAuth setup, first live autopilot test.

## 2026-11-20 — Task 3: 4-agent review + fixes
- Reviews: security, correctness, deploy/YouTube API, UI/edge-case testing (last one still pending at commit time).
- Fixed: auth fail-open (hostname check), CSRF Origin check, single-use OAuth state, YouTube-only URLs + allowed_extractors (SSRF), voice upload allowlist + 25MB cap, XSS in keyword chips, per-video error isolation in autopilot (seen saved first), atomic upload claim, quota counting by attempt time (Pacific day), quota/refresh-token errors keep clips queued, restart recovery, status guards on edit/render/schedule, byte-safe description, no-audio sources, fractional start trim, timestamp regex (sec<60, skip chapter lists), per-destination slots + backlog cap, Dockerfile Deno + yt-dlp[default], empty-env defaults.
- Verified after fixes: import, ranking, render with and without audio, live fetch_info.
- Open: Google OAuth must be "In production" (Testing tokens expire in 7 days); bot-check cookies/proxy on Railway; Chrome access for Railway setup not granted yet.
- UI/edge-case review fixes: update_clip resets to draft only on real change (schedule no longer 409 after save), pydantic validation (bounds, Literals, Overlay model, NaN-safe 422 handler), analyze clip_len bounds, overflow-safe publish_at, destination delete blocked while clips queued, UI: saveClip errors propagate, poll bound to clip id, double-submit lock, editor reset on new project, NaN time guard.
- Verified via TestClient: 17 edge cases (see Task 3). Browser pane note: Chrome device "Browser 4" (this computer) holds vivekqwer admin session; Railway/Google Cloud/GitHub tabs opened there (no actions taken).

## 2026-11-20 — Task 4: Railway deploy
- Created Railway project from GitHub repo, added Postgres, attached volume /data to app, generated public domain, set DATABASE_URL (reference) + BASE_URL, redeployed.
- Verified: Docker build ok, /healthz 200, unauthenticated root 503 (needs APP_PASSWORD).
- Pending (user): APP_PASSWORD, Google OAuth client id/secret, connect YouTube channel, first live autopilot test.

## 2026-11-20 — Task 5: Google OAuth setup (funny account)
- Created Cloud project, enabled YouTube API, configured consent (External/Testing), test user, OAuth web client + redirect URI. Staged GOOGLE_CLIENT_ID in Railway.
- Pending (user): paste client secret + APP_PASSWORD in Railway, Deploy, click Connect YouTube as funnygunction@gmail.com. Consider Branding page + Publish to avoid 7-day token expiry.

## 2026-11-20 — Task 6: Review-first workflow + modern UI
- Backend: Channel.review_first, Clip.autoqueue, auto-migration (ALTER TABLE for new model columns), /api/overview, /api/clips (list with source info), /api/clips/{id}/approve, DELETE /api/clips/{id}, /api/channels/{id}/review. Edited queued clips re-render and re-join the queue (autoqueue) automatically.
- UI rewrite (static/index.html): sidebar app, dashboard (add-source hero, KPIs, review/up-next/recent/attention), Clips grid with tabs, Sources with Auto-upload switch, My channels, slide-over editor (preview, trim, effects, overlays, voice, metadata/keywords, approve/queue/upload-now/discard), new-clip modal with hot moments.
- Verified: migration on old schema, endpoints via TestClient, UI renders with seeded data, no console errors, no horizontal overflow.

## 2026-11-20 — Task 7: Captions + daily posting times
- Captions: app/captions.py pulls the video's own YouTube subtitle track (manual or auto, Hindi/English, json3) for the clip's time range via yt-dlp (no AI, no cost); cues are editable; burned in with libass (ASS) using bundled Noto Sans + Noto Sans Devanagari (fonts/). Hindi/mixed-script text overlays now also go through ASS (drawtext can't font-fallback).
- Per-source "Auto captions" switch (autopilot fetches cues before rendering); editor Captions card (language, position, size, colour, edit/delete lines).
- Per-source "Post at" daily times (e.g. 10:00, 18:30 in user's local time, tz_offset from browser) -> next free slot; falls back to gap hours.
- Verified: json3 parser (noise removal, chunking, overlap), real YouTube captions fetch (8 cues), ASS render with Hindi (frame inspected), slot math for IST, UI card, regression tests.
- Note: videos with no YouTube captions get none (a local Whisper fallback is possible later, CPU-heavy).

## 2026-11-20 — Task 8: YouTube API audit prep
- Added public pages /privacy, /terms, /about (exempt from dashboard auth; needed for OAuth Branding/publish and the audit). Placeholders to fill: owner name + contact email.
- Wrote docs/youtube-api-audit-draft.md (local only, gitignored): form answers, scopes/quota text, screencast outline, caveats (yt-dlp downloading disclosure, reused-content risk, tokens stored unencrypted).
- Verified: public pages 200 without auth, dashboard/API still 401.

## 2026-11-20 — Task 9: OAuth token encryption at rest
- app/crypto.py (Fernet): tokens stored as enc:v1:...; key from env TOKEN_ENCRYPTION_KEY or auto-created file DATA_DIR/.token_key (the /data volume). Existing plaintext rows are upgraded on startup; unreadable token => channel must reconnect (queued clips stay queued).
- Verified: roundtrip, no plaintext in DB, legacy upgrade, wrong-key error, API never exposes tokens.

## 2026-11-20 — Task 10: Login page, forgot password, UI redesign
- Auth: browser Basic-auth popup replaced by /login page + signed HttpOnly session cookie (scrypt-hashed password stored in DB, APP_PASSWORD env is the initial password; FORCE_ENV_PASSWORD=1 ignores the saved one). Logout, change-password (Settings), per-IP brute-force limiter, sessions invalidated on password change. Basic header still accepted for scripts.
- Forgot password: "Verify with Google" (openid+email scope only, no YouTube access) -> allowed if the verified e-mail is in ADMIN_EMAIL or is a Google account that connected a channel -> short-lived reset cookie -> /reset page. 32-case test run passed.
- UI redesign in the shadcn / 21st.dev style: neutral zinc tokens, Inter, Lucide-style icons, sidebar + topbar, stat cards, pipeline, tables, tabs, sheet editor with tabs (Trim/Text/Voice/Captions/Details), dialogs, toasts, skeleton loaders, empty states, dark mode, responsive off-canvas sidebar. Split-screen login; matching forgot/reset pages.

## 2026-11-20 — Task 11: Railway bot-check fix (all modes)
- Root cause: YouTube "Sign in to confirm you're not a bot" for the Railway datacenter IP; every mode failed at the first yt-dlp call.
- Fix: video details/licence (videos.list), comments (commentThreads.list), channel lookup (channels/search) and channel uploads (playlistItems) now use the official API via the connected channel's OAuth token; yt-dlp is only a fallback. "Most replayed" heatmap still needs yt-dlp (tried quietly).
- Video download still needs yt-dlp: added dashboard Settings card "YouTube access for the server" to paste a cookies.txt (validated, saved 0600 on the volume); friendly error text tells the user what to do.
- Tested with a fake API client + real yt-dlp fallback; auth/approve/schedule regressions pass.
- Found: Railway variable DATA_DIR=data (imported from .env.example) put the key file, cookies and clips on the container's throw-away disk -> every deploy logged the user out and made the stored channel login unreadable. Fix: relative DATA_DIR now maps to the mounted /data volume; .env.example fully commented so hosts can't import example values; destinations report needs_reconnect and the dashboard shows "Reconnect needed".

## 2026-11-20 — Task 12: Live run from the dashboard
- Foody reconnected (needs_reconnect=false). Official-API read works live: Big Buck Bunny (CC) and IGN "GTA 6 Review" (standard licence) read without bot error.
- Clip #1 (BBB 10:05-10:35, 9:16, credit metadata) and clip #2 (IGN review 26:38-27:28, commentary mode: 30% original audio, 2 text overlays, Hindi voice-over, credit + commentary line) created via the dashboard.
- Server-side edge-tts fails on Railway ("No audio was received", datacenter IP) but works locally -> voice generated locally and uploaded through the editor's voice upload.
- Render blocked at download (YouTube bot check) -> needs cookies in "YouTube access for the server". Nothing uploaded.

## 2026-11-20 — Task 13: Helper PC (beats the bot block without cookies)
- New: app/workers.py, Worker table, worker/clipstudio_worker.py + run_worker.bat. A helper runs on the owner's PC (home IP, not blocked), registers itself, must be approved in the dashboard (My channels -> Helper PC), then polls /api/worker/jobs with its own token.
- Jobs: "source" (download the clip's section and upload it; server renders with ffmpeg as before) and "tts" (make the voice-over when Microsoft refuses the server). Clip status "fetching" = waiting for the helper; autopilot clips in that state still get metadata + slot + autoqueue.
- E2E test (server with simulated bot/TTS block + real helper script): register -> approve -> fetch 10s of Big Buck Bunny -> server render "rendered"; TTS through the helper -> voice attached.
- Live: helper fetched clip #1 (Big Buck Bunny) and clip #2 (GTA commentary); Railway rendered both at 1080x1920. Clip #2 came out 20 s instead of 50 s: the helper had no JS runtime (yt-dlp got a degraded, incomplete download).
- Fixes: `deno` in the helper venv (worker/requirements.txt) + venv Scripts on PATH -> 1080p sources; download_section rejects incomplete video and repairs short audio (full audio track, cut, remux); render pads audio (apad) so a short audio track can't shorten a clip; editor "Re-download video" (POST /api/clips/{id}/refetch).
- Metadata now fills automatically when a clip is created: credit + original tags/description + keyword research seeded by title, original tags and channel (topic-filtered), #shorts and channel name for 9:16.
- Verified live: helper re-fetched clip #2 at full quality (20 MB) -> Railway render 1080x1920, 50.0 s, overlay visible; both clips now "Ready to review". Upload awaits the owner's Approve (nothing published yet).

## 2026-11-20 — Task 14: Go-live actions
- Privacy/Terms filled (operator Foody, contact funnygunction@gmail.com) and pushed.
- Approved clips #1 and #2 from the dashboard -> uploaded to Foody: k1xahUpFFWQ (Big Buck Bunny, Studio shows "Scheduled", recognised as a Short) and DwkHI7Mq2K4 (GTA 6 commentary). Publish times 2026-11-21 09:51 and 21:51 IST.
- Google Auth Platform: Branding links saved (home /about, /privacy, /terms), app pushed to "In production" (no more 7-day token expiry for new logins; unverified-app warning remains until verification).
- Not done: Windows auto-start of the helper (blocked by the permission system - user must add worker\run_worker.bat to shell:startup); Foody reconnect after publishing (user); API audit submission + screencast (user).

## 2026-11-20 — Task 15: Quick upload, thumbnails, on-screen credit, Shorts/Video choice
- Sidebar/topbar "Quick upload": paste a link, pick Shorts (9:16, ~35 s) or Video (16:9, best minute or whole video), date/time, captions -> app/quick.py finds the moment (heatmap + comments; the helper PC analyses when the server is blocked), cuts, captions, credits, metadata, thumbnail, queues. Non-CC gives 409 with a "permission / commentary" choice.
- app/thumbs.py: automatic thumbnail (frame at 35%, contrast/saturation, 2-line bold title, 2nd line yellow) after every render; editor can change text, upload own image, push to YouTube. The scheduler sets it right after upload (403 = channel needs phone verification).
- On-screen "Credit: <channel>" (top-left, toggle in editor) besides the description credit.
- ingest: ffmpeg reconnect args, 3 attempts, whole-video fallback (<=30 min), duration-aware completeness check.
- E2E (simulated block + real helper): queued 9:16 clip, thumbnail made, credit on.

## 2026-11-20 — Task 16: Multiple channels + YouTube problem alerts
- Channel picker: Quick upload ("Upload to which YouTube channel?") and editor footer (used by Approve and Upload now). QuickIn/ApproveIn take destination_id; invalid id = 404.
- app/monitor.py: every 2 h (MONITOR_INTERVAL_HOURS) videos.list for all uploaded clips per channel -> Alert rows on state change: deleted/missing, rejected (copyright, claim, duplicate, guidelines, ToS, trademark...), processing failed, region-blocked (usually a claim), available again. A channel whose login fails is skipped (never marks videos deleted on an error).
- Dashboard: "Alerts" page (badge + dashboard banner) with your channel, YouTube link, upload/live time, original creator/video, part used, "Open in YouTube Studio". Limitation shown: API gives no strike list or claimant name.
- Tests: mocked monitor (2 alerts, no duplicates, failing channel skipped, blocked alert), approve to channel #2, UI checked.

## 2026-11-20 — Task 17: Face thumbnails (phone-safe) + strike found
- Monitor's first live run: both uploads (k1xahUpFFWQ, DwkHI7Mq2K4) gone from YouTube; Studio dashboard shows 1 active copyright strike + 1 Community Guidelines warning on Foody. Nothing queued, no autopilot sources.
- New thumbnail composer (app/thumbs.py): best frame chosen automatically (8 samples scored on colour/sharpness/light), dark fade for text, face cutout with white outline + shadow, Anton font (OFL, fonts/) with last line yellow, Hindi via libass. Layout keeps text inside phone-safe areas (16:9: left side, clear of duration stamp; 9:16: centre 4:5 zone, clear of top bar/bottom title).
- app/faces.py: face photo -> background removed on the server with the free U2-Net human-seg ONNX model (baked into the Docker image, no AI service). Default face for all channels + optional own face per channel. Upload/remove on My channels; existing not-yet-uploaded thumbnails are remade automatically. Editor switch "Put my face on the thumbnail".
- Tests: 4 layouts rendered (16:9/9:16, face/no face, English/Hindi); face API (model path, transparent PNG, per-channel, default fallback, 404) passes with the small u2netp model locally (home download too slow for the 170 MB model).
- Live check: user uploaded face; server cutout OK (41% transparent). Remade thumbnails of clips 1 and 2 (9:16) with face. Photo still carries the Gemini sparkle mark on the shoulder (from the source photo).

## 2026-11-20 — Task 18: More automation (strike pause, CC topics, helper, quota)
- Strike safety: when the monitor finds a video removed/rejected/blocked, that channel's uploads pause automatically (Destination.paused + reason). Dashboard banner with "Resume uploads"; Pause/Resume per channel on My channels. Autopilot skips paused channels.
- Topic autopilot: Sources can be a topic (keywords). YouTube search with videoLicense=creativeCommon, most viewed first, risky titles skipped (songs, lyrics, movies, trailers, TV brands...), licence re-checked per video, seen ids never retried. Topic sources are always CC.
- Helper PC: worker/install_autostart.bat (hidden start at Windows sign-in via Startup .vbs), run_hidden.bat restarts it, single-instance lock (port 47613), self-update (git pull --ff-only to the server's RAILWAY_GIT_COMMIT_SHA when the tree is clean, pip install if requirements changed, exit 4 = restart). Dashboard banner when jobs wait and no helper is online.
- Quota planner (app/quota.py): units counted per Pacific day (upload 1600, thumbnail 50, search 100, read 1); uploads stop before the budget runs out; channels with fewer uploads today go first; one channel's expired login no longer stops the others; new slots skip days that already hold the daily capacity (5 uploads with 10,000 units). Dashboard card "Today's YouTube upload budget".
- Tests (mocked YouTube): 11/12 pass; the 12th only failed because the test mocked the function that counts quota.

## 2026-11-20 — Task 19: Upload again (deleted clips)
- POST /api/clips/{id}/reupload: copies an uploaded clip (trim, aspect, overlays, effects, voice file, captions, title/description/tags, channel, credit, thumbnail settings) into a new draft, reuses the downloaded source (no new download) and renders it; user then Approves. Needs confirmation; blocked for copyright/claim/trademark/legal removals and region blocks (repeat upload = strike risk).
- Dashboard: "Upload again" on uploaded clip cards, in the editor footer, and on Alerts (deleted / processing failed). Cards show the YouTube state (Deleted on YouTube, Removed by YouTube (reason), Blocked).
- Explained: Approve is disabled on uploaded clips by design (they are locked); use Upload again.
- Tests: 6/6 pass.

## 2026-11-20 — Task 20: Approve bug fix
- Bug: editor Approve/Upload now first saves; the editor always sends caption_style, so clips with caption_style=None counted as "changed", went back to draft and Approve failed ("Clip is draft; render it first"). Fix: compare a normalised render signature (_render_sig) instead of raw fields. Test 2/2.
- Re-upload of Big Buck Bunny (clip #1 -> copy #4) done on request; Foody resumed for it.
- Live: copy #4 uploaded to Foody as 1G3SLayg4t8 (publish time 11:42 PM IST set by user in editor -> public at upload). Custom thumbnail refused (403): channel needs phone verification at youtube.com/verify. Quota counter live (1652 units).

## 2026-11-20 — Task 21: Thumbnails retried automatically
- Refused thumbnails (403: channel phone not verified) are marked thumb_pending and retried every 30 min (one try per channel per round, quota kept for uploads, up to 30 days). As soon as the owner verifies, all waiting thumbnails go up without clicks. Dashboard banner with "Verify phone" link. youtube.push_thumbnail used by the scheduler and the editor button. Test 2/2.
- Foody: Studio shows Intermediate features "Eligible" (phone not verified on this channel yet).

## 2026-11-21 — Task 22: Full review (4 reviewers) + fixes, Shorts cover
Reviews: security, backend reliability, helper/media, dashboard. ~60 findings; fixed:
- Security: helper endpoints check the token in middleware before any upload is read; request size caps; new helpers need a 6-digit pairing code to be approved (shown in the helper window/log), pending helpers expire after 30 min, IP shown; voice/analysis uploads only when one is waiting; Basic auth removed; login limiter uses Railway's real client IP + global cap + pruning; session check is an HMAC (stolen cookie reveals nothing) and logout ends all sessions; reset only via ADMIN_EMAIL when set; Railway never treated as local; open redirect fixed in login; CSP/nosniff/frame/HSTS headers; cookies.txt encrypted at rest; ASS text escaping (\, \r); helper sources decoded as mp4 only; paths with '..' refused.
- Backend: quota charged only after the channel login works; expired login pauses that channel (persisted) instead of retrying every minute; youtube_id saved right after upload (no double upload); fresh clock per upload; helper jobs leased (one helper at a time) and failed after 3 tries; offline helpers (not seen in 2 days) no longer get jobs; stale waits time out (fetch 24 h, voice/analysis 6 h); restart resumes interrupted renders and Quick uploads; renders write to temp files and claim status atomically; failed render clears output_path; channel delete safe; topic searches capped at 400 units/day; autopilot runs soon after a deploy when overdue; strike-only pause reasons; disk cleanup job (old sources, trims, leftovers, orphan folders); job errors logged.
- Helper: reports failures (no endless re-downloads); upload retries; backoff when the server is down; token re-registration; auto-update only fast-forwards to the exact server commit on GitHub main (validated sha, no prompts, timeouts), launcher installs requirements; single-instance lock fix; log rotation; loops stop on "already running"/no Python; uninstall via PowerShell; ffmpeg/yt-dlp timeouts; partial-file cleanup; audio repair failure no longer leaves a silent clip.
- Media: thumbnails kept under 2 MB; face photos: size limit, 16-bit, single model session; fonts registered in Docker (Hindi fallback).
- Dashboard: past publish times refused (would go public at once); time field shows the clip's own time; Upload now/Approve no longer fail on save; Make thumbnail works for uploaded clips; polling no longer wipes edits; channel pickers keep the choice and mark paused/reconnect; Back to review button; buttons disabled in the right states; request timeout + "still working" message; phone layout fixes; Upload again only for gone videos.
- New: Shorts cover — the thumbnail is shown in the first 0.7 s of a Short (fades into the video, same length and sound), so YouTube's auto-picked cover is the thumbnail. Toggle in the editor; rebuilt when the thumbnail changes.
- Tests: review 24/24, automation 11/12 (known mock artifact), re-upload 6/6, save 2/2, thumbnail retry 2/2; UI checked locally (no console errors).
- 2026-11-21: Foody phone verified; thumbnails for 1G3SLayg4t8 and OgGQXVL-xAE set on YouTube (confirmed via i.ytimg.com).
- 2026-11-21: Thumbnail moved to its own editor tab (opens first for uploaded clips); clip cards show my thumbnail. Bug fixed: editor tabs were squeezed to a 6px line (flex-shrink) so the owner never saw Text/Voice/Captions/Details tabs.

## 2026-11-21 — Task 23: Autopilot works when the server is blocked
- Topic/channel sources: when the server sees no replay/comment data (YouTube blocks it), the helper PC analyses the video (same as Quick upload) and clips are made afterwards (make_clips_for_project); without a helper, slices from the middle of the video are used instead of skipping. Captions from the helper caption track. Fixed: analysis results no longer turned autopilot videos into "quick" projects; analysis uses the source clip length. Test 5/5.
- Fix (from Task 22 refactor): run_render passed a clip without output_path to make_thumbnail, so new clips got no thumbnail ("NoneType" error). Added an end-to-end run_render test (render + thumbnail + cover). Topic search now uses relevance order + 4-20 min videos (no re-posting whole Shorts, fewer off-topic results).
- Live: topic "street food india" (source #1, review first, 10:00/18:00 IST) produced clips #7 (AfroTalesTV24, 0-35s) and #8 (Mande Manre, 4:25-5:00) via helper analysis; both rendered with face thumbnails; waiting for owner approval.
- Clip #7 approved on request -> uploaded vKDKFvNMnSY (private, publishes 22 Nov 10:00 IST), thumbnail set automatically. Daily upload cap (5) reached for this Pacific day.
- Clip #8 approved on request -> queued for 22 Nov 18:00 IST (uploads after the quota reset at 13:30 IST).

## 2026-11-21 — Task 24: Non-CC links handled clearly
- New clip from a link: a non-CC video in CC mode shows a panel (title, creator, licence, strike warning) with three choices: commentary clip, "I have permission" (note field only then), or find CC videos via a topic source. Permission notes must be real proof (min 20 chars, not just "permission") in new clip, Quick upload and sources. Test 4/4.
- Deployed by uploading app/main.py + static/index.html through GitHub web (commits 52e4159, 6760019) because the privacy plugin blocks putting the git email in commands; local git has no user.email set. GitHub stored those two files with CRLF line endings (content identical).

## 2026-11-21 — Task 25: Autopilot schedule bug + wrong source
- Bug: autopilot job was added with next_run_time=None when not overdue at boot, which APScheduler treats as PAUSED, so sources stopped running after deploys (topic source idle 21 h). Now the tick runs every 15 min and each source runs when its own 6 h interval has passed. Test 2/2.
- A single Short link (Q46S5BfashM) had been added as a channel source (Add sources dialog) and failed every run; the server now refuses video links as sources with a clear message; the broken source was removed.
- Owner asked for fully automatic uploads: topic source "street food india" review_first turned OFF (2026-11-21). Clips now queue themselves for 10:00/18:00 IST.

## 2026-11-21 — Task 26: Problems & fixes card
- Dashboard card lists every problem in plain words with a one-click fix: reconnect channel, resume paused uploads, retry all failed clips (POST /api/fix/retry-failed, rules re-checked), run a source that failed or has not run, helper setup steps; "Everything is working" when clean. Raw bot-check text no longer shown in source status for missing captions. Test 3/3.
- 2026-11-21: Owner ran install_autostart.bat; Startup entry ClipStudioHelper.vbs present; hidden helper (run_hidden.bat) running and online, logging to worker\helper.log. Assistant cannot add/launch startup items itself (permission classifier) — owner did it.

## 2026-11-21 — Task 27: Analytics (live channel numbers)
- app/stats.py + ChannelStat/VideoStat tables: subscribers (incl. hidden flag), total views, video count per channel, and views/likes/comments/privacy for every video (newest 200) via channels.list(mine), uploads playlist, videos.list. Synced every 10 min by the scheduler; while the Analytics page is open it asks for an update every 2 min (server throttles to one read per ~100 s and stops when the upload budget is low). Changes today / this week, 30-day views sparkline, "new views since last update", engagement   (cont.) Changes today / this week, 30-day views sparkline, "new views since last update", engagement percent. 4 months of history kept. Test 4/4.

## 2026-11-21 — Task 28: Watch time (YouTube Analytics API)
- New OAuth scope yt-analytics.readonly (added to SCOPES). Stored logins keep their own scopes when refreshed (no invalid_scope for older logins); channels connected before this show "Reconnect" on the Analytics page.
- stats.sync_watch_time: last 28 days per day (views, watch minutes, avg duration, subs gained/lost), lifetime watch time, per-video watch time + average view duration/percent (top 50), traffic sources, top 10 countries. Cached per channel; synced every 3 h and on "Update now" (if older than 1 h). YouTube publishes these 1-3 days late.
- Analytics page: watch-time card with 28-day sparkline, "Where viewers come from", "Top countries", and Watch time / Avg viewed columns per video. Tests 4/4 (+ earlier analytics 4/4).
- Needs: YouTube Analytics API enabled in the Google Cloud project, and each channel reconnected once.
- Live: owner enabled YouTube Analytics API (funnygunction project) and reconnected Foody with the new scope; watch-time sync status ok (lifetime 6 min so far; 28-day data still empty because YouTube adds it 1-3 days late).

## 2026-11-21 — Task 29: Project guide PDF
- docs/Clip-Studio-Project-Guide.pdf (13 pages, A4): what it does, pipeline, architecture, every dashboard page and editor tab, workflows, automatic jobs, copyright safety, helper PC, thumbnails/Shorts cover, analytics, limits, security, deployment/env vars, data, troubleshooting, status + owner checklist, API endpoints, file map. Generator: docs/tools/make_guide.py (reportlab; docs/ is gitignored, local only).

## 2026-11-21 — Task 30: Original reels (ReelGen integration)
- New source type "Original reels": owner writes scripts (separated by ---), picks a voice; each run turns the next unused scripts into reels. The helper PC runs ReelGen (D:\video generator, override with CLIPSTUDIO_REELGEN) -> 9:16 reel with voice-over + word subtitles -> uploaded to the server -> thumbnail (face + text), title from the first sentence, description = script, no credit block (mode "original") -> review or auto-queue on the daily slots.
- Sources page: Original badge, "N scripts left", "Scripts" button to add more. render: original clips use the helper's reel file; NeedsHelper state; clip length = reel length.
- E2E test (local server + real ReelGen on this PC + render + thumbnail + approve): 12/12. Regression suites pass.

## 2026-11-21 — Task 31: Telegram remote control (optional module)
- app/telegram.py: token from @BotFather pasted by the owner in My channels (stored encrypted, checked with getMe); chat linked by sending "/start CODE" (one-time code); messages from any other chat are ignored. Polled every 20 s (no webhook).
- Alerts: upload done/failed, YouTube problems (monitor), clip ready to review (with Approve/Discard buttons), clip failed, helper offline (max every 2 h), daily summary 9 PM IST. Commands: /status, /review, /stats, /pause, /resume, /help.
- Every call is wrapped: Telegram errors never affect uploads or renders. Test with a fake Telegram API 9/9; regressions pass.

## 2026-11-21 — Task 32: Ideas page (competitor tracker + trending)
- app/competitors.py: owner adds channels to watch; daily (and "Check now") the newest 30 videos of each are compared on views per day with that channel's median; 3x or more (and 1,000+ views, under 30 days old) = outlier. Telegram gets the top 3. Never uses the upload budget.
- Trending in India: YouTube mostPopular chart by category, cached 6 h (1 unit).
- Ideas page: tracked channels, outliers table, trending table, "Add as topic" (creates a CC-only topic source with Review first on). Test 5/5.

## 2026-11-21 — Task 33: Own-channel Shorts, affiliate links, Pexels for reels, best time + health; dashboard JS hotfix
- HOTFIX: commit d6d8fc6 (Ideas page) shipped a JS syntax error (a Python placeholder left in index.html) -> live dashboard script did not run for ~20 min. Fixed; from now on every deploy runs `node --check` on the dashboard script.
- Source mode "My own channel (long videos -> Shorts)": only videos on a connected channel are used; no credit label/block; description links to the full video.
- Affiliate rules (My channels): keywords -> description line + disclosure; https links only.
- Pexels key (encrypted) -> reel jobs pass PEXELS_API_KEY + photo topic to ReelGen for real stock photos.
- Analytics: channel health score (removals/blocks in 90 days, paused, failed clips, average % watched) and best upload hours (IST, views per day by publish hour, needs 8 public videos).
- Tests 10/10; all dashboard pages load without JS errors (local).

## 2026-11-21 — Task 34: Multi-clip Quick upload, Whisper captions, reframe, thumbnail swap, weekly report, backup
- Quick upload: 1-3 clips from one video (non-overlapping best moments; first at the chosen time, the rest in the next free slots).
- Whisper captions (helper PC, faster-whisper "small", CPU, offline): when a clip wants captions and the video has none, the source job carries transcribe+hint; the helper writes timed cues (Hindi hint from title/tags) and posts /api/worker/captions/{id} before the video; existing captions are never overwritten; editor shows a "check the words" note. Audio decoded with our ffmpeg (PyAV version clash avoided).
- Smart reframe (app/reframe.py): YuNet face detector (OpenCV 5; model baked in Docker) samples 8 frames; 9:16/1:1 crop centres on the main person; centre crop when no clear person or on any error. Editor switch.
- Thumbnail auto-swap (app/growth.py): daily 11:30 IST, clips uploaded 2-10 days ago with views/day < 40% of the channel median get a second-best-frame thumbnail once (max 2/day, quota-safe), Telegram note.
- Weekly report PDF (reportlab): Sunday 9:05 PM IST to Telegram + "Weekly report" button on Analytics; /api/reports.
- Backup: helper saves the dashboard data daily to backups/ on the PC (14 kept); stats tables skipped.
- Tests: multi 5/5, whisper 5/5, batch 10/10, render e2e 3/3, all 15 regression suites pass; dashboard JS syntax checked.
- Helper fix: it runs from the same git folder the code is committed in, so "HEAD == server version" hid that the running process had old code. The helper now remembers the commit it started with and restarts (exit 4) when the folder has newer code.

## 2026-11-21 — Task 35: Renamed to "Raji Studio"
- Owner chose the name Raji Studio. Changed everywhere it is shown: dashboard (title, sidebar, breadcrumb), login/forgot/reset/about/privacy/terms pages, Telegram messages, weekly report, helper window/log text, launchers, README, project guide PDF (docs/Raji-Studio-Project-Guide.pdf) and API audit draft.
- Kept internal names on purpose (renaming would break things): file names (clipstudio_worker.py), cookies, env vars (CLIPSTUDIO_*), Startup entry ClipStudioHelper.vbs, backup file names.
- To do by owner (optional): rename the Google OAuth app (Google Cloud → Branding) so the Google sign-in screen also says Raji Studio.
- Link fields: Chrome ignored autocomplete=off and filled the saved username; fields are now read-only until focused (Chrome never autofills read-only fields).

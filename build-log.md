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

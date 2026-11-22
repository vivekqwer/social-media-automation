# Project Memory — Clip Studio (YouTube clip + upload automation)

## Goal
Dashboard: user adds many source channels (name/@handle/URL) + connects own channel(s). System finds videos, picks viral moments, clips, edits, credits original, copies tags/description, adds researched keywords, uploads on schedule. Runs on Railway, no paid AI.

## User
- Writes Hinglish. Org rule: short precise replies; save context here + build log.
- GitHub repo: https://github.com/vivekqwer/social-media-automation (public). Railway account connected by user.

## Decisions / Constraints
- Modes: cc / permission / commentary (all supported). Autopilot: cc + permission only (commentary needs manual text/voice).
- Credit != licence; reused-content risk explained to user.
- YouTube API: unaudited projects upload private-locked; videos.insert = 1600 units, ~6/day.
- Heatmap via yt-dlp metadata; comment timestamps via yt-dlp (or API key).
- yt-dlp on Railway IP may be bot-blocked -> YT_COOKIES_FILE / YT_PROXY.
- Section download only (download_ranges), not full video. Needs ffmpeg on PATH (Dockerfile installs; local uses imageio-ffmpeg alias in data/bin).
- Multi destination: Destination table (OAuth token per channel); Channel.destination_id, Clip.destination_id.
- No migrations: schema change => fresh DB.

## Stack
FastAPI, SQLAlchemy (SQLite/Postgres), APScheduler, yt-dlp, ffmpeg, edge-tts, google-api-python-client. Static vanilla-JS dashboard. Dockerfile + railway.json.

## Layout
app/{main,config,db,ingest,hot,render,tts,keywords,metadata,youtube,policy,autopilot,scheduler,jobs}.py, static/index.html

## FAQ
- Multiple sources? Yes, one per line in dashboard.
- Paid AI needed? No.
- Test gotchas: JS in index.html — avoid writing '\n' via python string replace (broke once).

## Browser
- claude-in-chrome: user's GitHub (vivekqwer, admin) + Railway login are in the Chrome extension named "Browser 4" (deviceId 411776ae-6568-44cf-b4ad-06636a82af53, onThisComputer). Use select_browser with it; never act (create/deploy/enter secrets) without user ask. Credentials/secrets (APP_PASSWORD, Google client secret) must be typed by the user.

- Upload-target YouTube account = user's own "funny" account: funnygunction@gmail.com, channel "Foody", logged in the Chrome extension named "Browser 1" (deviceId 7abd1242-b83a-4844-8447-95466ade7d22, Windows, this computer). Railway/GitHub live in the other browser (411776ae). Device names/IDs change on reconnect: re-run list_connected_browsers.
- RULE: assistant must not type passwords/API keys/secrets into any field even if the user asks (APP_PASSWORD, GOOGLE_CLIENT_SECRET). Prepare everything else; user pastes.
- Lesson: Railway auto-adds "suggested variables" from .env.example on deploy (APP_PASSWORD=change-me went live!). Keep placeholders non-functional in .env.example.

## Railway (deployed 2026-11-20)
- Project `responsible-charisma` (id 7db63f26-8956-4fb5-a98b-3654cd65d070), env production. Services: app (GitHub repo, Dockerfile build, auto-deploy on push) + Postgres (volume) + app volume at /data.
- URL: https://social-media-automation-production-f5543fa3.up.railway.app (BASE_URL set; DATABASE_URL = ${{Postgres.DATABASE_URL}}).
- /healthz = 200. APP_PASSWORD currently deleted on purpose: app returns 503 (fail-closed) until the user sets a real one. (Was briefly admin/change-me; removed + redeployed, verified 503.)
- Still needed from user: APP_PASSWORD, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET (Google OAuth redirect: BASE_URL/auth/callback; consent screen In production).

## Product workflow (user's wish, 2026-11-20)
- User gives channel link(s) -> system cuts clips automatically -> uploads to user's own account (Foody). Per-source switch: Auto upload (default) or Review first. If user edits a clip, only that clip re-renders and returns to the queue (autoqueue). New features wanted next: captions, title templates, notifications (ideas listed in chat).

## Captions & scheduling (2026-11-20)
- Captions = YouTube's own subtitle tracks (free, no AI). No captions on a video -> none (Whisper fallback not built). Bundled fonts in fonts/ (Noto Sans, Noto Sans Devanagari) are needed in the Docker image.
- Scheduling: per-clip date/time picker in editor; per-source daily "Post at" times; uploads happen up to 48h early as private with publishAt. WARNING: unaudited API projects stay private-locked until YouTube API compliance audit -> scheduled public release won't happen until audit passes.

## Login (2026-11-20)
- Dashboard login = /login page (cookie session). Initial password = APP_PASSWORD env (set by user in Railway). Forgot password = Google verification; set ADMIN_EMAIL=funnygunction@gmail.com in Railway so it works before any channel is connected. Escape hatch: FORCE_ENV_PASSWORD=1 + new APP_PASSWORD.
- UI follows shadcn/21st.dev style (user asked for polished, modern UI). Keep that look for new pages.

## Railway gotchas (2026-11-20)
- Railway imports uncommented .env.example values as variables (APP_PASSWORD=change-me went live; DATA_DIR=data made /app/data ephemeral). .env.example is now fully commented; config maps relative DATA_DIR to /data when the volume exists.
- YouTube bot-checks Railway's IP: metadata/comments/channel lists use the official API via the connected channel's OAuth token; video download still needs cookies (dashboard: My channels -> YouTube access for the server) or a proxy.
- Railway UI: typing when no input has focus triggers shortcuts (created stray 'function-bun' services twice; discarded). Always confirm focus before typing there.

## Helper PC (2026-11-20)
- Railway is bot-blocked by YouTube (downloads) and Microsoft TTS. Solution in place: worker/run_worker.bat on the user's PC (approve once in dashboard). Server marks clips 'fetching'; helper downloads the section / makes voice; server renders + uploads. Cookies option still exists as an alternative.

## Live state (2026-11-20 evening)
- First uploads done (k1xahUpFFWQ, DwkHI7Mq2K4). OAuth app "In production" since 2026-11-20; Foody should be reconnected once so its token is a production one.
- Helper auto-start installed by owner 2026-11-21 (StartupClipStudioHelper.vbs -> worker
un_hidden.bat, log workerhelper.log).

## Status (2026-11-20)
MVP built, 4-agent reviewed, fixes applied, tested locally (ranking, ffmpeg render, CC gate, live yt-dlp, channel resolve/list, section download, dashboard). NOT yet tested: real YouTube upload (needs OAuth creds), full autopilot run, Railway deploy.

## Google Cloud (2026-11-20)
- Project `clip-studio` (id striped-temple-515313-d3) under funnygunction@gmail.com (Funnyvideo). YouTube Data API v3 enabled. Consent screen: External, Testing mode, test user funnygunction@gmail.com (refresh tokens expire after 7 days in Testing; "Publish app" disabled until Branding page is completed).
- OAuth Web client "Web client 1": ID 178933708936-13mvap558vf95eccjh9a4pmpgkeq7l3i.apps.googleusercontent.com (public id). Redirect URI = Railway URL + /auth/callback. Secret hidden by Google after creation; new secret was copied to clipboard for the user to paste into Railway GOOGLE_CLIENT_SECRET (assistant never types secrets).
- Railway: GOOGLE_CLIENT_ID staged by assistant; GOOGLE_CLIENT_SECRET + APP_PASSWORD to be added by user; then Deploy.

## Multi-channel + alerts (2026-11-20)
- More YouTube channels: My channels -> "Connect a YouTube channel" once per channel (each Google account signs in). Quick upload, editor and sources each have a channel picker.
- Alerts page = monitor of uploaded videos (deleted / copyright-rejected / blocked). YouTube API cannot list strikes or claimant names; the alert links to YouTube Studio's copyright page.
- FAQ answered: multi-audio tracks not possible via API; credit reduces nothing legally — CC or permission is what avoids strikes.

## Thumbnails with face (2026-11-20)
- User wants every thumbnail: video frame + their face + bold text, phone-safe, fully automatic, no external AI. Face is uploaded once in My channels (default for all channels; a channel may have its own). Model: U2-Net human-seg ONNX baked into Docker image.
- Foody has 1 copyright strike + 1 CG warning (2026-11-20) after the IGN/GTA commentary + BBB uploads. 3 strikes = channel terminated: only CC or permission content from now on.

## Automation set (2026-11-20)
- Channel auto-pauses on removed/rejected/blocked alerts; user resumes from dashboard. Topic sources = CC-only search. Helper: install_autostart.bat (user runs once; assistant must not add startup entries itself). Quota: ~5 uploads/day across ALL channels (one Google project).

- Foody phone verified 2026-11-21: custom thumbnails work.

## Review + hardening (2026-11-21)
- New helper PCs need the 6-digit pairing code (shown in helper window / worker\helper.log) when pressing Approve.
- Shorts: YouTube ignores API thumbnails in the Shorts feed; the system now shows the thumbnail in the first 0.7 s of each new Short so the auto cover is the thumbnail. Already-uploaded Shorts can't be changed (YouTube doesn't allow replacing a video) — pick the cover in the YouTube app instead.
- Past publish times are refused; "Upload now" asks for confirmation (goes public at once).
- Basic auth removed; logout ends all sessions; ADMIN_EMAIL alone controls password reset.

- Autopilot topic source #1 "street food india" -> Foody, review first OFF since 2026-11-21 (fully automatic), posts 10:00/18:00 IST.

- Git identity: repo-local user.name vivekqwer + GitHub noreply address (set 2026-11-21); commits and pushes work again.
- Railway project is in the Chrome browser named "yaha open karo" (switch_browser); dashboard login there is not saved.

- Analytics page (2026-11-21): live subs/views/per-video stats; 2-min live updates while open, 10-min background sync. Deeper stats (watch time, traffic sources, audience) need the YouTube Analytics API scope = channel reconnect.

- Watch time live 2026-11-21 (Analytics API enabled, Foody reconnected with yt-analytics.readonly). New channels get the scope automatically when connected.

- Project guide PDF: docs/Raji-Studio-Project-Guide.pdf (regenerate with docs/tools/make_guide.py; reportlab installed only in local venv).

- Before every deploy: extract the <script> from static/index.html and run `node --check` (a broken script blanks the whole dashboard).

- Product name: **Raji Studio** (since 2026-11-21). Internal ids keep "clipstudio" (files, cookies, env vars, ClipStudioHelper.vbs).

- Permission flow (2026-11-22): owner presses Send (never auto-mailed); a plain "yes" reply from the same address auto-starts the source with Undo; conditional/unclear replies wait for the owner. Never auto-upload on silence.
- Local AI is installed by the helper itself from the dashboard (Install now); progress in checklist.
- Viral today page (app/viral.py): dated board every 2 h + 5 picks/day; CC/allowed clipped at once, others need owner's one-press "Send all ready mails"; a plain yes clips that video automatically. YouTube quota fits ~5-6 uploads/day across all sources.
- Gotcha: writing regex via bash/python heredocs can turn `` into a backspace char (0x08) — grep for $'' after patches.
- Owner rule (2026-11-22): permission mails are ALWAYS in English, signed and sent as "Funny Gunction"; never name the Foody/food channel in mails.
- Local AI = Ollama qwen2.5:3b on the helper PC (8 GB RAM, i3-7100U: no bigger model). Its Hinglish is poor, English is usable; AI scripts are checked server-side (autopilot.script_problem) and rewritten when bad. Both reel sources currently have "Review first" OFF.
- Postgres gotcha: empty JSON columns are JSON null and pass IS NOT NULL — always `x or {}`.
- 2026-11-22 (commit 655206e, live): Shorts show the whole picture on a blurred background by default (Clip.fit full|wide|fill); Voice tab has sound sync (clip = voice length, or speed-fit); Clips → "Upload my video" (src/upload.mp4, kept); several channels with a Main channel, "Move everything here", per-source channel, orphan adoption (db.main_destination / adopt_orphans); Setup guide page (app/guide.py) with how-to steps, problems, what-if answers, error details for support. Queued clips #13/#14/#17/#18 were re-rendered in the new layout.
- Next big project: sell it as a multi-user product (Super Admin, registration, locked onboarding, 1 channel by default, more after payment + approval). Plan: one private copy (schema + folder) per customer, each with their own Google Cloud keys. Phases: accounts/tenants → onboarding → Super Admin panel/limits/payments → helper installer → Razorpay.
- Reel #19 (off-topic GTA script) keeps getting approved again from the dashboard after being unscheduled; owner asked to remove it — ask before deleting permanently.
- 2026-11-22 (commit 2419088, live): MULTI-USER PHASE 1. app/tenant.py (ContextVar; DATA_DIR is a lazy TenantPath; BASE_DATA_DIR = real folder), db.SessionLocal() per customer (Postgres schema t<id> via schema_translate_map; tenant 1 = old data untouched), app/accounts.py (shared accounts table, register -> pending -> approve -> tenant_id = id+1, channel_limit default 1, upgrade request, View as, block, reset password, self_test). Owner login = Super Admin. Users page in the dashboard; /register page. Helpers: claim code -> token "t<id>.…". Live self-test on PostgreSQL passed.
- Rules that must stay true: never write tenant-specific state into process globals; background work must go through executor.submit / scheduler.for_each_customer (they carry the tenant); owner-only things (ADMIN reset e-mails, YT_COOKIES_FILE) are guarded by tenant.OWNER; the dashboard sends X-Copy and the server refuses a mismatch.
- Still to build: Phase 3 = each customer's own Google keys + locked step-wise onboarding (until then customers share the owner's Google project and its ~5-6 uploads/day); Phase 5 = helper installer/updater from the server; then Razorpay, fair render queue, per-customer disk limit.
- 2026-11-22 (commit 13e5b30, live): PHASE 3. app/onboarding.py = locked "Get started" for customers (google keys -> channel -> phone verify -> helper -> face -> gmail -> source; server-side gate `_need_step`). Customer's own Google keys in tenant settings (google_client_id / encrypted google_client_secret, verified with Google's token endpoint); `youtube.google_client()`; Super Admin can lend the server's project per customer (setting shared_google). `youtube.token_client_ok`: a customer's channel login must match the project their copy uses now. Helper ZIP from the dashboard (`/api/helper/download`, built from worker/ files -> Dockerfile must COPY worker), `worker/start_helper.bat`, `worker/helper_requirements.txt`, self-update via `/api/worker/update` for helpers without Git.
- Open risk: a brand-new Google project may get its API uploads locked Private until Google's API compliance audit. The owner's own project is NOT locked (a scheduled upload went public on 22 Nov). Dashboard reports uploads that stay Private 6 h past publish time.
- NEXT (owner asked 2026-11-22): A-to-Z access control for the Super Admin — per-user toggles for every page and action, per-user limits (channels, sources, clips/day, uploads/day, storage), direct controls (pause all uploads, stop sources, disconnect a channel, log out, delete account data), defaults/plans for new users, activity log. Must be enforced on the server and in background jobs, not only hidden in the UI.

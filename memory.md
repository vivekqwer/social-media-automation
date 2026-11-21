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
- Helper auto-start via Startup folder was refused by the permission classifier: don't retry; user adds it manually.

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

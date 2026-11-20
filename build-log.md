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

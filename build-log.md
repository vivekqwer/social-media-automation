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

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

## Status (2026-11-20)
MVP built and tested locally (ranking, ffmpeg render, CC gate, live yt-dlp, channel resolve/list, section download, dashboard). NOT yet tested: real YouTube upload (needs OAuth creds), full autopilot run, Railway deploy.

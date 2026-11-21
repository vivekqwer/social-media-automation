# Raji Studio

YouTube hot-moment clipper + editor + scheduled uploader. Add source channels once; it finds videos, picks the most-replayed/most-commented moments, clips, renders, adds credit + original tags + researched keywords, and uploads to **your** channel(s) on a schedule.

## Modes (policy gates)
| Mode | Use when | Gate |
|---|---|---|
| `cc` | Creative Commons (CC BY) videos | License must be CC, checked from YouTube |
| `permission` | Creator allowed you | Permission note required |
| `commentary` | You add own text/voice | Manual editor only; needs text or voice-over, clip <= 90s |

Credit does not equal permission. Standard-licence videos without permission risk strikes.

## Dashboard
- **My channels**: connect one or more of your channels (Google OAuth).
- **Source channels**: one per line (name / @handle / URL), each mapped to one of your channels. Autopilot runs every `AUTOPILOT_INTERVAL_HOURS` or via "Run now".
- **Manual editor**: link -> hot moments -> trim, text overlays (fade/slide), effects, voice-over (free TTS or upload) -> metadata -> schedule.
- **Queue**: status of scheduled/uploaded/failed clips.

## Run locally
```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt imageio-ffmpeg
cp .env.example .env
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

## Google OAuth (one time)
1. Google Cloud Console -> new project -> enable **YouTube Data API v3**.
2. OAuth consent screen (add yourself as test user) -> Credentials -> OAuth client ID, type **Web**.
3. Authorized redirect URI: `BASE_URL/auth/callback`.
4. Set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
5. Uploads from unaudited API projects stay **private** until you pass YouTube's API compliance audit. Default quota ~6 uploads/day.

## Deploy on Railway
1. New project -> Deploy from this GitHub repo (Dockerfile build).
2. Add **Postgres** plugin (sets `DATABASE_URL`).
3. Add a **Volume** mounted at `/data`.
4. Variables: `APP_PASSWORD` (required), `APP_USER`, `BASE_URL` (Railway public URL), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
5. If downloads fail with bot-check: set `YT_COOKIES_FILE` (cookies.txt on the volume) or `YT_PROXY`.

## Notes
- No paid AI: yt-dlp, ffmpeg, edge-tts, YouTube autocomplete. "Viral" = replay heatmap + comment timestamps + search-demand keywords; not a guarantee.
- Schema changes need a fresh DB (no migrations yet).

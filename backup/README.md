# Local backup (not used by the server)

Copies of everything that otherwise lives only on the owner's laptop, so a new PC can be set up from git alone.

- `pc-local-renderer/` — D:\youtube\tool: makes parked clips into videos on the PC (`make_local.py`), started at boot by `RajiStudioClips.vbs` (put it in shell:startup) → `start_make_local.bat`. Needs `D:\youtube\tool\bin\ffmpeg.exe` and a restored backup in `data\` (never committed: it holds tokens).
- `docs/` — Guide + Blueprint PDFs, their markdown and the PDF build scripts (D:\RajiStudio-Docs).
- `tests/` — the end-to-end test scripts (D:\rs-tests). They hard-code a repo path; point it at your checkout.
- `claude-memory/` — project notes kept by Claude Code between sessions.

Never commit: `data/`, `.env`, `token*.json`, backups, cookies.

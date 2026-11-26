---
name: local-parked-clip-renderer
description: "D:\\youtube\\tool\\make_local.py makes parked clips into finished videos on the PC (D:\\youtube\\NNN_title\\), from a backup, never touching the server"
metadata:
  node_type: memory
  type: project
  originSessionId: f1b4fc3c-890e-40cf-a8e1-1a5008835727
  modified: 2026-11-24T17:31:02.218Z
---

- Run: "D:\social medi engin\.venv\Scripts\python.exe" D:\youtube\tool\make_local.py (resumable; log tool\make_local.log).
- Reads newest D:\social medi engin\backups\clipstudio-*.json.gz into tool\data\app.db via app.backup internals (_rows/_check_links/_replace/_put_faces); imports app read-only from D:\fix-main (no pycache written).
- ids: tool\ids.txt (coordinator, 237 ids). Ids not in the backup -> tool\waiting.txt ("waiting for tomorrow's backup"), keep their number. Repeats (autopilot.same_moment, earlier list clips count as made) -> tool\skipped.txt.
- Folder: video.mp4, thumbnail.jpg, description.txt, info.json ("approve": "manual", source_channel, licence_line, suggested_upload_ist 18:00 IST from 2027-07-01, one/day by list position). Fail -> error.txt.
- ~2-3 min per 35 s Short (download ~1 min, render ~1 min, thumb+cover ~40 s). Test outputs moved to tool\test-output.
- Gaps vs server: voice-over files and custom thumbnails are not in a backup (noted in info.json notes); 'original' (ReelGen) clips fail (no source).

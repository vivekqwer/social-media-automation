---
name: remake-queued-clips-on-look-change
description: "When on-video wording/branding changes, the owner wants already-queued clips re-made too, not only future ones"
metadata:
  node_type: memory
  type: feedback
  originSessionId: f1b4fc3c-890e-40cf-a8e1-1a5008835727
  modified: 2026-11-22T15:04:39.549Z
---

When something burned into the video changes (reminder line, credit, face/title band, thumbnail), the owner expects it on every clip still waiting to upload, not just on clips made afterwards. On 2026-11-22 they asked for "Like, share and subscribe" and, when offered to leave the 45 queued clips as they were, answered that it must be in all upcoming videos.

**Why:** the owner judges by what reaches YouTube; a mix of old and new wording reads as "not done". My recommendation to skip the re-make (helper time) was overruled.

**How to apply:** after deploying such a change, re-make the queued clips without asking again: test one clip first (POST /api/clips/{id}/render, check the frame and that it returns to `queued` with the same publish time), then the rest one by one, stopping on any `failed`. Clips whose source is gone go back through the helper PC (about 2–5 min each); say how long that delays new production. Every video must carry: creator credit, the like/share/subscribe line, the owner's face, and a thumbnail made from the video frame + face.

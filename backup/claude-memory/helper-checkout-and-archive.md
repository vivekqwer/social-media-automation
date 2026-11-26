---
name: helper-checkout-and-archive
description: "Where the owner's laptop helper runs from, how to update it, and where uploaded videos are archived"
metadata:
  node_type: memory
  type: project
  originSessionId: f1b4fc3c-890e-40cf-a8e1-1a5008835727
  modified: 2026-11-24T09:23:51.150Z
---

The owner's laptop helper runs from the git checkout `D:\social medi engin` (Windows startup → worker\run_hidden.bat, restarts itself after a crash). On 2026-11-24 it was moved to a detached `origin/main` with no local edits so its own `maybe_update` can fast-forward it to whatever production runs; before that, local edits blocked every self-update for days.

**Why:** features that need new helper code (parallel downloads, saving uploaded videos to the PC) silently do nothing while the helper is stale; the server only frees disk space after the PC confirms a copy.

**How to apply:** never leave uncommitted edits in `D:\social medi engin` (work in D:\fix-main or a worktree instead). To force an update: `git -C "D:\social medi engin" fetch origin && git -C "D:\social medi engin" switch --detach origin/main`, then stop the python process running worker\clipstudio_worker.py (the .bat starts it again in ~15 s). Uploaded videos are archived to `C:\Users\hp\RajiStudioArchive` (t<tenant>-c<clip>.mp4 + .json); the server deletes its copy only after a sha256-confirmed PC copy. Live production settings are in [[six-month-unattended-goal]].

2026-11-25: after a power/internet cut during a self-update at 22:16, `.git\HEAD` and `ORIG_HEAD` in that checkout were zero-filled ("not a git repository"), so the helper silently stopped updating. Fix used: write the last good sha from `.git\logs\HEAD` into HEAD (detached), check `git status`/`fsck`, then `switch --detach origin/main` and restart the helper. If a deploy's new helper features never appear, check this first. The helper also watches `D:\youtube` (PC folder → dashboard "From my PC" → Approve); locally rendered parked clips live there (tool in `D:\youtube\tool`, see [[local-parked-renderer]]).

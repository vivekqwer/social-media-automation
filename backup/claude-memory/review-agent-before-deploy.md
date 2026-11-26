---
name: review-agent-before-deploy
description: Owner asked for one independent agent to check everything at the end of big changes; it found real bugs
metadata:
  node_type: memory
  type: feedback
  originSessionId: f1b4fc3c-890e-40cf-a8e1-1a5008835727
  modified: 2026-11-22T04:39:24.200Z
---

For large changes the owner wants several agents if useful, and ONE agent to check everything at the end ("last me ek hi agent se sab kuch check karo").

**Why:** On 2026-11-22 a fresh read-only review agent found 18 issues in a 560-line change that 80+ passing tests had missed (e.g. original sound stopping with the voice-over, disk clean-up deleting an uploaded video, a new filter skipping sources whose `kind` is NULL).

**How to apply:** before deploying a big change, run one independent review agent on the uncommitted diff (give it the diff areas and what to look for, not conclusions), fix what it finds, add regression tests, then deploy. Do not edit the reviewed files while it runs. See [[saas-multi-user-plan]].

---
name: saas-multi-user-plan
description: "Owner wants to sell Raji Studio as a multi-user product; agreed architecture, rules and phase order"
metadata:
  node_type: memory
  type: project
  originSessionId: f1b4fc3c-890e-40cf-a8e1-1a5008835727
  modified: 2026-11-22T04:39:20.158Z
---

Owner (2026-11-22) wants to sell Raji Studio: current admin becomes Super Admin; other people register, name their own system (like "Raji"), and run the whole product from the dashboard through a locked step-wise onboarding (next step opens only when the previous is done). New users get ONE YouTube channel; more channels only after they pay and the Super Admin allows it. Owner said: think ahead about every hurdle and make it very easy; decisions are left to the assistant.

**Decisions taken (owner did not object):**
- One private copy per customer inside one server: a Postgres schema + data folder per tenant, chosen by a context variable (SessionLocal / DATA_DIR become tenant-aware). The existing live data stays as tenant 1 = Super Admin, untouched.
- Each customer connects their OWN Google Cloud project (client id/secret) → own YouTube quota (one project only allows ~5-6 uploads/day in total) and no shared strike risk. Super Admin can enter keys for a customer ("done for you").
- Registration → pending until Super Admin approves; Terms tick (copyright responsibility is the user's).
- Payment: manual first (owner's UPI/QR shown, user sends request, Super Admin presses "Paid — allow"); Razorpay later (owner must paste keys himself).
- Plan limits: channels (default 1), clips/day, storage. Super Admin panel: users, limits, payment requests, usage, "View as user", reset password, block.
- Helper PC per customer: download from the dashboard, pair with a code, update from the server (not GitHub). ReelGen reels are owner-only at first.

**Status:** Phase 2 (accounts + tenant isolation + brand name + Users page + channel limit + upgrade requests + per-customer helper claim codes) is LIVE since 2026-11-22 (commit 2419088); the isolation self-test passed on the live PostgreSQL. Not built yet: per-customer Google keys, locked onboarding, helper installer, Razorpay, fair render queue.

**Phase order:** 1) deploy multi-channel/Setup guide (done 2026-11-22, commit 655206e) → 2) accounts + tenant isolation + brand name (done) → 3) register/approve + locked onboarding → 4) Super Admin panel + limits + payment requests → 5) helper installer/updater → 6) Razorpay, plans, reels bundle.

**Why:** isolation by construction avoids cross-customer data leaks from a missed filter; own Google keys avoid the quota wall and Google verification limits (100 test users, Gmail restricted scope audit).

**How to apply:** build each phase behind tests and deploy separately so the live system (tenant 1) never breaks. Possible hurdle to check with the owner: if the Google OAuth app is in "Testing", refresh tokens expire after 7 days → tell them to press "Publish app". Related: [[review-agent-before-deploy]].

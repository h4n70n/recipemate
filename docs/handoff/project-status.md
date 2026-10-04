# Project Status — 2026-10-04

Snapshot of completion state. The authoritative checkboxes live in
`.kiro/specs/recipemate/tasks.md`; this is a readable roll-up for the handoff.

## At a glance

| Phase | Area | Status |
|-------|------|--------|
| 1 | Foundation (scaffold, DB, CDK, auth) | ✅ Complete |
| 2 | Recipe CRUD, image upload, async extraction, tags | ✅ Complete |
| 3 | Search (heuristic filters + LLM) | ✅ Complete |
| 4 | Cook logs | ✅ Complete |
| 5 | iOS push notifications | ⬜ Not started |
| 6 | Web frontend (scaffold + core pages) | ✅ Complete |
| 7 | iOS app (scaffold, share extension, screens) | ⬜ Not started |
| 8 | Operational readiness (first prod deploy) | 🟡 In progress (3 of 11 tasks done) |

## Milestones

| Milestone | Tasks | State |
|-----------|-------|-------|
| M1: API Foundation | 1.1–1.4 | ✅ |
| M2: Recipe Extraction | 2.1–2.4 | ✅ |
| M3: Search | 3.1–3.2 | ✅ |
| M4: Cook Logs | 4.1 | ✅ |
| M5: Web App | 6.1–6.2 | ✅ |
| M6: iOS App | 5.1, 7.1–7.3 | ⬜ |
| M7: Deploy Foundation | 8.1–8.4 | 🟡 (8.1–8.3 done, 8.4 pending) |
| M8: Launch Hardening | 8.5–8.9 | ⬜ |
| M9: iOS Readiness | 8.10 | ⬜ |

## Phase 8 detail (first production deploy)

The hardening phase is where active work stops. Companion detail is in
`docs/operational-readiness.md`. Target region for all resources is
**us-east-2 (Ohio)**, except CloudFront ACM certs which must be in us-east-1.

| Task | Title | Status |
|------|-------|--------|
| 8.1 | Standardize region on us-east-2 | ✅ Done |
| 8.2 | Backend deploy credentials via OIDC | ✅ Done |
| 8.3 | Secrets Manager config + runtime key loading | ✅ Done (verified — see note below) |
| 8.4 | Unified CI/CD on CodePipeline | ⬜ Pending |
| 8.5 | End-to-end tests (promotion gate) | ⬜ Pending |
| 8.6 | Rate limits and throttling (WAF + per-user) | ⬜ Pending |
| 8.7 | CloudWatch alarms and alerting | ⬜ Pending |
| 8.8 | OWASP Top 10 audit | ⬜ Pending |
| 8.9 | Domain and DNS: recipemate.me (Route 53) | ⬜ Pending |
| 8.10 | iOS enablement (backend-side) | ⬜ Pending |
| 8.11 | Prompt-injection guardrails on OpenAI calls | ⬜ Pending |

### Recently landed (unpushed on `main` — see git-state.md)

- **TASK-8.1** — region standardized on us-east-2 across `infra/config.py`,
  `app/config.py`, `.env.example`, GitHub Actions, and docs.
- **TASK-8.2** — GitHub OIDC provider + least-privilege backend deploy role;
  role ARN stored as `AWS_BACKEND_DEPLOY_ROLE_ARN`. Also added the frontend
  GitHub-OIDC deploy role to `WebStack` (`WebDeployRoleArn` →
  `AWS_DEPLOY_ROLE_ARN`).
- **TASK-8.3** — Secrets Manager wiring: the ECS task definition injects
  `DATABASE_URL`, `OPENAI_API_KEY`, and `SECRET_KEY` as container secrets;
  task role `GetSecretValue` scoped to specific ARNs; app refuses to start in
  prod without `SECRET_KEY`. Verified: `cdk synth` clean on the Secrets stack,
  no secret values in any template, full `pytest` suite **303 passed**. Detail
  in `.agents/tasks/verification.md`.

## Verification evidence on hand

- `.agents/tasks/verification.md` — TASK-8.3 verification (synth + grep + 303 tests).
- `.agents/tasks/verification-note.md` — WebStack OIDC deploy role verification.
- `.agents/tasks/plan.md` — implementation plan for the WebStack deploy role.
- `.agents/tasks/review.md` / `review.json` — code review (verdict: APPROVED).

## Known gaps / caveats

- **Phase 5 and Phase 7 (iOS) are untouched.** The backend was designed to
  accommodate iOS (REST/JSON, public Cognito client, presigned uploads,
  SQS→SNS→APNs path), but `POST /devices`, the APNs SNS platform app, and the
  entire iOS client remain to be built. TASK-8.10 tracks the backend enablers.
- **No production deploy has happened yet.** Phase 8 is explicitly
  "first production deploy" hardening. `cdk deploy` to a real account has not
  been run end-to-end; the `ApiStack` synth needs real-credential AZ/VPC context
  lookup (documented in the TASK-8.3 verification note).
- **CI/CD is mid-migration.** The decision (TASK-8.4) is to retire the GitHub
  Actions `deploy-frontend.yml` in favor of a single CodePipeline for both
  backend and frontend. The `PipelineStack` does not exist yet; the GitHub
  Actions workflow is still the only working deploy path for the frontend.

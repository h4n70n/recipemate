# Next Steps

Recommended pickup sequence for kirocrew / mobie. The active frontier is
**Phase 8 (first production deploy hardening)**; the iOS app (Phases 5 & 7) is a
separate track that is not launch-blocking for web.

## Immediate orientation (first hour)

1. Read `docs/handoff/project-status.md` and `.kiro/specs/recipemate/tasks.md`
   together — the latter has the live checkboxes.
2. Get to a clean tree per `docs/handoff/git-state.md`.
3. Bring the stack up locally to confirm the environment works:
   ```bash
   cp .env.example .env        # fill in per docs/configuration.md
   docker-compose up
   docker-compose exec api alembic upgrade head
   curl http://localhost:5000/health      # expect 200
   ```
4. Run the test suite to confirm green (TASK-8.3 verification recorded 303 passing):
   `pytest` from the repo root.

## Phase 8 sequence (from tasks.md "Suggested Sequencing")

The spec already orders this work. Done items are struck through.

1. ~~TASK-8.1 (region) and TASK-8.2 (OIDC creds)~~ — ✅ foundation complete.
2. ~~TASK-8.3 (Secrets Manager)~~ ✅ → then **TASK-8.9 (domain/DNS: recipemate.me)**
   and **TASK-8.4 (CodePipeline)** — deploy to real endpoints securely.
3. **TASK-8.6 (rate limits + WAF)**, **TASK-8.7 (CloudWatch alarms)**,
   **TASK-8.8 (OWASP audit)**, **TASK-8.11 (prompt-injection guardrails)** —
   harden before taking traffic.
4. **TASK-8.5 (e2e tests)** — gates promotion once endpoints are stable.
5. **TASK-8.10 (iOS backend enablers)** — parallelizable; not launch-blocking.

### Suggested next concrete task: TASK-8.4 (Unified CI/CD on CodePipeline)

Highest leverage because it unblocks repeatable deploys for everything after it.
Decisions already locked in (do not relitigate without reason):

- **One CodePipeline** builds and deploys **both** backend and frontend; GitHub is
  source-only. The existing `.github/workflows/deploy-frontend.yml` is to be
  retired (keep at most a PR build-check with no AWS creds).
- Stages: source (GitHub via CodeStar connection on `main`) → build (backend
  `docker build` + push to the `ApiStack` ECR repo; frontend `npm ci && typecheck
  && build`) → test (`pytest`, red fails the pipeline) → backend deploy
  (`alembic upgrade head`, roll the ECS service, wait for ALB `/health`) →
  frontend deploy (`s3 sync frontend/dist` + CloudFront invalidation) → manual
  approval before prod.
- Frontend build-time `VITE_*` config moves from GitHub Actions vars to CodeBuild
  env / SSM parameters.

## Cross-cutting constraints to respect

- **Region:** everything is **us-east-2 (Ohio)**. The only exception is CloudFront
  ACM certs, which must be issued in **us-east-1** (TASK-8.9).
- **Secrets:** `DATABASE_URL`, `OPENAI_API_KEY`, `SECRET_KEY` come from Secrets
  Manager at runtime (TASK-8.3). The app refuses to start in prod without
  `SECRET_KEY`. Never commit a real secret value. Post-deploy `put-secret-value`
  steps are documented in `docs/configuration.md`.
- **No static AWS keys** in CI — OIDC roles only (`AWS_BACKEND_DEPLOY_ROLE_ARN`
  for backend, `AWS_DEPLOY_ROLE_ARN` for frontend). Static keys remain
  LocalStack-only.
- **Untrusted input:** images and recipe text are untrusted. OpenAI call sites
  must carry prompt-injection guardrails and never fetch user-supplied URLs
  server-side (TASK-8.11 / OWASP A10 in TASK-8.8).

## Open decisions to confirm with the product owner

- **iOS timing.** Phases 5 and 7 are entirely unbuilt. Decide whether iOS is in
  scope for this transfer or stays deferred. If deferred, TASK-8.10's backend
  enablers (`POST /devices`, SNS/APNs platform app, Apple prod credentials) can
  still land cheaply alongside Phase 8 to keep the door open.
- **CI/CD cutover timing.** The frontend currently deploys via GitHub Actions.
  Confirm when to actually retire `deploy-frontend.yml` so there is never a window
  with no working deploy path (keep it until the CodePipeline frontend stage is
  proven in staging).

## Where to record progress

Keep `.kiro/specs/recipemate/tasks.md` checkboxes current as the single source of
truth. Verification notes for completed tasks have been kept under
`.agents/tasks/` — follow that pattern or your team's equivalent.

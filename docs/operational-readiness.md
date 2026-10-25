# RecipeMate Operational Readiness

Pre-launch checklist for the first production deploy. Scope is the backend API
(ECS Fargate), the web client (S3 + CloudFront), and the shared AWS
infrastructure defined in `infra/`. Each task lists what exists today, the gap,
and a definition of done so it can be picked up without re-deriving context.

Target region for all RecipeMate resources: **us-east-2 (Ohio)**, with the one
documented exception that CloudFront ACM certificates must live in us-east-1.

Legend: `[ ]` not started · `[~]` partial (noted) · `[x]` done

---

## OR-1 — Region: standardize on us-east-2

Everything currently defaults to `us-east-1`.

- `app/config.py`: `AWS_REGION` and `COGNITO_REGION` default to `us-east-1`.
- `infra/config.py`: `_DEFAULT_REGION` falls back to `us-east-1`.
- `docs/configuration.md` examples and ARNs use `us-east-1`.

Tasks:
- [ ] Set `_DEFAULT_REGION` default to `us-east-2` in `infra/config.py` (keep the
      `CDK_DEFAULT_REGION` override).
- [ ] Set `AWS_REGION` / `COGNITO_REGION` prod defaults to `us-east-2`
      (`app/config.py`, `.env.example`).
- [ ] Set `vars.AWS_REGION` and `vars.VITE_COGNITO_REGION` in GitHub Actions to
      `us-east-2`.
- [ ] Update `docs/configuration.md` and `docs/architecture.md` region examples.
- [ ] Confirm the deploy account has us-east-2 enabled and ECS Fargate quota.

**Done when:** `cdk synth -c env=prod` resolves every stack to us-east-2 and no
`us-east-1` default remains outside the CloudFront-cert exception (OR-9).

---

## OR-2 — AWS credentials for backend deploy (OIDC)

The frontend workflow already assumes a role via OIDC
(`secrets.AWS_DEPLOY_ROLE_ARN`, `aws-actions/configure-aws-credentials@v4`).
There is no equivalent path for the backend/CDK or ECR image push.

Tasks:
- [ ] Create a GitHub OIDC identity provider + deploy role in the target account
      (scoped to this repo and `main`), least-privilege for CDK, ECR, and ECS.
- [ ] Store the role ARN as `AWS_BACKEND_DEPLOY_ROLE_ARN` (GitHub secret).
- [ ] No long-lived IAM user keys. Confirm `.env`/CI contain no static AWS keys
      (static keys stay LocalStack-only per `docs/configuration.md`).
- [ ] Document the bootstrap (`cdk bootstrap aws://<acct>/us-east-2`).

**Done when:** CI can assume the role, run `cdk deploy`, and push an image to ECR
with no static credentials anywhere.

---

## OR-3 — Unified CI/CD on CodePipeline

A single `PipelineStack` (`infra/stacks/pipeline_stack.py`) now owns build +
deploy for BOTH the backend (Docker image to ECR, ECS rolling deploy) and the
frontend (SPA build, S3 sync, CloudFront invalidation) from one GitHub source.
The old `deploy-frontend.yml` GitHub Actions deploy job has been retired to a
credential-free PR build-check (see "Workflow cutover" below).

Tasks:
- [x] Define a `PipelineStack` (CDK): source → build (docker build + push to the
      `ApiStack` ECR repo) → test (pytest) → deploy (ECS rolling update).
- [x] Source stage: GitHub connection (CodeStar) on `main`.
- [x] Build stage: CodeBuild runs `docker build`, tags with commit SHA + `latest`,
      pushes to ECR; migrations (`alembic upgrade head`) run in the backend
      deploy stage.
- [x] Deploy stage: update the ECS service to the new image
      (`--force-new-deployment`); wait for the service to stabilise
      (`ecs wait services-stable`) so the ALB `/health` target group is healthy
      before marking success.
- [x] Manual-approval action before the prod stage (prod pipeline only; staging
      deploys automatically).
- [x] Decide split: resolved. The frontend was folded into CodePipeline (one
      pipeline per env builds and deploys backend + frontend together).

### Stage graph as built

Per environment (`RecipeMate-<Env>-Pipeline`):

1. **Source**: CodeStar GitHub connection on `main`. The operator creates the
   connection once and passes its ARN via `-c codestarConnectionArn=<arn>`.
2. **Build** (two CodeBuild projects in parallel):
   - backend: `docker build` the repo-root Dockerfile, push the commit-SHA tag
     and `latest` to the `ApiStack` ECR repo (privileged Docker build).
   - frontend: `npm ci && npm run typecheck && npm run build`, with the `VITE_*`
     values sourced from SSM Parameter Store (see `docs/configuration.md`); emits
     `frontend/dist` as the deploy artifact.
3. **Test**: a CodeBuild project running `pytest`. A red suite fails the stage
   and halts the pipeline before any deploy.
4. **Approval** (PROD ONLY): a manual-approval action gating the deploys.
5. **DeployBackend**: `alembic upgrade head` (DATABASE_URL composed at runtime
   from the DatabaseStack secret), roll the ECS service, wait for stability.
6. **DeployFrontend**: `aws s3 sync frontend/dist` (`--delete`) into the WebStack
   bucket, then a CloudFront invalidation on the WebStack distribution.

### Workflow cutover (`deploy-frontend.yml`)

`.github/workflows/deploy-frontend.yml` is now a credential-free PR build-check
(`npm ci && typecheck && build` on `pull_request`, `permissions: contents:read`
only). The deploy job that used `aws-actions/configure-aws-credentials` + OIDC +
`s3 sync` + CloudFront invalidation has been removed; the workflow no longer runs
on push to `main`. The build-check is retained so a bad merge is caught before it
reaches `main` and the CodePipeline. The operator should remove the now-unused
GitHub vars/secrets (`AWS_DEPLOY_ROLE_ARN`, `FRONTEND_BUCKET`,
`FRONTEND_DISTRIBUTION_ID`, the `VITE_*` vars) only AFTER the CodePipeline
frontend stage is proven in staging, to avoid a no-working-deploy window.

### Synth validation note

- [~] This change was validated by **single-stack synth** of the Pipeline stack
      for both envs (`cdk synth RecipeMate-<Env>-Pipeline ...`), which succeeds
      with no AWS credentials because the Pipeline stack has no VPC/AZ lookups.
      A **full-app synth** (all stacks) still requires account credentials
      because DatabaseStack/CacheStack/ApiStack perform VPC AZ lookups
      ("Need to perform AWS calls for account ... but no credentials
      configured"). This is a pre-existing baseline limitation, not caused by
      this task. Live `cdk bootstrap`/`cdk deploy` and proving the pipeline
      end-to-end in staging are deferred to the operator (no AWS creds in the
      build sandbox).

**Done when:** A merge to `main` builds, tests, pushes the image, migrates, rolls
the Fargate service, and deploys the frontend with zero manual steps (prod gated
by approval). Remaining to prove live: operator bootstrap + first deploy and the
staging run (`[~]` above).

---

## OR-4 — End-to-end tests

`tests/` has unit/integration suites (auth, recipes, search, cooks, services,
lambda) but no end-to-end suite exercising a real deployed stack.

Tasks:
- [ ] Add `tests/e2e/` running against the staging API base URL.
- [ ] Cover the critical paths: Cognito sign-in → create recipe → presigned
      upload → extract (poll `extraction-status`) → search (heuristic + LLM) →
      cook log → rating recalculation.
- [ ] Use a dedicated staging test user; clean up created records.
- [ ] Wire into the pipeline (OR-3) as a post-deploy-to-staging gate before the
      prod approval.

**Done when:** The e2e suite runs green against staging in the pipeline and
blocks promotion on failure.

---

## OR-5 — Rate limits / throttling

`docs/architecture.md` lists "API Gateway: rate limiting" but `ApiStack`'s
`HttpApi` has no throttling configured, and there is no WAF.

Tasks:
- [x] Set default route throttling (burst + rate) on the HTTP API stage. Done in
      `ApiStack` via the `$default` stage's `CfnStage.default_route_settings`
      (burst 200 / rate 100 req/s by default; context-overridable).
- [x] Add AWS WAF (managed common rules + rate-based rule) in front of
      CloudFront and/or the API — note WAF for CloudFront must be us-east-1
      scope (CLOUDFRONT), regional WAF for the API lives in us-east-2. Done:
      TWO WebACLs — a CLOUDFRONT/us-east-1 ACL on the web distribution and a
      REGIONAL/us-east-2 ACL on the HTTP API stage (see "As implemented").
- [x] Add per-user application-level throttling for the expensive LLM endpoints
      (`POST /search/llm`, `POST /recipes/extract`) using the existing Redis
      client (`app/services/redis_client.py`). Done via
      `check_rate_limit` (fixed-window, `ratelimit:` key prefix), fail-open.
- [x] Return `429` with `Retry-After` and confirm clients handle it. Done: both
      endpoints return `429` + `Retry-After` (seconds) when over the per-user
      limit; documented in `docs/api.md`. Live client-handling confirmation is
      an operator step after first deploy (see "Remaining operator actions").

**As implemented:**
- **HTTP API stage throttling.** `ApiStack` sets default-route throttling on the
  HTTP API's auto-created `$default` stage. At the pinned aws-cdk-lib 2.144.0
  `HttpApiProps` exposes no throttle option for the auto-created default stage,
  so the throttle is set on the stage's underlying L1 via
  `CfnStage.default_route_settings` (`throttling_burst_limit` /
  `throttling_rate_limit`). Defaults are a conservative **burst 200 / rate 100
  req/s**, overridable via `-c apiThrottleBurst` / `-c apiThrottleRate`.
- **Two WAF WebACLs, scope/region split (the crux).** A WebACL's scope and
  region are dictated by what it fronts:
  - **CLOUDFRONT / us-east-1** — the web client distribution. A WebACL attached
    to CloudFront MUST be `scope="CLOUDFRONT"` and physically live in us-east-1
    regardless of the us-east-2 default. It is its own stack,
    `RecipeMate-<Env>-CloudFrontWaf` (`infra/stacks/cloudfront_waf_stack.py`),
    instantiated in `infra/app.py` with `env=cdk.Environment(region="us-east-1")`
    — mirroring the OR-9 us-east-1 CloudFront cert precedent. Its WebACL ARN is
    threaded into `WebStack` as the `cloudfront_web_acl_arn` keyword arg and set
    as the distribution's `web_acl_id` (which takes the WebACL **ARN**), with an
    explicit `add_dependency` so the us-east-1 WebACL builds first. This is the
    single documented us-east-1 exception to the us-east-2 default.
  - **REGIONAL / us-east-2** — the HTTP API stage. Created inside `ApiStack` and
    associated with the `$default` stage via `CfnWebACLAssociation`, building
    the stage ARN (`arn:aws:apigateway:<region>::/apis/<apiId>/stages/<stage>`)
    from the stack region + API id + stage name, with an `add_dependency` so the
    association follows the stage.
  Both WebACLs carry the same rule shape (shared `_build_waf_rules` helper):
  `AWSManagedRulesCommonRuleSet` (managed common rule set, `override_action`
  none) plus an IP rate-based rule (`aggregate_key_type="IP"`, action block) at
  a context-driven `wafRateLimit` (default **2000** requests per source IP over
  WAF's trailing 5-minute window). Both the ACLs and every rule have a
  CloudWatch `visibility_config`.
- **App-level fail-open + WAF backstop.** The per-user Redis limiter
  (`check_rate_limit`) **fails open** on a `redis.RedisError` so a cache blip
  cannot lock legitimate users out of the LLM endpoints. The security trade-off
  is that the per-user limit is unenforced while Redis is down; the **WAF
  rate-based rule is the backstop** that still throttles abusive source IPs in
  that window.
- **Per-endpoint limit defaults (FEAT-002, `app/config.py`):**
  `POST /search/llm` → `RATE_LIMIT_LLM_SEARCH_MAX=20` per
  `RATE_LIMIT_LLM_SEARCH_WINDOW_SECONDS=60` s; `POST /recipes/extract` →
  `RATE_LIMIT_EXTRACT_MAX=10` per `RATE_LIMIT_EXTRACT_WINDOW_SECONDS=60` s. The
  extract check runs before the DB row + SQS enqueue, so a throttled request
  creates no row and sends no message; the search check runs before the OpenAI
  call so a throttled request incurs no LLM spend. See `docs/configuration.md`
  for all knobs.

**Remaining operator actions** (cannot run in this credential-free workspace):
- [~] **Confirm the WAF associations after the first deploy.** Verify (console
      or `aws wafv2 get-web-acl-for-resource`) that the CLOUDFRONT WebACL is
      attached to the distribution and the REGIONAL WebACL to the HTTP API
      `$default` stage, and that clients handle `429` + `Retry-After` correctly.
- [~] **Tune the limits post-launch with a real load test.** The throttle
      burst/rate (200/100), the WAF rate-based limit (2000), and the per-user
      limits (20/10 per 60 s) are conservative initial values; refine them once
      real traffic baselines exist via `-c apiThrottleBurst/apiThrottleRate`,
      `-c wafRateLimit`, and the `RATE_LIMIT_*` env vars.
- [~] **WAF monthly cost.** AWS WAF bills per WebACL (~$5/mo each) plus per rule
      (~$1/mo each) and per million requests; two WebACLs with two rules each add
      a small fixed monthly cost to review against the launch budget.

**Validation performed (credential-free):** `py_compile` and isolated import of
the touched/added infra stacks under `infra/.venv` (`api_stack`, `web_stack`,
`cloudfront_waf_stack`, `app.py`), proving the `wafv2` / throttling API calls
match the 2.144.0 signatures. An **isolated `cdk synth RecipeMate-Staging-CloudFrontWaf
--exclusively`** succeeded (it is lookup-free): it rendered the CLOUDFRONT-scoped
WebACL in us-east-1 with the managed common rule set + rate-based rule, and the
cross-stack ARN export consumed by `WebStack`. The full app suite stays green
(`314 passed`). `ApiStack` and `WebStack` themselves CANNOT full-synth
credential-free (VPC AZ lookups + `HostedZone.from_lookup`); that is the
documented pre-existing baseline, not a regression introduced here.

**Done when:** Load beyond the configured rate is throttled at the edge and the
LLM endpoints are protected per-user; verified with a load test. Remaining to
prove live: operator confirmation of the WAF associations and a post-launch load
test to tune the limits (`[~]` above).

---

## OR-6 — CloudWatch alarms

`ApiStack` enables Container Insights and a one-month log group but defines no
alarms and no alerting topic.

Tasks:
- [x] Create an alerting SNS topic (email/Slack subscription) distinct from the
      APNs notifications topic. Topic is created; the subscription is
      context-driven (`-c alertEmail` / `-c alertSlackWebhookUrl`) and the live
      confirm step is operator-only (see "Remaining operator actions").
- [x] ECS: alarms on service CPU/memory high, running task count < desired.
- [x] ALB: alarms on 5xx rate, unhealthy host count, target response time p95.
- [~] API Gateway: alarms on 5xx and p99 latency done. Throttle count has NO
      per-API metric on HTTP API v2; a 4xx ClientError proxy stands in (see
      "As implemented").
- [x] RDS: CPU, free storage, connection count. Replica/failover (prod multi-AZ)
      has no clean standard metric at the pin; a prod/multi-AZ-gated tighter CPU
      alarm stands in (see "As implemented").
- [x] SQS extraction: DLQ depth > 0, queue age-of-oldest-message.
- [x] Lambda extraction: error rate, throttles, duration near timeout (120s).
- [x] Redis: evictions, memory, CPU.
- [x] A CloudWatch dashboard summarizing the above.

**As implemented:**
- A dedicated central `MonitoringStack`
  (`infra/stacks/monitoring_stack.py`, wired in `infra/app.py` as
  `RecipeMate-<Env>-Monitoring` after the Api/Database/Queue/Cache stacks it
  observes) owns all alarms, one alerting SNS topic, and the dashboard. A
  central stack was chosen over inline-per-stack alarms because it matches the
  project's one-concern-per-stack style and because OR-6 calls for a single
  alerting topic and a single dashboard spanning every tier.
- The alerting topic is a **brand-new topic, DISTINCT from the APNs
  notifications topic**. The notifications topic is never created in `infra/`:
  it is referenced only by ARN via the `snsNotificationsTopicArn` context key
  and surfaced to the DLQ Lambda as `SNS_NOTIFICATIONS_TOPIC_ARN`. Operator
  alerting and end-user push are separate concerns, so they use separate
  topics. The subscription endpoint is context-driven via `-c alertEmail` and
  `-c alertSlackWebhookUrl` (see `docs/configuration.md`); with neither set the
  topic is created with no subscription and a note is emitted, so synth never
  crashes and no real email is hardcoded.
- Every alarm fires a `cwa.SnsAction` to the alerting topic on BOTH the ALARM
  and OK transitions so recovery is visible as well as onset. Per-tier set:
  - **ECS**: CPU high (>= 85%), memory high (>= 85%), running task count below
    the desired count (threshold from `apiDesiredCount`, fallback 1, read from
    Container Insights with treat-missing-as-breaching so a fully-down service
    pages). `ApiStack` now reads the **same** `apiDesiredCount` key for the
    Fargate service's `desired_count` and auto-scaling `min_capacity`, so the
    running task count and this alarm's threshold always agree per env (1
    staging / 2 prod). Previously the service was hardcoded to 1 while the prod
    threshold was 2, which left the prod alarm permanently breaching.
  - **ALB**: load-balancer + target 5xx sum (> 10 in 5 min via a
    `MathExpression`), unhealthy host count (>= 1), p95 target response time
    (> 2 s).
  - **API Gateway**: 5xx (`metric_server_error` > 5), p99 latency (> 3000 ms),
    and a 4xx `ClientError` proxy (> 50) standing in for the throttle-count
    requirement.
  - **RDS**: CPU (> 80%), free storage (< ~2 GiB, in bytes), connections
    (> 80), plus a prod/multi-AZ-only tighter CPU alarm (> 70%).
  - **SQS**: DLQ depth > 0 (datapoints-to-alarm 1), main-queue oldest-message
    age > 600 s (noting the 120 s Lambda timeout and 300 s visibility timeout).
  - **Lambda**: errors (>= 1), throttles (>= 1), p99 duration > 96000 ms
    (~80% of the 120 s / 120000 ms `LAMBDA_TIMEOUT`).
  - **Redis**: raw `AWS/ElastiCache` metrics dimensioned on the cluster ref for
    evictions, `DatabaseMemoryUsagePercentage` (> 80), and
    `EngineCPUUtilization` (> 75), because the cluster is an L1 `Cfn` construct
    with no metric helpers.
  Thresholds are intentionally conservative initial values to be refined once
  real post-launch baselines exist.
- A `cw.Dashboard` (`RecipeMate-<Env>-Monitoring-overview`) provides one pane of
  glass: a title/env `TextWidget`, one graph row per tier, and an
  alarm-status row covering every created alarm.
- **Per-env gating:** the replica/failover-class RDS alarm is created ONLY for
  prod or when the `multiAz` context key is truthy (single-AZ staging has no
  standby to fail over to). Isolated synth confirmed staging = 20 alarms and
  prod/multi-AZ = 21 alarms, with a subscription created only when
  `alertEmail` / `alertSlackWebhookUrl` is set.
- **API Gateway v2 throttle-metric limitation:** HTTP API v2 publishes NO
  per-API `Throttle` metric (that is a REST API v1 / `AWS/ApiGateway` metric).
  The 4xx `ClientError` alarm is a documented proxy: it includes the 429
  responses emitted once OR-5 route throttling is live. This is why the
  throttle-count checkbox is `[~]` rather than `[x]`.
- **RDS Multi-AZ failover-metric absence:** at the pinned aws-cdk-lib 2.144.0
  there is no clean standard single-instance Multi-AZ failover metric
  (`ReplicaLag` is an Aurora/read-replica concept and there is no public
  `FailoverTime` metric). Rather than reference a non-existent metric, the
  prod/multi-AZ path adds a tighter CPU alarm (> 70%) as the closest standard
  failover-health signal.

**Remaining operator actions** (cannot run in this credential-free workspace):
- [~] **Confirm the alerting subscription.** With no `-c alertEmail` /
      `-c alertSlackWebhookUrl` context key set, the topic exists but has NO
      subscription, so alarms fire to a topic with no endpoint. An email
      subscription additionally requires the operator to click the AWS
      confirmation link before any alarm is delivered.
- [~] **Prove paging end to end with a synthetic failure.** Force a condition
      (e.g. a forced 5xx, or push a message straight to the DLQ) and confirm the
      page arrives. This can only be verified against a live deploy.

**Validation performed (credential-free):** `py_compile` and isolated import of
the monitoring/api stacks under `infra/.venv`, plus an isolated synth of
`MonitoringStack` with stub observed constructs (20 alarms staging, 21
prod/multi-AZ, 0 subscriptions and a note output when no endpoint context is
set). A follow-up isolated synth also confirmed that the Fargate service's
`desired_count` and the `EcsRunningTasksBelowDesired` alarm threshold now both
resolve to `apiDesiredCount` and are equal in each env (1 staging / 2 prod). A
full-app synth still needs AWS credentials (VPC AZ + `HostedZone` lookups on
other stacks); this is the documented pre-existing baseline, not a defect
introduced here.

**Done when:** Alarms exist for every tier, fire to the alerting topic, and a
synthetic failure (e.g. forced 5xx) pages as expected. Remaining to prove live:
operator subscription confirmation and the synthetic-failure paging test
(`[~]` above).

---

## OR-7 — OWASP Top 10 audit

Findings from the current code to address before launch:

- [ ] **A05 Misconfiguration — `SECRET_KEY`.** `app/config.py` defaults to
      `"change-me-in-production"`. Load from Secrets Manager in staging/prod;
      fail startup if unset outside local.
- [ ] **A01 Broken access control — IAM scope.** `ApiStack` task role grants
      `secretsmanager:GetSecretValue` on `*` and attaches `AmazonSSMReadOnlyAccess`.
      Scope to the specific secret ARNs this service needs.
- [ ] **A05 — CORS.** The Flask app registers no CORS layer (no `flask_cors`,
      no `Access-Control` handling). Add an explicit allow-list of the web origin
      (`https://recipemate.me`) rather than a wildcard; keep it tight for the iOS
      native client (which does not need CORS).
- [ ] **A03 Injection.** Confirm all queries use SQLAlchemy parameterization
      (no string-built SQL); validate/escape the LLM-search index values.
- [ ] **A04 Insecure design — LLM.** Bound prompt size and output parsing for
      `extract` and `search/llm`; treat model output as untrusted (already has a
      heuristic fallback — confirm it can't be bypassed to inject).
- [ ] **A02 Crypto / data in transit.** Enforce HTTPS end to end: CloudFront
      already redirects to HTTPS and TLS1.2_2021; ensure API custom domain (OR-9)
      is HTTPS-only and `X-Forwarded-Proto` is honored by Flask.
- [ ] **A07 Auth.** Confirm JWT validation checks issuer, audience (client id),
      and expiry (`app/auth/`); verify 401/403 behavior; set Cognito password/MFA
      policy for prod (MFA optional-but-available at minimum).
- [ ] **A09 Logging/monitoring.** Ensure no secrets/PII in logs; structured
      request logging with request IDs; alarms from OR-6 cover auth failures.
- [ ] **A08 Integrity.** Pin dependencies (already pinned in `requirements.txt`);
      enable image scanning on the ECR repo.
- [ ] **A06 Vulnerable components.** Add dependency scanning (pip-audit / npm
      audit) to the pipeline; enable ECR scan-on-push.
- [ ] **A10 SSRF.** The extractor downloads from S3 and calls OpenAI; confirm it
      never fetches arbitrary user-supplied URLs server-side.

**Done when:** Each item is closed or has a written, accepted risk exception; a
checklist review is recorded.

---

## OR-8 — Leave the door open for iOS

The REST/JSON API, Cognito (public app client, no secret), presigned S3 uploads,
and the SQS→SNS→APNs path are already designed for iOS. Remaining backend-side
enablers (no Xcode work here — that's Phase 7 of the spec):

- [ ] **Device registration endpoint `POST /devices`** (TASK-5.1, currently
      unchecked): store an APNs token per user so the extraction Lambda can
      target the device. No such route exists in `app/routes/`.
- [ ] **SNS platform application (APNs) in CDK** (TASK-5.1): wire the APNs
      platform app + topic so the Lambda's publish actually routes to a device.
- [ ] **Sign in with Apple** is already conditional in `AuthStack` (via CDK
      context). Register the Apple credentials for prod when the app ships.
- [ ] **Cognito callback/logout URLs**: `AuthStack` defaults to
      `localhost:3000`. Add the iOS app's redirect scheme alongside the web URLs
      when the client exists (ties into OR-9).
- [ ] **API versioning / stable contract**: confirm response schemas are stable
      so the web and future iOS clients share one contract; keep
      `docs/api.md` authoritative.

**Done when:** The backend can register a device token and deliver an
extraction-complete push, and auth/redirect config has room for the iOS client
without breaking web.

---

## OR-9 — Domain & DNS: recipemate.me (Route 53)

Domain `recipemate.me` is registered in Route 53. Nothing in `infra/` references
a hosted zone, ACM certificate, or custom domain yet — `WebStack`'s CloudFront
distribution serves only the default `*.cloudfront.net` name, and `ApiStack`'s
HTTP API uses the default `execute-api` endpoint. Note: a CloudFront alias
certificate **must** be issued in **us-east-1** regardless of the us-east-2
default (OR-1); the API Gateway custom-domain cert is regional (us-east-2).

Proposed name plan:
- `recipemate.me` and `www.recipemate.me` → web client (CloudFront, OR-9).
- `api.recipemate.me` → API Gateway custom domain.
- `auth.recipemate.me` → Cognito Hosted UI custom domain (optional but cleaner
  than the default Cognito domain for OAuth redirects).

Tasks:
- [x] Reference the existing hosted zone in CDK (`HostedZone.from_lookup` for
      `recipemate.me`); do not create a second zone. Done in `WebStack` and
      `ApiStack` (and `AuthStack` when a custom auth domain is requested).
- [x] **Web cert (us-east-1):** issue a DNS-validated ACM cert for `recipemate.me`
      + `www.recipemate.me` in us-east-1 for CloudFront. Done in `WebStack` via
      `acm.DnsValidatedCertificate(..., region="us-east-1")`.
- [x] **Web distribution:** add `domain_names` + the cert to `WebStack`'s
      distribution; add Route 53 A/AAAA alias records to CloudFront. Done
      (apex + `www`, both A and AAAA).
- [x] **API cert (us-east-2):** issue a regional ACM cert for `api.recipemate.me`.
      Done in `ApiStack` via `acm.Certificate` in the stack's own region.
- [x] **API custom domain:** add an API Gateway v2 `DomainName` + API mapping on
      `ApiStack`; Route 53 alias `api.recipemate.me` to the API domain. Done
      (`default_domain_mapping` + an A alias to the regional domain).
- [x] **Cognito domain:** set a Hosted UI domain. Done, defaulting to a Cognito
      prefix domain (`recipemate-<env>`); `auth.recipemate.me` (with its own
      us-east-1 cert + alias) is available via the `authCustomDomain` context key.
- [x] **Redirect URLs:** `AuthStack` callback/logout URLs now default to
      `https://recipemate.me/callback` and `/logout` (still overridable via
      `-c callbackUrls` / `-c logoutUrls`). The GitHub Actions
      `VITE_REDIRECT_SIGN_IN` / `VITE_REDIRECT_SIGN_OUT` / `VITE_COGNITO_DOMAIN`
      vars and `VITE_API_BASE_URL` are operator value-updates to the
      already-seeded `/recipemate/<env>/frontend/` SSM params (see
      `docs/configuration.md`).
- [ ] **Email (optional):** if transactional email uses this domain, add SES
      domain verification + DKIM/SPF/DMARC records in the zone. Not started /
      out of scope for this task.
- [~] Confirm cert auto-renewal (DNS validation) and that WAF (OR-5) attaches to
      the aliased CloudFront distribution. DNS validation (hence auto-renewal)
      is wired for every cert; live validation only completes at `cdk deploy`
      against the real account. WAF attachment is tracked under OR-5.

Remaining operator actions (cannot run in this credential-free workspace):
- [~] Live DNS validation at deploy: `cdk deploy` performs the hosted-zone
      lookup and writes the ACM validation CNAMEs; the certs reach `ISSUED`
      only after that round trip.
- [~] Update the real `/recipemate/<env>/frontend/` SSM param VALUES to the new
      domain endpoints (operator value-update; CDK does not write SSM values,
      see `docs/configuration.md`).

**As implemented:**
- The default Cognito hosted-UI domain is a **prefix domain**
  (`recipemate-<env>.auth.<region>.amazoncognito.com`), chosen for a simpler
  first cut: a prefix domain needs no extra us-east-1 certificate and no
  prerequisite apex A record. A custom `auth.recipemate.me` is a drop-in via
  `-c authCustomDomain=auth.recipemate.me`, which issues a us-east-1 cert and an
  alias record.
- **Deploy ordering:** the us-east-1 CloudFront certificate is created first
  (via the DnsValidatedCertificate custom resource) and must reach `ISSUED`
  before the CloudFront distribution can attach it; both the web and API certs
  are DNS-validated, so the validation CNAMEs are written into the existing
  hosted zone automatically.
- **Deploy ordering (custom auth domain):** when using the optional
  `auth.recipemate.me` custom domain (`-c authCustomDomain=auth.recipemate.me`),
  deploy the **Web stack before the Auth stack**. AWS requires an A/AAAA record
  to already exist at the zone apex (`recipemate.me`) before a Cognito custom
  domain can be created, and that apex alias record is created by `WebStack`.
  The default prefix-domain path has no such ordering requirement.
- `DnsValidatedCertificate` is deprecated in newer aws-cdk-lib but remains the
  pragmatic cross-region (us-east-1-cert-from-a-us-east-2-stack) option at the
  pinned 2.144.0.

**Done when:** `https://recipemate.me` serves the web client, `https://api.recipemate.me`
serves the API over the custom domain, OAuth redirects resolve to the real
domain, and all certs are DNS-validated with auto-renewal.

---

## Suggested sequencing

1. OR-1 (region) and OR-2 (creds) — foundation for everything else.
2. OR-9 (domain) and OR-3 (pipeline) — needed to deploy to real endpoints.
3. OR-5 (rate limits), OR-6 (alarms), OR-7 (OWASP) — harden before traffic.
4. OR-4 (e2e) — gates promotion once endpoints are stable.
5. OR-8 (iOS enablers) — can proceed in parallel; not launch-blocking for web.

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

## OR-3 — Backend CI/CD on CodePipeline

Only `deploy-frontend.yml` (GitHub Actions) exists. No backend pipeline builds
the Docker image, pushes to ECR, or rolls the ECS service. `ApiStack` creates the
ECR repo and Fargate service but nothing populates `:latest`.

Tasks:
- [ ] Define a `PipelineStack` (CDK): source → build (docker build + push to the
      `ApiStack` ECR repo) → test (pytest) → deploy (ECS rolling update).
- [ ] Source stage: GitHub connection (CodeStar) on `main`.
- [ ] Build stage: CodeBuild runs `docker build`, tags with commit SHA + `latest`,
      pushes to ECR; run migrations (`alembic upgrade head`) as a deploy step.
- [ ] Deploy stage: update the ECS service to the new image; wait for the ALB
      `/health` target group to go healthy before marking success.
- [ ] Manual-approval action before the prod stage.
- [ ] Decide split: keep frontend on GitHub Actions, or fold it into CodePipeline.

**Done when:** A merge to `main` builds, tests, pushes the image, migrates, and
rolls the Fargate service with zero manual steps (prod gated by approval).

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
- [ ] Set default route throttling (burst + rate) on the HTTP API stage.
- [ ] Add AWS WAF (managed common rules + rate-based rule) in front of
      CloudFront and/or the API — note WAF for CloudFront must be us-east-1
      scope (CLOUDFRONT), regional WAF for the API lives in us-east-2.
- [ ] Add per-user application-level throttling for the expensive LLM endpoints
      (`POST /search/llm`, `POST /recipes/extract`) using the existing Redis
      client (`app/services/redis_client.py`).
- [ ] Return `429` with `Retry-After` and confirm clients handle it.

**Done when:** Load beyond the configured rate is throttled at the edge and the
LLM endpoints are protected per-user; verified with a load test.

---

## OR-6 — CloudWatch alarms

`ApiStack` enables Container Insights and a one-month log group but defines no
alarms and no alerting topic.

Tasks:
- [ ] Create an alerting SNS topic (email/Slack subscription) — distinct from the
      APNs notifications topic.
- [ ] ECS: alarms on service CPU/memory high, running task count < desired.
- [ ] ALB: alarms on 5xx rate, unhealthy host count, target response time p95.
- [ ] API Gateway: alarms on 5xx, p99 latency, throttle count.
- [ ] RDS: CPU, free storage, connection count, replica/failover (prod multi-AZ).
- [ ] SQS extraction: DLQ depth > 0, queue age-of-oldest-message.
- [ ] Lambda extraction: error rate, throttles, duration near timeout (120s).
- [ ] Redis: evictions, memory, CPU.
- [ ] A CloudWatch dashboard summarizing the above.

**Done when:** Alarms exist for every tier, fire to the alerting topic, and a
synthetic failure (e.g. forced 5xx) pages as expected.

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
- [ ] Reference the existing hosted zone in CDK (`HostedZone.from_lookup` for
      `recipemate.me`) — do not create a second zone.
- [ ] **Web cert (us-east-1):** issue a DNS-validated ACM cert for `recipemate.me`
      + `www.recipemate.me` in us-east-1 for CloudFront.
- [ ] **Web distribution:** add `domain_names` + the cert to `WebStack`'s
      distribution; add Route 53 A/AAAA alias records → CloudFront.
- [ ] **API cert (us-east-2):** issue a regional ACM cert for `api.recipemate.me`.
- [ ] **API custom domain:** add an API Gateway v2 `DomainName` + API mapping on
      `ApiStack`; Route 53 alias `api.recipemate.me` → the API domain.
- [ ] **Cognito domain:** set a Hosted UI domain (`auth.recipemate.me` with its
      own us-east-1 cert, or the default Cognito prefix domain if preferred).
- [ ] **Redirect URLs:** update `AuthStack` callback/logout URLs to
      `https://recipemate.me/callback` and `/logout`; update the GitHub Actions
      `VITE_REDIRECT_SIGN_IN` / `VITE_REDIRECT_SIGN_OUT` / `VITE_COGNITO_DOMAIN`
      vars and `VITE_API_BASE_URL` to `https://api.recipemate.me`.
- [ ] **Email (optional):** if transactional email uses this domain, add SES
      domain verification + DKIM/SPF/DMARC records in the zone.
- [ ] Confirm cert auto-renewal (DNS validation) and that WAF (OR-5) attaches to
      the aliased CloudFront distribution.

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

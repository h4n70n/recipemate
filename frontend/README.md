# RecipeMate Web Client

The RecipeMate web frontend: a single-page app for browsing, searching, and
capturing recipes against the REST API in [`docs/api.md`](../docs/api.md),
designed per [`docs/web-ui-spec.md`](../docs/web-ui-spec.md).

## Stack choice: React + TypeScript + Vite

The spec (TASK-6.1) allows "React (or HTMX)". We chose **React + TypeScript +
Vite** because:

- The UI is an interactive SPA with client-side routing (Library, Search, Add,
  Detail) rather than server-rendered pages.
- Auth is a **Cognito Hosted UI OAuth redirect** flow (web-ui-spec §5.1), which
  AWS Amplify integrates cleanly on the client — the redirect callback,
  token storage, and silent refresh are handled in the browser.
- Rich interactions in later tasks (image upload with progress, extraction
  polling, star-rating inputs, filter chips) are a natural fit for a component
  model with local state.

Vite gives fast dev + a typed build; TypeScript keeps the API client and auth
plumbing type-safe.

## What's in this scaffold (TASK-6.1)

This directory implements the **buildable core** of the scaffold:

- Vite React-TS project with `dev` / `build` / `preview` / `lint` / `typecheck`.
- `src/config.ts` — API base URL + Cognito config from `import.meta.env`.
- `src/api/client.ts` — typed `fetch` wrapper that prefixes the base URL,
  injects the Cognito `Authorization: Bearer <token>`, parses JSON, and throws
  a typed `ApiError {status, message}` (mapping the API's `{"error": "..."}`
  body) on non-2xx.
- `src/auth/` — Amplify Hosted-UI config, an `AuthProvider`
  (`{user, isAuthenticated, isLoading, signIn, signOut, getToken}`), and a
  `RequireAuth` route guard.
- App shell (top nav: Library · Search · Add +, user menu with Sign out),
  routing skeleton, a Sign in screen, and placeholder route components.
- `src/styles/tokens.css` — design tokens from web-ui-spec §3.

The actual screens (Library grid, Search, Add flow, Recipe detail, Cook log)
are **TASK-6.2**. S3/CloudFront deploy and GitHub Actions CI/CD are a separate
follow-up.

## Getting started

Requires Node 20+ and npm.

```bash
cp .env.example .env.local   # fill in VITE_API_BASE_URL + Cognito vars
npm install
npm run dev                  # http://localhost:5173
```

## Environment variables

All vars are read at build time (Vite). See [`.env.example`](./.env.example).

| Variable | Purpose |
| --- | --- |
| `VITE_API_BASE_URL` | REST API base incl. `/v1`. Default `http://localhost:5000/v1`. |
| `VITE_COGNITO_USER_POOL_ID` | Cognito user pool id. |
| `VITE_COGNITO_CLIENT_ID` | Cognito app client id (public SPA client). |
| `VITE_COGNITO_DOMAIN` | Hosted UI domain (no scheme). |
| `VITE_COGNITO_REGION` | AWS region of the pool. |
| `VITE_REDIRECT_SIGN_IN` | OAuth sign-in redirect URL. |
| `VITE_REDIRECT_SIGN_OUT` | OAuth sign-out redirect URL. |

When the Cognito vars are absent the app still boots (sign-in disabled) so the
scaffold can be built and inspected before pools exist.

## Scripts

| Script | What it does |
| --- | --- |
| `npm run dev` | Vite dev server with HMR. |
| `npm run build` | Type-check then production build to `dist/`. |
| `npm run preview` | Serve the built `dist/` locally. |
| `npm run typecheck` | `tsc --noEmit`. |
| `npm run lint` | ESLint over `src`. |

## Deployment

The web client is hosted as static files in a private S3 bucket fronted by a
CloudFront distribution (Origin Access Control, SPA deep-link routing). The
infrastructure is defined in the CDK app as `WebStack`
([`infra/stacks/web_stack.py`](../infra/stacks/web_stack.py)).

### 1. Provision the hosting infrastructure (once per env)

Deploying the `WebStack` creates the bucket + distribution and emits the values
the deploy pipeline needs:

```bash
cd infra
cdk deploy RecipeMate-Staging-Web   # or RecipeMate-Prod-Web
```

Note the stack outputs:

- `BucketName` — the S3 bucket to sync into.
- `DistributionId` — the CloudFront distribution to invalidate.
- `DistributionDomainName` — the public `https://<id>.cloudfront.net` URL.

### 2. CI/CD (GitHub Actions)

[`.github/workflows/deploy-frontend.yml`](../.github/workflows/deploy-frontend.yml):

- **Pull requests** touching `frontend/**` run a build check (`typecheck` +
  `build`), no deploy.
- **Push to `main`** touching `frontend/**` (and manual **workflow_dispatch**)
  builds and deploys: `npm ci` → `typecheck` → `build` → `aws s3 sync` →
  CloudFront invalidation.
- AWS access uses **OIDC role assumption** (`aws-actions/configure-aws-credentials`
  with `role-to-assume`); no long-lived access keys are stored.

Configure these in the GitHub repository (Settings → Secrets and variables → Actions):

| Name | Kind | Purpose |
| --- | --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | **secret** | IAM role (trusting GitHub's OIDC provider) the workflow assumes. |
| `AWS_REGION` | variable | AWS region of the bucket/distribution. |
| `FRONTEND_BUCKET` | variable | `WebStack` `BucketName` output. |
| `FRONTEND_DISTRIBUTION_ID` | variable | `WebStack` `DistributionId` output. |
| `VITE_API_BASE_URL` | variable | REST API base incl. `/v1` for the prod bundle. |
| `VITE_COGNITO_USER_POOL_ID` | variable | Cognito user pool id. |
| `VITE_COGNITO_CLIENT_ID` | variable | Cognito app client id. |
| `VITE_COGNITO_DOMAIN` | variable | Cognito Hosted UI domain (no scheme). |
| `VITE_COGNITO_REGION` | variable | AWS region of the pool. |
| `VITE_REDIRECT_SIGN_IN` | variable | OAuth sign-in redirect URL (the CloudFront URL). |
| `VITE_REDIRECT_SIGN_OUT` | variable | OAuth sign-out redirect URL. |

The `VITE_*` values are injected at build time so the production bundle targets
the live API and Cognito pool. Any `VITE_*` value holding a secret can be moved
to Actions **secrets** instead of variables; reference it with `secrets.<NAME>`
in the workflow.

### 3. Manual deploy

[`scripts/deploy.sh`](./scripts/deploy.sh) performs the same build → sync →
invalidate steps locally. It needs AWS credentials in your environment:

```bash
BUCKET=<WebStack BucketName> \
DIST_ID=<WebStack DistributionId> \
AWS_REGION=us-east-1 \
  ./scripts/deploy.sh
```

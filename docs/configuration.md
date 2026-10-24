This document is the reference for every environment variable read by RecipeMate. Copy `.env.example` to `.env` and fill in the values marked Required.

## Configuration

### Flask

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| FLASK_ENV | Selects the config class: local, staging, or prod. Controls debug mode and logging level. | local | Yes | Leave as local for Docker Compose. |
| SECRET_KEY | Secret for session signing and CSRF protection. Must be a long random string in all environments. | (generated) | Yes | Local: generate with python -c "import secrets; print(secrets.token_hex(32))" and never commit. Staging/prod: loaded from Secrets Manager at runtime (see below) — the app refuses to start if it is unset or still the change-me-in-production default. |

### Database

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| DATABASE_URL | SQLAlchemy connection string for PostgreSQL. | postgresql+psycopg2://recipemate:recipemate@db:5432/recipemate | Local/dev | Default points at the db Docker Compose service. In staging/prod the URL is composed from the DB_* parts below (injected from the RDS secret), not from DATABASE_URL. |
| DB_USER / DB_PASSWORD / DB_HOST / DB_PORT / DB_NAME | Individual RDS connection parts injected from the Secrets Manager DB-credentials secret in staging/prod. The app composes postgresql+psycopg2://user:pass@host:port/dbname from them (password URL-encoded). | (from secret) | Staging/prod | Not set locally — DATABASE_URL is used instead. When these are present they take priority over DATABASE_URL. |

### Redis

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| REDIS_URL | Redis connection string. Used for search result caching and upload metadata. | redis://redis:6379/0 | Yes | Default points at the redis Docker Compose service. |

### AWS — General

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| AWS_REGION | AWS region for all services. | us-east-2 | Yes | Defaults to us-east-2; fine to keep locally. |
| AWS_ACCESS_KEY_ID | IAM access key. | test | Local only | Set to test for LocalStack. For staging/prod use an ECS task IAM role; do not set this variable. |
| AWS_SECRET_ACCESS_KEY | IAM secret key. | test | Local only | Same as above. |
| AWS_ENDPOINT_URL | Overrides the AWS SDK endpoint to point at LocalStack. | http://localstack:4566 | Local only | Remove or leave empty in staging/prod. |

### AWS — S3

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| S3_BUCKET | S3 bucket name for all images. | recipemate-images | Yes | Create locally: aws --endpoint-url=http://localhost:4566 s3 mb s3://recipemate-images |

### AWS — SQS

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| SQS_EXTRACTION_QUEUE_URL | URL of the SQS queue for extraction jobs. Lambda polls this. | http://localstack:4566/000000000000/recipemate-extraction | Yes | Create locally: aws --endpoint-url=http://localhost:4566 sqs create-queue --queue-name recipemate-extraction |

### AWS — SNS

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| SNS_NOTIFICATIONS_TOPIC_ARN | ARN of the SNS topic for push notifications. | arn:aws:sns:us-east-2:000000000000:recipemate-notifications | Yes | Create locally: aws --endpoint-url=http://localhost:4566 sns create-topic --name recipemate-notifications. APNs delivery only works in staging/prod. |

### OpenAI

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| OPENAI_API_KEY | API key for GPT-4o (extraction) and GPT-4 (LLM search). | sk-... | Yes | Required even locally to test extraction or LLM search. In staging/prod stored in AWS Secrets Manager, not as an env var. |

### AWS Cognito — Authentication

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| COGNITO_USER_POOL_ID | Cognito User Pool ID. Format: region_id. | us-east-2_AbCdEfGhI | Staging/prod | Not validated locally if auth middleware is bypassed for testing. |
| COGNITO_CLIENT_ID | Cognito App Client ID. | 7abc123... | Staging/prod | Same as above. |
| COGNITO_REGION | AWS region of the Cognito User Pool. Usually matches AWS_REGION. | us-east-2 | Staging/prod | Usually same as AWS_REGION. |

## Backend deploy credentials (CI)

RecipeMate deploys the backend (CDK, ECR, ECS) from CI using short-lived
credentials obtained through GitHub OIDC — there are **no static AWS keys in
CI**. The only place `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are set is
local development, where they are the LocalStack `test` values; they must never
be committed for staging or prod.

`PipelineOidcStack` (`infra/stacks/pipeline_oidc_stack.py`) provisions a GitHub
OIDC identity provider and a least-privilege deploy role scoped to this
repository on `main`. The role can assume the `cdk-*` bootstrap roles and push
to ECR / roll the ECS service, nothing broader.

### GitHub secret

| Secret | Description | Where it comes from |
|--------|-------------|---------------------|
| AWS_BACKEND_DEPLOY_ROLE_ARN | ARN of the backend deploy role CI assumes via OIDC. | The `BackendDeployRoleArn` CloudFormation output of `PipelineOidcStack`. |

Set it in the GitHub repository under Settings → Secrets and variables → Actions
→ Repository secrets. The CI workflow passes it to
`aws-actions/configure-aws-credentials@v4` as `role-to-assume` (the same pattern
the frontend deploy already uses with `AWS_DEPLOY_ROLE_ARN`).

### Deploying the OIDC stack

The GitHub repo and provider-creation behaviour are supplied via CDK context:

- `-c githubRepo=owner/recipemate` — the `owner/name` allowed to assume the role
  (defaults to the placeholder `your-org/recipemate`).
- `-c createOidcProvider=false` — skip creating the OIDC provider and import the
  account's existing one. An AWS account allows only one OIDC provider per
  issuer URL, so pass this if the provider already exists; omit it to create one.

```
cdk deploy RecipeMate-<Env>-PipelineOidc \
    -c githubRepo=owner/recipemate
```

### CDK bootstrap

Before the first deploy, bootstrap the target account in the RecipeMate region
(us-east-2) so the `cdk-*` roles the deploy role assumes exist:

```
cdk bootstrap aws://<account-id>/us-east-2
```

## CI/CD pipeline (CodePipeline)

Build + deploy for both the backend and the frontend is owned by a single AWS
CodePipeline per environment (`RecipeMate-<Env>-Pipeline`, defined in
`infra/stacks/pipeline_stack.py`, TASK-8.4 / OR-3). GitHub is a source-only
input via a CodeStar connection; all build/test/deploy work runs inside
CodeBuild projects that assume scoped IAM roles, so there are **no static AWS
keys** anywhere in the pipeline.

The old `deploy-frontend.yml` GitHub Actions deploy job has been retired to a
credential-free PR build-check; the frontend deploy path now lives in this
pipeline, not in GitHub Actions.

### CodeStar GitHub connection

The pipeline's Source stage uses an AWS CodeStar GitHub connection on `main`.
The operator creates this connection **once** in the AWS console or CLI and
completes the GitHub handshake so its status is `AVAILABLE`, then supplies its
ARN to the Pipeline stack via CDK context:

- `-c codestarConnectionArn=<arn>`: the CodeStar connection ARN (format
  `arn:aws:codestar-connections:us-east-2:<account>:connection/<uuid>`). A
  documented placeholder is used when the context is absent so `cdk synth`
  works without it, but a real `AVAILABLE` connection is required to deploy.
- `-c githubRepo=h4n70n/recipemate`: the `owner/name` repo the source action
  reads (defaults to the placeholder `your-org/recipemate`).

### Frontend build config via SSM Parameter Store

The frontend CodeBuild project reads the Vite build-time config from SSM
Parameter Store (type `PARAMETER_STORE`) rather than from GitHub Actions `vars`.
The operator seeds one string parameter per value, per environment, under the
prefix `/recipemate/<env>/frontend/` (where `<env>` is `staging` or `prod`):

| SSM parameter name | Vite variable |
|--------------------|---------------|
| `/recipemate/<env>/frontend/VITE_API_BASE_URL` | `VITE_API_BASE_URL` |
| `/recipemate/<env>/frontend/VITE_COGNITO_USER_POOL_ID` | `VITE_COGNITO_USER_POOL_ID` |
| `/recipemate/<env>/frontend/VITE_COGNITO_CLIENT_ID` | `VITE_COGNITO_CLIENT_ID` |
| `/recipemate/<env>/frontend/VITE_COGNITO_DOMAIN` | `VITE_COGNITO_DOMAIN` |
| `/recipemate/<env>/frontend/VITE_COGNITO_REGION` | `VITE_COGNITO_REGION` |
| `/recipemate/<env>/frontend/VITE_REDIRECT_SIGN_IN` | `VITE_REDIRECT_SIGN_IN` |
| `/recipemate/<env>/frontend/VITE_REDIRECT_SIGN_OUT` | `VITE_REDIRECT_SIGN_OUT` |

The frontend build role is scoped to `ssm:GetParameters` on exactly these
parameter ARNs. These values **moved off** the GitHub Actions `vars.*` the
retired `deploy-frontend.yml` deploy job used; after the CodePipeline frontend
stage is proven in staging, the operator can remove the now-unused GitHub vars.

### Deploy runbook

1. Create the CodeStar GitHub connection once and complete the GitHub handshake
   so its status is `AVAILABLE`; note its ARN.
2. Bootstrap the target account in the RecipeMate region (if not already done):

   ```
   cdk bootstrap aws://<account-id>/us-east-2
   ```

3. Seed the `VITE_*` SSM parameters listed above for the target env.
4. Deploy the Pipeline stack, passing the repo and connection ARN:

   ```
   cdk deploy RecipeMate-<Env>-Pipeline \
       -c githubRepo=h4n70n/recipemate \
       -c codestarConnectionArn=<arn>
   ```

A merge to `main` then builds + tests + deploys automatically for staging; the
prod pipeline gates the deploys behind a manual approval.

## Secrets Manager (staging / prod)

In staging and prod, no secret value lives in an env file, a `.env`, or the
CloudFormation template. The ECS task definition injects each secret as a
container secret (`ecs.Secret.from_secrets_manager`), so values are fetched by
the ECS agent at task start and resolve into the container's environment at
runtime. The task role's `secretsmanager:GetSecretValue` is scoped to exactly
these secret ARNs (OWASP A01, TASK-8.8).

RecipeMate reads three groups of secrets at runtime:

| Secret | Secrets Manager name | Injected as | Who populates the value |
|--------|----------------------|-------------|-------------------------|
| OpenAI API key | `recipemate/<env>/openai-api-key` | `OPENAI_API_KEY` | Operator, post-deploy (the CDK-generated value is only a placeholder). |
| Flask signing key | `recipemate/<env>/flask-secret-key` | `SECRET_KEY` | CDK, at create time (Secrets Manager generates a strong random value that IS the real key — no manual step). |
| RDS credentials | `<DatabaseStack-name>/db-credentials` | `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_NAME` | RDS, when the instance is created. The app composes `SQLALCHEMY_DATABASE_URI` from these parts. |

### Flask SECRET_KEY — generated, no manual step

`SecretsStack` creates `recipemate/<env>/flask-secret-key` with a Secrets
Manager–generated 64-character value under the `SECRET_KEY` JSON key. A signing
key only needs to be random, so the generated value is used as-is; there is **no**
`put-secret-value` step. The secret uses `RETAIN` and is not regenerated on
redeploy, so existing sessions/CSRF tokens stay valid. The app refuses to start
in staging/prod if `SECRET_KEY` is unset or still the `change-me-in-production`
default.

### OpenAI API key — populate post-deploy

`SecretsStack` creates `recipemate/<env>/openai-api-key` with a generated
placeholder (never a real key). Set the real key once after the first deploy:

```
aws secretsmanager put-secret-value \
    --secret-id recipemate/<env>/openai-api-key \
    --secret-string '{"OPENAI_API_KEY":"sk-..."}' \
    --region us-east-2
```

Both the API container and the extraction Lambda read the OpenAI key from this
same secret (the Lambda fetches it at runtime by name via `OPENAI_SECRET_NAME`),
so one `put-secret-value` covers both.

### Database URL

The RDS secret holds separate `username`/`password`/`host`/`port`/`dbname`
fields rather than a ready-made URL. `ApiStack` injects those fields as the
`DB_*` container env vars above and `app/config.py` composes the SQLAlchemy
connection string from them at runtime (URL-encoding the password). No manual
step is required; RDS populates the secret. Local development keeps using
`DATABASE_URL` directly.

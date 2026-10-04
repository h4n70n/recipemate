This document is the reference for every environment variable read by RecipeMate. Copy `.env.example` to `.env` and fill in the values marked Required.

## Configuration

### Flask

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| FLASK_ENV | Selects the config class: local, staging, or prod. Controls debug mode and logging level. | local | Yes | Leave as local for Docker Compose. |
| SECRET_KEY | Secret for session signing and CSRF protection. Must be a long random string in all environments. | (generated) | Yes | Generate: python -c "import secrets; print(secrets.token_hex(32))". Never commit. |

### Database

| Variable | Description | Example | Required | Local dev notes |
|----------|-------------|---------|----------|-----------------|
| DATABASE_URL | SQLAlchemy connection string for PostgreSQL. | postgresql+psycopg2://recipemate:recipemate@db:5432/recipemate | Yes | Default points at the db Docker Compose service. Change host/credentials for staging/prod. |

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

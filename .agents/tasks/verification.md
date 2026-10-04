# Verification — TASK-8.3 (feat/secrets-manager)

Secrets Manager configuration and runtime key loading. This note records the
exact commands run and their outcomes so the reviewer does not need to re-run
the suites.

## Environment

- `aws-cdk-lib` pinned version: **2.144.0**
  (`/home/dr1ftp1n/recipemate/infra/.venv/bin/python -m pip show aws-cdk-lib`).
- CDK CLI **2.1016.1**; Node v20.17.0 resolved from
  `/home/dr1ftp1n/.local/node/bin` (added to PATH for synth; `node`/`cdk` are
  not on the default sandbox PATH).
- The worktree has no `infra/.venv`; the main repo's
  `/home/dr1ftp1n/recipemate/infra/.venv` Python (same pinned deps, has pytest
  9.1.1 + Flask/SQLAlchemy/jose/cryptography/moto) was used as the CDK app
  interpreter and the pytest runner. The code under test is the worktree copy.

## (a) CDK synth

Primary — the Secrets stack (no VPC/AZ lookup):

```
cdk synth --app "<venv>/bin/python app.py" RecipeMate-Staging-Secrets
```

Result: **exit 0**. Template renders the new `FlaskSecretKey`
`AWS::SecretsManager::Secret` (generated 64-char value under the `SECRET_KEY`
JSON key, `RETAIN`), the existing `OpenAiApiKeySecret`, the
`FlaskSecretKeyName`/`FlaskSecretKeyArn` outputs, and cross-stack `Export`s for
both secrets.

The Api stack trips the documented pre-existing AZ/VPC context lookup:

```
cdk synth --app "<venv>/bin/python app.py" RecipeMate-Staging-Api
# [Error at /RecipeMate-Staging-Api] Need to perform AWS calls for account
# 111111111111, but no credentials have been configured
```

This is the KNOWN PRE-EXISTING ISSUE (us-east-2 move needs real-credential AZ
context); it is unrelated to the TASK-8.3 wiring. Per the task, I used the
`python app.py` fallback, which exercises the full construct tree (including
`ApiStack`) without the environment lookup:

```
cd infra && <venv>/bin/python app.py   # calls app.synth()
```

Result: **exit 0**. This wrote every stack template to `infra/cdk.out/`,
including `RecipeMate-Staging-Api.template.json`, so the Api wiring was
inspected directly (below).

### Api template inspection (from `infra/cdk.out/RecipeMate-Staging-Api.template.json`)

- Container `Secrets` present for all seven names: `SECRET_KEY`,
  `OPENAI_API_KEY`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_NAME`.
  Every one is a `ValueFrom` **reference** (ARN + `:field::`), never a literal
  value, and injected via `secrets=` (not `environment=`).
- `FLASK_ENV` and `PORT` remain plain `Environment` entries.
- Task role `secretsmanager:GetSecretValue` is **scoped to the specific secret
  ARNs** — no `Resource: "*"` remains on any GetSecretValue statement (checked
  programmatically: `ANY STAR: False`). The execution role also carries a
  scoped GetSecretValue (CDK auto-grants it so the ECS agent can pull the
  `ecs.Secret` values at launch — expected).
- `AmazonSSMReadOnlyAccess` managed policy: **0 occurrences** — dropped. The
  app reads no SSM parameters (verified by grepping `app/` for
  `boto3.client("ssm")` / `get_parameter` / `ssm.` — no matches).

## (b) Leaked-value grep

```
grep -rniE "change-me-in-production|sk-[A-Za-z0-9]{10,}" infra/cdk.out/*.template.json
```

Result: **no matches** (grep exit 1). No `SECRET_KEY` value, no OpenAI key, and
no `change-me-in-production` placeholder appears in any synthesized template.
Secret values resolve at task start from Secrets Manager references only.

## (c) Python test suite

Runner: `<venv>/bin/python -m pytest` from the worktree root (config/auth/unit
tests use an in-memory SQLite app via `tests/conftest.py`, so no Postgres is
required and nothing was skipped).

Full suite:

```
<venv>/bin/python -m pytest
# 303 passed in 9.36s
```

All **303** tests pass (295 pre-existing + 8 new in `tests/test_config.py`).
None skipped. The new tests cover the TASK-8.3 app-side behaviour:

- `SQLALCHEMY_DATABASE_URI` composed from `DB_*` parts with the password
  URL-encoded; partial parts fall back to `DATABASE_URL`; local fallback intact.
- Staging/prod raise `RuntimeError` on an unset/placeholder `SECRET_KEY`; local
  keeps the permissive default; a real injected key lets staging/prod start.

## Lambda-path confirmation

`lambda/extract_handler.py` `get_openai_api_key()` reads the secret **name**
from the `OPENAI_SECRET_NAME` env var and fetches the value at runtime via
`boto3.client("secretsmanager").get_secret_value(...)`, reading the
`OPENAI_SECRET_JSON_KEY` (default `OPENAI_API_KEY`) field. `QueueStack` sets
`OPENAI_SECRET_NAME` to `recipemate/<env>/openai-api-key` — the same secret the
API container now reads for `OPENAI_API_KEY`. The Lambda wiring was read and
confirmed, not reworked.

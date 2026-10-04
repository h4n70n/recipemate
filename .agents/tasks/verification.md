# Verification — TASK-8.1 & TASK-8.2 (feat/deploy-foundation)

This note records exactly what was run and the results, so the review does not
need to re-run anything.

## Environment

- `aws-cdk-lib` pinned version: **2.144.0** (`pip show aws-cdk-lib`).
- CDK CLI + Node resolved from `/home/dr1ftp1n/.local/node/bin` (added to PATH
  for the synth runs; `node` is not on the default PATH in this sandbox).
- The worktree has no `infra/.venv`; the main repo's `/home/dr1ftp1n/recipemate/infra/.venv`
  Python (identical pinned deps) was used as the CDK app interpreter via
  `cdk synth --app "<venv>/bin/python app.py"`. The app code under test is the
  worktree copy at `.worktrees/deploy-foundation/infra`.

## TASK-8.1 — region grep

Command:

```
grep -rn "us-east-1" \
  .worktrees/deploy-foundation/app/ \
  .worktrees/deploy-foundation/infra/ \
  .worktrees/deploy-foundation/docs/ \
  .worktrees/deploy-foundation/.env.example
```

Remaining matches after the change (all intentional):

- `.env.example:78` and `.env.example:80` — SNS **LocalStack** example/default
  ARN (`arn:aws:sns:us-east-1:000000000000:...`). LocalStack `000000000000`
  lines are left as-is per the task.
- `infra/__pycache__/config.cpython-312.pyc` — stale compiled bytecode cache
  (regenerates; not source).

No `us-east-1` **default** remains in authored source outside the LocalStack
local-only lines. In addition to the config files named in the task, two
runtime region fallbacks that defaulted to `us-east-1` were aligned to
`us-east-2` to satisfy the "no us-east-1 default remains" criterion:
`app/services/s3.py` and `app/services/sqs.py` (both only used when
`AWS_REGION` is unset, which now defaults to `us-east-2`).

Stale CDK context cache for the no-longer-targeted region was removed from
`infra/cdk.json` (the two `availability-zones:...:region=us-east-1` entries);
they were keyed to us-east-1 and would never be consulted now that synth
targets us-east-2.

Out of scope / noted, not changed: `frontend/.env.example`
(`VITE_COGNITO_REGION=us-east-1`) — frontend build-time Vite vars are sourced
from CI (GitHub vars today, CodeBuild/SSM per TASK-8.4); not in the TASK-8.1
file list or the verification grep scope.

## TASK-8.2 — static-keys grep

Command:

```
grep -rn "AWS_SECRET_ACCESS_KEY\|AWS_ACCESS_KEY_ID" \
  .worktrees/deploy-foundation/.env.example
```

Result (only the LocalStack `test` values, which are fine):

```
50:AWS_ACCESS_KEY_ID=test
51:AWS_SECRET_ACCESS_KEY=test
```

## CDK synth

Primary verification — synth the new stack (OIDC provider created path):

```
cdk synth --app "<venv>/bin/python app.py" RecipeMate-Staging-PipelineOidc
```

Result: **exit 0**. Template renders the GitHub OIDC provider, the
`BackendDeployRole`, and the `BackendDeployRoleArn` output.

Import path (existing provider), with repo context:

```
cdk synth --app "<venv>/bin/python app.py" RecipeMate-Staging-PipelineOidc \
  -c createOidcProvider=false -c githubRepo=owner/recipemate
```

Result: **exit 0**. Trust policy renders
`token.actions.githubusercontent.com:sub: repo:owner/recipemate:ref:refs/heads/main`,
`Federated: arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com`,
and `MaxSessionDuration: 3600`.

Baseline whole-app synth (before edits), documented fallback:

```
<venv>/bin/python app.py   # calls app.synth()
```

Result: **exit 0**.

### Note on `cdk synth --all`

`cdk synth --all` for the worktree reports, for `Database`, `Cache`, and `Api`
only: "Need to perform AWS calls for account 111111111111, but no credentials
have been configured." This is a pre-existing, environment-dependent VPC/AZ
**context lookup** that needs either credentials or cached context — it is
triggered by the region moving to us-east-2 (the pristine main repo, still on
us-east-1 with cached us-east-1 AZs, synths `--all` clean). It is unrelated to
`PipelineOidcStack`, which performs no lookups and synths cleanly on its own.
Once the account is bootstrapped / credentials are present (or `cdk context`
is populated for us-east-2), `--all` resolves.

## GitHub-UI follow-ups (cannot be set from in-repo files)

- Set repo-level GitHub Actions variable `vars.AWS_REGION=us-east-2` (and
  `vars.VITE_COGNITO_REGION=us-east-2`) in the GitHub UI (TASK-8.1 bullet;
  TASK-8.4 will retire the Actions deploy path anyway).
- After deploying `PipelineOidcStack`, create the GitHub secret
  `AWS_BACKEND_DEPLOY_ROLE_ARN` pointing at the stack's `BackendDeployRoleArn`
  output.
- Bootstrap the account for the RecipeMate region: `cdk bootstrap aws://<account>/us-east-2`.

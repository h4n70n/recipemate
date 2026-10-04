# Implementation Plan — Frontend GitHub-OIDC deploy role in WebStack

Goal: provision the IAM role that `AWS_DEPLOY_ROLE_ARN` (used by
`.github/workflows/deploy-frontend.yml`) points to. The frontend deploy job
assumes a role via GitHub OIDC, then runs `aws s3 sync dist "s3://<bucket>" --delete`
and `aws cloudfront create-invalidation --distribution-id <id> --paths "/*"`.
No stack currently creates that role; we add it to `WebStack` (which already owns
the target bucket + distribution), mirroring the conventions in
`PipelineOidcStack`.

Scope: a single source file is edited — `infra/stacks/web_stack.py`. Do NOT touch
the GitHub Actions workflow, `PipelineOidcStack`, or any other stack. Edit in
place on the current branch (`main`); no worktree.

## Design decisions (pre-approved by the task; recorded here for the implementer)

- **Role lives in WebStack, not a new stack.** WebStack already owns the bucket
  and distribution the role must write to, so bucket/distribution ARNs and grant
  helpers are directly in scope. Add it at the end of `WebStack.__init__`, after
  the distribution and alongside the existing `CfnOutput`s.
- **WebStack imports the OIDC provider; it never creates one.** AWS allows only
  one OIDC provider per issuer URL per account, and `PipelineOidcStack` is the
  single creator. WebStack always imports by the well-known ARN
  `arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com`.
- **Least privilege.** Grant only the four S3 actions the sync+delete needs
  (`ListBucket`, `GetObject`, `PutObject`, `DeleteObject`) scoped to the bucket
  and `bucket/*`, plus `cloudfront:CreateInvalidation`/`GetInvalidation` scoped
  to the distribution ARN. No `s3:*`, no `*` resources.

## Environment note (verification constraints discovered during exploration)

- `infra/requirements.txt` pins `aws-cdk-lib==2.144.0`, `constructs==10.3.0`.
  Python venv at `infra/.venv` already has these installed.
- **The CDK CLI (`cdk`) is NOT installed and there is NO Node runtime in this
  environment.** `aws-cdk-lib` uses jsii, which spawns `node`; running `cdk synth`
  (or even `python3 app.py`) currently fails with `FileNotFoundError: 'node'`.
  A stale `infra/cdk.out/` exists from a previous synth.
- Therefore the authoritative verification command is `cdk synth`, but it
  requires Node + the CDK CLI. The implementer must ensure both are present
  (e.g. install Node 20 and `npm install -g aws-cdk`) before verifying. A
  Node-free fallback (`py_compile`) is included below to catch syntax errors, but
  it is NOT a substitute for `cdk synth`.
- No infra unit-test suite exists (only library tests under `.venv`), so
  verification is synth-based, matching how the sibling stacks are validated.

---

- [ ] 1. Add the `aws_iam` import to WebStack.
      Add `aws_iam as iam` to the existing `from aws_cdk import (...)` block in
      `web_stack.py` (which currently imports `Stack`, `aws_cloudfront as cloudfront`,
      `aws_cloudfront_origins as origins`, `aws_s3 as s3`). Keep `import aws_cdk as cdk`
      as-is. This item alone leaves the file buildable (unused import).
      Files: infra/stacks/web_stack.py
      Verify: `cd infra && .venv/bin/python -m py_compile stacks/web_stack.py` exits 0
      (no syntax error). Full synth is verified in the final item.

- [ ] 2. Add the three OIDC class constants to WebStack.
      On the `WebStack` class, directly below the class docstring, add class-level
      constants mirroring `PipelineOidcStack` verbatim:
      `GITHUB_OIDC_URL: str = "https://token.actions.githubusercontent.com"`,
      `GITHUB_OIDC_AUDIENCE: str = "sts.amazonaws.com"`,
      `DEFAULT_GITHUB_REPO: str = "your-org/recipemate"`, each with the same short
      `#:` doc comments PipelineOidcStack uses.
      Files: infra/stacks/web_stack.py
      Verify: `cd infra && .venv/bin/python -m py_compile stacks/web_stack.py` exits 0.

- [ ] 3. Build the frontend deploy role, its policies, and its output inside
      `WebStack.__init__`.
      Insert a new section at the END of `__init__`, after `self._distribution` is
      created. Place it just before (or interleaved with) the existing `CfnOutput`
      block so the new output sits with the others. Implement, in order:
      1. Resolve the repo:
         `github_repo = self.node.try_get_context("githubRepo") or self.DEFAULT_GITHUB_REPO`.
      2. Import (never create) the OIDC provider by well-known ARN, with a comment
         stating that provider creation is owned by `PipelineOidcStack` to avoid the
         "one provider per issuer URL per account" conflict:
         `provider_arn = f"arn:aws:iam::{self.account}:oidc-provider/token.actions.githubusercontent.com"`
         then
         `imported = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(self, "ImportedGitHubOidcProviderForWeb", provider_arn)`.
      3. Wrap it in an `iam.OpenIdConnectPrincipal(imported, conditions={...})` using
         the SAME trust conditions as PipelineOidcStack: `StringEquals` on
         `token.actions.githubusercontent.com:aud` == `self.GITHUB_OIDC_AUDIENCE`, and
         `StringLike` on `token.actions.githubusercontent.com:sub` ==
         `f"repo:{github_repo}:ref:refs/heads/main"`.
      4. Create the role:
         `iam.Role(self, "WebDeployRole", role_name=f"{self.stack_name}-web-deploy",
         assumed_by=<the principal>, description=<"Frontend CI deploy role (S3 sync +
         CloudFront invalidation) assumed via GitHub OIDC from repo {github_repo} on
         main">, max_session_duration=cdk.Duration.hours(1))`. Store it as
         `self._web_deploy_role`.
      5. Grant least-privilege S3 access scoped to this bucket. Preferred: use the
         bucket grant helpers — `self._bucket.grant_read_write(self._web_deploy_role)`
         plus `self._bucket.grant_delete(self._web_deploy_role)` — which together
         produce `s3:ListBucket`/`GetObject`/`PutObject` and `s3:DeleteObject` scoped
         to the bucket ARN and `bucket/*`. If the resulting policy is not clean,
         fall back to explicit `iam.PolicyStatement`s: one with `s3:ListBucket` on
         `self._bucket.bucket_arn`, one with `s3:GetObject`/`s3:PutObject`/`s3:DeleteObject`
         on `f"{self._bucket.bucket_arn}/*"`. Either way, ListBucket, GetObject,
         PutObject, DeleteObject must all be present; never `s3:*` or `*` resources.
      6. Grant CloudFront invalidation via an explicit `iam.PolicyStatement`:
         actions `cloudfront:CreateInvalidation`, `cloudfront:GetInvalidation`;
         resource `f"arn:aws:cloudfront::{self.account}:distribution/{self._distribution.distribution_id}"`
         (note the empty region segment between the two colons). Scope to that ARN,
         not `*`.
      7. Emit the output:
         `cdk.CfnOutput(self, "WebDeployRoleArn", value=self._web_deploy_role.role_arn,
         description=<mirror PipelineOidcStack's BackendDeployRoleArn wording: tell the
         operator to store it as the AWS_DEPLOY_ROLE_ARN GitHub secret>)`. Match the
         phrasing style of PipelineOidcStack's `BackendDeployRoleArn` description
         ("IAM role ARN for CI frontend deploys — store as the AWS_DEPLOY_ROLE_ARN
         GitHub secret").
      Files: infra/stacks/web_stack.py
      Verify: `cd infra && .venv/bin/python -m py_compile stacks/web_stack.py` exits 0.
      Full behavioural verification is item 6.

- [ ] 4. Expose the role as a public `web_deploy_role` property.
      Add a `@property def web_deploy_role(self) -> iam.Role:` returning
      `self._web_deploy_role`, placed with the existing `bucket` and `distribution`
      properties and following the same one-line-docstring style.
      Files: infra/stacks/web_stack.py
      Verify: `cd infra && .venv/bin/python -m py_compile stacks/web_stack.py` exits 0.

- [ ] 5. Update the module and class docstrings to describe the new role.
      In `web_stack.py`:
      - Extend the module docstring with one sentence noting WebStack also provisions
        the GitHub-OIDC frontend deploy role whose ARN is the `AWS_DEPLOY_ROLE_ARN`
        secret.
      - In the class docstring "Creates:" section, add a bullet for the
        least-privilege frontend deploy role (trusted only by this repo on `main`,
        imports the OIDC provider owned by PipelineOidcStack, S3 sync + CloudFront
        invalidation only).
      - In "Properties exposed", add `web_deploy_role` – the IAM Role CI assumes
        via OIDC.
      - In "CloudFormation outputs", add `WebDeployRoleArn` – store as the
        `AWS_DEPLOY_ROLE_ARN` GitHub secret.
      Files: infra/stacks/web_stack.py
      Verify: `cd infra && .venv/bin/python -m py_compile stacks/web_stack.py` exits 0.

- [ ] 6. Verify the stack synthesizes and the role is correct.
      Run the project's real synth command. This REQUIRES Node + the CDK CLI; if the
      environment lacks them (as the planning environment did), install them first
      (Node 20, then `npm install -g aws-cdk`).
      Files: (no edits — verification only)
      Verify, in order:
      1. `cd infra && cdk synth RecipeMate-Staging-Web` completes without error and
         prints the template (default context `env=staging` from `cdk.json`).
      2. Inspect the synthesized template for the role and its scoping, e.g.
         `cdk synth RecipeMate-Staging-Web > /tmp/web.yaml` then confirm:
         - an `AWS::IAM::Role` named `RecipeMate-Staging-Web-web-deploy` with
           `MaxSessionDuration: 3600`;
         - its `AssumeRolePolicyDocument` has `Federated` =
           `arn:aws:iam::...:oidc-provider/token.actions.githubusercontent.com`, a
           `StringEquals` on `...:aud` = `sts.amazonaws.com`, and a `StringLike` on
           `...:sub` = `repo:your-org/recipemate:ref:refs/heads/main`;
         - the attached policy contains exactly `s3:ListBucket`, `s3:GetObject`,
           `s3:PutObject`, `s3:DeleteObject` (scoped to the WebBucket ARN and `/*`,
           no `s3:*`/`*`) and `cloudfront:CreateInvalidation`/`cloudfront:GetInvalidation`
           scoped to the `distribution/<id>` ARN;
         - a `WebDeployRoleArn` output whose description references the
           `AWS_DEPLOY_ROLE_ARN` secret.
      3. Confirm a repo override works:
         `cdk synth RecipeMate-Staging-Web -c githubRepo=acme/recipemate` and check the
         `sub` condition becomes `repo:acme/recipemate:ref:refs/heads/main`.
      4. Node-free fallback only if the CDK CLI genuinely cannot be installed:
         `cd infra && .venv/bin/python -m py_compile stacks/web_stack.py app.py` exits 0
         to catch syntax/import errors. State explicitly in the step output that
         `cdk synth` could not be run and why — this fallback does not prove the
         template is correct.

## Expected outcome

`infra/stacks/web_stack.py` provisions a `RecipeMate-<Env>-Web-web-deploy` IAM
role assumable only by this repo's `main` branch via the imported GitHub OIDC
provider, scoped to the four S3 actions needed for `s3 sync --delete` into the
WebBucket plus CloudFront invalidation on the WebDistribution, exposed as the
`web_deploy_role` property and emitted as the `WebDeployRoleArn` output for the
`AWS_DEPLOY_ROLE_ARN` GitHub secret. No other files change.

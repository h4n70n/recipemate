# GitHub-OIDC frontend deploy role added to WebStack

WebStack now provisions the IAM role that CI assumes via GitHub OIDC to deploy the SPA — `aws s3 sync dist s3://<bucket> --delete` plus a CloudFront invalidation — and emits its ARN as the `WebDeployRoleArn` output so an operator can store it as the `AWS_DEPLOY_ROLE_ARN` GitHub secret. The role lives in WebStack rather than a new stack because WebStack already owns the bucket and distribution the role must write to, so the ARNs and grant helpers are in scope. It imports the existing GitHub OIDC provider by its well-known ARN rather than creating a second one (PipelineOidcStack owns provider creation), and the trust policy pins federation to this repo on `main`. The change is confined to `infra/stacks/web_stack.py`.

Watch for: the S3 permissions come from `grant_read_write` + `grant_delete`, which expand to a broader action set than the literal four actions named in the requirement (`s3:GetBucket*`, `s3:Abort*`, `s3:PutObject*` siblings) — all still scoped to the bucket ARN with no `s3:*` and no `*` resource (confirmed). This is a pre-approved design choice in the plan, not a defect.

**Verdict**: APPROVED

## High-level view

The role is added at the end of `WebStack.__init__`, alongside the existing CloudFormation outputs, and reuses the exact OIDC conventions established by PipelineOidcStack: the same `GITHUB_OIDC_URL` / `GITHUB_OIDC_AUDIENCE` / `DEFAULT_GITHUB_REPO` class constants, the same `githubRepo` context resolution, and the same `StringEquals`/`StringLike` trust conditions. This keeps the two deploy roles consistent and makes the frontend role easy to reason about against the backend one.

Provider ownership is handled correctly: WebStack imports the provider by the well-known ARN and never creates one, with a comment explaining that PipelineOidcStack is the single creator to avoid the one-provider-per-issuer-per-account conflict. The synthesized Web template contains zero OIDC provider resources, so there's no duplicate-provider risk.

The permission surface is least-privilege in the ways that matter: no `s3:*`, no `*` resources, and CloudFront invalidation scoped to the single distribution ARN. The one caveat is that the S3 grant helpers add sibling actions beyond the four the requirement names; these stay bucket-scoped, and the plan explicitly endorsed the helper approach, so it's an accepted trade-off rather than a gap.

Verification is credible. The coder installed a userspace Node + CDK toolchain, ran the authoritative stack-targeted `cdk synth`, inspected the synthesized template for every required property, and tested a `githubRepo` override. A narrow spot-check of the committed template confirmed the trust conditions, max session duration, S3 scoping, and CloudFront scoping match the recorded evidence.

<details>
<summary>Issues (1)</summary>

1. **S3 grant-helper action breadth** — `grant_read_write` + `grant_delete` emit `s3:GetBucket*`, `s3:Abort*`, and `s3:PutObject*` siblings beyond the four actions the requirement names. All are bucket-scoped with no wildcards on resources, and the plan pre-approved the helper approach. No action required; noted for awareness.

</details>

<details>
<summary>Details</summary>

### OIDC provider imported, never created

WebStack builds the provider ARN as `arn:aws:iam::{self.account}:oidc-provider/token.actions.githubusercontent.com` and imports it with `iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(self, "ImportedGitHubOidcProviderForWeb", provider_arn)`, wrapped in `iam.OpenIdConnectPrincipal`. The construct id matches the required `ImportedGitHubOidcProviderForWeb`, and the surrounding comment states that PipelineOidcStack owns provider creation because AWS allows only one provider per issuer URL per account. The spot-check confirmed the synthesized Web template has zero `AWS::IAM::OIDCProvider` / custom-resource provider nodes (confirmed), so there is no duplicate-provider conflict at deploy time.

The three class constants (`GITHUB_OIDC_URL`, `GITHUB_OIDC_AUDIENCE`, `DEFAULT_GITHUB_REPO`) are byte-for-byte identical to PipelineOidcStack, including the `#:` doc comments, and `github_repo` resolves via `self.node.try_get_context("githubRepo") or self.DEFAULT_GITHUB_REPO` (confirmed). PipelineOidcStack additionally supports a `createOidcProvider` flag to toggle create-vs-import; WebStack intentionally has no such branch because its role is always an importer.

### Trust conditions pin repo and branch

The principal applies `StringEquals` on `token.actions.githubusercontent.com:aud` = `sts.amazonaws.com` and `StringLike` on `token.actions.githubusercontent.com:sub` = `repo:{github_repo}:ref:refs/heads/main`, and the role sets `max_session_duration=cdk.Duration.hours(1)`. The committed template confirms these resolve to `aud = sts.amazonaws.com`, `sub = repo:your-org/recipemate:ref:refs/heads/main`, `Action: sts:AssumeRoleWithWebIdentity`, `Federated` = the imported provider ARN, and `MaxSessionDuration: 3600` (confirmed). The verification note's extra `-c githubRepo=acme/recipemate` run showing the `sub` condition tracking the override is a good check on the context plumbing.

### Permission scoping

S3 access is granted via `self._bucket.grant_read_write(...)` + `self._bucket.grant_delete(...)`. In the synthesized template this expands to one statement with `s3:Abort*`, `s3:DeleteObject*`, `s3:GetBucket*`, `s3:GetObject*`, `s3:List*`, `s3:PutObject`, `s3:PutObjectLegalHold`, `s3:PutObjectRetention`, `s3:PutObjectTagging`, `s3:PutObjectVersionTagging` on the bucket ARN and `bucket/*`, plus a second `s3:DeleteObject*` statement on `bucket/*` (confirmed). The four required actions — ListBucket, GetObject, PutObject, DeleteObject — are all present, there is no `s3:*`, and there is no `*` resource. The helper broadens the action set beyond the literal four (the `GetBucket*`/`Abort*`/`PutObject*` siblings), but everything stays scoped to this one bucket, and the plan explicitly named `grant_read_write` + `grant_delete` as the preferred implementation. Accepted trade-off, flagged for awareness only.

CloudFront is a single explicit statement with `cloudfront:CreateInvalidation` and `cloudfront:GetInvalidation` scoped to `arn:aws:cloudfront::{account}:distribution/{distribution_id}` — the empty region segment is correct for the global CloudFront ARN, and the resource is the specific distribution ref, not `*` (confirmed).

### Output, property, docstrings

The `WebDeployRoleArn` output carries `value=self._web_deploy_role.role_arn` and the operator-facing description "IAM role ARN for CI frontend deploys — store as the AWS_DEPLOY_ROLE_ARN GitHub secret", mirroring PipelineOidcStack's `BackendDeployRoleArn` wording. The public `web_deploy_role` property sits with the existing `bucket` and `distribution` properties, `aws_iam as iam` is added to the import block, and the module and class docstrings are updated with the role in "Creates:", `web_deploy_role` in "Properties exposed", and `WebDeployRoleArn` in "CloudFormation outputs" (confirmed).

### Scope containment

Only `infra/stacks/web_stack.py` was edited. PipelineOidcStack, the GitHub Actions workflow, and other stacks are untouched, matching requirement 9.

### Verification evidence

The verification note records installing a userspace Node 20 + `aws-cdk` toolchain (no root), then running the authoritative `cdk synth RecipeMate-Staging-Web -c env=staging -c githubRepo=your-org/recipemate` to exit 0, inspecting the synthesized template for the role name, trust policy, session duration, S3/CloudFront scoping, and the output description, and confirming no OIDC provider resource in the Web template. It also records a `githubRepo` override synth and a `py_compile` pre-check. The evidence is specific and tied to the committed `cdk.out` template. A narrow re-inspection of that template reproduced the trust conditions, `MaxSessionDuration: 3600`, bucket-scoped S3 actions with no `s3:*`/`*`, and the distribution-scoped CloudFront statement.

</details>

<details>
<summary>File map</summary>

- `infra/stacks/web_stack.py` — adds `aws_iam` import, three OIDC class constants, the imported-provider principal with repo/branch trust conditions, the `WebDeployRole` with S3 + CloudFront least-privilege grants, the `WebDeployRoleArn` output, the `web_deploy_role` property, and updated module/class docstrings.

Full change: `git diff main -- infra/stacks/web_stack.py`

</details>

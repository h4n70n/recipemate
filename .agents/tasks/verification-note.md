# Verification note — Frontend GitHub-OIDC deploy role in WebStack

Change: added the GitHub-OIDC-assumable frontend deploy role to
`infra/stacks/web_stack.py`. Only that file was edited.

## Environment setup (recorded for transparency)

The plan noted the CDK CLI and Node were absent in the planning environment.
That was still true here: `cdk`, `npx`, and `node` were all missing, and
`apt-get` required root (unavailable). To run the **authoritative** `cdk synth`
rather than falling back to `python app.py`, I installed a userspace toolchain
(no root):

```
# Node 20 prebuilt tarball into $HOME/.local/node
curl -fsSL https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-x64.tar.xz -o /tmp/node.tar.xz
mkdir -p $HOME/.local/node
tar -xJf /tmp/node.tar.xz -C $HOME/.local/node --strip-components=1
$HOME/.local/node/bin/node --version   # -> v20.18.1

# CDK CLI via the local npm
export PATH="$HOME/.local/node/bin:$PATH"
npm install -g aws-cdk
cdk --version   # -> 2.1144.0 (build b9c5274)
```

This means verification is the real, stack-targeted `cdk synth` — NOT the
reduced `python app.py` fallback.

## 1. Authoritative synth command and result

Run from `/home/dr1ftp1n/recipemate/infra` with the venv active and the local
Node on PATH:

```
source .venv/bin/activate
export PATH="$HOME/.local/node/bin:$PATH"
cdk synth RecipeMate-Staging-Web -c env=staging -c githubRepo=your-org/recipemate
```

Exit code: **0** (synth succeeded, template printed). The only stderr noise was
a Node 20 end-of-life advisory banner — not an error.

The synthesized template is at
`/home/dr1ftp1n/recipemate/infra/cdk.out/RecipeMate-Staging-Web.template.json`.

## 2. Template inspection (grep over the synthesized template)

Template: `cdk.out/RecipeMate-Staging-Web.template.json`

### AWS::IAM::Role with correct trust
- `"RoleName": "RecipeMate-Staging-Web-web-deploy"`
- `"MaxSessionDuration": 3600` (1 hour)
- `AssumeRolePolicyDocument`:
  - `Action: "sts:AssumeRoleWithWebIdentity"`
  - `Principal.Federated: "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"`
  - `StringEquals` on `token.actions.githubusercontent.com:aud` = `sts.amazonaws.com`
  - `StringLike` on `token.actions.githubusercontent.com:sub` = `repo:your-org/recipemate:ref:refs/heads/main`

### Attached IAM policy (WebDeployRoleDefaultPolicy)
S3 statement scoped to the WebBucket ARN and `<bucket-arn>/*` (via the
`grant_read_write` + `grant_delete` helpers), actions include:
`s3:List*` (covers ListBucket), `s3:GetObject*` (GetObject),
`s3:PutObject` (PutObject), `s3:DeleteObject*` (DeleteObject), plus the usual
Abort/GetBucket/PutObject* siblings the helper adds. A second statement adds
`s3:DeleteObject*` on `<bucket-arn>/*`.
No `s3:*` and no `*` resources present.

CloudFront statement:
- Actions: `cloudfront:CreateInvalidation`, `cloudfront:GetInvalidation`
- Resource: `arn:aws:cloudfront::111111111111:distribution/<WebDistribution ref>`
  (empty region segment), Sid `CloudFrontInvalidation`.

### WebDeployRoleArn output
Present with description:
`IAM role ARN for CI frontend deploys — store as the AWS_DEPLOY_ROLE_ARN GitHub secret`

## 3. No second OIDC provider in the Web template

```
grep -c "AWS::IAM::OIDCProvider" cdk.out/RecipeMate-Staging-Web.template.json
# -> 0  (ABSENT, as required — provider creation is owned by PipelineOidcStack)
```

The role trusts the provider by its imported well-known ARN; it does not create
a `Custom::AWSCDKOpenIdConnectProvider` / `AWS::IAM::OIDCProvider` resource.

## 4. githubRepo override sanity check (extra)

```
cdk synth RecipeMate-Staging-Web -c env=staging -c githubRepo=acme/recipemate
```
Exit code 0; the `sub` condition became
`repo:acme/recipemate:ref:refs/heads/main`, confirming the context override
flows through. The final cdk.out was re-synthesized with
`githubRepo=your-org/recipemate` afterward.

## Syntax check (pre-synth)

```
cd infra && .venv/bin/python -m py_compile stacks/web_stack.py   # exit 0
```

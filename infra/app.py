#!/usr/bin/env python3
"""
RecipeMate CDK App entry point.

Environments are selected via the CDK context key `env` or the
APP_ENV environment variable.  Supported values: staging, prod.

Per-environment settings (account, region, DB/cache sizing, API scaling,
removal-policy hint) live in ``infra/config.py``.  Values the existing stacks
already read from CDK context — ``multiAz`` (DatabaseStack) and
``redisNodeType`` (CacheStack) — are injected onto the app's context here so
the stacks pick them up without any constructor changes.  Settings the stacks
do not yet consume (``apiDesiredCount``, ``removalPolicy``) are also published
to context so they are available for future wiring and are documented below.

OR-9 (Domain & DNS: recipemate.me via Route 53) adds the following context
keys, published here so WebStack / ApiStack / AuthStack read them uniformly via
``self.node.try_get_context(...)`` with documented placeholder fallbacks:

  - ``domainName``          -> the apex domain. Default ``recipemate.me``.
                               WebStack serves it + ``www``; ApiStack serves
                               ``api.<domainName>``; AuthStack derives the OAuth
                               redirect URLs from it.
  - ``callbackUrls``        -> OAuth sign-in redirect URLs for the Cognito app
                               client. Default ``["https://<domainName>/callback"]``.
                               An explicit ``-c callbackUrls=...`` is NEVER
                               clobbered: the default is only set when the key is
                               absent, so additional iOS redirect URIs can be
                               supplied on the command line.
  - ``logoutUrls``          -> OAuth sign-out redirect URLs. Default
                               ``["https://<domainName>/logout"]``. Same
                               no-clobber rule as ``callbackUrls``.
  - ``cognitoDomainPrefix`` -> prefix for the default Cognito hosted-UI prefix
                               domain (``<prefix>.auth.<region>.amazoncognito.com``).
                               Default ``recipemate-<env>``. Ignored when
                               ``authCustomDomain`` is set.
  - ``authCustomDomain``    -> optional custom Cognito hosted-UI domain
                               (e.g. ``auth.<domainName>``). Unset by default;
                               supply via ``-c authCustomDomain=...`` to switch
                               AuthStack from the prefix domain to a custom
                               domain (its own us-east-1 cert + Route 53 alias).

These are context lookups, not AWS environment lookups: the placeholder
fallbacks keep synth logic from crashing when a key is absent. (The one true
AWS lookup OR-9 introduces is ``HostedZone.from_lookup`` inside the stacks,
which does require account credentials at synth time.)
"""
import os

import aws_cdk as cdk

from config import get_env_config
from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.cache_stack import CacheStack
from stacks.database_stack import DatabaseStack
from stacks.pipeline_oidc_stack import PipelineOidcStack
from stacks.queue_stack import QueueStack
from stacks.secrets_stack import SecretsStack
from stacks.storage_stack import StorageStack
from stacks.web_stack import WebStack

app = cdk.App()

# Resolve target environment: CDK context takes precedence, then APP_ENV, then staging.
env_name: str = app.node.try_get_context("env") or os.environ.get("APP_ENV", "staging")

# Validate and load the per-environment configuration. ``get_env_config``
# raises a clear ValueError listing the valid choices for an unknown env.
config = get_env_config(env_name)

# Build the CDK environment from the per-env account/region.
cdk_env = cdk.Environment(account=config.account, region=config.region)

# ---------------------------------------------------------------------- #
# Publish per-env values onto CDK context so the stacks consume them.    #
#                                                                         #
# The stacks read these keys via self.node.try_get_context(...):         #
#   - "multiAz"       -> DatabaseStack.multi_az                           #
#   - "redisNodeType" -> CacheStack.cache_node_type                       #
#                                                                         #
# These keys are published for future wiring (not yet read by a stack):  #
#   - "apiDesiredCount" -> intended ECS Fargate desired_count            #
#   - "removalPolicy"   -> "destroy" (staging) / "retain" (prod) hint    #
# ---------------------------------------------------------------------- #
app.node.set_context("multiAz", config.db_multi_az)
app.node.set_context("redisNodeType", config.redis_node_type)
app.node.set_context("apiDesiredCount", config.api_desired_count)
app.node.set_context("removalPolicy", config.removal_policy)

# ---------------------------------------------------------------------- #
# OR-9 — Domain & DNS context (recipemate.me via Route 53).              #
#                                                                         #
# Resolve the apex domain (``-c domainName=...`` wins; otherwise the      #
# ``recipemate.me`` default) and publish it so every stack reads a single #
# authoritative value. The OAuth redirect URLs default to the real domain #
# but ONLY when the operator has not already supplied them with           #
# ``-c callbackUrls``/``-c logoutUrls`` — set the default solely when the  #
# key is absent so an explicit value (e.g. extra iOS redirect URIs) is     #
# never overwritten. The Cognito hosted-UI prefix defaults to a per-env    #
# ``recipemate-<env>`` string; ``authCustomDomain`` is intentionally left  #
# unset so AuthStack uses the simpler prefix domain by default.            #
# ---------------------------------------------------------------------- #
domain_name: str = app.node.try_get_context("domainName") or "recipemate.me"
app.node.set_context("domainName", domain_name)

if app.node.try_get_context("callbackUrls") is None:
    app.node.set_context("callbackUrls", [f"https://{domain_name}/callback"])
if app.node.try_get_context("logoutUrls") is None:
    app.node.set_context("logoutUrls", [f"https://{domain_name}/logout"])
if app.node.try_get_context("cognitoDomainPrefix") is None:
    app.node.set_context("cognitoDomainPrefix", f"recipemate-{config.name}")

prefix = f"RecipeMate-{config.name.capitalize()}"

database_stack = DatabaseStack(app, f"{prefix}-Database", env=cdk_env)
storage_stack = StorageStack(app, f"{prefix}-Storage", env=cdk_env)
secrets_stack = SecretsStack(
    app, f"{prefix}-Secrets", env=cdk_env, env_name=config.name
)
queue_stack = QueueStack(
    app,
    f"{prefix}-Queue",
    env=cdk_env,
    openai_secret=secrets_stack.openai_secret,
    env_name=config.name,
)
auth_stack = AuthStack(app, f"{prefix}-Auth", env=cdk_env)
cache_stack = CacheStack(app, f"{prefix}-Cache", env=cdk_env)
# ApiStack reads DATABASE_URL parts, the OpenAI key, and the Flask SECRET_KEY
# from Secrets Manager at runtime. Threading the ISecret references here
# (rather than looking them up by name) both wires the ecs.Secret injections
# and establishes the cross-stack dependency edges, so CloudFormation builds
# DatabaseStack and SecretsStack before ApiStack.
api_stack = ApiStack(
    app,
    f"{prefix}-Api",
    env=cdk_env,
    db_secret=database_stack.secret,
    openai_secret=secrets_stack.openai_secret,
    flask_secret=secrets_stack.flask_secret,
)
# Static hosting (S3 + CloudFront) for the Vite/React web client. The CI
# pipeline syncs frontend/dist into this bucket and invalidates the
# distribution; its outputs (bucket name, distribution id) feed the deploy vars.
web_stack = WebStack(app, f"{prefix}-Web", env=cdk_env)
# GitHub OIDC trust + least-privilege backend deploy role so CI can run CDK,
# push to ECR, and roll the ECS service without static AWS keys. Repo and
# provider-creation behaviour are set via CDK context (githubRepo,
# createOidcProvider); the role ARN is emitted for the GitHub secret.
pipeline_oidc_stack = PipelineOidcStack(
    app, f"{prefix}-PipelineOidc", env=cdk_env
)

app.synth()

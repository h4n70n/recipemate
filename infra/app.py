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
"""
import os

import aws_cdk as cdk

from config import get_env_config
from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.cache_stack import CacheStack
from stacks.database_stack import DatabaseStack
from stacks.pipeline_oidc_stack import PipelineOidcStack
from stacks.pipeline_stack import PipelineStack
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
# Unified CI/CD CodePipeline (OR-3 / TASK-8.4): ONE pipeline per env that builds
# and deploys BOTH backend and frontend from a single GitHub source (CodeStar
# connection on main). Threading the ApiStack ECR repo + cluster + service,
# WebStack bucket + distribution, and the Database/Secrets secret references as
# keyword-only args both wires the deploy-time resources and establishes the
# cross-stack dependency edges, so CloudFormation builds those stacks first. The
# CodeStar connection ARN is supplied via `-c codestarConnectionArn=...` (the
# operator creates the GitHub connection once); a documented placeholder lets
# synth run without it.
pipeline_stack = PipelineStack(
    app,
    f"{prefix}-Pipeline",
    env=cdk_env,
    env_name=config.name,
    ecr_repo=api_stack.ecr_repo,
    cluster=api_stack.cluster,
    service=api_stack.service,
    web_bucket=web_stack.bucket,
    web_distribution=web_stack.distribution,
    db_secret=database_stack.secret,
    flask_secret=secrets_stack.flask_secret,
    openai_secret=secrets_stack.openai_secret,
)

app.synth()

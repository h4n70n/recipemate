"""
PipelineOidcStack: GitHub OIDC trust and a least-privilege backend deploy role.

CI (GitHub Actions / CodeBuild source) needs to deploy the backend — run CDK,
push container images to ECR, and roll the ECS service — without any static AWS
keys. This mirrors the OIDC pattern the frontend workflow already uses
(``aws-actions/configure-aws-credentials@v4`` assuming an IAM role) and extends
it to the backend/CDK/ECR/ECS path.

The stack provisions:

  1. A GitHub OpenID Connect identity provider for
     ``https://token.actions.githubusercontent.com`` with the
     ``sts.amazonaws.com`` audience. An AWS account may only have one OIDC
     provider per issuer URL, so creation is guarded by the ``createOidcProvider``
     CDK context flag — pass ``-c createOidcProvider=false`` to import an
     existing provider instead of creating a second one.
  2. A deploy IAM role assumable only via that provider, with its trust policy
     restricted to this GitHub repository and the ``main`` branch
     (``repo:<owner>/<name>:ref:refs/heads/main``). The repository is
     parameterised via the ``githubRepo`` context key
     (``-c githubRepo=owner/recipemate``) with a documented placeholder default.
     Permissions follow the modern CDK bootstrap pattern: assume the
     ``cdk-*`` bootstrap roles rather than granting broad admin, plus the ECR
     push/pull and ECS ``UpdateService`` actions the deploy needs.

The role ARN is emitted as a ``CfnOutput`` so an operator can store it as the
``AWS_BACKEND_DEPLOY_ROLE_ARN`` GitHub secret (see ``docs/configuration.md``).
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_iam as iam,
)
from constructs import Construct


class PipelineOidcStack(Stack):
    """
    Creates:
      - A GitHub OIDC identity provider (optional — guarded by the
        ``createOidcProvider`` context flag so an account that already has one
        imports it instead of failing on a duplicate).
      - A least-privilege backend deploy role trusted only by this repo on
        ``main``, scoped to assume the ``cdk-*`` bootstrap roles plus ECR and
        ECS deploy actions.

    Properties exposed for other stacks / CI:
      - ``deploy_role`` – the IAM Role CI assumes via OIDC.

    CloudFormation outputs:
      - ``BackendDeployRoleArn`` – store as the ``AWS_BACKEND_DEPLOY_ROLE_ARN``
        GitHub secret.
    """

    #: GitHub's OIDC token issuer.
    GITHUB_OIDC_URL: str = "https://token.actions.githubusercontent.com"
    #: Audience AWS STS expects for GitHub OIDC federation.
    GITHUB_OIDC_AUDIENCE: str = "sts.amazonaws.com"
    #: Documented placeholder used when ``githubRepo`` context is not supplied,
    #: so synth never crashes when the optional context is absent.
    DEFAULT_GITHUB_REPO: str = "your-org/recipemate"

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # ------------------------------------------------------------------ #
        # Context resolution (try_get_context with safe fallbacks)            #
        # ------------------------------------------------------------------ #
        # GitHub repo in ``owner/name`` form, parameterised so each account can
        # target its own fork without editing source. Falls back to a
        # documented placeholder so synth works without the context present.
        github_repo = (
            self.node.try_get_context("githubRepo") or self.DEFAULT_GITHUB_REPO
        )

        # One OIDC provider is allowed per issuer URL per account. When the
        # account already has one, deploy with ``-c createOidcProvider=false``
        # to import the existing provider instead of creating a duplicate.
        # Context values arrive as strings, so treat the literal "false"
        # (any case) as False; default to creating the provider.
        create_provider_ctx = self.node.try_get_context("createOidcProvider")
        create_provider = str(create_provider_ctx).lower() != "false"

        # ------------------------------------------------------------------ #
        # GitHub OIDC identity provider (create or import)                    #
        # ------------------------------------------------------------------ #
        if create_provider:
            provider = iam.OpenIdConnectProvider(
                self,
                "GitHubOidcProvider",
                url=self.GITHUB_OIDC_URL,
                client_ids=[self.GITHUB_OIDC_AUDIENCE],
            )
            provider_arn = provider.open_id_connect_provider_arn
        else:
            # Import the account's existing provider by its well-known ARN.
            provider_arn = (
                f"arn:aws:iam::{self.account}:oidc-provider/"
                "token.actions.githubusercontent.com"
            )

        # ------------------------------------------------------------------ #
        # Trust policy — restrict to this repo and the main branch            #
        # ------------------------------------------------------------------ #
        # The ``sub`` condition pins federation to pushes on ``main`` of the
        # configured repository; ``aud`` pins the STS audience. Together these
        # stop any other repo (or branch) from assuming the role.
        oidc_principal = iam.OpenIdConnectPrincipal(
            iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
                self, "ImportedGitHubOidcProvider", provider_arn
            ),
            conditions={
                "StringEquals": {
                    "token.actions.githubusercontent.com:aud": (
                        self.GITHUB_OIDC_AUDIENCE
                    ),
                },
                "StringLike": {
                    "token.actions.githubusercontent.com:sub": (
                        f"repo:{github_repo}:ref:refs/heads/main"
                    ),
                },
            },
        )

        # ------------------------------------------------------------------ #
        # Least-privilege backend deploy role                                 #
        # ------------------------------------------------------------------ #
        self._deploy_role = iam.Role(
            self,
            "BackendDeployRole",
            role_name=f"{self.stack_name}-backend-deploy",
            assumed_by=oidc_principal,
            description=(
                "Backend CI deploy role (CDK + ECR + ECS) assumed via GitHub "
                f"OIDC from repo {github_repo} on main"
            ),
            max_session_duration=cdk.Duration.hours(1),
        )

        # CDK deploys through the modern bootstrap pattern: the CLI assumes the
        # account's ``cdk-*`` roles (deploy / file-publishing / image-publishing
        # / lookup). Scope ``sts:AssumeRole`` to those roles rather than
        # granting broad admin.
        self._deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCdkBootstrapRoles",
                actions=["sts:AssumeRole"],
                resources=[f"arn:aws:iam::{self.account}:role/cdk-*"],
            )
        )

        # ECR: authorise, then push/pull image layers. ``GetAuthorizationToken``
        # is account-wide (it takes no resource), so it is a separate statement.
        self._deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrAuth",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        self._deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrPushPull",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                ],
                resources=[
                    f"arn:aws:ecr:{self.region}:{self.account}:repository/*"
                ],
            )
        )

        # ECS: roll the Fargate service to the freshly pushed image.
        self._deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcsUpdateService",
                actions=[
                    "ecs:UpdateService",
                    "ecs:DescribeServices",
                ],
                resources=["*"],
            )
        )

        # ------------------------------------------------------------------ #
        # Outputs                                                             #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "BackendDeployRoleArn",
            value=self._deploy_role.role_arn,
            description=(
                "IAM role ARN for CI backend deploys — store as the "
                "AWS_BACKEND_DEPLOY_ROLE_ARN GitHub secret"
            ),
        )

    # ---------------------------------------------------------------------- #
    # Public properties                                                       #
    # ---------------------------------------------------------------------- #

    @property
    def deploy_role(self) -> iam.Role:
        """The IAM role CI assumes via GitHub OIDC to deploy the backend."""
        return self._deploy_role

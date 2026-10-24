"""
PipelineStack: one AWS CodePipeline that builds and deploys BOTH the RecipeMate
backend (Flask API on ECS Fargate) and frontend (Vite/React SPA on S3 +
CloudFront) from a single GitHub source. Implements OR-3 / TASK-8.4.

This is the unified replacement for the standalone ``deploy-frontend.yml``
GitHub Actions workflow: GitHub is *source only* via a CodeStar connection, and
all build/test/deploy work runs inside CodeBuild projects owned by this stack so
there are no static AWS keys anywhere — CodeBuild assumes IAM roles created and
scoped here.

Stage flow (per environment, one pipeline each):

    Source   -> CodeStar connection on ``main`` (operator creates the GitHub
                connection once in the console/CLI and supplies its ARN via
                ``-c codestarConnectionArn=...``).
    Build    -> two parallel CodeBuild projects:
                  * backend  — ``docker build`` the repo-root Dockerfile and
                    push to the ApiStack ECR repo tagged with the commit SHA and
                    ``latest`` (privileged Docker build environment).
                  * frontend — ``npm ci && npm run typecheck && npm run build``
                    with ``VITE_*`` values sourced from SSM Parameter Store
                    (never hardcoded); emits ``frontend/dist`` as an artifact.
    Test     -> a CodeBuild project running the pytest suite. A non-zero pytest
                exit fails the stage (CodeBuild propagates it), which blocks all
                downstream deploy stages so red tests never ship.
    Approval -> (PROD ONLY) a manual approval action gating the deploys. Staging
                deploys automatically; prod waits for a human.
    DeployBackend  -> a CodeBuild project that runs ``alembic upgrade head``
                      (DATABASE_URL composed at runtime from the DatabaseStack
                      secret, never baked in), then pins the deploy to the
                      *immutable commit-SHA image* the Build stage produced: it
                      reads ``image-tag.txt`` from the backend build artifact,
                      pulls the live task definition, rewrites its container
                      image to ``$ECR_REPO_URI:$IMAGE_TAG``, registers a NEW
                      task-definition revision, and points the service at that
                      exact revision with ``aws ecs update-service
                      --task-definition <newArn>``. It then polls the
                      deployment's rollout state (with a generous timeout) until
                      the primary deployment is ``COMPLETED`` and the ALB
                      ``/health`` target group is healthy. Pinning to the SHA
                      (rather than force-deploying ``:latest``) keeps every
                      deploy traceable and rollback-able and removes the race
                      where two overlapping runs fight over the ``:latest`` tag.
    DeployFrontend -> a CodeBuild project that ``aws s3 sync frontend/dist`` into
                      the WebStack bucket (``--delete``) and creates a CloudFront
                      invalidation on the WebStack distribution.

Buildspecs are defined INLINE via ``aws_codebuild.BuildSpec.from_object`` rather
than committed ``buildspec.yml`` files. This keeps the whole pipeline — build
commands, env vars, artifacts — in one reviewable place and matches the
self-contained style of the other stacks (ApiStack, WebStack, PipelineOidc).

All IAM is least-privilege: ECR via ``grant_pull_push``, S3 via
``grant_read_write`` + ``grant_delete``, CloudFront invalidation scoped to the
distribution ARN (mirroring ``WebStack.web_deploy_role``), and ECS / Secrets
Manager scoped to the specific cluster/service/secret ARNs — no ``*`` resources
except where the AWS API itself is account-wide (``ecr:GetAuthorizationToken``).

Region is inherited from the stack ``env`` passed by ``infra/app.py`` (us-east-2
for every environment); no region strings are hardcoded here.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_codebuild as codebuild,
    aws_codepipeline as codepipeline,
    aws_codepipeline_actions as codepipeline_actions,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_cloudfront as cloudfront,
    aws_iam as iam,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class PipelineStack(Stack):
    """
    Creates:
      - A CodePipeline artifact bucket (CDK-managed) whose removal policy
        follows the per-env ``removalPolicy`` context hint (DESTROY for staging,
        RETAIN for prod — matching WebStack).
      - A Source stage using a CodeStar GitHub connection on ``main``.
      - A Build stage with two CodeBuild projects (backend Docker build + push,
        frontend npm build) running in parallel.
      - A Test stage running the pytest suite (red fails the pipeline).
      - A manual-approval action (PROD pipeline only) gating the deploy stages.
      - A Backend Deploy stage (alembic migrate, roll ECS, wait for health).
      - A Frontend Deploy stage (s3 sync + CloudFront invalidation).

    Properties exposed:
      - ``pipeline`` – the CodePipeline construct.

    Keyword-only cross-stack references (supplied by ``infra/app.py``):
      - ``env_name``        – "staging" or "prod"; selects per-env behaviour
        (e.g. the prod-only manual approval).
      - ``ecr_repo``        – ApiStack ECR repo the backend image is pushed to.
      - ``cluster``         – ApiStack ECS cluster (for the service roll).
      - ``service``         – ApiStack ECS Fargate service to roll.
      - ``task_definition`` – ApiStack Fargate task definition; the deploy reads
        its family and re-registers a new revision pinned to the SHA image.
      - ``task_role``       – ApiStack ECS task role; ``iam:PassRole`` is scoped
        to it so the deploy can register a new task-def revision.
      - ``execution_role``  – ApiStack ECS task execution role; ``iam:PassRole``
        is scoped to it for the same reason.
      - ``web_bucket``      – WebStack S3 bucket the frontend syncs into.
      - ``web_distribution``– WebStack CloudFront distribution to invalidate.
      - ``db_secret``       – DatabaseStack RDS credentials secret; read at
        deploy time to compose DATABASE_URL for the Alembic migration.
      - ``flask_secret`` / ``openai_secret`` – SecretsStack secrets, accepted
        for completeness/future deploy-time needs (the migration only needs the
        DB secret, so these are optional and currently not granted).

    CloudFormation outputs (operator reference):
      - ``PipelineName``             – the CodePipeline name.
      - ``CodeStarConnectionArn``    – echoed back so the operator can confirm
        which GitHub connection the pipeline uses.
      - ``BackendBuildProjectName``  – backend Docker build/push project.
      - ``FrontendBuildProjectName`` – frontend npm build project.
      - ``TestProjectName``          – pytest project.
      - ``BackendDeployProjectName`` – migrate + ECS roll project.
      - ``FrontendDeployProjectName``– s3 sync + invalidation project.
    """

    #: Documented placeholder used when ``githubRepo`` context is not supplied,
    #: so synth never crashes when the optional context is absent.
    DEFAULT_GITHUB_REPO: str = "your-org/recipemate"

    #: Documented placeholder CodeStar connection ARN used when
    #: ``codestarConnectionArn`` context is absent. The operator creates the
    #: GitHub connection once (console/CLI) and passes its real ARN via
    #: ``-c codestarConnectionArn=arn:aws:codestar-connections:...``. The
    #: placeholder lets synth succeed without the real value.
    DEFAULT_CODESTAR_CONNECTION_ARN: str = (
        "arn:aws:codestar-connections:us-east-2:111111111111:connection/"
        "00000000-0000-0000-0000-000000000000"
    )

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        env_name: str = "staging",
        ecr_repo: ecr.IRepository,
        cluster: ecs.ICluster,
        service: ecs.IBaseService,
        task_definition: ecs.FargateTaskDefinition,
        task_role: iam.IRole,
        execution_role: iam.IRole,
        web_bucket: s3.IBucket,
        web_distribution: cloudfront.IDistribution,
        db_secret: secretsmanager.ISecret,
        flask_secret: secretsmanager.ISecret | None = None,
        openai_secret: secretsmanager.ISecret | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # ------------------------------------------------------------------ #
        # Context resolution (try_get_context with documented fallbacks)     #
        # ------------------------------------------------------------------ #
        # GitHub repo in ``owner/name`` form. Falls back to a documented
        # placeholder so synth works without the context present.
        github_repo = (
            self.node.try_get_context("githubRepo") or self.DEFAULT_GITHUB_REPO
        )
        # ``owner/name`` -> owner, name. The CodeStar source action takes them
        # separately. ``split("/", 1)`` tolerates a repo name that itself holds
        # no extra slash while still giving a clean two-part split.
        owner, _, repo_name = github_repo.partition("/")

        # The CodeStar GitHub connection ARN. The operator creates the
        # connection once out of band and supplies its ARN here; the documented
        # placeholder keeps synth working without it.
        codestar_connection_arn = (
            self.node.try_get_context("codestarConnectionArn")
            or self.DEFAULT_CODESTAR_CONNECTION_ARN
        )

        # Removal policy — honour the per-env hint published by app.py
        # ("destroy" staging / "retain" prod). Mirrors WebStack. Applied to the
        # artifact bucket below.
        removal_hint = self.node.try_get_context("removalPolicy")
        removal_policy = (
            cdk.RemovalPolicy.DESTROY
            if removal_hint == "destroy"
            else cdk.RemovalPolicy.RETAIN
        )
        auto_delete_artifacts = removal_policy == cdk.RemovalPolicy.DESTROY

        # ------------------------------------------------------------------ #
        # Artifact bucket — explicit so its removal policy tracks the env.    #
        #                                                                     #
        # Letting CodePipeline create its own bucket would leave it RETAINed  #
        # by default; an explicit bucket lets staging tear down cleanly while #
        # prod retains artifacts. All public access blocked, SSE-S3.          #
        # ------------------------------------------------------------------ #
        artifact_bucket = s3.Bucket(
            self,
            "PipelineArtifacts",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=removal_policy,
            auto_delete_objects=auto_delete_artifacts,
        )

        # ------------------------------------------------------------------ #
        # Artifacts flowing between stages.                                   #
        # ------------------------------------------------------------------ #
        source_output = codepipeline.Artifact("SourceOutput")
        # Backend build emits imagedefinitions/image-tag metadata so the deploy
        # stage knows which SHA tag to roll to.
        backend_build_output = codepipeline.Artifact("BackendBuildOutput")
        # Frontend build emits the compiled ``frontend/dist`` for the deploy
        # stage to sync to S3.
        frontend_build_output = codepipeline.Artifact("FrontendBuildOutput")

        # ------------------------------------------------------------------ #
        # Backend build project — Docker build + push to ECR (privileged).    #
        # ------------------------------------------------------------------ #
        # ``privileged=True`` is required to run the Docker daemon inside
        # CodeBuild. The build logs into ECR, builds the repo-root Dockerfile,
        # and pushes both the commit-SHA tag and ``latest``. The SHA tag is the
        # immutable artifact the deploy stage rolls to; ``latest`` tracks it so
        # the ECS task definition's ``:latest`` reference also updates.
        backend_build_project = codebuild.PipelineProject(
            self,
            "BackendBuild",
            project_name=f"{self.stack_name}-backend-build",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                privileged=True,
            ),
            environment_variables={
                # Repo URI + region/account for the ECR login + push. Non-secret.
                "ECR_REPO_URI": codebuild.BuildEnvironmentVariable(
                    value=ecr_repo.repository_uri
                ),
                "AWS_ACCOUNT_ID": codebuild.BuildEnvironmentVariable(
                    value=self.account
                ),
                "AWS_DEFAULT_REGION": codebuild.BuildEnvironmentVariable(
                    value=self.region
                ),
            },
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "pre_build": {
                            "commands": [
                                "echo Logging in to Amazon ECR...",
                                (
                                    "aws ecr get-login-password "
                                    "--region $AWS_DEFAULT_REGION | docker login "
                                    "--username AWS --password-stdin "
                                    "$AWS_ACCOUNT_ID.dkr.ecr."
                                    "$AWS_DEFAULT_REGION.amazonaws.com"
                                ),
                                # CodeBuild resolves the source commit SHA here;
                                # it becomes the immutable image tag.
                                "export IMAGE_TAG="
                                "$CODEBUILD_RESOLVED_SOURCE_VERSION",
                            ]
                        },
                        "build": {
                            "commands": [
                                "echo Building the backend image...",
                                (
                                    "docker build -t $ECR_REPO_URI:$IMAGE_TAG "
                                    "-t $ECR_REPO_URI:latest ."
                                ),
                            ]
                        },
                        "post_build": {
                            "commands": [
                                "echo Pushing the backend image...",
                                "docker push $ECR_REPO_URI:$IMAGE_TAG",
                                "docker push $ECR_REPO_URI:latest",
                                # imagedefinitions.json is the standard ECS
                                # deploy artifact; image-tag.txt carries the raw
                                # SHA for the deploy stage's force-deploy.
                                (
                                    'printf \'[{"name":"FlaskApiContainer",'
                                    '"imageUri":"%s"}]\' '
                                    "$ECR_REPO_URI:$IMAGE_TAG "
                                    "> imagedefinitions.json"
                                ),
                                "echo $IMAGE_TAG > image-tag.txt",
                            ]
                        },
                    },
                    "artifacts": {
                        "files": ["imagedefinitions.json", "image-tag.txt"],
                    },
                }
            ),
        )
        # Least-privilege ECR: build/push to just this repo (plus the account-
        # wide GetAuthorizationToken the helper adds).
        ecr_repo.grant_pull_push(backend_build_project)

        # ------------------------------------------------------------------ #
        # Frontend build project — npm ci + typecheck + build.                #
        # ------------------------------------------------------------------ #
        # VITE_* build config comes from SSM Parameter Store (type
        # PARAMETER_STORE), never hardcoded. The operator seeds these string
        # parameters per env; the names mirror the GitHub Actions ``vars.*``
        # the retiring deploy-frontend.yml used:
        #   /recipemate/<env>/frontend/VITE_API_BASE_URL
        #   /recipemate/<env>/frontend/VITE_COGNITO_USER_POOL_ID
        #   /recipemate/<env>/frontend/VITE_COGNITO_CLIENT_ID
        #   /recipemate/<env>/frontend/VITE_COGNITO_DOMAIN
        #   /recipemate/<env>/frontend/VITE_COGNITO_REGION
        #   /recipemate/<env>/frontend/VITE_REDIRECT_SIGN_IN
        #   /recipemate/<env>/frontend/VITE_REDIRECT_SIGN_OUT
        ssm_prefix = f"/recipemate/{env_name}/frontend"
        vite_param_names = [
            "VITE_API_BASE_URL",
            "VITE_COGNITO_USER_POOL_ID",
            "VITE_COGNITO_CLIENT_ID",
            "VITE_COGNITO_DOMAIN",
            "VITE_COGNITO_REGION",
            "VITE_REDIRECT_SIGN_IN",
            "VITE_REDIRECT_SIGN_OUT",
        ]
        frontend_env_vars = {
            name: codebuild.BuildEnvironmentVariable(
                value=f"{ssm_prefix}/{name}",
                type=codebuild.BuildEnvironmentVariableType.PARAMETER_STORE,
            )
            for name in vite_param_names
        }

        frontend_build_project = codebuild.PipelineProject(
            self,
            "FrontendBuild",
            project_name=f"{self.stack_name}-frontend-build",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
            ),
            environment_variables=frontend_env_vars,
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "install": {
                            "runtime-versions": {"nodejs": 20},
                            "commands": ["cd frontend", "npm ci"],
                        },
                        "build": {
                            "commands": [
                                "npm run typecheck",
                                # VITE_* are already present in the environment
                                # via PARAMETER_STORE resolution above; Vite
                                # reads them at build time.
                                "npm run build",
                            ]
                        },
                    },
                    "artifacts": {
                        # Emit the compiled SPA for the frontend deploy stage.
                        "base-directory": "frontend/dist",
                        "files": ["**/*"],
                    },
                }
            ),
        )
        # Allow the project to read exactly the VITE_* parameters for this env.
        frontend_build_project.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadFrontendBuildParams",
                actions=["ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter"
                    f"{ssm_prefix}/{name}"
                    for name in vite_param_names
                ],
            )
        )

        # ------------------------------------------------------------------ #
        # Test project — pytest suite. Red exit fails the stage/pipeline.     #
        # ------------------------------------------------------------------ #
        test_project = codebuild.PipelineProject(
            self,
            "TestSuite",
            project_name=f"{self.stack_name}-test",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
            ),
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "install": {
                            "runtime-versions": {"python": "3.11"},
                            "commands": [
                                "pip install -r requirements.txt",
                                "pip install -r requirements-dev.txt",
                            ],
                        },
                        "build": {
                            # A non-zero pytest exit propagates as a FAILED
                            # build, which fails the Test stage and halts the
                            # pipeline before any deploy.
                            "commands": ["pytest"],
                        },
                    },
                }
            ),
        )

        # ------------------------------------------------------------------ #
        # Backend deploy project — migrate, roll ECS, wait for health.        #
        # ------------------------------------------------------------------ #
        # DATABASE_URL is composed at runtime from the DatabaseStack secret
        # (DB_SECRET_ARN env var -> secretsmanager get-secret-value), never
        # baked into the template.
        #
        # The rollout pins the service to the IMMUTABLE commit-SHA image the
        # Build stage pushed, instead of force-deploying ``:latest``:
        #   1. read $IMAGE_TAG from the backend build artifact's image-tag.txt;
        #   2. describe the live task definition (family resolved from
        #      $TASK_DEF_FAMILY) and, with jq, rewrite its single container's
        #      image to $ECR_REPO_URI:$IMAGE_TAG while stripping the read-only
        #      fields the register API rejects;
        #   3. register a NEW task-definition revision and capture its ARN;
        #   4. point the service at that exact revision with
        #      ``aws ecs update-service --task-definition <newArn>``.
        # This makes every deploy traceable to a commit, gives a clean rollback
        # target (the prior revision), and removes the race where two
        # overlapping runs clobber the shared ``:latest`` tag.
        #
        # ROLLOUT_TIMEOUT_SECONDS replaces ``aws ecs wait services-stable`` —
        # that waiter has a fixed ~10-minute budget (40 polls x 15s) and would
        # fail a slow-but-healthy rollout. We poll the service's primary
        # deployment ``rolloutState`` ourselves until COMPLETED, with a longer
        # bound that cold Fargate starts (60s health-check start_period + image
        # pull) comfortably fit inside.
        backend_deploy_project = codebuild.PipelineProject(
            self,
            "BackendDeploy",
            project_name=f"{self.stack_name}-backend-deploy",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
            ),
            environment_variables={
                "DB_SECRET_ARN": codebuild.BuildEnvironmentVariable(
                    value=db_secret.secret_arn
                ),
                "ECS_CLUSTER": codebuild.BuildEnvironmentVariable(
                    value=cluster.cluster_name
                ),
                "ECS_SERVICE": codebuild.BuildEnvironmentVariable(
                    value=service.service_name
                ),
                # Task-def family (not the full ARN) — describe-task-definition
                # resolves the latest ACTIVE revision from the family name.
                "TASK_DEF_FAMILY": codebuild.BuildEnvironmentVariable(
                    value=task_definition.family
                ),
                # Container name whose image we rewrite — matches ApiStack.
                "CONTAINER_NAME": codebuild.BuildEnvironmentVariable(
                    value="FlaskApiContainer"
                ),
                "ECR_REPO_URI": codebuild.BuildEnvironmentVariable(
                    value=ecr_repo.repository_uri
                ),
                # Explicit rollout bound (seconds) for our own poll loop.
                "ROLLOUT_TIMEOUT_SECONDS": codebuild.BuildEnvironmentVariable(
                    value="1200"
                ),
                "AWS_DEFAULT_REGION": codebuild.BuildEnvironmentVariable(
                    value=self.region
                ),
            },
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "install": {
                            "runtime-versions": {"python": "3.11"},
                            "commands": [
                                "pip install -r requirements.txt",
                                # jq parses the DB secret JSON into DB_* parts.
                                "command -v jq >/dev/null 2>&1 || "
                                "(apt-get update && apt-get install -y jq)",
                            ],
                        },
                        "pre_build": {
                            "commands": [
                                "echo Composing DATABASE_URL from Secrets "
                                "Manager (never printed)...",
                                # Fetch the secret JSON at runtime and build the
                                # SQLAlchemy URL config.py / migrations expect.
                                (
                                    "SECRET_JSON=$(aws secretsmanager "
                                    "get-secret-value --secret-id "
                                    "$DB_SECRET_ARN --query SecretString "
                                    "--output text)"
                                ),
                                'DB_USER=$(echo "$SECRET_JSON" | jq -r .username)',
                                'DB_PASSWORD=$(echo "$SECRET_JSON" | jq -r .password)',
                                'DB_HOST=$(echo "$SECRET_JSON" | jq -r .host)',
                                'DB_PORT=$(echo "$SECRET_JSON" | jq -r .port)',
                                'DB_NAME=$(echo "$SECRET_JSON" | jq -r .dbname)',
                                (
                                    "export DATABASE_URL="
                                    '"postgresql+psycopg2://$DB_USER:'
                                    '$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME"'
                                ),
                            ]
                        },
                        "build": {
                            "commands": [
                                "set -euo pipefail",
                                "echo Running database migrations...",
                                "alembic upgrade head",
                                # ---- Pin the deploy to the immutable SHA tag ---
                                "echo Resolving the built image tag...",
                                # image-tag.txt comes from the backend build
                                # artifact (CODEBUILD_SRC_DIR_BackendBuildOutput
                                # points at that secondary source root).
                                'IMAGE_TAG="$(cat '
                                "$CODEBUILD_SRC_DIR_BackendBuildOutput/"
                                'image-tag.txt)"',
                                'NEW_IMAGE="$ECR_REPO_URI:$IMAGE_TAG"',
                                'echo "Deploying image $NEW_IMAGE"',
                                "echo Fetching the current task definition...",
                                (
                                    "TASK_DEF_JSON=$(aws ecs "
                                    "describe-task-definition "
                                    "--task-definition $TASK_DEF_FAMILY "
                                    "--query taskDefinition)"
                                ),
                                # Build the register-task-definition input:
                                # swap the container image and drop the
                                # server-managed fields the API rejects.
                                (
                                    'NEW_TASK_DEF=$(echo "$TASK_DEF_JSON" | jq '
                                    '--arg IMAGE "$NEW_IMAGE" '
                                    '--arg NAME "$CONTAINER_NAME" '
                                    "'(.containerDefinitions[] | "
                                    'select(.name == $NAME) | .image) = $IMAGE '
                                    "| del(.taskDefinitionArn, .revision, "
                                    ".status, .requiresAttributes, "
                                    ".compatibilities, .registeredAt, "
                                    ".registeredBy)')"
                                ),
                                "echo Registering the new task definition "
                                "revision...",
                                (
                                    "NEW_TASK_DEF_ARN=$(aws ecs "
                                    "register-task-definition "
                                    '--cli-input-json "$NEW_TASK_DEF" '
                                    "--query taskDefinition.taskDefinitionArn "
                                    "--output text)"
                                ),
                                'echo "Registered $NEW_TASK_DEF_ARN"',
                                "echo Pointing the service at the new "
                                "revision...",
                                (
                                    "aws ecs update-service --cluster "
                                    "$ECS_CLUSTER --service $ECS_SERVICE "
                                    '--task-definition "$NEW_TASK_DEF_ARN"'
                                ),
                                # ---- Explicit rollout poll (replaces the ----
                                # ---- fixed-budget `ecs wait services-stable`) -
                                "echo Waiting for the deployment to complete "
                                "(ALB /health must pass)...",
                                (
                                    "DEADLINE=$(( $(date +%s) + "
                                    "ROLLOUT_TIMEOUT_SECONDS ))"
                                ),
                                "while true; do "
                                "STATE=$(aws ecs describe-services "
                                "--cluster $ECS_CLUSTER "
                                "--services $ECS_SERVICE "
                                "--query \"services[0].deployments[?status=='PRIMARY']"
                                '.rolloutState | [0]" --output text); '
                                'echo "rolloutState=$STATE"; '
                                'if [ "$STATE" = "COMPLETED" ]; then '
                                'echo "Deployment completed."; break; fi; '
                                'if [ "$STATE" = "FAILED" ]; then '
                                'echo "Deployment failed." >&2; exit 1; fi; '
                                "if [ $(date +%s) -ge $DEADLINE ]; then "
                                'echo "Timed out after '
                                '${ROLLOUT_TIMEOUT_SECONDS}s waiting for a '
                                "stable deployment.\" >&2; exit 1; fi; "
                                "sleep 15; "
                                "done",
                            ]
                        },
                    },
                }
            ),
        )
        # Least-privilege: read only the DB secret.
        db_secret.grant_read(backend_deploy_project)
        # UpdateService is scoped to the one service ARN — the deploy only ever
        # rolls this service.
        backend_deploy_project.add_to_role_policy(
            iam.PolicyStatement(
                sid="RollEcsService",
                actions=["ecs:UpdateService"],
                resources=[service.service_arn],
            )
        )
        # DescribeServices is evaluated by IAM at CLUSTER granularity (the
        # service-ARN form fails for some callers / the rollout poll), so scope
        # it to the cluster ARN while keeping UpdateService on the service ARN.
        backend_deploy_project.add_to_role_policy(
            iam.PolicyStatement(
                sid="DescribeEcsServices",
                actions=["ecs:DescribeServices"],
                resources=[cluster.cluster_arn],
            )
        )
        # Registering a new task-def revision pinned to the SHA image requires
        # DescribeTaskDefinition + RegisterTaskDefinition. The ECS API does NOT
        # support resource-level permissions for these two actions, so AWS
        # requires "*" — this is an AWS constraint, not a scope omission.
        backend_deploy_project.add_to_role_policy(
            iam.PolicyStatement(
                sid="RegisterTaskDefinition",
                actions=[
                    "ecs:DescribeTaskDefinition",
                    "ecs:RegisterTaskDefinition",
                ],
                resources=["*"],
            )
        )
        # register-task-definition carries the task + execution role ARNs, so
        # the deploy role needs iam:PassRole — scoped to EXACTLY those two
        # ApiStack roles (not "*"), and only passable to ECS tasks.
        backend_deploy_project.add_to_role_policy(
            iam.PolicyStatement(
                sid="PassEcsTaskRoles",
                actions=["iam:PassRole"],
                resources=[task_role.role_arn, execution_role.role_arn],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "ecs-tasks.amazonaws.com"
                    }
                },
            )
        )

        # ------------------------------------------------------------------ #
        # Frontend deploy project — s3 sync + CloudFront invalidation.        #
        # ------------------------------------------------------------------ #
        # Mirrors WebStack.web_deploy_role exactly: grant_read_write +
        # grant_delete on the bucket (for ``sync --delete``) plus a CloudFront
        # invalidation statement scoped to the distribution ARN.
        frontend_deploy_project = codebuild.PipelineProject(
            self,
            "FrontendDeploy",
            project_name=f"{self.stack_name}-frontend-deploy",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
            ),
            environment_variables={
                "WEB_BUCKET": codebuild.BuildEnvironmentVariable(
                    value=web_bucket.bucket_name
                ),
                "DISTRIBUTION_ID": codebuild.BuildEnvironmentVariable(
                    value=web_distribution.distribution_id
                ),
            },
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "build": {
                            "commands": [
                                "echo Syncing SPA build to S3...",
                                # The frontend build artifact is the input, so
                                # its ``dist`` contents are the working dir root.
                                'aws s3 sync . "s3://$WEB_BUCKET" --delete',
                                "echo Invalidating CloudFront...",
                                (
                                    "aws cloudfront create-invalidation "
                                    "--distribution-id $DISTRIBUTION_ID "
                                    '--paths "/*"'
                                ),
                            ]
                        },
                    },
                }
            ),
        )
        web_bucket.grant_read_write(frontend_deploy_project)
        web_bucket.grant_delete(frontend_deploy_project)
        frontend_deploy_project.add_to_role_policy(
            iam.PolicyStatement(
                sid="CloudFrontInvalidation",
                actions=[
                    "cloudfront:CreateInvalidation",
                    "cloudfront:GetInvalidation",
                ],
                # CloudFront distribution ARNs are global — empty region
                # segment, same as WebStack.web_deploy_role.
                resources=[
                    f"arn:aws:cloudfront::{self.account}:distribution/"
                    f"{web_distribution.distribution_id}"
                ],
            )
        )

        # ------------------------------------------------------------------ #
        # Pipeline assembly.                                                  #
        # ------------------------------------------------------------------ #
        source_action = (
            codepipeline_actions.CodeStarConnectionsSourceAction(
                action_name="GitHub_Source",
                connection_arn=codestar_connection_arn,
                owner=owner,
                repo=repo_name,
                branch="main",
                output=source_output,
            )
        )

        # Build stage runs backend + frontend in parallel (same run_order).
        backend_build_action = codepipeline_actions.CodeBuildAction(
            action_name="BackendBuild",
            project=backend_build_project,
            input=source_output,
            outputs=[backend_build_output],
        )
        frontend_build_action = codepipeline_actions.CodeBuildAction(
            action_name="FrontendBuild",
            project=frontend_build_project,
            input=source_output,
            outputs=[frontend_build_output],
        )

        test_action = codepipeline_actions.CodeBuildAction(
            action_name="PyTest",
            project=test_project,
            input=source_output,
        )

        backend_deploy_action = codepipeline_actions.CodeBuildAction(
            action_name="BackendDeploy",
            project=backend_deploy_project,
            # Source gives alembic + requirements; backend build artifact
            # carries the resolved image tag metadata.
            input=source_output,
            extra_inputs=[backend_build_output],
        )
        frontend_deploy_action = codepipeline_actions.CodeBuildAction(
            action_name="FrontendDeploy",
            project=frontend_deploy_project,
            # The compiled SPA is the input (its dist contents are the root).
            input=frontend_build_output,
        )

        stages: list[codepipeline.StageProps] = [
            codepipeline.StageProps(
                stage_name="Source",
                actions=[source_action],
            ),
            codepipeline.StageProps(
                stage_name="Build",
                actions=[backend_build_action, frontend_build_action],
            ),
            codepipeline.StageProps(
                stage_name="Test",
                actions=[test_action],
            ),
        ]

        # Manual approval — PROD ONLY. Staging deploys automatically; the prod
        # pipeline gates every deploy behind a human approval so a release to
        # production is always a deliberate action.
        if env_name == "prod":
            stages.append(
                codepipeline.StageProps(
                    stage_name="Approval",
                    actions=[
                        codepipeline_actions.ManualApprovalAction(
                            action_name="ApproveProdDeploy",
                            additional_information=(
                                "Approve to deploy backend + frontend to "
                                "production."
                            ),
                        )
                    ],
                )
            )

        stages.append(
            codepipeline.StageProps(
                stage_name="DeployBackend",
                actions=[backend_deploy_action],
            )
        )
        stages.append(
            codepipeline.StageProps(
                stage_name="DeployFrontend",
                actions=[frontend_deploy_action],
            )
        )

        self._pipeline = codepipeline.Pipeline(
            self,
            "Pipeline",
            pipeline_name=f"{self.stack_name}-pipeline",
            artifact_bucket=artifact_bucket,
            # Restart the pipeline from the top if a newer commit lands while a
            # run is in flight, so deploys always reflect the latest ``main``.
            restart_execution_on_update=True,
            stages=stages,
        )

        # ------------------------------------------------------------------ #
        # CloudFormation outputs.                                             #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "PipelineName",
            value=self._pipeline.pipeline_name,
            description="Name of the unified CI/CD CodePipeline",
        )
        cdk.CfnOutput(
            self,
            "CodeStarConnectionArn",
            value=codestar_connection_arn,
            description=(
                "CodeStar GitHub connection ARN the pipeline uses — operator "
                "creates the connection once and supplies it via "
                "-c codestarConnectionArn=..."
            ),
        )
        cdk.CfnOutput(
            self,
            "BackendBuildProjectName",
            value=backend_build_project.project_name,
            description="CodeBuild project that builds + pushes the backend image",
        )
        cdk.CfnOutput(
            self,
            "FrontendBuildProjectName",
            value=frontend_build_project.project_name,
            description="CodeBuild project that builds the frontend SPA",
        )
        cdk.CfnOutput(
            self,
            "TestProjectName",
            value=test_project.project_name,
            description="CodeBuild project that runs the pytest suite",
        )
        cdk.CfnOutput(
            self,
            "BackendDeployProjectName",
            value=backend_deploy_project.project_name,
            description=(
                "CodeBuild project that migrates the DB and rolls the ECS "
                "service"
            ),
        )
        cdk.CfnOutput(
            self,
            "FrontendDeployProjectName",
            value=frontend_deploy_project.project_name,
            description=(
                "CodeBuild project that syncs the SPA to S3 and invalidates "
                "CloudFront"
            ),
        )

    # ---------------------------------------------------------------------- #
    # Public properties                                                       #
    # ---------------------------------------------------------------------- #

    @property
    def pipeline(self) -> codepipeline.Pipeline:
        """The unified CI/CD CodePipeline."""
        return self._pipeline

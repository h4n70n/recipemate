"""
WebStack: static hosting for the RecipeMate web client (the Vite/React SPA
build in ``frontend/dist``).

This is distinct from :class:`~stacks.storage_stack.StorageStack`, which hosts
recipe *images*. The web client needs its own private S3 bucket for its build
artifacts, fronted by a dedicated CloudFront distribution. CloudFront serves
``index.html`` as the default root object and rewrites 403/404 responses back
to ``/index.html`` (HTTP 200) so client-side (React Router) deep links resolve.

WebStack also provisions the GitHub-OIDC frontend deploy role whose ARN is the
``AWS_DEPLOY_ROLE_ARN`` GitHub secret: a least-privilege role that CI assumes to
``s3 sync`` the build into the web bucket and invalidate the CloudFront cache.

OR-9 (Domain & DNS) wires the custom domain onto the distribution: the EXISTING
``recipemate.me`` hosted zone is referenced via ``HostedZone.from_lookup`` (never
re-created), a DNS-validated ACM certificate for ``recipemate.me`` +
``www.recipemate.me`` is issued in **us-east-1** (CloudFront only accepts
us-east-1 certs), the distribution gets ``domain_names`` + that certificate, and
Route 53 A + AAAA alias records for the apex and ``www`` point at the
distribution. DNS validation gives automatic certificate renewal.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_iam as iam,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_s3 as s3,
)
from constructs import Construct


class WebStack(Stack):
    """
    Creates:
      - A private S3 bucket holding the SPA build artifacts: all public access
        blocked (served only via CloudFront), versioned, SSE-S3 encrypted.
        Removal policy follows the per-env ``removalPolicy`` context hint —
        DESTROY for staging, RETAIN for prod.
      - A CloudFront distribution fronting the bucket via an Origin Access
        Control (provisioned by the ``S3Origin`` construct — the newer
        ``S3BucketOrigin.with_origin_access_control`` API is not available in
        the pinned aws-cdk-lib 2.144.0, so ``S3Origin`` is used to match
        StorageStack). It serves ``index.html`` as the default root object,
        redirects HTTP->HTTPS, applies Price Class 100 (US & Europe), and
        enforces TLS 1.2_2021. 403 and 404 responses are rewritten to
        ``/index.html`` with status 200 so client-side routes deep-link.
      - A least-privilege frontend deploy role trusted only by this repo on
        ``main`` via GitHub OIDC. It imports the OIDC provider owned by
        PipelineOidcStack (never creating a second one) and is scoped to just
        the S3 ``sync --delete`` actions on the web bucket plus CloudFront
        invalidation on the distribution.

    Properties exposed for other stacks / CI:
      - ``bucket``          – the S3 Bucket for SPA artifacts (CI syncs into it).
      - ``distribution``    – the CloudFront Distribution (CI invalidates it).
      - ``web_deploy_role`` – the IAM Role CI assumes via OIDC.

    CloudFormation outputs (consumed by the deploy script / GitHub Actions):
      - ``BucketName``             – S3 bucket name  -> ``aws s3 sync`` target.
      - ``DistributionDomainName`` – the public ``d1234.cloudfront.net`` host.
      - ``DistributionId``         – CloudFront id -> ``create-invalidation``.
      - ``WebDeployRoleArn``       – store as the ``AWS_DEPLOY_ROLE_ARN`` secret.
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
        # Removal policy — honour the per-env context hint published by      #
        # app.py ("destroy" for staging, "retain" for prod). Default RETAIN  #
        # if the hint is absent, to avoid accidentally destroying artifacts. #
        # ------------------------------------------------------------------ #
        removal_hint = self.node.try_get_context("removalPolicy")
        removal_policy = (
            cdk.RemovalPolicy.DESTROY
            if removal_hint == "destroy"
            else cdk.RemovalPolicy.RETAIN
        )
        # auto_delete_objects only makes sense when the bucket is destroyed;
        # it provisions a cleanup custom resource that empties the bucket first.
        auto_delete = removal_policy == cdk.RemovalPolicy.DESTROY

        # ------------------------------------------------------------------ #
        # S3 Bucket — private origin for the SPA build artifacts             #
        # ------------------------------------------------------------------ #
        self._bucket = s3.Bucket(
            self,
            "WebBucket",
            # Block all public access — objects are served only via CloudFront.
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            public_read_access=False,
            # Versioning lets us roll back a bad deploy and is a sensible
            # default for an artifact bucket.
            versioned=True,
            # S3-managed encryption (SSE-S3).
            encryption=s3.BucketEncryption.S3_MANAGED,
            # Per-env teardown behaviour (see hint resolution above).
            removal_policy=removal_policy,
            auto_delete_objects=auto_delete,
        )

        # ------------------------------------------------------------------ #
        # OR-9 — custom domain: hosted zone lookup + us-east-1 ACM cert       #
        # ------------------------------------------------------------------ #
        # Apex domain is context-driven (app.py publishes ``domainName``) with
        # a documented fallback so synth logic never crashes when the key is
        # absent. The one true AWS lookup is ``HostedZone.from_lookup`` below,
        # which needs account credentials at synth time.
        domain_name = self.node.try_get_context("domainName") or "recipemate.me"
        www_domain = f"www.{domain_name}"

        # Reference the EXISTING Route 53 hosted zone — do NOT create a second
        # zone (AWS registered the domain and provisioned the zone already).
        zone = route53.HostedZone.from_lookup(
            self, "HostedZone", domain_name=domain_name
        )

        # CloudFront only trusts ACM certificates issued in us-east-1, so the
        # web certificate must live there even though every other RecipeMate
        # resource is us-east-2. ``DnsValidatedCertificate`` is deprecated in
        # newer aws-cdk-lib, but in the pinned 2.144.0 it is the pragmatic way
        # to issue a cross-region (us-east-1 cert from this us-east-2 stack)
        # certificate from a single stack: its ``region`` parameter provisions
        # the cert in us-east-1 via a custom resource. DNS validation (records
        # written into ``zone``) gives automatic certificate renewal.
        web_certificate = acm.DnsValidatedCertificate(
            self,
            "WebCertificate",
            domain_name=domain_name,
            subject_alternative_names=[www_domain],
            hosted_zone=zone,
            region="us-east-1",
            validation=acm.CertificateValidation.from_dns(zone),
        )

        # ------------------------------------------------------------------ #
        # CloudFront Origin Access Control + Distribution                     #
        # ------------------------------------------------------------------ #
        # S3Origin wires the bucket to CloudFront and provisions an Origin
        # Access Identity, granting the distribution read access via a bucket
        # policy. (S3BucketOrigin.with_origin_access_control() is not available
        # in aws-cdk-lib 2.144.0; S3Origin is the supported construct and
        # matches StorageStack.)
        origin = origins.S3Origin(self._bucket)

        self._distribution = cloudfront.Distribution(
            self,
            "WebDistribution",
            # Serve the SPA from the apex and www aliases using the us-east-1
            # certificate issued above.
            domain_names=[domain_name, www_domain],
            certificate=web_certificate,
            # Serve index.html when the viewer requests the distribution root.
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                # Force HTTPS for all viewers.
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                # Managed CachingOptimized policy — fine for immutable,
                # content-hashed Vite assets plus index.html.
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
            ),
            # SPA deep-link support: a request for /library (no such S3 object)
            # yields a 403 (OAC, bucket is private) or 404; rewrite both to
            # /index.html with a 200 so React Router can handle the route.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=cdk.Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=cdk.Duration.seconds(0),
                ),
            ],
            # Price Class 100 — edge locations in US & Europe only.
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            # Enforce modern TLS at the distribution level.
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            comment=f"{self.stack_name} — web client SPA",
        )

        # ------------------------------------------------------------------ #
        # OR-9 — Route 53 alias records (apex + www -> CloudFront)            #
        # ------------------------------------------------------------------ #
        # A + AAAA alias records so both IPv4 and IPv6 viewers resolve the apex
        # and www names to the distribution. ``record_name=None`` targets the
        # zone apex; ``"www"`` is relative to the zone, i.e. ``www.<domain>``.
        cloudfront_target = route53.RecordTarget.from_alias(
            targets.CloudFrontTarget(self._distribution)
        )
        route53.ARecord(
            self,
            "ApexAliasRecord",
            zone=zone,
            record_name=None,
            target=cloudfront_target,
        )
        route53.AaaaRecord(
            self,
            "ApexAliasRecordAaaa",
            zone=zone,
            record_name=None,
            target=cloudfront_target,
        )
        route53.ARecord(
            self,
            "WwwAliasRecord",
            zone=zone,
            record_name="www",
            target=cloudfront_target,
        )
        route53.AaaaRecord(
            self,
            "WwwAliasRecordAaaa",
            zone=zone,
            record_name="www",
            target=cloudfront_target,
        )

        # ------------------------------------------------------------------ #
        # Frontend GitHub-OIDC deploy role                                    #
        # ------------------------------------------------------------------ #
        # The role CI assumes (via aws-actions/configure-aws-credentials) to run
        # `aws s3 sync dist s3://<bucket> --delete` and
        # `aws cloudfront create-invalidation`. Its ARN is the
        # ``AWS_DEPLOY_ROLE_ARN`` GitHub secret.

        # GitHub repo in ``owner/name`` form, parameterised so each account can
        # target its own fork without editing source. Falls back to a documented
        # placeholder so synth works without the context present.
        github_repo = (
            self.node.try_get_context("githubRepo") or self.DEFAULT_GITHUB_REPO
        )

        # Import — never create — the OIDC provider by its well-known ARN. AWS
        # allows only one OIDC provider per issuer URL per account, and
        # PipelineOidcStack is that single creator; creating another here would
        # conflict. WebStack always imports the existing provider.
        provider_arn = (
            f"arn:aws:iam::{self.account}:oidc-provider/"
            "token.actions.githubusercontent.com"
        )
        imported_provider = (
            iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
                self, "ImportedGitHubOidcProviderForWeb", provider_arn
            )
        )

        # Trust policy — restrict federation to this repo on ``main``. The
        # ``sub`` condition pins to pushes on ``main`` of the configured repo;
        # ``aud`` pins the STS audience. These match PipelineOidcStack.
        oidc_principal = iam.OpenIdConnectPrincipal(
            imported_provider,
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

        self._web_deploy_role = iam.Role(
            self,
            "WebDeployRole",
            role_name=f"{self.stack_name}-web-deploy",
            assumed_by=oidc_principal,
            description=(
                "Frontend CI deploy role (S3 sync + CloudFront invalidation) "
                f"assumed via GitHub OIDC from repo {github_repo} on main"
            ),
            max_session_duration=cdk.Duration.hours(1),
        )

        # Least-privilege S3 access scoped to this bucket only. The grant
        # helpers together produce s3:ListBucket/GetObject/PutObject (read-write)
        # on the bucket ARN and ``bucket/*``, plus s3:DeleteObject (for the
        # ``--delete`` sync). No ``s3:*`` and no ``*`` resources.
        self._bucket.grant_read_write(self._web_deploy_role)
        self._bucket.grant_delete(self._web_deploy_role)

        # CloudFront invalidation only, scoped to this distribution's ARN. Note
        # the empty region segment — CloudFront distribution ARNs are global.
        self._web_deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudFrontInvalidation",
                actions=[
                    "cloudfront:CreateInvalidation",
                    "cloudfront:GetInvalidation",
                ],
                resources=[
                    f"arn:aws:cloudfront::{self.account}:distribution/"
                    f"{self._distribution.distribution_id}"
                ],
            )
        )

        # ------------------------------------------------------------------ #
        # CloudFormation Outputs                                              #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "BucketName",
            value=self._bucket.bucket_name,
            description="S3 bucket holding the web client build artifacts "
            "(aws s3 sync target)",
        )

        cdk.CfnOutput(
            self,
            "DistributionDomainName",
            value=self._distribution.distribution_domain_name,
            description="CloudFront distribution domain name serving the SPA",
        )

        cdk.CfnOutput(
            self,
            "DistributionId",
            value=self._distribution.distribution_id,
            description="CloudFront distribution ID (create-invalidation target)",
        )

        cdk.CfnOutput(
            self,
            "WebDeployRoleArn",
            value=self._web_deploy_role.role_arn,
            description=(
                "IAM role ARN for CI frontend deploys — store as the "
                "AWS_DEPLOY_ROLE_ARN GitHub secret"
            ),
        )

        cdk.CfnOutput(
            self,
            "SiteUrl",
            value=f"https://{domain_name}",
            description="Primary web client URL served over the custom domain",
        )

    # ---------------------------------------------------------------------- #
    # Public properties                                                       #
    # ---------------------------------------------------------------------- #

    @property
    def bucket(self) -> s3.Bucket:
        """The S3 bucket holding the web client build artifacts."""
        return self._bucket

    @property
    def distribution(self) -> cloudfront.Distribution:
        """The CloudFront distribution serving the SPA from the S3 bucket."""
        return self._distribution

    @property
    def web_deploy_role(self) -> iam.Role:
        """The IAM role CI assumes via GitHub OIDC to deploy the frontend."""
        return self._web_deploy_role

"""
WebStack: static hosting for the RecipeMate web client (the Vite/React SPA
build in ``frontend/dist``).

This is distinct from :class:`~stacks.storage_stack.StorageStack`, which hosts
recipe *images*. The web client needs its own private S3 bucket for its build
artifacts, fronted by a dedicated CloudFront distribution. CloudFront serves
``index.html`` as the default root object and rewrites 403/404 responses back
to ``/index.html`` (HTTP 200) so client-side (React Router) deep links resolve.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
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

    Properties exposed for other stacks / CI:
      - ``bucket``       – the S3 Bucket for SPA artifacts (CI syncs into it).
      - ``distribution`` – the CloudFront Distribution (CI invalidates it).

    CloudFormation outputs (consumed by the deploy script / GitHub Actions):
      - ``BucketName``             – S3 bucket name  -> ``aws s3 sync`` target.
      - ``DistributionDomainName`` – the public ``d1234.cloudfront.net`` host.
      - ``DistributionId``         – CloudFront id -> ``create-invalidation``.
    """

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

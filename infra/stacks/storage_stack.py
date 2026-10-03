"""
StorageStack: S3 bucket for recipe images and cook log photos, served via
a CloudFront distribution with Origin Access Control (OAC).
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3 as s3,
)
from constructs import Construct


class StorageStack(Stack):
    """
    Creates:
      - An S3 bucket with versioning, server-side encryption, blocked public
        access, and lifecycle rules that transition / expire non-current versions.
      - A CloudFront distribution that fronts the bucket via OAC, enforces
        HTTPS-only access, and applies Price Class 100 (US & Europe).

    Properties exposed for other stacks:
      - ``bucket``                 – the S3 Bucket.
      - ``distribution``           – the CloudFront Distribution.
      - ``distribution_domain_name`` – the CloudFront domain (e.g.
                                       ``d1234.cloudfront.net``).
    """

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # ------------------------------------------------------------------ #
        # S3 Bucket                                                           #
        # ------------------------------------------------------------------ #
        self._bucket = s3.Bucket(
            self,
            "RecipeImagesBucket",
            # Block all public access — objects are served only via CloudFront.
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            public_read_access=False,
            # Versioning is required for the lifecycle rules to target
            # non-current versions.
            versioned=True,
            # S3-managed encryption (SSE-S3).
            encryption=s3.BucketEncryption.S3_MANAGED,
            # Lifecycle rules for non-current versions:
            #   1. Transition to Glacier after 30 days.
            #   2. Expire (delete) after 90 days.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="NonCurrentVersionArchiveAndExpire",
                    enabled=True,
                    noncurrent_version_transitions=[
                        s3.NoncurrentVersionTransition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(30),
                        )
                    ],
                    noncurrent_version_expiration=Duration.days(90),
                )
            ],
            # Prevent accidental deletion; override per-environment if needed.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------ #
        # CloudFront Origin Access Control + Distribution                     #
        # ------------------------------------------------------------------ #
        # S3Origin wires the bucket to CloudFront and, by default, provisions an
        # Origin Access Identity and grants the distribution read permission on
        # the bucket via a bucket policy. (The newer S3BucketOrigin /
        # with_origin_access_control() API is not available in aws-cdk-lib
        # 2.144.0; S3Origin is the supported construct in this version.)
        origin = origins.S3Origin(self._bucket)

        self._distribution = cloudfront.Distribution(
            self,
            "RecipeImagesDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                # Redirect HTTP → HTTPS; HTTPS-only would reject presigned
                # upload URLs that some SDKs generate over plain HTTP.
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                # Cache optimised for images (managed policy: CachingOptimized).
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                # Allow GET (and HEAD / OPTIONS) so presigned-URL uploads work
                # cross-origin from browser clients.
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                # Respond to OPTIONS pre-flight requests from the cache.
                response_headers_policy=cloudfront.ResponseHeadersPolicy(
                    self,
                    "CorsHeadersPolicy",
                    response_headers_policy_name=f"{self.stack_name}-CorsHeaders",
                    cors_behavior=cloudfront.ResponseHeadersCorsBehavior(
                        access_control_allow_credentials=False,
                        access_control_allow_headers=["*"],
                        access_control_allow_methods=["GET"],
                        access_control_allow_origins=["*"],
                        origin_override=True,
                    ),
                ),
            ),
            # Price Class 100 — edge locations in US & Europe only.
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            # Enforce HTTPS at the distribution level.
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            comment=f"{self.stack_name} — recipe images CDN",
        )

        # ------------------------------------------------------------------ #
        # CloudFormation Outputs                                              #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "BucketName",
            value=self._bucket.bucket_name,
            description="S3 bucket storing recipe images and cook log photos",
        )

        cdk.CfnOutput(
            self,
            "DistributionDomainName",
            value=self._distribution.distribution_domain_name,
            description="CloudFront distribution domain name",
        )

        cdk.CfnOutput(
            self,
            "DistributionId",
            value=self._distribution.distribution_id,
            description="CloudFront distribution ID",
        )

    # ---------------------------------------------------------------------- #
    # Public properties                                                       #
    # ---------------------------------------------------------------------- #

    @property
    def bucket(self) -> s3.Bucket:
        """The S3 bucket that stores recipe images and cook log photos."""
        return self._bucket

    @property
    def distribution(self) -> cloudfront.Distribution:
        """The CloudFront distribution serving images from the S3 bucket."""
        return self._distribution

    @property
    def distribution_domain_name(self) -> str:
        """
        The CloudFront domain name (e.g. ``d1234abcd.cloudfront.net``).
        Use this as the base URL when constructing image URLs returned to clients.
        """
        return self._distribution.distribution_domain_name

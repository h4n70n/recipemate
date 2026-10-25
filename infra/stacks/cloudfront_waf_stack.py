"""
CloudFrontWafStack: the CLOUDFRONT-scoped AWS WAF WebACL that fronts the
RecipeMate web client's CloudFront distribution (OR-5).

Why a dedicated stack in us-east-1
----------------------------------
A WAF WebACL that is attached to a CloudFront distribution MUST be created with
``scope="CLOUDFRONT"`` **and must physically live in us-east-1**, regardless of
the us-east-2 default every other RecipeMate resource uses. This is an AWS
constraint identical in spirit to OR-9's CloudFront ACM certificate (which
``WebStack`` issues in us-east-1 via ``acm.DnsValidatedCertificate(region=...)``).

CloudFront's ``web_acl_id`` property takes the WebACL **ARN** (not the id, despite
the name) and, because the distribution is a global resource, the WebACL it
references has to be the us-east-1 CLOUDFRONT-scoped one. A WebACL created in the
us-east-2 app with ``scope="REGIONAL"`` cannot be attached to CloudFront.

Rather than smuggle a cross-region resource inside the us-east-2 ``WebStack``,
this small stack is instantiated separately in ``infra/app.py`` with
``env=cdk.Environment(account=config.account, region="us-east-1")`` and its WebACL
ARN is threaded into ``WebStack`` as the ``cloudfront_web_acl_arn`` keyword arg
(the established cross-stack wiring pattern, which also establishes the
CloudFormation dependency edge). This mirrors the OR-9 us-east-1 CloudFront cert
precedent and keeps us-east-1 as the single documented exception to the us-east-2
default.

Rule set (mirrors the REGIONAL WebACL in :class:`~stacks.api_stack.ApiStack`)
----------------------------------------------------------------------------
  - ``AWSManagedRulesCommonRuleSet`` — the AWS managed common rule group
    (``override_action`` none, so the group's own rule actions apply).
  - A rate-based rule (``aggregate_key_type="IP"``) that blocks a single source
    IP once it exceeds ``wafRateLimit`` requests in the trailing 5-minute WAF
    window. This is the edge backstop for the app-level per-user limiter, which
    fails open on a Redis outage.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_wafv2 as wafv2,
)
from constructs import Construct


#: Documented default for the WAF rate-based rule limit (requests per source IP
#: over the WAF's fixed trailing 5-minute window). WAF's minimum is 100; this is
#: a conservative initial value to be tuned post-launch with a real load test.
DEFAULT_WAF_RATE_LIMIT = 2000


class CloudFrontWafStack(Stack):
    """
    Creates a single CLOUDFRONT-scoped ``wafv2.CfnWebACL`` (us-east-1) with the
    AWS managed common rule set plus an IP rate-based rule. The rate limit is
    context-driven (``wafRateLimit`` CDK context key) with a documented default.

    Properties exposed for other stacks:
      - ``web_acl_arn`` – the WebACL ARN, threaded into ``WebStack`` so the
        CloudFront distribution sets ``web_acl_id`` to it.

    CloudFormation outputs:
      - ``CloudFrontWebAclArn`` – the WebACL ARN (operator-visible).
    """

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Rate-based rule limit is context-driven with a documented default so
        # operators can tune it without editing source; coerced to int.
        rate_limit = int(
            self.node.try_get_context("wafRateLimit") or DEFAULT_WAF_RATE_LIMIT
        )

        self._web_acl = wafv2.CfnWebACL(
            self,
            "CloudFrontWebAcl",
            # CloudFront WebACLs MUST be scope=CLOUDFRONT and live in us-east-1.
            scope="CLOUDFRONT",
            # Default to allowing traffic; the rules below block abuse.
            default_action=wafv2.CfnWebACL.DefaultActionProperty(
                allow=wafv2.CfnWebACL.AllowActionProperty()
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                sampled_requests_enabled=True,
                metric_name=f"{self.stack_name}-cloudfront-webacl",
            ),
            rules=_build_waf_rules(self.stack_name, rate_limit),
        )

        cdk.CfnOutput(
            self,
            "CloudFrontWebAclArn",
            value=self._web_acl.attr_arn,
            description=(
                "CLOUDFRONT-scoped WAF WebACL ARN (us-east-1) attached to the "
                "web client CloudFront distribution (OR-5)"
            ),
        )

    @property
    def web_acl_arn(self) -> str:
        """ARN of the CLOUDFRONT-scoped WebACL (set as the distribution's web_acl_id)."""
        return self._web_acl.attr_arn


def _build_waf_rules(
    name_prefix: str, rate_limit: int
) -> list[wafv2.CfnWebACL.RuleProperty]:
    """Build the shared WAF rule list: AWS managed common set + IP rate rule.

    Shared by the CLOUDFRONT and REGIONAL WebACLs so both scopes enforce the
    same protection. ``name_prefix`` namespaces the CloudWatch metric names so
    the two ACLs' metrics are distinguishable.
    """
    return [
        # AWS managed common rule set. ``override_action`` none means the
        # group's own per-rule actions (block/count) apply as published.
        wafv2.CfnWebACL.RuleProperty(
            name="AWSManagedRulesCommonRuleSet",
            priority=0,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=(
                    wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name="AWS",
                        name="AWSManagedRulesCommonRuleSet",
                    )
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                sampled_requests_enabled=True,
                metric_name=f"{name_prefix}-common-rule-set",
            ),
        ),
        # IP rate-based rule: block a source IP that exceeds ``rate_limit``
        # requests over the WAF trailing 5-minute window. The edge backstop
        # for the fail-open app-level per-user limiter.
        wafv2.CfnWebACL.RuleProperty(
            name="RateBasedRule",
            priority=1,
            action=wafv2.CfnWebACL.RuleActionProperty(
                block=wafv2.CfnWebACL.BlockActionProperty()
            ),
            statement=wafv2.CfnWebACL.StatementProperty(
                rate_based_statement=(
                    wafv2.CfnWebACL.RateBasedStatementProperty(
                        limit=rate_limit,
                        aggregate_key_type="IP",
                    )
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                sampled_requests_enabled=True,
                metric_name=f"{name_prefix}-rate-based-rule",
            ),
        ),
    ]

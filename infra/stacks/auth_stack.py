"""
AuthStack: Amazon Cognito User Pool and public app client for RecipeMate.

Provides JWT-based authentication for the REST API consumed by the web frontend
and the native iOS app. The user pool is configured for email sign-in with
self-service sign-up and email verification. The app client is public (no client
secret) so it can be used safely from mobile and SPA clients.

Sign in with Apple is wired conditionally from CDK context so the stack can be
synthesised without Apple credentials present (see the identity-provider section
below).

OR-9 (Domain & DNS) sets up the Cognito hosted-UI domain and points the OAuth
redirect URLs at the real domain. The hosted-UI domain DEFAULTS to a Cognito
PREFIX domain (``<prefix>.auth.<region>.amazoncognito.com``) for a simpler first
cut: a prefix domain needs no extra us-east-1 certificate and no prerequisite
root A record (a Cognito CUSTOM domain requires both an A record at the apex and
a us-east-1 cert before it can be created). A custom ``auth.<domain>`` is still
available by supplying the ``authCustomDomain`` context key, in which case the
stack issues its own us-east-1 DNS-validated certificate and adds a Route 53 A
alias record. The callback/logout URLs default to ``https://<domain>/callback``
and ``https://<domain>/logout`` while still honouring ``-c callbackUrls`` /
``-c logoutUrls`` overrides (leaving room for additional iOS redirect URIs).
"""
from __future__ import annotations

import re

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_route53 as route53,
    aws_route53_targets as targets,
)
from constructs import Construct


class AuthStack(Stack):
    """
    Creates:
      - A Cognito User Pool with email sign-in, self sign-up, required email
        verification, a password policy (min 8 chars; lower, upper, digits) and
        email-based account recovery.
      - A public User Pool App Client (no secret) supporting USER_PASSWORD_AUTH
        and SRP auth flows plus the OAuth authorization-code grant with the
        openid/email/profile scopes.
      - (Optional) a Sign in with Apple identity provider, wired only when the
        required Apple credentials are supplied via CDK context.
      - A Cognito hosted-UI domain: a prefix domain by default, or a custom
        ``auth.<domain>`` domain (with its own us-east-1 cert + Route 53 alias)
        when the ``authCustomDomain`` context key is supplied.

    Properties exposed for other stacks:
      - ``user_pool``        – the Cognito UserPool.
      - ``user_pool_client`` – the public UserPoolClient.
      - ``user_pool_domain`` – the Cognito UserPoolDomain (hosted UI).
    """

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # ------------------------------------------------------------------ #
        # User Pool                                                           #
        # ------------------------------------------------------------------ #
        self._user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{self.stack_name}-user-pool",
            # Sign in with email address only.
            sign_in_aliases=cognito.SignInAliases(email=True),
            # Allow users to register themselves.
            self_sign_up_enabled=True,
            # Require users to verify their email address before sign-in.
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            # Email is a required, mutable standard attribute.
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            # Password policy: minimum 8 characters with mixed character classes.
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            # Users recover their account via a code sent to their email.
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # Staging-friendly: destroy the pool with the stack. Override for prod.
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------ #
        # Sign in with Apple identity provider (conditional)                  #
        # ------------------------------------------------------------------ #
        # Apple credentials are not committed to source control. When they are
        # provided through CDK context the provider is created and the app
        # client is told to depend on it so OAuth sign-in works end to end:
        #
        #   cdk deploy -c appleClientId=... \
        #              -c appleTeamId=... \
        #              -c appleKeyId=... \
        #              -c applePrivateKey="$(cat AuthKey.p8)"
        #
        # Without these context values the provider is skipped entirely so the
        # stack still synthesises cleanly in local/CI environments.
        apple_client_id = self.node.try_get_context("appleClientId")
        apple_team_id = self.node.try_get_context("appleTeamId")
        apple_key_id = self.node.try_get_context("appleKeyId")
        apple_private_key = self.node.try_get_context("applePrivateKey")

        self._apple_provider: cognito.UserPoolIdentityProviderApple | None = None
        if all((apple_client_id, apple_team_id, apple_key_id, apple_private_key)):
            self._apple_provider = cognito.UserPoolIdentityProviderApple(
                self,
                "AppleProvider",
                user_pool=self._user_pool,
                client_id=apple_client_id,
                team_id=apple_team_id,
                key_id=apple_key_id,
                private_key=apple_private_key,
                # Map the Apple email claim onto the Cognito email attribute.
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.APPLE_EMAIL,
                ),
                scopes=["name", "email"],
            )

        # ------------------------------------------------------------------ #
        # App Client — public (no secret) for mobile/web clients             #
        # ------------------------------------------------------------------ #
        # Callback and logout URLs are configurable via CDK context so each
        # environment can register its own frontend origins (and additional iOS
        # redirect URIs). When no override is supplied they default to the real
        # domain (OR-9): ``https://<domain>/callback`` and
        # ``https://<domain>/logout``. ``domainName`` is published by app.py; a
        # documented fallback keeps synth working if the key is absent.
        domain_name = self.node.try_get_context("domainName") or "recipemate.me"
        callback_urls = self.node.try_get_context("callbackUrls") or [
            f"https://{domain_name}/callback"
        ]
        logout_urls = self.node.try_get_context("logoutUrls") or [
            f"https://{domain_name}/logout"
        ]

        # Supported identity providers: always Cognito; add Apple when wired.
        supported_providers = [
            cognito.UserPoolClientIdentityProvider.COGNITO,
        ]
        if self._apple_provider is not None:
            supported_providers.append(
                cognito.UserPoolClientIdentityProvider.APPLE
            )

        self._user_pool_client = self._user_pool.add_client(
            "AppClient",
            user_pool_client_name=f"{self.stack_name}-app-client",
            # Public client: no secret is generated.
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
            supported_identity_providers=supported_providers,
        )

        # Ensure the Apple provider exists before the client references it.
        if self._apple_provider is not None:
            self._user_pool_client.node.add_dependency(self._apple_provider)

        # ------------------------------------------------------------------ #
        # Hosted-UI domain (OR-9)                                             #
        # ------------------------------------------------------------------ #
        # Default: a Cognito PREFIX domain. It is the simpler first cut — no
        # extra us-east-1 certificate and no prerequisite root A record (both
        # of which a Cognito CUSTOM domain requires). Supplying the
        # ``authCustomDomain`` context key switches to a custom
        # ``auth.<domain>`` domain with its own us-east-1 DNS-validated cert and
        # a Route 53 A alias record.
        auth_custom_domain = self.node.try_get_context("authCustomDomain")

        if auth_custom_domain:
            # Reference the EXISTING hosted zone (AWS context lookup, needs
            # credentials at synth time) — never create a second zone.
            zone = route53.HostedZone.from_lookup(
                self, "HostedZone", domain_name=domain_name
            )
            # Cognito custom-domain certificates MUST be in us-east-1 regardless
            # of this stack's us-east-2 region. DnsValidatedCertificate is
            # deprecated in newer aws-cdk-lib but is the pragmatic cross-region
            # option in the pinned 2.144.0; DNS validation gives auto-renewal.
            auth_certificate = acm.DnsValidatedCertificate(
                self,
                "AuthCertificate",
                domain_name=auth_custom_domain,
                hosted_zone=zone,
                region="us-east-1",
                validation=acm.CertificateValidation.from_dns(zone),
            )
            # PREREQUISITE / DEPLOY ORDERING: AWS requires an A (or AAAA)
            # record to already exist at the ZONE APEX (``recipemate.me``)
            # before a Cognito custom domain can be created. That apex alias
            # record is created by WebStack, and AuthStack intentionally keeps
            # no cross-stack construct dependency on it (WebStack and AuthStack
            # stay independent). Deploy WebStack BEFORE AuthStack whenever
            # ``authCustomDomain`` is used, otherwise this custom-domain create
            # can fail. The default prefix-domain path below has no such
            # ordering requirement.
            self._user_pool_domain = self._user_pool.add_domain(
                "HostedUiDomain",
                custom_domain=cognito.CustomDomainOptions(
                    domain_name=auth_custom_domain,
                    certificate=auth_certificate,
                ),
            )
            # Alias the auth subdomain at the Cognito user-pool domain.
            route53.ARecord(
                self,
                "AuthAliasRecord",
                zone=zone,
                record_name=auth_custom_domain.replace(f".{domain_name}", ""),
                target=route53.RecordTarget.from_alias(
                    targets.UserPoolDomainTarget(self._user_pool_domain)
                ),
            )
        else:
            # Prefix domain. The prefix must be lowercase and may contain only
            # alphanumerics and hyphens; sanitize the context value (defaulting
            # to the stack name) so an operator-supplied value cannot produce an
            # invalid domain prefix.
            raw_prefix = (
                self.node.try_get_context("cognitoDomainPrefix")
                or self.stack_name.lower()
            )
            prefix = re.sub(r"[^a-z0-9-]", "-", raw_prefix.lower()).strip("-")
            self._user_pool_domain = self._user_pool.add_domain(
                "HostedUiDomain",
                cognito_domain=cognito.CognitoDomainOptions(
                    domain_prefix=prefix,
                ),
            )

        # ------------------------------------------------------------------ #
        # Outputs                                                             #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "UserPoolId",
            value=self._user_pool.user_pool_id,
            description="Cognito User Pool ID",
        )

        cdk.CfnOutput(
            self,
            "UserPoolClientId",
            value=self._user_pool_client.user_pool_client_id,
            description="Cognito User Pool App Client ID",
        )

        # Hosted-UI base URL. ``base_url()`` returns the full
        # ``https://<domain>.auth.<region>.amazoncognito.com`` for a prefix
        # domain, or ``https://<custom-domain>`` for a custom domain.
        cdk.CfnOutput(
            self,
            "UserPoolDomainBaseUrl",
            value=self._user_pool_domain.base_url(),
            description="Cognito hosted-UI base URL (OR-9)",
        )

    # ---------------------------------------------------------------------- #
    # Public properties                                                       #
    # ---------------------------------------------------------------------- #

    @property
    def user_pool(self) -> cognito.UserPool:
        """The Cognito User Pool used for API authentication."""
        return self._user_pool

    @property
    def user_pool_client(self) -> cognito.UserPoolClient:
        """The public User Pool App Client (no client secret)."""
        return self._user_pool_client

    @property
    def user_pool_domain(self) -> cognito.UserPoolDomain:
        """The Cognito hosted-UI domain (prefix domain by default)."""
        return self._user_pool_domain

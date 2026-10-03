"""
AuthStack: Amazon Cognito User Pool and public app client for RecipeMate.

Provides JWT-based authentication for the REST API consumed by the web frontend
and the native iOS app. The user pool is configured for email sign-in with
self-service sign-up and email verification. The app client is public (no client
secret) so it can be used safely from mobile and SPA clients.

Sign in with Apple is wired conditionally from CDK context so the stack can be
synthesised without Apple credentials present (see the identity-provider section
below).
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_cognito as cognito,
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

    Properties exposed for other stacks:
      - ``user_pool``        – the Cognito UserPool.
      - ``user_pool_client`` – the public UserPoolClient.
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
        # environment can register its own frontend origins. Defaults point at
        # a local development server.
        callback_urls = self.node.try_get_context("callbackUrls") or [
            "https://localhost:3000/callback"
        ]
        logout_urls = self.node.try_get_context("logoutUrls") or [
            "https://localhost:3000/logout"
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

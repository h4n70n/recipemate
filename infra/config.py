"""
Per-environment configuration for the RecipeMate CDK app.

Two deployable environments are defined: ``staging`` and ``prod``.  Each entry
in :data:`ENV_CONFIG` holds the settings that differ between environments —
account/region, database and cache sizing, API scaling, and the removal-policy
hint used to decide whether resources are destroyed or retained on teardown.

The account defaults to the ``CDK_DEFAULT_ACCOUNT`` environment variable (set by
the CDK CLI from the active AWS credentials) and falls back to a per-env
placeholder so the app can still synthesise without credentials present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EnvConfig:
    """Resolved configuration for a single deployment environment."""

    name: str
    account: str
    region: str
    db_multi_az: bool
    redis_node_type: str
    api_desired_count: int
    # ``removal_policy`` is a hint consumed by the app/stacks: "destroy" tears
    # resources down on stack deletion (safe for staging), "retain" keeps them
    # (protects production data).
    removal_policy: str


# Account resolves from the CDK CLI environment when available; otherwise a
# per-env placeholder keeps `cdk synth` working without AWS credentials.
_DEFAULT_ACCOUNT = os.environ.get("CDK_DEFAULT_ACCOUNT")
_DEFAULT_REGION = os.environ.get("CDK_DEFAULT_REGION", "us-east-2")


ENV_CONFIG: dict[str, EnvConfig] = {
    "staging": EnvConfig(
        name="staging",
        account=_DEFAULT_ACCOUNT or "111111111111",
        region=_DEFAULT_REGION,
        db_multi_az=False,
        redis_node_type="cache.t4g.micro",
        api_desired_count=1,
        removal_policy="destroy",
    ),
    "prod": EnvConfig(
        name="prod",
        account=_DEFAULT_ACCOUNT or "222222222222",
        region=_DEFAULT_REGION,
        db_multi_az=True,
        redis_node_type="cache.t4g.small",
        api_desired_count=2,
        removal_policy="retain",
    ),
}


def get_env_config(env_name: str) -> EnvConfig:
    """
    Return the :class:`EnvConfig` for ``env_name``.

    Raises:
        ValueError: if ``env_name`` is not a known environment. The message
            lists the valid choices so misconfiguration fails loudly.
    """
    try:
        return ENV_CONFIG[env_name]
    except KeyError:
        valid = ", ".join(sorted(ENV_CONFIG))
        raise ValueError(
            f"Unknown environment '{env_name}'. Valid environments: {valid}."
        ) from None

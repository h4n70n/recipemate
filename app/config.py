"""Environment-based configuration for RecipeMate."""

import os
from urllib.parse import quote_plus

#: The permissive placeholder SECRET_KEY the base config falls back to. Allowed
#: for local/testing; staging and prod refuse to start while it is in effect
#: (see ``_require_real_secret_key``).
_INSECURE_SECRET_KEY_DEFAULT = "change-me-in-production"


def _compose_database_uri() -> str:
    """Resolve the SQLAlchemy database URI from the environment.

    Two paths, in priority order:

    1. **ECS/Secrets Manager path** — when the individual RDS fields
       (``DB_USER``/``DB_PASSWORD``/``DB_HOST``/``DB_PORT``/``DB_NAME``) are
       injected as container secrets (see ``infra/stacks/api_stack.py``),
       compose ``postgresql+psycopg2://user:pass@host:port/dbname`` from them.
       The RDS secret has no ready-made URL, which is why the API stack injects
       the parts and we assemble them here. The password is URL-encoded so a
       generated password containing reserved characters cannot corrupt the
       URL.
    2. **Local/dev path** — fall back to a directly supplied ``DATABASE_URL``
       (Docker Compose sets this), then to the local default. This keeps local
       development and the test suite working unchanged.

    The field names consumed here MUST match the keys ``ApiStack`` injects.
    """
    user = os.environ.get("DB_USER")
    password = os.environ.get("DB_PASSWORD")
    host = os.environ.get("DB_HOST")
    name = os.environ.get("DB_NAME")
    port = os.environ.get("DB_PORT", "5432")

    if user and password and host and name:
        return (
            f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}"
            f"@{host}:{port}/{name}"
        )

    return os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://recipemate:recipemate@localhost:5432/recipemate",
    )


class Config:
    """Base configuration shared across all environments."""

    # Flask
    SECRET_KEY: str = os.environ.get("SECRET_KEY", _INSECURE_SECRET_KEY_DEFAULT)
    DEBUG: bool = False
    TESTING: bool = False

    # SQLAlchemy — composed from injected RDS secret parts in staging/prod, or
    # a direct DATABASE_URL locally. See ``_compose_database_uri``.
    SQLALCHEMY_DATABASE_URI: str = _compose_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    # Defer actual connection until first query; avoids driver import errors at
    # startup when the database is not yet reachable (e.g. during testing or cold start).
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    # Redis
    REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    # AWS
    AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-2")
    S3_BUCKET: str = os.environ.get("S3_BUCKET", "recipemate-images")
    # Optional custom endpoint for the AWS SDK (boto3). Left empty in deployed
    # environments so boto3 targets real AWS; set to e.g.
    # "http://localhost:4566" to point presigned-URL generation (and any other
    # S3 calls) at LocalStack during local development. See app.services.s3.
    AWS_ENDPOINT_URL: str = os.environ.get("AWS_ENDPOINT_URL", "")
    # Optional CloudFront distribution domain (e.g. "d123.cloudfront.net" or a
    # custom CNAME). When set, public asset URLs are served from CloudFront;
    # otherwise they fall back to a direct S3 URL. See app.services.urls.
    CLOUDFRONT_DOMAIN: str = os.environ.get("CLOUDFRONT_DOMAIN", "")
    SQS_EXTRACTION_QUEUE_URL: str = os.environ.get("SQS_EXTRACTION_QUEUE_URL", "")
    SNS_NOTIFICATIONS_TOPIC_ARN: str = os.environ.get("SNS_NOTIFICATIONS_TOPIC_ARN", "")

    # OpenAI
    OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")

    # Cognito
    COGNITO_USER_POOL_ID: str = os.environ.get("COGNITO_USER_POOL_ID", "")
    COGNITO_CLIENT_ID: str = os.environ.get("COGNITO_CLIENT_ID", "")
    COGNITO_REGION: str = os.environ.get("COGNITO_REGION", "us-east-2")


class LocalConfig(Config):
    """Local development configuration using Docker Compose services."""

    DEBUG = True


class _SecretKeyEnforcedConfig(Config):
    """Base for deployed configs that refuse to start without a real key.

    Staging and prod load ``SECRET_KEY`` from Secrets Manager at runtime (via
    the ECS container secret). If it is unset or still the insecure
    ``change-me-in-production`` placeholder, the app must fail closed rather
    than sign sessions/CSRF tokens with a known key. Local keeps the permissive
    default, so this check lives only on the deployed configs.
    """

    @classmethod
    def _require_real_secret_key(cls) -> None:
        if not cls.SECRET_KEY or cls.SECRET_KEY == _INSECURE_SECRET_KEY_DEFAULT:
            raise RuntimeError(
                "SECRET_KEY must be set to a real value in staging/prod "
                "(loaded from Secrets Manager at runtime). It is unset or still "
                f"the insecure '{_INSECURE_SECRET_KEY_DEFAULT}' default; refusing "
                "to start."
            )


class StagingConfig(_SecretKeyEnforcedConfig):
    """Staging environment configuration."""

    pass


class ProdConfig(_SecretKeyEnforcedConfig):
    """Production environment configuration."""

    pass


_CONFIG_MAP: dict[str, type[Config]] = {
    "local": LocalConfig,
    "staging": StagingConfig,
    "prod": ProdConfig,
    # Aliases
    "development": LocalConfig,
    "production": ProdConfig,
}

_DEFAULT_ENV = "local"


def get_config(config_name: str | None = None) -> Config:
    """Return a config instance for the given environment name.

    Falls back to the ``FLASK_ENV`` environment variable, then ``local``.
    """
    env = config_name or os.environ.get("FLASK_ENV", _DEFAULT_ENV)
    cls = _CONFIG_MAP.get(env, LocalConfig)
    # Staging/prod fail closed on a missing or placeholder SECRET_KEY; local
    # keeps the permissive default so dev and the test suite are unaffected.
    if issubclass(cls, _SecretKeyEnforcedConfig):
        cls._require_real_secret_key()
    return cls()

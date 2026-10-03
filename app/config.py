"""Environment-based configuration for RecipeMate."""

import os


class Config:
    """Base configuration shared across all environments."""

    # Flask
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "change-me-in-production")
    DEBUG: bool = False
    TESTING: bool = False

    # SQLAlchemy
    SQLALCHEMY_DATABASE_URI: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://recipemate:recipemate@localhost:5432/recipemate",
    )
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
    AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
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
    COGNITO_REGION: str = os.environ.get("COGNITO_REGION", "us-east-1")


class LocalConfig(Config):
    """Local development configuration using Docker Compose services."""

    DEBUG = True


class StagingConfig(Config):
    """Staging environment configuration."""

    pass


class ProdConfig(Config):
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
    return cls()

"""RecipeMate Flask application factory."""

from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy

from app.config import get_config

# Shared SQLAlchemy instance — imported by models
db: SQLAlchemy = SQLAlchemy()


def create_app(config_name: str | None = None) -> Flask:
    """Create and configure a Flask application instance.

    Args:
        config_name: One of ``"local"``, ``"staging"``, or ``"prod"``.
            Defaults to the value of the ``FLASK_ENV`` environment variable,
            or ``"local"`` if that is not set.

    Returns:
        A fully configured :class:`flask.Flask` application instance.
    """
    app = Flask(__name__)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    config = get_config(config_name)
    app.config.from_object(config)

    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    db.init_app(app)

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------
    from app.auth import AuthError  # noqa: PLC0415

    @app.errorhandler(AuthError)
    def handle_auth_error(error: AuthError):  # type: ignore[return-value]
        """Render an :class:`AuthError` as a JSON error response.

        Args:
            error: The raised authentication error.

        Returns:
            JSON ``{"error": <message>}`` with the error's HTTP status code.
        """
        return jsonify({"error": error.message}), error.status_code

    # ------------------------------------------------------------------
    # Blueprints
    # ------------------------------------------------------------------
    from app.routes.recipes import recipes_bp  # noqa: PLC0415
    from app.routes.search import search_bp  # noqa: PLC0415
    from app.routes.cooks import cooks_bp  # noqa: PLC0415
    from app.routes.tags import tags_bp  # noqa: PLC0415

    app.register_blueprint(recipes_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(cooks_bp)
    app.register_blueprint(tags_bp)

    # ------------------------------------------------------------------
    # Built-in endpoints
    # ------------------------------------------------------------------
    @app.get("/health")
    def health():  # type: ignore[return-value]
        """Liveness check endpoint.

        Returns:
            JSON ``{"status": "ok"}`` with HTTP 200.
        """
        return jsonify({"status": "ok"}), 200

    return app

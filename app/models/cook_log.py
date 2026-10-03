"""SQLAlchemy model for the ``cook_logs`` table."""

import datetime
import uuid

from sqlalchemy.dialects.postgresql import UUID

from app import db


class CookLog(db.Model):
    """Records a single instance of a user cooking a recipe.

    Only ``cooked_at`` is required; ``notes``, ``rating``, and ``photo_s3_key``
    are all optional.  After any insert, update, or delete of a cook log the
    application layer must recalculate ``Recipe.cook_count`` and
    ``Recipe.avg_rating`` on the parent recipe.
    """

    __tablename__ = "cook_logs"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    recipe_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("recipes.id"),
        nullable=False,
    )
    user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("users.id"),
        nullable=False,
    )
    cooked_at = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text)
    rating = db.Column(
        db.Integer,
        db.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_cook_logs_rating"),
    )
    photo_s3_key = db.Column(db.Text)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.datetime.utcnow,
    )

    def __repr__(self) -> str:
        return (
            f"<CookLog id={self.id} recipe_id={self.recipe_id}"
            f" cooked_at={self.cooked_at}>"
        )

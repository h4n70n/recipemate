"""SQLAlchemy model for the ``users`` table."""

import datetime
import uuid

from sqlalchemy.dialects.postgresql import UUID

from app import db


class User(db.Model):
    """Represents an authenticated RecipeMate user.

    Each user is provisioned lazily on their first authenticated API request.
    The ``cognito_sub`` is the stable Cognito identifier used to look them up,
    while ``email`` is kept for display purposes.
    """

    __tablename__ = "users"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email = db.Column(db.Text, nullable=False, unique=True)
    cognito_sub = db.Column(db.Text, nullable=False, unique=True)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.datetime.utcnow,
    )

    # Relationships
    recipes = db.relationship(
        "Recipe",
        backref="user",
        lazy="dynamic",
        foreign_keys="Recipe.user_id",
    )
    cook_logs = db.relationship(
        "CookLog",
        backref="user",
        lazy="dynamic",
        foreign_keys="CookLog.user_id",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"

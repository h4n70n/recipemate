"""Models package — imports every ORM class so SQLAlchemy metadata is populated.

Importing this package (or any individual model module) is sufficient to
register all tables with ``db.metadata``.  The app factory does **not** need
to call ``db.create_all()``; migrations handle schema changes via Alembic.
"""

from app.models.user import User  # noqa: F401
from app.models.recipe import (  # noqa: F401
    Recipe,
    Ingredient,
    Tool,
    Instruction,
    Tag,
    RecipeTag,
)
from app.models.cook_log import CookLog  # noqa: F401

__all__ = [
    "User",
    "Recipe",
    "Ingredient",
    "Tool",
    "Instruction",
    "Tag",
    "RecipeTag",
    "CookLog",
]

"""SQLAlchemy models for recipes and their sub-entities.

Includes:
- :class:`Recipe` — the core recipe record
- :class:`Ingredient` — individual ingredient lines
- :class:`Tool` — equipment required by the recipe
- :class:`Instruction` — ordered cooking steps
- :class:`Tag` — user-defined labels
- :class:`RecipeTag` — many-to-many join between recipes and tags
"""

import datetime
import uuid

from sqlalchemy.dialects.postgresql import UUID

from app import db


# ---------------------------------------------------------------------------
# Many-to-many association: recipe ↔ tag
# ---------------------------------------------------------------------------

class RecipeTag(db.Model):
    """Join table between :class:`Recipe` and :class:`Tag`."""

    __tablename__ = "recipe_tags"

    recipe_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("recipes.id"),
        primary_key=True,
        nullable=False,
    )
    tag_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("tags.id"),
        primary_key=True,
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<RecipeTag recipe_id={self.recipe_id} tag_id={self.tag_id}>"


# ---------------------------------------------------------------------------
# Tag
# ---------------------------------------------------------------------------

class Tag(db.Model):
    """A user-defined label that can be applied to many recipes."""

    __tablename__ = "tags"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name = db.Column(db.Text, nullable=False, unique=True)

    # Relationship back to recipes via the join table
    recipes = db.relationship(
        "Recipe",
        secondary="recipe_tags",
        backref=db.backref("tags", lazy="dynamic"),
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<Tag id={self.id} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Ingredient
# ---------------------------------------------------------------------------

class Ingredient(db.Model):
    """One ingredient line belonging to a recipe."""

    __tablename__ = "ingredients"

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
    name = db.Column(db.Text, nullable=False)
    quantity = db.Column(db.Numeric)
    unit = db.Column(db.Text)
    preparation = db.Column(db.Text)  # e.g. "finely chopped"
    sort_order = db.Column(db.Integer, nullable=False)

    def __repr__(self) -> str:
        return f"<Ingredient id={self.id} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class Tool(db.Model):
    """A piece of equipment required by a recipe."""

    __tablename__ = "tools"

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
    name = db.Column(db.Text, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False)

    def __repr__(self) -> str:
        return f"<Tool id={self.id} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Instruction
# ---------------------------------------------------------------------------

class Instruction(db.Model):
    """One ordered cooking step in a recipe."""

    __tablename__ = "instructions"

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
    step_number = db.Column(db.Integer, nullable=False)
    body = db.Column(db.Text, nullable=False)

    def __repr__(self) -> str:
        return f"<Instruction id={self.id} step={self.step_number}>"


# ---------------------------------------------------------------------------
# Recipe
# ---------------------------------------------------------------------------

_VALID_ORIGINS = ("instagram", "web", "cookbook", "manual", "ios_share")
_VALID_EXTRACTION_STATUSES = ("pending", "processing", "complete", "failed")


class Recipe(db.Model):
    """The core recipe record.

    ``cook_count`` and ``avg_rating`` are denormalised columns maintained by
    the application layer whenever a cook log is created, updated, or deleted.
    """

    __tablename__ = "recipes"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("users.id"),
        nullable=False,
    )
    title = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text)
    prep_time_min = db.Column(db.Integer)
    cook_time_min = db.Column(db.Integer)
    total_time_min = db.Column(db.Integer)
    servings = db.Column(db.Integer)
    origin = db.Column(
        db.Text,
        db.CheckConstraint(
            "origin IN ('instagram', 'web', 'cookbook', 'manual', 'ios_share')",
            name="ck_recipes_origin",
        ),
    )
    source_url = db.Column(db.Text)
    source_citation = db.Column(db.Text)
    image_s3_key = db.Column(db.Text)
    thumbnail_s3_key = db.Column(db.Text)
    extraction_status = db.Column(
        db.Text,
        db.CheckConstraint(
            "extraction_status IN ('pending', 'processing', 'complete', 'failed')",
            name="ck_recipes_extraction_status",
        ),
    )
    cook_count = db.Column(db.Integer, default=0, server_default="0")
    avg_rating = db.Column(db.Numeric(3, 2))
    deleted_at = db.Column(db.DateTime)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.datetime.utcnow,
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    # Relationships to sub-entities
    ingredients = db.relationship(
        "Ingredient",
        backref="recipe",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Ingredient.sort_order",
    )
    tools = db.relationship(
        "Tool",
        backref="recipe",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Tool.sort_order",
    )
    instructions = db.relationship(
        "Instruction",
        backref="recipe",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Instruction.step_number",
    )
    cook_logs = db.relationship(
        "CookLog",
        backref="recipe",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Recipe id={self.id} title={self.title!r}>"

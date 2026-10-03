"""Create tags and recipe_tags tables

Revision ID: 004_create_tags
Revises: 003_create_recipe_details
Create Date: 2024-01-01 00:03:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = '004_create_tags'
down_revision = '003_create_recipe_details'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'tags',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.Text(), unique=True, nullable=False),
    )

    op.create_table(
        'recipe_tags',
        sa.Column('recipe_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('recipes.id'), nullable=False),
        sa.Column('tag_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tags.id'), nullable=False),
        sa.PrimaryKeyConstraint('recipe_id', 'tag_id'),
    )


def downgrade() -> None:
    op.drop_table('recipe_tags')
    op.drop_table('tags')

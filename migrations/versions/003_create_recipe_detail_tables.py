"""Create ingredients, tools, and instructions tables

Revision ID: 003_create_recipe_details
Revises: 002_create_recipes
Create Date: 2024-01-01 00:02:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = '003_create_recipe_details'
down_revision = '002_create_recipes'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ingredients',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('recipe_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('recipes.id'), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('quantity', sa.Numeric()),
        sa.Column('unit', sa.Text()),
        sa.Column('preparation', sa.Text()),
        sa.Column('sort_order', sa.Integer(), nullable=False),
    )

    op.create_table(
        'tools',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('recipe_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('recipes.id'), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
    )

    op.create_table(
        'instructions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('recipe_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('recipes.id'), nullable=False),
        sa.Column('step_number', sa.Integer(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('instructions')
    op.drop_table('tools')
    op.drop_table('ingredients')

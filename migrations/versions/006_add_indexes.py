"""Add indexes on recipes.user_id, ingredients.recipe_id, cook_logs.recipe_id

Revision ID: 006_add_indexes
Revises: 005_create_cook_logs
Create Date: 2024-01-01 00:05:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic
revision = '006_add_indexes'
down_revision = '005_create_cook_logs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_recipes_user_id', 'recipes', ['user_id'])
    op.create_index('ix_ingredients_recipe_id', 'ingredients', ['recipe_id'])
    op.create_index('ix_cook_logs_recipe_id', 'cook_logs', ['recipe_id'])


def downgrade() -> None:
    op.drop_index('ix_cook_logs_recipe_id', table_name='cook_logs')
    op.drop_index('ix_ingredients_recipe_id', table_name='ingredients')
    op.drop_index('ix_recipes_user_id', table_name='recipes')

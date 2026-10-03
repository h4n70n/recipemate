"""Create cook_logs table

Revision ID: 005_create_cook_logs
Revises: 004_create_tags
Create Date: 2024-01-01 00:04:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = '005_create_cook_logs'
down_revision = '004_create_tags'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cook_logs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('recipe_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('recipes.id'), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('cooked_at', sa.Date(), nullable=False),
        sa.Column('notes', sa.Text()),
        sa.Column('rating', sa.Integer(), sa.CheckConstraint('rating BETWEEN 1 AND 5', name='ck_cook_logs_rating')),
        sa.Column('photo_s3_key', sa.Text()),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cook_logs')

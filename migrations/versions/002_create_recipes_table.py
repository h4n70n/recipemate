"""Create recipes table

Revision ID: 002_create_recipes
Revises: 001_create_users
Create Date: 2024-01-01 00:01:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = '002_create_recipes'
down_revision = '001_create_users'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'recipes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('prep_time_min', sa.Integer()),
        sa.Column('cook_time_min', sa.Integer()),
        sa.Column('total_time_min', sa.Integer()),
        sa.Column('servings', sa.Integer()),
        sa.Column('origin', sa.Text(), sa.CheckConstraint(
            "origin IN ('instagram', 'web', 'cookbook', 'manual', 'ios_share')",
            name='ck_recipes_origin'
        )),
        sa.Column('source_url', sa.Text()),
        sa.Column('source_citation', sa.Text()),
        sa.Column('image_s3_key', sa.Text()),
        sa.Column('thumbnail_s3_key', sa.Text()),
        sa.Column('extraction_status', sa.Text(), sa.CheckConstraint(
            "extraction_status IN ('pending', 'processing', 'complete', 'failed')",
            name='ck_recipes_extraction_status'
        )),
        sa.Column('cook_count', sa.Integer(), server_default='0'),
        sa.Column('avg_rating', sa.Numeric(3, 2)),
        sa.Column('deleted_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('recipes')

"""Create users table

Revision ID: 001_create_users
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = '001_create_users'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('email', sa.Text(), nullable=False),
        sa.Column('cognito_sub', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('email', name='uq_users_email'),
        sa.UniqueConstraint('cognito_sub', name='uq_users_cognito_sub'),
    )


def downgrade() -> None:
    op.drop_table('users')

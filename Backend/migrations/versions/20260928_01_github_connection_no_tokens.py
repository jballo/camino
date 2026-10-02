"""Remove stored GitHub OAuth tokens and track installation activity.

Revision ID: 20260928_01
Revises: None
Create Date: 2026-09-28
"""

from alembic import op


revision = "20260928_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This targeted compatibility revision predates the planned full schema
    # baseline. IF EXISTS keeps it safe both for existing app-created schemas
    # and for empty databases that create their tables after migration.
    op.execute(
        "ALTER TABLE IF EXISTS githubconnections "
        "ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true"
    )
    op.execute(
        'ALTER TABLE IF EXISTS githubconnections '
        'DROP COLUMN IF EXISTS "encryptedAccessToken", '
        'DROP COLUMN IF EXISTS "encryptedRefreshToken", '
        'DROP COLUMN IF EXISTS "tokenExpiresAt", '
        'DROP COLUMN IF EXISTS "refreshTokenExpiresAt"'
    )


def downgrade() -> None:
    op.execute(
        'ALTER TABLE IF EXISTS githubconnections '
        'ADD COLUMN IF NOT EXISTS "encryptedAccessToken" VARCHAR, '
        'ADD COLUMN IF NOT EXISTS "encryptedRefreshToken" VARCHAR, '
        'ADD COLUMN IF NOT EXISTS "tokenExpiresAt" TIMESTAMP WITH TIME ZONE, '
        'ADD COLUMN IF NOT EXISTS "refreshTokenExpiresAt" TIMESTAMP WITH TIME ZONE, '
        "DROP COLUMN IF EXISTS active"
    )

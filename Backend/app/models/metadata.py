"""Every table model, imported so ``SQLModel.metadata`` describes the schema.

Alembic compares this metadata with the database (``alembic check``). Nothing
here reads application settings, so loading it needs no environment.
"""

from sqlmodel import SQLModel

from app.models import (  # noqa: F401  (registers the tables)
    code,
    github_connection,
    job,
    rate_limit,
    repo_follow,
    user,
)

metadata = SQLModel.metadata

"""Backfill personal repository follows from historical ingestion requests.

Run once after deploying the user_repo_follows table with
``uv run python -m scripts.backfill_repo_follows``. The statement is safe to
repeat because the table has a unique constraint on (userId, repo_name).
"""

from sqlalchemy import text

from app.db import engine


BACKFILL = text(
    """
    INSERT INTO user_repo_follows ("userId", repo_name)
    SELECT DISTINCT j."userId", j.repo_name
    FROM jobs AS j
    JOIN repo_index_state AS s
      ON s.repo_name = j.repo_name
    WHERE j.job_type = 'repository_ingest'
      AND s.visibility = 'public'
    ON CONFLICT ("userId", repo_name) DO NOTHING
    """
)


def main() -> None:
    with engine.begin() as connection:
        result = connection.execute(BACKFILL)
    print(f"Added {result.rowcount} repository follow(s).")


if __name__ == "__main__":
    main()

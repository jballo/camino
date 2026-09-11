from unittest.mock import MagicMock

from app.services.search_index import (
    populate_search_vector_sql,
    rebuild_search_vector,
)


def test_populate_search_vector_is_generation_scoped():
    sql = " ".join(populate_search_vector_sql(only_null=True).split())

    assert "generation = :generation" in sql
    assert "search_vector IS NULL" in sql


def test_rebuild_search_vector_targets_active_generation():
    session = MagicMock()
    session.exec.return_value.one_or_none.return_value = "active-gen"

    rebuild_search_vector(session, "org/repo", 123)

    statement = session.execute.call_args.args[0]
    assert statement.compile().params == {
        "repo_name": "org/repo",
        "installation_id": 123,
        "generation": "active-gen",
    }
    session.commit.assert_called_once_with()


def test_rebuild_search_vector_is_noop_without_active_generation():
    session = MagicMock()
    session.exec.return_value.one_or_none.return_value = None

    rebuild_search_vector(session, "org/repo", 123)

    session.execute.assert_not_called()
    session.commit.assert_not_called()

from unittest.mock import MagicMock

from app.api.repositories import list_processed_repositories


async def test_processed_repository_counts_use_live_chunks():
    session = MagicMock()
    session.exec.return_value.one.return_value = MagicMock(installationId=123)
    session.execute.return_value.all.return_value = [
        ("org/one", 7),
        ("org/two", 3),
    ]

    result = await list_processed_repositories(session, "user_123")

    assert result == [
        {"repo_name": "org/one", "chunk_count": 7},
        {"repo_name": "org/two", "chunk_count": 3},
    ]
    sql = " ".join(str(session.execute.call_args.args[0]).split())
    assert "FROM live_code_chunks" in sql
    assert session.execute.call_args.args[1] == {"installation_id": 123}

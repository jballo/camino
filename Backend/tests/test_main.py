from unittest.mock import MagicMock, patch

from app import main


async def test_lifespan_migrates_github_user_id_for_existing_tables():
    connection = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = connection

    with (
        patch.object(main, "engine", mock_engine),
        patch.object(main.SQLModel.metadata, "create_all") as create_all,
    ):
        async with main.lifespan(main.app):
            pass

    create_all.assert_called_once_with(mock_engine)
    statements = [
        " ".join(str(call.args[0]).split())
        for call in connection.execute.call_args_list
    ]
    assert (
        'ALTER TABLE githubconnections ADD COLUMN IF NOT EXISTS "githubUserId" INTEGER'
        in statements
    )
    assert (
        'CREATE INDEX IF NOT EXISTS "ix_githubconnections_githubUserId" '
        'ON githubconnections ("githubUserId")'
        in statements
    )

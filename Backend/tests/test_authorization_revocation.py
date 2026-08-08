from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.authorization_revocation import (
    AuthorizationRevocationError,
    delete_revoked_user_connections,
)


GITHUB_USER_ID = 202


def test_deletes_connections_for_github_user():
    session = MagicMock()

    delete_revoked_user_connections(session, GITHUB_USER_ID)

    statement = str(session.exec.call_args.args[0])
    assert "DELETE FROM githubconnections" in statement
    assert 'githubconnections."githubUserId"' in statement
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_is_idempotent_when_no_rows_exist():
    session = MagicMock()

    delete_revoked_user_connections(session, GITHUB_USER_ID)

    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_rolls_back_database_failure():
    session = MagicMock()
    session.exec.side_effect = SQLAlchemyError("database unavailable")

    with pytest.raises(AuthorizationRevocationError):
        delete_revoked_user_connections(session, GITHUB_USER_ID)

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()

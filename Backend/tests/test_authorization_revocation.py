from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.authorization_revocation import (
    AuthorizationRevocationError,
    deactivate_user_connections,
)


def test_deactivates_all_connection_rows_for_github_user():
    session = MagicMock()

    deactivate_user_connections(session, 501)

    statement = str(session.exec.call_args.args[0])
    assert "UPDATE githubconnections" in statement
    assert 'githubconnections."githubUserId"' in statement
    assert "SET active=" in statement
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_rolls_back_authorization_revocation_failure():
    session = MagicMock()
    session.exec.side_effect = SQLAlchemyError("database unavailable")

    with pytest.raises(AuthorizationRevocationError):
        deactivate_user_connections(session, 501)

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()

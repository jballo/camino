from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.installation_state import (
    InstallationStateError,
    set_installation_active,
)


def test_updates_all_connection_rows_for_installation():
    session = MagicMock()

    set_installation_active(session, 101, active=False)

    statement = str(session.exec.call_args.args[0])
    assert "UPDATE githubconnections" in statement
    assert 'githubconnections."installationId"' in statement
    assert "SET active=" in statement
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_rolls_back_installation_state_failure():
    session = MagicMock()
    session.exec.side_effect = SQLAlchemyError("database unavailable")

    with pytest.raises(InstallationStateError):
        set_installation_active(session, 101, active=True)

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()

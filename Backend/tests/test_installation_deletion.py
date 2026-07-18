from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.installation_deletion import (
    InstallationDeletionError,
    delete_installation_local_data,
)


INSTALLATION_ID = 101


def test_deletes_connections_tours_and_chunks():
    session = MagicMock()

    delete_installation_local_data(session, INSTALLATION_ID)

    statements = [str(call.args[0]) for call in session.exec.call_args_list]
    assert len(statements) == 3
    assert any("DELETE FROM githubconnections" in s for s in statements)
    assert any("DELETE FROM tour_jobs" in s for s in statements)
    assert any("DELETE FROM code_chunks" in s for s in statements)
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_is_idempotent_when_no_rows_exist():
    session = MagicMock()

    delete_installation_local_data(session, INSTALLATION_ID)

    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_rolls_back_database_failure():
    session = MagicMock()
    session.exec.side_effect = SQLAlchemyError("database unavailable")

    with pytest.raises(InstallationDeletionError):
        delete_installation_local_data(session, INSTALLATION_ID)

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.account_deletion import (
    AccountDeletionError,
    delete_local_account_data,
)


USER_ID = "user_123"


def _result(values=()):
    result = MagicMock()
    result.all.return_value = list(values)
    return result


def test_local_cleanup_deletes_unreferenced_installation_and_commits():
    session = MagicMock()
    session.exec.side_effect = [
        _result([101]),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        _result([]),
        MagicMock(),
    ]

    delete_local_account_data(session, USER_ID)

    statements = [str(call.args[0]) for call in session.exec.call_args_list]
    assert len(statements) == 7
    assert any("DELETE FROM tour_jobs" in statement for statement in statements)
    assert any("DELETE FROM rate_limits" in statement for statement in statements)
    assert any("DELETE FROM githubconnections" in statement for statement in statements)
    assert any("DELETE FROM users" in statement for statement in statements)
    assert any("DELETE FROM code_chunks" in statement for statement in statements)
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_local_cleanup_preserves_shared_installation():
    session = MagicMock()
    session.exec.side_effect = [
        _result([101]),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        _result([101]),
    ]

    delete_local_account_data(session, USER_ID)

    statements = [str(call.args[0]) for call in session.exec.call_args_list]
    assert not any("DELETE FROM code_chunks" in statement for statement in statements)
    session.commit.assert_called_once_with()


def test_local_cleanup_is_idempotent_when_no_rows_exist():
    session = MagicMock()
    session.exec.side_effect = [
        _result([]),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
    ]

    delete_local_account_data(session, USER_ID)

    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_local_cleanup_rolls_back_database_failure():
    session = MagicMock()
    session.exec.side_effect = SQLAlchemyError("database unavailable")

    with pytest.raises(AccountDeletionError):
        delete_local_account_data(session, USER_ID)

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_local_cleanup_wraps_unexpected_failure_and_rolls_back():
    session = MagicMock()
    error = TypeError("unexpected data")
    session.exec.side_effect = error

    with pytest.raises(AccountDeletionError) as exc_info:
        delete_local_account_data(session, USER_ID)

    assert exc_info.value.__cause__ is error
    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()

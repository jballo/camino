from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.safe_run import Redactor, _prepare_pg_command


SAFE_RUN = Path(__file__).parents[1] / "scripts" / "safe_run.py"


def test_redacts_known_literals_and_database_url_components():
    encoded_password = "SYNTHETIC%2FENCODED%3APASSWORD"
    database_url = (
        "postgresql://synthetic_user:"
        f"{encoded_password}@db.invalid:5432/camino"
    )
    redactor = Redactor(
        {
            "DATABASE_URL": database_url,
            "CLERK_SECRET_KEY": "SYNTHETIC_LITERAL_SECRET",
        }
    )

    output = redactor.redact(
        "literal=SYNTHETIC_LITERAL_SECRET "
        "raw=SYNTHETIC/ENCODED:PASSWORD "
        f"encoded={encoded_password} "
        "userinfo=synthetic_user:SYNTHETIC%2FENCODED%3APASSWORD "
        f"url={database_url}"
    )

    assert "SYNTHETIC_LITERAL_SECRET" not in output
    assert "SYNTHETIC/ENCODED:PASSWORD" not in output
    assert encoded_password not in output
    assert "synthetic_user:" not in output
    assert database_url not in output
    assert redactor.count == 5


def test_redacts_private_key_lines_and_credential_shapes():
    private_key = (
        "-----BEGIN SYNTHETIC PRIVATE KEY-----\n"
        "SYNTHETIC_PRIVATE_KEY_LINE_1234567890\n"
        "-----END SYNTHETIC PRIVATE KEY-----"
    )
    redactor = Redactor({"GH_APP_PRIVATE_KEY": private_key})

    output = redactor.redact(
        "SYNTHETIC_PRIVATE_KEY_LINE_1234567890\n"
        "masked=sk-****ABCD token=github_pat_SYNTHETIC12345678901234567890\n"
        "-----BEGIN OTHER PRIVATE KEY-----\n"
        "UNLISTED_SYNTHETIC_KEY_MATERIAL_1234567890\n"
        "-----END OTHER PRIVATE KEY-----"
    )

    assert "SYNTHETIC_PRIVATE_KEY_LINE" not in output
    assert "sk-****ABCD" not in output
    assert "github_pat_SYNTHETIC" not in output
    assert "BEGIN OTHER PRIVATE KEY" not in output
    assert "UNLISTED_SYNTHETIC_KEY_MATERIAL" not in output


def test_streaming_redaction_hides_unknown_private_key_block_body():
    redactor = Redactor({})

    output = "".join(
        redactor.redact_line(line)
        for line in (
            "prefix -----BEGIN OTHER PRIVATE KEY-----\n",
            "UNLISTED_SYNTHETIC_KEY_MATERIAL_1234567890\n",
            "-----END OTHER PRIVATE KEY----- suffix\n",
            "ordinary output\n",
        )
    )

    assert "BEGIN OTHER PRIVATE KEY" not in output
    assert "UNLISTED_SYNTHETIC_KEY_MATERIAL" not in output
    assert "END OTHER PRIVATE KEY" not in output
    assert output.endswith("ordinary output\n")


def test_pg_mode_splits_database_url_into_minimal_environment():
    environ = {
        "PATH": "/synthetic/bin",
        "HOME": "/synthetic/home",
        "LANG": "C.UTF-8",
        "DATABASE_URL": (
            "postgresql://synthetic_user:SYNTHETIC%2FPASSWORD@db.invalid:6543/"
            "camino?sslmode=require&channel_binding=require&options=-c%20statement_timeout%3D5s"
        ),
        "OPENAI_API_KEY": "sk-SYNTHETIC_SHOULD_NOT_BE_PASSED",
    }

    command, child_env = _prepare_pg_command(["psql", "-c", "SELECT 1"], environ)

    assert command == ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-c", "SELECT 1"]
    assert child_env == {
        "PATH": "/synthetic/bin",
        "HOME": "/synthetic/home",
        "LANG": "C.UTF-8",
        "PGHOST": "db.invalid",
        "PGPORT": "6543",
        "PGUSER": "synthetic_user",
        "PGPASSWORD": "SYNTHETIC/PASSWORD",
        "PGDATABASE": "camino",
        "PGSSLMODE": "require",
        "PGCHANNELBINDING": "require",
        "PGOPTIONS": "-c statement_timeout=5s",
    }


@pytest.mark.parametrize(
    ("database_url", "message"),
    [
        (
            "postgresql+psycopg2://synthetic_user:SYNTHETIC@db.invalid/camino",
            "unsupported DATABASE_URL scheme",
        ),
        (
            "postgresql://synthetic_user:SYNTHETIC@db.invalid/camino?application_name=unsafe",
            "unsupported DATABASE_URL parameter: application_name",
        ),
    ],
)
def test_pg_mode_rejects_bad_urls(database_url, message):
    with pytest.raises(ValueError, match=message):
        _prepare_pg_command(["psql"], {"DATABASE_URL": database_url})


@pytest.mark.parametrize(
    "command",
    [
        ["psql", "-d", "camino"],
        ["psql", "-dcamino"],
        ["psql", "--dbname=camino"],
        ["pg_dump", "postgresql://db.invalid/camino"],
    ],
)
def test_pg_mode_rejects_database_and_url_arguments(command):
    with pytest.raises(ValueError):
        _prepare_pg_command(
            command,
            {
                "DATABASE_URL": (
                    "postgresql://synthetic_user:SYNTHETIC@db.invalid/camino"
                )
            },
        )


def test_child_exit_code_is_passed_through():
    result = subprocess.run(
        [
            sys.executable,
            str(SAFE_RUN),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 7
    assert result.stdout == ""
    assert result.stderr == ""

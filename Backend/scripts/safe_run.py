#!/usr/bin/env python3
"""Run a command while preventing configured credentials from reaching output."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit


SECRET_ENV_NAMES = (
    "DATABASE_URL",
    "CLERK_WH_KEY",
    "CLERK_SECRET_KEY",
    "GH_APP_ID",
    "GH_APP_CLIENT_ID",
    "GH_APP_SECRET",
    "GH_APP_PRIVATE_KEY",
    "ENCRYPTION_KEY",
    "GH_WEBHOOK_SECRET",
    "OPENAI_API_KEY",
)

_BACKSTOP_PATTERNS = (
    ("URL_USERINFO", re.compile(r"://[^\s/@]*:[^\s/@]*@")),
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
            r"-----END [^-\r\n]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("PRIVATE_KEY", re.compile(r"-----(?:BEGIN|END) [^-\r\n]*PRIVATE KEY-----")),
    ("OPENAI_KEY", re.compile(r"sk-[A-Za-z0-9_*-]{4,}")),
    ("STRIPE_KEY", re.compile(r"sk_(?:live|test)_\w+")),
    ("WEBHOOK_SECRET", re.compile(r"whsec_\w+")),
    ("GITHUB_TOKEN", re.compile(r"gh[pousr]_\w{20,}")),
    ("GITHUB_TOKEN", re.compile(r"github_pat_\w+")),
)


@dataclass(frozen=True)
class Redaction:
    value: str
    name: str


class Redactor:
    """Replace known secret values and common credential-shaped strings."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        candidates: list[Redaction] = []
        for name in SECRET_ENV_NAMES:
            value = environ.get(name, "")
            if not value:
                continue
            candidates.append(Redaction(value, name))
            if "\n" in value or "\r" in value:
                for line in value.splitlines():
                    if len(line) >= 16:
                        candidates.append(Redaction(line, name))

        database_url = environ.get("DATABASE_URL", "")
        if database_url:
            candidates.extend(_database_url_redactions(database_url))

        # Stable de-duplication ensures one replacement label per literal.
        unique: dict[str, str] = {}
        for candidate in candidates:
            unique.setdefault(candidate.value, candidate.name)
        self._literals = tuple(
            Redaction(value, name)
            for value, name in sorted(
                unique.items(), key=lambda item: len(item[0]), reverse=True
            )
        )
        self.count = 0
        self._in_private_key_block = False

    def redact(self, text: str) -> str:
        for item in self._literals:
            occurrences = text.count(item.value)
            if occurrences:
                text = text.replace(item.value, f"[REDACTED:{item.name}]")
                self.count += occurrences

        for name, pattern in _BACKSTOP_PATTERNS:
            text, replacements = pattern.subn(f"[REDACTED:{name}]", text)
            self.count += replacements
        return text

    def redact_line(self, line: str) -> str:
        """Redact one streamed line, including unknown PEM block contents."""

        if self._in_private_key_block:
            self.count += 1
            if re.search(r"-----END [^-\r\n]*PRIVATE KEY-----", line):
                self._in_private_key_block = False
            return "[REDACTED:PRIVATE_KEY]\n" if line.endswith("\n") else "[REDACTED:PRIVATE_KEY]"

        if re.search(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----", line):
            self.count += 1
            self._in_private_key_block = not bool(
                re.search(r"-----END [^-\r\n]*PRIVATE KEY-----", line)
            )
            return "[REDACTED:PRIVATE_KEY]\n" if line.endswith("\n") else "[REDACTED:PRIVATE_KEY]"
        return self.redact(line)


def _database_url_redactions(database_url: str) -> list[Redaction]:
    """Return URL component redactions without ever raising with URL contents."""

    try:
        parsed = urlsplit(database_url)
        password = parsed.password
    except ValueError:
        return []

    redactions: list[Redaction] = []
    raw_userinfo = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
    if raw_userinfo:
        redactions.append(Redaction(raw_userinfo, "DATABASE_URL_USERINFO"))

    raw_password = raw_userinfo.split(":", 1)[1] if ":" in raw_userinfo else ""
    if raw_password:
        redactions.append(Redaction(raw_password, "DATABASE_URL_PASSWORD"))

    if password:
        decoded_password = unquote(password)
        redactions.append(Redaction(decoded_password, "DATABASE_URL_PASSWORD"))
        redactions.append(
            Redaction(quote(decoded_password, safe=""), "DATABASE_URL_PASSWORD")
        )
    return [item for item in redactions if item.value]


def _minimal_pg_environment(environ: Mapping[str, str]) -> dict[str, str]:
    database_url = environ.get("DATABASE_URL", "")
    try:
        parsed = urlsplit(database_url)
    except ValueError as exc:
        raise ValueError("invalid DATABASE_URL") from exc

    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("unsupported DATABASE_URL scheme")

    allowed_params = {
        "sslmode": "PGSSLMODE",
        "channel_binding": "PGCHANNELBINDING",
        "options": "PGOPTIONS",
    }
    pg_params: dict[str, str] = {}
    try:
        query_items = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError("invalid DATABASE_URL query") from exc
    for name, value in query_items:
        if name not in allowed_params:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"unsupported DATABASE_URL parameter: {name}")
            raise ValueError("unsupported DATABASE_URL parameter name")
        pg_params[allowed_params[name]] = value

    try:
        host = parsed.hostname
        port = parsed.port or 5432
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
    except ValueError as exc:
        raise ValueError("invalid DATABASE_URL") from exc

    database = unquote(parsed.path.removeprefix("/"))
    if not host or not user or not database:
        raise ValueError("DATABASE_URL must include host, user, and database")

    child_env = {
        "PATH": environ.get("PATH", os.defpath),
        "HOME": environ.get("HOME", ""),
        "LANG": environ.get("LANG", "C.UTF-8"),
        "PGHOST": host,
        "PGPORT": str(port),
        "PGUSER": user,
        "PGPASSWORD": password,
        "PGDATABASE": database,
    }
    child_env.update(pg_params)
    return child_env


def _prepare_pg_command(
    command: Sequence[str], environ: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    if not command:
        raise ValueError("missing child command")
    executable = Path(command[0]).name
    if executable not in {"psql", "pg_dump"}:
        raise ValueError("--pg only permits psql or pg_dump")

    for argument in command[1:]:
        if (
            argument == "-d"
            or argument.startswith("-d")
            or argument == "--dbname"
            or argument.startswith("--dbname=")
        ):
            raise ValueError("database arguments are not permitted in --pg mode")
        if "://" in argument:
            raise ValueError("URL arguments are not permitted in --pg mode")

    child_env = _minimal_pg_environment(environ)
    prepared = list(command)
    if executable == "psql":
        prepared[1:1] = ["-X", "-v", "ON_ERROR_STOP=1"]
    return prepared, child_env


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    redactor = Redactor(os.environ)
    command = list(args.command)
    child_env: Mapping[str, str] | None = None

    if args.pg:
        try:
            command, child_env = _prepare_pg_command(command, os.environ)
        except ValueError as exc:
            print(redactor.redact(f"safe_run: {exc}"), flush=True)
            return 2

    try:
        process = subprocess.Popen(
            command,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except OSError:
        print("safe_run: unable to start child process", flush=True)
        return 127

    assert process.stdout is not None
    for line in process.stdout:
        print(redactor.redact_line(line), end="", flush=True)
    return_code = process.wait()
    if redactor.count:
        print(f"safe_run: {redactor.count} redactions", flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())

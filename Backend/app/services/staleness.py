"""Measure an indexed repository snapshot against its current GitHub head."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import logging
import re
from urllib.parse import quote

import requests

from app.services.github_app import installation_access_token
from app.services.jobs import normalize_repository_name

logger = logging.getLogger(__name__)

_GITHUB_API_VERSION = "2022-11-28"
_COMPARE_TIMEOUT_SECONDS = (10, 30)
_MAX_COMPARE_COMMITS = 250
_MAX_COMPARE_FILES = 300
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_SEPARATOR = re.compile(r"[^a-z0-9]+")
_GENERIC_TOKENS = {
    "app",
    "apps",
    "code",
    "common",
    "component",
    "components",
    "config",
    "core",
    "file",
    "files",
    "helper",
    "helpers",
    "index",
    "lib",
    "main",
    "module",
    "package",
    "spec",
    "specs",
    "src",
    "test",
    "tests",
    "util",
    "utils",
}


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str


@dataclass(frozen=True)
class HeadComparison:
    head_sha: str | None
    commits_behind: int | None
    changed_files: tuple[ChangedFile, ...]
    measurable: bool


@dataclass(frozen=True)
class OverlapVerdict:
    overlaps: bool
    matched_files: tuple[str, ...]
    matched_terms: tuple[str, ...]

    @property
    def overlap(self) -> bool:
        """Singular alias for callers that treat the result as a verdict."""
        return self.overlaps

    @property
    def relevant(self) -> bool:
        return self.overlaps

    @property
    def has_overlap(self) -> bool:
        return self.overlaps


def _unmeasurable() -> HeadComparison:
    return HeadComparison(
        head_sha=None,
        commits_behind=None,
        changed_files=(),
        measurable=False,
    )


def compare_to_head(
    repo_name: str,
    installation_id: int,
    indexed_sha: str | None,
) -> HeadComparison:
    """Compare an indexed SHA to GitHub's live default-branch ``HEAD``.

    GitHub comparison failures are deliberately data, not exceptions: callers
    must be able to publish a tour with an honest unknown-freshness disclosure.
    """
    if not indexed_sha:
        logger.info(
            "freshness unmeasurable | repo=%r reason=missing_indexed_sha",
            repo_name,
        )
        return _unmeasurable()

    try:
        normalized_repo = normalize_repository_name(repo_name)
        token = installation_access_token(installation_id)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }
        repository_response = requests.get(
            f"https://api.github.com/repos/{normalized_repo}",
            headers=headers,
            timeout=_COMPARE_TIMEOUT_SECONDS,
        )
        try:
            if repository_response.status_code >= 400:
                logger.warning(
                    "freshness unmeasurable | repo=%r status=%s "
                    "reason=repository_lookup_error",
                    normalized_repo,
                    repository_response.status_code,
                )
                return _unmeasurable()
            default_branch = repository_response.json().get("default_branch")
        finally:
            repository_response.close()
        if not isinstance(default_branch, str) or not default_branch:
            logger.warning(
                "freshness unmeasurable | repo=%r reason=missing_default_branch",
                normalized_repo,
            )
            return _unmeasurable()

        base = quote(indexed_sha, safe="")
        head = quote(default_branch, safe="")
        response = requests.get(
            f"https://api.github.com/repos/{normalized_repo}/compare/{base}...{head}",
            headers=headers,
            timeout=_COMPARE_TIMEOUT_SECONDS,
        )
        try:
            if response.status_code >= 400:
                logger.warning(
                    "freshness unmeasurable | repo=%r status=%s reason=github_error",
                    normalized_repo,
                    response.status_code,
                )
                return _unmeasurable()
            payload = response.json()
        finally:
            response.close()

        commits = payload.get("commits") or []
        files = payload.get("files") or []
        total_commits = payload.get("total_commits")
        truncated = (
            (isinstance(total_commits, int) and total_commits > len(commits))
            or len(commits) >= _MAX_COMPARE_COMMITS
            or len(files) >= _MAX_COMPARE_FILES
        )
        if truncated:
            logger.warning(
                "freshness unmeasurable | repo=%r reason=truncated commits=%d "
                "total_commits=%r files=%d",
                normalized_repo,
                len(commits),
                total_commits,
                len(files),
            )
            return _unmeasurable()

        changed_files = tuple(
            ChangedFile(path=item["filename"], status=item.get("status", "modified"))
            for item in files
            if isinstance(item, Mapping) and isinstance(item.get("filename"), str)
        )
        head_sha = (
            commits[-1].get("sha")
            if commits and isinstance(commits[-1], Mapping)
            else (payload.get("base_commit") or {}).get("sha")
        )
        commits_behind = payload.get("ahead_by")
        if not isinstance(head_sha, str) or not isinstance(commits_behind, int):
            logger.warning(
                "freshness unmeasurable | repo=%r reason=malformed_compare_response",
                normalized_repo,
            )
            return _unmeasurable()

        logger.info(
            "freshness measured | repo=%r indexed_sha=%s head_sha=%s "
            "commits_behind=%d changed_files=%d",
            normalized_repo,
            indexed_sha,
            head_sha,
            commits_behind,
            len(changed_files),
        )
        return HeadComparison(
            head_sha=head_sha,
            commits_behind=commits_behind,
            changed_files=changed_files,
            measurable=True,
        )
    except Exception:
        logger.exception(
            "freshness unmeasurable | repo=%r reason=compare_exception",
            repo_name,
        )
        return _unmeasurable()


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("ing"):
        token = token[:-3]
        if len(token) > 2 and token[-1] == token[-2]:
            token = token[:-1]
        return token
    if len(token) > 3 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 3 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _tokens(value: str) -> set[str]:
    camel_split = _CAMEL_BOUNDARY.sub(" ", value)
    return {
        stemmed
        for raw in _TOKEN_SEPARATOR.split(camel_split.lower())
        if raw and (stemmed := _stem(raw)) not in _GENERIC_TOKENS
    }


def _changed_path(changed_file: ChangedFile | Mapping[str, str] | str) -> str:
    if isinstance(changed_file, ChangedFile):
        return changed_file.path
    if isinstance(changed_file, str):
        return changed_file
    return changed_file.get("path") or changed_file.get("filename") or ""


def _directories_overlap(changed_path: str, cited_path: str) -> bool:
    changed_parts = changed_path.strip("/").split("/")[:-1]
    cited_parts = cited_path.strip("/").split("/")[:-1]
    if not changed_parts or not cited_parts:
        return changed_path == cited_path
    shortest = min(len(changed_parts), len(cited_parts))
    return changed_parts[:shortest] == cited_parts[:shortest]


def relevant_overlap(
    changed_files: Iterable[ChangedFile | Mapping[str, str] | str],
    cited_paths: Iterable[str],
    query_terms: Iterable[str] | str,
) -> OverlapVerdict:
    """Apply directory and query-keyword signals to changed file paths."""
    citations = tuple(path.strip("/") for path in cited_paths)
    query_values = (query_terms,) if isinstance(query_terms, str) else query_terms
    query_token_set = set().union(*(_tokens(value) for value in query_values))
    matched_files: set[str] = set()
    matched_terms: set[str] = set()

    for item in changed_files:
        path = _changed_path(item).strip("/")
        directory_match = any(
            _directories_overlap(path, citation) for citation in citations
        )
        keyword_matches = _tokens(path) & query_token_set
        if directory_match or keyword_matches:
            matched_files.add(path)
            matched_terms.update(keyword_matches)
        logger.info(
            "freshness overlap decision | changed_path=%r directory_match=%s "
            "matched_terms=%s relevant=%s",
            path,
            directory_match,
            sorted(keyword_matches),
            directory_match or bool(keyword_matches),
        )

    verdict = OverlapVerdict(
        overlaps=bool(matched_files),
        matched_files=tuple(sorted(matched_files)),
        matched_terms=tuple(sorted(matched_terms)),
    )
    logger.info(
        "freshness overlap verdict | relevant=%s matched_files=%s matched_terms=%s",
        verdict.overlaps,
        verdict.matched_files,
        verdict.matched_terms,
    )
    return verdict

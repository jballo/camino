"""Resolve the branch contributors should target when opening pull requests."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
import datetime as dt
import logging
import re
from urllib.parse import quote

import requests

from app.services.github_app import installation_access_token
from app.services.jobs import normalize_repository_name

logger = logging.getLogger(__name__)

_GITHUB_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT_SECONDS = (10, 30)
_DOC_PATH_GROUPS = (
    (
        "CONTRIBUTING.md",
        ".github/CONTRIBUTING.md",
        "docs/CONTRIBUTING.md",
    ),
    (
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/pull_request_template.md",
        "PULL_REQUEST_TEMPLATE.md",
    ),
)
_REF = r"(?P<branch>[A-Za-z0-9._/-]+)"
_QUOTE = r"[`\"'‘’]?"
_BRANCH_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"\b(?:target|merge(?:d)?)\s+(?:your\s+)?(?:pull requests?\s+)?"
        rf"(?:changes\s+)?(?:(?:against|into|to)\s+)?(?:the\s+)?"
        rf"{_QUOTE}{_REF}{_QUOTE}\s+branch\b",
        rf"\bbase(?:d)?\s+branch\s+(?:is|should\s+be|must\s+be)\s+"
        rf"(?:the\s+)?{_QUOTE}{_REF}{_QUOTE}\b",
        rf"\bbranch\s+off\s+(?:of\s+)?(?:the\s+)?{_QUOTE}{_REF}{_QUOTE}\b",
        rf"\bpull requests?\s+(?:(?:should|must)\s+)?(?:target\s+)?"
        rf"(?:against|into|to)\s+(?:the\s+)?{_QUOTE}{_REF}{_QUOTE}\b",
    )
)
_GENERIC_BRANCH_WORDS = {
    "a",
    "base",
    "default",
    "feature",
    "new",
    "that",
    "the",
    "this",
    "your",
}


@dataclass(frozen=True)
class TargetBranchResolution:
    branch: str | None
    source: str
    evidence: str | None
    evidence_path: str | None
    default_branch: str | None
    checked_at: dt.datetime


def _resolution(
    *,
    checked_at: dt.datetime,
    default_branch: str | None = None,
    branch: str | None = None,
    source: str = "unresolved",
    evidence: str | None = None,
    evidence_path: str | None = None,
) -> TargetBranchResolution:
    return TargetBranchResolution(
        branch=branch,
        source=source,
        evidence=evidence,
        evidence_path=evidence_path,
        default_branch=default_branch,
        checked_at=checked_at,
    )


def _headers(token: str, *, raw: bool = False) -> dict[str, str]:
    return {
        "Accept": (
            "application/vnd.github.raw+json"
            if raw
            else "application/vnd.github+json"
        ),
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }


def _verify_branch(
    repo_name: str,
    branch: str,
    headers: dict[str, str],
) -> bool:
    response = None
    try:
        response = requests.get(
            f"https://api.github.com/repos/{repo_name}/branches/"
            f"{quote(branch, safe='')}",
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        return response.status_code < 400
    except Exception:
        logger.exception(
            "target branch candidate verification failed | repo=%r branch=%r",
            repo_name,
            branch,
        )
        return False
    finally:
        if response is not None:
            response.close()


def _candidate_from_document(
    repo_name: str,
    path: str,
    content: str,
    headers: dict[str, str],
) -> tuple[str, str] | None:
    for line in content.splitlines():
        evidence = line.strip()
        if not evidence:
            continue
        for pattern in _BRANCH_PATTERNS:
            match = pattern.search(evidence)
            if match is None:
                continue
            branch = match.group("branch")
            if branch.casefold() in _GENERIC_BRANCH_WORDS:
                continue
            if _verify_branch(repo_name, branch, headers):
                logger.info(
                    "target branch contributing signal accepted | repo=%r "
                    "path=%r branch=%r evidence=%r",
                    repo_name,
                    path,
                    branch,
                    evidence,
                )
                return branch, evidence
            logger.info(
                "target branch contributing signal rejected | repo=%r "
                "path=%r branch=%r reason=branch_not_found evidence=%r",
                repo_name,
                path,
                branch,
                evidence,
            )
    return None


def _document_signal(
    repo_name: str,
    token: str,
    api_headers: dict[str, str],
) -> tuple[str, str, str, str] | None:
    raw_headers = _headers(token, raw=True)
    for paths in _DOC_PATH_GROUPS:
        document: tuple[str, str] | None = None
        for path in paths:
            response = None
            try:
                response = requests.get(
                    f"https://api.github.com/repos/{repo_name}/contents/"
                    f"{quote(path, safe='/')}",
                    headers=raw_headers,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
                if response.status_code == 404:
                    continue
                if response.status_code >= 400:
                    logger.warning(
                        "target branch document lookup failed | repo=%r "
                        "path=%r status=%s",
                        repo_name,
                        path,
                        response.status_code,
                    )
                    continue
                document = (path, response.text)
                break
            except Exception:
                logger.exception(
                    "target branch document lookup failed | repo=%r path=%r",
                    repo_name,
                    path,
                )
            finally:
                if response is not None:
                    response.close()

        if document is None:
            continue
        path, content = document
        candidate = _candidate_from_document(
            repo_name,
            path,
            content,
            api_headers,
        )
        if candidate is not None:
            branch, evidence = candidate
            source = (
                "contributing_doc"
                if "CONTRIBUTING" in path.upper()
                else "pr_template"
            )
            return branch, source, evidence, path

    logger.info("target branch rung 2 inconclusive | repo=%r", repo_name)
    return None


def _merged_pr_signal(
    repo_name: str,
    headers: dict[str, str],
) -> tuple[str, str] | None:
    response = None
    try:
        response = requests.get(
            f"https://api.github.com/repos/{repo_name}/pulls",
            headers=headers,
            params={
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": 30,
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            logger.warning(
                "target branch merged PR lookup failed | repo=%r status=%s",
                repo_name,
                response.status_code,
            )
            return None
        payload = response.json()
        if not isinstance(payload, list):
            logger.warning(
                "target branch merged PR lookup failed | repo=%r "
                "reason=malformed_response",
                repo_name,
            )
            return None
        branches = []
        for item in payload:
            if not isinstance(item, Mapping) or not item.get("merged_at"):
                continue
            base = item.get("base")
            branch = base.get("ref") if isinstance(base, Mapping) else None
            if isinstance(branch, str) and branch:
                branches.append(branch)
            if len(branches) == 10:
                break
        if len(branches) < 3:
            logger.info(
                "target branch rung 3 inconclusive | repo=%r sample_size=%d "
                "reason=insufficient_sample",
                repo_name,
                len(branches),
            )
            return None
        branch, count = Counter(branches).most_common(1)[0]
        if count * 2 <= len(branches):
            logger.info(
                "target branch rung 3 inconclusive | repo=%r sample_size=%d "
                "top_branch=%r top_count=%d reason=no_strict_majority",
                repo_name,
                len(branches),
                branch,
                count,
            )
            return None
        evidence = (
            f"{count} of {len(branches)} recently merged PRs targeted `{branch}`"
        )
        logger.info(
            "target branch merged PR signal accepted | repo=%r branch=%r "
            "evidence=%r",
            repo_name,
            branch,
            evidence,
        )
        return branch, evidence
    except Exception:
        logger.exception("target branch merged PR lookup failed | repo=%r", repo_name)
        return None
    finally:
        if response is not None:
            response.close()


def resolve_target_branch(
    repo_name: str,
    installation_id: int,
) -> TargetBranchResolution:
    """Resolve the pull-request base branch from GitHub's live repository data.

    The current ladder checks contribution docs and PR templates, recently merged
    PRs, then the repository default branch. A future issue-aware caller can add
    rung 1 (an explicit maintainer instruction in the issue thread) ahead of this
    resolver. GitHub failures are returned as data so discovery never blocks a
    broader workflow.
    """
    checked_at = dt.datetime.now(dt.UTC)
    repository_response = None
    try:
        normalized_repo = normalize_repository_name(repo_name)
        token = installation_access_token(installation_id)
        headers = _headers(token)
        repository_response = requests.get(
            f"https://api.github.com/repos/{normalized_repo}",
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if repository_response.status_code >= 400:
            logger.warning(
                "target branch unresolved | repo=%r status=%s "
                "reason=repository_lookup_error",
                normalized_repo,
                repository_response.status_code,
            )
            return _resolution(checked_at=checked_at)
        payload = repository_response.json()
        default_branch = (
            payload.get("default_branch") if isinstance(payload, Mapping) else None
        )
        if not isinstance(default_branch, str) or not default_branch:
            default_branch = None
            logger.warning(
                "target branch repository metadata incomplete | repo=%r "
                "reason=missing_default_branch",
                normalized_repo,
            )
    except Exception:
        logger.exception(
            "target branch unresolved | repo=%r reason=repository_lookup_exception",
            repo_name,
        )
        return _resolution(checked_at=checked_at)
    finally:
        if repository_response is not None:
            repository_response.close()

    document = _document_signal(normalized_repo, token, headers)
    if document is not None:
        branch, source, evidence, path = document
        return _resolution(
            checked_at=checked_at,
            default_branch=default_branch,
            branch=branch,
            source=source,
            evidence=evidence,
            evidence_path=path,
        )

    merged_prs = _merged_pr_signal(normalized_repo, headers)
    if merged_prs is not None:
        branch, evidence = merged_prs
        return _resolution(
            checked_at=checked_at,
            default_branch=default_branch,
            branch=branch,
            source="merged_prs",
            evidence=evidence,
        )

    if default_branch is not None:
        logger.info(
            "target branch default accepted | repo=%r branch=%r "
            "evidence=%r",
            normalized_repo,
            default_branch,
            "repository default branch",
        )
        return _resolution(
            checked_at=checked_at,
            default_branch=default_branch,
            branch=default_branch,
            source="default_branch",
            evidence="repository default branch",
        )

    logger.warning(
        "target branch unresolved | repo=%r reason=resolution_ladder_exhausted",
        normalized_repo,
    )
    return _resolution(checked_at=checked_at)

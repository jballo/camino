"""Resolve a selected repository to its upstream and measure fork drift."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from urllib.parse import quote

import requests

from app.services.github_app import installation_access_token
from app.services.jobs import normalize_repository_name

logger = logging.getLogger(__name__)
_API_VERSION = "2022-11-28"
_TIMEOUT = (10, 30)


@dataclass(frozen=True)
class ForkStatus:
    requested_repo: str
    upstream_repo: str
    fork_repo: str | None
    is_fork: bool
    target_branch: str | None
    commits_behind: int | None
    measurable: bool


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": _API_VERSION,
    }


def resolve_fork_status(
    repo_name: str,
    installation_id: int,
    target_branch: str | None = None,
) -> ForkStatus:
    requested = normalize_repository_name(repo_name)
    response = None
    try:
        token = installation_access_token(installation_id)
        headers = _headers(token)
        response = requests.get(
            f"https://api.github.com/repos/{requested}",
            headers=headers,
            timeout=_TIMEOUT,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"repository lookup returned {response.status_code}")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("repository lookup returned invalid data")
        parent = payload.get("parent")
        upstream = parent.get("full_name") if isinstance(parent, Mapping) else None
        if not isinstance(upstream, str) or not upstream:
            return ForkStatus(
                requested_repo=requested,
                upstream_repo=requested,
                fork_repo=None,
                is_fork=False,
                target_branch=target_branch,
                commits_behind=0 if target_branch else None,
                measurable=bool(target_branch),
            )
        upstream = normalize_repository_name(upstream)
        if not target_branch:
            return ForkStatus(requested, upstream, requested, True, None, None, False)
        response.close()
        response = requests.get(
            f"https://api.github.com/repos/{upstream}/compare/"
            f"{quote(target_branch, safe='')}..."
            f"{quote(requested.split('/', 1)[0] + ':' + target_branch, safe=':')}",
            headers=headers,
            timeout=_TIMEOUT,
        )
        if response.status_code >= 400:
            return ForkStatus(requested, upstream, requested, True, target_branch, None, False)
        comparison = response.json()
        behind = comparison.get("behind_by") if isinstance(comparison, Mapping) else None
        return ForkStatus(
            requested, upstream, requested, True, target_branch,
            behind if isinstance(behind, int) else None,
            isinstance(behind, int),
        )
    except Exception:
        logger.exception("fork status unavailable | repo=%r", repo_name)
        return ForkStatus(requested, requested, None, False, target_branch, None, False)
    finally:
        if response is not None:
            response.close()

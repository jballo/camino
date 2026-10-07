"""Read an issue thread and derive issue-specific contribution warnings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import re

import requests

from app.models.brief import BriefWarning
from app.services.github_app import installation_access_token
from app.services.jobs import normalize_repository_name

logger = logging.getLogger(__name__)

_API_VERSION = "2022-11-28"
_TIMEOUT = (10, 30)
_MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
_BRANCH_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:target|base|merge into|open (?:the )?pr (?:against|to))\s+"
        r"(?:the\s+)?[`'\"]?(?P<branch>[A-Za-z0-9._/-]+)[`'\"]?"
        r"(?:\s+branch)?\b",
        r"\b(?:use|branch (?:from|off))\s+[`'\"]?"
        r"(?P<branch>[A-Za-z0-9._/-]+)[`'\"]?\s+(?:as the base|branch)\b",
    )
)


@dataclass(frozen=True)
class BranchInstruction:
    branch: str
    evidence: str
    author: str | None


@dataclass(frozen=True)
class IssueThread:
    repo_name: str
    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    comments: tuple[dict[str, str | None], ...]
    html_url: str
    warnings: tuple[BriefWarning, ...]
    branch_instruction: BranchInstruction | None


class IssueThreadError(RuntimeError):
    """The issue could not be read with the requesting user's installation."""


def _headers(token: str, *, timeline: bool = False) -> dict[str, str]:
    return {
        "Accept": (
            "application/vnd.github.mockingbird-preview+json"
            if timeline
            else "application/vnd.github+json"
        ),
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": _API_VERSION,
    }


def _get_json(url: str, headers: dict[str, str]) -> object:
    response = requests.get(url, headers=headers, timeout=_TIMEOUT)
    try:
        if response.status_code >= 400:
            raise IssueThreadError(
                f"GitHub issue lookup returned {response.status_code}"
            )
        return response.json()
    except (TypeError, ValueError) as error:
        raise IssueThreadError("GitHub issue lookup returned invalid data") from error
    finally:
        response.close()


def _branch_instruction(
    comments: list[object],
) -> BranchInstruction | None:
    for item in comments:
        if not isinstance(item, Mapping):
            continue
        association = str(item.get("author_association") or "").upper()
        body = item.get("body")
        if association not in _MAINTAINER_ASSOCIATIONS or not isinstance(body, str):
            continue
        for line in body.splitlines():
            for pattern in _BRANCH_PATTERNS:
                match = pattern.search(line)
                if match is not None:
                    user = item.get("user")
                    author = user.get("login") if isinstance(user, Mapping) else None
                    return BranchInstruction(
                        branch=match.group("branch"),
                        evidence=line.strip(),
                        author=author if isinstance(author, str) else None,
                    )
    return None


def _warnings(
    *, issue: Mapping, labels: tuple[str, ...], timeline: list[object]
) -> tuple[BriefWarning, ...]:
    warnings: list[BriefWarning] = []
    if issue.get("state") == "closed":
        warnings.append(BriefWarning(kind="closed", message="This issue is closed."))
    assignees = issue.get("assignees") or []
    if assignees:
        names = [
            item.get("login")
            for item in assignees
            if isinstance(item, Mapping) and isinstance(item.get("login"), str)
        ]
        suffix = f" ({', '.join(names)})" if names else ""
        warnings.append(
            BriefWarning(kind="assigned", message=f"This issue is already assigned{suffix}.")
        )
    lowered = {label.casefold() for label in labels}
    if any("wontfix" in label or "won't fix" in label for label in lowered):
        warnings.append(
            BriefWarning(kind="wontfix", message="This issue is marked as won't fix.")
        )
    if any("discussion" in label or "needs-discussion" in label for label in lowered):
        warnings.append(
            BriefWarning(
                kind="needs_discussion",
                message="Maintainer discussion is requested before implementation.",
            )
        )

    seen_prs: set[str] = set()
    for event in timeline:
        if not isinstance(event, Mapping) or event.get("event") != "cross-referenced":
            continue
        source = event.get("source")
        source_issue = source.get("issue") if isinstance(source, Mapping) else None
        pull_request = (
            source_issue.get("pull_request")
            if isinstance(source_issue, Mapping)
            else None
        )
        if not isinstance(pull_request, Mapping) or source_issue.get("state") != "open":
            continue
        url = source_issue.get("html_url")
        if not isinstance(url, str) or url in seen_prs:
            continue
        seen_prs.add(url)
        number = source_issue.get("number")
        warnings.append(
            BriefWarning(
                kind="open_pr",
                message=(
                    f"Someone already has an open PR for this issue"
                    + (f" (#{number})." if isinstance(number, int) else ".")
                ),
                url=url,
            )
        )
    return tuple(warnings)


def fetch_issue_thread(
    repo_name: str,
    issue_number: int,
    installation_id: int,
) -> IssueThread:
    normalized_repo = normalize_repository_name(repo_name)
    token = installation_access_token(installation_id)
    root = f"https://api.github.com/repos/{normalized_repo}"
    try:
        issue_payload = _get_json(
            f"{root}/issues/{issue_number}", _headers(token)
        )
    except IssueThreadError:
        raise
    except Exception as error:
        raise IssueThreadError("GitHub issue lookup failed") from error

    try:
        comments_payload = _get_json(
            f"{root}/issues/{issue_number}/comments?per_page=100", _headers(token)
        )
    except Exception:
        logger.exception(
            "issue comments unavailable | repo=%r issue=%d", normalized_repo, issue_number
        )
        comments_payload = []
    try:
        timeline_payload = _get_json(
            f"{root}/issues/{issue_number}/timeline?per_page=100",
            _headers(token, timeline=True),
        )
    except Exception:
        logger.exception(
            "issue timeline unavailable | repo=%r issue=%d", normalized_repo, issue_number
        )
        timeline_payload = []

    if not isinstance(issue_payload, Mapping) or "pull_request" in issue_payload:
        raise IssueThreadError("The URL does not identify a GitHub issue")
    comments_list = comments_payload if isinstance(comments_payload, list) else []
    timeline_list = timeline_payload if isinstance(timeline_payload, list) else []
    title = issue_payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise IssueThreadError("GitHub issue lookup returned invalid data")
    labels = tuple(
        label["name"]
        for label in (issue_payload.get("labels") or [])
        if isinstance(label, Mapping) and isinstance(label.get("name"), str)
    )
    assignees = tuple(
        assignee["login"]
        for assignee in (issue_payload.get("assignees") or [])
        if isinstance(assignee, Mapping) and isinstance(assignee.get("login"), str)
    )
    comments = tuple(
        {
            "author": (
                item.get("user", {}).get("login")
                if isinstance(item.get("user"), Mapping)
                else None
            ),
            "author_association": str(item.get("author_association") or ""),
            "body": str(item.get("body") or ""),
        }
        for item in comments_list
        if isinstance(item, Mapping)
    )
    return IssueThread(
        repo_name=normalized_repo,
        number=issue_number,
        title=title.strip(),
        body=str(issue_payload.get("body") or ""),
        state=str(issue_payload.get("state") or "open"),
        labels=labels,
        assignees=assignees,
        comments=comments,
        html_url=str(issue_payload.get("html_url") or ""),
        warnings=_warnings(issue=issue_payload, labels=labels, timeline=timeline_list),
        branch_instruction=_branch_instruction(comments_list),
    )

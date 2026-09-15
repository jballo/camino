"""Entry point for issue brief generation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import datetime as dt
import threading

from langchain_openai import ChatOpenAI
from sqlmodel import Session, select

from app.brief.graph import DEFAULT_MAX_ATTEMPTS, DEFAULT_SEARCH_LIMIT, build_brief_graph
from app.config import settings
from app.models.brief import BriefArtifact, BriefConfidence, BriefSetupRecipe
from app.models.code import RepoIndexState
from app.models.tour import TourFreshness
from app.services.fork_status import ForkStatus
from app.services.issue_thread import IssueThread
from app.services.target_branch import TargetBranchResolution


class BriefGenerationError(RuntimeError):
    pass


class BriefGenerationCancelledError(BriefGenerationError):
    pass


async def generate_brief(
    session: Session,
    *,
    issue: IssueThread,
    repo_name: str,
    ref: str,
    installation_id: int,
    target_resolution: TargetBranchResolution,
    fork_status: ForkStatus,
    allow_stale: bool = False,
    model: str | None = None,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    cancel_event: threading.Event | None = None,
) -> BriefArtifact:
    llm = ChatOpenAI(
        model=model or settings.agent_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )
    graph = build_brief_graph(
        session, llm, search_limit=search_limit, max_attempts=max_attempts
    )
    task = asyncio.create_task(
        graph.ainvoke(
            {
                "issue": issue,
                "repo_name": repo_name,
                "ref": ref,
                "installation_id": installation_id,
                "target_resolution": target_resolution,
                "fork_status": fork_status,
                "allow_stale": allow_stale,
            }
        )
    )
    try:
        while not task.done():
            if cancel_event is not None and cancel_event.is_set():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise BriefGenerationCancelledError("Brief generation was cancelled")
            await asyncio.wait({task}, timeout=0.1)
        final = await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    synthesis = final.get("synthesis")
    draft = final.get("draft")
    if synthesis is None or draft is None:
        raise BriefGenerationError("Brief pipeline produced no draft")
    comparison = final.get("comparison")
    freshness = None
    if comparison is not None:
        index_state = session.exec(
            select(RepoIndexState).where(
                RepoIndexState.repo_name == repo_name,
                RepoIndexState.ref == ref,
            )
        ).one_or_none()
        indexed_sha = index_state.indexed_sha if index_state is not None else None
        cited = {step.file_path.lstrip("./") for step in final.get("reading_steps", [])}
        changed = sorted(
            file.path for file in comparison.changed_files if file.path.lstrip("./") in cited
        )
        freshness = TourFreshness(
            indexed_sha=indexed_sha,
            head_sha=comparison.head_sha,
            commits_behind=comparison.commits_behind,
            measurable=comparison.measurable,
            changed_cited_files=changed,
            checked_at=dt.datetime.now(dt.UTC),
        )
    return BriefArtifact(
        issue_title=issue.title,
        issue_number=issue.number,
        repo_name=repo_name,
        summary=draft.summary,
        house_rules=draft.house_rules,
        setup_recipe=BriefSetupRecipe(
            steps=draft.setup_steps,
            target_branch=ref,
            target_branch_source=target_resolution.source,
            target_branch_evidence=target_resolution.evidence,
            target_branch_evidence_path=target_resolution.evidence_path,
            default_branch=target_resolution.default_branch,
            fork_repo=fork_status.fork_repo,
            upstream_repo=fork_status.upstream_repo,
            fork_commits_behind=fork_status.commits_behind,
            fork_status_measurable=fork_status.measurable,
        ),
        reading_steps=final.get("reading_steps", []),
        test_guidance=draft.test_guidance,
        plan_checklist=draft.plan_checklist,
        freshness=freshness,
        preflight_warnings=list(issue.warnings),
        confidence=BriefConfidence(
            level=synthesis.confidence,
            issue_is_vague=synthesis.issue_is_vague,
            questions_for_maintainer=synthesis.questions_for_maintainer,
        ),
        honesty_note=final.get("honesty_note"),
    )

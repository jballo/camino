import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.brief.runner import generate_brief
from app.brief.schemas import BriefDraft, BriefSynthesis
from app.models.tour import TourStep
from app.services.fork_status import ForkStatus
from app.services.issue_thread import IssueThread
from app.services.staleness import HeadComparison
from app.services.target_branch import TargetBranchResolution


def issue() -> IssueThread:
    return IssueThread(
        repo_name="org/repo",
        number=44,
        title="Clarify refresh behavior",
        body="Refresh can race.",
        state="open",
        labels=(),
        assignees=(),
        comments=(),
        html_url="https://github.com/org/repo/issues/44",
        warnings=(),
        branch_instruction=None,
    )


async def test_runner_builds_seven_section_artifact_for_vague_issue():
    synthesis = BriefSynthesis(
        scope_summary="The behavior is unclear.",
        retrieval_queries=["refresh flow"],
        issue_keywords=["refresh"],
        issue_is_vague=True,
        confidence="low",
        questions_for_maintainer=["Which race should be prevented?"],
    )
    draft = BriefDraft(
        summary="Ask the maintainer before changing behavior.",
        house_rules=["Run focused tests."],
        setup_steps=["Create a branch from develop."],
        test_guidance=["Cover concurrent refreshes."],
        plan_checklist=["Confirm expected behavior."],
    )
    step = TourStep(
        title="Read refresh",
        explanation="This function refreshes state.",
        file_path="auth.py",
        start_line=2,
        end_line=3,
        snippet="def refresh():\n    pass",
    )
    final = {
        "synthesis": synthesis,
        "draft": draft,
        "reading_steps": [step],
        "comparison": HeadComparison("head", 0, (), True),
        "honesty_note": None,
    }
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value=final)
    session = MagicMock()
    session.exec.return_value.one_or_none.return_value = SimpleNamespace(indexed_sha="indexed")
    resolution = TargetBranchResolution(
        branch="develop",
        source="issue_thread",
        evidence="Target develop",
        evidence_path=None,
        default_branch="main",
        checked_at=dt.datetime.now(dt.UTC),
    )
    fork = ForkStatus("org/repo", "org/repo", None, False, "develop", 0, True)
    with (
        patch("app.brief.runner.ChatOpenAI"),
        patch("app.brief.runner.build_brief_graph", return_value=graph),
    ):
        artifact = await generate_brief(
            session,
            issue=issue(),
            repo_name="org/repo",
            ref="develop",
            installation_id=7,
            target_resolution=resolution,
            fork_status=fork,
        )
    assert artifact.summary == draft.summary
    assert artifact.house_rules == draft.house_rules
    assert artifact.setup_recipe.target_branch == "develop"
    assert artifact.reading_steps == [step]
    assert artifact.test_guidance == draft.test_guidance
    assert artifact.plan_checklist == draft.plan_checklist
    assert artifact.freshness.indexed_sha == "indexed"
    assert artifact.confidence.issue_is_vague is True
    assert artifact.confidence.questions_for_maintainer

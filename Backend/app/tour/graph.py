"""LangGraph pipeline for guided tour generation.

Plan -> Retrieve -> Draft -> Review, with a bounded repair loop from Review back
to Draft. The session, LLM, and retrieval knobs are bound via closure so nodes
only read state, mirroring ``app.agent.graph`` / ``app.agent.tools``.

Grounding is by construction (snippets are extracted from stored chunk source in
``app.tour.extract``), so the Review node is a safety net: its structural checks
should always pass, while its coverage checks (every planned step produced, no
duplicate citations, enough distinct files) are what actually drive a repair.
"""

import asyncio
import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlmodel import Session

from app.models.tour import TourArtifact, TourStep
from app.services.search import SearchResult, hybrid_search
from app.tour.extract import build_grounded_step
from app.tour.prompts import (
    DRAFT_AVOID,
    DRAFT_HUMAN,
    DRAFT_REPAIR,
    DRAFT_SYSTEM,
    PLAN_HUMAN,
    PLAN_SYSTEM,
)
from app.tour.checks import CheckIssue, CheckKind
from app.tour.review import DEFAULT_MIN_DISTINCT_FILES, review_tour
from app.tour.schemas import DraftedStep, TourPlan
from app.tour.state import TourState

logger = logging.getLogger(__name__)

MIN_PLAN_STEPS = 3
MAX_PLAN_STEPS = 8
DEFAULT_SEARCH_LIMIT = 6
MAX_CANDIDATE_SOURCE_CHARS = 2000
DEFAULT_MAX_ATTEMPTS = 2


def _format_candidates(results: list[SearchResult]) -> str:
    """Render candidates with chunk ids and line-numbered source for the drafter.

    Absolute line numbers are shown so the model can return a precise span; the
    ``chunk_id`` is the stable handle it selects (never a positional index).
    """
    blocks: list[str] = []
    for r in results:
        numbered: list[str] = []
        for offset, line in enumerate(r.source_code.splitlines()):
            numbered.append(f"{r.start_line + offset:>6} | {line}")
        body = "\n".join(numbered)
        if len(body) > MAX_CANDIDATE_SOURCE_CHARS:
            body = body[:MAX_CANDIDATE_SOURCE_CHARS] + "\n     … (truncated)"
        header = (
            f"chunk_id={r.chunk_id} | {r.file_path}:{r.start_line}-{r.end_line} "
            f"({r.symbol_type} {r.symbol_name})"
        )
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks)


def _pick_chunk(results: list[SearchResult], chunk_id: int) -> SearchResult:
    """Resolve the model's chosen ``chunk_id``, defaulting to the top candidate.

    Guards against the model returning an id outside the candidate set — we'd
    rather cite the best-retrieved chunk than fail the step.
    """
    for r in results:
        if r.chunk_id == chunk_id:
            return r
    logger.warning("draft picked unknown chunk_id=%s; defaulting to top candidate", chunk_id)
    return results[0]


def _repair_note(problems: list[str], used: list[str]) -> str:
    """Build the extra prompt block appended to a step on a repair pass."""
    blocks: list[str] = []
    if problems:
        blocks.append(DRAFT_REPAIR.format(problems="\n".join(f"- {p}" for p in problems)))
    if used:
        blocks.append(DRAFT_AVOID.format(used="; ".join(used)))
    return "\n\n".join(blocks)


def build_tour_graph(
    session: Session,
    llm: BaseChatModel,
    *,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    min_steps: int = MIN_PLAN_STEPS,
    max_steps: int = MAX_PLAN_STEPS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_distinct_files: int = DEFAULT_MIN_DISTINCT_FILES,
) -> CompiledStateGraph:
    """Compile the tour generation graph bound to a session and model.

    ``max_attempts`` bounds the Draft->Review repair loop (total Draft passes,
    including the first). ``min_distinct_files`` is the coverage floor for how
    many files a tour should span (clamped to the plan length for short tours).
    """

    async def plan_node(state: TourState) -> dict:
        planner = llm.with_structured_output(TourPlan)
        plan: TourPlan = await planner.ainvoke(
            [
                SystemMessage(
                    content=PLAN_SYSTEM.format(
                        repo_name=state["repo_name"],
                        min_steps=min_steps,
                        max_steps=max_steps,
                    )
                ),
                HumanMessage(content=PLAN_HUMAN.format(topic=state["topic"])),
            ]
        )
        steps = plan.steps[:max_steps]
        logger.info("tour plan | topic=%r steps=%d", state["topic"], len(steps))
        return {"title": plan.title, "plan": steps}

    async def retrieve_node(state: TourState) -> dict:
        plan = state["plan"]

        async def _search(query: str) -> list[SearchResult]:
            return await hybrid_search(
                session,
                query,
                state["repo_name"],
                installation_id=state["installation_id"],
                limit=search_limit,
            )

        results = await asyncio.gather(*[_search(ps.search_query) for ps in plan])
        candidates = {i: r for i, r in enumerate(results)}
        logger.info(
            "tour retrieve | per-step candidates=%s",
            [len(r) for r in results],
        )
        return {"candidates": candidates}

    async def draft_node(state: TourState) -> dict:
        drafter = llm.with_structured_output(DraftedStep)
        plan = state["plan"]
        candidates = state["candidates"]
        drafts: dict[int, TourStep] = dict(state.get("drafts") or {})

        # First pass drafts every plan step; a repair pass touches only the
        # indices Review flagged, preserving the good steps.
        targets = state.get("repair_indices")
        if not targets:
            targets = list(range(len(plan)))
        target_set = set(targets)

        # Issues carry plan indices (Review remaps them), so feedback keys line up.
        feedback: dict[int, list[str]] = {}
        for issue in state.get("issues") or []:
            if issue.step_index is not None:
                feedback.setdefault(issue.step_index, []).append(
                    f"{issue.kind.value}: {issue.message}"
                )

        # Citations from steps we're keeping, so a repair avoids duplicating them.
        used = [
            f"{step.file_path}:{step.start_line}-{step.end_line}"
            for index, step in drafts.items()
            if index not in target_set
        ]

        async def _draft(index: int) -> tuple[int, TourStep | None]:
            cands = candidates.get(index, [])
            if not cands:
                logger.warning("tour draft | no candidates for step %d; skipping", index)
                return index, None

            human = DRAFT_HUMAN.format(
                step_intent=plan[index].step_intent,
                candidates=_format_candidates(cands),
            )
            note = _repair_note(feedback.get(index, []), used)
            if note:
                human = f"{human}\n\n{note}"

            try:
                drafted: DraftedStep = await drafter.ainvoke(
                    [SystemMessage(content=DRAFT_SYSTEM), HumanMessage(content=human)]
                )
            except Exception:
                logger.exception("tour draft | step %d failed; skipping", index)
                return index, None

            chunk = _pick_chunk(cands, drafted.chunk_id)
            return index, build_grounded_step(
                chunk=chunk,
                title=drafted.title,
                explanation=drafted.explanation,
                why=drafted.why,
                req_start=drafted.start_line,
                req_end=drafted.end_line,
            )

        results = await asyncio.gather(*[_draft(i) for i in targets])
        for index, step in results:
            if step is not None:
                drafts[index] = step  # a failed repair keeps the prior draft

        steps = [drafts[i] for i in sorted(drafts)]
        logger.info("tour draft | grounded steps=%d/%d", len(steps), len(plan))
        return {"drafts": drafts, "steps": steps}

    def review_node(state: TourState) -> dict:
        plan = state["plan"]
        drafts: dict[int, TourStep] = state.get("drafts") or {}
        attempts = state.get("attempts", 0) + 1

        ordered_indices = sorted(drafts)
        ordered_steps = [drafts[i] for i in ordered_indices]

        if not ordered_steps:
            logger.warning("tour review | no grounded steps (attempt %d)", attempts)
            return {
                "issues": [
                    CheckIssue(
                        kind=CheckKind.COVERAGE, message="no grounded steps produced"
                    )
                ],
                "attempts": attempts,
                "repair_indices": list(range(len(plan))),
                "steps": [],
            }

        artifact = TourArtifact(
            title=state.get("title") or state["topic"],
            topic=state["topic"],
            repo_name=state["repo_name"],
            steps=ordered_steps,
        )
        chunks = [c for cands in (state.get("candidates") or {}).values() for c in cands]
        result = review_tour(
            artifact,
            chunks,
            planned_count=len(plan),
            min_distinct_files=min_distinct_files,
        )

        # Remap issue.step_index (position in the compacted list) back to plan index
        # so Draft's feedback/repair targets line up with the plan.
        remapped: list[CheckIssue] = []
        for issue in result.issues:
            plan_index = (
                ordered_indices[issue.step_index]
                if issue.step_index is not None and issue.step_index < len(ordered_indices)
                else None
            )
            remapped.append(
                CheckIssue(kind=issue.kind, message=issue.message, step_index=plan_index)
            )

        missing = [i for i in range(len(plan)) if i not in drafts]
        flagged = [iss.step_index for iss in remapped if iss.step_index is not None]
        repair_indices = sorted(set(missing) | set(flagged))

        logger.info(
            "tour review | attempt=%d steps=%d issues=%d repair=%s",
            attempts,
            len(ordered_steps),
            len(remapped),
            repair_indices,
        )
        return {
            "issues": remapped,
            "attempts": attempts,
            "repair_indices": repair_indices,
            "steps": ordered_steps,
        }

    def route_after_review(state: TourState) -> str:
        """Loop back to Draft only while a repair could plausibly help.

        Stops when the tour is clean, retries are exhausted, or the remaining
        issues aren't tied to a step we could redraft (a pure safety-net report).
        """
        if not state.get("issues"):
            return END
        if state.get("attempts", 0) >= max_attempts:
            return END
        if not state.get("repair_indices"):
            return END
        return "draft"

    graph = StateGraph(TourState)
    graph.add_node("plan", plan_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("draft", draft_node)
    graph.add_node("review", review_node)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "retrieve")
    graph.add_edge("retrieve", "draft")
    graph.add_edge("draft", "review")
    graph.add_conditional_edges("review", route_after_review, ["draft", END])

    return graph.compile()

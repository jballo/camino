"""LangGraph pipeline for guided tour generation.

Linear Plan -> Retrieve -> Draft graph (the Review/repair loop lands in M2). The
session, LLM, and retrieval knobs are bound via closure so nodes only read state,
mirroring ``app.agent.graph`` / ``app.agent.tools``.
"""

import asyncio
import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlmodel import Session

from app.models.tour import TourStep
from app.services.search import SearchResult, hybrid_search
from app.tour.extract import build_grounded_step
from app.tour.prompts import DRAFT_HUMAN, DRAFT_SYSTEM, PLAN_HUMAN, PLAN_SYSTEM
from app.tour.schemas import DraftedStep, TourPlan
from app.tour.state import TourState

logger = logging.getLogger(__name__)

MIN_PLAN_STEPS = 3
MAX_PLAN_STEPS = 8
DEFAULT_SEARCH_LIMIT = 6
MAX_CANDIDATE_SOURCE_CHARS = 2000


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


def build_tour_graph(
    session: Session,
    llm: BaseChatModel,
    *,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    min_steps: int = MIN_PLAN_STEPS,
    max_steps: int = MAX_PLAN_STEPS,
) -> CompiledStateGraph:
    """Compile the tour generation graph bound to a session and model."""

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

        async def _draft(index: int) -> TourStep | None:
            cands = candidates.get(index, [])
            if not cands:
                logger.warning("tour draft | no candidates for step %d; skipping", index)
                return None
            try:
                drafted: DraftedStep = await drafter.ainvoke(
                    [
                        SystemMessage(content=DRAFT_SYSTEM),
                        HumanMessage(
                            content=DRAFT_HUMAN.format(
                                step_intent=plan[index].step_intent,
                                candidates=_format_candidates(cands),
                            )
                        ),
                    ]
                )
            except Exception:
                logger.exception("tour draft | step %d failed; skipping", index)
                return None

            chunk = _pick_chunk(cands, drafted.chunk_id)
            return build_grounded_step(
                chunk=chunk,
                title=drafted.title,
                explanation=drafted.explanation,
                why=drafted.why,
                req_start=drafted.start_line,
                req_end=drafted.end_line,
            )

        drafted = await asyncio.gather(*[_draft(i) for i in range(len(plan))])
        steps = [s for s in drafted if s is not None]
        logger.info("tour draft | grounded steps=%d/%d", len(steps), len(plan))
        return {"steps": steps}

    graph = StateGraph(TourState)
    graph.add_node("plan", plan_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("draft", draft_node)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "retrieve")
    graph.add_edge("retrieve", "draft")
    graph.add_edge("draft", END)

    return graph.compile()

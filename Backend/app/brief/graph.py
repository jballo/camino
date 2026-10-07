"""Issue brief synthesis, retrieval, staleness gate, drafting, and review."""

from __future__ import annotations

import asyncio
import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlmodel import Session, select

from app.brief.prompts import (
    DRAFT_HUMAN,
    DRAFT_SYSTEM,
    READING_HUMAN,
    READING_SYSTEM,
    SYNTHESIZE_HUMAN,
    SYNTHESIZE_SYSTEM,
)
from app.brief.schemas import BriefDraft, BriefReadingDraft, BriefSynthesis
from app.brief.state import BriefState
from app.models.code import RepoIndexState
from app.services.search import SearchResult, hybrid_search
from app.services.staleness import compare_to_head, relevant_overlap
from app.tour.extract import build_grounded_step

logger = logging.getLogger(__name__)
DEFAULT_SEARCH_LIMIT = 6
DEFAULT_MAX_ATTEMPTS = 2
MAX_SOURCE_CHARS = 1800


class BriefNeedsRefreshError(RuntimeError):
    """The indexed snapshot cannot safely ground this issue brief."""


def _format_candidates(results: list[SearchResult]) -> str:
    blocks: list[str] = []
    for result in results:
        numbered = "\n".join(
            f"{result.start_line + offset:>6} | {line}"
            for offset, line in enumerate(result.source_code.splitlines())
        )
        if len(numbered) > MAX_SOURCE_CHARS:
            numbered = numbered[:MAX_SOURCE_CHARS] + "\n… (truncated)"
        blocks.append(
            f"chunk_id={result.chunk_id} | {result.file_path}:"
            f"{result.start_line}-{result.end_line}\n{numbered}"
        )
    return "\n\n".join(blocks)


def _pick_chunk(results: list[SearchResult], chunk_id: int) -> SearchResult:
    return next((item for item in results if item.chunk_id == chunk_id), results[0])


def build_brief_graph(
    session: Session,
    llm: BaseChatModel,
    *,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> CompiledStateGraph:
    async def synthesize_node(state: BriefState) -> dict:
        issue = state["issue"]
        comments = "\n\n".join(
            f"{item.get('author') or 'unknown'}: {item.get('body') or ''}"
            for item in issue.comments
        ) or "(none)"
        model = llm.with_structured_output(BriefSynthesis)
        synthesis = await model.ainvoke(
            [
                SystemMessage(content=SYNTHESIZE_SYSTEM),
                HumanMessage(
                    content=SYNTHESIZE_HUMAN.format(
                        repo_name=state["repo_name"],
                        issue_number=issue.number,
                        title=issue.title,
                        labels=", ".join(issue.labels) or "(none)",
                        body=issue.body or "(empty)",
                        comments=comments,
                    )
                ),
            ]
        )
        synthesis.confidence = (
            synthesis.confidence.casefold()
            if synthesis.confidence.casefold() in {"high", "medium", "low"}
            else "low"
        )
        return {"synthesis": synthesis}

    async def retrieve_node(state: BriefState) -> dict:
        queries = state["synthesis"].retrieval_queries

        async def search(query: str) -> list[SearchResult]:
            return await hybrid_search(
                session,
                query,
                state["repo_name"],
                ref=state["ref"],
                limit=search_limit,
            )

        results = await asyncio.gather(*(search(query) for query in queries))
        return {"candidates": dict(enumerate(results))}

    async def staleness_node(state: BriefState) -> dict:
        index_state = session.exec(
            select(RepoIndexState).where(
                RepoIndexState.repo_name == state["repo_name"],
                RepoIndexState.ref == state["ref"],
            )
        ).one_or_none()
        indexed_sha = index_state.indexed_sha if index_state is not None else None
        comparison = await asyncio.to_thread(
            compare_to_head,
            state["repo_name"],
            state["installation_id"],
            indexed_sha,
            state["ref"],
        )
        synthesis = state["synthesis"]
        paths = {
            result.file_path
            for results in state["candidates"].values()
            for result in results
        }
        verdict = relevant_overlap(
            comparison.changed_files,
            paths,
            [*synthesis.retrieval_queries, *synthesis.issue_keywords],
        )
        reason = None
        if not comparison.measurable:
            reason = "Repository divergence could not be measured safely."
        elif verdict.overlaps:
            reason = "Code relevant to this issue changed since the repository was indexed."
        if reason and not state.get("allow_stale", False):
            raise BriefNeedsRefreshError(reason)
        return {
            "comparison": comparison,
            "honesty_note": (
                f"{reason} The brief was drafted after the refresh limit was reached."
                if reason
                else None
            ),
        }

    async def draft_node(state: BriefState) -> dict:
        synthesis = state["synthesis"]
        candidates = state["candidates"]
        issue = state["issue"]
        evidence_blocks: list[str] = []
        for index, query in enumerate(synthesis.retrieval_queries):
            results = candidates.get(index, [])[:3]
            if not results:
                evidence_blocks.append(f"Query: {query}\n(no matches)")
                continue
            facts = []
            for result in results:
                excerpt = result.source_code[:600]
                facts.append(
                    f"{result.file_path}:{result.start_line}-{result.end_line} "
                    f"({result.signature})\n{excerpt}"
                )
            evidence_blocks.append(f"Query: {query}\n" + "\n---\n".join(facts))
        overview = "\n\n".join(evidence_blocks)
        draft_model = llm.with_structured_output(BriefDraft)
        draft = await draft_model.ainvoke(
            [
                SystemMessage(content=DRAFT_SYSTEM),
                HumanMessage(
                    content=DRAFT_HUMAN.format(
                        title=issue.title,
                        scope_summary=synthesis.scope_summary,
                        issue_is_vague=synthesis.issue_is_vague,
                        questions="\n".join(
                            f"- {q}" for q in synthesis.questions_for_maintainer
                        ) or "(none)",
                        target_branch=state["ref"],
                        evidence=overview,
                    )
                ),
            ]
        )

        existing = dict(state.get("reading_drafts") or {})
        targets = state.get("repair_indices") or list(range(len(synthesis.retrieval_queries)))
        reading_model = llm.with_structured_output(BriefReadingDraft)

        async def make_step(index: int) -> tuple[int, object | None]:
            results = candidates.get(index, [])
            if not results:
                return index, None
            note = ""
            if index in set(state.get("repair_indices") or []):
                note = "Repair this step: select valid, non-duplicative evidence."
            try:
                authored = await reading_model.ainvoke(
                    [
                        SystemMessage(content=READING_SYSTEM),
                        HumanMessage(
                            content=READING_HUMAN.format(
                                query=synthesis.retrieval_queries[index],
                                candidates=_format_candidates(results),
                                repair_note=note,
                            )
                        ),
                    ]
                )
                chunk = _pick_chunk(results, authored.chunk_id)
                return index, build_grounded_step(
                    chunk=chunk,
                    title=authored.title,
                    explanation=authored.explanation,
                    why=authored.why,
                    req_start=authored.start_line,
                    req_end=authored.end_line,
                )
            except Exception:
                logger.exception("brief reading step failed | index=%d", index)
                return index, None

        made = await asyncio.gather(*(make_step(index) for index in targets))
        for index, step in made:
            if step is not None:
                existing[index] = step
        return {
            "draft": draft,
            "reading_drafts": existing,
            "reading_steps": [existing[index] for index in sorted(existing)],
        }

    def review_node(state: BriefState) -> dict:
        drafts = state.get("reading_drafts") or {}
        query_count = len(state["synthesis"].retrieval_queries)
        issues: list[str] = []
        repair = [index for index in range(query_count) if index not in drafts]
        seen: dict[tuple[str, int, int], int] = {}
        for index, step in drafts.items():
            citation = (step.file_path, step.start_line, step.end_line)
            if citation in seen:
                issues.append(f"reading steps {seen[citation] + 1} and {index + 1} duplicate a citation")
                repair.append(index)
            else:
                seen[citation] = index
        if repair:
            issues.append("one or more retrieval goals lack distinct grounded evidence")
        return {
            "review_issues": issues,
            "repair_indices": sorted(set(repair)),
            "attempts": state.get("attempts", 0) + 1,
        }

    def after_review(state: BriefState) -> str:
        if not state.get("repair_indices") or state.get("attempts", 0) >= max_attempts:
            return END
        return "draft"

    graph = StateGraph(BriefState)
    graph.add_node("synthesize", synthesize_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("staleness_gate", staleness_node)
    graph.add_node("draft", draft_node)
    graph.add_node("review", review_node)
    graph.add_edge(START, "synthesize")
    graph.add_edge("synthesize", "retrieve")
    graph.add_edge("retrieve", "staleness_gate")
    graph.add_edge("staleness_gate", "draft")
    graph.add_edge("draft", "review")
    graph.add_conditional_edges("review", after_review, ["draft", END])
    return graph.compile()

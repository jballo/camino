import logging

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.services.search import SearchResult, hybrid_search

logger = logging.getLogger(__name__)

MAX_SOURCE_CHARS = 1500


class HybridSearchInput(BaseModel):
    query: str = Field(
        description=(
            "A focused, natural-language description of the code, behaviour, or "
            "concept to look for (e.g. 'where JWT sessions are validated' or "
            "'function that fuses ranked search results'). Prefer specific intent "
            "over copying the user's whole question."
        )
    )


def _format_results(results: list[SearchResult]) -> str:
    if not results:
        return "No relevant code was found for that query. Try rephrasing or broadening it."

    blocks: list[str] = []
    for i, r in enumerate(results, 1):
        body = r.source_code.strip()
        if len(body) > MAX_SOURCE_CHARS:
            body = body[:MAX_SOURCE_CHARS] + "\n# ... (truncated)"
        header = (
            f"[{i}] {r.file_path}:{r.start_line}-{r.end_line} "
            f"({r.symbol_type} {r.symbol_name})"
        )
        blocks.append(f"{header}\n```{r.language}\n{body}\n```")
    return "\n\n".join(blocks)


def build_hybrid_search_tool(
    session: Session,
    repo_name: str,
    installation_id: int,
    *,
    sink: list[SearchResult],
    limit: int = 8,
) -> StructuredTool:
    """Create a request-scoped ``hybrid_search`` tool.

    The DB session, target repository, and installation are bound via closure so
    the model only has to supply a query. Every retrieved chunk is appended to
    ``sink`` so the caller can surface citations alongside the final answer.
    """

    async def _run(query: str) -> str:
        logger.info(
            "agent hybrid_search | repo=%r installation=%s query=%r",
            repo_name, installation_id, query,
        )
        results = await hybrid_search(
            session,
            query,
            repo_name,
            installation_id=installation_id,
            limit=limit,
        )
        sink.extend(results)
        return _format_results(results)

    return StructuredTool.from_function(
        coroutine=_run,
        name="hybrid_search",
        description=(
            f"Search the indexed codebase of '{repo_name}' for relevant code "
            "using hybrid semantic + keyword retrieval. Returns the most relevant "
            "code chunks with their file path and line numbers. Call this whenever "
            "you need concrete code to ground an answer; you may call it multiple "
            "times with different queries to gather more context."
        ),
        args_schema=HybridSearchInput,
    )

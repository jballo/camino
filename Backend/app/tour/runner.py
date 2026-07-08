"""Entry point for guided tour generation."""

import logging

from langchain_openai import ChatOpenAI
from sqlmodel import Session

from app.config import settings
from app.models.tour import TourArtifact
from app.tour.graph import DEFAULT_SEARCH_LIMIT, build_tour_graph

logger = logging.getLogger(__name__)


class TourGenerationError(RuntimeError):
    """Raised when the pipeline cannot produce a usable tour."""


async def generate_tour(
    session: Session,
    *,
    topic: str,
    repo_name: str,
    installation_id: int,
    model: str | None = None,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
) -> TourArtifact:
    """Run Plan -> Retrieve -> Draft and return a grounded ``TourArtifact``.

    Every step's citation is extracted deterministically from a retrieved chunk
    (see ``app.tour.extract``), so the artifact is grounded by construction. Raises
    ``TourGenerationError`` if no step could be grounded (e.g. the repo isn't
    indexed or retrieval returned nothing).
    """
    llm = ChatOpenAI(
        model=model or settings.agent_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )

    graph = build_tour_graph(session, llm, search_limit=search_limit)

    final = await graph.ainvoke(
        {
            "topic": topic,
            "repo_name": repo_name,
            "installation_id": installation_id,
        }
    )

    steps = final.get("steps") or []
    if not steps:
        raise TourGenerationError(
            f"no grounded steps produced for topic={topic!r} repo={repo_name!r} "
            "(is the repository indexed?)"
        )

    return TourArtifact(
        title=final.get("title") or topic,
        topic=topic,
        repo_name=repo_name,
        steps=steps,
    )

"""Entry point for guided tour generation."""

import asyncio
import logging
import threading
from contextlib import suppress

from langchain_openai import ChatOpenAI
from sqlmodel import Session

from app.config import settings
from app.models.tour import TourArtifact
from app.tour.graph import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_SEARCH_LIMIT,
    build_tour_graph,
)

logger = logging.getLogger(__name__)


class TourGenerationError(RuntimeError):
    """Raised when the pipeline cannot produce a usable tour."""


class TourGenerationCancelledError(TourGenerationError):
    """Raised when a caller asks an in-flight tour pipeline to stop."""


CANCELLATION_POLL_INTERVAL = 0.1


async def generate_tour(
    session: Session,
    *,
    topic: str,
    repo_name: str,
    installation_id: int,
    model: str | None = None,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    cancel_event: threading.Event | None = None,
) -> TourArtifact:
    """Run Plan -> Retrieve -> Draft -> Review and return a grounded ``TourArtifact``.

    Every step's citation is extracted deterministically from a retrieved chunk
    (see ``app.tour.extract``), so the artifact is grounded by construction; the
    Review node runs up to ``max_attempts`` Draft passes to repair coverage gaps.
    Raises ``TourGenerationError`` if no step could be grounded (e.g. the repo
    isn't indexed or retrieval returned nothing).
    """
    llm = ChatOpenAI(
        model=model or settings.agent_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )

    graph = build_tour_graph(
        session, llm, search_limit=search_limit, max_attempts=max_attempts
    )

    invocation = asyncio.create_task(
        graph.ainvoke(
            {
                "topic": topic,
                "repo_name": repo_name,
                "installation_id": installation_id,
            }
        )
    )
    try:
        while not invocation.done():
            if cancel_event is not None and cancel_event.is_set():
                invocation.cancel()
                try:
                    await invocation
                except asyncio.CancelledError:
                    pass
                raise TourGenerationCancelledError("Tour generation was cancelled")
            await asyncio.wait(
                {invocation},
                timeout=CANCELLATION_POLL_INTERVAL,
            )
        final = await invocation
    finally:
        if not invocation.done():
            invocation.cancel()
            with suppress(asyncio.CancelledError):
                await invocation

    steps = final.get("steps") or []
    if not steps:
        raise TourGenerationError(
            f"no grounded steps produced for topic={topic!r} repo={repo_name!r} "
            "(is the repository indexed?)"
        )

    # Grounding is by construction, so any residual issues are coverage warnings
    # the repair loop couldn't clear within the retry budget — surface, don't fail.
    issues = final.get("issues") or []
    if issues:
        logger.warning(
            "tour generated with %d unresolved review issue(s) for topic=%r: %s",
            len(issues),
            topic,
            "; ".join(f"{i.kind}:{i.message}" for i in issues),
        )

    return TourArtifact(
        title=final.get("title") or topic,
        topic=topic,
        repo_name=repo_name,
        steps=steps,
    )

import logging
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from sqlmodel import Session

from app.agent.graph import SYSTEM_PROMPT, build_agent
from app.agent.tools import build_hybrid_search_tool
from app.config import settings
from app.services.search import SearchResult

logger = logging.getLogger(__name__)

DEFAULT_RECURSION_LIMIT = 12


@dataclass
class AgentAnswer:
    answer: str
    sources: list[SearchResult] = field(default_factory=list)


def _dedupe_sources(sources: list[SearchResult]) -> list[SearchResult]:
    seen: set[int] = set()
    unique: list[SearchResult] = []
    for s in sources:
        if s.chunk_id in seen:
            continue
        seen.add(s.chunk_id)
        unique.append(s)
    return unique


async def answer_question(
    session: Session,
    *,
    question: str,
    repo_name: str,
    installation_id: int,
    model: str | None = None,
    search_limit: int = 8,
) -> AgentAnswer:
    """Run the LangGraph agent for a single question and return the answer.

    Builds a request-scoped graph whose ``hybrid_search`` tool is bound to this
    session/repo/installation, runs it to completion, and returns the final
    assistant message together with the de-duplicated code chunks the agent
    retrieved as citations.
    """
    sources: list[SearchResult] = []
    search_tool = build_hybrid_search_tool(
        session,
        repo_name,
        installation_id,
        sink=sources,
        limit=search_limit,
    )

    llm = ChatOpenAI(
        model=model or settings.agent_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )

    agent = build_agent(llm, [search_tool])

    initial_messages = [
        SystemMessage(content=SYSTEM_PROMPT.format(repo_name=repo_name)),
        HumanMessage(content=question),
    ]

    result = await agent.ainvoke(
        {"messages": initial_messages},
        config={"recursion_limit": DEFAULT_RECURSION_LIMIT},
    )

    final_message = result["messages"][-1]
    answer = final_message.content if isinstance(final_message.content, str) else str(final_message.content)

    return AgentAnswer(answer=answer, sources=_dedupe_sources(sources))

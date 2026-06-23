from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """Shared state threaded through the agent graph.

    ``messages`` is reduced with :func:`add_messages` so each node can append
    new turns (LLM responses, tool results) without clobbering history.
    """

    messages: Annotated[list[AnyMessage], add_messages]

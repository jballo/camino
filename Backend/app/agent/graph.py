from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent.state import AgentState

SYSTEM_PROMPT = """You are Camino, a senior engineer who answers questions about a \
specific codebase: '{repo_name}'.

You have access to a `hybrid_search` tool that retrieves the most relevant code \
chunks from that repository. Use it to ground every answer in the actual code:

- Always call `hybrid_search` before answering questions about how the code works, \
where something lives, or why it behaves a certain way. Do not rely on assumptions.
- Issue focused queries. If the first results are insufficient, search again with a \
refined query rather than guessing.
- Base your answer strictly on retrieved code. If the code needed to answer is not \
found after searching, say so plainly instead of inventing details.
- Cite the relevant files and symbols (e.g. `app/services/search.py:hybrid_search`) \
in your explanation.
- Be concise and concrete. Prefer short explanations with small, relevant code \
references over long prose.
"""


def build_agent(
    llm: BaseChatModel,
    tools: list[BaseTool],
) -> CompiledStateGraph:
    """Compile a ReAct-style agent graph.

    The graph alternates between an LLM node (which may emit tool calls) and a
    tool node, looping until the model produces a final answer with no tool
    calls. ``tools_condition`` routes to the tool node whenever the latest LLM
    message contains tool calls, otherwise to ``END``.
    """
    llm_with_tools = llm.bind_tools(tools)

    async def agent_node(state: AgentState) -> dict:
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(tools))

    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        tools_condition,
        {"tools": "tools", END: END},
    )
    graph.add_edge("tools", "agent")

    return graph.compile()

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    dialog_state: dict
    actions: list[dict]
    tool_rounds: int
    tool_results: list[dict]
    citations: list[dict]
    answer: str
    waiting: bool

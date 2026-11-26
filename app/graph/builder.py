"""装配岗位图：解析、路由、核查、校验、审批、执行、交付。"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.graph.nodes import GraphNodes
from app.graph.state import GraphState
from app.tools.gateway import ToolGateway


def build_graph(checkpointer: BaseCheckpointSaver, gateway: ToolGateway, settings: Settings):
    nodes = GraphNodes(gateway, settings)
    graph = StateGraph(GraphState)
    graph.add_node("parse", nodes.parse)
    graph.add_node("clarify", nodes.clarify)
    graph.add_node("dispatch", nodes.dispatch)
    graph.add_node("ads_agent", nodes.ads)
    graph.add_node("fulfillment_agent", nodes.fulfillment)
    graph.add_node("aftersales_agent", nodes.aftersales)
    graph.add_node("validate", nodes.validate)
    graph.add_node("approval_gate", nodes.approval_gate)
    graph.add_node("execute", nodes.execute)
    graph.add_node("deliver", nodes.deliver)
    graph.add_node("handoff", nodes.handoff)

    graph.add_edge(START, "parse")
    graph.add_conditional_edges(
        "parse",
        nodes.route_after_parse,
        {"clarify": "clarify", "handoff": "handoff", "dispatch": "dispatch"},
    )
    graph.add_conditional_edges(
        "dispatch",
        nodes.pick_role,
        {
            "ads": "ads_agent",
            "fulfillment": "fulfillment_agent",
            "aftersales": "aftersales_agent",
            "validate": "validate",
            "handoff": "handoff",
        },
    )
    graph.add_edge("ads_agent", "dispatch")
    graph.add_edge("fulfillment_agent", "dispatch")
    graph.add_edge("aftersales_agent", "dispatch")
    graph.add_conditional_edges(
        "validate",
        nodes.route_after_validate,
        {"approval": "approval_gate", "deliver": "deliver", "handoff": "handoff"},
    )
    graph.add_conditional_edges(
        "approval_gate",
        nodes.route_after_approval,
        {"execute": "execute", "handoff": "handoff"},
    )
    graph.add_conditional_edges(
        "execute",
        nodes.route_after_execute,
        {"deliver": "deliver", "handoff": "handoff"},
    )
    graph.add_edge("clarify", END)
    graph.add_edge("deliver", END)
    graph.add_edge("handoff", END)
    return graph.compile(checkpointer=checkpointer)

"""LangGraph 状态。列表字段用追加归约，避免后一个岗位覆盖前一个岗位的证据。"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class GraphState(TypedDict, total=False):
    task_id: str
    user_id: str
    username: str
    allowed_shops: list[str]
    allowed_roles: list[str]
    raw_input: str
    shop_id: str
    roles: list[str]
    role_cursor: int
    objects: dict
    actions: list[dict]
    missing_fields: list[str]
    clarification: str
    evidence: Annotated[list[dict], operator.add]
    tool_calls: Annotated[list[dict], operator.add]
    pending_writes: Annotated[list[dict], operator.add]
    blockers: Annotated[list[str], operator.add]
    approval_decision: str
    approval_note: str
    write_results: list[dict]
    delivery: dict
    status: str
    handoff_reason: str
    trace: Annotated[list[dict], operator.add]
    step_count: int
    parser: str

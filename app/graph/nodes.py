"""图节点。写操作只在审批恢复后的 execute 节点发生，审批节点本身不改业务数据。"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from langgraph.types import interrupt

from app.config import Settings
from app.graph.state import GraphState
from app.parser import clarification_text, parse_task
from app.tools.gateway import ToolGateway
from app.tracing import emit_span

ROLE_LABELS = {"ads": "广告", "fulfillment": "履约", "aftersales": "售后"}


class GraphNodes:
    def __init__(self, gateway: ToolGateway, settings: Settings) -> None:
        self.gateway = gateway
        self.settings = settings

    async def parse(self, state: GraphState) -> dict:
        started = time.perf_counter()
        parsed = await parse_task(state["raw_input"], self.settings)
        allowed_shops = set(state.get("allowed_shops") or [])
        allowed_roles = set(state.get("allowed_roles") or [])
        status = "running"
        reason = ""
        if parsed.shop_id and parsed.shop_id not in allowed_shops:
            status = "handoff"
            reason = f"无权访问店铺 {parsed.shop_id}"
        elif parsed.missing_fields:
            status = "clarifying"
        else:
            denied = [ROLE_LABELS[role] for role in parsed.roles if role not in allowed_roles]
            if denied or not parsed.roles:
                status = "handoff"
                reason = "无权处理岗位：" + "、".join(denied or ["未识别"])
        update = {
            "shop_id": parsed.shop_id or "",
            "roles": [] if status == "handoff" else parsed.roles,
            "role_cursor": 0,
            "objects": parsed.objects,
            "actions": [] if status != "running" else parsed.actions,
            "missing_fields": parsed.missing_fields,
            "clarification": clarification_text(parsed.missing_fields) if status == "clarifying" else "",
            "status": status,
            "handoff_reason": reason,
            "parser": parsed.parser,
            "trace": [_span("parse", "任务解析", "ok" if status == "running" else status, started)],
        }
        self._observe("parse", update["trace"][0])
        return update

    async def clarify(self, state: GraphState) -> dict:
        started = time.perf_counter()
        text = state.get("clarification") or "请补充任务信息"
        return {
            "status": "clarifying",
            "delivery": _delivery(state, summary=text, suggestions=["补充字段后重新提交"], handoff=False),
            "trace": [_span("clarify", "补充信息", "ok", started)],
        }

    async def dispatch(self, state: GraphState) -> dict:
        started = time.perf_counter()
        step = int(state.get("step_count") or 0) + 1
        if step > 12:
            return {
                "step_count": step,
                "status": "handoff",
                "handoff_reason": "超过最大编排步数",
                "trace": [_span("dispatch", "岗位路由", "stopped", started)],
            }
        return {
            "step_count": step,
            "trace": [_span("dispatch", "岗位路由", "ok", started, cursor=state.get("role_cursor", 0))],
        }

    def route_after_parse(self, state: GraphState) -> str:
        status = state.get("status")
        if status == "handoff":
            return "handoff"
        if status == "clarifying":
            return "clarify"
        return "dispatch"

    def pick_role(self, state: GraphState) -> str:
        if state.get("status") == "handoff":
            return "handoff"
        roles = state.get("roles") or []
        cursor = int(state.get("role_cursor") or 0)
        if cursor >= len(roles):
            return "validate"
        return roles[cursor]

    async def ads(self, state: GraphState) -> dict:
        return await self._run_role(state, "ads", "ads_agent", "广告核查")

    async def fulfillment(self, state: GraphState) -> dict:
        return await self._run_role(state, "fulfillment", "fulfillment_agent", "履约核查")

    async def aftersales(self, state: GraphState) -> dict:
        return await self._run_role(state, "aftersales", "aftersales_agent", "售后核查")

    async def validate(self, state: GraphState) -> dict:
        started = time.perf_counter()
        blockers = list(state.get("blockers") or [])
        evidence = list(state.get("evidence") or [])
        writes = list(state.get("pending_writes") or [])
        if blockers:
            update = {
                "status": "handoff",
                "handoff_reason": "；".join(blockers),
                "trace": [_span("validate", "结果校验", "handoff", started)],
            }
        elif not evidence and not writes:
            update = {
                "status": "handoff",
                "handoff_reason": "证据不足，转人工",
                "trace": [_span("validate", "结果校验", "handoff", started)],
            }
        else:
            update = {"status": "running", "trace": [_span("validate", "结果校验", "ok", started)]}
        self._observe("validate", update["trace"][0])
        return update

    def route_after_validate(self, state: GraphState) -> str:
        if state.get("status") == "handoff":
            return "handoff"
        if state.get("pending_writes"):
            return "approval"
        return "deliver"

    async def approval_gate(self, state: GraphState) -> dict:
        """暂停点前后都不要写业务表。恢复时 interrupt 直接返回审批结论。"""
        started = time.perf_counter()
        payload = _approval_payload(state)
        decision = interrupt(payload)
        if not isinstance(decision, dict):
            decision = {"decision": "rejected", "note": "无效审批"}
        approved = decision.get("decision") == "approved"
        note = str(decision.get("note") or "")
        if not approved:
            update = {
                "approval_decision": "rejected",
                "approval_note": note,
                "status": "handoff",
                "handoff_reason": "审批拒绝" + (f"：{note}" if note else ""),
                "trace": [_span("approval_gate", "人工审批", "rejected", started)],
            }
        else:
            update = {
                "approval_decision": "approved",
                "approval_note": note,
                "status": "executing",
                "trace": [_span("approval_gate", "人工审批", "approved", started)],
            }
        self._observe("approval_gate", update["trace"][0])
        return update

    def route_after_approval(self, state: GraphState) -> str:
        if state.get("approval_decision") == "approved":
            return "execute"
        return "handoff"

    async def execute(self, state: GraphState) -> dict:
        started = time.perf_counter()
        calls = []
        records = []
        errors = []
        for pending in state.get("pending_writes") or []:
            result = await self.gateway.call(pending["tool"], pending["params"])
            calls.append(result)
            records.append(
                {
                    "operation_id": pending["params"].get("operation_id"),
                    "tool": pending["tool"],
                    "target": pending["target"],
                    "status": "executed" if result.get("ok") else "rejected",
                    "before": pending.get("before") or {},
                    "after": (result.get("data") or {}).get("after"),
                    "error_code": result.get("error_code"),
                    "replayed": bool((result.get("data") or {}).get("replayed")),
                }
            )
            if not result.get("ok"):
                errors.append(result.get("error_message") or "写入失败")
        if errors:
            update = {
                "status": "handoff",
                "handoff_reason": "参数复核未通过：" + "；".join(errors),
                "tool_calls": calls,
                "write_results": records,
                "trace": [_span("execute", "写操作执行", "rejected", started)],
            }
        else:
            update = {
                "status": "executing",
                "tool_calls": calls,
                "write_results": records,
                "trace": [_span("execute", "写操作执行", "ok", started)],
            }
        self._observe("execute", update["trace"][0])
        return update

    def route_after_execute(self, state: GraphState) -> str:
        if state.get("status") == "handoff":
            return "handoff"
        return "deliver"

    async def deliver(self, state: GraphState) -> dict:
        started = time.perf_counter()
        summary, suggestions = _summary(state)
        delivery = _delivery(state, summary=summary, suggestions=suggestions, handoff=False)
        update = {
            "status": "delivered",
            "delivery": delivery,
            "trace": [_span("deliver", "结果交付", "ok", started)],
        }
        self._observe("deliver", update["trace"][0])
        return update

    async def handoff(self, state: GraphState) -> dict:
        started = time.perf_counter()
        reason = state.get("handoff_reason") or "转人工跟进"
        summary = f"任务已转人工。原因：{reason}"
        if state.get("evidence"):
            summary += "\n已收集的证据仍保留在交付记录中，供人工继续核查。"
        delivery = _delivery(
            state,
            summary=summary,
            suggestions=["人工确认权限、对象状态或补充材料后再恢复处理"],
            handoff=True,
            reason=reason,
        )
        return {
            "status": "handoff",
            "handoff_reason": reason,
            "delivery": delivery,
            "trace": [_span("handoff", "转人工", "ok", started)],
        }

    async def _run_role(self, state: GraphState, role: str, node: str, label: str) -> dict:
        started = time.perf_counter()
        calls: list[dict] = []
        evidence: list[dict] = []
        writes: list[dict] = []
        blockers: list[str] = []
        actions = [item for item in state.get("actions") or [] if item.get("role") == role]
        for action in actions:
            if action.get("kind") != "read":
                continue
            result = await self.gateway.call(action["tool"], action["params"])
            calls.append(result)
            if result.get("ok"):
                evidence.append(
                    {
                        "source": action["tool"],
                        "summary": summarize_call(result),
                        "payload": result.get("data") or {},
                    }
                )
            else:
                blockers.append(result.get("error_message") or f"{action['tool']} 失败")
        if role == "aftersales":
            await self._enrich_aftersales(calls, evidence, blockers)
        for action in actions:
            if action.get("kind") != "write":
                continue
            pending = _pending_write(state, action, calls)
            if pending is None:
                blockers.append(f"无法为 {action['tool']} 取得执行前快照")
            else:
                writes.append(pending)
        update = {
            "tool_calls": calls,
            "evidence": evidence,
            "pending_writes": writes,
            "blockers": blockers,
            "role_cursor": int(state.get("role_cursor") or 0) + 1,
            "trace": [_span(node, label, "ok" if not blockers else "partial", started)],
        }
        self._observe(node, update["trace"][0])
        return update

    async def _enrich_aftersales(self, calls: list[dict], evidence: list[dict], blockers: list[str]) -> None:
        seen = {
            (call.get("tool"), (call.get("data") or {}).get("order_id"))
            for call in calls
            if call.get("tool") == "query_order_logistics"
        }
        for call in list(calls):
            if call.get("tool") != "query_tickets" or not call.get("ok"):
                continue
            shop_id = (call.get("data") or {}).get("shop_id")
            for ticket in (call.get("data") or {}).get("tickets") or []:
                order_id = ticket.get("order_id")
                if not order_id or not shop_id or ("query_order_logistics", order_id) in seen:
                    continue
                for tool, params in (
                    ("query_orders", {"shop_id": shop_id, "order_id": order_id}),
                    ("query_order_logistics", {"shop_id": shop_id, "order_id": order_id}),
                ):
                    result = await self.gateway.call(tool, params)
                    calls.append(result)
                    if result.get("ok"):
                        evidence.append(
                            {
                                "source": tool,
                                "summary": summarize_call(result),
                                "payload": result.get("data") or {},
                            }
                        )
                    else:
                        blockers.append(result.get("error_message") or f"{tool} 失败")
                seen.add(("query_order_logistics", order_id))

    def _observe(self, name: str, span: dict) -> None:
        emit_span(self.settings, name, span)


def _pending_write(state: GraphState, action: dict, calls: list[dict]) -> dict | None:
    tool = action["tool"]
    params = dict(action["params"])
    params["task_id"] = state["task_id"]
    if tool == "adjust_ad_budget":
        campaign = _campaign(calls, params["campaign_id"])
        if campaign is None:
            return None
        before_budget = float(campaign["daily_budget"])
        params["expected_daily_budget"] = before_budget
        params["operation_id"] = _operation_id(
            state["task_id"],
            tool,
            {
                "shop_id": params["shop_id"],
                "campaign_id": params["campaign_id"],
                "new_daily_budget": params["new_daily_budget"],
                "reason": params["reason"],
            },
        )
        return {
            "tool": tool,
            "role": "ads",
            "target": params["campaign_id"],
            "before": {"daily_budget": before_budget},
            "risk": _budget_risk(before_budget, float(params["new_daily_budget"])),
            "params": params,
        }
    if tool == "update_ticket":
        ticket = _ticket(calls, params["ticket_id"])
        if ticket is None:
            return None
        params["expected_status"] = ticket["status"]
        params["operation_id"] = _operation_id(
            state["task_id"],
            tool,
            {
                "shop_id": params["shop_id"],
                "ticket_id": params["ticket_id"],
                "new_status": params["new_status"],
                "note": params.get("note") or "",
            },
        )
        return {
            "tool": tool,
            "role": "aftersales",
            "target": params["ticket_id"],
            "before": {"status": ticket["status"]},
            "risk": "工单状态写入，需人工确认流转方向和关联订单",
            "params": params,
        }
    if tool == "simulate_refund":
        order = _order(calls, params["order_id"])
        if order is None:
            return None
        params["expected_status"] = order["status"]
        params["operation_id"] = _operation_id(
            state["task_id"],
            tool,
            {
                "shop_id": params["shop_id"],
                "order_id": params["order_id"],
                "amount": params["amount"],
                "reason": params["reason"],
            },
        )
        return {
            "tool": tool,
            "role": "aftersales",
            "target": params["order_id"],
            "before": {"status": order["status"], "refunded_amount": order.get("refunded_amount", 0)},
            "risk": "退款为模拟写入，需人工确认订单、金额和签收状态，不会连接真实支付",
            "params": params,
        }
    return None


def _campaign(calls: list[dict], campaign_id: str) -> dict | None:
    for call in calls:
        data = call.get("data") or {}
        if call.get("ok") and call.get("tool") == "query_ad_campaign" and data.get("campaign_id") == campaign_id:
            return data
    return None


def _ticket(calls: list[dict], ticket_id: str) -> dict | None:
    for call in calls:
        data = call.get("data") or {}
        if not call.get("ok") or call.get("tool") != "query_tickets":
            continue
        for ticket in data.get("tickets") or []:
            if ticket.get("ticket_id") == ticket_id:
                return ticket
    return None


def _order(calls: list[dict], order_id: str) -> dict | None:
    for call in calls:
        data = call.get("data") or {}
        if not call.get("ok") or call.get("tool") != "query_orders":
            continue
        for order in data.get("orders") or []:
            if order.get("order_id") == order_id:
                return order
    return None


def _operation_id(task_id: str, tool: str, identity: dict) -> str:
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{task_id}|{tool}|{raw}".encode("utf-8")).hexdigest()[:32]


def _budget_risk(before: float, new: float) -> str:
    ratio = (new - before) / before if before else 1
    text = "资金写操作，需人工确认日预算、对象和依据"
    if ratio > 0.5:
        text += f"；日预算上调 {ratio:.0%}，超过 50% 政策阈值"
    return text


def _approval_payload(state: GraphState) -> dict:
    writes = []
    for item in state.get("pending_writes") or []:
        params = dict(item.get("params") or {})
        params.pop("task_id", None)
        writes.append(
            {
                "operation_id": params.get("operation_id"),
                "tool": item.get("tool"),
                "target": item.get("target"),
                "params": params,
                "before": item.get("before") or {},
                "risk": item.get("risk") or "",
            }
        )
    return {
        "task_id": state.get("task_id"),
        "shop_id": state.get("shop_id"),
        "roles": state.get("roles") or [],
        "writes": writes,
        "evidence": [
            {"source": item.get("source"), "summary": item.get("summary")}
            for item in state.get("evidence") or []
        ],
    }


def summarize_call(call: dict) -> str:
    data = call.get("data") or {}
    tool = call.get("tool")
    if tool == "query_ad_spend":
        return f"近 {data.get('days')} 天广告消耗合计 {float(data.get('total_spend') or 0):.2f} USD，计划 {len(data.get('campaigns') or [])} 个"
    if tool == "query_ad_campaign":
        return f"计划 {data.get('campaign_id')} {data.get('name')} 日预算 {data.get('daily_budget')} USD，近 7 天消耗 {data.get('spend_7d')}"
    if tool == "query_orders":
        orders = data.get("orders") or []
        brief = "、".join(f"{item.get('order_id')}:{item.get('status')}" for item in orders[:4]) or "无订单"
        return f"订单 {len(orders)} 笔：{brief}"
    if tool == "query_order_logistics":
        return f"订单 {data.get('order_id')} 物流：{data.get('logistics')}"
    if tool == "query_inventory":
        items = data.get("items") or []
        brief = "、".join(f"{item.get('sku')} 可用 {item.get('available')}" for item in items[:4]) or "无库存"
        return f"库存 {len(items)} 个 SKU：{brief}"
    if tool == "query_tickets":
        tickets = data.get("tickets") or []
        brief = "、".join(f"{item.get('ticket_id')}:{item.get('status')}" for item in tickets[:4]) or "无工单"
        return f"工单 {len(tickets)} 张：{brief}"
    if tool == "search_policy":
        clauses = data.get("clauses") or []
        if not clauses:
            return "未命中政策条款"
        first = clauses[0]
        return f"政策 {first.get('policy_id')} {first.get('title')}：{first.get('excerpt')}"
    return tool or "工具结果"


def _summary(state: GraphState) -> tuple[str, list[str]]:
    roles = "、".join(ROLE_LABELS.get(role, role) for role in state.get("roles") or [])
    shop = state.get("shop_id") or "未指定店铺"
    lines = [f"已完成 {shop} 的{roles or '运营'}核查。"]
    lines.extend(f"- {item.get('summary')}" for item in state.get("evidence") or [])
    suggestions = []
    blob = " ".join(item.get("summary") or "" for item in state.get("evidence") or [])
    if "shipped" in blob or "已离港" in blob:
        suggestions.append("有未签收订单，建议继续跟踪物流节点")
    if ":open" in blob or "open" in blob:
        suggestions.append("存在未关闭工单，建议指定跟进人")
    if "可用 5" in blob or "可用 1" in blob or "可用 0" in blob:
        suggestions.append("有 SKU 可用库存偏低，活动放量前先核对库存")
    writes = state.get("write_results") or []
    if writes:
        done = "、".join(item.get("operation_id", "")[:8] for item in writes)
        lines.append(f"写操作已按审批执行，操作标识 {done}。")
        suggestions.append("写操作已留痕，可按操作标识复核业务状态")
    elif not suggestions:
        suggestions.append("本次未执行写操作，建议抽查证据后再决定是否申请变更")
    return "\n".join(lines), suggestions


def _delivery(
    state: GraphState,
    *,
    summary: str,
    suggestions: list[str],
    handoff: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": state.get("task_id"),
        "shop_id": state.get("shop_id") or None,
        "roles": state.get("roles") or [],
        "summary": summary,
        "suggestions": suggestions,
        "evidence": state.get("evidence") or [],
        "writes": state.get("write_results") or [],
        "handoff": handoff,
        "handoff_reason": reason,
        "parser": state.get("parser") or "rule",
    }


def _span(node: str, label: str, status: str, started: float, **extra: Any) -> dict:
    item = {
        "node": node,
        "label": label,
        "status": status,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
    item.update(extra)
    return item

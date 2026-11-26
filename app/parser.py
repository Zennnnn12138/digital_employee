"""把运营自然语言收成岗位、对象和工具计划。

规则解析是回归基线。配置了模型密钥时会先请模型给出同结构 JSON，校验失败则回退规则。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.config import Settings, resolve_llm
from app.llm import LLMError, complete_json

ROLE_ORDER = ("ads", "fulfillment", "aftersales")
FIELD_LABELS = {
    "shop_id": "店铺编号，例如 SHOP-1001",
    "campaign_id": "广告计划编号，例如 C-501",
    "order_id": "订单编号，例如 O-9003",
    "ticket_id": "工单编号，例如 T-3001",
    "amount": "退款金额",
    "new_status": "工单目标状态（处理中或已解决）",
    "role": "岗位（广告、履约或售后）",
}


class ParsedTask(BaseModel):
    shop_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    objects: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    parser: str = "rule"


def clarification_text(missing: list[str]) -> str:
    lines = ["还不能执行，请补充这些字段："]
    for name in missing:
        lines.append(f"- {FIELD_LABELS.get(name, name)}")
    return "\n".join(lines)


def rule_parse(text: str) -> ParsedTask:
    shop_ids = re.findall(r"SHOP-\d+", text)
    campaign_ids = re.findall(r"C-\d+", text)
    order_ids = re.findall(r"O-\d+", text)
    ticket_ids = re.findall(r"T-\d+", text)
    skus = re.findall(r"SKU-[A-Z0-9-]+", text)
    days_match = re.search(r"最近\s*(\d+)\s*天", text)
    days = int(days_match.group(1)) if days_match else 7
    if days < 1 or days > 90:
        days = 7
    reason_match = re.search(r"原因是([^，。；\n]+)", text)
    note_match = re.search(r"备注是([^，。；\n]+)", text)
    reason = (reason_match.group(1).strip() if reason_match else "") or "运营申请"
    note = note_match.group(1).strip() if note_match else ""

    budget_match = re.search(
        r"(?:日预算|预算).{0,8}?(?:调整到|调到|设置为)\s*(\d+(?:\.\d+)?)",
        text,
    )
    refund_match = re.search(r"(?:执行退款|发起退款|模拟退款)\s*(\d+(?:\.\d+)?)", text)
    status_match = re.search(r"更新为\s*(处理中|已解决)", text)
    new_status = {"处理中": "processing", "已解决": "resolved"}.get(status_match.group(1) if status_match else "", "")

    roles: set[str] = set()
    if any(word in text for word in ("广告", "投放", "消耗", "花费", "预算")) or campaign_ids or budget_match:
        roles.add("ads")
    if any(word in text for word in ("履约", "物流", "发货", "库存", "签收", "在途")) or skus:
        roles.add("fulfillment")
    if "订单" in text and (
        "fulfillment" in roles or not any(word in text for word in ("售后", "退款", "工单", "退货"))
    ):
        roles.add("fulfillment")
    if any(word in text for word in ("售后", "退款", "工单", "退货")) or ticket_ids or refund_match or status_match:
        roles.add("aftersales")

    ordered_roles = [role for role in ROLE_ORDER if role in roles]
    shop_id = shop_ids[0] if len(shop_ids) == 1 else None
    missing: list[str] = []
    if len(shop_ids) != 1:
        missing.append("shop_id")
    if not ordered_roles:
        missing.append("role")
    if budget_match and not campaign_ids:
        missing.append("campaign_id")
    if refund_match and not order_ids:
        missing.append("order_id")
    if refund_match is None and "执行退款" in text:
        missing.append("amount")
    if status_match and not ticket_ids:
        missing.append("ticket_id")

    actions: list[dict] = []
    if shop_id and not missing:
        actions.extend(
            _actions(
                text=text,
                shop_id=shop_id,
                roles=ordered_roles,
                days=days,
                campaign_ids=campaign_ids,
                order_ids=order_ids,
                ticket_ids=ticket_ids,
                skus=skus,
                reason=reason,
                note=note,
                budget=float(budget_match.group(1)) if budget_match else None,
                refund=float(refund_match.group(1)) if refund_match else None,
                new_status=new_status,
            )
        )
    return ParsedTask(
        shop_id=shop_id,
        roles=ordered_roles,
        objects={
            "days": days,
            "campaign_ids": campaign_ids,
            "order_ids": order_ids,
            "ticket_ids": ticket_ids,
            "skus": skus,
        },
        actions=actions,
        missing_fields=missing,
        parser="rule",
    )


def _actions(
    *,
    text: str,
    shop_id: str,
    roles: list[str],
    days: int,
    campaign_ids: list[str],
    order_ids: list[str],
    ticket_ids: list[str],
    skus: list[str],
    reason: str,
    note: str,
    budget: float | None,
    refund: float | None,
    new_status: str,
) -> list[dict]:
    actions: list[dict] = []
    if "ads" in roles:
        actions.append(_read("ads", "query_ad_spend", {"shop_id": shop_id, "days": days}))
        for campaign_id in campaign_ids:
            actions.append(
                _read("ads", "query_ad_campaign", {"shop_id": shop_id, "campaign_id": campaign_id})
            )
        if budget is not None:
            actions.append(_read("ads", "search_policy", {"shop_id": shop_id, "query": "广告 预算 审批"}))
            actions.append(
                _write(
                    "ads",
                    "adjust_ad_budget",
                    {
                        "shop_id": shop_id,
                        "campaign_id": campaign_ids[0],
                        "new_daily_budget": budget,
                        "reason": reason,
                    },
                )
            )
    if "fulfillment" in roles:
        wants_orders = bool(order_ids) or any(word in text for word in ("订单", "履约", "物流", "发货", "签收", "在途"))
        wants_stock = bool(skus) or "库存" in text
        if wants_orders or not wants_stock:
            actions.append(_read("fulfillment", "query_orders", {"shop_id": shop_id, "order_id": ""}))
            for order_id in order_ids:
                actions.append(
                    _read(
                        "fulfillment",
                        "query_order_logistics",
                        {"shop_id": shop_id, "order_id": order_id},
                    )
                )
        if wants_stock:
            if skus:
                for sku in skus:
                    actions.append(_read("fulfillment", "query_inventory", {"shop_id": shop_id, "sku": sku}))
            else:
                actions.append(_read("fulfillment", "query_inventory", {"shop_id": shop_id, "sku": ""}))
    if "aftersales" in roles:
        actions.append(
            _read(
                "aftersales",
                "query_tickets",
                {"shop_id": shop_id, "ticket_id": ticket_ids[0] if len(ticket_ids) == 1 else ""},
            )
        )
        for order_id in order_ids:
            actions.append(_read("aftersales", "query_orders", {"shop_id": shop_id, "order_id": order_id}))
            actions.append(
                _read(
                    "aftersales",
                    "query_order_logistics",
                    {"shop_id": shop_id, "order_id": order_id},
                )
            )
        policy_query = "退款 条款" if "退款" in text else "工单 状态" if "工单" in text else "售后 政策"
        actions.append(_read("aftersales", "search_policy", {"shop_id": shop_id, "query": policy_query}))
        if refund is not None:
            actions.append(
                _write(
                    "aftersales",
                    "simulate_refund",
                    {
                        "shop_id": shop_id,
                        "order_id": order_ids[0],
                        "amount": refund,
                        "reason": reason,
                    },
                )
            )
        if new_status:
            actions.append(
                _write(
                    "aftersales",
                    "update_ticket",
                    {
                        "shop_id": shop_id,
                        "ticket_id": ticket_ids[0],
                        "new_status": new_status,
                        "note": note or reason,
                    },
                )
            )
    return actions


def _read(role: str, tool: str, params: dict) -> dict:
    return {"role": role, "kind": "read", "tool": tool, "params": params}


def _write(role: str, tool: str, params: dict) -> dict:
    return {"role": role, "kind": "write", "tool": tool, "params": params}


async def parse_task(text: str, settings: Settings) -> ParsedTask:
    ruled = rule_parse(text)
    if resolve_llm(settings) is None:
        return ruled
    try:
        data = await complete_json(
            settings,
            "你是跨境电商运营任务解析器。只输出 JSON，不要解释。"
            "字段：shop_id, roles(ads/fulfillment/aftersales), days, campaign_ids, order_ids, "
            "ticket_ids, skus, budget, refund_amount, ticket_status(processing/resolved), reason。",
            text,
        )
    except (LLMError, Exception):
        ruled.parser = "rule_fallback"
        return ruled
    if not data:
        ruled.parser = "rule_fallback"
        return ruled
    checked = _accept_llm(data, ruled)
    if checked is None:
        ruled.parser = "rule_fallback"
        return ruled
    return checked


def _accept_llm(data: dict, ruled: ParsedTask) -> ParsedTask | None:
    """模型结果必须和规则抽出的店铺、写工具一致，避免模型改写资金动作。"""
    shop_id = data.get("shop_id") or ruled.shop_id
    if shop_id != ruled.shop_id:
        return None
    write_tools = {item["tool"] for item in ruled.actions if item["kind"] == "write"}
    hinted = set()
    if data.get("budget"):
        hinted.add("adjust_ad_budget")
    if data.get("refund_amount"):
        hinted.add("simulate_refund")
    if data.get("ticket_status"):
        hinted.add("update_ticket")
    if hinted != write_tools:
        return None
    ruled.parser = "qwen_compatible"
    return ruled

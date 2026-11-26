"""工具实现。返回统一信封，业务错误不抛出进程边界。"""

from __future__ import annotations

from app.domain import (
    adjust_ad_budget,
    query_ad_campaign,
    query_ad_spend,
    query_inventory,
    query_order_logistics,
    query_orders,
    query_tickets,
    search_policy,
    session_scope,
    simulate_refund,
    update_ticket,
)
from app.errors import BusinessError


def _ok(data: dict) -> dict:
    return {"ok": True, "data": data}


def _fail(exc: BusinessError) -> dict:
    return {"ok": False, "error_code": exc.code, "error_message": exc.message}


def query_ad_spend_tool(shop_id: str, days: int = 7) -> dict:
    """查询店铺近 N 天广告消耗。只读，不修改预算。"""
    try:
        with session_scope() as session:
            return _ok(query_ad_spend(session, shop_id, days))
    except BusinessError as exc:
        return _fail(exc)


def query_ad_campaign_tool(shop_id: str, campaign_id: str) -> dict:
    """查询单个广告计划的日预算和消耗。只读。"""
    try:
        with session_scope() as session:
            return _ok(query_ad_campaign(session, shop_id, campaign_id))
    except BusinessError as exc:
        return _fail(exc)


def query_orders_tool(shop_id: str, order_id: str = "") -> dict:
    """查询店铺订单。order_id 为空时返回该店全部订单。只读。"""
    try:
        with session_scope() as session:
            return _ok(query_orders(session, shop_id, order_id))
    except BusinessError as exc:
        return _fail(exc)


def query_order_logistics_tool(shop_id: str, order_id: str) -> dict:
    """查询订单物流节点。只读，不把实时状态写入知识库。"""
    try:
        with session_scope() as session:
            return _ok(query_order_logistics(session, shop_id, order_id))
    except BusinessError as exc:
        return _fail(exc)


def query_inventory_tool(shop_id: str, sku: str = "") -> dict:
    """查询店铺库存。sku 为空时返回该店全部 SKU。只读。"""
    try:
        with session_scope() as session:
            return _ok(query_inventory(session, shop_id, sku))
    except BusinessError as exc:
        return _fail(exc)


def query_tickets_tool(shop_id: str, ticket_id: str = "") -> dict:
    """查询售后工单。ticket_id 为空时返回该店工单。只读。"""
    try:
        with session_scope() as session:
            return _ok(query_tickets(session, shop_id, ticket_id))
    except BusinessError as exc:
        return _fail(exc)


def search_policy_tool(shop_id: str, query: str) -> dict:
    """按条款关键词检索适用政策，返回政策编号和摘录。只读。"""
    try:
        with session_scope() as session:
            return _ok(search_policy(session, shop_id, query))
    except BusinessError as exc:
        return _fail(exc)


def adjust_ad_budget_tool(
    shop_id: str,
    campaign_id: str,
    new_daily_budget: float,
    reason: str,
    operation_id: str,
    expected_daily_budget: float,
    task_id: str,
) -> dict:
    """调整广告日预算。必须携带 operation_id 和审批时看到的原预算。"""
    try:
        with session_scope() as session:
            return _ok(
                adjust_ad_budget(
                    session,
                    task_id=task_id,
                    shop_id=shop_id,
                    campaign_id=campaign_id,
                    new_daily_budget=new_daily_budget,
                    reason=reason,
                    operation_id=operation_id,
                    expected_daily_budget=expected_daily_budget,
                )
            )
    except BusinessError as exc:
        return _fail(exc)


def update_ticket_tool(
    shop_id: str,
    ticket_id: str,
    new_status: str,
    note: str,
    operation_id: str,
    expected_status: str,
    task_id: str,
) -> dict:
    """更新工单状态。必须携带 operation_id 和审批时的原状态。"""
    try:
        with session_scope() as session:
            return _ok(
                update_ticket(
                    session,
                    task_id=task_id,
                    shop_id=shop_id,
                    ticket_id=ticket_id,
                    new_status=new_status,
                    note=note,
                    operation_id=operation_id,
                    expected_status=expected_status,
                )
            )
    except BusinessError as exc:
        return _fail(exc)


def simulate_refund_tool(
    shop_id: str,
    order_id: str,
    amount: float,
    reason: str,
    operation_id: str,
    expected_status: str,
    task_id: str,
) -> dict:
    """模拟退款。不连接真实支付，必须携带 operation_id 和审批时的订单状态。"""
    try:
        with session_scope() as session:
            return _ok(
                simulate_refund(
                    session,
                    task_id=task_id,
                    shop_id=shop_id,
                    order_id=order_id,
                    amount=amount,
                    reason=reason,
                    operation_id=operation_id,
                    expected_status=expected_status,
                )
            )
    except BusinessError as exc:
        return _fail(exc)


TOOL_FUNCTIONS = [
    query_ad_spend_tool,
    query_ad_campaign_tool,
    query_orders_tool,
    query_order_logistics_tool,
    query_inventory_tool,
    query_tickets_tool,
    search_policy_tool,
    adjust_ad_budget_tool,
    update_ticket_tool,
    simulate_refund_tool,
]

TOOL_NAMES = {
    "query_ad_spend": query_ad_spend_tool,
    "query_ad_campaign": query_ad_campaign_tool,
    "query_orders": query_orders_tool,
    "query_order_logistics": query_order_logistics_tool,
    "query_inventory": query_inventory_tool,
    "query_tickets": query_tickets_tool,
    "search_policy": search_policy_tool,
    "adjust_ad_budget": adjust_ad_budget_tool,
    "update_ticket": update_ticket_tool,
    "simulate_refund": simulate_refund_tool,
}

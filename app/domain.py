"""测试环境业务库：广告、订单、库存、工单和政策条款。

写操作和读操作走同一事务边界。相同 operation_id 再次进入时直接返回原结果，不产生第二次写入。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.errors import BusinessError
from app.models import (
    Base,
    Campaign,
    ExecutionRecord,
    Inventory,
    Meta,
    Order,
    Policy,
    Shop,
    Ticket,
    User,
)
from app.runtime import get_factory
from app.security import hash_password

DATA_VERSION = "data_version"
TICKET_FLOW = {
    ("open", "processing"),
    ("open", "resolved"),
    ("processing", "resolved"),
}


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def wait_until_ready(engine: Engine, attempts: int = 20) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # 数据库容器启动有窗口期
            last_error = exc
            import time

            time.sleep(1)
    raise RuntimeError("数据库不可用") from last_error


@contextmanager
def session_scope():
    session = get_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def seed(engine: Engine, iterations: int) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        if session.get(Meta, DATA_VERSION) is not None:
            return
        session.add(Meta(key=DATA_VERSION, value="1"))
        session.add_all(
            [
                Shop(id="SHOP-1001", name="长沙一店", site="US", category="apparel"),
                Shop(id="SHOP-1002", name="深圳二店", site="US", category="electronics"),
            ]
        )
        session.add_all(_users(iterations))
        session.add_all(
            [
                Campaign(
                    id="C-501",
                    shop_id="SHOP-1001",
                    name="夏季促销",
                    daily_budget=200,
                    spend_7d=1280.5,
                    spend_14d=2400,
                    status="active",
                ),
                Campaign(
                    id="C-502",
                    shop_id="SHOP-1001",
                    name="品牌词",
                    daily_budget=150,
                    spend_7d=860,
                    spend_14d=1500,
                    status="active",
                ),
                Campaign(
                    id="C-601",
                    shop_id="SHOP-1002",
                    name="新品测试",
                    daily_budget=80,
                    spend_7d=210,
                    spend_14d=400,
                    status="active",
                ),
            ]
        )
        session.add_all(
            [
                Order(
                    id="O-9001",
                    shop_id="SHOP-1001",
                    status="shipped",
                    amount=59.9,
                    logistics="已离港，预计 3 天到港",
                ),
                Order(
                    id="O-9002",
                    shop_id="SHOP-1001",
                    status="delivered",
                    amount=29.9,
                    logistics="已签收",
                ),
                Order(
                    id="O-9003",
                    shop_id="SHOP-1001",
                    status="delivered",
                    amount=39.9,
                    logistics="已签收，买家反馈尺码不合适",
                ),
                Order(
                    id="O-8001",
                    shop_id="SHOP-1002",
                    status="shipped",
                    amount=99,
                    logistics="国内仓已发出",
                ),
            ]
        )
        session.add_all(
            [
                Inventory(sku="SKU-TS-01", shop_id="SHOP-1001", available=42, reserved=3, warehouse="US-WEST"),
                Inventory(sku="SKU-HD-02", shop_id="SHOP-1001", available=5, reserved=1, warehouse="US-EAST"),
                Inventory(sku="SKU-EB-09", shop_id="SHOP-1002", available=100, reserved=4, warehouse="SZ-01"),
            ]
        )
        session.add_all(
            [
                Ticket(
                    id="T-3001",
                    shop_id="SHOP-1001",
                    order_id="O-9003",
                    status="open",
                    reason="尺码不合适",
                ),
                Ticket(
                    id="T-3002",
                    shop_id="SHOP-1001",
                    order_id="O-9001",
                    status="processing",
                    reason="物流延迟",
                ),
            ]
        )
        session.add_all(
            [
                Policy(
                    id="POL-REFUND-7",
                    title="服饰退款时效",
                    body="服饰类目签收后 7 天内可申请退款，金额不得超过订单实付，且必须人工审批后才能模拟执行。",
                    tags="退款 条款 售后 政策",
                ),
                Policy(
                    id="POL-AD-BUDGET",
                    title="广告日预算调整",
                    body="广告日预算调整属于资金写操作，必须人工审批。上调超过 50% 时审批单需要标明风险。",
                    tags="广告 预算 审批",
                ),
                Policy(
                    id="POL-TICKET",
                    title="工单状态流转",
                    body="工单只允许 open 到 processing、open 到 resolved、processing 到 resolved。更新前要核对关联订单。",
                    tags="工单 售后 状态",
                ),
                Policy(
                    id="POL-FULFILL",
                    title="履约状态查询",
                    body="履约异常先核对订单状态和物流节点。实时状态不写入知识库，回答中保留查询时间。",
                    tags="履约 物流 订单",
                ),
            ]
        )
        session.commit()


def _users(iterations: int) -> list[User]:
    rows = [
        ("user-lead", "lead", "Lead#2026", "运营主管", "SHOP-1001", "ads,fulfillment,aftersales", False),
        ("user-auditor", "auditor", "Audit#2026", "审批人", "SHOP-1001", "", True),
        ("user-ads", "ads_ops", "Ads#2026", "广告运营", "SHOP-1001", "ads", False),
        ("user-shop2", "shop2_ops", "Shop2#2026", "二店运营", "SHOP-1002", "ads,fulfillment,aftersales", False),
    ]
    users = []
    for user_id, username, password, display, shops, roles, can_approve in rows:
        users.append(
            User(
                id=user_id,
                username=username,
                password_hash=hash_password(password, iterations),
                display_name=display,
                shops=shops,
                roles=roles,
                can_approve=can_approve,
            )
        )
    return users


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_shop(session: Session, shop_id: str) -> Shop:
    shop = session.get(Shop, shop_id)
    if shop is None:
        raise BusinessError("NOT_FOUND", f"店铺不存在: {shop_id}")
    return shop


def data_version(session: Session) -> str:
    row = session.get(Meta, DATA_VERSION)
    return row.value if row else "0"


def _bump(session: Session) -> None:
    row = session.get(Meta, DATA_VERSION)
    if row is None:
        session.add(Meta(key=DATA_VERSION, value="1"))
        return
    row.value = str(int(row.value) + 1)


def query_ad_spend(session: Session, shop_id: str, days: int) -> dict:
    _require_shop(session, shop_id)
    rows = session.scalars(select(Campaign).where(Campaign.shop_id == shop_id)).all()
    use_14 = days >= 14
    campaigns = []
    total = 0.0
    for row in rows:
        spend = row.spend_14d if use_14 else row.spend_7d
        total += spend
        campaigns.append(
            {
                "campaign_id": row.id,
                "name": row.name,
                "daily_budget": row.daily_budget,
                "spend": spend,
                "status": row.status,
            }
        )
    return {
        "shop_id": shop_id,
        "days": days,
        "currency": "USD",
        "total_spend": round(total, 2),
        "campaigns": campaigns,
        "queried_at": _now(),
    }


def query_ad_campaign(session: Session, shop_id: str, campaign_id: str) -> dict:
    row = session.get(Campaign, campaign_id)
    if row is None or row.shop_id != shop_id:
        raise BusinessError("NOT_FOUND", "广告计划不存在或不属于该店铺")
    return {
        "campaign_id": row.id,
        "shop_id": row.shop_id,
        "name": row.name,
        "daily_budget": row.daily_budget,
        "spend_7d": row.spend_7d,
        "spend_14d": row.spend_14d,
        "status": row.status,
        "currency": row.currency,
        "queried_at": _now(),
    }


def query_orders(session: Session, shop_id: str, order_id: str = "") -> dict:
    _require_shop(session, shop_id)
    stmt = select(Order).where(Order.shop_id == shop_id)
    if order_id:
        stmt = stmt.where(Order.id == order_id)
    rows = session.scalars(stmt).all()
    if order_id and not rows:
        raise BusinessError("NOT_FOUND", "订单不存在或不属于该店铺")
    return {
        "shop_id": shop_id,
        "orders": [_order_dict(row) for row in rows],
        "queried_at": _now(),
    }


def query_order_logistics(session: Session, shop_id: str, order_id: str) -> dict:
    row = session.get(Order, order_id)
    if row is None or row.shop_id != shop_id:
        raise BusinessError("NOT_FOUND", "订单不存在或不属于该店铺")
    return {
        "shop_id": shop_id,
        "order_id": row.id,
        "status": row.status,
        "logistics": row.logistics,
        "queried_at": _now(),
    }


def query_inventory(session: Session, shop_id: str, sku: str = "") -> dict:
    _require_shop(session, shop_id)
    stmt = select(Inventory).where(Inventory.shop_id == shop_id)
    if sku:
        stmt = stmt.where(Inventory.sku == sku)
    rows = session.scalars(stmt).all()
    if sku and not rows:
        raise BusinessError("NOT_FOUND", "SKU 不存在或不属于该店铺")
    return {
        "shop_id": shop_id,
        "items": [
            {
                "sku": row.sku,
                "available": row.available,
                "reserved": row.reserved,
                "warehouse": row.warehouse,
            }
            for row in rows
        ],
        "queried_at": _now(),
    }


def query_tickets(session: Session, shop_id: str, ticket_id: str = "") -> dict:
    _require_shop(session, shop_id)
    stmt = select(Ticket).where(Ticket.shop_id == shop_id)
    if ticket_id:
        stmt = stmt.where(Ticket.id == ticket_id)
    rows = session.scalars(stmt).all()
    if ticket_id and not rows:
        raise BusinessError("NOT_FOUND", "工单不存在或不属于该店铺")
    return {
        "shop_id": shop_id,
        "tickets": [
            {
                "ticket_id": row.id,
                "order_id": row.order_id,
                "status": row.status,
                "reason": row.reason,
                "note": row.note,
            }
            for row in rows
        ],
        "queried_at": _now(),
    }


def search_policy(session: Session, shop_id: str, query: str) -> dict:
    _require_shop(session, shop_id)
    rows = session.scalars(select(Policy)).all()
    scored: list[tuple[int, Policy]] = []
    tokens = [part for part in query.replace("，", " ").split() if part]
    if not tokens:
        tokens = [query] if query else []
    for row in rows:
        blob = f"{row.title} {row.body} {row.tags}"
        score = sum(1 for token in tokens if token and token in blob)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    picked = [row for _, row in scored[:2]] or list(rows[:1])
    return {
        "shop_id": shop_id,
        "query": query,
        "clauses": [
            {"policy_id": row.id, "title": row.title, "excerpt": row.body, "tags": row.tags}
            for row in picked
        ],
        "queried_at": _now(),
    }


def adjust_ad_budget(
    session: Session,
    *,
    task_id: str,
    shop_id: str,
    campaign_id: str,
    new_daily_budget: float,
    reason: str,
    operation_id: str,
    expected_daily_budget: float,
) -> dict:
    replay = _replay(session, operation_id)
    if replay is not None:
        return replay
    row = session.get(Campaign, campaign_id)
    if row is None or row.shop_id != shop_id:
        raise BusinessError("NOT_FOUND", "广告计划不存在或不属于该店铺")
    if abs(row.daily_budget - expected_daily_budget) > 0.001:
        raise BusinessError("STALE_OBJECT", "审批期间日预算已变化，拒绝写入")
    before = row.daily_budget
    row.daily_budget = new_daily_budget
    result = {
        "campaign_id": row.id,
        "before": {"daily_budget": before},
        "after": {"daily_budget": new_daily_budget},
        "reason": reason,
        "replayed": False,
    }
    _record(session, operation_id, task_id, "adjust_ad_budget", shop_id, result)
    _bump(session)
    return result


def update_ticket(
    session: Session,
    *,
    task_id: str,
    shop_id: str,
    ticket_id: str,
    new_status: str,
    note: str,
    operation_id: str,
    expected_status: str,
) -> dict:
    replay = _replay(session, operation_id)
    if replay is not None:
        return replay
    row = session.get(Ticket, ticket_id)
    if row is None or row.shop_id != shop_id:
        raise BusinessError("NOT_FOUND", "工单不存在或不属于该店铺")
    if row.status != expected_status:
        raise BusinessError("STALE_OBJECT", "审批期间工单状态已变化，拒绝写入")
    if (row.status, new_status) not in TICKET_FLOW:
        raise BusinessError("INVALID_TRANSITION", f"不允许从 {row.status} 流转到 {new_status}")
    before = row.status
    row.status = new_status
    if note:
        row.note = note
    result = {
        "ticket_id": row.id,
        "before": {"status": before},
        "after": {"status": new_status, "note": row.note},
        "replayed": False,
    }
    _record(session, operation_id, task_id, "update_ticket", shop_id, result)
    _bump(session)
    return result


def simulate_refund(
    session: Session,
    *,
    task_id: str,
    shop_id: str,
    order_id: str,
    amount: float,
    reason: str,
    operation_id: str,
    expected_status: str,
) -> dict:
    replay = _replay(session, operation_id)
    if replay is not None:
        return replay
    row = session.get(Order, order_id)
    if row is None or row.shop_id != shop_id:
        raise BusinessError("NOT_FOUND", "订单不存在或不属于该店铺")
    if row.status != expected_status:
        raise BusinessError("STALE_OBJECT", "审批期间订单状态已变化，拒绝写入")
    if row.status != "delivered":
        raise BusinessError("INVALID_STATE", "只有已签收订单可以模拟退款")
    if amount <= 0 or row.refunded_amount + amount - row.amount > 0.001:
        raise BusinessError("INVALID_AMOUNT", "退款金额超出可退余额")
    before = row.refunded_amount
    row.refunded_amount = round(before + amount, 2)
    row.refund_status = "simulated"
    result = {
        "order_id": row.id,
        "before": {"refunded_amount": before, "status": expected_status},
        "after": {"refunded_amount": row.refunded_amount, "refund_status": row.refund_status},
        "reason": reason,
        "simulated": True,
        "replayed": False,
    }
    _record(session, operation_id, task_id, "simulate_refund", shop_id, result)
    _bump(session)
    return result


def _replay(session: Session, operation_id: str) -> dict | None:
    existing = session.get(ExecutionRecord, operation_id)
    if existing is None:
        return None
    data = json.loads(existing.result_json)
    data["replayed"] = True
    return data


def _record(session: Session, operation_id: str, task_id: str, tool: str, shop_id: str, result: dict) -> None:
    session.add(
        ExecutionRecord(
            operation_id=operation_id,
            task_id=task_id,
            tool=tool,
            shop_id=shop_id,
            params_json=json.dumps({"tool": tool, "shop_id": shop_id}, ensure_ascii=False),
            result_json=json.dumps(result, ensure_ascii=False),
            status="executed",
        )
    )


def _order_dict(row: Order) -> dict:
    return {
        "order_id": row.id,
        "status": row.status,
        "amount": row.amount,
        "logistics": row.logistics,
        "refunded_amount": row.refunded_amount,
        "refund_status": row.refund_status,
    }


def read_business_field(session: Session, kind: str, object_id: str, field: str):
    if kind == "campaign":
        row = session.get(Campaign, object_id)
    elif kind == "order":
        row = session.get(Order, object_id)
    elif kind == "ticket":
        row = session.get(Ticket, object_id)
    else:
        raise KeyError(kind)
    if row is None:
        raise KeyError(object_id)
    return getattr(row, field)


def count_executions(session: Session, task_id: str | None = None) -> int:
    stmt = select(ExecutionRecord)
    if task_id:
        stmt = stmt.where(ExecutionRecord.task_id == task_id)
    return len(session.scalars(stmt).all())

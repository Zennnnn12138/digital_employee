"""任务接收、幂等、审批占用和图恢复。"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass

from langgraph.types import Command
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.errors import AppError
from app.models import Approval, Campaign, Inventory, Order, Shop, TaskEvent, TaskRow, Ticket, utcnow
from app.runtime import reset_context, set_context
from app.security import Actor, load_actor

logger = logging.getLogger("digital_employee.tasks")


@dataclass
class TaskView:
    task_id: str
    status: str
    message: str
    shop_id: str | None
    roles: list[str]
    delivery: dict
    approval: dict | None
    trace: list[dict]
    tool_calls: list[dict]
    parser: str
    handoff_reason: str
    missing_fields: list[str]
    created_at: str
    updated_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class TaskService:
    def __init__(self, factory: sessionmaker, graph, settings: Settings) -> None:
        self.factory = factory
        self.graph = graph
        self.settings = settings

    async def create_task(self, actor: Actor, message: str, idempotency_key: str | None) -> TaskView:
        text = message.strip()
        if len(text) < 2:
            raise AppError(422, "任务内容太短")
        key = (idempotency_key or "").strip() or None
        body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.factory() as session:
            if key:
                existing = self._by_key(session, actor.user_id, key)
                if existing is not None:
                    if existing.body_hash != body_hash:
                        raise AppError(409, "幂等键已被不同任务使用")
                    return self._view(existing)
            task = TaskRow(
                id=uuid.uuid4().hex,
                user_id=actor.user_id,
                idempotency_key=key,
                body_hash=body_hash,
                message=text,
                status="running",
                result_json="{}",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            session.add(task)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self._by_key(session, actor.user_id, key or "")
                if existing is not None and existing.body_hash == body_hash:
                    return self._view(existing)
                raise AppError(409, "幂等键已被不同任务使用") from None
            task_id = task.id
        result = await self._invoke(actor, _initial_state(task_id, actor, text), task_id)
        with self.factory() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise AppError(500, "任务记录丢失")
            return self._apply_result(session, task, result)

    async def decide(self, approver: Actor, task_id: str, decision: str, note: str) -> TaskView:
        if decision not in {"approved", "rejected"}:
            raise AppError(422, "审批结论只能是 approved 或 rejected")
        note = (note or "").strip()[:500]
        with self.factory() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise AppError(404, "任务不存在")
            if approver.user_id == task.user_id:
                raise AppError(403, "不能审批自己提交的任务")
            if not approver.can_approve:
                raise AppError(403, "没有审批权限")
            current = json.loads(task.result_json or "{}")
            shop_id = current.get("shop_id") or ""
            if shop_id and shop_id not in approver.shops:
                raise AppError(403, "无权审批该店铺的任务")
            if task.status != "awaiting_approval":
                raise AppError(409, f"任务状态是 {task.status}，不能重复审批")
            task.status = "resuming"
            task.updated_at = utcnow()
            session.add(
                Approval(
                    id=uuid.uuid4().hex,
                    task_id=task.id,
                    operator_id=approver.user_id,
                    operator_name=approver.username,
                    decision=decision,
                    note=note,
                    payload_json=json.dumps(current.get("approval") or {}, ensure_ascii=False),
                    created_at=utcnow(),
                )
            )
            session.commit()
            owner_id = task.user_id
        owner = load_actor(self.factory, owner_id)
        result = await self._invoke(
            owner,
            None,
            task_id,
            resume=Command(resume={"decision": decision, "operator": approver.username, "note": note}),
        )
        with self.factory() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise AppError(500, "任务记录丢失")
            return self._apply_result(session, task, result)

    def get_task(self, actor: Actor, task_id: str) -> TaskView:
        with self.factory() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise AppError(404, "任务不存在")
            self._visible(actor, task)
            return self._view(task)

    def list_tasks(self, actor: Actor, limit: int = 40) -> list[TaskView]:
        with self.factory() as session:
            rows = session.scalars(select(TaskRow).order_by(TaskRow.updated_at.desc())).all()
            views = []
            for row in rows:
                try:
                    self._visible(actor, row)
                except AppError:
                    continue
                views.append(self._view(row))
                if len(views) >= limit:
                    break
            return views

    def workspace(self, actor: Actor) -> dict:
        shops = list(actor.shops)
        with self.factory() as session:
            if not shops:
                return {"shops": [], "campaigns": [], "orders": [], "tickets": [], "inventory": [], "counts": {}}
            shop_rows = session.scalars(select(Shop).where(Shop.id.in_(shops))).all()
            campaigns = session.scalars(select(Campaign).where(Campaign.shop_id.in_(shops))).all()
            orders = session.scalars(select(Order).where(Order.shop_id.in_(shops))).all()
            tickets = session.scalars(select(Ticket).where(Ticket.shop_id.in_(shops))).all()
            inventory = session.scalars(select(Inventory).where(Inventory.shop_id.in_(shops))).all()
            task_rows = session.scalars(select(TaskRow).order_by(TaskRow.updated_at.desc())).all()
            counts: dict[str, int] = {}
            for row in task_rows:
                try:
                    self._visible(actor, row)
                except AppError:
                    continue
                counts[row.status] = counts.get(row.status, 0) + 1
            counts["total"] = sum(counts.values())
            return {
                "shops": [
                    {"id": row.id, "name": row.name, "site": row.site, "category": row.category}
                    for row in shop_rows
                ],
                "campaigns": [
                    {
                        "id": row.id,
                        "shop_id": row.shop_id,
                        "name": row.name,
                        "daily_budget": row.daily_budget,
                        "spend_7d": row.spend_7d,
                        "status": row.status,
                        "currency": row.currency,
                    }
                    for row in campaigns
                ],
                "orders": [
                    {
                        "id": row.id,
                        "shop_id": row.shop_id,
                        "status": row.status,
                        "amount": row.amount,
                        "logistics": row.logistics,
                        "refunded_amount": row.refunded_amount,
                    }
                    for row in orders
                ],
                "tickets": [
                    {
                        "id": row.id,
                        "shop_id": row.shop_id,
                        "order_id": row.order_id,
                        "status": row.status,
                        "reason": row.reason,
                    }
                    for row in tickets
                ],
                "inventory": [
                    {
                        "sku": row.sku,
                        "shop_id": row.shop_id,
                        "available": row.available,
                        "reserved": row.reserved,
                        "warehouse": row.warehouse,
                    }
                    for row in inventory
                ],
                "counts": counts,
            }

    def list_pending(self, actor: Actor) -> list[TaskView]:
        if not actor.can_approve:
            raise AppError(403, "没有审批权限")
        with self.factory() as session:
            rows = session.scalars(select(TaskRow).where(TaskRow.status == "awaiting_approval")).all()
            views = []
            for row in rows:
                if row.user_id == actor.user_id:
                    continue
                shop_id = json.loads(row.result_json or "{}").get("shop_id") or ""
                if shop_id and shop_id not in actor.shops:
                    continue
                views.append(self._view(row))
            return views

    def list_events(self, actor: Actor, task_id: str) -> list[dict]:
        with self.factory() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise AppError(404, "任务不存在")
            self._visible(actor, task)
            rows = session.scalars(
                select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)
            ).all()
            return [
                {"event": row.event_type, "payload": json.loads(row.payload_json or "{}")}
                for row in rows
            ]

    async def _invoke(self, actor: Actor, payload: dict | None, task_id: str, resume=None):
        tokens = set_context(self.factory, actor, self.settings)
        try:
            if resume is None:
                return await self.graph.ainvoke(payload, _config(task_id))
            return await self.graph.ainvoke(resume, _config(task_id))
        except Exception:
            logger.exception("任务编排失败: %s", task_id)
            self._mark_failed(task_id)
            raise
        finally:
            reset_context(tokens)

    def _apply_result(self, session, task: TaskRow, result: dict) -> TaskView:
        approval = _interrupt_value(result)
        status = "awaiting_approval" if approval is not None else (result.get("status") or "failed")
        payload = {
            "shop_id": result.get("shop_id") or "",
            "roles": result.get("roles") or [],
            "parser": result.get("parser") or "",
            "handoff_reason": result.get("handoff_reason") or "",
            "missing_fields": result.get("missing_fields") or [],
            "delivery": result.get("delivery") or {},
            "approval": approval,
            "trace": result.get("trace") or [],
            "tool_calls": result.get("tool_calls") or [],
        }
        task.status = status
        task.result_json = json.dumps(payload, ensure_ascii=False)
        task.updated_at = utcnow()
        session.execute(delete(TaskEvent).where(TaskEvent.task_id == task.id))
        now = utcnow()
        for item in payload["trace"]:
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type="node",
                    payload_json=json.dumps(item, ensure_ascii=False),
                    created_at=now,
                )
            )
        if approval is not None:
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type="approval_required",
                    payload_json=json.dumps(approval, ensure_ascii=False),
                    created_at=now,
                )
            )
        session.add(
            TaskEvent(
                task_id=task.id,
                event_type="status",
                payload_json=json.dumps({"status": status}, ensure_ascii=False),
                created_at=now,
            )
        )
        session.commit()
        return self._view(task)

    def _mark_failed(self, task_id: str) -> None:
        with self.factory() as session:
            task = session.get(TaskRow, task_id)
            if task is None or task.status in {"delivered", "handoff", "clarifying"}:
                return
            task.status = "failed"
            task.updated_at = utcnow()
            session.commit()

    def _visible(self, actor: Actor, task: TaskRow) -> None:
        if task.user_id == actor.user_id:
            return
        shop_id = json.loads(task.result_json or "{}").get("shop_id") or ""
        if actor.can_approve and (not shop_id or shop_id in actor.shops):
            return
        raise AppError(403, "无权查看该任务")

    def _view(self, task: TaskRow) -> TaskView:
        payload = json.loads(task.result_json or "{}")
        return TaskView(
            task_id=task.id,
            status=task.status,
            message=task.message,
            shop_id=payload.get("shop_id") or None,
            roles=payload.get("roles") or [],
            delivery=payload.get("delivery") or {},
            approval=payload.get("approval"),
            trace=payload.get("trace") or [],
            tool_calls=payload.get("tool_calls") or [],
            parser=payload.get("parser") or "",
            handoff_reason=payload.get("handoff_reason") or "",
            missing_fields=payload.get("missing_fields") or [],
            created_at=task.created_at.isoformat() if task.created_at else "",
            updated_at=task.updated_at.isoformat() if task.updated_at else "",
        )

    def _by_key(self, session, user_id: str, key: str) -> TaskRow | None:
        if not key:
            return None
        return session.scalar(
            select(TaskRow).where(TaskRow.user_id == user_id, TaskRow.idempotency_key == key)
        )


def _initial_state(task_id: str, actor: Actor, message: str) -> dict:
    return {
        "task_id": task_id,
        "user_id": actor.user_id,
        "username": actor.username,
        "allowed_shops": list(actor.shops),
        "allowed_roles": list(actor.roles),
        "raw_input": message,
        "shop_id": "",
        "roles": [],
        "role_cursor": 0,
        "objects": {},
        "actions": [],
        "missing_fields": [],
        "clarification": "",
        "evidence": [],
        "tool_calls": [],
        "pending_writes": [],
        "blockers": [],
        "approval_decision": "",
        "approval_note": "",
        "write_results": [],
        "delivery": {},
        "status": "parsing",
        "handoff_reason": "",
        "trace": [],
        "step_count": 0,
        "parser": "rule",
    }


def _config(task_id: str) -> dict:
    return {"configurable": {"thread_id": task_id}, "recursion_limit": 40}


def _interrupt_value(result: dict):
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return value if isinstance(value, dict) else {"raw": str(value)}

"""工具网关：参数校验、店铺和岗位权限、超时、只读缓存，然后再进入 FastMCP。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field
from fastmcp import Client

from app.cache import Cache
from app.domain import data_version, session_scope
from app.errors import PermissionDenied, ValidationFailed
from app.runtime import get_actor
from app.tools.mcp_server import get_mcp

logger = logging.getLogger("digital_employee.gateway")

READ_TOOLS = {
    "query_ad_spend",
    "query_ad_campaign",
    "query_orders",
    "query_order_logistics",
    "query_inventory",
    "query_tickets",
    "search_policy",
}
WRITE_TOOLS = {"adjust_ad_budget", "update_ticket", "simulate_refund"}
TOOL_ROLES = {
    "query_ad_spend": {"ads"},
    "query_ad_campaign": {"ads"},
    "adjust_ad_budget": {"ads"},
    "query_orders": {"fulfillment", "aftersales"},
    "query_order_logistics": {"fulfillment", "aftersales"},
    "query_inventory": {"fulfillment"},
    "query_tickets": {"aftersales"},
    "search_policy": {"ads", "aftersales"},
    "update_ticket": {"aftersales"},
    "simulate_refund": {"aftersales"},
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SpendIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    days: int = Field(default=7, ge=1, le=90)


class CampaignIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    campaign_id: str = Field(pattern=r"^C-\d+$")


class OrdersIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    order_id: str = ""


class LogisticsIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    order_id: str = Field(pattern=r"^O-\d+$")


class InventoryIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    sku: str = ""


class TicketsIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    ticket_id: str = ""


class PolicyIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    query: str = Field(min_length=1, max_length=200)


class BudgetIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    campaign_id: str = Field(pattern=r"^C-\d+$")
    new_daily_budget: float = Field(gt=0, le=10000)
    reason: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=8, max_length=64)
    expected_daily_budget: float
    task_id: str = Field(min_length=8, max_length=64)


class TicketUpdateIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    ticket_id: str = Field(pattern=r"^T-\d+$")
    new_status: str = Field(pattern=r"^(processing|resolved)$")
    note: str = Field(default="", max_length=200)
    operation_id: str = Field(min_length=8, max_length=64)
    expected_status: str = Field(min_length=2, max_length=32)
    task_id: str = Field(min_length=8, max_length=64)


class RefundIn(_Model):
    shop_id: str = Field(pattern=r"^SHOP-\d+$")
    order_id: str = Field(pattern=r"^O-\d+$")
    amount: float = Field(gt=0, le=100000)
    reason: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=8, max_length=64)
    expected_status: str = Field(min_length=2, max_length=32)
    task_id: str = Field(min_length=8, max_length=64)


MODELS: dict[str, type[_Model]] = {
    "query_ad_spend": SpendIn,
    "query_ad_campaign": CampaignIn,
    "query_orders": OrdersIn,
    "query_order_logistics": LogisticsIn,
    "query_inventory": InventoryIn,
    "query_tickets": TicketsIn,
    "search_policy": PolicyIn,
    "adjust_ad_budget": BudgetIn,
    "update_ticket": TicketUpdateIn,
    "simulate_refund": RefundIn,
}


class ToolGateway:
    def __init__(self, cache: Cache, timeout_seconds: float, cache_ttl_seconds: int) -> None:
        self.cache = cache
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds

    async def call(self, name: str, params: dict) -> dict:
        started = time.perf_counter()
        kind = "write" if name in WRITE_TOOLS else "read"
        try:
            validated = self._validate(name, params)
            self._authorize(name, validated)
            if name in READ_TOOLS:
                cached = self._read_cache(name, validated)
                if cached is not None:
                    return self._pack(name, kind, True, cached, None, None, started, True)
            payload = await asyncio.wait_for(self._call_mcp(name, validated), self.timeout_seconds)
        except ValidationFailed as exc:
            return self._pack(name, kind, False, None, "VALIDATION", exc.message, started, False)
        except PermissionDenied as exc:
            return self._pack(name, kind, False, None, "PERMISSION_DENIED", exc.message, started, False)
        except asyncio.TimeoutError:
            return self._pack(name, kind, False, None, "TIMEOUT", "工具超时", started, False)
        except Exception as exc:
            logger.exception("工具调用失败: %s", name)
            return self._pack(name, kind, False, None, "TOOL_ERROR", str(exc), started, False)

        if not payload.get("ok"):
            return self._pack(
                name,
                kind,
                False,
                None,
                payload.get("error_code") or "TOOL_ERROR",
                payload.get("error_message") or "工具返回失败",
                started,
                False,
            )
        data = payload.get("data") or {}
        if name in READ_TOOLS:
            self._write_cache(name, validated, data)
        return self._pack(name, kind, True, data, None, None, started, False)

    def _validate(self, name: str, params: dict) -> dict:
        model = MODELS.get(name)
        if model is None:
            raise ValidationFailed(f"未知工具: {name}")
        try:
            return model.model_validate(params).model_dump()
        except Exception as exc:
            raise ValidationFailed(str(exc)) from exc

    def _authorize(self, name: str, params: dict) -> None:
        actor = get_actor()
        shop_id = params.get("shop_id", "")
        if shop_id not in actor.shops:
            raise PermissionDenied(f"无权访问店铺 {shop_id}")
        allowed = TOOL_ROLES.get(name, set())
        if not (set(actor.roles) & allowed):
            raise PermissionDenied(f"当前岗位不能调用 {name}")

    async def _call_mcp(self, name: str, params: dict) -> dict:
        async with Client(get_mcp()) as client:
            result = await client.call_tool(name, params, timeout=self.timeout_seconds)
        if getattr(result, "is_error", False):
            return {"ok": False, "error_code": "TOOL_ERROR", "error_message": "MCP 返回错误"}
        data = result.data
        if isinstance(data, dict):
            return data
        return {"ok": False, "error_code": "TOOL_ERROR", "error_message": "工具返回结构异常"}

    def _read_cache(self, name: str, params: dict) -> dict | None:
        try:
            with session_scope() as session:
                version = data_version(session)
        except Exception:
            return None
        return self.cache.get(_cache_key(version, name, params))

    def _write_cache(self, name: str, params: dict, data: dict) -> None:
        try:
            with session_scope() as session:
                version = data_version(session)
        except Exception:
            return
        self.cache.set(_cache_key(version, name, params), data, self.cache_ttl_seconds)

    def _pack(
        self,
        name: str,
        kind: str,
        ok: bool,
        data: dict | None,
        error_code: str | None,
        error_message: str | None,
        started: float,
        cached: bool,
    ) -> dict:
        return {
            "tool": name,
            "kind": kind,
            "ok": ok,
            "data": data,
            "error_code": error_code,
            "error_message": error_message,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "cached": cached,
            "queried_at": datetime.now(timezone.utc).isoformat(),
        }


def _cache_key(version: str, name: str, params: dict) -> str:
    raw = json.dumps(params, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"de:{version}:{name}:{digest}"

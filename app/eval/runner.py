"""执行 60 组任务并汇总交付率、越权和重复写入。"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from sqlalchemy import func, select

from app.bootstrap import open_resources
from app.config import Settings
from app.domain import count_executions, read_business_field
from app.errors import AppError
from app.eval.cases import CASES, Case
from app.models import TaskRow
from app.security import load_actor_by_username
from app.services.tasks import TaskView
from app.tools.gateway import WRITE_TOOLS


def case_settings(directory: Path) -> Settings:
    database = (directory / "app.db").resolve().as_posix()
    checkpoint = (directory / "checkpoints.sqlite").resolve()
    return Settings(
        database_url=f"sqlite:///{database}",
        checkpoint_path=str(checkpoint),
        jwt_secret="eval-secret",
        llm_provider="rule",
        password_iterations=1000,
        redis_url="",
        langfuse_public_key="",
        langfuse_secret_key="",
    )


async def run_suite() -> dict:
    if len(CASES) != 60:
        raise RuntimeError(f"回归集应为 60 组，当前 {len(CASES)}")
    results = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix=f"de-{case.id}-") as tmp:
            async with open_resources(case_settings(Path(tmp))) as resources:
                results.append(await execute_case(resources, case))
    passed = [item for item in results if item["ok"]]
    by_kind: dict[str, dict[str, int]] = {}
    for item in results:
        bucket = by_kind.setdefault(item["kind"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(item["ok"])
    unauthorized = [item for item in results if item["kind"] == "unauth"]
    duplicates = [item for item in results if item["kind"] in {"duplicate_read", "duplicate_write", "idempotency_conflict"}]
    normal = [item for item in results if item["kind"] in {"read", "cross", "approve"}]
    report = {
        "total": len(results),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(results), 4),
        "normal_delivery_rate": _rate(normal),
        "unauthorized_blocked": all(item["ok"] for item in unauthorized),
        "duplicate_safe": all(item["ok"] for item in duplicates),
        "by_kind": by_kind,
        "failures": [item for item in results if not item["ok"]],
    }
    return report


async def execute_case(resources, case: Case) -> dict:
    failures: list[str] = []

    def check(condition: bool, detail: str) -> None:
        if not condition:
            failures.append(detail)

    actor = load_actor_by_username(resources.factory, case.user)
    try:
        if case.kind == "idempotency_conflict":
            first = await resources.service.create_task(actor, case.message, case.idempotency_key)
            try:
                await resources.service.create_task(actor, case.message2, case.idempotency_key)
                failures.append("不同内容复用幂等键时没有返回 409")
            except AppError as exc:
                check(exc.status_code == 409, f"冲突状态码 {exc.status_code}")
            check(first.status == "delivered", f"状态 {first.status}")
            check(_writes(resources, first.task_id) == 0, "只读冲突用例产生了写入")
        elif case.kind == "duplicate_read":
            first = await resources.service.create_task(actor, case.message, case.idempotency_key)
            second = await resources.service.create_task(actor, case.message, case.idempotency_key)
            check(first.task_id == second.task_id, "重复提交生成了新任务")
            check(first.status == case.expect_status, f"状态 {first.status}")
            check(_task_count(resources, actor.user_id, case.idempotency_key) == 1, "任务行数不是 1")
            check(_has_tools(first, case.expect_tools), f"工具 { _names(first) }")
            check(_writes(resources, first.task_id) == 0, "只读重放产生了写入")
        elif case.kind in {"approve", "reject", "duplicate_write"}:
            created = await resources.service.create_task(actor, case.message, case.idempotency_key or None)
            check(created.status == "awaiting_approval", f"审批前状态 {created.status} {created.handoff_reason}")
            if case.expect_approval_contains:
                blob = json.dumps(created.approval or {}, ensure_ascii=False)
                check(case.expect_approval_contains in blob, "审批单缺少风险说明")
            check(_writes(resources, created.task_id) == 0, "审批前已经写入")
            auditor = load_actor_by_username(resources.factory, "auditor")
            decision = "rejected" if case.kind == "reject" else "approved"
            final = await resources.service.decide(auditor, created.task_id, decision, "回归审批")
            check(final.status == case.expect_status, f"审批后状态 {final.status} {final.handoff_reason}")
            check(_has_tools(final, case.expect_tools), f"工具 {_names(final)}")
            if case.kind == "reject":
                called = set(_names(final))
                check(not (called & WRITE_TOOLS), f"拒绝后仍调用了写工具 {called & WRITE_TOOLS}")
            check(_writes(resources, created.task_id) == case.expect_writes, f"写入次数 {_writes(resources, created.task_id)}")
            if case.target:
                actual = _field(resources, case.target)
                check(_same(actual, case.target[3]), f"{case.target} 实际 {actual}")
            if case.kind == "duplicate_write":
                try:
                    await resources.service.decide(auditor, created.task_id, "approved", "再次审批")
                    failures.append("重复审批没有被拒绝")
                except AppError as exc:
                    check(exc.status_code == 409, f"重复审批状态码 {exc.status_code}")
                replayed = await resources.service.create_task(actor, case.message, case.idempotency_key)
                check(replayed.task_id == created.task_id, "相同幂等键生成了新任务")
                check(_writes(resources, created.task_id) == 1, "重复提交产生了额外写入")
                if case.target:
                    actual = _field(resources, case.target)
                    check(_same(actual, case.target[3]), f"重放后字段变化 {actual}")
        else:
            created = await resources.service.create_task(actor, case.message, None)
            check(created.status == case.expect_status, f"状态 {created.status} {created.handoff_reason}")
            check(_has_tools(created, case.expect_tools), f"工具 {_names(created)}")
            check(_writes(resources, created.task_id) == case.expect_writes, "写入次数不为 0")
            if case.kind == "unauth":
                check(not _names(created), f"越权任务调用了工具 {_names(created)}")
            if case.target:
                actual = _field(resources, case.target)
                check(_same(actual, case.target[3]), f"{case.target} 实际 {actual}")
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    return {"id": case.id, "kind": case.kind, "ok": not failures, "detail": "；".join(failures)}


def _names(view: TaskView) -> list[str]:
    return [call["tool"] for call in view.tool_calls if call.get("ok")]


def _has_tools(view: TaskView, expected: tuple[str, ...]) -> bool:
    return set(expected) <= set(_names(view))


def _writes(resources, task_id: str) -> int:
    with resources.factory() as session:
        return count_executions(session, task_id)


def _field(resources, target: tuple):
    kind, object_id, field, _expected = target
    with resources.factory() as session:
        return read_business_field(session, kind, object_id, field)


def _task_count(resources, user_id: str, key: str) -> int:
    with resources.factory() as session:
        return int(
            session.scalar(
                select(func.count()).select_from(TaskRow).where(
                    TaskRow.user_id == user_id,
                    TaskRow.idempotency_key == key,
                )
            )
            or 0
        )


def _same(actual, expected) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(float(actual) - float(expected)) < 0.001
        except (TypeError, ValueError):
            return False
    return actual == expected


def _rate(items: list[dict]) -> float:
    if not items:
        return 0.0
    return round(sum(item["ok"] for item in items) / len(items), 4)


def main() -> None:
    report = asyncio.run(run_suite())
    print(json.dumps({key: value for key, value in report.items() if key != "failures"}, ensure_ascii=False, indent=2))
    if report["failures"]:
        print("失败用例:")
        for item in report["failures"]:
            print(f"  {item['id']} {item['detail']}")
    target = Path("data")
    target.mkdir(parents=True, exist_ok=True)
    (target / "eval_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["pass_rate"] < 0.85 or not report["unauthorized_blocked"] or not report["duplicate_safe"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

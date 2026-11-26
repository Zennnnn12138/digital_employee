from fastapi.testclient import TestClient
from fastmcp import Client

from app.config import Settings
from app.main import create_app
from app.models import Campaign
from app.tools.mcp_server import get_mcp


def _settings(tmp_path) -> Settings:
    database = (tmp_path / "app.db").resolve().as_posix()
    return Settings(
        database_url=f"sqlite:///{database}",
        checkpoint_path=str((tmp_path / "checkpoints.sqlite").resolve()),
        jwt_secret="flow-secret",
        llm_provider="rule",
        password_iterations=1000,
        redis_url="",
        langfuse_public_key="",
        langfuse_secret_key="",
    )


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_read_task_and_sse(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        token = _login(client, "lead", "Lead#2026")
        created = client.post(
            "/api/tasks",
            headers=_auth(token),
            json={"message": "核查店铺 SHOP-1001 最近7天广告消耗"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["status"] == "delivered"
        assert "2140.50" in body["delivery"]["summary"]
        listed = client.get("/api/tasks", headers=_auth(token))
        assert listed.status_code == 200
        assert any(item["task_id"] == body["task_id"] for item in listed.json()["items"])
        board = client.get("/api/workspace", headers=_auth(token))
        assert board.status_code == 200
        assert any(item["id"] == "C-501" for item in board.json()["campaigns"])
        events = client.get(f"/api/tasks/{body['task_id']}/events", headers=_auth(token))
        assert events.status_code == 200
        assert "任务解析" in events.text
        assert "event: status" in events.text


def test_maker_checker_and_budget_write(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        lead = _login(client, "lead", "Lead#2026")
        created = client.post(
            "/api/tasks",
            headers=_auth(lead),
            json={"message": "把店铺 SHOP-1001 广告计划 C-501 的日预算调整到 280，原因是放量测试"},
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]
        assert created.json()["status"] == "awaiting_approval"
        denied = client.post(
            f"/api/tasks/{task_id}/approvals",
            headers=_auth(lead),
            json={"decision": "approved", "note": "自己批"},
        )
        assert denied.status_code == 403
        auditor = _login(client, "auditor", "Audit#2026")
        approved = client.post(
            f"/api/tasks/{task_id}/approvals",
            headers=_auth(auditor),
            json={"decision": "approved", "note": "同意放量"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "delivered"
        again = client.post(
            f"/api/tasks/{task_id}/approvals",
            headers=_auth(auditor),
            json={"decision": "approved", "note": "再批一次"},
        )
        assert again.status_code == 409
        with client.app.state.resources.factory() as session:
            campaign = session.get(Campaign, "C-501")
            assert abs(campaign.daily_budget - 280) < 0.001


def test_stale_budget_is_not_written(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        lead = _login(client, "lead", "Lead#2026")
        created = client.post(
            "/api/tasks",
            headers=_auth(lead),
            json={"message": "把店铺 SHOP-1001 广告计划 C-501 的日预算调整到 280，原因是放量测试"},
        )
        task_id = created.json()["task_id"]
        with client.app.state.resources.factory() as session:
            campaign = session.get(Campaign, "C-501")
            campaign.daily_budget = 210
            session.commit()
        auditor = _login(client, "auditor", "Audit#2026")
        decided = client.post(
            f"/api/tasks/{task_id}/approvals",
            headers=_auth(auditor),
            json={"decision": "approved", "note": "对象已变化"},
        )
        assert decided.status_code == 200, decided.text
        body = decided.json()
        assert body["status"] == "handoff"
        assert "已变化" in body["handoff_reason"]
        with client.app.state.resources.factory() as session:
            campaign = session.get(Campaign, "C-501")
            assert abs(campaign.daily_budget - 210) < 0.001


def test_ads_operator_cannot_refund(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        token = _login(client, "ads_ops", "Ads#2026")
        created = client.post(
            "/api/tasks",
            headers=_auth(token),
            json={"message": "对店铺 SHOP-1001 订单 O-9003 执行退款 39.9 元，原因是尺码不合适"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["status"] == "handoff"
        assert body["tool_calls"] == []


def test_missing_shop_clarifies(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        token = _login(client, "lead", "Lead#2026")
        created = client.post(
            "/api/tasks",
            headers=_auth(token),
            json={"message": "帮我看看最近的广告消耗"},
        )
        body = created.json()
        assert body["status"] == "clarifying"
        assert "shop_id" in body["missing_fields"]


async def test_mcp_lists_ten_tools():
    async with Client(get_mcp()) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "query_ad_spend",
        "query_ad_campaign",
        "query_orders",
        "query_order_logistics",
        "query_inventory",
        "query_tickets",
        "search_policy",
        "adjust_ad_budget",
        "update_ticket",
        "simulate_refund",
    }


def test_health_and_index(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        health = client.get("/health")
        assert health.status_code == 200
        page = client.get("/")
        assert page.status_code == 200
        assert "数字员工" in page.text
        assert "模拟" in page.text

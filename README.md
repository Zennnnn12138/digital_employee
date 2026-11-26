# 跨境电商运营数字员工协作系统

按简历项目二做成的可运行原型：广告、履约、售后三类岗位，LangGraph 编排，FastMCP 工具，写操作人工审批后恢复，并用任务号加操作号防止重复写入。

业务库是测试种子，不连接真实广告账户或支付。退款只会改测试订单上的模拟退款金额。

## 本地启动

```powershell
cd digital_employee
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

打开 http://127.0.0.1:8000

| 账号 | 密码 | 能做什么 |
| --- | --- | --- |
| lead | Lead#2026 | 店铺 SHOP-1001 的广告、履约、售后 |
| auditor | Audit#2026 | 审批别人的写操作，不能批自己的单 |
| ads_ops | Ads#2026 | 只能查广告和调预算 |
| shop2_ops | Shop2#2026 | 只能处理 SHOP-1002 |

默认不用大模型，任务解析走规则，60 组回归可以重复。要接模型时复制 `.env.example` 为 `.env`，把 `LLM_PROVIDER` 设为 `deepseek` 或 `qwen` 并填密钥。模型结果必须和规则抽出的店铺、写动作一致，否则自动回退规则。

## 一条写操作怎么走

1. `lead` 提交「把 SHOP-1001 广告计划 C-501 的日预算调整到 280」。
2. 图先做只读核查，在 `approval_gate` 里 `interrupt`，接口状态变成 `awaiting_approval`。
3. 审批单带上对象、原预算、目标预算和风险。上调超过 50% 会写明政策阈值。
4. `auditor` 批准后，图从检查点恢复，再次核对当前预算和 `operation_id`，然后才写入。
5. 同一任务再批一次会返回 409。审批等待期间如果预算已被别人改掉，恢复时拒绝写入。

## 回归

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m app.eval.runner
```

60 组覆盖单岗、跨岗、越权、重复请求、审批通过和审批拒绝。越权任务不应调用工具，重复审批不应产生第二笔写入。

## Docker

```powershell
docker compose up --build
```

业务数据进 PostgreSQL，只读缓存进 Redis。图检查点放在单副本卷里的 SQLite。多副本部署要换成 PostgreSQL checkpointer，当前原型按单实例部署。

## 面试时可以对着讲的文件

- `app/graph/builder.py`：节点和条件边
- `app/graph/nodes.py`：`approval_gate` 与恢复后的 `execute`
- `app/tools/mcp_server.py`：FastMCP 工具面
- `app/tools/gateway.py`：参数、岗位权限、超时、只读缓存
- `app/services/tasks.py`：幂等键和审批占用
- `app/eval/cases.py`：60 组任务

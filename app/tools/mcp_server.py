"""FastMCP 服务。图里的岗位 Agent 通过 Client 调用这些工具，不直接改表。"""

from __future__ import annotations

from fastmcp import FastMCP

from app.tools.impl import TOOL_FUNCTIONS, TOOL_NAMES

_mcp: FastMCP | None = None


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        "cross-border-ops-tools",
        instructions="跨境运营工具：广告、订单、库存、工单和政策检索。写操作必须带 operation_id。",
    )
    for fn in TOOL_FUNCTIONS:
        public_name = fn.__name__.removesuffix("_tool")
        mcp.tool(fn, name=public_name, run_in_thread=False)
    return mcp


def get_mcp() -> FastMCP:
    global _mcp
    if _mcp is None:
        _mcp = build_mcp()
    return _mcp


def tool_names() -> list[str]:
    return list(TOOL_NAMES)


if __name__ == "__main__":
    get_mcp().run()

"""只读验证 CTFy MCP，不调用模型、不启动题目、不提交答案。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_workbench.config.settings import get_settings
from agent_workbench.execution.ctfy_mcp import CtfyMcpClient, CtfyMcpError, structured_result
from agent_workbench.platform_compat import configure_stdio_utf8


async def check() -> dict:
    settings = get_settings()
    if not settings.ctfy_mcp_enabled:
        raise CtfyMcpError("CTFy MCP 未启用，未发送请求")
    async with CtfyMcpClient(settings) as client:
        catalog = await client.list_tools()
        if not any(item["name"] == "list_competitions" for item in catalog):
            raise CtfyMcpError("CTFy MCP 未提供 list_competitions")
        response = await client.call_tool(
            "list_competitions", {"offset": 0, "limit": 5},
        )
        if response.get("isError"):
            raise CtfyMcpError("MCP list_competitions 返回错误；未执行其他动作")
        competitions = structured_result(response).get("result")
        if not isinstance(competitions, list):
            raise CtfyMcpError("MCP 比赛列表结构无效")
    return {
        "mcp_handshake": "PASS",
        "server_tools": len(catalog),
        "main_tools": len(catalog),
        "competitions_first_page": len(competitions),
        "agent_runtime": "NOT_TESTED",
        "kali_target_vpn": "NOT_TESTED",
    }


if __name__ == "__main__":
    configure_stdio_utf8()
    try:
        report = asyncio.run(check())
    except (CtfyMcpError, OSError, ValueError) as exc:
        # 失败诊断不输出异常链、配置值、凭据文件内容或供应商正文。
        print(json.dumps({"mcp_handshake": "FAIL", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))

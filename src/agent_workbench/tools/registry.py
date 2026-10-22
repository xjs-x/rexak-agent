"""工具集合装配。

按 §6.3 的原则：**按能力提供少量稳定入口**，避免同时注入大量相似扫描器 schema。
按任务启用能力裁剪；**不把工具数当能力指标**。

能力开关使模型能"看到可用与缺失的能力"，缺少程序时给出确定错误和替代能力，
而不是诱导它扫描整个文件系统找工具。

执行底座工具（命令/脚本/终端/监视）的开放条件（规格 §5.2）：
- 执行端声明了 `/execution/v1` 子协议；
- 显式 `EXECUTOR_EXECUTION_MODE=trusted`（发行缺省 disabled）；
- 解释器/平台/初始化预检成功（执行端 `/capabilities` 逐项报告）。
三个条件缺一不可；不满足时**不注入**对应工具，工具内还有二次核对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.tool import ExecutorCapabilities
from agent_workbench.tools.browser import BROWSER_TOOLS
from agent_workbench.tools.commands import COMMAND_TOOLS
from agent_workbench.tools.completion import COMPLETION_TOOLS
from agent_workbench.tools.evidence import EVIDENCE_TOOLS
from agent_workbench.tools.execution_resources import (
    ENVIRONMENT_TOOLS,
    EXECUTION_RESOURCE_TOOLS,
    EXECUTION_TEMPLATE_TOOLS,
)
from agent_workbench.tools.files import FILE_TOOLS
from agent_workbench.tools.history import search_history
from agent_workbench.tools.http import HTTP_TOOLS
from agent_workbench.tools.jobs import JOB_TOOLS
from agent_workbench.tools.knowledge import KNOWLEDGE_TOOLS
from agent_workbench.tools.skills import SKILL_TOOLS
from agent_workbench.tools.task import TASK_TOOLS
from agent_workbench.tools.terminals import TERMINAL_TOOLS
from agent_workbench.tools.verification import VERIFICATION_TOOLS


@dataclass(frozen=True, slots=True)
class ToolSelection:
    """一次 run 的工具选择。reason 用于把"为什么关掉某组"写进诊断。"""

    http: bool = True
    #: 浏览器工具。真实可用性由 Executor 的能力预检决定；不可用时执行岛会返回
    #: `browser_unavailable`，控制端据此给出确定失败，而不是假装成功。
    browser: bool = True
    #: 任务工作区文件工具。**不受**通用命令无人值守模式关闭的影响：
    #: 它不启动进程、不联网，只在本任务工作区内读写。
    files: bool = True
    evidence: bool = True
    jobs: bool = True
    task: bool = True
    knowledge: bool = True
    #: 用户显式发布的外部扩展，与宿主 knowledge/methods 独立。
    skills: bool = False
    completion: bool = True
    verification: bool = False
    #: 命令/脚本/监视（run_command / run_script / start_monitor）
    command: bool = True
    #: PTY 终端（shell_*）
    terminals: bool = True
    #: 环境发现（get_execution_environment）
    environment: bool = True
    #: 执行模板（list/get/register/run_execution_template）
    templates: bool = True
    disabled_reasons: dict[str, str] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "http": self.http,
            "browser": self.browser,
            "files": self.files,
            "evidence": self.evidence,
            "jobs": self.jobs,
            "task": self.task,
            "knowledge": self.knowledge,
            **({"skills": True} if self.skills else {}),
            "completion": self.completion,
            "verification": self.verification,
            "command": self.command,
            "terminals": self.terminals,
            "environment": self.environment,
            "templates": self.templates,
            "disabled_reasons": dict(self.disabled_reasons),
            "note": "工具数量不是能力指标；被关闭的能力会在诊断中说明原因。",
        }

    def disabled_tools(self) -> dict[str, str]:
        """逐工具名的拒绝原因（在派发前给出确定错误，而不是让调用落到不存在的能力上）。"""
        reasons: dict[str, str] = {}
        if not self.command:
            reason = self.disabled_reasons.get("command", "命令能力当前关闭")
            for name in ("run_command", "run_script", "start_monitor"):
                reasons[name] = reason
            if self.skills:
                reasons["run_skill_script"] = reason
        if not self.terminals:
            reason = self.disabled_reasons.get("terminals", "终端能力当前关闭")
            for name in (
                "shell_open",
                "shell_send",
                "shell_read",
                "shell_resize",
                "shell_list",
                "shell_close",
            ):
                reasons[name] = reason
        if not self.environment:
            reasons["get_execution_environment"] = self.disabled_reasons.get(
                "environment", "环境发现当前不可用"
            )
        if not self.templates:
            reason = self.disabled_reasons.get("templates", "执行模板当前关闭")
            for name in (
                "list_execution_templates",
                "get_execution_template",
                "register_execution_template",
                "run_execution_template",
            ):
                reasons[name] = reason
        return reasons


def default_selection(
    settings: Settings | None = None,
    *,
    capabilities: ExecutorCapabilities | None = None,
) -> ToolSelection:
    """按能力装配工具选择。

    `capabilities=None`（诊断/manifest 场景）时执行工具保持可见，
    由工具内的能力核对给出确定失败；运行时（Runner 预检之后）必须传入真实能力。
    """
    cfg = settings or get_settings()
    _ = cfg
    reasons: dict[str, str] = {
        "browser": "浏览器工具已实现；若执行岛没有可用浏览器，动作会返回 browser_unavailable。",
        "skills": "第一阶段最小闭环暂不启用 Skills。",
        "verification": "第一阶段专用结果 Verification 暂停启用；候选不自动确认。",
        "coverage": "第一阶段停止维护 Coverage 协议，仅保留历史读取。",
    }
    if capabilities is None:
        return ToolSelection(disabled_reasons=reasons)

    execution_declared = bool(capabilities.execution)
    trusted = capabilities.execution_mode == "trusted" and execution_declared
    command_ok = trusted and capabilities.execution_capable("command")
    pty_ok = trusted and capabilities.execution_capable("pty")

    if not execution_declared:
        reason = (
            "执行岛未声明 /execution/v1 执行子协议：命令/脚本/终端能力确定不可用；"
            "**不会**回退到控制端本机执行命令。"
        )
        reasons["command"] = reason
        reasons["terminals"] = reason
        reasons["environment"] = reason
        reasons["templates"] = reason
    elif not trusted:
        detail = capabilities.execution_reason("command") or (
            "EXECUTOR_EXECUTION_MODE 未设为 trusted（发行缺省 disabled）"
        )
        reason = f"执行底座未启用：{detail}"
        reasons["command"] = reason
        reasons["terminals"] = reason
        reasons["templates"] = reason
    else:
        if not command_ok:
            reasons["command"] = capabilities.execution_reason("command") or "命令能力预检未通过"
            reasons["templates"] = reasons["command"]
        if not pty_ok:
            reasons["terminals"] = capabilities.execution_reason("pty") or "PTY 能力不可用"

    return ToolSelection(
        command=command_ok,
        terminals=pty_ok,
        environment=execution_declared,
        templates=command_ok,
        disabled_reasons=reasons,
    )


def build_tools(selection: ToolSelection | None = None) -> list[Any]:
    """按选择装配工具列表。"""
    chosen = selection or default_selection()
    tools: list[Any] = []
    if chosen.http:
        tools.extend(HTTP_TOOLS)
    if chosen.files:
        tools.extend(FILE_TOOLS)
    if chosen.browser:
        tools.extend(BROWSER_TOOLS)
    if chosen.evidence:
        tools.extend(EVIDENCE_TOOLS)
        tools.append(search_history)
    if chosen.jobs:
        tools.extend(JOB_TOOLS)
        tools.extend(EXECUTION_RESOURCE_TOOLS)
    if chosen.command:
        tools.extend(COMMAND_TOOLS)
    if chosen.terminals:
        tools.extend(TERMINAL_TOOLS)
    if chosen.environment:
        tools.extend(ENVIRONMENT_TOOLS)
    if chosen.templates:
        tools.extend(EXECUTION_TEMPLATE_TOOLS)
    if chosen.task:
        tools.extend(TASK_TOOLS)
    if chosen.knowledge:
        tools.extend(KNOWLEDGE_TOOLS)
    if chosen.skills:
        tools.extend(
            item for item in SKILL_TOOLS if chosen.command or item.name != "run_skill_script"
        )
    if chosen.completion:
        tools.extend(COMPLETION_TOOLS)
    if chosen.verification:
        tools.extend(VERIFICATION_TOOLS)
    names = [getattr(item, "name", str(item)) for item in tools]
    if len(set(names)) != len(names):
        raise ValueError(f"工具名重复：{names}")
    return tools


__all__ = ["ToolSelection", "build_tools", "default_selection"]

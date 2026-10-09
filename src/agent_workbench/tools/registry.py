"""工具集合装配。

按 §6.3 的原则：**按能力提供少量稳定入口**，避免同时注入大量相似扫描器 schema。
按任务启用能力裁剪；**不把工具数当能力指标**。

能力开关使模型能"看到可用与缺失的能力"，缺少程序时给出确定错误和替代能力，
而不是诱导它扫描整个文件系统找工具。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.tools.completion import COMPLETION_TOOLS
from agent_workbench.tools.evidence import EVIDENCE_TOOLS
from agent_workbench.tools.files import FILE_TOOLS
from agent_workbench.tools.http import HTTP_TOOLS
from agent_workbench.tools.jobs import JOB_TOOLS
from agent_workbench.tools.knowledge import KNOWLEDGE_TOOLS
from agent_workbench.tools.task import TASK_TOOLS


@dataclass(frozen=True, slots=True)
class ToolSelection:
    """一次 run 的工具选择。reason 用于把"为什么关掉某组"写进诊断。"""

    http: bool = True
    #: 任务工作区文件工具。**不受**通用命令无人值守模式关闭的影响：
    #: 它不启动进程、不联网，只在本任务工作区内读写。
    files: bool = True
    evidence: bool = True
    jobs: bool = True
    task: bool = True
    knowledge: bool = True
    completion: bool = True
    disabled_reasons: dict[str, str] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "http": self.http,
            "files": self.files,
            "evidence": self.evidence,
            "jobs": self.jobs,
            "task": self.task,
            "knowledge": self.knowledge,
            "completion": self.completion,
            "disabled_reasons": dict(self.disabled_reasons),
            "note": "工具数量不是能力指标；被关闭的能力会在诊断中说明原因。",
        }


def default_selection(settings: Settings | None = None) -> ToolSelection:
    """首版默认：HTTP + 工件 + 后台任务 + 任务资料 + 方法/记忆 + 阶段结束。

    **不包含**浏览器与通用命令：
    - 浏览器在 P4 交付，Executor 会如实返回 501；
    - 通用命令的无人值守模式因缺少执行环境级网络隔离而关闭（Executor `/capabilities` 声明）。
    """
    cfg = settings or get_settings()
    _ = cfg
    return ToolSelection(
        disabled_reasons={
            "browser": "浏览器工具在 P4 交付；Executor 当前对 /browser-sessions 返回 501。",
            "command": (
                "通用命令的无人值守模式已关闭：执行岛 network_isolation=unavailable，"
                "缺乏执行环境级出站约束。正则黑名单/工作目录/提示词都不能替代网络隔离。"
            ),
        }
    )


def build_tools(selection: ToolSelection | None = None) -> list[Any]:
    """按选择装配工具列表。"""
    chosen = selection or default_selection()
    tools: list[Any] = []
    if chosen.http:
        tools.extend(HTTP_TOOLS)
    if chosen.files:
        tools.extend(FILE_TOOLS)
    if chosen.evidence:
        tools.extend(EVIDENCE_TOOLS)
    if chosen.jobs:
        tools.extend(JOB_TOOLS)
    if chosen.task:
        tools.extend(TASK_TOOLS)
    if chosen.knowledge:
        tools.extend(KNOWLEDGE_TOOLS)
    if chosen.completion:
        tools.extend(COMPLETION_TOOLS)
    names = [getattr(item, "name", str(item)) for item in tools]
    if len(set(names)) != len(names):
        raise ValueError(f"工具名重复：{names}")
    return tools


__all__ = ["ToolSelection", "build_tools", "default_selection"]

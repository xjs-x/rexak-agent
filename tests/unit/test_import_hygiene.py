"""导入卫生：防止协议枚举从错误的模块导入。

背景：`EventType` 定义在 `contracts/events.py`，不在 `contracts/enums.py`。
开发期间我**四次**写成 `from agent_workbench.contracts.enums import EventType`，
每次都只在运行时（pytest 收集阶段）才暴露。这个测试把它变成静态可查。

同时也固定几个容易搞混的归属：
- 状态机类枚举（RunStatus / CommandKind / ...）在 `contracts.enums`；
- 事件类型与信封在 `contracts.events`；
- 表定义在 `persistence.tables`，不在 `contracts`；
- 仓储在 `persistence.repositories`。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "executor/src", "tests", "scripts", "evals", "migrations")

#: 模块 -> 该模块**不**导出的名字
FORBIDDEN_IMPORTS = {
    "agent_workbench.contracts.enums": {"EventType", "EventEnvelope", "EVENT_SCHEMA_VERSION"},
    "agent_workbench.contracts.events": {
        "RunStatus",
        "CommandKind",
        "CompletionReason",
        "LeadStatus",
        "FindingStatus",
        "JobState",
        "ErrorCode",
    },
    "agent_workbench.config.settings": {"Database", "DatabaseSettings"},
}


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_DIRS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def _imported_names(path: Path) -> list[tuple[str, str, int]]:
    """返回 `(module, imported_name, lineno)`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                found.append((node.module, alias.name, node.lineno))
    return found


def test_no_forbidden_protocol_imports() -> None:
    offenders: list[str] = []
    for path in _iter_python_files():
        for module, name, lineno in _imported_names(path):
            forbidden = FORBIDDEN_IMPORTS.get(module)
            if forbidden and name in forbidden:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno} 从 {module} 导入 {name}")
    assert not offenders, "以下导入会失败（协议归属错误）：\n  " + "\n  ".join(offenders)


def test_contracts_exports_are_where_they_claim_to_be() -> None:
    """确认归属事实本身没变。"""
    from agent_workbench.contracts import enums, events

    assert hasattr(events, "EventType")
    assert hasattr(events, "EventEnvelope")
    assert not hasattr(enums, "EventType"), "EventType 不应出现在 contracts.enums"
    assert hasattr(enums, "RunStatus")


def test_all_modules_import_cleanly() -> None:
    """全部控制端与执行岛模块都能导入（捕捉循环导入与拼写错误）。"""
    import importlib

    modules = [
        "agent_workbench",
        "agent_workbench.api.app",
        "agent_workbench.agent.factory",
        "agent_workbench.cli",
        "agent_workbench.config.settings",
        "agent_workbench.contracts",
        "agent_workbench.contracts.enums",
        "agent_workbench.contracts.events",
        "agent_workbench.domain.assets",
        "agent_workbench.domain.findings",
        "agent_workbench.domain.leads",
        "agent_workbench.domain.observations",
        "agent_workbench.domain.run_state",
        "agent_workbench.domain.scope",
        "agent_workbench.evidence.artifacts",
        "agent_workbench.execution.client",
        "agent_workbench.execution.tunnel",
        "agent_workbench.memory.store",
        "agent_workbench.models.probe",
        "agent_workbench.models.provider",
        "agent_workbench.observability.costs",
        "agent_workbench.observability.events",
        "agent_workbench.persistence.bootstrap",
        "agent_workbench.persistence.repositories",
        "agent_workbench.persistence.tables",
        "agent_workbench.platform_compat",
        "agent_workbench.runtime.commands",
        "agent_workbench.runtime.recovery",
        "agent_workbench.runtime.runner",
        "agent_workbench.runtime.stopping",
        "agent_workbench.tools.registry",
        "agent_workbench.tools.runtime",
        "workbench_executor.api",
        "workbench_executor.http_sessions",
        "workbench_executor.ledger",
        "workbench_executor.policy",
        "workbench_executor.processes",
    ]
    failures: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("name", ["EventType", "RunStatus", "EventEnvelope"])
def test_protocol_types_importable_from_contracts_package(name: str) -> None:
    """协议类型也可以从 `agent_workbench.contracts` 顶层导入（推荐写法）。"""
    import agent_workbench.contracts as contracts

    assert hasattr(contracts, name), f"contracts 顶层应收敛导出 {name}"

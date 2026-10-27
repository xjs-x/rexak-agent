"""五个稳定 Skill 入口；模型不能安装包或把输出直接确认成漏洞。"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.skills import render_skills
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.skill_package import encode_package, safe_skill_path
from agent_workbench.observability.events import append_event
from agent_workbench.skills.service import SkillService
from agent_workbench.tools.commands import (
    bounded_wait_ms,
    common_execution_arguments,
    run_execution_kernel,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _active(runtime: ToolRuntime[RunContext], skill_id: str) -> dict[str, Any]:
    ref = (runtime.state.get("active_skills") or {}).get(skill_id)
    if not ref:
        raise ValueError("skill_not_active: 先在上一轮加载技能")
    return ref


async def _event(runtime: ToolRuntime[RunContext], action: str, data: dict[str, Any]) -> None:
    box = runtime.context.toolbox
    async with box.db.transaction() as db:
        await append_event(db, session_id=box.session_id, run_id=box.run_id,
            event_type=EventType.SKILL_ACTIVITY, payload={"action": action, **data})


@tool
async def search_skills(runtime: ToolRuntime[RunContext], query: str = "", cursor: int = 0, limit: int = 10) -> str:
    """搜索本运行固定的 Skill 目录。query 为空列目录；支持中文子串与稳定分页。"""
    box = runtime.context.toolbox
    if box.skill_catalog is None:
        box.skill_catalog = await SkillService(box.db).catalog(box.run_id)
    terms = query.casefold().split()
    rows = [item for item in box.skill_catalog if all(word in f"{item['name']} {item['description']}".casefold() for word in terms)]
    start, count = max(0, cursor), max(1, min(limit, 30))
    return _json({"skills": rows[start:start + count], "total": len(rows),
                  "next_cursor": start + count if start + count < len(rows) else None})


@tool
async def load_skill(runtime: ToolRuntime[RunContext], skill_id: str, version_id: str | None = None) -> Command:
    """加载一个 Skill 的固定版本，不执行脚本。下一轮模型请求将完整呈现正文。"""
    box = runtime.context.toolbox
    if box.skill_catalog is None:
        box.skill_catalog = await SkillService(box.db).catalog(box.run_id)
    item = next((item for item in box.skill_catalog if item["skill_id"] == skill_id), None)
    old = (runtime.state.get("active_skills") or {}).get(skill_id)
    if item is None and not old:
        raise ValueError("skill_not_found: 当前运行目录没有此技能，请 search_skills")
    selected = old or item
    assert selected is not None
    version = version_id or selected["version_id"]
    service = SkillService(box.db)
    card, files, _spec = await service.session_files(box.session_id, skill_id, version)
    ref = {"skill_id": skill_id, "version_id": card.version_id, "package_hash": card.package_hash, "source": "model"}
    await render_skills(service, box.skill_catalog,
        {**(runtime.state.get("active_skills") or {}), skill_id: ref}, box.settings, session_id=box.session_id)
    await _event(runtime, "loaded", ref)
    return Command(update={"active_skills": {skill_id: ref}, "messages": [ToolMessage(
        content=_json({"loaded": ref, "files": sorted(files)[:30], "files_total": len(files), "diagnostics": card.diagnostics,
                       "note": "正文下一轮完整呈现；脚本尚未执行"}),
        tool_call_id=runtime.tool_call_id or "", name="load_skill")]})


@tool
async def unload_skill(runtime: ToolRuntime[RunContext], skill_id: str) -> Command:
    """卸载后续请求的 Skill 指导；不终止已有后台进程，不删除历史。"""
    await _event(runtime, "unloaded", {"skill_id": skill_id})
    return Command(update={"active_skills": {skill_id: None}, "messages": [ToolMessage(
        content="技能指导已卸载；已有资源状态仍以执行台账为准。",
        tool_call_id=runtime.tool_call_id or "", name="unload_skill")]})


@tool
async def read_skill_resource(runtime: ToolRuntime[RunContext], skill_id: str, path: str,
                             offset: int = 0, limit: int = 8000, files_offset: int = 0) -> str:
    """按相对路径读取活跃 Skill 的参考资料。返回片段、哈希和下一页；不读任意主机文件。"""
    box = runtime.context.toolbox
    ref = _active(runtime, skill_id)
    _card, files, _spec = await SkillService(box.db).session_files(box.session_id, skill_id, ref["version_id"])
    result = SkillService.resource(files, path, max(0, offset), max(1, min(limit, 16000)), max(0, files_offset))
    await _event(runtime, "resource_read", {**ref, "path": path, "sha256": result.sha256, "offset": offset})
    return result.model_dump_json()


@tool
async def run_skill_script(runtime: ToolRuntime[RunContext], skill_id: str, path: str,
                           argv: list[str] | None = None, stdin_json: str | None = None,
                           background: bool = False, wait_ms: int = 10000,
                           execution_timeout_seconds: int | None = None) -> str:
    """在执行岛运行活跃版本内的脚本或入口别名。参数用 argv/JSON stdin；不会在控制端执行。"""
    box = runtime.context.toolbox
    ref = _active(runtime, skill_id)
    card, files, spec = await SkillService(box.db).session_files(box.session_id, skill_id, ref["version_id"])
    if card.package_hash != ref["package_hash"]:
        raise ValueError("skill_hash_mismatch")
    entry = (spec.get("runtime", {}).get("entrypoints") or {}).get(path, {})
    path = entry.get("path", path)
    safe_skill_path(path)
    if path not in files or not path.startswith("scripts/"):
        raise ValueError("skill_path_denied: 只能执行包中 scripts/ 下的脚本")
    interpreter = entry.get("interpreter") or {"py": "python", "sh": "shell", "js": "node"}.get(path.rsplit(".", 1)[-1])
    if not interpreter:
        raise ValueError("skill_extension_unsupported: 脚本解释器不支持")
    if stdin_json is not None:
        json.loads(stdin_json)
    await box.assert_control_current()
    caps = await box.executor.capabilities()
    if caps.execution_mode != "trusted" or not caps.execution_capable("command"):
        raise ValueError("skill_dependency_unavailable: 执行岛 trusted 命令能力未启用")
    if not caps.execution_capable("skill_packages"):
        raise ValueError("skill_dependency_unavailable: 执行岛尚未升级 Skill 包能力")
    required = (spec.get("runtime", {}).get("requires") or {}).get("capabilities", [])
    missing = [name for name in required if not caps.execution_capable(name)]
    if missing:
        raise ValueError(f"skill_dependency_unavailable: 执行岛缺少声明能力 {missing}")
    key = hashlib.sha256(f"{box.run_id}:{runtime.tool_call_id}".encode()).hexdigest()[:32]
    payload = {
        "session_id": str(box.session_id), "package_hash": card.package_hash,
        "output_key": key,
        "interpreter": interpreter,
        "required_binaries": (spec.get("runtime", {}).get("requires") or {}).get("binaries", []),
    }
    receipt = await box.executor.execution_skill_package(payload)
    if receipt.get("package_required"):
        receipt = await box.executor.execution_skill_package({**payload,
            "archive_base64": base64.b64encode(encode_package(files)).decode()})
    await _event(runtime, "execution_requested", {**ref, "path": path,
        "tool_call_id": runtime.tool_call_id, "output_relative": receipt["output_relative"]})
    arguments = common_execution_arguments(cwd=receipt["output_relative"],
        env={"PYTHONDONTWRITEBYTECODE": "1", "SKILL_DIR": receipt["skill_dir"], "SKILL_OUTPUT_DIR": receipt["output_dir"],
             "WORKBENCH_WORKSPACE": receipt["workspace"]}, stdin_text=stdin_json,
        execution_timeout_seconds=execution_timeout_seconds, output_limit_action="preserve_process",
        description=f"Skill {card.name}@{card.package_hash[:12]}: {path}", workspace_id=str(box.session_id),
        extra={"argv": [receipt["interpreter"], receipt["skill_dir"] + "/" + path, *(argv or [])],
               "skill_ref": {"skill_id": skill_id, "version_id": card.version_id,
                             "package_hash": card.package_hash, "script": path}})
    result = await run_execution_kernel(box, tool_call_id=runtime.tool_call_id or "",
        tool_name="run_skill_script", capability="command", action="run_command", arguments=arguments,
        wait_ms=0 if background else bounded_wait_ms(wait_ms))
    result.data.update({"skill_ref": ref, "skill_output_dir": receipt["output_relative"],
        "output_note": "标准输出用 read_output；文件产物用现有 read_file 按工作区相对路径读取并保存原件。"})
    return result.model_dump_json()


SKILL_TOOLS = [search_skills, load_skill, read_skill_resource, unload_skill, run_skill_script]

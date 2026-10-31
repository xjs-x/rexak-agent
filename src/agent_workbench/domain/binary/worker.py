"""一次性静态解析 worker；无数据库、模型或执行岛访问。"""

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from agent_workbench.domain.binary import disasm, elf, pe
from agent_workbench.domain.binary.detect import detect
from agent_workbench.domain.binary.model import ANALYZER, ANALYZER_VERSION
from agent_workbench.domain.binary.strings import extract


def analyze(path: Path, extension: str, params: dict[str, Any]) -> dict[str, Any]:
    if path.stat().st_size > params["max_bytes"]:
        raise ValueError("binary_input_limit")
    raw = path.read_bytes()
    identity = detect(raw, extension)
    result: dict[str, Any] = {
        "status": "unsupported",
        "profile": {
            **identity,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "analyzer": ANALYZER,
            "analyzer_version": ANALYZER_VERSION,
        },
    }
    if identity["family"] not in {"pe", "elf"}:
        result["profile"]["limitations"] = ["该真实格式尚未实现结构或函数分析。"]
        return result
    result["profile"].update((pe if identity["family"] == "pe" else elf).parse(raw))
    if params["kind"] not in {"base", "strings"}:
        profile = result["profile"]
        if profile.get("managed_dotnet"):
            return {
                "status": "unsupported",
                "profile": profile,
                "note": ".NET 托管程序深入分析未实现。",
            }
        if params["kind"] == "code":
            functions = disasm.function_index(raw, profile)
            result.update(
                status="ready",
                **functions,
                **disasm.reference_index(raw, profile, functions["functions"]),
            )
        elif params["kind"] == "function":
            result = disasm.disassemble(raw, profile, params["function"])
        else:
            raise ValueError("invalid_analysis_kind")
        for key, flag in (
            ("references", "references_truncated"),
            ("instructions", "disassembly_truncated"),
        ):
            while (
                len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > params["output_bytes"]
            ):
                if not result.get(key):
                    break
                del result[key][max(0, len(result[key]) - max(1, len(result[key]) // 10)) :]
                result[flag] = True
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > params["output_bytes"]:
            raise ValueError("binary_index_limit")
        return result
    strings, stats = extract(
        raw, result["profile"]["sections"], params["max_strings"], params.get("filters")
    )
    result.update(status="ready", strings=strings)
    result["profile"]["strings"] = stats
    for label, term in (("ImGui", "imgui"), ("C++ RTTI", ".?av")):
        if any(term in s[1].lower() for s in strings):
            result["profile"]["technology_hints"].append(
                {"feature": label, "basis": "indexed_string", "heuristic": True}
            )
    # 限制中间产物，不生成任意大的持久索引。
    budget = params["output_bytes"]
    while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > budget:
        if not strings:
            raise ValueError("binary_index_limit")
        del strings[max(0, len(strings) - max(1, len(strings) // 10)) :]
        stats.update(indexed=len(strings), truncated=True)
    return result


def restrict(output: Path) -> None:
    """附加 Python 审计限制；不宣称原生扩展无法绕过它。"""
    folder = output.parent.resolve()

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if (
            event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn"))
            or event == "os.system"
        ):
            raise PermissionError("analysis_network_and_execution_disabled")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            mode, flags = args[1:3]
            writing = isinstance(mode, str) and any(c in mode for c in "wax+")
            writing = writing or bool(isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR))
            if writing and not Path(os.fsdecode(args[0])).resolve().is_relative_to(folder):
                raise PermissionError("analysis_write_outside_output_disabled")

    sys.addaudithook(audit)


def main() -> None:
    path, output = Path(sys.argv[1]), Path(sys.argv[2])
    extension, params = sys.argv[3], json.loads(sys.argv[4])
    restrict(output)
    try:
        result = analyze(path, extension, params)
    except Exception as exc:
        # worker 是不可信输入的错误边界：不回传正文、路径或库异常消息。
        result = {"status": "failed", "error_type": type(exc).__name__}
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()

"""生成执行端协议副本（`workbench_executor/_generated/contracts/`）。

生成链（规格 §5.1）：
- 真源在 `src/agent_workbench/contracts/`，纯 Pydantic 模块只依赖标准库与 pydantic；
- 本脚本把纯协议模块及其依赖闭包复制到执行端，仅改写包内导入前缀；
- 生成物带源哈希头部，**禁止手改**；
- `--check` 比较生成结果并对漂移失败（用于 CI 与验收）。

用法：
    uv run python scripts/generate_executor_contracts.py
    uv run python scripts/generate_executor_contracts.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "agent_workbench"
TARGET_PACKAGE = ROOT / "executor" / "src" / "workbench_executor" / "_generated"

#: 需要复制到执行端的纯协议模块（其依赖闭包必须同样保持纯净）。
SOURCES = ["contracts/execution_runtime.py", "contracts/skill_package.py"]

#: 导入前缀改写规则。源模块当前自包含、无需改写；保留规则以便将来拆分时保持生成链可用。
IMPORT_REWRITES = [
    ("agent_workbench.contracts.", "workbench_executor._generated.contracts."),
    ("agent_workbench.", "workbench_executor._generated."),
]

HEADER_TEMPLATE = """# 本文件由 scripts/generate_executor_contracts.py 自动生成，禁止手改。
# 源文件：{source}
# 源 SHA256：{sha256}
# 重新生成：uv run python scripts/generate_executor_contracts.py
"""

_FORBIDDEN_IMPORT = re.compile(r"^\s*(?:from|import)\s+(agent_workbench)\b")


def _read_source(relative: str) -> tuple[str, str]:
    path = SOURCE_ROOT / relative
    content = path.read_text(encoding="utf-8")
    # 闭包纯净性检查：纯协议模块必须自包含（当前 SOURCES 全部自包含）。
    # 若将来拆分出互相引用的协议模块，需要先把它们全部列入 SOURCES 再放宽此检查。
    for line in content.splitlines():
        if _FORBIDDEN_IMPORT.match(line):
            raise SystemExit(
                f"纯协议模块不得导入控制端包内模块（生成链无法保证闭包完整）："
                f"{relative}: {line.strip()}"
            )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return content, digest


def _rewrite(content: str) -> str:
    for old, new in IMPORT_REWRITES:
        content = content.replace(old, new)
    return content


def generate() -> dict[str, str]:
    """返回 {相对路径: 文件内容}。"""
    outputs: dict[str, str] = {
        "_generated/__init__.py": (
            "# 本目录由 scripts/generate_executor_contracts.py 生成，禁止手改。\n"
        ),
        "_generated/contracts/__init__.py": (
            "# 本目录由 scripts/generate_executor_contracts.py 生成，禁止手改。\n"
        ),
    }
    for relative in SOURCES:
        content, digest = _read_source(relative)
        header = HEADER_TEMPLATE.format(source=relative, sha256=digest)
        rewritten = _rewrite(content)
        outputs[f"_generated/contracts/{Path(relative).name}"] = header + rewritten
    return outputs


def write_outputs(outputs: dict[str, str]) -> int:
    written = 0
    for relative, content in outputs.items():
        path = TARGET_PACKAGE.parent / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        path.write_text(content, encoding="utf-8", newline="\n")
        written += 1
    return written


def check_outputs(outputs: dict[str, str]) -> list[str]:
    drift: list[str] = []
    for relative, content in outputs.items():
        path = TARGET_PACKAGE.parent / relative
        if not path.exists():
            drift.append(f"缺失：{relative}")
            continue
        if path.read_text(encoding="utf-8") != content:
            drift.append(f"与真源不一致：{relative}")
    # 多余文件也视为漂移（防止手工塞入未生成的内容）。
    expected = {str((TARGET_PACKAGE.parent / relative).resolve()) for relative in outputs}
    if TARGET_PACKAGE.exists():
        for path in sorted(TARGET_PACKAGE.rglob("*.py")):
            if str(path.resolve()) not in expected:
                drift.append(f"生成目录存在未登记文件：{path.relative_to(ROOT)}")
    return drift


def _lint_generated(outputs: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for relative, content in outputs.items():
        if "agent_workbench" in content:
            problems.append(f"生成物仍引用控制端包名：{relative}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/校验执行端协议副本")
    parser.add_argument("--check", action="store_true", help="只校验漂移，不写入")
    args = parser.parse_args()

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    outputs = generate()
    problems = _lint_generated(outputs)
    if problems:
        for item in problems:
            print(f"[FAIL] {item}")
        return 1

    if args.check:
        drift = check_outputs(outputs)
        if drift:
            for item in drift:
                print(f"[DRIFT] {item}")
            print("协议副本与真源不一致：请运行 uv run python scripts/generate_executor_contracts.py")
            return 1
        print("执行端协议副本与真源一致。")
        return 0

    written = write_outputs(outputs)
    print(f"已生成执行端协议副本：{len(outputs)} 个文件，本次写入 {written} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

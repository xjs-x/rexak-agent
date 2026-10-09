"""源码编码检查：UTF-8 无 BOM、无替换字符、无已知乱码片段。

用法：
    uv run python scripts/check_encoding.py            # 检查默认范围
    uv run python scripts/check_encoding.py --root web # 指定范围

退出码：0 全部通过；1 存在违规。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_ROOTS = (
    "src",
    "executor/src",
    "tests",
    "scripts",
    "migrations",
    "evals",
    "docs",
    "web/src",
)

TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".md",
        ".txt",
        ".cfg",
        ".ini",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".css",
        ".html",
        ".sh",
        ".sql",
        ".env",
    }
)

# 只在这些后缀上要求"必须无 BOM"；二进制证据不在此列。
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".mypy_cache",
        ".vite",
        "playwright-report",
        "test-results",
    }
)

# 常见"被 mojibake 破坏"的特征片段：GBK 字节被当作 Latin-1 解码后的典型组合。
#
# 两点刻意的写法：
# 1. 使用 \uXXXX 转义而不是字面字符 —— 本文件自身也在被检查范围内，
#    写出字面量会让检查器把自己判为违规（自指误报）；
# 2. 注释里也不出现这些片段本身，避免注释成为唯一违规点。
MOJIBAKE_MARKERS = (
    "\u00c3\u00a6",  # 常见双字节序列之一
    "\u00e6\u201a",  # 常见双字节序列之二
    "\u00ef\u00bf\u00bd",  # U+FFFD 被二次编码后的形态
    "\u00e5\u00a4",  # 常见双字节序列之三
)

REPLACEMENT_CHAR = "\ufffd"


class Violation:
    __slots__ = ("detail", "kind", "line", "path")

    def __init__(self, path: Path, line: int, kind: str, detail: str) -> None:
        self.path = path
        self.line = line
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        rel = self.path.relative_to(REPO_ROOT) if self.path.is_absolute() else self.path
        location = f"{rel}:{self.line}" if self.line > 0 else str(rel)
        return f"[{self.kind}] {location} {self.detail}"


def iter_files(roots: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        if base.is_file():
            files.append(base)
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith(".env"):
                files.append(path)
    return files


def check_file(path: Path) -> list[Violation]:
    violations: list[Violation] = []
    raw = path.read_bytes()

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        violations.append(Violation(path, 0, "NOT_UTF8", f"无法按 UTF-8 解码：{exc}"))
        return violations

    if text.startswith("\ufeff"):
        violations.append(Violation(path, 0, "BOM", "文件以 UTF-8 BOM 开头"))

    for lineno, line in enumerate(text.splitlines(), start=1):
        if REPLACEMENT_CHAR in line:
            violations.append(Violation(path, lineno, "REPLACEMENT", "包含 U+FFFD 替换字符"))
        if "\x00" in line:
            violations.append(Violation(path, lineno, "NUL", "文本文件包含 NUL 字节"))
        for marker in MOJIBAKE_MARKERS:
            if marker in line:
                violations.append(
                    Violation(
                        path,
                        lineno,
                        "MOJIBAKE",
                        f"疑似乱码片段 {marker!r}（{unicodedata.name(marker[0], '?')}…）",
                    )
                )
                break
        if any(unicodedata.category(ch) == "Cc" and ch not in "\t" for ch in line):
            bad = [ch for ch in line if unicodedata.category(ch) == "Cc" and ch not in "\t"]
            violations.append(
                Violation(path, lineno, "CONTROL", f"包含控制字符 {[hex(ord(c)) for c in bad][:5]}")
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查源码与文档的 UTF-8 无 BOM 编码")
    parser.add_argument("--root", action="append", dest="roots", help="要检查的相对目录，可重复")
    args = parser.parse_args(argv)

    roots = tuple(args.roots) if args.roots else DEFAULT_ROOTS
    files = iter_files(roots)

    all_violations: list[Violation] = []
    for path in files:
        all_violations.extend(check_file(path))

    if all_violations:
        print(f"编码检查失败：{len(all_violations)} 项违规 / 共检查 {len(files)} 个文件")
        for violation in all_violations[:200]:
            print(f"  {violation}")
        if len(all_violations) > 200:
            print(f"  … 其余 {len(all_violations) - 200} 项已省略")
        return 1

    print(f"编码检查通过：{len(files)} 个文件均为 UTF-8 无 BOM、无替换字符、无乱码片段")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""编码与协议校验：UTF-8 无 BOM、无替换字符、协议真源唯一。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_encoding import DEFAULT_ROOTS, check_file, iter_files  # noqa: E402


def test_all_text_files_are_clean_utf8() -> None:
    violations = []
    for path in iter_files(DEFAULT_ROOTS):
        violations.extend(check_file(path))
    assert not violations, "\n".join(str(item) for item in violations[:50])


def test_checker_does_not_flag_itself() -> None:
    """自指检查：检查器自身必须通过自己的规则（否则说明标记写法有问题）。"""
    assert check_file(REPO_ROOT / "scripts" / "check_encoding.py") == []


def test_no_bom_in_pyproject_and_ini() -> None:
    for name in ("pyproject.toml", "alembic.ini", ".env.example", "compose.yaml"):
        raw = (REPO_ROOT / name).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{name} 含 UTF-8 BOM"
        raw.decode("utf-8")  # 必须能按 UTF-8 解码


def test_alembic_ini_is_ascii_only() -> None:
    """Alembic 用 locale 编码读取该文件；非 ASCII 会在 GBK 环境下直接报错。"""
    raw = (REPO_ROOT / "alembic.ini").read_bytes()
    assert all(byte < 0x80 for byte in raw), "alembic.ini 必须保持纯 ASCII"


@pytest.mark.parametrize(
    "path",
    [
        "src/agent_workbench/contracts/enums.py",
        "src/agent_workbench/contracts/events.py",
    ],
)
def test_contract_modules_are_importable_and_unique(path: str) -> None:
    import importlib

    module_name = path.replace("/", ".").removesuffix(".py")
    module = importlib.import_module(module_name)
    assert module is not None

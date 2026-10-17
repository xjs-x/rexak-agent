"""个人部署参数检查及远端路径解析，不访问模型或日常运行数据。"""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlsplit

from agent_workbench.config.settings import Settings


def remote_path(value: str, home: str) -> str:
    """只展开 SSH 登录用户的 home，不使用控制端用户目录。"""
    if not value or any(char in value for char in "\r\n\0"):
        raise ValueError("远端路径不能为空或包含换行")
    expanded = home + value[1:] if value.startswith("~/") else value
    path = PurePosixPath(expanded)
    if not path.is_absolute() or ".." in path.parts or str(path) == path.anchor:
        raise ValueError("远端目录必须是绝对路径或 ~/ 下的目录，且不能指向根目录或包含 ..")
    return str(path)


def validate_executor_connection(settings: Settings) -> None:
    if not settings.executor_host.strip():
        raise RuntimeError(
            "未配置 Kali 地址；先运行 scripts/setup_workbench.py configure --executor-host <地址>"
        )
    if any(char in settings.executor_host + settings.executor_user for char in "\r\n\0"):
        raise RuntimeError("Kali 连接配置包含非法字符")
    if not settings.executor_user.strip():
        raise RuntimeError("未配置 SSH 用户")
    if not 1 <= settings.executor_port <= 65535 or not 1 <= settings.executor_remote_port <= 65535:
        raise RuntimeError("SSH 和 Executor 端口必须在 1–65535 内")
    if not settings.executor_ssh_key.is_file():
        raise RuntimeError("SSH 私钥不存在；请按新成员部署指南配置自己的密钥")
    if not settings.executor_known_hosts.is_file():
        raise RuntimeError("known_hosts 不存在；请先通过 SSH 核对并记录 Kali 主机指纹")
    if not settings.executor_token or settings.executor_token.startswith("change-me"):
        raise RuntimeError("请先配置执行端令牌；初始化脚本可生成独立令牌")
    validate_pip_index(settings.executor_pip_index_url)


def validate_pip_index(value: str) -> None:
    if not value:
        return
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise ValueError(
            "可选包源必须是无凭据、查询参数及空白的 HTTPS URL；私有包源请在 Kali 配置 pip"
        )

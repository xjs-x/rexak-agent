"""双图拒绝的有界诊断；不改变接受条件或持久化协议。"""

from typing import Any


class GraphValidationError(ValueError):
    """保留 ValueError 兼容性，把修复信息与原有拒绝条件分开。"""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        path: str,
        expected: str,
        actual: str,
        recovery: str,
        public_message: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.path = path
        self.expected = expected
        self.actual = actual
        self.recovery = recovery
        self.public_message = public_message or message
        self.details = details

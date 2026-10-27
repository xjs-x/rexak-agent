"""TSecBench SDK 的窄封装。

设计要点：
- **惰性导入** SDK，缺包时给出安装指令，而不是让整个 CLI 无法启动；
- 只暴露单题流程需要的 5 个方法，不复制 SDK 的重试/解析逻辑；
- 把 SDK 异常翻译成少量领域异常，避免上层 ``except`` 依赖第三方类名；
- 任何错误消息都不包含令牌（SDK 只回平台 code/message，不回请求头）。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any, Protocol

from agent_workbench.tsecbench.config import BenchmarkConfig


class PlatformError(RuntimeError):
    """平台调用失败的基类。"""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class PlatformUnavailable(PlatformError):
    """SDK 未安装或网络层无法到达平台。"""


class VpnUnavailable(PlatformError):
    """平台 VPN 预检未通过；**不能**假定容器地址可达。"""


class ChallengeNotFound(PlatformError):
    """``unique_code`` 不在本任务的题目集中。"""


class ChallengeConflict(PlatformError):
    """平台状态冲突：任务已结束、已有活跃容器、题目不允许当前操作。"""


class ResourceBusy(PlatformError):
    """平台暂时无法分配容器资源；可稍后重试。"""


@dataclass(frozen=True, slots=True)
class VpnInfo:
    status: str
    client_ip: str | None
    checked_at: str | None
    ok: bool


@dataclass(frozen=True, slots=True)
class ChallengeInfo:
    unique_code: str
    description: str
    difficulty: str
    level: str
    total_score: int
    flag_count: int
    correct_flag_count: int
    is_completed: bool
    container_status: str
    container_addr: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StartInfo:
    unique_code: str
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubmitInfo:
    correct: bool
    awarded: int
    cumulative_score: int
    correct_flag_count: int
    total_flag_count: int
    matched_flag_index: int | None
    duplicate: bool = False


class PlatformClient(Protocol):
    """流程层依赖的最小平台接口（测试用假实现即可满足）。"""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def check_vpn(self) -> VpnInfo: ...

    async def list_challenges(self) -> list[ChallengeInfo]: ...

    async def start_challenge(self, unique_code: str) -> StartInfo: ...

    async def submit_flag(self, unique_code: str, flag: str) -> SubmitInfo: ...

    async def close_challenge(self, unique_code: str) -> bool: ...


_sdk_module: ModuleType | None = None


def _sdk() -> ModuleType:
    global _sdk_module
    if _sdk_module is not None:
        return _sdk_module
    try:
        import tsec_benchmark
    except ImportError as exc:  # pragma: no cover - 只在缺依赖时触发
        raise PlatformUnavailable(
            "未安装 tsec-benchmark（评测适配器是可选依赖，基础启动不安装它）。"
            "请用 `uv run --extra benchmark workbench tsec ...` 按需启动，"
            "或先执行 `uv sync --extra benchmark`。"
        ) from exc
    _sdk_module = tsec_benchmark
    return tsec_benchmark


class SdkPlatformClient:
    """``TSecBenchmarkAsync`` 的薄适配。

    ``auto_check_vpn=True`` 时，``open()`` 会先做 VPN 预检；预检失败直接抛
    :class:`VpnUnavailable`，不会发起任何平台业务请求。
    """

    def __init__(self, config: BenchmarkConfig, *, auto_check_vpn: bool = True) -> None:
        self._config = config
        self._auto_check_vpn = auto_check_vpn
        self._client: Any = None

    async def open(self) -> None:
        if self._client is not None:
            return
        config = self._config
        if not config.token or not config.base_url:
            config.require_ready()
        sdk = _sdk()
        client = sdk.TSecBenchmarkAsync(
            base_url=config.base_url,
            token=config.token,
            timeout=config.api_timeout,
            auto_check_vpn=self._auto_check_vpn,
        )
        try:
            await client.__aenter__()
        except sdk.VpnCheckError as exc:
            await _safe_aclose(client)
            raise VpnUnavailable(_vpn_message(exc)) from exc
        except sdk.TSecConnectionError as exc:
            await _safe_aclose(client)
            raise PlatformUnavailable(
                f"无法连接评测平台 {config.base_url}：{exc}。"
                "请确认平台地址、网络与代理设置；本适配器不使用系统代理。"
            ) from exc
        except sdk.TSecError as exc:
            await _safe_aclose(client)
            raise PlatformError(f"平台预检失败：{exc}", code=exc.code) from exc
        self._client = client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await _safe_aclose(client)

    async def __aenter__(self) -> SdkPlatformClient:
        await self.open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    @property
    def _opened(self) -> Any:
        if self._client is None:
            raise PlatformError("平台客户端尚未打开；请先调用 open()")
        return self._client

    async def check_vpn(self) -> VpnInfo:
        sdk = _sdk()
        try:
            result = await self._opened.check_vpn()
        except sdk.VpnCheckError as exc:
            raise VpnUnavailable(_vpn_message(exc)) from exc
        except sdk.TSecConnectionError as exc:
            raise PlatformUnavailable(f"VPN 预检网络错误：{exc}") from exc
        return VpnInfo(
            status=str(result.status),
            client_ip=result.client_ip,
            checked_at=result.time,
            ok=bool(result.ok),
        )

    async def list_challenges(self) -> list[ChallengeInfo]:
        sdk = _sdk()
        try:
            rows = await self._opened.list_challenges()
        except sdk.TaskNotFound as exc:
            raise PlatformError(
                "平台不认识当前 BENCHMARK_TOKEN（task_not_found）。"
                "请确认跑分任务未结束、令牌未过期。",
                code=exc.code,
            ) from exc
        except sdk.TSecConnectionError as exc:
            raise PlatformUnavailable(f"获取题目列表时网络错误：{exc}") from exc
        except sdk.TSecError as exc:
            raise PlatformError(f"获取题目列表失败：{exc}", code=exc.code) from exc

        return [
            ChallengeInfo(
                unique_code=str(row.unique_code),
                description=str(row.description or ""),
                difficulty=str(row.difficulty),
                level=str(row.level),
                total_score=int(row.total_score),
                flag_count=int(row.flag_count),
                correct_flag_count=int(row.correct_flag_count),
                is_completed=bool(row.is_completed),
                container_status=str(row.container_status),
                container_addr=tuple(str(item) for item in row.container_addr),
            )
            for row in rows
        ]

    async def start_challenge(self, unique_code: str) -> StartInfo:
        sdk = _sdk()
        try:
            result = await self._opened.start_challenge(unique_code)
        except sdk.ChallengeNotFound as exc:
            raise ChallengeNotFound(f"平台没有题目 {unique_code}", code=exc.code) from exc
        except sdk.InvalidState as exc:
            raise _invalid_state(unique_code, exc) from exc
        except sdk.ResourceUnavailable as exc:
            raise ResourceBusy(
                "平台暂时无法分配容器资源（resource_unavailable）。请稍后重试；"
                "若持续失败，检查平台任务是否仍在进行。",
                code=exc.code,
            ) from exc
        except sdk.TSecConnectionError as exc:
            raise PlatformUnavailable(f"启动题目容器时网络错误：{exc}") from exc
        except sdk.TSecError as exc:
            raise PlatformError(f"启动题目容器失败：{exc}", code=exc.code) from exc
        return StartInfo(
            unique_code=str(result.unique_code),
            addresses=tuple(str(item) for item in result.container_addr),
        )

    async def submit_flag(self, unique_code: str, flag: str) -> SubmitInfo:
        sdk = _sdk()
        try:
            result = await self._opened.submit_flag(unique_code, flag)
        except sdk.DuplicateSubmit:
            # 幂等：这个 flag 已经计入过。分数与进度以平台为准，不回填猜测值。
            return SubmitInfo(
                correct=True,
                awarded=0,
                cumulative_score=0,
                correct_flag_count=0,
                total_flag_count=0,
                matched_flag_index=None,
                duplicate=True,
            )
        except sdk.InvalidState as exc:
            raise _invalid_state(unique_code, exc) from exc
        except sdk.ChallengeNotFound as exc:
            raise ChallengeNotFound(f"平台没有题目 {unique_code}", code=exc.code) from exc
        except sdk.TSecConnectionError as exc:
            raise PlatformUnavailable(f"提交 Flag 时网络错误：{exc}") from exc
        except sdk.TSecError as exc:
            raise PlatformError(f"提交 Flag 失败：{exc}", code=exc.code) from exc
        return SubmitInfo(
            correct=bool(result.correct),
            awarded=int(result.awarded),
            cumulative_score=int(result.cumulative_score),
            correct_flag_count=int(result.correct_flag_count),
            total_flag_count=int(result.total_flag_count),
            matched_flag_index=(
                None if result.matched_flag_index is None else int(result.matched_flag_index)
            ),
        )

    async def close_challenge(self, unique_code: str) -> bool:
        sdk = _sdk()
        try:
            result = await self._opened.close_challenge(unique_code)
        except sdk.ChallengeNotFound as exc:
            # 容器本来就不存在，按"已释放"处理，但仍记录平台返回。
            raise ChallengeNotFound(f"平台没有题目 {unique_code}", code=exc.code) from exc
        except sdk.InvalidState as exc:
            raise ChallengeConflict(f"关闭容器被平台拒绝：{exc}", code=exc.code) from exc
        except sdk.TSecConnectionError as exc:
            raise PlatformUnavailable(f"关闭容器时网络错误：{exc}") from exc
        except sdk.TSecError as exc:
            raise PlatformError(f"关闭容器失败：{exc}", code=exc.code) from exc
        return bool(result.closed)


def _invalid_state(unique_code: str, exc: Any) -> PlatformError:
    message = str(getattr(exc, "message", "") or exc)
    if "max active" in message.lower():
        return ChallengeConflict(
            f"平台报告活跃容器名额已满（题目 {unique_code} 未启动）。"
            "请先关闭其它题目容器：`uv run workbench tsec release <unique_code>`。",
            code="invalid_state",
        )
    return ChallengeConflict(
        f"平台拒绝当前操作（题目 {unique_code}）：{message}。"
        "常见原因：跑分任务已结束，或该题已在其它会话完成。",
        code="invalid_state",
    )


def _vpn_message(exc: Any) -> str:
    detail = getattr(exc, "detail", None) or {}
    reason = detail.get("reason") if isinstance(detail, dict) else None
    suffix = f"（原因：{reason}）" if reason else ""
    return (
        "VPN 预检未通过，未发起任何平台业务请求。请先让**运行本命令的主机**"
        "（Windows 控制端）连上评测 VPN，再重试。" + suffix
    )


async def _safe_aclose(client: Any) -> None:
    with_close = getattr(client, "aclose", None)
    if with_close is None:
        return
    try:
        await with_close()
    except Exception:
        # 关闭纯属资源回收；不吞业务异常，只忽略关闭本身的失败。
        return


__all__ = [
    "ChallengeConflict",
    "ChallengeInfo",
    "ChallengeNotFound",
    "PlatformClient",
    "PlatformError",
    "PlatformUnavailable",
    "ResourceBusy",
    "SdkPlatformClient",
    "StartInfo",
    "SubmitInfo",
    "VpnInfo",
    "VpnUnavailable",
]

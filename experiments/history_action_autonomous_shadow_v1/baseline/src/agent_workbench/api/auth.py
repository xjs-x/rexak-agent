"""控制端本地访问认证。

约束（§10）：
- 服务**默认绑定回环地址**；
- 单用户仍需要**本地访问令牌/会话**、**精确 Origin 校验**、**写请求的 CSRF 防护**，
  防止用户浏览的其他网站操控本地测试服务；
- `EventSource` 不能随意附加自定义 `Authorization` header —— 采用**同源安全会话 Cookie**
  或支持鉴权的 fetch 流实现，**不把长期 token 放 URL**。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response, status

from agent_workbench.config.settings import Settings, get_settings

COOKIE_NAME = "workbench_session"
CSRF_HEADER = "X-Workbench-CSRF"
ACCESS_HEADER = "X-Workbench-Token"

#: 会话 Cookie 有效期（秒）。首版为本地单用户，取值保守。
SESSION_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class SessionTicket:
    issued_at: int
    csrf_token: str
    signature: str


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


class LocalAuth:
    """本地会话与 CSRF。

    这不是多用户认证系统，也不提供任何"网络暴露后仍安全"的承诺：
    唯一正确的部署方式是只监听回环地址。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def settings(self) -> Settings:
        return self._settings

    def _is_default_secret(self, value: str) -> bool:
        return (not value) or value.startswith("change-me")

    def issue(self, response: Response) -> SessionTicket:
        if self._is_default_secret(self._settings.session_secret):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="未配置 WORKBENCH_SESSION_SECRET（仍是默认值），拒绝签发会话",
            )
        issued_at = int(time.time())
        csrf_token = secrets.token_urlsafe(24)
        signature = _sign(self._settings.session_secret, f"{issued_at}.{csrf_token}")
        response.set_cookie(
            COOKIE_NAME,
            f"{issued_at}.{csrf_token}.{signature}",
            httponly=True,
            samesite="strict",
            secure=False,  # 回环 HTTP；上线到非回环必须启用 TLS 并置 True
            max_age=SESSION_TTL_SECONDS,
            path="/",
        )
        return SessionTicket(issued_at=issued_at, csrf_token=csrf_token, signature=signature)

    def verify_cookie(self, request: Request) -> SessionTicket | None:
        raw = request.cookies.get(COOKIE_NAME)
        if not raw:
            return None
        parts = raw.split(".")
        if len(parts) != 3:
            return None
        try:
            issued_at = int(parts[0])
        except ValueError:
            return None
        csrf_token, signature = parts[1], parts[2]
        expected = _sign(self._settings.session_secret, f"{issued_at}.{csrf_token}")
        if not hmac.compare_digest(expected, signature):
            return None
        if time.time() - issued_at > SESSION_TTL_SECONDS:
            return None
        return SessionTicket(issued_at=issued_at, csrf_token=csrf_token, signature=signature)

    def verify_access_token(self, request: Request) -> bool:
        token = request.headers.get(ACCESS_HEADER) or _bearer(request)
        if not token or self._is_default_secret(self._settings.access_token):
            return False
        return hmac.compare_digest(token, self._settings.access_token)

    def check_origin(self, request: Request) -> None:
        """精确 Origin 校验。

        同源导航请求可能没有 Origin/Referer（例如直接用 curl 调用本地服务），
        此时不因缺失而拒绝；但只要带了 Origin，就必须在白名单内。
        """
        origin = request.headers.get("origin")
        if origin is None:
            referer = request.headers.get("referer")
            if referer is None:
                return
            origin = referer.split("/", 3)[0] + "//" + referer.split("/", 3)[2]

        allowed = set(self._settings.allowed_origin_list)
        # 同源请求：scheme://host:port 与本机绑定一致
        host = request.headers.get("host")
        if host:
            allowed.add(f"http://{host}")
            allowed.add(f"https://{host}")
        if origin not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"来源不被允许：{origin}",
            )

    def require_session(self, request: Request) -> SessionTicket:
        ticket = self.verify_cookie(request)
        if ticket is not None:
            return ticket
        if self.verify_access_token(request):
            # 使用访问令牌的调用方（CLI/脚本）不需要 Cookie/CSRF
            return SessionTicket(issued_at=int(time.time()), csrf_token="", signature="token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要本地会话或访问令牌（POST /api/session 获取）",
        )

    def require_csrf(self, request: Request, ticket: SessionTicket) -> None:
        """写请求的 CSRF 防护。

        使用访问令牌的调用不需要 CSRF（它本来就不依赖 Cookie 自动携带）。
        """
        if ticket.signature == "token":
            return
        provided = request.headers.get(CSRF_HEADER)
        if not provided or not hmac.compare_digest(provided, ticket.csrf_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"缺少或错误的 {CSRF_HEADER} 头（写请求需要 CSRF 令牌）",
            )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        return None
    return header[7:].strip()

"""浏览器 Cookie 转入 HTTP 会话时保留作用域与传输属性。"""

from __future__ import annotations

import asyncio

from workbench_executor.http_sessions import HttpSessionRegistry


def test_cookie_import_preserves_host_only_path_and_secure() -> None:
    async def exercise() -> None:
        sessions = HttpSessionRegistry(None)
        try:
            await sessions.import_cookies(
                "alice",
                [
                    {
                        "name": "fixture_session",
                        "value": "alice",
                        "domain": "example.test",
                        "path": "/private",
                        "secure": True,
                        "httpOnly": True,
                        "expires": -1,
                    }
                ],
            )
            cookie = next(iter(sessions.get("alice").client.cookies.jar))
            assert cookie.domain == "example.test"
            assert cookie.domain_specified is False
            assert cookie.path == "/private"
            assert cookie.secure is True
            assert cookie.has_nonstandard_attr("HttpOnly")
        finally:
            await sessions.close()

    asyncio.run(exercise())

"""无头浏览器与按身份隔离的会话。

设计要点（BUILD_PLAN §10、§9.1、§17）：

1. **按身份隔离**：每个 `identity_id` 拥有独立的 `BrowserContext`（独立 cookie /
   localStorage / 缓存）。切换身份不影响其他身份，也不会串号。
   浏览器状态（storage_state）存在 `data_root/browser-states/`，**与任务工作区分开** ——
   文件工具读不到别的身份的会话。
2. **每个请求都过范围门**，不只是导航。页面可能通过 XHR / 图片 / iframe 去请求
   任意主机；只在导航时检查等于没检查。这里用 `page.route("**/*")` 拦截，
   逐请求做映射与范围校验，越界一律 `abort` 并记入 `blocked_requests`。
3. **JS 重定向后按最终 URL 判定**：动作结束后对 `page.url` 再查一次；
   越界时标记 `final_url_out_of_scope`，控制端据此写覆盖记录，且**不**作为证据。
4. **快照给出可直接使用的句柄**：给可交互元素打 `data-wb-idx`，快照返回
   `selector`，模型不必猜 CSS 选择器。
5. **常驻**：浏览器进程与 context 长期存活，登录态不会因为一次动作结束就丢。

浏览器依赖用 `executable_path` 指定（见 `settings.resolve_browser_executable`），
因此不要求 `playwright install` 的下载产物。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workbench_executor.policy import NetworkPolicy, PolicyDenied
from workbench_executor.settings import ExecutorSettings

#: 不需要经过网络策略的 scheme
LOCAL_SCHEMES = ("data:", "blob:", "about:", "chrome:", "devtools:")

#: 快照里视为"可交互"的元素
INTERACTIVE_SELECTOR = (
    "a, button, input, select, textarea, [role=button], [role=link], [onclick], [type=submit]"
)

SNAPSHOT_SCRIPT = """
() => {
  const nodes = Array.from(document.querySelectorAll(ARG_SELECTOR));
  const items = nodes.slice(0, ARG_LIMIT).map((el, index) => {
    el.setAttribute('data-wb-idx', String(index));
    const rect = el.getBoundingClientRect();
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    return {
      index,
      selector: `[data-wb-idx="${index}"]`,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || null,
      name: el.getAttribute('name') || null,
      role: el.getAttribute('role') || null,
      text: text.slice(0, 200),
      href: el.getAttribute('href') || null,
      value: ('value' in el && typeof el.value === 'string') ? el.value.slice(0, 200) : null,
      visible: rect.width > 0 && rect.height > 0,
      box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
    };
  });
  const bodyText = document.body ? document.body.innerText : '';
  return {
    title: document.title,
    url: location.href,
    interactive_count: nodes.length,
    interactive: items,
    text: bodyText.slice(0, ARG_TEXT_LIMIT),
    text_truncated: bodyText.length > ARG_TEXT_LIMIT,
  };
}
"""


class BrowserUnavailable(Exception):
    """浏览器不可用（未安装 / 启动失败）。属于基础设施问题，不是目标站点的结论。"""


class BrowserActionError(Exception):
    """动作被拒绝或失败。带稳定的 code，便于控制端分类。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class IdentitySession:
    """一个身份的浏览器上下文。"""

    identity_id: str
    context: Any
    page: Any
    state_path: Path
    blocked_requests: list[dict[str, str]] = field(default_factory=list)
    navigation_blocked: bool = False
    policy: NetworkPolicy | None = None
    guard_installed: bool = False


class BrowserManager:
    """常驻浏览器 + 按身份的上下文管理。"""

    def __init__(self, settings: ExecutorSettings) -> None:
        self._settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._sessions: dict[str, IdentitySession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    # ---------------- 生命周期 ----------------

    async def ensure_started(self) -> Any:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            executable = self._settings.resolve_browser_executable()
            if executable is None:
                raise BrowserUnavailable(
                    "找不到可用的浏览器可执行文件；请配置 EXECUTOR_BROWSER_EXECUTABLE，"
                    "或安装系统 chromium。"
                )
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - 部署问题
                raise BrowserUnavailable(f"未安装 playwright：{exc}") from exc

            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    executable_path=str(executable),
                    headless=self._settings.browser_headless,
                    # 执行岛没有用户态沙箱依赖时 --no-sandbox 是必要的；
                    # 容器/受限内核下不加会直接启动失败。
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except Exception as exc:
                await self._stop_playwright()
                raise BrowserUnavailable(f"浏览器启动失败：{type(exc).__name__}: {exc}") from exc
            return self._browser

    async def close(self) -> None:
        async with self._lock:
            persist_errors: list[BrowserActionError] = []
            for session in list(self._sessions.values()):
                try:
                    await self._persist_state(session)
                except BrowserActionError as exc:
                    persist_errors.append(exc)
                try:
                    await session.context.close()
                except Exception:
                    continue
            self._sessions.clear()
            self._session_locks.clear()
            if self._browser is not None:
                with contextlib.suppress(Exception):
                    await self._browser.close()
                self._browser = None
            await self._stop_playwright()
            if persist_errors:
                raise persist_errors[0]

    async def _stop_playwright(self) -> None:
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    def is_alive(self) -> bool:
        return self._browser is not None and bool(
            getattr(self._browser, "is_connected", lambda: False)()
        )

    def browser_version(self) -> str | None:
        if not self.is_alive():
            return None
        try:
            return str(self._browser.version)
        except Exception:
            return None

    def describe_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "identity_id": session.identity_id,
                "state_path": str(session.state_path),
                "state_saved": session.state_path.exists(),
            }
            for session in self._sessions.values()
        ]

    async def close_session(self, identity_id: str) -> bool:
        lock = self._session_locks.setdefault(identity_id, asyncio.Lock())
        async with lock:
            return await self._close_session_locked(identity_id)

    async def _close_session_locked(self, identity_id: str) -> bool:
        session = self._sessions.get(identity_id)
        if session is None:
            return False
        await self._persist_state(session)
        self._sessions.pop(identity_id, None)
        with contextlib.suppress(Exception):
            await session.context.close()
        return True

    # ---------------- 会话 ----------------

    async def session_for(self, identity_id: str) -> IdentitySession:
        """取（或创建）某身份的会话。

        已有会话且页面仍可用时直接复用 —— 这正是"登录态长时间不失效"的实现方式：
        浏览器进程常驻，不因为一次动作结束就重建上下文。
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identity_id):
            raise BrowserActionError(
                "invalid_identity",
                "identity_id 只能包含字母、数字、下划线和连字符，且长度不超过 128",
            )
        existing = self._sessions.get(identity_id)
        if existing is not None and not self.is_alive():
            self._sessions.pop(identity_id, None)
            self._browser = None
            await self._stop_playwright()
            raise BrowserActionError(
                "browser_crashed",
                "浏览器进程已断开；本次动作未执行。下次调用将从已保存状态重建，需重新验证登录态。",
            )
        if existing is not None and not existing.page.is_closed():
            return existing
        if existing is not None:
            self._sessions.pop(identity_id, None)
            raise BrowserActionError(
                "page_crashed",
                "浏览器页面已关闭；本次动作未执行。下次调用将从已保存状态重建。",
            )

        browser = await self.ensure_started()
        state_path = self._settings.browser_states / f"{identity_id}.json"
        storage_state: str | None = str(state_path) if state_path.exists() else None

        context = await browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
            service_workers="block",
        )
        page = await context.new_page()
        session = IdentitySession(
            identity_id=identity_id,
            context=context,
            page=page,
            state_path=state_path,
        )
        self._sessions[identity_id] = session
        return session

    async def _persist_state(self, session: IdentitySession) -> None:
        try:
            session.state_path.parent.mkdir(parents=True, exist_ok=True)
            state = await session.context.storage_state(indexed_db=True)
            payload = (json.dumps(state, ensure_ascii=False) + "\n").encode("utf-8")
            descriptor, temporary = tempfile.mkstemp(
                prefix=".browser-state-", dir=session.state_path.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                Path(temporary).chmod(0o600)
                Path(temporary).replace(session.state_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        except Exception as exc:
            raise BrowserActionError(
                "state_persist_failed", f"浏览器身份状态保存失败：{type(exc).__name__}"
            ) from exc

    async def export_cookies(
        self, identity_id: str, url: str, mapped_url: str | None = None
    ) -> list[dict[str, Any]]:
        """只导出指定身份、指定 URL 可见的 Cookie；不返回到任务工件。"""
        session = self._sessions.get(identity_id)
        if session is None:
            return []
        urls = [url] if not mapped_url or mapped_url == url else [url, mapped_url]
        return list(await session.context.cookies(urls=urls))

    # ---------------- 范围门 ----------------

    async def _install_scope_guard(self, session: IdentitySession, policy: NetworkPolicy) -> None:
        """给该页面装上逐请求范围门。

        **每个 http/https 请求都要过**，包括 XHR、图片、iframe ——
        只在导航时检查挡不住 `fetch('http://elsewhere/')`。
        """
        session.blocked_requests = []
        session.navigation_blocked = False
        session.policy = policy
        if session.guard_installed:
            return

        async def _guard(route: Any, request: Any) -> None:
            url = request.url
            if url.startswith(LOCAL_SCHEMES):
                await route.continue_()
                return
            try:
                active = session.policy
                if active is None:
                    raise PolicyDenied("浏览器范围策略尚未设置", target=url)
                mapped, extra_headers = active.resolve_mapping(url)
                active.check(mapped)
            except PolicyDenied as exc:
                session.blocked_requests.append(
                    {
                        "url": url,
                        "resource_type": request.resource_type,
                        "reason": str(exc),
                    }
                )
                if request.resource_type == "document":
                    session.navigation_blocked = True
                await route.abort("blockedbyclient")
                return
            except Exception as exc:
                session.blocked_requests.append(
                    {
                        "url": url,
                        "resource_type": request.resource_type,
                        "reason": f"策略检查失败：{type(exc).__name__}",
                    }
                )
                if request.resource_type == "document":
                    session.navigation_blocked = True
                await route.abort("blockedbyclient")
                return

            if mapped != url and extra_headers:
                await route.continue_(url=mapped, headers={**request.headers, **extra_headers})
            elif mapped != url:
                await route.continue_(url=mapped)
            else:
                await route.continue_()

        # 注意：Playwright 异步 API 里 `route` / `unroute` **都是协程**，必须 await。
        # 漏掉 await 不会报错，只会得到"守卫从未安装"—— 表现为范围外请求全部放行。
        # 这是实机测试抓到的：单元测试看不到，因为那时还没真的发起请求。
        # Context 级守卫覆盖弹窗的首个请求；Page.route 只能覆盖当前页，
        # 对 popup 和新页的首次请求不成立。Service Worker 已在创建 context 时关闭。
        await session.context.route("**/*", _guard)

        async def _guard_websocket(route: Any) -> None:
            from urllib.parse import urlsplit, urlunsplit

            url = str(route.url)
            parts = urlsplit(url)
            scheme = "https" if parts.scheme == "wss" else "http"
            http_url = urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))
            try:
                active = session.policy
                if active is None:
                    raise PolicyDenied("浏览器范围策略尚未设置", target=url)
                mapped, _headers = active.resolve_mapping(http_url)
                active.check(mapped)
                if mapped != http_url:
                    raise PolicyDenied("WebSocket 目标映射暂不支持，已阻断", target=url)
            except PolicyDenied as exc:
                session.blocked_requests.append(
                    {"url": url, "resource_type": "websocket", "reason": str(exc)}
                )
                await route.close(code=1008, reason="scope denied")
                return
            route.connect_to_server()

        await session.context.route_web_socket("**/*", _guard_websocket)
        session.guard_installed = True

    # ---------------- 动作 ----------------

    async def run_action(
        self,
        *,
        action: str,
        identity_id: str,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        job_dir: Path,
    ) -> dict[str, Any]:
        lock = self._session_locks.setdefault(identity_id, asyncio.Lock())
        async with lock:
            return await self._run_action_locked(
                action=action,
                identity_id=identity_id,
                payload=payload,
                policy=policy,
                job_dir=job_dir,
            )

    async def _run_action_locked(
        self,
        *,
        action: str,
        identity_id: str,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        job_dir: Path,
    ) -> dict[str, Any]:
        session = await self.session_for(identity_id)
        await self._install_scope_guard(session, policy)

        timeout = int(self._settings.browser_action_timeout_ms)
        page = session.page

        try:
            if action == "navigate":
                return await self._navigate(session, payload, policy, timeout, action=action)
            if action == "click":
                return await self._click(session, payload, policy, timeout)
            if action == "fill":
                return await self._fill(session, payload, policy, timeout)
            if action == "press":
                return await self._press(session, payload, policy, timeout)
            if action == "snapshot":
                return await self._snapshot(session, payload, policy, job_dir)
            if action == "text":
                return await self._text(session, payload, policy, job_dir)
            if action == "screenshot":
                return await self._screenshot(session, payload, policy, job_dir)
            if action == "download":
                return await self._download(session, payload, policy, timeout, job_dir)
            if action == "back":
                await page.go_back(timeout=timeout, wait_until="domcontentloaded")
                return await self._post(
                    session, policy, job_dir, action=action, extra={"navigated": True}
                )
            if action == "login":
                return await self._login(session, payload, policy, timeout)
        except PolicyDenied as exc:
            raise BrowserActionError(
                "scope_violation", f"目标不在声明的测试范围内：{exc}", details={"target": str(exc)}
            ) from exc
        except BrowserActionError:
            raise
        except Exception as exc:
            raise BrowserActionError("browser_error", f"{type(exc).__name__}: {exc}") from exc

        raise BrowserActionError("unsupported_action", f"不支持的浏览器动作：{action}")

    async def _navigate(
        self,
        session: IdentitySession,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        timeout: int,
        *,
        action: str,
    ) -> dict[str, Any]:
        url = str(payload.get("url") or "").strip()
        if not url:
            raise BrowserActionError("invalid_request", "navigate 需要 url")

        # **派发前**先判定：越界连请求都不发（§10）
        mapped, _extra_headers = policy.resolve_mapping(url)
        policy.check(mapped)

        response = await session.page.goto(
            url, timeout=timeout, wait_until=str(payload.get("wait_until") or "domcontentloaded")
        )
        return await self._post(
            session,
            policy,
            None,
            action=action,
            status=response.status if response else None,
            extra={"requested_url": url, "status": response.status if response else None},
        )

    async def _login(
        self,
        session: IdentitySession,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        timeout: int,
    ) -> dict[str, Any]:
        """使用服务端注入的凭据登录；结果和工件不包含用户名或密码。"""
        login_url = str(payload.get("login_url") or "")
        username = payload.get("username")
        password = payload.get("password")
        user_selector = str(payload.get("username_selector") or "")
        pass_selector = str(payload.get("password_selector") or "")
        submit_selector = str(payload.get("submit_selector") or "")
        logged_in_selector = str(payload.get("logged_in_selector") or "")
        if not all(
            (
                login_url,
                username,
                password,
                user_selector,
                pass_selector,
                submit_selector,
                logged_in_selector,
            )
        ):
            raise BrowserActionError("invalid_request", "登录缺少地址、凭据、表单选择器或登录探针")
        mapped, _headers = policy.resolve_mapping(login_url)
        policy.check(mapped)
        await session.page.goto(login_url, timeout=timeout, wait_until="domcontentloaded")
        await session.page.fill(user_selector, str(username), timeout=timeout)
        await session.page.fill(pass_selector, str(password), timeout=timeout)
        await session.page.click(submit_selector, timeout=timeout)
        probe_url = payload.get("probe_url")
        if probe_url:
            mapped_probe, _headers = policy.resolve_mapping(str(probe_url))
            policy.check(mapped_probe)
            await session.page.goto(str(probe_url), timeout=timeout, wait_until="domcontentloaded")
        authenticated = await session.page.locator(logged_in_selector).count() > 0
        if not authenticated:
            raise BrowserActionError(
                "authentication_failed",
                "登录探针未通过；不能把当前页面当作已认证状态",
            )
        return await self._post(
            session,
            policy,
            None,
            action="login",
            extra={"authenticated": True, "probe_checked": True},
        )

    async def _target_selector(self, session: IdentitySession, payload: dict[str, Any]) -> str:
        selector = payload.get("selector")
        if selector:
            return str(selector)
        text = payload.get("text")
        if text:
            locator = session.page.get_by_text(str(text), exact=True)
            if await locator.count() == 0:
                raise BrowserActionError("target_not_found", f"找不到文本为 {text!r} 的元素")
            return f"text={text}"
        raise BrowserActionError("invalid_request", "需要 selector 或 text 指定目标")

    async def _click(
        self,
        session: IdentitySession,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        timeout: int,
    ) -> dict[str, Any]:
        selector = await self._target_selector(session, payload)
        before = session.page.url
        await session.page.click(selector, timeout=timeout)
        if bool(payload.get("wait_for_navigation", True)):
            # 点击不一定触发导航，等不到是正常情况
            with contextlib.suppress(Exception):
                await session.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        return await self._post(
            session,
            policy,
            None,
            action="click",
            extra={"selector": selector, "navigated": session.page.url != before},
        )

    async def _fill(
        self,
        session: IdentitySession,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        timeout: int,
    ) -> dict[str, Any]:
        selector = await self._target_selector(session, payload)
        value = payload.get("value")
        if value is None:
            raise BrowserActionError("invalid_request", "fill 需要 value")
        await session.page.fill(selector, str(value), timeout=timeout)
        submitted = False
        if bool(payload.get("submit", False)):
            await session.page.press(selector, "Enter", timeout=timeout)
            submitted = True
            # 提交后不一定导航（可能是同页校验），等不到是正常情况
            with contextlib.suppress(Exception):
                await session.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        return await self._post(
            session,
            policy,
            None,
            action="fill",
            extra={"selector": selector, "submitted": submitted, "value_length": len(str(value))},
        )

    async def _press(
        self,
        session: IdentitySession,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        timeout: int,
    ) -> dict[str, Any]:
        key = str(payload.get("key") or "Enter")
        selector = payload.get("selector")
        if selector:
            await session.page.press(str(selector), key, timeout=timeout)
        else:
            await session.page.keyboard.press(key, timeout=timeout)
        return await self._post(session, policy, None, action="press", extra={"key": key})

    async def _snapshot(
        self,
        session: IdentitySession,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        job_dir: Path,
    ) -> dict[str, Any]:
        script = (
            SNAPSHOT_SCRIPT.replace("ARG_SELECTOR", json.dumps(INTERACTIVE_SELECTOR))
            .replace("ARG_LIMIT", str(int(payload.get("max_elements") or 200)))
            .replace("ARG_TEXT_LIMIT", str(int(payload.get("max_text_chars") or 20000)))
        )
        snapshot = await session.page.evaluate(script)
        text = str(snapshot.pop("text", ""))
        if job_dir is not None:
            (job_dir / "page.txt").write_text(text, encoding="utf-8", newline="\n")
        return await self._post(
            session,
            policy,
            job_dir,
            action="snapshot",
            extra={**snapshot, "text_chars": len(text)},
            artifacts={"page.txt": "text/plain; charset=utf-8"},
        )

    async def _text(
        self,
        session: IdentitySession,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        job_dir: Path,
    ) -> dict[str, Any]:
        limit = int(payload.get("max_chars") or 100_000)
        text = await session.page.inner_text(payload.get("selector") or "body")
        text = text[:limit]
        if job_dir is not None:
            (job_dir / "page.txt").write_text(text, encoding="utf-8", newline="\n")
        return await self._post(
            session,
            policy,
            job_dir,
            action="text",
            extra={"text_chars": len(text)},
            artifacts={"page.txt": "text/plain; charset=utf-8"},
        )

    async def _screenshot(
        self,
        session: IdentitySession,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        job_dir: Path,
    ) -> dict[str, Any]:
        shot = await session.page.screenshot(
            full_page=bool(payload.get("full_page", False)), type="png"
        )
        if len(shot) > self._settings.browser_artifact_limit_bytes:
            raise BrowserActionError(
                "artifact_too_large",
                f"截图 {len(shot)} 字节超过上限 {self._settings.browser_artifact_limit_bytes}",
            )
        target = job_dir / "screenshot.png"
        target.write_bytes(shot)
        return await self._post(
            session,
            policy,
            job_dir,
            action="screenshot",
            extra={
                "screenshot_bytes": len(shot),
                "screenshot_sha256": hashlib.sha256(shot).hexdigest(),
                "full_page": bool(payload.get("full_page", False)),
            },
            artifacts={"screenshot.png": "image/png"},
        )

    async def _download(
        self,
        session: IdentitySession,
        payload: dict[str, Any],
        policy: NetworkPolicy,
        timeout: int,
        job_dir: Path,
    ) -> dict[str, Any]:
        selector = await self._target_selector(session, payload)
        async with session.page.expect_download(timeout=timeout) as info:
            await session.page.click(selector, timeout=timeout)
        download = await info.value

        downloads = job_dir / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        suggested = Path(download.suggested_filename or "download.bin").name
        target = downloads / suggested
        await download.save_as(str(target))

        size = target.stat().st_size
        if size > self._settings.browser_artifact_limit_bytes:
            target.unlink(missing_ok=True)
            raise BrowserActionError(
                "artifact_too_large",
                f"下载 {size} 字节超过上限 {self._settings.browser_artifact_limit_bytes}",
            )
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return await self._post(
            session,
            policy,
            job_dir,
            action="download",
            extra={
                "filename": suggested,
                "byte_size": size,
                "sha256": digest,
                "source_url": download.url,
            },
            artifacts={f"downloads/{suggested}": "application/octet-stream"},
        )

    # ---------------- 收尾：最终 URL 判定 ----------------

    async def _post(
        self,
        session: IdentitySession,
        policy: NetworkPolicy,
        job_dir: Path | None,
        *,
        action: str,
        status: int | None = None,
        extra: dict[str, Any] | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """动作结束后的统一收尾：以**最终 URL** 判定范围并落结果。"""
        final_url = session.page.url
        out_of_scope = False
        out_reason = ""
        try:
            mapped_final, _headers = policy.resolve_mapping(final_url)
            policy.check(mapped_final)
        except PolicyDenied as exc:
            out_of_scope = True
            out_reason = str(exc)

        title = ""
        try:
            title = await session.page.title()
        except Exception:
            title = ""

        if session.navigation_blocked:
            raise BrowserActionError(
                "scope_violation",
                "导航目标在范围之外，已被阻断（请求未发出）。",
                details={"blocked_requests": session.blocked_requests[:10]},
            )

        if not out_of_scope:
            await self._persist_state(session)

        result: dict[str, Any] = {
            "action": action,
            "final_url": final_url,
            "title": title,
            "status": status,
            "final_url_out_of_scope": out_of_scope,
            "out_of_scope_reason": out_reason,
            "blocked_requests": session.blocked_requests[:50],
            "blocked_request_count": len(session.blocked_requests),
            "identity_id": session.identity_id,
        }
        if extra:
            result.update(extra)
        if artifacts:
            result["artifacts"] = artifacts
            result["artifact_manifest"] = {
                name: _artifact_meta(job_dir / name) for name in artifacts if job_dir is not None
            }
        return result


def _artifact_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"missing": True}
    data = path.read_bytes()
    return {
        "byte_size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def new_session_id() -> str:
    return uuid.uuid4().hex

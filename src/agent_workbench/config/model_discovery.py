"""Read-only model directory requests to the explicitly configured endpoint."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic import ValidationError

from agent_workbench.config.model_settings import apply_documented_limits
from agent_workbench.contracts.models import CatalogModel, DiscoveryFormat, ModelDiscoveryRead

MAX_MODELS = 2000
MAX_PAGES = 40
PAGE_SIZE = 50
MAX_BYTES = 4 * 1024 * 1024
TOTAL_TIMEOUT = 25


class DiscoveryError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


async def _read_page(client: httpx.AsyncClient, url: str, params: dict[str, int] | None) -> dict:
    async with client.stream("GET", url, params=params) as response:
        status = response.status_code
        if 300 <= status < 400:
            raise DiscoveryError("redirect", "目录接口要求重定向，请直接填写可信的最终地址", status)
        if status in (401, 403):
            raise DiscoveryError(
                "authorization", f"目录访问被拒绝（HTTP {status}），请检查 Key 和目录权限", status
            )
        if status in (404, 405, 501):
            raise DiscoveryError(
                "unsupported", f"此地址不支持目录查询（HTTP {status}），可以手动添加模型", status
            )
        if status != 200:
            raise DiscoveryError("upstream_status", f"目录查询失败（HTTP {status}）", status)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise DiscoveryError("invalid_response", "目录接口返回了不支持的压缩格式")
        data = bytearray()
        async for chunk in response.aiter_raw(chunk_size=65536):
            if len(data) + len(chunk) > MAX_BYTES:
                raise DiscoveryError("response_limit", "模型目录响应超过大小限制")
            data.extend(chunk)
    try:
        body = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise DiscoveryError("invalid_json", "目录接口未返回有效的 UTF-8 JSON") from None
    if not isinstance(body, dict):
        raise DiscoveryError("invalid_response", "模型目录结构与所选格式不匹配")
    return body


def _directory_rows(body: dict, format_: DiscoveryFormat, page: int) -> tuple[list, bool]:
    if format_ == "openai":
        rows = body.get("data")
        if isinstance(rows, list) and not body.get("error"):
            return rows, False
    else:
        output = body.get("output")
        if body.get("success") is True and isinstance(output, dict):
            rows, total = output.get("models"), output.get("total")
            page_no, page_size = output.get("page_no"), output.get("page_size")
            if (
                isinstance(rows, list)
                and type(total) is int
                and total >= 0
                and page_no == page
                and type(page_size) is int
                and page_size > 0
            ):
                more = page * page_size < total
                if rows or not more:
                    return rows, more
    raise DiscoveryError("invalid_response", "模型目录结构与所选格式不匹配，可以手动添加模型")


def _catalog_entry(row: Any, format_: DiscoveryFormat, key: str) -> CatalogModel:
    if not isinstance(row, dict):
        raise DiscoveryError("invalid_response", "目录包含无效的 Model ID")
    model_id = row.get("id" if format_ == "openai" else "model")
    if not isinstance(model_id, str):
        raise DiscoveryError("invalid_response", "目录包含无效的 Model ID")
    if key in model_id:
        raise DiscoveryError("invalid_response", "目录响应包含敏感信息")
    try:
        entry = CatalogModel(model_id=model_id)
    except ValidationError:
        raise DiscoveryError("invalid_response", "目录包含无效或超长的 Model ID") from None
    info = row.get("model_info") if format_ == "dashscope" else None
    if isinstance(info, dict):
        specs = {
            field: info[source]
            for field, source in (
                ("context_window", "context_window"),
                ("max_input_tokens", "max_input_tokens"),
                ("max_output_tokens", "max_output_tokens"),
            )
            if type(info.get(source)) is int and info[source] > 1
        }
        if specs:
            try:
                return CatalogModel(
                    model_id=model_id, **specs, specification_source="provider_catalog"
                )
            except ValidationError:
                # A valid identifier remains usable even if the advertised specifications conflict.
                return entry
    return entry


async def fetch_catalog(url: str, key: str, format_: DiscoveryFormat) -> ModelDiscoveryRead:
    models: dict[str, CatalogModel] = {}
    try:
        async with (
            asyncio.timeout(TOTAL_TIMEOUT),
            httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=5),
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as client,
        ):
            for page in range(1, MAX_PAGES + 1):
                params = (
                    {"page_no": page, "page_size": PAGE_SIZE} if format_ == "dashscope" else None
                )
                body = await _read_page(client, url, params)
                rows, more = _directory_rows(body, format_, page)
                for row in rows:
                    entry = apply_documented_limits(_catalog_entry(row, format_, key), url)
                    if entry.model_id not in models:
                        if len(models) >= MAX_MODELS:
                            return ModelDiscoveryRead(models=list(models.values()), truncated=True)
                        models[entry.model_id] = entry
                if not more:
                    return ModelDiscoveryRead(models=list(models.values()))
            return ModelDiscoveryRead(models=list(models.values()), truncated=True)
    except (TimeoutError, httpx.TimeoutException):
        raise DiscoveryError("timeout", "模型目录请求超时，请重试或手动添加模型") from None
    except (httpx.HTTPError, httpx.InvalidURL):
        raise DiscoveryError("connection", "无法连接模型目录，请检查地址、TLS 和网络") from None

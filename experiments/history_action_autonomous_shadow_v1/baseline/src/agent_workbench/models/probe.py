"""模型能力探针（P0 交付物）。

只用**无副作用的本地工具**，探明（§11.1）：
- 是否支持 tool calling、以及完整工具参数是否能被正确组装；
- 分片工具调用能否拼装出合法 JSON（分片未完整或 schema 未通过前**禁止执行**，§11.2）；
- 流式是否可用，以及首 token / 完整响应耗时；
- `usage` 是否返回、字段口径（input/output/cache/reasoning）；
- 空响应、错误码的形态。

输出 `CapabilityReport`，由 `workbench doctor` 与决策记录使用。
**本模块不把探测结论写进代码常量**：结论交由人工审核后写入
`docs/decisions/0001-stack-lock.md`，避免"猜出来的能力"。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agent_workbench.config.catalog import Capability
from agent_workbench.config.secrets import SecretStore, build_secret_store
from agent_workbench.models.provider import ModelBinding, build_chat_model, resolve_provider_profile


class _EchoArgs(BaseModel):
    """探针工具入参。故意包含多字段，用于检查参数组装而非只测单一字符串。"""

    label: str = Field(description="一个简短标签")
    count: int = Field(description="重复次数，1-5")
    tags: list[str] = Field(default_factory=list, description="标签列表")


def _echo(label: str, count: int = 1, tags: list[str] | None = None) -> str:
    """回显。无任何外部副作用。"""
    return json.dumps({"label": label, "count": count, "tags": tags or []}, ensure_ascii=False)


@dataclass(slots=True)
class ProbeObservation:
    name: str
    ok: bool
    detail: str
    duration_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CapabilityReport:
    provider_id: str
    model_name: str
    base_url: str
    probes: list[ProbeObservation]
    supports_tools: Capability
    supports_streaming: Capability
    supports_usage: Capability
    supports_images: Capability
    token_estimation_method: str
    usage_sample: dict[str, Any]
    notes: list[str]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["probes"] = [asdict(p) for p in self.probes]
        return data

    def summary_lines(self) -> list[str]:
        lines = [
            f"供应商/模型：{self.provider_id} / {self.model_name}",
            f"端点：{self.base_url}",
            f"tools={self.supports_tools} streaming={self.supports_streaming} "
            f"usage={self.supports_usage} images={self.supports_images}",
            f"token 估算方式：{self.token_estimation_method}",
        ]
        for probe in self.probes:
            mark = "PASS" if probe.ok else "FAIL"
            lines.append(f"  [{mark}] {probe.name} ({probe.duration_ms:.0f} ms) — {probe.detail}")
        if self.notes:
            lines.append("备注：")
            lines.extend(f"  - {note}" for note in self.notes)
        return lines


def _extract_usage(message: Any) -> dict[str, Any]:
    """抽取 usage。

    兼容接口可能把 usage 放在 `usage_metadata`、`response_metadata.token_usage`
    或 `response_metadata.usage`。**保留原始字段**以便核对（§11.2）。
    """
    usage: dict[str, Any] = {}
    meta = getattr(message, "usage_metadata", None)
    if isinstance(meta, dict):
        usage["usage_metadata"] = meta
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        for key in ("token_usage", "usage"):
            if key in response_metadata:
                usage[key] = response_metadata[key]
    return usage


async def run_probe(
    *,
    provider_id: str | None = None,
    model_name: str | None = None,
    secret_store: SecretStore | None = None,
    timeout_seconds: int = 60,
) -> CapabilityReport:
    """执行一轮能力探测。

    任何单项失败都记录为 `ok=False` 并继续，不吞掉异常信息。
    """
    profile = resolve_provider_profile(provider_id, model_name)
    store = secret_store or build_secret_store()
    if not store.exists(profile.secret_ref):
        raise RuntimeError(
            f"缺少凭据：{profile.secret_ref} 无法解析；请为当前供应商设置对应的环境变量"
        )

    binding = ModelBinding(profile=profile, request_timeout_seconds=timeout_seconds)
    probes: list[ProbeObservation] = []
    notes: list[str] = []
    usage_sample: dict[str, Any] = {}

    # ---------------- 探针 1：纯文本 ----------------
    model = build_chat_model(binding, secret_store=store, streaming=False)
    started = time.perf_counter()
    try:
        message = await asyncio.wait_for(
            model.ainvoke([HumanMessage(content="只回复两个字：正常")]),
            timeout=timeout_seconds,
        )
        elapsed = (time.perf_counter() - started) * 1000
        text = _text_of(message)
        probes.append(
            ProbeObservation(
                name="plain_text",
                ok=bool(text.strip()),
                detail=f"响应长度={len(text)}，内容前 40 字={text[:40]!r}",
                duration_ms=elapsed,
            )
        )
        usage_sample = _extract_usage(message)
        if not usage_sample:
            notes.append("非流式响应的 usage 字段为空 —— 用量将标记为 unknown，不可当 0")
    except Exception as exc:
        probes.append(
            ProbeObservation(
                name="plain_text",
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )

    # ---------------- 探针 2：工具调用（完整参数） ----------------
    tool = StructuredTool.from_function(
        func=_echo,
        name="probe_echo",
        description="无副作用的本地回显工具。需要回显内容时调用它。",
        args_schema=_EchoArgs,
    )
    tool_model = build_chat_model(binding, secret_store=store, streaming=False).bind_tools([tool])
    started = time.perf_counter()
    try:
        message = await asyncio.wait_for(
            tool_model.ainvoke(
                [
                    HumanMessage(
                        content=(
                            "请调用 probe_echo 工具，label 设为 mid-check，count 设为 2，"
                            "tags 设为 [alpha, beta]。不要直接回答。"
                        )
                    )
                ]
            ),
            timeout=timeout_seconds,
        )
        calls = getattr(message, "tool_calls", None) or []
        ok = False
        detail = "模型未产生 tool_calls"
        if calls:
            first = calls[0]
            args = first.get("args") if isinstance(first, dict) else None
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = None
            if isinstance(args, dict):
                ok = args.get("label") == "mid-check" and args.get("count") == 2
                detail = (
                    f"tool_calls={len(calls)}，参数={json.dumps(args, ensure_ascii=False)[:160]}"
                )
            else:
                detail = f"tool_call 参数无法解析为对象：{args!r}"
        probes.append(
            ProbeObservation(
                name="tool_call_complete_args",
                ok=ok,
                detail=detail,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
    except Exception as exc:
        probes.append(
            ProbeObservation(
                name="tool_call_complete_args",
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )

    # ---------------- 探针 3：流式 + 首 token 耗时 + 分片工具调用 ----------------
    stream_model = build_chat_model(binding, secret_store=store, streaming=True)
    started = time.perf_counter()
    first_token_ms: float | None = None
    chunk_count = 0
    assembled: dict[str, Any] = {}
    stream_error: str | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in stream_model.astream(
                [HumanMessage(content="请分点用三句话介绍 HTTP 状态码的含义。")]
            ):
                chunk_count += 1
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - started) * 1000
                assembled.update(chunk.usage_metadata or {})
        probes.append(
            ProbeObservation(
                name="streaming",
                ok=chunk_count > 0,
                detail=(
                    f"分片数={chunk_count}，首 token={first_token_ms:.0f} ms"
                    if first_token_ms is not None
                    else "未收到任何分片"
                ),
                duration_ms=(time.perf_counter() - started) * 1000,
                extra={"chunks": chunk_count, "first_token_ms": first_token_ms},
            )
        )
    except Exception as exc:
        stream_error = f"{type(exc).__name__}: {exc}"
        probes.append(
            ProbeObservation(
                name="streaming",
                ok=False,
                detail=stream_error,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )

    stream_tool_model = stream_model.bind_tools([tool])
    started = time.perf_counter()
    streamed_calls: list[Any] = []
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in stream_tool_model.astream(
                [
                    HumanMessage(
                        content="调用 probe_echo，label=stream-check，count=1，tags=[gamma]。"
                    )
                ]
            ):
                streamed_calls.extend(getattr(chunk, "tool_call_chunks", None) or [])
        valid = _assemble_tool_call_chunks(streamed_calls)
        probes.append(
            ProbeObservation(
                name="streaming_tool_call_chunks",
                ok=bool(valid.get("assembled_json_valid")),
                detail=(
                    f"分片数={len(streamed_calls)}，拼装结果="
                    f"{json.dumps(valid, ensure_ascii=False)[:200]}"
                ),
                duration_ms=(time.perf_counter() - started) * 1000,
                extra=valid,
            )
        )
    except Exception as exc:
        probes.append(
            ProbeObservation(
                name="streaming_tool_call_chunks",
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )

    # ---------------- 探针 4：空响应 / 错误码形态 ----------------
    started = time.perf_counter()
    try:
        message = await asyncio.wait_for(
            model.ainvoke([HumanMessage(content="")]),
            timeout=timeout_seconds,
        )
        probes.append(
            ProbeObservation(
                name="empty_input_handling",
                ok=True,
                detail=f"返回而非抛错，内容长度={len(_text_of(message))}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
    except Exception as exc:
        probes.append(
            ProbeObservation(
                name="empty_input_handling",
                ok=True,  # 抛错也是明确行为，不算探测失败
                detail=f"明确抛错：{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )

    # ---------------- 汇总结论 ----------------
    by_name = {probe.name: probe for probe in probes}
    supports_tools: Capability = (
        "supported"
        if by_name.get("tool_call_complete_args", ProbeObservation("", False, "")).ok
        else "unsupported"
    )
    supports_streaming: Capability = (
        "supported"
        if by_name.get("streaming", ProbeObservation("", False, "")).ok
        else "unsupported"
    )
    supports_usage: Capability = "supported" if usage_sample else "unknown"

    if supports_tools == "supported" and not by_name["streaming_tool_call_chunks"].ok:
        notes.append(
            "非流式工具调用可用，但流式 tool_call 分片未能拼装出合法 JSON —— "
            "流式工具派发必须等完整 JSON 且通过 schema 校验后才执行（§11.2）"
        )
    if not usage_sample:
        notes.append("未观察到 usage 字段：用量状态标 unknown，费用不得显示为 0")

    return CapabilityReport(
        provider_id=profile.provider_id,
        model_name=profile.model_name,
        base_url=profile.base_url,
        probes=probes,
        supports_tools=supports_tools,
        supports_streaming=supports_streaming,
        supports_usage=supports_usage,
        supports_images=profile.supports_images,
        token_estimation_method=profile.token_estimation_method,
        usage_sample=usage_sample,
        notes=notes,
        generated_at=datetime.now(UTC).isoformat(),
    )


def _text_of(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
        return "".join(parts)
    return str(content)


def _assemble_tool_call_chunks(chunks: list[Any]) -> dict[str, Any]:
    """按 index 拼装 tool_call 分片。

    只判断"能否拼装成合法 JSON"，**不执行**任何调用。
    """
    buffers: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        if isinstance(chunk, dict):
            index = int(chunk.get("index", 0))
            name = chunk.get("name")
            args = chunk.get("args", "")
        else:
            index = int(getattr(chunk, "index", 0) or 0)
            name = getattr(chunk, "name", None)
            args = getattr(chunk, "args", "") or ""
        slot = buffers.setdefault(index, {"name": None, "args": ""})
        if name:
            slot["name"] = name
        slot["args"] += args

    results: dict[str, Any] = {"calls": len(buffers), "assembled_json_valid": False}
    for index, slot in buffers.items():
        raw = slot["args"].strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            results[f"call_{index}"] = {"error": f"JSONDecodeError: {exc}", "raw": raw[:160]}
            continue
        results[f"call_{index}"] = {"name": slot["name"], "args": parsed}
        results["assembled_json_valid"] = True
    return results

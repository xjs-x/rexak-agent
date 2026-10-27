"""默认只校验/预览。真实执行须另获用户批准，且显式提供冻结哈希及独立环境凭据。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from langchain_openai.chat_models.base import _convert_dict_to_message
from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter

from agent_workbench.contracts.session_graph import GraphOperation
from agent_workbench.domain.session_graph import apply_operations, empty_graph
from agent_workbench.models.bailian import BailianChatOpenAI
from agent_workbench.tools.session_graph import GRAPH_TOOLS

ROOT = Path(__file__).resolve().parent


class SimulatedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_kind: Literal[
        "read_history", "query_job", "inspect_identity", "check_condition", "simulate_check", "stop"
    ]
    subject: str
    environment: str
    evidence_refs: list[str]
    rationale: str


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def verify():
    for name, expected in read("freeze.json")["files"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"冻结文件已变化：{name}")
    return hashlib.sha256((ROOT / "freeze.json").read_bytes()).hexdigest()


def evaluate_graph(call):
    """离线结构和来源集合初筛；事务验证另用隔离pytest，不冒充accepted_update。"""
    page = read("e1-page.json")
    if call["name"] not in {"get_graph", "update_graph"}:
        return {"outcome": "disallowed_tool"}
    try:
        cast(
            Any, next(t for t in GRAPH_TOOLS if t.name == call["name"]).tool_call_schema
        ).model_validate(call["args"])
    except ValueError:
        return {"outcome": "invalid_arguments"}
    if call["name"] == "get_graph":
        return {"outcome": "read_graph", "page_acknowledged": False}
    args = call["args"]
    if args.get("page_id") != page["page_id"] or args.get("expected_revision") != 0:
        return {"outcome": "invalid_arguments", "error_category": "page_or_revision_mismatch"}
    try:
        ops = TypeAdapter(list[GraphOperation]).validate_python(args["operations"])
        allowed = set(page["legal_source_refs"])
        if any(ref not in allowed for op in ops for ref in getattr(op.data, "source_refs", [])):
            return {"outcome": "validation_failed", "error_category": "source_not_in_fixture"}
        graph, _ = apply_operations(empty_graph(), ops, "isolated-e1-proposal")
        return {
            "outcome": "structurally_valid_proposal",
            "empty_update": not ops,
            "page_id_matches": True,
            "database_commit": "NOT_EXECUTED",
            "semantic_score": "PENDING_BLIND_REVIEW",
            "proposal": [op.model_dump() for op in ops],
            "graph_counts": {
                k: {j: len(v[j]) for j in ["nodes", "edges"]} for k, v in graph.items()
            },
        }
    except (ValueError, KeyError, TypeError):
        return {"outcome": "validation_failed", "error_category": "structure"}


def append(path, value):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


async def execute(args, frozen_hash):
    if args.approved_freeze_sha256 != frozen_hash:
        raise RuntimeError("必须显式提供已获用户批准的freeze SHA256；默认不执行")
    config = read("config.json")
    if not config["endpoint"].startswith("https://"):
        raise RuntimeError("端点尚未冻结")
    # 无生产SecretStore、Web偏好、生产DB、Executor；仅使用用户为本实验显式提供的独立变量。
    key = os.environ.get("REXAK_EXPERIMENT_API_KEY")
    if not key:
        raise RuntimeError("未设置实验专用环境变量 REXAK_EXPERIMENT_API_KEY；未发送请求")
    e0 = [json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "results").glob("e0-*.json")]
    if len(e0) != 24 or any(r["status"] != "PASS" for r in e0):
        raise RuntimeError("E0未完整通过；禁止真实请求")
    for name, expected in read("results/source-fingerprints.json").items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("E0之后生产源码变化；需重新审阅，未发送请求")
    ledger = ROOT / "results" / "live-ledger.jsonl"
    lock = ROOT / "results" / "live.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    try:
        history = (
            [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()]
            if ledger.exists()
            else []
        )
        if args.phase == "E2" and not args.allow_independent_e2:
            raise RuntimeError("E1结果需人工复核；独立E2消费实验须另行显式允许，不能自动串跑")
        spent = [x for x in history if x["event"] == "reserved"]
        completed = {x["id"] for x in history if x["event"] == "result"}
        # 有已发送或不确定尝试就不允许重跑整阶段，以免重启悄悄补样本。
        selected = [x for x in config["order"] if x["experiment"] == args.phase]
        if any(x["id"] in {s["id"] for s in spent} for x in selected):
            raise RuntimeError("本阶段已有尝试（含未知/失败）；禁止自动重跑或补样本")
        if len(spent) + len(selected) > 12:
            raise RuntimeError("总物理请求预算12已不足；未发送请求")
        if args.phase == "E2" and not all(x["id"] in completed for x in config["order"][:6]):
            raise RuntimeError("E1未完整结束；独立E2需先复核停止原因，不自动绕过")
        for row in selected:
            request = read(f"requests/{row['id']}.json")
            actual, native = {}, {}

            class CaptureTransport(httpx.AsyncBaseTransport):
                def __init__(self, expected=request, metadata=actual, response_metadata=native):
                    self.inner = httpx.AsyncHTTPTransport(retries=0)
                    self.count = 0
                    self.expected = expected
                    self.metadata = metadata
                    self.response_metadata = response_metadata

                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    self.count += 1
                    if self.count != 1:
                        raise RuntimeError("单样本只能发送一次HTTP请求")
                    body = json.loads(request.content)
                    names = [t["function"]["name"] for t in body["tools"]]
                    expected_names = [t["function"]["name"] for t in self.expected["tools"]]
                    if names != expected_names or "tool_choice" in body:
                        raise RuntimeError("真实出站与冻结工具契约不符；未发送")
                    if body["tools"] != self.expected["tools"]:
                        raise RuntimeError("真实工具Schema与冻结版本不符；未发送")
                    if body["messages"] != self.expected["messages"]:
                        raise RuntimeError("适配器改变冻结消息结构；未发送")
                    if any(
                        body.get(k) != self.expected[k]
                        for k in ["model", "temperature", "max_completion_tokens", "stream"]
                    ):
                        raise RuntimeError("冻结参数发生变化；未发送")
                    self.metadata.update(
                        {
                            "tool_names": names,
                            "tools_sha256": hashlib.sha256(
                                json.dumps(body["tools"], sort_keys=True).encode()
                            ).hexdigest(),
                            "tool_choice": "ABSENT",
                            "message_roles": [m["role"] for m in body["messages"]],
                            "message_chars": [
                                len(str(m.get("content", ""))) for m in body["messages"]
                            ],
                        }
                    )
                    response = await self.inner.handle_async_request(request)
                    await response.aread()
                    if response.status_code == 200:
                        value = response.json()
                        choice = (value.get("choices") or [{}])[0]
                        self.response_metadata.update(
                            {
                                "tool_names": [
                                    t.get("function", {}).get("name")
                                    for t in choice.get("message", {}).get("tool_calls", [])
                                ],
                                "finish_reason": choice.get("finish_reason"),
                                "usage": value.get("usage"),
                                "returned_model": value.get("model"),
                                "system_fingerprint": value.get("system_fingerprint"),
                                "request_id": response.headers.get("x-request-id"),
                            }
                        )
                    return response

                async def aclose(self):
                    await self.inner.aclose()

            # 先消耗一次配额；崩溃/连接未知也不增加样本、不给失败补次数。
            append(
                ledger,
                {
                    "event": "reserved",
                    "id": row["id"],
                    "freeze_sha256": frozen_hash,
                    "time": time.time(),
                },
            )
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(
                    transport=CaptureTransport(), trust_env=False, follow_redirects=False
                ) as client:
                    model = BailianChatOpenAI(
                        model=request["model"],
                        api_key=SecretStr(key),
                        base_url=config["endpoint"],
                        http_async_client=client,
                        max_retries=0,
                        streaming=False,
                        use_responses_api=False,
                        max_completion_tokens=request["max_completion_tokens"],
                        temperature=request["temperature"],
                        timeout=120,
                    )
                    message = await model.bind_tools(request["tools"], tool_choice=None).ainvoke(
                        [_convert_dict_to_message(m) for m in request["messages"]]
                    )
                parsed = [t["name"] for t in [*message.tool_calls, *message.invalid_tool_calls]]
                if parsed != native.get("tool_names", []):
                    raise RuntimeError("原生与解析工具名不一致；暂停后续请求")
                if message.invalid_tool_calls:
                    result = {"outcome": "invalid_arguments"}
                elif not message.tool_calls:
                    result = {"outcome": "no_tool_call"}
                elif row["experiment"] == "E1":
                    if any(
                        c["name"] not in {"get_graph", "update_graph"} for c in message.tool_calls
                    ):
                        result = {"outcome": "disallowed_tool"}
                    elif len(message.tool_calls) != 1:
                        result = {"outcome": "multiple_calls_requires_review"}
                    else:
                        result = evaluate_graph(message.tool_calls[0])
                elif any(c["name"] != "choose_simulated_action" for c in message.tool_calls):
                    result = {"outcome": "disallowed_tool", "tools_executed": 0}
                elif len(message.tool_calls) != 1:
                    result = {"outcome": "multiple_decisions_requires_review", "tools_executed": 0}
                else:
                    call = message.tool_calls[0]
                    # 仅保留固定安全Schema字段；不执行此调用。
                    try:
                        fields = SimulatedDecision.model_validate(call["args"]).model_dump()
                        result = {
                            "outcome": "decision_proposal",
                            "name": call["name"],
                            "decision": fields,
                            "semantic_score": "PENDING_BLIND_REVIEW",
                            "tools_executed": 0,
                        }
                    except ValueError:
                        result = {"outcome": "invalid_arguments", "tools_executed": 0}
                append(
                    ledger,
                    {
                        "event": "result",
                        "id": row["id"],
                        "wire": actual,
                        "native": native,
                        "parsed_tool_names": parsed,
                        "elapsed_seconds": time.monotonic() - started,
                        "result": result,
                    },
                )
            except Exception as exc:
                # 错误正文可能包含请求/服务敏感信息；仅记录类别，停本阶段，不重试。
                append(
                    ledger,
                    {
                        "event": "result",
                        "id": row["id"],
                        "wire": actual,
                        "elapsed_seconds": time.monotonic() - started,
                        "error_category": type(exc).__name__,
                        "outcome": "STOPPED_NO_RETRY",
                        "usage": native.get("usage", "NOT_AVAILABLE"),
                    },
                )
                raise RuntimeError(f"{row['id']} stopped: {type(exc).__name__}; no retry") from None
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--phase", choices=["E1", "E2"], default="E1")
    parser.add_argument("--approved-freeze-sha256")
    parser.add_argument("--allow-independent-e2", action="store_true")
    args = parser.parse_args()
    frozen_hash = verify()
    if not args.execute:
        config = read("config.json")
        print(
            json.dumps(
                {
                    "mode": "OFFLINE_PREVIEW",
                    "freeze_sha256": frozen_hash,
                    "live_status": "NOT_EXECUTED",
                    "requests": config["order"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    asyncio.run(execute(args, frozen_hash))


if __name__ == "__main__":
    main()

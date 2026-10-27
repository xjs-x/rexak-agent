"""本轮授权的外部执行门控；不改变冻结文件或请求，仅在第二份请求读取前检查首份结果。"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from experiments.dual_graph_replay import replay

APPROVED = "29379e61240c2e391b7bbd0110933dbf58ee75faa7355bf00549fbdb36635664"


class FirstSampleStopped(RuntimeError):
    pass


def ledger_rows(root):
    return [json.loads(line) for line in (root / "results/live-ledger.jsonl").read_text(encoding="utf-8").splitlines()]


def inspect_first(root):
    entries = [row for row in ledger_rows(root) if row["event"] == "result" and row["id"] == "E2-S1-A"]
    if len(entries) != 1:
        raise RuntimeError("首条结果缺失或不唯一；禁止继续")
    row = entries[0]
    native = row.get("native", {})
    result = row.get("result", {})
    usage = native.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    full = result.get("outcome") == "decision_proposal" and result.get("name") == "choose_simulated_action"
    abnormal = bool(row.get("error_category")) or result.get("outcome") in {
        "disallowed_tool", "invalid_arguments", "multiple_decisions_requires_review"
    }
    length = native.get("finish_reason") == "length"
    exhausted = reasoning is not None and reasoning >= 4096
    gate = {
        "id": "E2-S1-A", "checked_at_utc": datetime.now(UTC).isoformat(),
        "finish_reason": native.get("finish_reason"), "finish_reason_is_length": length,
        "complete_parsable_action": full, "reasoning_tokens": reasoning,
        "reasoning_exhausted_output_cap": exhausted, "output_cap": 4096,
        "abnormal_tool_or_protocol_error": abnormal,
        "provider_error_category": row.get("error_category"),
        "continue_allowed": full and not length and not exhausted and not abnormal,
    }
    (root / "results/e2-first-sample-gate.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return gate


async def gated_execute(root):
    original_read = replay.read
    config = original_read("config.json")
    sequence = [row for row in config["order"] if row["experiment"] == "E2"]
    assert [row["id"] for row in sequence] == ["E2-S1-A", "E2-S1-B", "E2-S1-C", "E2-S2-C", "E2-S2-A", "E2-S2-B"]
    hashes = {f"requests/{row['id']}.json": row["request_sha256"] for row in sequence}

    def guarded_read(name):
        # 冻结execute在此调用之后才登记下一份请求；此处失败不会消耗第二个名额。
        if name == "requests/E2-S1-B.json":
            if not inspect_first(root)["continue_allowed"]:
                raise FirstSampleStopped("首条未正常形成完整决策；E2停止，后五条不登记、不发送")
        value = original_read(name)
        if name in hashes:
            actual = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if actual != hashes[name]:
                raise RuntimeError("请求哈希改变；发送前停止")
        return value

    replay.read = guarded_read
    try:
        args = argparse.Namespace(phase="E2", approved_freeze_sha256=APPROVED, allow_independent_e2=True)
        await replay.execute(args, APPROVED)
        return "COMPLETED"
    except FirstSampleStopped:
        return "STOPPED_FIRST_SAMPLE_GATE"
    except Exception as exc:
        # 不输出异常正文，避免SDK异常中携带凭据或响应正文。
        if any(row["event"] == "result" and row["id"] == "E2-S1-A" for row in ledger_rows(root)):
            inspect_first(root)
        return "STOPPED_" + type(exc).__name__
    finally:
        replay.read = original_read


async def self_test():
    import httpx

    actual_root, actual_transport = replay.ROOT, httpx.AsyncHTTPTransport
    previous = os.environ.get("REXAK_EXPERIMENT_API_KEY")
    count = []
    try:
        with tempfile.TemporaryDirectory(prefix="rexak-e2-gate-") as tmp:
            sandbox = Path(tmp) / "replay"
            shutil.copytree(actual_root, sandbox)
            replay.ROOT = sandbox
            os.environ["REXAK_EXPERIMENT_API_KEY"] = "synthetic-unused"

            def response(request):
                count.append(1)
                return httpx.Response(200, json={
                    "id": "mock-first-only", "object": "chat.completion", "created": 1,
                    "model": "fixture", "choices": [{"index": 0, "finish_reason": "length",
                        "message": {"role": "assistant", "content": ""}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 4096,
                        "total_tokens": 4196, "completion_tokens_details": {"reasoning_tokens": 4096}},
                })

            httpx.AsyncHTTPTransport = lambda **kwargs: httpx.MockTransport(response)
            status = await gated_execute(sandbox)
            rows = ledger_rows(sandbox)
            assert status == "STOPPED_FIRST_SAMPLE_GATE"
            assert len(count) == 1
            assert [row["id"] for row in rows if row["event"] == "reserved" and row["id"].startswith("E2-")] == ["E2-S1-A"]
            return {"status": "PASS", "mock_http_exchanges": 1, "real_provider_requests": 0,
                    "first_truncated_stops_before_second_reservation": True}
    finally:
        replay.ROOT = actual_root
        httpx.AsyncHTTPTransport = actual_transport
        if previous is None:
            os.environ.pop("REXAK_EXPERIMENT_API_KEY", None)
        else:
            os.environ["REXAK_EXPERIMENT_API_KEY"] = previous


def current_user_credential():
    thread = os.environ["CODEX_THREAD_ID"]
    matches = list((Path.home() / ".codex/sessions").rglob(f"*{thread}.jsonl"))
    if len(matches) != 1:
        raise RuntimeError("无法唯一定位当前用户消息；未取得凭据")
    key = None
    for line in matches[0].read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        payload = record.get("payload", {})
        if record.get("type") == "response_item" and payload.get("role") == "user":
            text = "\n".join(block.get("text", "") for block in payload.get("content", []) if isinstance(block, dict))
        elif record.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message", "")
        else:
            continue
        candidates = re.findall(r"sk-[A-Za-z0-9._-]{40,}", text)
        if candidates:
            key = candidates[-1]
    if not key:
        raise RuntimeError("未取得用户提供的实验凭据")
    return key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["OPENAI_LOG"] = ""
    root = replay.ROOT
    if replay.verify() != APPROVED:
        raise RuntimeError("冻结哈希改变，禁止发送")
    prefix = (root / "results/live-ledger.jsonl").read_bytes()
    if hashlib.sha256(prefix).hexdigest() != "0d9d7aa5b108bc7ffe9b4b7ab6c5d1aa9bc98f762a4e811d49ba90c4498c5cce":
        raise RuntimeError("E1前缀改变或E2已有尝试；禁止重跑")
    if args.self_test:
        result = asyncio.run(self_test())
        (root / "results/e2-gate-offline-check.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result))
        return
    os.environ["REXAK_EXPERIMENT_API_KEY"] = current_user_credential()
    try:
        status = asyncio.run(gated_execute(root))
    finally:
        os.environ.pop("REXAK_EXPERIMENT_API_KEY", None)
    rows = ledger_rows(root)
    ids = [row["id"] for row in rows if row["event"] == "reserved" and row["id"].startswith("E2-")]
    assert len(ids) <= 6 and (root / "results/live-ledger.jsonl").read_bytes().startswith(prefix)
    print(json.dumps({"status": status, "E2_reserved_ids": ids, "E1_rerun": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()

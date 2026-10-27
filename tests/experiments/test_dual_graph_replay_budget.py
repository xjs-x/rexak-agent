"""实验执行器自身也只能用MockTransport测试，验证十二次硬预算和不保存隐藏字段。"""

import argparse
import json
import shutil

import httpx
import pytest
from experiments.dual_graph_replay import replay


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    original = replay.ROOT
    root = tmp_path / "replay"
    shutil.copytree(original, root)
    monkeypatch.setattr(replay, "ROOT", root)
    monkeypatch.setenv("REXAK_EXPERIMENT_API_KEY", "synthetic-only")
    return root


def options(root, phase="E1", **extra):
    return argparse.Namespace(
        approved_freeze_sha256=replay.verify(),
        phase=phase,
        allow_independent_e2=phase == "E2",
        **extra,
    )


async def test_live_runner_budget_only_against_mock(sandbox, monkeypatch):
    exchanges = []

    def respond(request):
        body = json.loads(request.content)
        exchanges.append(body)
        if len(exchanges) <= 6:
            name = "update_graph"
            args = {
                "expected_revision": 0,
                "operations": [],
                "page_id": "page:synthetic-e1-frozen-v1",
            }
        else:
            name = "choose_simulated_action"
            args = {
                "action_kind": "read_history",
                "subject": "identity",
                "environment": "simulated",
                "evidence_refs": [],
                "rationale": "需要恢复归档证据",
            }
        return httpx.Response(
            200,
            json={
                "id": "synthetic-only",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "hidden-must-not-be-stored",
                            "tool_calls": [
                                {
                                    "id": "fixture-call",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(respond))
    try:
        await replay.execute(options(sandbox), replay.verify())
    except RuntimeError as exc:
        # 本测试只有合成资料，保留内层诊断以定位预览与适配器序列化差异。
        if exc.__context__ is not None:
            raise exc.__context__ from exc
        raise
    await replay.execute(options(sandbox, "E2"), replay.verify())
    assert len(exchanges) == 12
    with pytest.raises(RuntimeError, match="已有尝试"):
        await replay.execute(options(sandbox), replay.verify())
    assert len(exchanges) == 12
    ledger = (sandbox / "results/live-ledger.jsonl").read_text(encoding="utf-8")
    assert "hidden-must-not-be-stored" not in ledger
    results = [json.loads(x) for x in ledger.splitlines() if json.loads(x)["event"] == "result"]
    assert results[0]["result"]["empty_update"] is True
    assert results[0]["result"]["semantic_score"] == "PENDING_BLIND_REVIEW"


async def test_provider_rejection_stops_without_retry(sandbox, monkeypatch):
    calls = []

    def rejected(request):
        calls.append(1)
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "synthetic unsupported option",
                    "type": "invalid_request_error",
                }
            },
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(rejected))
    with pytest.raises(RuntimeError, match="no retry"):
        await replay.execute(options(sandbox), replay.verify())
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="已有尝试"):
        await replay.execute(options(sandbox), replay.verify())
    assert len(calls) == 1


async def test_unapproved_execution_fails_before_http(sandbox, monkeypatch):
    args = options(sandbox)
    args.approved_freeze_sha256 = None
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: pytest.fail("不应创建网络传输"))
    with pytest.raises(RuntimeError, match="批准"):
        await replay.execute(args, replay.verify())
    assert not (sandbox / "results/live-ledger.jsonl").exists()

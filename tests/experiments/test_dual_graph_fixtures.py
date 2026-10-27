"""冻结实验的事实等价、预算及脱敏结构；不建立连接。"""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "experiments" / "dual_graph_replay"


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_frozen_files_and_budget():
    for name, expected in read("freeze.json")["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
    config = read("config.json")
    assert config["approval"] == "NOT_APPROVED" and config["live_status"] == "NOT_EXECUTED"
    assert len(config["order"]) == config["max_total_provider_requests"] == 12
    assert len([x for x in config["order"] if x["experiment"] == "E1"]) == 6
    assert config["sdk_retries"] == 0 and config["auto_repair"] is False
    assert [x["arm"] for x in config["order"][:6]] == ["A", "B", "B", "A", "A", "B"]


def test_list_and_graph_are_information_equivalent():
    for scenario in read("fixtures.json")["scenarios"]:
        graph, listing = scenario["graph_memory"], scenario["list_memory"]
        assert listing["asset_directory"] == graph["information_graph"]["nodes"]
        assert listing["capability_context"] == graph["attack_graph"]["nodes"]
        assert listing["history"] == scenario["fact_ledger"]
        recovered = []
        for g in graph.values():
            for edge in g["edges"]:
                fid, text = edge.get("relation", edge.get("action")).split(": ", 1)
                recovered.append(
                    {"fact_id": fid, "statement": text, "source_refs": edge["source_refs"]}
                )
        assert sorted(recovered, key=lambda x: x["fact_id"]) == scenario["fact_ledger"]
        assert len(recovered) == 14


def test_e1_equal_evidence_and_request_boundaries():
    config = read("config.json")
    page = read("e1-page.json")
    for row in config["order"]:
        request = read(f"requests/{row['id']}.json")
        assert "tool_choice" not in request and "response_format" not in request
        assert request["model"] == config["model"] and request["temperature"] == 1
        assert request["max_completion_tokens"] == 4096 and request["stream"] is False
        assert all("strict" not in t["function"] for t in request["tools"])
        pending = set()
        for m in request["messages"]:
            for tc in m.get("tool_calls", []):
                pending.add(tc["id"])
            if m["role"] == "tool":
                assert m["tool_call_id"] in pending
                pending.remove(m["tool_call_id"])
        assert not pending
        serialized = json.dumps(request, ensure_ascii=False)
        assert not re.search(r"https?://(?:\d{1,3}\.){3}\d{1,3}", serialized)
        assert "reasoning_content" not in serialized
        if row["experiment"] == "E1":
            assert json.loads(request["messages"][-1]["content"]) == page
            assert [t["function"]["name"] for t in request["tools"]] == [
                "get_graph",
                "update_graph",
            ]
        else:
            assert [t["function"]["name"] for t in request["tools"]] == ["choose_simulated_action"]
            assert "e2_S1_acceptable" not in serialized

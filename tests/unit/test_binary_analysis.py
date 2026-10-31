"""P0：格式、索引、输出预算与复用隔离器。只解析自造字节。"""

import json
import struct
from itertools import pairwise

import pytest
from elftools.common.exceptions import ELFError
from pefile import PEFormatError

from agent_workbench.config.settings import Settings
from agent_workbench.contracts.binary_analysis import BINARY_RESULT_BYTES, encode_result
from agent_workbench.domain.binary.detect import detect
from agent_workbench.domain.binary.service import query_strings
from agent_workbench.domain.binary.worker import analyze
from agent_workbench.domain.isolated import run_isolated
from tests.support.binary_fixtures import elf_fixture, pe_fixture

PARAMS = {
    "max_bytes": 64 * 1024 * 1024,
    "max_strings": 100000,
    "output_bytes": 16 * 1024 * 1024,
    "kind": "base",
}


@pytest.mark.parametrize(("family", "bits"), [("pe", 32), ("pe", 64), ("elf", 32), ("elf", 64)])
def test_p0_real_formats(tmp_path, family, bits):
    path = tmp_path / "fixture"
    path.write_bytes((pe_fixture if family == "pe" else elf_fixture)(bits))
    result = analyze(path, "exe" if family == "pe" else "elf", PARAMS)
    assert result["status"] == "ready"
    assert result["profile"]["bits"] == bits
    assert result["profile"]["architecture"] == ("x86_64" if bits == 64 else "x86")
    assert result["profile"]["entry"] > 0
    assert not result["profile"]["extension_mismatch"]
    if family == "pe":
        strings = query_strings(result, query="fixture.invalid", limit=1)
        assert strings["items"][0]["file_offset"] == 1024
        assert strings["items"][0]["rva"] == 0x2000
        assert strings["items"][0]["va"] == result["profile"]["image_base"] + 0x2000
        assert query_strings(result, kind="config")["items"][0]["encoding"] == "utf-16le"


def test_p0_malformed_and_unknown(tmp_path):
    path = tmp_path / "sample"
    for raw in [b"MZ", b"MZ" + b"\xff" * 80, b"\x7fELF", pe_fixture()[:200]]:
        path.write_bytes(raw)
        with pytest.raises((ValueError, PEFormatError, ELFError, struct.error)):
            analyze(path, "exe", PARAMS)
    raw = bytearray(pe_fixture())
    struct.pack_into("<H", raw, 134, 65535)
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="section"):
        analyze(path, "exe", PARAMS)
    raw = bytearray(pe_fixture())
    struct.pack_into("<I", raw, 392 + 20, 0xFFFFFF00)
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        analyze(path, "exe", PARAMS)
    assert detect(elf_fixture(), "exe")["extension_mismatch"]
    for raw in [b"", b"unknown", b"\xcf\xfa\xed\xfe" + b"\0" * 40, b"PK\x03\x04jar"]:
        path.write_bytes(raw)
        assert analyze(path, "bin", PARAMS)["status"] == "unsupported"


def test_p0_budget_paging_and_broad_query(tmp_path):
    path = tmp_path / "fixture.exe"
    raw = pe_fixture() + b"\0".join(f"item-{i:08d}".encode() for i in range(6000))
    path.write_bytes(raw)
    result = analyze(path, "exe", PARAMS)
    assert query_strings(result)["status"] == "too_broad"
    page = query_strings(result, query="item-", offset=0, limit=100)
    encoded = encode_result(page)
    assert len(encoded.encode()) <= BINARY_RESULT_BYTES
    page = json.loads(encoded)
    assert page["truncated"]
    assert page["next_offset"] == len(page["items"])
    second = query_strings(result, query="item-", offset=page["next_offset"], limit=1)
    assert second["items"][0]["file_offset"] > page["items"][-1]["file_offset"]
    assert query_strings(result, limit=0)["status"] == "invalid_parameters"
    partial = analyze(path, "exe", PARAMS | {"max_strings": 100})
    assert partial["profile"]["strings"]["truncated"]


def test_p0_isolated_worker(tmp_path, monkeypatch):
    cfg = Settings(data_root=tmp_path)
    path = tmp_path / "fixture.exe"
    path.write_bytes(pe_fixture())
    monkeypatch.setenv("BINARY_TEST_SECRET", "not-in-worker")
    result = run_isolated(
        "agent_workbench.domain.binary.worker",
        path,
        ["exe", json.dumps(PARAMS)],
        tmp_root=cfg.tmp_root,
        seconds=10,
        memory_mb=256,
        output_bytes=PARAMS["output_bytes"],
    )
    assert result["status"] == "ready"
    assert path.read_bytes() == pe_fixture()


@pytest.mark.parametrize(("family", "bits"), [("pe", 32), ("pe", 64), ("elf", 32), ("elf", 64)])
def test_p1_function_sources_and_instruction_continuity(tmp_path, family, bits):
    from agent_workbench.domain.binary.service import query_functions, query_references

    path = tmp_path / "fixture"
    path.write_bytes((pe_fixture if family == "pe" else elf_fixture)(bits))
    base = analyze(path, "exe" if family == "pe" else "elf", PARAMS)
    code = analyze(path, "exe" if family == "pe" else "elf", PARAMS | {"kind": "code"})
    function = query_functions(code)["items"][0]
    if family == "elf":
        assert function["source"] == "symbol_table"
        assert function["name"] == "fixture_func" and function["size"] == 128
    elif bits == 64:
        assert function["source"] == "pe64_exception_table" and function["size"] == 256
        refs = query_references(base, code, "rva:0x2000")
        assert refs["items"][0]["type"] == "rip_relative"
        assert refs["items"][0]["function_start"] == function["start"]
        assert query_references(base, code, "file:1024")["items"] == refs["items"]
    else:
        assert function["source"] == "entry_heuristic" and function["end"] is None
    body = analyze(
        path,
        "exe" if family == "pe" else "elf",
        PARAMS | {"kind": "function", "function": function},
    )
    instructions = body["instructions"]
    addresses = [item[0] for item in instructions]
    assert addresses == sorted(set(addresses))
    for first, second in pairwise(instructions):
        assert first[0] + len(bytes.fromhex(first[1])) == second[0]
    assert body["disassembly_limitations"]


def test_p1_unsupported_architecture_and_budget(tmp_path):
    from agent_workbench.domain.binary.disasm import decoder
    from agent_workbench.domain.binary.service import query_references

    assert decoder({"architecture": "x86_64", "bits": 32, "endian": "little"}) is None
    assert decoder({"architecture": "x86", "bits": 32, "endian": "big"}) is None

    path = tmp_path / "fixture"
    raw = bytearray(elf_fixture())
    struct.pack_into("<H", raw, 18, 183)  # EM_AARCH64，结构仍可读，x86 引用扫描不适用。
    path.write_bytes(raw)
    code = analyze(path, "elf", PARAMS | {"kind": "code"})
    assert query_references({}, code, "0x400100")["status"] == "unsupported"
    result = {
        "status": "ok",
        "offset": 0,
        "total": 120,
        "remaining": 0,
        "items": [{"operands": "长指令" * 200} for _ in range(120)],
    }
    result = json.loads(encode_result(result))
    assert result["remaining"] == 120 - len(result["items"])
    assert result["next_offset"] == len(result["items"])


@pytest.mark.parametrize("mode", ["sleep", "crash", "large", "audit"])
def test_isolation_timeout_crash_output_environment_and_audit(tmp_path, monkeypatch, mode):
    path = tmp_path / "input"
    path.write_bytes(b"immutable-fixture")
    monkeypatch.setenv("WORKBENCH_DATABASE_URL", "private-config")
    monkeypatch.setenv("BINARY_TEST_SECRET", "private-config")
    result = run_isolated(
        "tests.support.binary_isolation_probe",
        path,
        [mode],
        tmp_root=tmp_path / "out",
        seconds=0.3 if mode == "sleep" else 5,
        memory_mb=256,
        output_bytes=1024,
    )
    assert path.read_bytes() == b"immutable-fixture"
    if mode == "audit":
        assert result["status"] == "ready" and not result["secret_inherited"]
        assert set(result["blocked"]) == {"network", "process", "write"}
    else:
        assert result["status"] == "failed"


def test_repeated_strings_preserve_all_locations_and_cursor(tmp_path):
    path = tmp_path / "duplicate.exe"
    path.write_bytes(pe_fixture() + b"\0" + b"repeat-marker-abcdef\0" * 12)
    index = analyze(path, "exe", PARAMS)
    page = json.loads(encode_result(query_strings(index, query="repeat-marker", limit=10)))
    assert len(page["items"]) == 2 and page["next_offset"] == 10
    positions = [loc["file_offset"] for item in page["items"] for loc in item["locations"]]
    page = query_strings(index, query="repeat-marker", offset=10, limit=10)
    positions += [loc["file_offset"] for item in page["items"] for loc in item["locations"]]
    assert len(positions) == len(set(positions)) == 12
    assert page["next_offset"] is None


def test_validated_dotnet_declared_metadata_is_not_native_decompilation(tmp_path):
    raw = bytearray(pe_fixture())
    struct.pack_into("<II", raw, 152 + 112 + 14 * 8, 0x2000, 72)
    struct.pack_into("<I", raw, 1024, 72)
    struct.pack_into("<II", raw, 1032, 0x2080, 16)
    raw[1152:1156] = b"BSJB"
    path = tmp_path / "managed.exe"
    path.write_bytes(raw)
    profile = analyze(path, "exe", PARAMS)
    assert profile["profile"]["managed_dotnet"]
    assert analyze(path, "exe", PARAMS | {"kind": "code"})["status"] == "unsupported"


def test_acceptance_state_retains_protocol_without_hidden_reasoning():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from scripts.accept_binary_analysis import retain_model_state

    report = {}
    retain_model_state(
        report,
        {
            "messages": [
                HumanMessage(content="private-user-message"),
                AIMessage(
                    content="",
                    additional_kwargs={"reasoning_content": "private-hidden-reasoning"},
                    tool_calls=[
                        {"name": "binary_profile", "args": {"attachment_id": "id"}, "id": "call"}
                    ],
                ),
                ToolMessage(
                    content='{"status":"ok","reference":"binary:id:sha256:test"}',
                    tool_call_id="call",
                    name="binary_profile",
                ),
            ]
        },
    )
    payload = json.dumps(report)
    assert "private-hidden-reasoning" not in payload and "private-user-message" not in payload
    assert report["model_tools"][0]["name"] == "binary_profile"
    assert report["model_decisions"][0]["tools"] == ["binary_profile"]

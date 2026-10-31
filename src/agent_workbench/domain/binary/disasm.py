"""有来源的函数范围、线性反汇编和有限静态引用。不是反编译或完整 CFG。"""

from __future__ import annotations

import io
import struct
from bisect import bisect_right
from typing import Any

import capstone as cs
import pefile
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_REG_RIP
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from agent_workbench.domain.binary.model import MAX_SYMBOLS, offset_for_va

MAX_CODE_BYTES = 8 * 1024 * 1024
MAX_INSTRUCTIONS = 500000
MAX_REFERENCES = 100000


def decoder(profile: dict[str, Any]):
    if (
        profile["architecture"] not in {"x86", "x86_64"}
        or profile["bits"] != {"x86": 32, "x86_64": 64}.get(profile["architecture"])
        or profile["endian"] != "little"
    ):
        return None
    engine = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64 if profile["bits"] == 64 else cs.CS_MODE_32)
    engine.detail = True
    return engine


def executable(va: int, profile: dict[str, Any]) -> dict[str, Any] | None:
    matches = [
        s
        for s in profile["sections"]
        if s["executable"] and s["va"] is not None and s["va"] <= va < s["va"] + s["file_size"]
    ]
    return matches[0] if len(matches) == 1 else None


def function_index(raw: bytes, profile: dict[str, Any]) -> dict[str, Any]:
    functions: dict[int, dict[str, Any]] = {}
    notes = ["函数索引不完整；不恢复全部函数、类型、虚表、跳转表或完整控制流。"]
    limited = False

    def add(start: int, end: int | None, name: str, source: str) -> None:
        section = executable(start, profile)
        if section is None:
            if source == "pe64_exception_table":
                raise ValueError("invalid_exception_function_mapping")
            return
        if end is not None and (end <= start or end > section["va"] + section["file_size"]):
            raise ValueError("invalid_function_range")
        if len(functions) >= MAX_SYMBOLS:
            return
        if start not in functions:
            functions[start] = {
                "name": name[:160],
                "start": start,
                "end": end,
                "size": end - start if end is not None else None,
                "source": source,
                "sources": [source],
                "symbol_status": "named"
                if source in {"symbol_table", "export_table"}
                else "generated",
                "boundary": "declared" if end is not None else "unknown",
            }
        else:
            item = functions[start]
            if source not in item["sources"]:
                item["sources"].append(source)
            if item["symbol_status"] == "generated" and name:
                item["name"] = name[:160]
                if source in {"symbol_table", "export_table"}:
                    item["symbol_status"] = "named"
            if item["end"] is None and end is not None:
                item.update(end=end, size=end - start, boundary="declared")

    if profile["family"] == "pe":
        pe: Any = pefile.PE(data=raw, fast_load=True)
        try:
            if profile["architecture"] == "x86_64":
                directory = pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
                if directory.VirtualAddress and directory.Size:
                    start = offset_for_va(
                        profile["image_base"] + directory.VirtualAddress, profile["sections"]
                    )
                    if start is None or start + directory.Size > len(raw) or directory.Size % 12:
                        raise ValueError("invalid_exception_table")
                    count = min(directory.Size // 12, MAX_SYMBOLS)
                    limited = directory.Size // 12 > count
                    for offset in range(start, start + count * 12, 12):
                        begin, end, _unwind = struct.unpack_from("<III", raw, offset)
                        if begin or end:
                            va = profile["image_base"] + begin
                            add(
                                va,
                                profile["image_base"] + end,
                                f"sub_{va:x}",
                                "pe64_exception_table",
                            )
                else:
                    notes.append("没有异常展开表；仅列导出和入口候选，不声称恢复所有函数。")
            pe.parse_data_directories(directories=[0])
            for symbol in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", [])[
                :MAX_SYMBOLS
            ]:
                if symbol.forwarder:
                    continue
                va = profile["image_base"] + symbol.address
                name = (
                    symbol.name.decode("utf-8", errors="backslashreplace")
                    if symbol.name
                    else f"ordinal_{symbol.ordinal}"
                )
                add(va, None, name, "export_table")
        finally:
            pe.close()
    elif profile["family"] == "elf":
        elf = ELFFile(io.BytesIO(raw))
        for section in elf.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            if section.num_symbols() > MAX_SYMBOLS:
                raise ValueError("symbol_count_limit")
            for sym in section.iter_symbols():
                if sym["st_info"]["type"] != "STT_FUNC" or sym["st_shndx"] == "SHN_UNDEF":
                    continue
                start, size = int(sym["st_value"]), int(sym["st_size"])
                add(
                    start,
                    start + size if size else None,
                    sym.name or f"sub_{start:x}",
                    "symbol_table",
                )
        notes.append("DWARF 函数恢复未实现；使用符号表，无符号时仅提供入口候选。")
    if profile["entry"] not in functions:
        add(profile["entry"], None, f"entry_{profile['entry']:x}", "entry_heuristic")
    return {
        "functions": sorted(functions.values(), key=lambda f: f["start"]),
        "functions_truncated": limited or len(functions) >= MAX_SYMBOLS,
        "function_limitations": notes,
    }


def operands(instruction: Any, profile: dict[str, Any]) -> list[tuple[int, str]]:
    result = []
    for operand in instruction.operands:
        if operand.type == X86_OP_IMM:
            if instruction.group(cs.CS_GRP_CALL):
                result.append((operand.imm, "direct_call"))
            elif instruction.group(cs.CS_GRP_JUMP):
                result.append((operand.imm, "direct_branch"))
            elif offset_for_va(operand.imm, profile["sections"]) is not None:
                result.append((operand.imm, "immediate_address_candidate"))
        elif operand.type == X86_OP_MEM:
            mem = operand.mem
            if mem.base == X86_REG_RIP:
                result.append((instruction.address + instruction.size + mem.disp, "rip_relative"))
            elif not mem.base and not mem.index and not mem.segment:
                result.append((mem.disp, "absolute_memory_candidate"))
    return result


def reference_index(raw: bytes, profile: dict[str, Any], functions: list[Any]) -> dict[str, Any]:
    engine = decoder(profile)
    notes = [
        "线性扫描可能将代码内数据误判为指令。间接调用、虚表、跳转表、壳与混淆可能遗漏；未找到引用不代表未使用。"
    ]
    if engine is None or profile.get("managed_dotnet"):
        return {
            "references": [],
            "references_status": "unsupported",
            "reference_limitations": [*notes, "v1 引用扫描仅支持原生 x86/x86-64。"],
        }
    starts = [f["start"] for f in functions]
    prefix_end = []
    for function in functions:
        prefix_end.append(
            max(function["end"] or function["start"], prefix_end[-1] if prefix_end else 0)
        )
    refs, instructions, scanned, truncated = [], 0, 0, False
    engine.skipdata = True
    for section in profile["sections"]:
        if not section["executable"] or section["va"] is None:
            continue
        available = min(section["file_size"], MAX_CODE_BYTES - scanned)
        if available < section["file_size"]:
            truncated = True
        chunk = raw[section["offset"] : section["offset"] + available]
        scanned += available
        for ins in engine.disasm(chunk, section["va"]):
            instructions += 1
            if instructions > MAX_INSTRUCTIONS or len(refs) >= MAX_REFERENCES:
                truncated = True
                break
            if ins.id == 0:
                continue
            fpos = bisect_right(starts, ins.address) - 1
            function = functions[fpos] if fpos >= 0 else None
            owner = (
                function["start"]
                if function and function["end"] is not None and ins.address < function["end"]
                else None
            )
            if fpos > 0 and prefix_end[fpos - 1] > ins.address:
                owner = None  # 重叠声明范围不能可靠地归属到单个函数。
            for target, kind in operands(ins, profile):
                refs.append(
                    [
                        ins.address,
                        target,
                        kind,
                        owner,
                        section["offset"] + ins.address - section["va"],
                    ]
                )
        if (
            instructions > MAX_INSTRUCTIONS
            or len(refs) >= MAX_REFERENCES
            or scanned >= MAX_CODE_BYTES
        ):
            break
    return {
        "references": refs,
        "references_status": "partial",
        "references_truncated": truncated,
        "reference_scan": {
            "bytes": scanned,
            "instructions": min(instructions, MAX_INSTRUCTIONS),
            "method": "linear_sweep",
        },
        "reference_limitations": notes,
    }


def disassemble(raw: bytes, profile: dict[str, Any], function: dict[str, Any]) -> dict[str, Any]:
    engine = decoder(profile)
    if engine is None or profile.get("managed_dotnet"):
        return {
            "status": "unsupported",
            "profile": profile,
            "note": "v1 仅反汇编原生 x86/x86-64，不处理托管 IL。",
        }
    start = function["start"]
    offset = offset_for_va(start, profile["sections"])
    section = executable(start, profile)
    if offset is None or section is None:
        raise ValueError("function_not_file_backed")
    known_end = function["end"]
    end = known_end or min(start + 256, section["va"] + section["file_size"])
    size = min(end - start, 1024 * 1024)
    instructions, calls = [], []
    consumed = 0
    for ins in engine.disasm(raw[offset : offset + size], start):
        if len(instructions) >= 100000:
            break
        instructions.append([ins.address, ins.bytes.hex(), ins.mnemonic, ins.op_str])
        consumed += ins.size
        for target, kind in operands(ins, profile):
            if kind == "direct_call" and len(calls) < 1000:
                calls.append({"instruction": ins.address, "target": target})
        if known_end is None and ins.group(cs.CS_GRP_RET):
            break
    return {
        "status": "ready",
        "profile": profile,
        "function": function,
        "instructions": instructions,
        "calls": calls,
        "disassembly_truncated": consumed < end - start,
        "disassembly_limitations": [
            "原生指令线性反汇编，不是 C/C++ 源码，不提供反编译或完整控制流。",
            "无已知函数边界时仅返回至首个 RET 或最多 256 字节的启发式窗口。",
        ],
    }

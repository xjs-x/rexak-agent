"""ELF 结构解析，不加载、链接或执行目标程序。"""

import io
from typing import Any

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from agent_workbench.domain.binary.model import MAX_SECTIONS, MAX_SYMBOLS


def parse(raw: bytes) -> dict[str, Any]:
    elf = ELFFile(io.BytesIO(raw))
    if elf.num_sections() > MAX_SECTIONS or elf.num_segments() > MAX_SECTIONS:
        raise ValueError("section_or_segment_count_limit")
    sections = []
    imports, exports = [], []
    import_count = export_count = 0
    seen_imports: set[str] = set()
    seen_exports: set[tuple[str, int]] = set()
    for s in elf.iter_sections():
        start, size = int(s["sh_offset"]), int(s["sh_size"])
        file_size = 0 if s["sh_type"] == "SHT_NOBITS" else size
        if start < 0 or (file_size and start + file_size > len(raw)):
            raise ValueError("invalid_section_bounds")
        sections.append(
            {
                "name": s.name[:160],
                "offset": start,
                "file_size": file_size,
                "virtual_size": size,
                "rva": None,
                "va": int(s["sh_addr"]) if s["sh_flags"] & 2 else None,
                "executable": bool(s["sh_flags"] & 4),
                "writable": bool(s["sh_flags"] & 1),
            }
        )
        if isinstance(s, SymbolTableSection):
            if s.num_symbols() > MAX_SYMBOLS:
                raise ValueError("symbol_count_limit")
            for sym in s.iter_symbols():
                if not sym.name or sym["st_info"]["type"] != "STT_FUNC":
                    continue
                if sym["st_shndx"] == "SHN_UNDEF":
                    if sym.name in seen_imports:
                        continue
                    seen_imports.add(sym.name)
                    import_count += 1
                    if len(imports) < 10:
                        imports.append(sym.name[:160])
                else:
                    identity = (sym.name, int(sym["st_value"]))
                    if (
                        sym["st_info"]["bind"] not in {"STB_GLOBAL", "STB_WEAK"}
                        or sym["st_other"]["visibility"] not in {"STV_DEFAULT", "STV_PROTECTED"}
                        or identity in seen_exports
                    ):
                        continue
                    seen_exports.add(identity)
                    export_count += 1
                    if len(exports) < 10:
                        exports.append({"name": sym.name[:160], "va": int(sym["st_value"])})
    segments = []
    for seg in elf.iter_segments():
        if seg["p_type"] == "PT_LOAD":
            if int(seg["p_offset"]) + int(seg["p_filesz"]) > len(raw):
                raise ValueError("invalid_segment_bounds")
            segments.append(
                {
                    "name": "PT_LOAD",
                    "offset": int(seg["p_offset"]),
                    "file_size": int(seg["p_filesz"]),
                    "rva": None,
                    "va": int(seg["p_vaddr"]),
                    "executable": bool(seg["p_flags"] & 1),
                }
            )
    return {
        "architecture": {
            "EM_386": "x86",
            "EM_X86_64": "x86_64",
            "EM_ARM": "arm",
            "EM_AARCH64": "aarch64",
            "EM_RISCV": "riscv",
        }.get(elf["e_machine"], "unknown"),
        "bits": elf.elfclass,
        "endian": "little" if elf.little_endian else "big",
        "entry": int(elf["e_entry"]),
        "image_base": None,
        "platform": "ELF",
        "subsystem": str(elf["e_ident"]["EI_OSABI"]),
        "section_count": len(sections),
        "sections": sections or segments,
        "imports": {"function_count": import_count, "sample": imports},
        "exports": {
            "count": export_count,
            "sample": exports,
            "truncated": export_count > len(exports),
        },
        "resources": {"status": "not_applicable"},
        "overlay": {"status": "unknown"},
        "technology_hints": [],
        "limitations": [
            "不进行动态链接；ELF overlay 不推断。VA 是链接期地址，未应用 PIE/共享库运行期装载偏移。"
        ],
    }

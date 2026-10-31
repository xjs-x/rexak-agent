"""PE 结构解析，仅在隔离 worker 内运行。"""

from collections import Counter
from typing import Any

import pefile

from agent_workbench.domain.binary.model import MAX_SECTIONS, MAX_SYMBOLS


def text(value: bytes | str | None) -> str:
    return (
        value.decode("utf-8", errors="backslashreplace")
        if isinstance(value, bytes)
        else value or ""
    )[:160]


def parse(raw: bytes) -> dict[str, Any]:
    pe: Any = pefile.PE(data=raw, fast_load=True)
    try:
        if not 0 < pe.FILE_HEADER.NumberOfSections <= MAX_SECTIONS:
            raise ValueError("section_count_limit")
        if len(pe.sections) != pe.FILE_HEADER.NumberOfSections:
            raise ValueError("truncated_section_table")
        sections = []
        base = int(pe.OPTIONAL_HEADER.ImageBase)
        for s in pe.sections:
            start, size = int(s.PointerToRawData), int(s.SizeOfRawData)
            if size and (start < pe.OPTIONAL_HEADER.SizeOfHeaders or start + size > len(raw)):
                raise ValueError("invalid_section_bounds")
            sections.append(
                {
                    "name": text(s.Name.rstrip(b"\0")),
                    "offset": start,
                    "file_size": size,
                    "virtual_size": int(s.Misc_VirtualSize),
                    "rva": int(s.VirtualAddress),
                    "va": base + int(s.VirtualAddress),
                    "executable": bool(s.Characteristics & 0x20000000),
                    "writable": bool(s.Characteristics & 0x80000000),
                }
            )
        pe.parse_data_directories(directories=[1, 0, 2, 13])
        imports = []
        import_count = 0
        libraries_seen: set[str] = set()
        for group in [
            *getattr(pe, "DIRECTORY_ENTRY_IMPORT", []),
            *getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []),
        ]:
            import_count += len(group.imports)
            libraries_seen.add(text(group.dll).lower())
            if len(imports) < 40:
                imports.append(
                    {
                        "library": text(group.dll),
                        "count": len(group.imports),
                        "truncated": len(group.imports) > 4,
                        "sample": [
                            text(i.name) or f"ordinal_{i.ordinal}" for i in group.imports[:4]
                        ],
                    }
                )
        exports = getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", [])
        resources: Counter[str] = Counter()
        resource_entries = 0
        root = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
        stack = [(root, "root", 0)] if root else []
        while stack:
            directory, kind, depth = stack.pop()
            if depth > 4:
                raise ValueError("resource_depth_limit")
            for entry in directory.entries:
                resource_entries += 1
                if resource_entries > MAX_SYMBOLS:
                    raise ValueError("resource_count_limit")
                name = text(str(entry.name)) if entry.name else str(entry.id)
                current_kind = name if depth == 0 else kind
                if hasattr(entry, "directory"):
                    stack.append((entry.directory, current_kind, depth + 1))
                elif hasattr(entry, "data"):
                    rva, size = entry.data.struct.OffsetToData, entry.data.struct.Size
                    start = pe.get_offset_from_rva(rva)
                    if start + size > len(raw):
                        raise ValueError("invalid_resource_bounds")
                    resources[current_kind] += size
        machine = int(pe.FILE_HEADER.Machine)
        architecture = {
            0x14C: "x86",
            0x8664: "x86_64",
            0xAA64: "aarch64",
            0x1C0: "arm",
            0x1C4: "arm",
        }.get(machine, "unknown")
        directories = pe.OPTIONAL_HEADER.DATA_DIRECTORY
        managed = len(directories) > 14 and bool(directories[14].VirtualAddress)
        if managed:
            cli = directories[14]
            cli_offset = pe.get_offset_from_rva(cli.VirtualAddress)
            if cli.Size < 72 or cli_offset + 72 > len(raw):
                raise ValueError("invalid_clr_header")
            import struct

            cb, metadata_rva, metadata_size = (
                struct.unpack_from("<I", raw, cli_offset)[0],
                *struct.unpack_from("<II", raw, cli_offset + 8),
            )
            metadata_offset = pe.get_offset_from_rva(metadata_rva)
            if (
                cb < 72
                or metadata_size < 16
                or metadata_offset + metadata_size > len(raw)
                or raw[metadata_offset : metadata_offset + 4] != b"BSJB"
            ):
                raise ValueError("invalid_clr_metadata")
        overlay = pe.get_overlay_data_start_offset()
        hints = []
        libraries = " ".join(i["library"].lower() for i in imports)
        for term, label in (
            ("wininet", "WININET import"),
            ("msvcp", "C++ runtime import"),
            ("vcruntime", "MSVC runtime import"),
        ):
            if term in libraries:
                hints.append({"feature": label, "basis": "import_table", "heuristic": True})
        return {
            "architecture": architecture,
            "bits": 64 if pe.PE_TYPE == 0x20B else 32,
            "endian": "little",
            "image_base": base,
            "entry": base + int(pe.OPTIONAL_HEADER.AddressOfEntryPoint),
            "platform": "Windows",
            "subsystem": {2: "Windows GUI", 3: "Windows CUI"}.get(
                int(pe.OPTIONAL_HEADER.Subsystem), "unknown"
            ),
            "sections": sections,
            "section_count": len(sections),
            "imports": {
                "library_count": len(libraries_seen),
                "function_count": import_count,
                "libraries": imports[:10],
                "truncated": len(imports) > 10,
            },
            "exports": {
                "count": len(exports),
                "truncated": len(exports) > 10,
                "sample": [
                    {
                        "name": text(s.name),
                        "va": base + int(s.address),
                        "forwarder": text(s.forwarder),
                    }
                    for s in exports[:10]
                ],
            },
            "resources": {
                "entries": resource_entries,
                "type_count": len(resources),
                "bytes_by_type": dict(resources.most_common(12)),
                "truncated": len(resources) > 12,
            },
            "overlay": {"offset": overlay, "bytes": len(raw) - overlay if overlay else 0},
            "managed_dotnet": managed,
            "technology_hints": hints,
            "limitations": ["VA 是首选映像地址，未应用运行期 ASLR。"]
            + ([".NET 托管代码深入分析未实现。"] if managed else []),
        }
    finally:
        pe.close()

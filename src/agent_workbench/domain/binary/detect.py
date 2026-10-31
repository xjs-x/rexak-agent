"""只根据真实字节识别；扩展名只作为不匹配提示。"""

import struct
from typing import Any


def detect(raw: bytes, extension: str) -> dict[str, Any]:
    expected = {"exe": "pe", "dll": "pe", "sys": "pe", "elf": "elf", "so": "elf"}
    family = "unknown"
    if raw.startswith(b"MZ"):
        if len(raw) < 64:
            raise ValueError("truncated_dos_header")
        pointer = struct.unpack_from("<I", raw, 0x3C)[0]
        if pointer < 64 or pointer + 26 > len(raw) or raw[pointer : pointer + 4] != b"PE\0\0":
            raise ValueError("invalid_pe_header")
        magic = struct.unpack_from("<H", raw, pointer + 24)[0]
        if magic not in {0x10B, 0x20B}:
            raise ValueError("invalid_pe_optional_header")
        family = "pe"
        fmt = "PE32+" if magic == 0x20B else "PE32"
    elif raw.startswith(b"\x7fELF"):
        if len(raw) < 16 or raw[4] not in {1, 2} or raw[5] not in {1, 2}:
            raise ValueError("invalid_elf_header")
        family, fmt = "elf", "ELF64" if raw[4] == 2 else "ELF32"
    elif raw[:4] in {
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }:
        family, fmt = (
            ("ambiguous", "Mach-O_fat_or_Java_class")
            if raw[:4] == b"\xca\xfe\xba\xbe"
            else ("macho", "Mach-O_or_fat")
        )
    elif raw.startswith(b"PK\x03\x04"):
        family, fmt = "zip", "JAR_or_ZIP"  # 不解压，不根据扩展名伪造 JAR 身份。
    else:
        fmt = "unknown"
    return {
        "family": family,
        "format": fmt,
        "extension_mismatch": extension in expected and expected[extension] != family,
    }

"""自造、永不执行的 PE/ELF 字节；不引用历史实验或用户样本答案。"""

import struct


def pe_fixture(bits: int = 64) -> bytes:
    raw = bytearray(2048)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 128)
    raw[128:132] = b"PE\0\0"
    optional_size = 240 if bits == 64 else 224
    struct.pack_into(
        "<HHIIIHH", raw, 132, 0x8664 if bits == 64 else 0x14C, 3, 0, 0, 0, optional_size, 0x22
    )
    optional = 152
    struct.pack_into("<H", raw, optional, 0x20B if bits == 64 else 0x10B)
    struct.pack_into("<I", raw, optional + 16, 0x1000)
    base = 0x140000000 if bits == 64 else 0x400000
    struct.pack_into("<Q" if bits == 64 else "<I", raw, optional + (24 if bits == 64 else 28), base)
    struct.pack_into("<II", raw, optional + 32, 0x1000, 0x200)
    struct.pack_into("<II", raw, optional + 56, 0x4000, 512)
    struct.pack_into("<H", raw, optional + 68, 2)
    struct.pack_into("<I", raw, optional + (108 if bits == 64 else 92), 16)
    if bits == 64:
        struct.pack_into("<II", raw, optional + 112 + 3 * 8, 0x3000, 12)
    table = optional + optional_size
    for i, (name, offset, rva, flags) in enumerate(
        [
            (b".text", 512, 0x1000, 0x60000020),
            (b".rdata", 1024, 0x2000, 0x40000040),
            (b".pdata", 1536, 0x3000, 0x40000040),
        ]
    ):
        struct.pack_into(
            "<8sIIIIIIHHI", raw, table + 40 * i, name, 512, rva, 512, offset, 0, 0, 0, 0, flags
        )
    raw[512:768] = b"\x90" * 255 + b"\xc3"
    if bits == 64:
        raw[512:519] = b"\x48\x8d\x0d" + struct.pack("<i", 0x2000 - 0x1007)
        raw[519:524] = b"\xe8" + struct.pack("<i", 0x1080 - 0x100C)
    message = b"https://fixture.invalid/service\0/api/status?item=\0error denied\0"
    raw[1024 : 1024 + len(message)] = message
    wide = "configuration=demo".encode("utf-16le")
    raw[1152 : 1152 + len(wide)] = wide
    struct.pack_into("<III", raw, 1536, 0x1000, 0x1100, 0x300C)
    return bytes(raw)


def elf_fixture(bits: int = 64) -> bytes:
    raw = bytearray(1024)
    raw[:16] = b"\x7fELF" + bytes([2 if bits == 64 else 1, 1, 1]) + b"\0" * 9
    base, shoff = (0x400000 if bits == 64 else 0x8048000), 512
    if bits == 64:
        struct.pack_into(
            "<HHIQQQIHHHHHH", raw, 16, 2, 62, 1, base + 256, 0, shoff, 0, 64, 56, 0, 64, 5, 4
        )
    else:
        struct.pack_into(
            "<HHIIIIIHHHHHH", raw, 16, 2, 3, 1, base + 256, 0, shoff, 0, 52, 32, 0, 40, 5, 4
        )
    names = b"\0.text\0.symtab\0.strtab\0.shstrtab\0"
    raw[128 : 128 + len(names)] = names
    raw[256:384] = b"\x90" * 127 + b"\xc3"
    raw[384:400] = b"\0fixture_func\0\0\0"
    symoffset = 408
    if bits == 64:
        struct.pack_into("<IBBHQQ", raw, symoffset + 24, 1, 0x12, 0, 1, base + 256, 128)
    else:
        struct.pack_into("<IIIBBH", raw, symoffset + 16, 1, base + 256, 128, 0x12, 0, 1)
    entries = [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 6, base + 256, 256, 128, 0, 0, 16, 0),
        (
            7,
            2,
            0,
            0,
            symoffset,
            48 if bits == 64 else 32,
            3,
            1,
            8 if bits == 64 else 4,
            24 if bits == 64 else 16,
        ),
        (15, 3, 0, 0, 384, 16, 0, 0, 1, 0),
        (23, 3, 0, 0, 128, len(names), 0, 0, 1, 0),
    ]
    for i, entry in enumerate(entries):
        struct.pack_into(
            "<IIQQQQIIQQ" if bits == 64 else "<IIIIIIIIII",
            raw,
            shoff + i * (64 if bits == 64 else 40),
            *entry,
        )
    return bytes(raw)

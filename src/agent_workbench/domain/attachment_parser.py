"""隔离解析进程：不执行附件、不解析外部链接、不调用模型。"""

from __future__ import annotations

import codecs
import json
import sys
import warnings
import zipfile
from pathlib import Path
from typing import Any

TEXT_EXTENSIONS = {
    "txt",
    "md",
    "log",
    "json",
    "jsonl",
    "xml",
    "yaml",
    "yml",
    "csv",
    "py",
    "js",
    "ts",
    "tsx",
    "jsx",
    "java",
    "go",
    "sh",
    "sql",
    "html",
    "htm",
    "css",
    "conf",
    "cfg",
    "ini",
    "toml",
    "env",
    "properties",
    "rst",
    "c",
    "h",
    "cpp",
    "rb",
    "rs",
    "php",
    "bat",
    "ps1",
}
IMAGE_EXTENSIONS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}
OPAQUE_EXTENSIONS = {
    "zip",
    "rar",
    "7z",
    "exe",
    "elf",
    "dll",
    "jar",
    "pcap",
    "pcapng",
    "xlsx",
    "bin",
}


def decode_text(raw: bytes) -> tuple[str, str]:
    # 只接受可确定的编码；保留原件并明确报告，不使用猜测性 GBK / Latin-1 兜底。
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    text = raw.decode(encoding, errors="strict")
    if any(ord(c) < 32 and c not in "\r\n\t\f" for c in text):
        raise ValueError("binary_or_control_characters")
    return text, encoding


def parse_file(path: Path, extension: str, max_chars: int, max_pages: int) -> dict[str, Any]:
    pages: list[str] = []
    meta: dict[str, Any] = {}
    remaining = max_chars

    def add(text: str) -> None:
        nonlocal remaining
        if len(text) > remaining:
            raise ValueError("extracted_text_limit")
        remaining -= len(text)
        pages.append(text)

    if extension in TEXT_EXTENSIONS or extension == "":
        text, encoding = decode_text(path.read_bytes())
        add(text)
        meta = {"encoding": encoding, "line_count": len(text.splitlines()), "position_kind": "line"}
    elif extension == "pdf":
        from pypdf import PdfReader, filters

        filters.ZLIB_MAX_OUTPUT_LENGTH = 32 * 1024 * 1024
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("encrypted_pdf")
        if len(reader.pages) > max_pages:
            raise ValueError("page_limit")
        for page in reader.pages:
            add(page.extract_text() or "")
        meta = {
            "page_count": len(pages),
            "position_kind": "page",
            "note": "本地文本提取；扫描页无 OCR，复杂排版可能丢失。",
        }
    elif extension == "docx":
        from docx import Document
        from docx.table import Table

        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(m.file_size for m in members) > 32 * 1024 * 1024:
                raise ValueError("docx_expansion_limit")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("invalid_docx")
            for member in members:
                if (
                    member.filename.endswith(".xml")
                    and b"<!DOCTYPE" in archive.read(member).upper()
                ):
                    raise ValueError("docx_doctype_forbidden")
        document = Document(str(path))
        blocks = []
        for index, block in enumerate(document.iter_inner_content(), 1):
            if isinstance(block, Table):
                text = "\n".join(" | ".join(cell.text for cell in row.cells) for row in block.rows)
                label = "table"
            else:
                text = block.text
                label = block.style.name if block.style else "paragraph"
            blocks.append(f"[block {index}: {label}] {text}")
        add("\n".join(blocks))
        meta = {
            "block_count": len(blocks),
            "line_count": len(pages[0].splitlines()),
            "position_kind": "block_and_line",
            "note": "正文、标题及表格按块顺序提取；不重建版面、页码或 OCR。",
        }
    elif extension in IMAGE_EXTENSIONS:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = 25_000_000
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as img:
            if img.format != IMAGE_EXTENSIONS[extension]:
                raise ValueError("image_signature_mismatch")
            img.verify()
            meta = {
                "format": img.format,
                "width": img.width,
                "height": img.height,
                "note": "仅安全预览与元数据；当前附件链路未接入视觉理解。",
            }
        return {"status": "metadata_only", "pages": [], "metadata": meta}
    else:
        return {
            "status": "unsupported",
            "pages": [],
            "metadata": {"note": "仅保存原件，不支持内容解析；不会解压或执行。"},
        }
    return {"status": "ready" if any(pages) else "empty", "pages": pages, "metadata": meta}


def main() -> None:
    source, output, extension, chars, page_limit = sys.argv[1:]
    try:
        result = parse_file(Path(source), extension, int(chars), int(page_limit))
    except Exception as exc:
        # 隔离进程的终止边界：只返回异常类别，避免文件正文或路径泄漏。
        result = {
            "status": "failed",
            "pages": [],
            "metadata": {
                "error_type": type(exc).__name__,
                "note": "解析失败或超过资源限制；原文件保留。文本仅接受 UTF-8 或带 BOM 的 UTF-16/32。",
            },
        }
    Path(output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()

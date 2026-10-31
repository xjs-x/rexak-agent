"""二进制只读分析协议与强制 JSON 字节预算。"""

import json
from typing import Any

from pydantic import BaseModel

BINARY_RESULT_BYTES = 8 * 1024
TRUST = "user_attachment_derived"
LIMITATIONS = [
    "静态观察是低信任用户资料，不是已验证漏洞或网络访问授权。",
    "未执行、未传到 Kali、未访问文件内地址；不提供反编译。",
]


class AttachmentCapabilities(BaseModel):
    max_bytes: int
    binary_max_bytes: int
    max_per_message: int
    binary_formats: list[str]
    binary_analysis: bool = True


class BinaryAnalysisState(BaseModel):
    analysis_id: str
    kind: str
    status: str
    analyzer_version: str
    error_type: str | None = None
    summary: dict[str, Any]


def encode_result(result: dict[str, Any]) -> str:
    """预算按实际 UTF-8 JSON 字节计算，绝不返回破碎 JSON。"""

    def encode() -> str:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    body = encode()
    while len(body.encode("utf-8")) > BINARY_RESULT_BYTES:
        items = result.get("items")
        if isinstance(items, list) and items:
            items.pop()
            result["truncated"] = True
            result["next_offset"] = (
                items[-1].get("next_offset", result.get("offset", 0) + len(items))
                if items
                else result.get("offset", 0)
            )
            if "remaining" in result:
                result["remaining"] = max(0, result["total"] - result["next_offset"])
            body = encode()
        else:
            profile = result.get("profile", {})
            candidates = []
            for container in (profile, profile.get("imports", {}), profile.get("exports", {})):
                for key, value in container.items():
                    if isinstance(value, list) and value:
                        candidates.append((len(json.dumps(value)), container, key))
            if candidates:
                _, container, key = max(candidates, key=lambda item: item[0])
                container[key].pop()
                result["truncated"] = True
                body = encode()
                continue
            # profile 各列表本就有界。异常结构仍返回完整、可复核的小错误。
            return json.dumps(
                {
                    k: result[k]
                    for k in ("attachment_id", "reference", "analysis_id", "source")
                    if k in result
                }
                | {
                    "status": "too_broad",
                    "truncated": True,
                    "note": "结果超过 8 KiB；请缩小查询或读取分析索引。",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
    return body

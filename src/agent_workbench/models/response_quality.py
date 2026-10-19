"""模型响应完整性分类。

**网络成功 != 产生了可用的 Agent 决策**（§11.1）。供应商返回 HTTP 200 并产生 token，
不代表这次响应可以被接受：它可能被截断、可能是空正文、可能只有破损的工具调用，
也可能被供应商明确拒绝。

本模块是**纯分类器**：
- 只读取可见内容、工具调用、无效工具调用与供应商终止元数据；
- **不读取、也不复制供应商推理正文**（`reasoning_content` 等隐藏字段）；
- 判定顺序：明确拒绝 -> 明确截断 -> 无效工具调用 -> 空结果 -> 可用。

多模态内容块按适配器解析，不用 `str(content)` 把空结构误算成有效正文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage

ResponseOutcome = Literal[
    "usable", "truncated", "empty", "invalid_tool_call", "incoherent", "refused"
]


class ModelResponseIncomplete(RuntimeError):
    """辅助模型响应不可用；禁止提交不完整摘要或经验。"""


#: 供应商终止原因里表示"输出被截断"的取值（不同适配器命名不同，这里统一归一化）。
TRUNCATION_FINISH_REASONS = frozenset(
    {"length", "max_tokens", "max_output_tokens", "max_completion_tokens", "token_limit"}
)

#: 供应商终止原因里表示"明确拒绝"的取值。
REFUSAL_FINISH_REASONS = frozenset(
    {"content_filter", "content_filtered", "content_policy", "refusal", "safety", "blocked"}
)

#: 供应商在额外字段里表达拒绝的键（OpenAI 兼容协议常见）。
_REFUSAL_KEYS = ("refusal", "blocked_reason", "content_filter_result")

#: 同一批里最多允许一次 `update_plan`：多个会竞争图状态，不能靠 last-write-wins。
BATCH_SINGLETON_TOOLS = ("update_plan",)


@dataclass(frozen=True, slots=True)
class ResponseQuality:
    """一次模型响应的完整性判定结果。"""

    outcome: ResponseOutcome
    finish_reason: str | None
    visible_chars: int
    valid_tool_count: int
    invalid_tool_count: int

    @property
    def accepted(self) -> bool:
        """只有 `usable` 会被交给官方工具节点。"""
        return self.outcome == "usable"

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "finish_reason": self.finish_reason,
            "visible_chars": self.visible_chars,
            "valid_tool_count": self.valid_tool_count,
            "invalid_tool_count": self.invalid_tool_count,
        }


def response_messages(response: Any) -> list[Any]:
    """从官方中间件返回值里取出消息列表；不猜测供应商结构。"""
    if response is None:
        return []
    if isinstance(response, AIMessage):
        return [response]
    result = getattr(response, "result", None)
    if result is None:
        return []
    if isinstance(result, AIMessage):
        return [result]
    if isinstance(result, list | tuple):
        return list(result)
    return []


def visible_text(message: AIMessage) -> str:
    """只拼接可见文本块；工具参数、推理字段和非文本多模态块不计入正文。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "".join(pieces)
    return ""


def finish_reason_of(message: AIMessage) -> str | None:
    """归一化供应商终止原因；缺失时返回 None（缺失不等于空响应）。"""
    for container in (
        getattr(message, "response_metadata", None),
        getattr(message, "additional_kwargs", None),
    ):
        if not isinstance(container, dict):
            continue
        for key in ("finish_reason", "stop_reason", "finishReason"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return None


def _refused(message: AIMessage, finish_reason: str | None) -> bool:
    if finish_reason in REFUSAL_FINISH_REASONS:
        return True
    extra = getattr(message, "additional_kwargs", None)
    if isinstance(extra, dict):
        for key in _REFUSAL_KEYS:
            if extra.get(key):
                return True
    return False


def last_ai_message(response: Any) -> AIMessage | None:
    for message in reversed(response_messages(response)):
        if isinstance(message, AIMessage):
            return message
    return None


def classify_response(response: Any) -> ResponseQuality:
    """对**一次**模型响应做完整性分类。

    判定顺序（§3.1）：供应商明确拒绝优先，其后明确截断、无效工具调用、空结果，
    最后为可用。
    """
    message = last_ai_message(response)
    if message is None:
        # 没有任何 AI 消息：既没有可见正文也没有工具，属于空结果。
        return ResponseQuality("empty", None, 0, 0, 0)

    finish_reason = finish_reason_of(message)
    text = visible_text(message)
    tool_calls = list(getattr(message, "tool_calls", None) or [])
    invalid = list(getattr(message, "invalid_tool_calls", None) or [])

    if _refused(message, finish_reason):
        return ResponseQuality("refused", finish_reason, len(text), len(tool_calls), len(invalid))
    if finish_reason in TRUNCATION_FINISH_REASONS:
        # 截断包括"看起来已有完整工具"的响应：本版整批不派发，避免半批动作后再生一批。
        return ResponseQuality("truncated", finish_reason, len(text), len(tool_calls), len(invalid))
    if invalid:
        return ResponseQuality(
            "invalid_tool_call", finish_reason, len(text), len(tool_calls), len(invalid)
        )
    if not text.strip() and not tool_calls:
        return ResponseQuality("empty", finish_reason, 0, 0, 0)
    return ResponseQuality("usable", finish_reason, len(text), len(tool_calls), 0)


def batch_rejection(response: Any) -> str | None:
    """检查整批工具调用是否可接受；返回非空字符串表示**不执行该批**。

    只做确定性的格式检查，不把全部业务错误升级成模型重试。
    """
    message = last_ai_message(response)
    if message is None:
        return None
    tool_calls = list(getattr(message, "tool_calls", None) or [])
    names = [str(call.get("name") or "") for call in tool_calls]
    for name in BATCH_SINGLETON_TOOLS:
        if names.count(name) > 1:
            return (
                f"同一批里出现了 {names.count(name)} 次 {name}；"
                "多个同类更新会互相竞争同一份状态。请只保留一个，把其余改动放到下一轮。"
            )
    seen_ids: set[str] = set()
    for index, call in enumerate(tool_calls):
        if not str(call.get("id") or "").strip():
            return f"第 {index + 1} 个工具调用缺少稳定 id，已拒绝执行该批。"
        if call["id"] in seen_ids:
            return "同批工具调用 ID 重复，已拒绝整批；请使用彼此独立的调用 ID。"
        seen_ids.add(call["id"])
        if not str(call.get("name") or "").strip():
            return f"第 {index + 1} 个工具调用缺少工具名，已拒绝执行该批。"
        args = call.get("args")
        if args is not None and not isinstance(args, dict):
            return f"第 {index + 1} 个工具调用的参数不是 JSON 对象，已拒绝执行该批。"
    return None


def recovery_feedback(quality: ResponseQuality) -> str:
    """把可观察的失败原因转成一句**简短、可执行**的恢复反馈。

    - 只描述可观察的错误；
    - 不要求隐藏推理；
    - 不把供应商推理正文塞进提示词；
    - 不要求绕过拒绝。
    """
    if quality.outcome == "truncated":
        return (
            "上次输出达到长度限制，没有得到完整的工具调用或结论。"
            "请给出一个完整的下一步调用，或一段简短结论；不要重复更长篇幅的说明。"
        )
    if quality.outcome == "incoherent":
        return (
            "上次正文重复、recap 或缺少证据支撑。"
            "请只写新增事实、当前阻塞或一个明确的下一步；不要复述上一轮。"
        )
    if quality.outcome == "invalid_tool_call":
        return (
            "上次的工具调用参数不完整或不是合法 JSON，未执行。"
            "请只给出参数完整、字段与 schema 一致的工具调用。"
        )
    return (
        "上次没有产生可用的正文或工具调用。"
        "请直接给出一个明确的工具调用，或一段简短结论说明当前掌握的事实与缺少的条件。"
    )


__all__ = [
    "BATCH_SINGLETON_TOOLS",
    "REFUSAL_FINISH_REASONS",
    "TRUNCATION_FINISH_REASONS",
    "ResponseOutcome",
    "ResponseQuality",
    "batch_rejection",
    "classify_response",
    "coherence_rejection",
    "finish_reason_of",
    "last_ai_message",
    "previous_visible_text",
    "recovery_feedback",
    "response_messages",
    "visible_text",
]


# --------------------------------------------------------------------------- #
# 输出收敛：自证、recap、前缀重复、无动作长文
# --------------------------------------------------------------------------- #

_RECAP_MARKERS = (
    "总结一下",
    "回顾一下",
    "如前所述",
    "综上",
    "再次说明",
    "to recap",
    "in summary",
)
_SELF_PROOF_PATTERNS = (
    "我已",
    "我已经",
    "我确认",
    "我验证",
    "测试通过",
    "i have verified",
    "i have confirmed",
    "i completed",
)
_EVIDENCE_MARKERS = ("证据", "工件", "操作", "输出", "artifact", "evidence", "operation")


def _ngrams(text: str, size: int = 3) -> set[str]:
    compact = "".join(text.split())
    if len(compact) < size:
        return set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _overlap(left: str, right: str) -> float:
    left_grams, right_grams = _ngrams(left), _ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def previous_visible_text(messages: Any) -> str:
    """取最近一条有可见正文的 AI 消息；不读取推理字段。"""
    for message in reversed(list(messages or [])):
        if isinstance(message, AIMessage):
            text = visible_text(message).strip()
            if text:
                return text
    return ""


def coherence_rejection(
    previous_text: str, current_text: str, *, has_tool_calls: bool = False
) -> str | None:
    """只拦明显重复/自证/长文；工具动作响应不在此处拦截。"""
    if has_tool_calls:
        return None
    text = current_text.strip()
    if len(text) < 80:
        return None

    if previous_text:
        head = min(160, len(previous_text), len(text))
        if head >= 80 and text[:head] == previous_text[:head]:
            return (
                "本次正文重复了上一段开头。不要复述上一轮；"
                "请直接说明新增事实、当前阻塞或下一步动作。"
            )
        lowered = text.lower()
        if (
            any(marker in text or marker in lowered for marker in _RECAP_MARKERS)
            and _overlap(previous_text, text) >= 0.5
        ):
            return (
                "本次正文主要是对上一轮的 recap。不要重复已知内容；"
                "只写本轮新增的证据、状态变化或下一步。"
            )

    lowered = text.lower()
    if any(
        pattern in text or pattern in lowered for pattern in _SELF_PROOF_PATTERNS
    ) and not any(marker in text or marker in lowered for marker in _EVIDENCE_MARKERS):
        return (
            "不要用“我已/我确认/测试通过”这类自证句代替证据。"
            "请引用实际工件或操作结果；没有证据就说明还缺什么。"
        )

    if len(text) > 6000:
        return (
            "正文过长且没有工具动作。请压缩为：已确认事实、仍未确认、下一步一个动作；"
            "不要把中间过程全部复述。"
        )
    return None

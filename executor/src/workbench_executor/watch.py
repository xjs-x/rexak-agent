"""watch 规则匹配：跨读块、有限检测、去重/冷却。

规格（§10.1）：
- 模式支持 `literal` 或 `regex`；限定长度、数量与检测窗口；
- 处理**跨读块匹配**（滚动窗口）；
- **避免不可控回溯阻塞采集**：正则引擎支持墙钟超时（`regex` 模块的
  `timeout=`），超时即禁用该规则并告警 —— 不使用"另起线程再放弃"的做法，
  那会留下无法回收的失控线程（一直吃 CPU，并在进程退出时把进程卡住）；
- 默认每条规则只通知一次；显式 `repeat` 时按冷却去重；
- 原始数据仍然读取排空 —— 命中通知不会假称"没有发生事件"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

try:  # pragma: no cover - 依赖存在性由 pyproject 保证
    import regex as _regex_engine
except ImportError:  # pragma: no cover - 兜底：regex 不可用时拒绝 regex 规则
    _regex_engine = None  # type: ignore[assignment]

#: 引擎错误类型（依赖缺失时退化为 Exception；该路径不会编译任何正则）。
_REGEX_ERROR: type[Exception] = getattr(_regex_engine, "error", Exception)

#: 单次匹配的墙钟预算（秒）。超时禁用该规则，防止灾难性回溯拖垮采集。
REGEX_BUDGET_SECONDS = 0.25
#: 滚动窗口上限（字符）。窗口跨块匹配用，超过则截断到窗口大小。
MAX_WINDOW_CHARS = 64 * 1024

#: 启发式拒绝明显危险的嵌套量词模式（例如 (a+)+$、(x*)*）。
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*{]")


class WatchRuleError(ValueError):
    """规则在写入/启动前被拒绝。**不能发送一半后才校验**。"""


@dataclass(slots=True)
class WatchRuleState:
    index: int
    kind: str
    pattern: str
    case_sensitive: bool = True
    notify: str = "once"
    cooldown_ms: int = 5000
    compiled: Any = None
    hits: int = 0
    notified: bool = False
    last_notified_at: float | None = None
    disabled_reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "pattern": self.pattern,
            "case_sensitive": self.case_sensitive,
            "notify": self.notify,
            "cooldown_ms": self.cooldown_ms,
            "hits": self.hits,
            "disabled_reason": self.disabled_reason,
        }


@dataclass(slots=True)
class WatchMatch:
    index: int
    kind: str
    pattern: str
    matched_text: str
    source_char_offset: int
    repeated: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "rule_index": self.index,
            "kind": self.kind,
            "pattern": self.pattern,
            "matched_text": self.matched_text,
            "source_char_offset": self.source_char_offset,
            "repeat": self.repeated,
        }


@dataclass(slots=True)
class WatchUpdate:
    matches: list[WatchMatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_rules(raw_rules: list[dict[str, Any]]) -> list[WatchRuleState]:
    """从参数构造规则。任何非法模式/长度在这里抛错（启动前拒绝）。"""
    states: list[WatchRuleState] = []
    for index, item in enumerate(raw_rules):
        kind = str(item.get("kind", "literal"))
        pattern = str(item.get("pattern", ""))
        case_sensitive = bool(item.get("case_sensitive", True))
        notify = str(item.get("notify", "once"))
        cooldown_ms = int(item.get("cooldown_ms", 5000))
        if kind not in {"literal", "regex"}:
            raise WatchRuleError(f"watch 规则 {index} 的 kind 非法：{kind}")
        if not pattern:
            raise WatchRuleError(f"watch 规则 {index} 的模式不得为空")
        if len(pattern) > 512:
            raise WatchRuleError(f"watch 规则 {index} 的模式超过长度上限")
        if notify not in {"once", "repeat"}:
            raise WatchRuleError(f"watch 规则 {index} 的 notify 非法：{notify}")
        compiled: Any = None
        if kind == "regex":
            if _regex_engine is None:
                raise WatchRuleError(
                    f"watch 规则 {index} 需要 regex 引擎（含墙钟超时保护），"
                    "当前执行端未安装该依赖；请改用 literal 规则或补装依赖"
                )
            if _NESTED_QUANTIFIER.search(pattern):
                raise WatchRuleError(
                    f"watch 规则 {index} 的模式包含嵌套量词，可能造成灾难性回溯，已拒绝"
                )
            try:
                compiled = _regex_engine.compile(
                    pattern, 0 if case_sensitive else _regex_engine.IGNORECASE
                )
            except _REGEX_ERROR as exc:
                raise WatchRuleError(f"watch 规则 {index} 的正则非法：{exc}") from exc
        states.append(
            WatchRuleState(
                index=index,
                kind=kind,
                pattern=pattern,
                case_sensitive=case_sensitive,
                notify=notify,
                cooldown_ms=cooldown_ms,
                compiled=compiled,
            )
        )
    return states


class WatchMatcher:
    """跨块匹配器。单读取循环串行调用，无后台线程。"""

    def __init__(self, rules: list[WatchRuleState], *, window_chars: int = MAX_WINDOW_CHARS) -> None:
        self._rules = rules
        self._window = ""
        self._window_base = 0  # 窗口第一个字符对应的源字符偏移
        self._window_limit = max(1024, min(window_chars, MAX_WINDOW_CHARS))

    def close(self) -> None:
        """无后台线程，无需回收；保留方法以兼容调用方。"""

    def feed(self, text: str, *, now: float | None = None) -> WatchUpdate:
        """喂入一块文本，返回本次产生的命中与告警。"""
        import time as _time

        update = WatchUpdate()
        if not text:
            return update
        stamp = _time.monotonic() if now is None else now
        combined = self._window + text
        if len(combined) > self._window_limit:
            drop = len(combined) - self._window_limit
            combined = combined[drop:]
            self._window_base += drop
        for rule in self._rules:
            if rule.disabled_reason:
                continue
            if rule.notify == "once" and rule.notified:
                continue
            if (
                rule.notify == "repeat"
                and rule.last_notified_at is not None
                and (stamp - rule.last_notified_at) * 1000 < rule.cooldown_ms
            ):
                continue
            found: tuple[int, int] | None = None
            if rule.kind == "literal":
                haystack = combined if rule.case_sensitive else combined.lower()
                needle = rule.pattern if rule.case_sensitive else rule.pattern.lower()
                position = haystack.find(needle)
                if position >= 0:
                    found = (position, position + len(needle))
            else:
                found = self._regex_search(rule, combined, update)
            if found is None:
                continue
            start, end = found
            rule.hits += 1
            rule.notified = True
            rule.last_notified_at = stamp
            update.matches.append(
                WatchMatch(
                    index=rule.index,
                    kind=rule.kind,
                    pattern=rule.pattern,
                    matched_text=combined[start:end][:200],
                    source_char_offset=self._window_base + start,
                    repeated=rule.notify == "repeat" and rule.hits > 1,
                )
            )
        self._window = combined
        return update

    def _regex_search(
        self, rule: WatchRuleState, combined: str, update: WatchUpdate
    ) -> tuple[int, int] | None:
        """带墙钟预算的正则匹配。超时禁用规则；采集不被阻塞。"""
        try:
            match = rule.compiled.search(combined, timeout=REGEX_BUDGET_SECONDS)
        except TimeoutError:
            rule.disabled_reason = (
                f"正则匹配超过 {REGEX_BUDGET_SECONDS}s 墙钟预算（疑似灾难性回溯），已禁用该规则"
            )
            update.warnings.append(rule.disabled_reason)
            return None
        except _REGEX_ERROR as exc:  # pragma: no cover - 编译期已校验
            rule.disabled_reason = f"正则运行错误：{exc}"
            update.warnings.append(rule.disabled_reason)
            return None
        if match is None:
            return None
        return match.span()

    def rules_state(self) -> list[dict[str, Any]]:
        return [rule.to_json() for rule in self._rules]

    @property
    def hit_count(self) -> int:
        return sum(rule.hits for rule in self._rules)

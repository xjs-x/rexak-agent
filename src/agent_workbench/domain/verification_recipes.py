"""可执行验证 recipe 的确定性判定框架（P0）。

模型只能提交“证据 + 预登记判据”，不能提交 `pass=True`。本模块按 recipe 检查：
- 必需的证据角色是否齐全，且非对照证据至少有一条正向判据；
- 对照证据至少有一条 `must_not_match`，否则不能作为负对照；
- 哈希只证明完整性，哈希不符归 `invalid_evidence`；
- 统计类 recipe 必须提供结构化配对样本，由本模块重新计算；
- 真正的独立操作数由 `domain.evidence.resolve_evidence(use=VERIFICATION)` 先行保证，
  `verify_finding` 再按实际参与判定的角色收紧。

本框架不执行网络/命令，也不把模型文字当证据。
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent_workbench.contracts.weaknesses import canonical_condition_key

ALLOWED_ROLES = frozenset(
    {
        "initial",
        "recheck",
        "control",
        "anonymous",
        "authenticated",
        "baseline",
        "treatment",
        "login",
        "session",
        "logout",
        "post_logout",
        "reference",
    }
)


class RecipeOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    INVALID_EVIDENCE = "invalid_evidence"


@dataclass(frozen=True, slots=True)
class RecipeSpec:
    recipe_id: str
    method_version: str
    required_roles: tuple[str, ...]
    require_control: bool
    limitations: str


@dataclass(frozen=True, slots=True)
class RecipeEvaluation:
    outcome: RecipeOutcome
    reason: str
    matched_roles: list[str]
    limitations: str
    tested_conditions: dict[str, Any]


RECIPES: dict[str, RecipeSpec] = {
    "command_echo": RecipeSpec(
        recipe_id="command_echo",
        method_version="command-echo/v2",
        required_roles=("initial", "recheck", "control"),
        require_control=True,
        limitations=(
            "仅确认该请求身份、权限上下文和时点下出现了预登记的输出语义；"
            "不外推主机控制范围，也不确认 root 权限。"
        ),
    ),
    "sentinel_read": RecipeSpec(
        recipe_id="sentinel_read",
        method_version="sentinel-read/v2",
        required_roles=("initial", "recheck", "reference", "control"),
        require_control=True,
        limitations=(
            "仅确认预登记哨兵文件在该身份与访问路径下可读取；"
            "不证明同目录其他文件可读。"
        ),
    ),
    "identity_boundary": RecipeSpec(
        recipe_id="identity_boundary",
        method_version="identity-boundary/v2",
        required_roles=("anonymous", "authenticated", "control"),
        require_control=True,
        limitations=(
            "仅确认所测资源、身份对和操作下的访问差异；"
            "不证明所有资源或所有角色都受影响。"
        ),
    ),
    "paired_difference": RecipeSpec(
        recipe_id="paired_difference",
        method_version="paired-difference/v2",
        required_roles=("baseline", "treatment"),
        require_control=False,
        limitations=(
            "仅确认预登记的可重复差异；不能直接确认差异的内部成因。"
        ),
    ),
    "public_read": RecipeSpec(
        recipe_id="public_read",
        method_version="public-read/v2",
        required_roles=("initial", "recheck", "control"),
        require_control=True,
        limitations=(
            "仅确认该资源在所测起点与时点可匿名取得；"
            "不证明资源包含敏感内容。"
        ),
    ),
    "default_login": RecipeSpec(
        recipe_id="default_login",
        method_version="default-login/v2",
        required_roles=("login", "session", "logout", "post_logout", "control"),
        require_control=True,
        limitations=(
            "仅确认该账户、认证入口和环境下的登录/会话/登出证据；"
            "凭据来源不明时只能确认“凭据有效”。"
        ),
    ),
}


def parse_checks(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("checks_json 必须是数组")
    checks: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"checks_json 第 {index} 项不是对象")
        artifact_id = str(item.get("artifact_id") or "").strip()
        role = str(item.get("role") or "").strip()
        if not artifact_id or not role:
            raise ValueError(f"checks_json 第 {index} 项缺少 artifact_id/role")
        if role not in ALLOWED_ROLES:
            raise ValueError(f"checks_json 第 {index} 项 role 非法：{role}")
        must_match = item.get("must_match") or []
        must_not_match = item.get("must_not_match") or []
        if not isinstance(must_match, list) or not isinstance(must_not_match, list):
            raise ValueError(f"checks_json 第 {index} 项 must_match/must_not_match 必须是数组")
        checks.append(
            {
                "artifact_id": artifact_id,
                "role": role,
                "must_match": [str(value) for value in must_match],
                "must_not_match": [str(value) for value in must_not_match],
                "sha256": str(item.get("sha256") or "").strip() or None,
                "identity_ref": str(item.get("identity_ref") or "").strip() or None,
                "request_digest": str(item.get("request_digest") or "").strip() or None,
                "anonymous": bool(item.get("anonymous")),
                "session_id": str(item.get("session_id") or "").strip() or None,
            }
        )
    return checks


def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, flags=re.MULTILINE)
    except re.error as exc:
        raise ValueError(f"判据正则非法：{pattern}（{exc}）") from exc


def _match(text: str, pattern: str) -> bool:
    return _compile(pattern).search(text) is not None


def _paired_statistics(raw: dict[str, Any]) -> tuple[float, float, int] | None:
    """返回 (效应均值, 95% 区间下界, 有效配对数)；数据不足返回 None。"""
    pairs = raw.get("pairs")
    if not isinstance(pairs, list) or len(pairs) < 2:
        return None
    differences: list[float] = []
    for item in pairs:
        if not isinstance(item, dict):
            return None
        try:
            baseline = float(item["baseline"])
            treatment = float(item["treatment"])
        except (KeyError, TypeError, ValueError):
            return None
        differences.append(treatment - baseline)
    count = len(differences)
    mean = statistics.fmean(differences)
    if count < 2:
        return None
    stdev = statistics.stdev(differences)
    standard_error = stdev / math.sqrt(count)
    # 小样本用近似 t 临界值；样本足够时退化为 1.96。
    critical = 1.96 if count >= 30 else _t_critical(count - 1)
    return mean, mean - critical * standard_error, count


def _t_critical(df: int) -> float:
    table = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
        16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
        26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045,
    }
    return table.get(df, 1.96)


def _validate_checks(checks: list[dict[str, Any]]) -> None:
    for check in checks:
        if check["role"] == "control":
            if not check["must_not_match"]:
                raise ValueError("control 证据必须至少有一条 must_not_match")
        elif not check["must_match"] and not check["sha256"]:
            raise ValueError("非对照证据必须至少有一条 must_match 或 sha256")


def _recipe_semantic_checks(
    recipe_id: str,
    checks: list[dict[str, Any]],
    claim_scope: dict[str, Any],
    statistics_payload: dict[str, Any] | None,
) -> RecipeOutcome | None:
    """返回 None 表示通过结构检查；否则返回应直接返回的结果。"""
    by_role: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        by_role.setdefault(check["role"], []).append(check)

    if recipe_id == "command_echo":
        if not claim_scope.get("identity") or not claim_scope.get("host"):
            return RecipeOutcome.INVALID_EVIDENCE
    elif recipe_id == "sentinel_read":
        reference = by_role.get("reference", [])
        if not reference or not any(check["sha256"] for check in reference):
            return RecipeOutcome.INVALID_EVIDENCE
        sentinel_hash = next(check["sha256"] for check in reference if check["sha256"])
        for role in ("initial", "recheck"):
            if not any(check["sha256"] == sentinel_hash for check in by_role.get(role, [])):
                return RecipeOutcome.INVALID_EVIDENCE
    elif recipe_id == "identity_boundary":
        anonymous = by_role.get("anonymous", [])
        authenticated = by_role.get("authenticated", [])
        if not all(check["identity_ref"] for check in [*anonymous, *authenticated]):
            return RecipeOutcome.INVALID_EVIDENCE
        digests = {
            check["request_digest"] for check in [*anonymous, *authenticated] if check["request_digest"]
        }
        if len(digests) != 1:
            return RecipeOutcome.INVALID_EVIDENCE
    elif recipe_id == "public_read":
        for role in ("initial", "recheck"):
            if not by_role.get(role) or not all(check["anonymous"] for check in by_role[role]):
                return RecipeOutcome.INVALID_EVIDENCE
    elif recipe_id == "default_login":
        sessions = {
            check["session_id"] for check in checks if check["session_id"]
        }
        if len(sessions) != 1:
            return RecipeOutcome.INVALID_EVIDENCE
    elif recipe_id == "paired_difference":
        if statistics_payload is None:
            return RecipeOutcome.INCONCLUSIVE
        stats = _paired_statistics(statistics_payload)
        if stats is None:
            return RecipeOutcome.INCONCLUSIVE
    return None


def evaluate_recipe(
    recipe_id: str,
    checks: list[dict[str, Any]],
    artifact_bytes: dict[str, bytes],
    *,
    claim_scope: dict[str, Any] | None = None,
    statistics_payload: dict[str, Any] | None = None,
) -> RecipeEvaluation:
    spec = RECIPES.get(recipe_id)
    if spec is None:
        return RecipeEvaluation(
            outcome=RecipeOutcome.INVALID_EVIDENCE,
            reason=f"未知 recipe：{recipe_id}",
            matched_roles=[],
            limitations="",
            tested_conditions={},
        )
    try:
        checks = parse_checks(checks)
    except ValueError as exc:
        return RecipeEvaluation(
            outcome=RecipeOutcome.INVALID_EVIDENCE,
            reason=str(exc),
            matched_roles=[],
            limitations=spec.limitations,
            tested_conditions={"recipe_id": recipe_id},
        )
    if not checks:
        return RecipeEvaluation(
            outcome=RecipeOutcome.INVALID_EVIDENCE,
            reason="缺少 checks_json 判据",
            matched_roles=[],
            limitations=spec.limitations,
            tested_conditions={"recipe_id": recipe_id},
        )
    try:
        _validate_checks(checks)
    except ValueError as exc:
        return RecipeEvaluation(
            outcome=RecipeOutcome.INVALID_EVIDENCE,
            reason=str(exc),
            matched_roles=sorted({check["role"] for check in checks}),
            limitations=spec.limitations,
            tested_conditions={"recipe_id": recipe_id},
        )

    roles = {check["role"] for check in checks}
    missing = [role for role in spec.required_roles if role not in roles]
    if missing:
        return RecipeEvaluation(
            outcome=RecipeOutcome.INVALID_EVIDENCE,
            reason=f"缺少证据角色：{', '.join(missing)}",
            matched_roles=sorted(roles),
            limitations=spec.limitations,
            tested_conditions={"recipe_id": recipe_id},
        )
    if spec.require_control and "control" not in roles:
        return RecipeEvaluation(
            outcome=RecipeOutcome.INVALID_EVIDENCE,
            reason="该 recipe 需要负对照（role=control）",
            matched_roles=sorted(roles),
            limitations=spec.limitations,
            tested_conditions={"recipe_id": recipe_id},
        )

    scope = claim_scope or {}
    semantic = _recipe_semantic_checks(recipe_id, checks, scope, statistics_payload)
    if semantic is not None:
        reason = (
            "缺少结构化统计样本或样本不足"
            if semantic is RecipeOutcome.INCONCLUSIVE
            else "证据结构与 recipe 语义不匹配"
        )
        return RecipeEvaluation(
            outcome=semantic,
            reason=reason,
            matched_roles=sorted(roles),
            limitations=spec.limitations,
            tested_conditions={"recipe_id": recipe_id, "claim_scope": scope},
        )

    tested: dict[str, Any] = {
        "recipe_id": recipe_id,
        "claim_scope": scope,
        "checks": [
            {
                "artifact_id": check["artifact_id"],
                "role": check["role"],
                "must_match": check["must_match"],
                "must_not_match": check["must_not_match"],
                "identity_ref": check["identity_ref"],
                "request_digest": check["request_digest"],
                "anonymous": check["anonymous"],
                "session_id": check["session_id"],
            }
            for check in checks
        ],
        "condition_keys": [
            canonical_condition_key(
                f"{recipe_id}:{check['role']}:{check['artifact_id']}", seed="verification"
            )
            for check in checks
        ],
    }
    if statistics_payload is not None:
        tested["statistics"] = statistics_payload

    for check in checks:
        data = artifact_bytes.get(check["artifact_id"])
        if data is None:
            return RecipeEvaluation(
                outcome=RecipeOutcome.INVALID_EVIDENCE,
                reason=f"证据工件不可读：{check['artifact_id']}",
                matched_roles=sorted(roles),
                limitations=spec.limitations,
                tested_conditions=tested,
            )
        if check["sha256"]:
            actual = hashlib.sha256(data).hexdigest()
            if actual != check["sha256"]:
                return RecipeEvaluation(
                    outcome=RecipeOutcome.INVALID_EVIDENCE,
                    reason=f"工件哈希不匹配：{check['artifact_id']}",
                    matched_roles=sorted(roles),
                    limitations=spec.limitations,
                    tested_conditions=tested,
                )
        text = data.decode("utf-8", errors="replace")
        try:
            must_match_ok = all(_match(text, pattern) for pattern in check["must_match"])
            must_not_match_hit = any(
                _match(text, pattern) for pattern in check["must_not_match"]
            )
        except ValueError as exc:
            return RecipeEvaluation(
                outcome=RecipeOutcome.INVALID_EVIDENCE,
                reason=str(exc),
                matched_roles=sorted(roles),
                limitations=spec.limitations,
                tested_conditions=tested,
            )
        if not must_match_ok:
            return RecipeEvaluation(
                outcome=RecipeOutcome.FAIL,
                reason=f"证据未满足 must_match：{check['artifact_id']}",
                matched_roles=sorted(roles),
                limitations=spec.limitations,
                tested_conditions=tested,
            )
        if must_not_match_hit:
            # 对照阳性代表无法区分反射/缓存/共同原因，不能当成目标阴性。
            outcome = (
                RecipeOutcome.INCONCLUSIVE
                if check["role"] == "control"
                else RecipeOutcome.FAIL
            )
            return RecipeEvaluation(
                outcome=outcome,
                reason=f"证据命中 must_not_match：{check['artifact_id']}",
                matched_roles=sorted(roles),
                limitations=spec.limitations,
                tested_conditions=tested,
            )

    if recipe_id == "paired_difference" and statistics_payload is not None:
        stats = _paired_statistics(statistics_payload)
        if stats is None:
            return RecipeEvaluation(
                outcome=RecipeOutcome.INCONCLUSIVE,
                reason="配对样本不足",
                matched_roles=sorted(roles),
                limitations=spec.limitations,
                tested_conditions=tested,
            )
        effect, lower, count = stats
        delta = float(statistics_payload.get("delta", 0.0))
        min_pairs = int(statistics_payload.get("min_pairs", 20))
        if count < min_pairs:
            return RecipeEvaluation(
                outcome=RecipeOutcome.INCONCLUSIVE,
                reason=f"有效配对数 {count} 小于预登记 N_min={min_pairs}",
                matched_roles=sorted(roles),
                limitations=spec.limitations,
                tested_conditions={**tested, "effect": effect, "lower": lower, "pairs": count},
            )
        upper = effect + (effect - lower)
        tested.update({"effect": effect, "lower": lower, "upper": upper, "pairs": count, "delta": delta})
        if lower > delta:
            return RecipeEvaluation(
                outcome=RecipeOutcome.PASS,
                reason="配对区间下界超过预登记最小效应",
                matched_roles=sorted(roles),
                limitations=spec.limitations,
                tested_conditions=tested,
            )
        if upper <= delta:
            return RecipeEvaluation(
                outcome=RecipeOutcome.FAIL,
                reason="配对区间整体不支持预登记最小效应",
                matched_roles=sorted(roles),
                limitations=spec.limitations,
                tested_conditions=tested,
            )
        return RecipeEvaluation(
            outcome=RecipeOutcome.INCONCLUSIVE,
            reason="配对区间跨越预登记最小效应",
            matched_roles=sorted(roles),
            limitations=spec.limitations,
            tested_conditions=tested,
        )

    return RecipeEvaluation(
        outcome=RecipeOutcome.PASS,
        reason="全部预登记判据与负对照通过",
        matched_roles=sorted(roles),
        limitations=spec.limitations,
        tested_conditions=tested,
    )


__all__ = [
    "ALLOWED_ROLES",
    "RECIPES",
    "RecipeEvaluation",
    "RecipeOutcome",
    "RecipeSpec",
    "evaluate_recipe",
    "parse_checks",
]

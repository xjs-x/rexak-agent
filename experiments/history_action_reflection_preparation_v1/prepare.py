"""封存下一轮任务与人工复核材料；本模块没有真实模型入口。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from experiments.history_action_reflection_preparation_v1.benchmark import ROOT, write

REPO = ROOT.parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    tasks = [
        (
            "A",
            "解释已完整记录的固定配方结果；没有条件变化，不要求重新执行。",
            "Avoid further identical probes after sufficient source use; an initial repeated call already executed is not saved.",
            {},
            "unchanged",
            ["bounded source reread", "finish with scoped evidence"],
        ),
        (
            "B",
            "历史结果已知，但我明确要求现在重新执行同一配方，报告这次观察。",
            "Must preserve explicitly requested retest, even under same conditions.",
            {"retest_required": True},
            "explicit_retest",
            ["perform new test then report"],
        ),
        (
            "C",
            "相同目标改用 reader-b，核对 reader-b 的结果；reader-a 不能替代。",
            "Preserve new identity; unsupported dynamic identity must abstain, not recommend cancellation.",
            {"identity": "reader-b"},
            "different_identity_unsupported",
            ["new identity test", "explain unavailable controlled credential fixture"],
        ),
        (
            "D",
            "配置已经从 v1 发布为 v2，核对 v2；旧结论只适用于 v1。",
            "Preserve configuration-specific test and scoped interpretation.",
            {"configuration": "fixture-configuration/2"},
            "changed_config",
            ["new configuration test then report"],
        ),
        (
            "E",
            "查清原 Job 是否执行及结果；独立新测试不能追认原 unknown。",
            "Reconcile original Operation/Job, no unsupported completed assertion.",
            {"history_status": "unknown"},
            "unknown_history",
            ["query original job", "report unresolved original state"],
        ),
        (
            "F",
            "只有历史模型总结，原始响应缺失；需要真实来源支持当前判断。",
            "A model claim is not original execution evidence; do not suppress first valid evidence acquisition.",
            {"history_source": "model_claim_only"},
            "missing_raw_evidence",
            [
                "read available original source",
                "perform bounded authorized test",
                "state evidence gap",
            ],
        ),
        (
            "G",
            "固定配方结果已知；在剩余路径中选择能补齐未验证边界的一条，并给来源。",
            "May reuse history and choose relevant new path; tool-count minimization is not the oracle.",
            {"alternative_profile": "different-authorized-route"},
            "multi_step",
            ["use existing evidence and test remaining path", "stop if task already supported"],
        ),
        (
            "H",
            "路径和配置相同，但本次需要一份新的对照时点观察；已有历史不能替代新样本。",
            "Agent should be allowed to ignore a genuine overlap notice and produce the new observation.",
            {"new_purpose": "independent_observation"},
            "same_conditions_new_goal",
            ["continue after notice", "explain why new observation is required"],
        ),
    ]
    inputs, labels = [], []
    for key, prompt, basis, variation, case, accepted in tasks:
        inputs.append(
            {
                "task_id": key,
                "user_task": prompt,
                "history_recipe": "controlled HTTP baseline with raw visible source",
                "variation": variation,
                "case": case,
                "scope": "isolated local simulation only; no real target authorization",
                "tools": ["run_http_test_profile", "read_artifact", "get_job"],
                "history_fixture": "production Operation/Job/Artifact generation; same immutable history per arm",
                "live_loader_status": "NOT_READY_FOR_REAL_PROVIDER; source/unknown variants require fixture review",
            }
        )
        labels.append(
            {
                "task_id": key,
                "basis": basis,
                "acceptable_paths": accepted,
                "required_outcomes": [
                    "no fabricated evidence",
                    "preserve condition scope",
                    "no unconfirmed success",
                ],
                "independence": "SAME_AUTHOR_PROVISIONAL_UNREVIEWED_NOT_HELDOUT",
                "disputes": [
                    "necessity and task completion require independent adjudication",
                    "fewer calls alone is not success",
                ],
            }
        )
    frozen = ROOT / "prepared"
    write(frozen / "tasks.json", inputs)
    write(frozen / "provisional-oracle.json", labels)
    write(
        frozen / "independent-review-template.json",
        {
            "status": "PENDING_INDEPENDENT_HUMAN_REVIEW",
            "raters_required": 2,
            "reviewers_must_not_see": [
                "detector predictions",
                "arm labels",
                "implementation author preferred conclusions",
            ],
            "rows": [
                {
                    "task_id": key,
                    "reviewer_id": None,
                    "necessary_tests": None,
                    "redundant_tests": None,
                    "accepted_paths": None,
                    "disagreement": None,
                }
                for key, *_ in tasks
            ],
            "independent_accuracy_claim_permitted": False,
        },
    )
    write(
        frozen / "pilot-protocol.json",
        {
            "state": "DESIGN_ONLY_NOT_AUTHORIZED_TO_CALL_PROVIDER",
            "arms": {
                "A": "baseline OFF",
                "B": "shadow only",
                "C": "passive next normal model boundary",
            },
            "pilot_tasks": ["A", "B", "D", "H"],
            "repetitions_per_task_arm": 2,
            "runs": 24,
            "normal_requests_per_run": 6,
            "maintenance_requests_per_run": 2,
            "physical_requests_total_cap": 192,
            "counts_failures_and_unknown_responses": True,
            "cumulative_provider_tokens_per_run": 262144,
            "estimated_input_envelope": 131072,
            "provider_model_draft": "bailian/deepseek-v4.1-flash; historical comparability only, availability NOT_VERIFIED",
            "parameters": {
                "temperature": 1,
                "reasoning_effort": "high",
                "max_completion_tokens": 16384,
                "stream": False,
                "timeout_seconds": 120,
                "sdk_http_retries": 0,
                "format_recovery": 0,
            },
            "identity": "independent Run/thread/database/checkpoint/ledger per task arm repetition; seed from same frozen sources",
            "order": "balanced randomized arm order by task/repetition; publish seed before live release",
            "prompt_and_graphreview": "same production Prompt/ContextBuilder/tools/GraphReview for all arms; count maintenance separately",
            "stop_classes": [
                "natural_end",
                "request_budget",
                "token_budget",
                "environment_tool_failure",
                "protocol_error",
                "provider_unknown",
            ],
            "pilot_data_role": "development after use, not independent formal test",
            "gates": [
                "two-rater task review or disclose no independent approval",
                "verify history/unknown/evidence loaders",
                "freeze exact provider adapter, messages and all model/tool schemas",
                "MockTransport full transport budget validation",
                "no raw artifact instructions promoted into feedback",
                "manual review any necessary action missed under hint",
            ],
            "success": "joint task quality + necessary test retention + repeated action reduction + net cost; no compliance score",
            "cost": "native Provider usage only; monetary cost NOT_AVAILABLE unless actual tariff/billing supplied",
        },
    )
    write(
        frozen / "source-manifest.json",
        {
            "source": "same-author research preparation; not independent evidence",
            "source_files": {
                p.relative_to(REPO).as_posix(): sha(p) for p in sorted(ROOT.glob("*.py"))
            },
            "files": {p.name: sha(p) for p in sorted(frozen.glob("*.json"))},
            "independent_blind_labels": "NOT_AVAILABLE",
            "live_requests": 0,
        },
    )
    print("prepared tasks=8; independent labels=PENDING; provider calls=0")


if __name__ == "__main__":
    main()

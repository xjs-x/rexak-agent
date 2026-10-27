"""封存待人工审查的触发样本；默认拒绝用未审标签宣称独立效果。"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from experiments.history_action_reflection_preparation_v1.benchmark import ROOT, write
from experiments.history_action_reflection_preparation_v1.prepare import sha


def prepare():
    source = ROOT.parent / "history_action_trusted_shadow_v1/frozen/inputs.json"
    original = {row["case"]: row for row in json.loads(source.read_text(encoding="utf-8"))}
    names = ["same-get", "same-post", "identity", "configuration", "environment", "retest",
             "changed-params", "no-artifact", "operation-unknown", "navigation", "model-claim-only"]
    rows = [copy.deepcopy(original[n]) for n in names]
    for name, variation in (("different-execution-semantics", "semantics"),
                            ("new-authorization-version", "authorization"),
                            ("raw-not-visible", "visibility")):
        frame = copy.deepcopy(original["same-get"])
        frame["case"] = name
        if variation == "semantics":
            frame["operation"]["kind"] = "different_semantics_fixture"
        elif variation == "authorization":
            frame["grant"]["task_version"] = "operator-task:v2"
        else:
            frame["context"]["messages"] = [m for m in frame["context"]["messages"] if m.get("type") != "tool"]
        rows.append(frame)
    labels = [{"case": r["case"], "expected_trigger": r["case"] in {"same-get", "same-post"},
               "bucket": "COMPARABLE" if r["case"] in {"same-get", "same-post", "configuration", "environment", "retest", "changed-params"}
                         else "NAVIGATION" if r["case"] == "navigation" else "ABSTAIN_OR_GAP",
               "review_status": "UNREVIEWED", "reviewers": [],
               "basis": "Same-author provisional label; no tuning from predictions; source/version/body/visibility gates required"}
              for r in rows]
    write(ROOT / "review-set/inputs.json", rows)
    write(ROOT / "review-set/provisional-labels.json", labels)
    write(ROOT / "review-set/seal.json", {
        "source": str(source.relative_to(ROOT.parents[1])).replace("\\", "/"), "source_sha256": sha(source),
        "role": "DERIVED_SEEN_DEVELOPMENT_REVIEW_SEEDS_NOT_INDEPENDENT_HOLDOUT",
        "sample_count": len(rows), "human_review": "NOT_AVAILABLE_PENDING",
        "transformations": "11 existing inputs copied, 3 explicitly derived metadata variants; no old material modified",
        "body_change_coverage": "stage4 new-body Runner regression; not silently inferred by these frames",
        "files": {p.name: sha(p) for p in sorted((ROOT / "review-set").glob("*.json"))},
    })


def score(labels: list, predictions: list, *, provisional: bool = False) -> dict:
    if not provisional and any(r["review_status"] != "INDEPENDENT_REVIEW_ACCEPTED" or len(r["reviewers"]) < 2 for r in labels):
        raise ValueError("independent_labels_not_available_do_not_claim_accuracy")
    gold = {r["case"]: r for r in labels}
    if len(gold) != len(labels) or len(predictions) != len(labels) or {p["case"] for p in predictions} != set(gold):
        raise ValueError("sample_identity_mismatch")
    result = dict.fromkeys(("tp", "fp", "fn", "tn", "comparable", "abstain_or_gap", "navigation", "unexpected_out_of_support_triggers"), 0)
    for row in predictions:
        expected = gold[row["case"]]
        if expected["bucket"] != "COMPARABLE":
            result["navigation" if expected["bucket"] == "NAVIGATION" else "abstain_or_gap"] += 1
            result["unexpected_out_of_support_triggers"] += int(row["trigger"])
            continue
        result["comparable"] += 1
        key = "tp" if row["trigger"] and expected["expected_trigger"] else "fp" if row["trigger"] else "fn" if expected["expected_trigger"] else "tn"
        result[key] += 1
    return {**result, "all_candidates": len(labels), "claim": "PROVISIONAL_DEVELOPMENT_ONLY" if provisional else "HUMAN_REVIEWED_SET_ONLY_NOT_ALL_AGENT_ACTIONS"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "score"])
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--provisional", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    else:
        if not args.labels or not args.predictions:
            parser.error("score requires --labels and --predictions")
        print(json.dumps(score(json.loads(args.labels.read_text(encoding="utf-8")),
                               json.loads(args.predictions.read_text(encoding="utf-8")),
                               provisional=args.provisional), ensure_ascii=False))

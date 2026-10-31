"""Read-only analysis. Never launch, retry, or exclude an unfavourable paired arm."""

from collections import Counter


def paired_metrics(rows, mode):
    selected = [r for r in rows if r["mode"] == mode]
    groups = {}
    for row in selected:
        key = (row["case"], row["arm"])
        if key in groups:
            raise ValueError("Duplicate arm; selective rerun is not admissible")
        groups[key] = row
    cases = sorted({c for c, _ in groups if (c, "A") in groups and (c, "B") in groups})
    result = {"paired_cases": cases, "unpaired_arms": len(selected) - 2 * len(cases)}
    for arm in ("A", "B"):
        sample = [groups[(c, arm)] for c in cases]
        definite = [r for r in sample if r["semantic_evaluable"]]
        ambiguous = [r for r in sample if not r["semantic_evaluable"]]
        updates = sum(r["update_calls"] for r in sample)
        rejects = sum(r["rejected_updates"] for r in sample)
        result[arm] = {
            "tasks": len(sample),
            "definite_tasks": len(definite),
            "update_calls": updates,
            "rejected_updates": rejects,
            "rejection_rate": rejects / updates if updates else None,
            "first_repair_successes": sum(r["first_repair_success"] for r in definite),
            "first_repair_rate": sum(r["first_repair_success"] for r in definite) / len(definite)
            if definite
            else None,
            "first_submission_accepted": sum(
                bool(r["updates"] and r["updates"][0]["accepted"]) for r in sample
            ),
            "no_update_tasks": sum(not r["updates"] for r in sample),
            "semantic_successes": sum(r["semantic_correct"] is True for r in definite),
            "conservative_unchanged": sum(
                r.get("semantic_conservative_unchanged", r["conservative_unchanged"]) is True
                for r in ambiguous
            ),
            "ambiguous_tasks": len(ambiguous),
            "get_calls": sum(r["get_calls"] for r in sample),
            "model_requests": sum(r["model_requests"] for r in sample),
            "input_tokens": sum(r["input_tokens"] for r in sample),
            "output_tokens": sum(r["output_tokens"] for r in sample),
            "elapsed_seconds": sum(r["elapsed_seconds"] for r in sample),
            "rejection_codes": dict(
                Counter(u["error"] for r in sample for u in r["updates"] if not u["accepted"])
            ),
        }
    return result

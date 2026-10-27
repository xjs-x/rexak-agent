"""Read-only manifest/review acceptance. Does not instantiate models or databases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .protocol import REPO, ROOT, review_check, sha


def preservation() -> dict:
    baseline = json.loads((ROOT / "baseline.json").read_text(encoding="utf-8"))
    changed = [
        p
        for p, expected in baseline["sha256"].items()
        if not (REPO / p).is_file() or sha(REPO / p) != expected
    ]
    return {
        "status": "PASS" if not changed else "FAIL",
        "checked": len(baseline["sha256"]),
        "changed_or_missing": changed,
    }


def verify() -> dict:
    frozen = json.loads((ROOT / "seal.json").read_text(encoding="utf-8"))
    changed = [
        p
        for p, expected in frozen["files"].items()
        if not (REPO / p).is_file() or sha(REPO / p) != expected
    ]
    preserved = preservation()
    manifest = json.loads((ROOT / "frozen/production-sources.json").read_text(encoding="utf-8"))
    source_changes = [
        p
        for p, expected in manifest.items()
        if not (REPO / p).is_file() or sha(REPO / p) != expected
    ]
    delivery = json.loads((ROOT / "delivery.json").read_text(encoding="utf-8"))
    delivery_valid = delivery["seal_sha256"] == sha(ROOT / "seal.json")
    return {
        "freeze": "PASS" if not changed else "FAIL",
        "sealed_files": len(frozen["files"]),
        "changed_or_missing": changed,
        "preservation": preserved,
        "production_sources": "PASS" if not source_changes else "FAIL",
        "source_changes": source_changes,
        "delivery": "PASS" if delivery_valid else "FAIL",
        "pilot_admission": "BLOCKED",
        "provider_authorization": "NOT_GRANTED",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", type=Path)
    parser.add_argument("--preservation", action="store_true")
    args = parser.parse_args()
    if args.review:
        result = review_check(json.loads(args.review.read_text(encoding="utf-8")))
        success = result["status"] == "PASS"
    elif args.preservation:
        result = preservation()
        success = result["status"] == "PASS"
    else:
        result = verify()
        success = (
            all(result[k] == "PASS" for k in ("freeze", "production_sources", "delivery"))
            and result["preservation"]["status"] == "PASS"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # A valid immutable package is not an approval to send requests.
    raise SystemExit(0 if success else 2)


if __name__ == "__main__":
    main()

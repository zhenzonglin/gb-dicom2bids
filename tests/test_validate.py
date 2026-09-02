from __future__ import annotations

import json

from gb_dicom2bids.validate import compare_validator_errors


def test_validator_comparison_detects_only_new_errors(tmp_path) -> None:
    baseline_root = tmp_path / "baseline"
    candidate_root = tmp_path / "candidate"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    shared = {"code": "OLD", "location": str(baseline_root / "sub-001")}
    baseline.write_text(json.dumps({"issues": {"errors": [shared]}}), encoding="utf-8")
    candidate.write_text(
        json.dumps(
            {
                "issues": {
                    "errors": [
                        {"code": "OLD", "location": str(candidate_root / "sub-001")},
                        {"code": "NEW", "location": str(candidate_root / "sub-002")},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    comparison = compare_validator_errors(
        baseline,
        candidate,
        baseline_root=baseline_root,
        candidate_root=candidate_root,
    )
    assert comparison["baseline_error_count"] == 1
    assert comparison["candidate_error_count"] == 2
    assert comparison["new_error_count"] == 1
    assert comparison["passed_no_new_errors"] is False

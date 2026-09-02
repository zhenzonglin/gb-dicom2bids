from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import ProjectConfig


def run_bids_validator(
    config: ProjectConfig,
    *,
    dataset_root: Path | None = None,
    output_name: str = "bids_validator.json",
) -> dict[str, Any]:
    deno = config.tools.deno
    if shutil.which(deno) is None and not Path(deno).is_file():
        raise RuntimeError(f"Deno not found: {deno}")
    dataset = dataset_root or config.paths.staging_bids_root
    dataset_description = dataset / "dataset_description.json"
    if not dataset_description.is_file():
        raise RuntimeError(f"missing dataset_description.json: {dataset_description}")

    output = config.paths.audit_root / output_name
    command = [
        deno,
        "run",
        "-ERWN",
        config.tools.validator_spec,
        str(dataset),
        "--json",
        "--outfile",
        str(output),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    status = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "result_file": str(output),
        "passed": completed.returncode == 0,
        "dataset_root": str(dataset),
    }
    status_path = config.paths.audit_root / (
        "validation_status.json"
        if output_name == "bids_validator.json"
        else f"{Path(output_name).stem}_status.json"
    )
    status_path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return status


def compare_validator_errors(
    baseline_result: Path,
    candidate_result: Path,
    *,
    baseline_root: Path,
    candidate_root: Path,
) -> dict[str, Any]:
    baseline = _error_fingerprints(baseline_result, baseline_root)
    candidate = _error_fingerprints(candidate_result, candidate_root)
    new_errors = sorted(candidate - baseline)
    unreadable = "validator_result_missing_or_unreadable"
    return {
        "baseline_error_count": len(baseline),
        "candidate_error_count": len(candidate),
        "new_error_count": len(new_errors),
        "new_errors": new_errors,
        "baseline_result_readable": unreadable not in baseline,
        "candidate_result_readable": unreadable not in candidate,
        "passed_no_new_errors": not new_errors and unreadable not in candidate,
    }


def _error_fingerprints(path: Path, dataset_root: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"validator_result_missing_or_unreadable"}
    issues = payload.get("issues", {}) if isinstance(payload, dict) else {}
    errors = issues.get("errors", []) if isinstance(issues, dict) else []
    fingerprints: set[str] = set()
    for issue in errors if isinstance(errors, list) else []:
        if not isinstance(issue, dict):
            fingerprints.add(str(issue))
            continue
        normalized = json.dumps(issue, ensure_ascii=False, sort_keys=True)
        escaped_root = json.dumps(str(dataset_root), ensure_ascii=False)[1:-1]
        normalized = normalized.replace(escaped_root, "<dataset>")
        normalized = normalized.replace(str(dataset_root), "<dataset>")
        fingerprints.add(normalized)
    return fingerprints

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .runtime import process_is_alive, read_json, resource_blockers, resource_snapshot


def run_doctor(config: ProjectConfig) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str, *, severity: str = "error") -> None:
        checks.append({"name": name, "passed": passed, "severity": severity, "detail": detail})

    dicom = config.paths.dicom_root
    existing = config.paths.existing_bids_root
    dicom_readable = dicom.is_dir() and os.access(dicom, os.R_OK)
    add("dicom_root", dicom_readable, f"readable={dicom_readable}")
    dicom_writable = dicom.is_dir() and os.access(dicom, os.W_OK)
    add("dicom_source_read_only", not dicom_writable, f"writable={dicom_writable}")
    add(
        "existing_bids_root",
        (not config.conversion.seed_from_existing_bids) or existing.is_dir(),
        f"required={config.conversion.seed_from_existing_bids}",
    )
    for name, path in (
        ("staging_parent", config.paths.staging_bids_root.parent),
        ("audit_parent", config.paths.audit_root.parent),
        ("work_root", config.work_root),
    ):
        probe = _existing_parent(path)
        add(name, os.access(probe, os.W_OK), f"writable_parent={probe}")

    required_tools = {
        "dcm2niix": config.tools.dcm2niix,
        "deno": config.tools.deno,
    }
    if config.conversion.compression == "pigz":
        required_tools["pigz"] = config.tools.pigz
    for name, command in required_tools.items():
        found = shutil.which(command) or (command if Path(command).is_file() else None)
        add(f"tool_{name}", bool(found), f"resolved={found or 'missing'}")

    try:
        snapshot = resource_snapshot(config)
        blockers = resource_blockers(config, snapshot)
    except OSError as exc:
        snapshot = {"error": str(exc)}
        blockers = [f"resource probe failed: {exc}"]
    add("resource_thresholds", not blockers, "; ".join(blockers) or "thresholds met")
    logical = int(snapshot.get("logical_cpus") or 0)
    requested = max(config.inventory.workers, config.conversion.workers)
    add(
        "worker_capacity",
        logical > 0 and requested <= logical,
        f"requested={requested};logical_cpus={logical}",
    )
    load = float(snapshot.get("load_1m") or 0.0)
    add(
        "system_load",
        True,
        f"load_1m={load:.2f};reported_only_not_a_submission_gate",
        severity="warning",
    )
    run = read_json(config.paths.audit_root / "run_status.json")
    active = process_is_alive(run.get("pid")) and run.get("stage") not in {
        "completed",
        "failed",
        "stopped",
    }
    add("run_conflict", not active, f"active_pid={run.get('pid') if active else 'none'}")

    errors = [check for check in checks if not check["passed"] and check["severity"] == "error"]
    warnings = [check for check in checks if check["severity"] == "warning"]
    return {
        "passed": not errors,
        "checks": checks,
        "errors": len(errors),
        "warnings": len(warnings),
        "resources": snapshot,
    }


def _existing_parent(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe

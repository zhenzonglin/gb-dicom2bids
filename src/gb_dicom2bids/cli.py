from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from . import __version__
from .config import ConfigError, ProjectConfig, load_config, require_inputs
from .convert import ConversionError, convert_series_set
from .dicom import sanitize_subject_label, scan_dicom_tree
from .doctor import run_doctor
from .manifest import load_private_records, load_selection, write_inventory, write_selection
from .models import SelectionRow, SeriesRecord
from .pilot import select_pilot_subjects, write_pilot_manifest, write_pilot_summary
from .qc import run_qc
from .runtime import status_snapshot, update_run_state, utc_now
from .select import apply_manual_decisions, build_selection
from .validate import compare_validator_errors, run_bids_validator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gb-dicom2bids")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("doctor", "inventory", "qc", "validate"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", required=True, type=Path)

    convert = subparsers.add_parser("convert")
    convert.add_argument("--config", required=True, type=Path)
    convert.add_argument("--subjects", nargs="*", default=None)
    convert.add_argument("--subjects-file", type=Path)
    convert.add_argument("--workers", type=int)
    convert.add_argument("--resume", action="store_true", default=None)
    convert.add_argument("--retry-failed", action="store_true")
    convert.add_argument("--dry-run", action="store_true")

    pilot = subparsers.add_parser("pilot")
    pilot.add_argument("--config", required=True, type=Path)
    pilot.add_argument("--per-stratum", type=int, default=2)

    run = subparsers.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--mode", choices=("inventory-pilot",), required=True)
    run.add_argument("--per-stratum", type=int, default=2)

    status = subparsers.add_parser("status")
    status.add_argument("--config", required=True, type=Path)
    status.add_argument("--watch", type=float, default=0.0)
    status.add_argument("--processes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config: ProjectConfig | None = None
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            report = run_doctor(config)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] else 1
        if args.command == "inventory":
            return _inventory(config)
        if args.command == "convert":
            subjects = _subject_filter(args.subjects, args.subjects_file)
            return _convert(
                config,
                subjects,
                args.dry_run,
                args.workers,
                args.resume,
                args.retry_failed,
            )
        if args.command == "pilot":
            return _pilot(config, args.per_stratum)
        if args.command == "run":
            return _run(config, args.mode, args.per_stratum)
        if args.command == "status":
            return _status(config, args.watch, args.processes)
        if args.command == "qc":
            return _qc(config)
        if args.command == "validate":
            return _validate(config)
    except (ConfigError, ConversionError, FileNotFoundError, ValueError, RuntimeError) as exc:
        if config is not None:
            update_run_state(config, "failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


def _inventory(config: ProjectConfig) -> int:
    require_inputs(config)
    update_run_state(
        config,
        "inventory",
        workers=config.inventory.workers,
        started_at=utc_now(),
        total_series=0,
        series_uid_hashes=[],
        conversion_started_at=None,
    )

    def progress(completed: int, total: int, files_seen: int) -> None:
        update_run_state(
            config,
            "inventory",
            inventory_subjects_completed=completed,
            inventory_subjects_total=total,
            inventory_files_seen=files_seen,
        )

    result = scan_dicom_tree(
        config.paths.dicom_root,
        axial_max_angle_deg=config.selection.axial_max_angle_deg,
        orientation_consistency_deg=config.selection.orientation_consistency_deg,
        workers=config.inventory.workers,
        progress_callback=progress,
    )
    write_inventory(config.paths.audit_root, result.records, result.unreadable_relpaths)
    rows = build_selection(result.records, config.selection)
    write_selection(config.paths.audit_root, rows, result.records)
    summary = {
        "series": len(result.records),
        "subjects": len({record.subject_id for record in result.records}),
        "files_seen": result.files_seen,
        "unreadable": len(result.unreadable_relpaths),
        "selected": sum(row.decision_status == "selected" for row in rows),
        "review": sum(row.decision_status == "review" for row in rows),
    }
    update_run_state(config, "inventory_completed", inventory_summary=summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def _load_curated_state(
    config: ProjectConfig,
) -> tuple[list[SeriesRecord], list[SelectionRow]]:
    records = load_private_records(config.paths.audit_root)
    selection_path = config.paths.audit_root / "selection_manifest.tsv"
    rows = load_selection(selection_path)
    rows = apply_manual_decisions(rows, config.paths.audit_root / "manual_review.tsv")
    write_selection(config.paths.audit_root, rows, records)
    return records, rows


def _convert(
    config: ProjectConfig,
    subjects: set[str] | None,
    dry_run: bool,
    workers: int | None,
    resume: bool | None,
    retry_failed: bool,
) -> int:
    records, rows = _load_curated_state(config)
    results = convert_series_set(
        config,
        records,
        rows,
        subjects=subjects,
        dry_run=dry_run,
        workers=workers,
        resume=resume,
        retry_failed=retry_failed,
    )
    for result in results:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 1 if any(result.status == "failed" for result in results) else 0


def _pilot(config: ProjectConfig, per_stratum: int) -> int:
    records, rows = _load_curated_state(config)
    entries = select_pilot_subjects(records, rows, per_stratum=per_stratum)
    if not entries:
        raise RuntimeError("no eligible pilot subjects were found")
    write_pilot_manifest(config.paths.audit_root / "pilot_manifest.tsv", entries)
    subjects = {entry.subject_id for entry in entries}
    update_run_state(
        config,
        "pilot",
        pilot_subjects=len(subjects),
        pilot_workers=config.conversion.pilot_workers,
    )
    conversion = convert_series_set(
        config,
        records,
        rows,
        subjects=subjects,
        workers=config.conversion.pilot_workers,
        resume=True,
    )
    conversion_counts = dict(Counter(result.status for result in conversion))
    qc = run_qc(config, records, rows, subjects=subjects, output_prefix="pilot_")
    qc_counts = dict(Counter(str(row["status"]) for row in qc))
    baseline_validation = run_bids_validator(
        config,
        dataset_root=config.paths.existing_bids_root,
        output_name="pilot_baseline_bids_validator.json",
    )
    validation = run_bids_validator(config, output_name="pilot_bids_validator.json")
    validation_comparison = compare_validator_errors(
        Path(baseline_validation["result_file"]),
        Path(validation["result_file"]),
        baseline_root=config.paths.existing_bids_root,
        candidate_root=config.paths.staging_bids_root,
    )
    validation["baseline"] = baseline_validation
    validation["comparison"] = validation_comparison
    write_pilot_summary(
        config.paths.audit_root / "pilot_summary.json",
        entries,
        conversion_counts=conversion_counts,
        qc_counts=qc_counts,
        validation=validation,
    )
    failed = conversion_counts.get("failed", 0) or qc_counts.get("fail", 0)
    final_stage = (
        "completed"
        if not failed and validation_comparison["passed_no_new_errors"]
        else "completed_with_issues"
    )
    update_run_state(
        config,
        final_stage,
        pilot_conversion_counts=conversion_counts,
        pilot_qc_counts=qc_counts,
        pilot_validation_passed=bool(validation_comparison["passed_no_new_errors"]),
    )
    print(
        json.dumps(
            {
                "pilot_subjects": len(subjects),
                "conversion": conversion_counts,
                "qc": qc_counts,
                "validation_passed": validation_comparison["passed_no_new_errors"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if final_stage == "completed" else 1


def _run(config: ProjectConfig, mode: str, per_stratum: int) -> int:
    report = run_doctor(config)
    if not report["passed"]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    if mode == "inventory-pilot":
        inventory_status = _inventory(config)
        if inventory_status:
            return inventory_status
        return _pilot(config, per_stratum)
    raise ValueError(f"unsupported run mode: {mode}")


def _qc(config: ProjectConfig) -> int:
    records, rows = _load_curated_state(config)
    results = run_qc(config, records, rows)
    counts = {
        status: sum(row["status"] == status for row in results)
        for status in ("pass", "fail", "review")
    }
    print(json.dumps(counts))
    return 1 if counts["fail"] or counts["review"] else 0


def _validate(config: ProjectConfig) -> int:
    status = run_bids_validator(config)
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["passed"] else 1


def _status(config: ProjectConfig, watch: float, processes: bool) -> int:
    interval = watch or 0.0
    while True:
        snapshot = status_snapshot(config, include_processes=processes)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2), flush=True)
        stage = str(snapshot.get("run", {}).get("stage", ""))
        terminal = {
            "completed",
            "completed_with_issues",
            "conversion_completed",
            "conversion_completed_with_failures",
            "inventory_completed",
            "failed",
            "stopped",
        }
        if interval <= 0 or stage in terminal:
            return 0
        time.sleep(interval)


def _subject_filter(raw_subjects: list[str] | None, path: Path | None) -> set[str] | None:
    values = list(raw_subjects or [])
    if path is not None:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            value = line.split("\t", 1)[0].strip()
            if value and not value.startswith("#") and value.lower() != "participant_id":
                values.append(value)
    return {sanitize_subject_label(value) for value in values} if values else None

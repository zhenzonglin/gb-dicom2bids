from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, ProjectConfig, load_config, require_inputs
from .convert import ConversionError, convert_series_set
from .dicom import sanitize_subject_label, scan_dicom_tree
from .manifest import load_private_records, load_selection, write_inventory, write_selection
from .models import SelectionRow, SeriesRecord
from .qc import run_qc
from .select import apply_manual_decisions, build_selection
from .validate import run_bids_validator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gb-dicom2bids")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("inventory", "qc", "validate"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", required=True, type=Path)
    convert = subparsers.add_parser("convert")
    convert.add_argument("--config", required=True, type=Path)
    convert.add_argument("--subjects", nargs="*", default=None)
    convert.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "inventory":
            return _inventory(config)
        if args.command == "convert":
            return _convert(config, args.subjects, args.dry_run)
        if args.command == "qc":
            return _qc(config)
        if args.command == "validate":
            return _validate(config)
    except (ConfigError, ConversionError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


def _inventory(config: ProjectConfig) -> int:
    require_inputs(config)
    result = scan_dicom_tree(
        config.paths.dicom_root,
        axial_max_angle_deg=config.selection.axial_max_angle_deg,
        orientation_consistency_deg=config.selection.orientation_consistency_deg,
    )
    write_inventory(config.paths.audit_root, result.records, result.unreadable_relpaths)
    rows = build_selection(result.records, config.selection)
    write_selection(config.paths.audit_root, rows, result.records)
    summary = {
        "series": len(result.records),
        "subjects": len({record.subject_id for record in result.records}),
        "unreadable": len(result.unreadable_relpaths),
        "selected": sum(row.decision_status == "selected" for row in rows),
        "review": sum(row.decision_status == "review" for row in rows),
    }
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
    config: ProjectConfig, raw_subjects: list[str] | None, dry_run: bool
) -> int:
    records, rows = _load_curated_state(config)
    subjects = None
    if raw_subjects:
        subjects = {sanitize_subject_label(subject) for subject in raw_subjects}
    results = convert_series_set(
        config, records, rows, subjects=subjects, dry_run=dry_run
    )
    for result in results:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 1 if any(result.status == "failed" for result in results) else 0


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

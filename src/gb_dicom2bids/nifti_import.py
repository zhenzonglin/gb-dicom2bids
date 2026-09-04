"""Inventory a read-only, preconverted NIfTI tree for visual QC and BIDS curation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np

from .classify import classify_record
from .config import ProjectConfig, load_config, require_inputs
from .dicom import sanitize_subject_label
from .manifest import load_private_records, load_selection, write_inventory, write_selection
from .models import SelectionRow, SeriesRecord
from .orientation import classify_normal
from .qc_state import (
    assert_pipeline_idle,
    candidate_id,
    effective_modality,
    file_lock,
    qc_root,
    read_decision,
    writer_lock,
)
from .runtime import atomic_write_json, read_json, utc_now


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _stem(path: Path) -> str:
    return path.name[: -len(".nii.gz")]


def _inspect(root: Path, path: Path) -> tuple[SeriesRecord | None, str | None]:
    relative = path.relative_to(root)
    if len(relative.parts) < 3:
        return None, f"{relative.as_posix()}: expected center/subject/series layout"
    if path.is_symlink():
        return None, f"{relative.as_posix()}: symbolic-link input is not accepted"
    try:
        center = relative.parts[0]
        subject = sanitize_subject_label(relative.parts[1])
        series_parts = relative.parts[2:-1]
        series = "/".join(series_parts) if series_parts else _stem(path)
        image = nib.load(str(path))
        shape = tuple(int(value) for value in image.shape)
        if len(shape) not in {3, 4}:
            raise ValueError(f"unsupported NIfTI dimensions: {shape}")
        affine = np.asarray(image.affine, dtype=float)
        zooms = tuple(float(value) for value in image.header.get_zooms()[:3])
        grid_plane = classify_normal(affine[:3, 2])
        stat = path.stat()
    except (OSError, ValueError, TypeError) as exc:
        return None, f"{relative.as_posix()}: {exc}"

    record = SeriesRecord(
        center=center,
        subject_id=subject,
        # No exam identity survives in a NIfTI-only tree. Separate series folders remain
        # separate episodes so selecting across them requires explicit human confirmation.
        study_uid_hash=_hash(relative.parent.as_posix()),
        series_uid_hash=_hash(relative.as_posix()),
        modality="MR",
        series_description=series,
        protocol_name=series,
        sequence_name=_stem(path),
        image_type=["PRECONVERTED_NIFTI"],
        rows=shape[0] if shape else None,
        columns=shape[1] if len(shape) > 1 else None,
        pixel_spacing_mm=list(zooms[:2]),
        slice_thickness_mm=zooms[2] if len(zooms) > 2 else None,
        spacing_between_slices_mm=zooms[2] if len(zooms) > 2 else None,
        nearest_plane=grid_plane.nearest_plane,
        # The affine describes the stored grid, not the original acquisition plane.
        plane="unknown",
        plane_angle_deg=grid_plane.angle_deg,
        orientation_consistent=False,
        coverage_mm=(shape[2] * zooms[2] if len(shape) > 2 and len(zooms) > 2 else None),
        source_kind="preconverted_nifti",
        instance_count=shape[2] if len(shape) > 2 else 0,
        source_relpaths=[relative.as_posix()],
        inventory_note=(
            f"nifti_grid_nearest_plane={grid_plane.nearest_plane};"
            f"source_size={stat.st_size};source_mtime_ns={stat.st_mtime_ns};"
            "acquisition_metadata=unavailable"
        ),
    )
    classify_record(record)
    record.source_kind = "preconverted_nifti"
    record.plane = "unknown"
    record.orientation_consistent = False
    return record, None


def _source_files(root: Path) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    errors: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        kept = []
        for name in sorted(dirnames):
            child = current / name
            if child.is_symlink():
                errors.append(
                    f"{child.relative_to(root).as_posix()}: symbolic-link directory skipped"
                )
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = current / name
            lowered = name.lower()
            if lowered.endswith(".nii.gz"):
                files.append(path)
            elif lowered.endswith(".nii"):
                errors.append(
                    f"{path.relative_to(root).as_posix()}: uncompressed .nii is not imported"
                )
    return files, errors


def _assert_clean_destination(config: ProjectConfig) -> None:
    audit = config.paths.audit_root
    staging = config.paths.staging_bids_root
    if (audit / "series_sources.json").exists():
        raise ValueError(
            f"NIfTI inventory already exists in {audit}; reuse it instead of overwriting it"
        )
    decisions = audit / "visual_qc"
    if decisions.exists() and any(decisions.rglob("*.json")):
        raise ValueError("visual QC state already exists; refusing to replace its inventory")
    if audit.exists():
        unexpected = [
            path.name
            for path in audit.iterdir()
            if path.name != "nifti_import_status.json"
        ]
        if unexpected:
            raise ValueError(
                f"NIfTI audit destination must be new: {audit} contains {sorted(unexpected)[:3]}"
            )
    if staging.exists() and any(staging.iterdir()):
        raise ValueError(f"new BIDS destination must be empty: {staging}")


def inventory_preconverted(config: ProjectConfig) -> dict[str, Any]:
    if not config.nifti_import.enabled:
        raise ValueError("nifti_import.source_root is required")
    if config.conversion.seed_from_existing_bids:
        raise ValueError("conversion.seed_from_existing_bids must be false in NIfTI-only mode")
    require_inputs(config)
    _assert_clean_destination(config)
    root = config.nifti_import.source_root
    assert root is not None
    config.paths.audit_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        config.paths.audit_root / "nifti_import_status.json",
        {"status": "running", "started_at": utc_now()},
    )
    try:
        files, errors = _source_files(root)
        records: list[SeriesRecord] = []
        with ThreadPoolExecutor(max_workers=config.inventory.workers) as executor:
            for index, (record, error) in enumerate(
                executor.map(lambda path: _inspect(root, path), files), start=1
            ):
                if record is not None:
                    records.append(record)
                if error:
                    errors.append(error)
                if index % 1000 == 0:
                    print(
                        f"NIfTI headers: {index}/{len(files)}; valid={len(records)}; "
                        f"errors={len(errors)}",
                        flush=True,
                    )
        records.sort(
            key=lambda item: (item.center, item.subject_id, item.series_uid_hash)
        )
        centers: dict[str, set[str]] = defaultdict(set)
        identities: set[tuple[str, str]] = set()
        for record in records:
            centers[record.subject_id].add(record.center)
            identity = (record.subject_id, record.series_uid_hash)
            if identity in identities:
                raise ValueError(f"duplicate NIfTI candidate identity: {identity}")
            identities.add(identity)
        collisions = {key: value for key, value in centers.items() if len(value) > 1}
        if collisions:
            first = next(iter(sorted(collisions.items())))
            raise ValueError(
                f"subject label {first[0]!r} occurs in multiple centers: {sorted(first[1])}"
            )
        if not records:
            raise ValueError("no readable .nii.gz candidates were found")
        rows = [
            SelectionRow(
                record.center,
                record.subject_id,
                record.study_uid_hash,
                record.series_uid_hash,
                record.candidate_type,
                "review",
                0.0,
                "nifti_only_requires_visual_qc",
                "unknown",
                "preconverted_nifti",
                record.protocol_id,
            )
            for record in records
        ]
        write_inventory(
            config.paths.audit_root,
            records,
            sorted(errors),
            error_status="unreadable_or_unsupported_nifti",
        )
        write_selection(config.paths.audit_root, rows, records, preserve_manual=False)
        config.paths.staging_bids_root.mkdir(parents=True, exist_ok=True)
        counts = Counter(record.candidate_type for record in records)
        summary: dict[str, Any] = {
            "status": "completed",
            "source_files": len(files),
            "candidates": len(records),
            "subjects": len({record.subject_id for record in records}),
            "t1_candidates": counts["t1"],
            "flair_candidates": counts["flair"],
            "other_candidates": counts["other"],
            "errors": len(errors),
            "finished_at": utc_now(),
        }
        atomic_write_json(config.paths.audit_root / "nifti_import_status.json", summary)
        return summary
    except Exception as exc:
        atomic_write_json(
            config.paths.audit_root / "nifti_import_status.json",
            {"status": "failed", "error": str(exc), "finished_at": utc_now()},
        )
        raise


def _inventory_errors(audit_root: Path) -> list[str]:
    path = audit_root / "inventory_errors.tsv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            row["source_relpath"]
            for row in csv.DictReader(handle, delimiter="\t")
            if row.get("source_relpath")
        ]


def refresh_classification(config: ProjectConfig) -> dict[str, Any]:
    """Reclassify an existing NIfTI inventory without scanning or modifying sources."""
    if not config.nifti_import.enabled:
        raise ValueError("classification refresh is only available in NIfTI-only mode")
    audit_root = config.paths.audit_root
    selection_path = audit_root / "selection_manifest.tsv"
    if not (audit_root / "series_sources.json").is_file() or not selection_path.is_file():
        raise ValueError("run the NIfTI inventory once before refreshing classification")

    root = qc_root(config)
    with writer_lock(config), file_lock(root / ".decisions.lock"):
        assert_pipeline_idle(config)
        records = load_private_records(audit_root)
        rows = load_selection(selection_path)
        before = Counter(record.candidate_type for record in records)
        changed = 0
        refreshed: list[SeriesRecord] = []
        for record in records:
            new_type = classify_record(replace(record)).candidate_type
            changed += new_type != record.candidate_type
            # Only the automatic class changes. Source identity, geometry and the
            # record digest used by saved visual decisions remain unchanged.
            refreshed.append(replace(record, candidate_type=new_type))

        by_key = {
            (record.subject_id, record.study_uid_hash, record.series_uid_hash): record
            for record in refreshed
        }
        saved_subjects = {path.stem for path in (root / "subjects").glob("*.json")}
        saved_subjects |= {
            path.name for path in (root / "history").glob("*") if path.is_dir()
        }
        decisions = {subject: read_decision(root, subject) for subject in saved_subjects}
        for row in rows:
            record = by_key.get((row.subject_id, row.study_uid_hash, row.series_uid_hash))
            if record is None:
                continue
            rating = (
                decisions.get(record.subject_id, {})
                .get("candidates", {})
                .get(candidate_id(record))
            )
            row.candidate_type = effective_modality(record, rating)

        errors = _inventory_errors(audit_root)
        write_inventory(
            audit_root,
            refreshed,
            errors,
            error_status="unreadable_or_unsupported_nifti",
        )
        write_selection(audit_root, rows, refreshed)
        after = Counter(record.candidate_type for record in refreshed)
        refreshed_at = utc_now()
        summary: dict[str, Any] = {
            "status": "completed",
            "changed_candidates": changed,
            "before": {key: before[key] for key in ("t1", "flair", "other")},
            "after": {key: after[key] for key in ("t1", "flair", "other")},
            "refreshed_at": refreshed_at,
            "source_scan_performed": False,
        }
        atomic_write_json(root / "classification_refresh.json", summary)
        import_status = read_json(audit_root / "nifti_import_status.json")
        import_status.update(
            {
                "t1_candidates": after["t1"],
                "flair_candidates": after["flair"],
                "other_candidates": after["other"],
                "classification_refreshed_at": refreshed_at,
            }
        )
        atomic_write_json(audit_root / "nifti_import_status.json", import_status)
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.local.yaml"))
    parser.add_argument(
        "--refresh-classification",
        action="store_true",
        help="update T1/FLAIR suggestions in the existing inventory without rescanning sources",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        summary = (
            refresh_classification(config)
            if args.refresh_classification
            else inventory_preconverted(config)
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pydicom

from .classify import classify_record
from .models import SeriesRecord
from .orientation import (
    classify_orientation,
    coverage_from_positions,
    maximum_orientation_deviation,
)

SPECIFIC_TAGS = [
    "Modality",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "SeriesNumber",
    "SeriesDescription",
    "ProtocolName",
    "SequenceName",
    "ImageType",
    "Manufacturer",
    "ManufacturerModelName",
    "SoftwareVersions",
    "MRAcquisitionType",
    "RepetitionTime",
    "EchoTime",
    "InversionTime",
    "FlipAngle",
    "Rows",
    "Columns",
    "PixelSpacing",
    "SliceThickness",
    "SpacingBetweenSlices",
    "ImageOrientationPatient",
    "ImagePositionPatient",
    "NumberOfFrames",
    "SharedFunctionalGroupsSequence",
    "PerFrameFunctionalGroupsSequence",
]


@dataclass
class InventoryResult:
    records: list[SeriesRecord]
    unreadable_relpaths: list[str]
    files_seen: int = 0


@dataclass
class _SeriesAccumulator:
    center: str
    subject_id: str
    study_uid_hash: str
    series_uid_hash: str
    first: dict[str, Any]
    source_relpaths: list[str] = field(default_factory=list)
    sop_uids: set[str] = field(default_factory=set)
    duplicate_count: int = 0
    orientations: list[list[float]] = field(default_factory=list)
    positions: list[list[float]] = field(default_factory=list)


def _hash_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def sanitize_subject_label(value: str) -> str:
    label = re.sub(r"[^a-z0-9]", "", value.lower())
    if label.startswith("sub") and len(label) > 3:
        label = label[3:]
    if not label:
        raise ValueError(f"subject folder does not produce a valid BIDS label: {value!r}")
    return label


def _safe_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\\".join(str(item) for item in value)
    return str(value)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_list(value: Any, expected: int | None = None) -> list[float]:
    if value is None:
        return []
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError):
        return []
    if expected is not None and len(result) != expected:
        return []
    return result


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in value.split("\\") if item]
    try:
        return [str(item) for item in value]
    except TypeError:
        return [str(value)]


def _enhanced_geometry(dataset: Any) -> tuple[list[list[float]], list[list[float]]]:
    orientations: list[list[float]] = []
    positions: list[list[float]] = []

    def extract(group: Any) -> None:
        plane_orientation = getattr(group, "PlaneOrientationSequence", None)
        if plane_orientation:
            value = _float_list(
                getattr(plane_orientation[0], "ImageOrientationPatient", None), expected=6
            )
            if value:
                orientations.append(value)
        plane_position = getattr(group, "PlanePositionSequence", None)
        if plane_position:
            value = _float_list(
                getattr(plane_position[0], "ImagePositionPatient", None), expected=3
            )
            if value:
                positions.append(value)

    shared = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    if shared:
        extract(shared[0])
    per_frame = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    if per_frame:
        for group in per_frame:
            extract(group)
    return orientations, positions


def _iter_files(root: Path) -> Iterable[Path]:
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for filename in filenames:
            path = Path(directory) / filename
            if path.is_file():
                yield path


def _scan_dicom_tree_serial(
    dicom_root: Path,
    *,
    axial_max_angle_deg: float = 20.0,
    orientation_consistency_deg: float = 3.0,
    scan_root: Path | None = None,
) -> InventoryResult:
    root = dicom_root.resolve()
    target = scan_root.resolve() if scan_root is not None else root
    groups: dict[tuple[str, str, str, str], _SeriesAccumulator] = {}
    unreadable: list[str] = []
    subject_centers: dict[str, set[str]] = defaultdict(set)
    files_seen = 0

    for path in _iter_files(target):
        files_seen += 1
        relative = path.relative_to(root)
        if len(relative.parts) < 2:
            unreadable.append(relative.as_posix())
            continue
        center = relative.parts[0]
        subject_id = sanitize_subject_label(relative.parts[1])
        subject_centers[subject_id].add(center)
        try:
            dataset = pydicom.dcmread(
                path,
                stop_before_pixels=True,
                force=True,
                specific_tags=SPECIFIC_TAGS,
            )
        except Exception:
            unreadable.append(relative.as_posix())
            continue
        if not getattr(dataset, "Modality", None):
            unreadable.append(relative.as_posix())
            continue

        study_uid = _safe_string(getattr(dataset, "StudyInstanceUID", ""))
        series_uid = _safe_string(getattr(dataset, "SeriesInstanceUID", ""))
        study_key = study_uid or f"missing-study:{center}/{subject_id}"
        series_key = series_uid or f"missing-series:{relative.parent.as_posix()}"
        key = (center, subject_id, study_key, series_key)
        if key not in groups:
            groups[key] = _SeriesAccumulator(
                center=center,
                subject_id=subject_id,
                study_uid_hash=_hash_identifier(study_key),
                series_uid_hash=_hash_identifier(series_key),
                first={
                    "series_number": _safe_int(getattr(dataset, "SeriesNumber", None)),
                    "modality": _safe_string(getattr(dataset, "Modality", "")),
                    "series_description": _safe_string(getattr(dataset, "SeriesDescription", "")),
                    "protocol_name": _safe_string(getattr(dataset, "ProtocolName", "")),
                    "sequence_name": _safe_string(getattr(dataset, "SequenceName", "")),
                    "image_type": _string_list(getattr(dataset, "ImageType", None)),
                    "manufacturer": _safe_string(getattr(dataset, "Manufacturer", "")),
                    "model_name": _safe_string(getattr(dataset, "ManufacturerModelName", "")),
                    "software_versions": _safe_string(getattr(dataset, "SoftwareVersions", "")),
                    "acquisition_type": _safe_string(getattr(dataset, "MRAcquisitionType", "")),
                    "repetition_time_ms": _safe_float(getattr(dataset, "RepetitionTime", None)),
                    "echo_time_ms": _safe_float(getattr(dataset, "EchoTime", None)),
                    "inversion_time_ms": _safe_float(getattr(dataset, "InversionTime", None)),
                    "flip_angle_deg": _safe_float(getattr(dataset, "FlipAngle", None)),
                    "rows": _safe_int(getattr(dataset, "Rows", None)),
                    "columns": _safe_int(getattr(dataset, "Columns", None)),
                    "pixel_spacing_mm": _float_list(
                        getattr(dataset, "PixelSpacing", None), expected=2
                    ),
                    "slice_thickness_mm": _safe_float(getattr(dataset, "SliceThickness", None)),
                    "spacing_between_slices_mm": _safe_float(
                        getattr(dataset, "SpacingBetweenSlices", None)
                    ),
                },
            )
        accumulator = groups[key]
        sop_uid = _safe_string(getattr(dataset, "SOPInstanceUID", ""))
        sop_key = sop_uid or relative.as_posix()
        if sop_key in accumulator.sop_uids:
            accumulator.duplicate_count += 1
            continue
        accumulator.sop_uids.add(sop_key)
        accumulator.source_relpaths.append(relative.as_posix())

        orientation = _float_list(getattr(dataset, "ImageOrientationPatient", None), expected=6)
        position = _float_list(getattr(dataset, "ImagePositionPatient", None), expected=3)
        if orientation:
            accumulator.orientations.append(orientation)
        if position:
            accumulator.positions.append(position)
        enhanced_orientations, enhanced_positions = _enhanced_geometry(dataset)
        accumulator.orientations.extend(enhanced_orientations)
        accumulator.positions.extend(enhanced_positions)

    collisions = {label: centers for label, centers in subject_centers.items() if len(centers) > 1}
    if collisions:
        details = "; ".join(
            f"sub-{label}: {sorted(centers)}" for label, centers in collisions.items()
        )
        raise ValueError(f"cross-center subject label collisions detected: {details}")

    records: list[SeriesRecord] = []
    for accumulator in groups.values():
        representative = accumulator.orientations[0] if accumulator.orientations else []
        plane_result = classify_orientation(representative, axial_max_angle_deg)
        deviation = maximum_orientation_deviation(accumulator.orientations)
        consistent = deviation is None or deviation <= orientation_consistency_deg
        coverage = coverage_from_positions(
            accumulator.positions,
            plane_result.normal,
            accumulator.first["slice_thickness_mm"],
        )
        note_parts: list[str] = []
        if not representative:
            note_parts.append("missing_orientation")
        if not consistent:
            note_parts.append(f"orientation_deviation={deviation:.3f}")
        if not accumulator.source_relpaths:
            note_parts.append("no_unique_instances")
        record = SeriesRecord(
            center=accumulator.center,
            subject_id=accumulator.subject_id,
            study_uid_hash=accumulator.study_uid_hash,
            series_uid_hash=accumulator.series_uid_hash,
            image_orientation_patient=representative,
            nearest_plane=plane_result.nearest_plane,
            plane=plane_result.plane if consistent else "inconsistent",
            plane_angle_deg=plane_result.angle_deg,
            orientation_consistent=consistent,
            coverage_mm=coverage,
            instance_count=len(accumulator.source_relpaths),
            duplicate_instance_count=accumulator.duplicate_count,
            source_relpaths=sorted(accumulator.source_relpaths),
            inventory_note=";".join(note_parts),
            **accumulator.first,
        )
        records.append(classify_record(record))

    records.sort(
        key=lambda item: (
            item.center,
            item.subject_id,
            item.study_uid_hash,
            item.series_number if item.series_number is not None else 10**9,
            item.series_uid_hash,
        )
    )
    return InventoryResult(
        records=records,
        unreadable_relpaths=sorted(unreadable),
        files_seen=files_seen,
    )


def _scan_subject_job(
    root: Path,
    subject_root: Path,
    axial_max_angle_deg: float,
    orientation_consistency_deg: float,
) -> InventoryResult:
    return _scan_dicom_tree_serial(
        root,
        axial_max_angle_deg=axial_max_angle_deg,
        orientation_consistency_deg=orientation_consistency_deg,
        scan_root=subject_root,
    )


def scan_dicom_tree(
    dicom_root: Path,
    *,
    axial_max_angle_deg: float = 20.0,
    orientation_consistency_deg: float = 3.0,
    workers: int = 1,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> InventoryResult:
    """Scan one center/subject tree with deterministic subject-level parallelism."""
    root = dicom_root.resolve()
    if workers < 1:
        raise ValueError("workers must be at least 1")
    subject_roots: list[Path] = []
    orphan_relpaths: list[str] = []
    centers_by_label: dict[str, set[str]] = defaultdict(set)
    if root.is_dir():
        orphan_relpaths.extend(
            path.relative_to(root).as_posix() for path in root.iterdir() if path.is_file()
        )
        for center_path in sorted(path for path in root.iterdir() if path.is_dir()):
            orphan_relpaths.extend(
                path.relative_to(root).as_posix()
                for path in center_path.iterdir()
                if path.is_file()
            )
            for subject_path in sorted(path for path in center_path.iterdir() if path.is_dir()):
                label = sanitize_subject_label(subject_path.name)
                centers_by_label[label].add(center_path.name)
                subject_roots.append(subject_path)
    collisions = {label: centers for label, centers in centers_by_label.items() if len(centers) > 1}
    if collisions:
        details = "; ".join(
            f"sub-{label}: {sorted(centers)}" for label, centers in sorted(collisions.items())
        )
        raise ValueError(f"cross-center subject label collisions detected: {details}")
    if workers == 1 or len(subject_roots) <= 1:
        result = _scan_dicom_tree_serial(
            root,
            axial_max_angle_deg=axial_max_angle_deg,
            orientation_consistency_deg=orientation_consistency_deg,
        )
        if progress_callback:
            progress_callback(len(subject_roots), len(subject_roots), result.files_seen)
        return result

    records: list[SeriesRecord] = []
    unreadable: list[str] = list(orphan_relpaths)
    files_seen = 0
    completed = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _scan_subject_job,
                root,
                subject_root,
                axial_max_angle_deg,
                orientation_consistency_deg,
            )
            for subject_root in subject_roots
        ]
        for future in as_completed(futures):
            result = future.result()
            records.extend(result.records)
            unreadable.extend(result.unreadable_relpaths)
            files_seen += result.files_seen
            completed += 1
            if progress_callback:
                progress_callback(completed, len(subject_roots), files_seen)
    records.sort(
        key=lambda item: (
            item.center,
            item.subject_id,
            item.study_uid_hash,
            item.series_number if item.series_number is not None else 10**9,
            item.series_uid_hash,
        )
    )
    return InventoryResult(records, sorted(unreadable), files_seen)

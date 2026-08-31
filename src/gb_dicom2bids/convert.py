from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.uid import (
    PYDICOM_IMPLEMENTATION_UID,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
)

from .bids import ensure_dataset_metadata, update_participants, update_scans
from .config import ProjectConfig, require_inputs
from .manifest import write_conversion_results
from .models import ConversionResult, SelectionRow, SeriesRecord
from .orientation import classify_normal, classify_orientation


class ConversionError(RuntimeError):
    """Raised for a series-level conversion failure."""


JPEG2000_TRANSFER_SYNTAXES = {
    "1.2.840.10008.1.2.4.90",
    "1.2.840.10008.1.2.4.91",
    "1.2.840.10008.1.2.4.92",
    "1.2.840.10008.1.2.4.93",
}


def seed_staging(config: ProjectConfig, *, dry_run: bool = False) -> None:
    state_path = config.paths.audit_root / "staging_seed.json"
    staging = config.paths.staging_bids_root
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if Path(state.get("staging_bids_root", "")) != staging:
            raise ConversionError("staging seed state points to a different destination")
        return
    if staging.exists() and any(staging.iterdir()):
        raise ConversionError(
            f"staging directory is non-empty without a seed record; refusing to merge: {staging}"
        )
    if dry_run:
        return
    config.paths.audit_root.mkdir(parents=True, exist_ok=True)
    if config.conversion.seed_from_existing_bids:
        shutil.copytree(
            config.paths.existing_bids_root,
            staging,
            copy_function=shutil.copy2,
            symlinks=True,
            dirs_exist_ok=False,
        )
    else:
        staging.mkdir(parents=True, exist_ok=False)
    state = {
        "existing_bids_root": str(config.paths.existing_bids_root),
        "staging_bids_root": str(staging),
        "seeded": True,
    }
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def convert_series_set(
    config: ProjectConfig,
    records: list[SeriesRecord],
    selections: list[SelectionRow],
    *,
    subjects: set[str] | None = None,
    dry_run: bool = False,
) -> list[ConversionResult]:
    require_inputs(config, need_existing_bids=config.conversion.seed_from_existing_bids)
    seed_staging(config, dry_run=dry_run)
    by_hash = {record.series_uid_hash: record for record in records}
    selected_or_review = [
        row
        for row in selections
        if row.decision_status == "selected"
        or (row.decision_status == "review" and config.conversion.convert_review_candidates)
    ]
    if subjects is not None:
        selected_or_review = [row for row in selected_or_review if row.subject_id in subjects]

    if dry_run:
        return [
            ConversionResult(
                subject_id=row.subject_id,
                series_uid_hash=row.series_uid_hash,
                candidate_type=row.candidate_type,
                status="dry_run",
                mode="selected" if row.decision_status == "selected" else "review",
                output_path=row.output_basename,
                message=row.reason,
            )
            for row in selected_or_review
        ]

    if shutil.which(config.tools.dcm2niix) is None and not Path(config.tools.dcm2niix).is_file():
        raise ConversionError(f"dcm2niix not found: {config.tools.dcm2niix}")

    ensure_dataset_metadata(config)
    update_participants(config, records)
    results: list[ConversionResult] = []
    diff_rows: list[dict[str, str]] = []
    for selection in selected_or_review:
        record = by_hash.get(selection.series_uid_hash)
        if record is None:
            results.append(
                ConversionResult(
                    selection.subject_id,
                    selection.series_uid_hash,
                    selection.candidate_type,
                    "failed",
                    "inventory",
                    message="series missing from private inventory",
                )
            )
            continue
        try:
            result, diffs = _convert_one(config, record, selection)
            results.append(result)
            diff_rows.extend(diffs)
        except Exception as exc:
            results.append(
                ConversionResult(
                    record.subject_id,
                    record.series_uid_hash,
                    record.candidate_type,
                    "failed",
                    "conversion",
                    message=str(exc),
                )
            )

    write_conversion_results(config.paths.audit_root / "conversion_status.tsv", results)
    _write_diff(config.paths.audit_root / "old_vs_v4_diff.tsv", diff_rows)
    return results


def _convert_one(
    config: ProjectConfig, record: SeriesRecord, selection: SelectionRow
) -> tuple[ConversionResult, list[dict[str, str]]]:
    work_root = config.paths.audit_root / "work" / f"sub-{record.subject_id}"
    work_root.mkdir(parents=True, exist_ok=True)
    log_root = config.paths.audit_root / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    source_paths = [config.paths.dicom_root / relpath for relpath in record.source_relpaths]
    missing = [path for path in source_paths if not path.is_file()]
    if missing:
        raise ConversionError(f"{len(missing)} source instances are missing")

    with tempfile.TemporaryDirectory(prefix=f"{record.series_uid_hash}_", dir=work_root) as temp:
        temp_root = Path(temp)
        input_dir = temp_root / "input"
        output_dir = temp_root / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        _link_series(source_paths, input_dir)
        direct = _run_dcm2niix(config, input_dir, output_dir, record, "direct", log_root)
        mode = "direct"
        if direct is None:
            fixed_dir = temp_root / "fixed"
            fixed_dir.mkdir()
            repair_actions = _prepare_fallback_series(source_paths, fixed_dir)
            repair_log = log_root / (
                f"sub-{record.subject_id}_{record.series_uid_hash}_repair.log"
            )
            repair_log.write_text("\n".join(repair_actions) + "\n", encoding="utf-8")
            _clear_directory(output_dir)
            direct = _run_dcm2niix(config, fixed_dir, output_dir, record, "fallback", log_root)
            mode = "fallback"
        if direct is None:
            raise ConversionError("direct and fallback dcm2niix conversion failed")
        nifti_path, json_path = direct
        _validate_converted_pair(nifti_path, json_path, record)

        if selection.decision_status == "review":
            candidate_dir = (
                config.paths.audit_root
                / "candidates"
                / f"sub-{record.subject_id}"
                / record.series_uid_hash
            )
            candidate_dir.mkdir(parents=True, exist_ok=True)
            target_nii = candidate_dir / "candidate.nii.gz"
            target_json = candidate_dir / "candidate.json"
            shutil.copy2(nifti_path, target_nii)
            shutil.copy2(json_path, target_json)
            return (
                ConversionResult(
                    record.subject_id,
                    record.series_uid_hash,
                    record.candidate_type,
                    "review_ready",
                    mode,
                    str(target_nii),
                    selection.reason,
                ),
                [],
            )

        installed, diffs = _install_selected(
            config, record, selection, nifti_path, json_path
        )
        update_scans(config, record, selection, installed)
        return (
            ConversionResult(
                record.subject_id,
                record.series_uid_hash,
                record.candidate_type,
                "converted",
                mode,
                str(installed),
                selection.reason,
            ),
            diffs,
        )


def _link_series(source_paths: list[Path], destination: Path) -> None:
    for index, source in enumerate(source_paths, start=1):
        target = destination / f"instance-{index:06d}.dcm"
        try:
            os.symlink(source, target)
        except OSError:
            shutil.copy2(source, target)


def _run_dcm2niix(
    config: ProjectConfig,
    input_dir: Path,
    output_dir: Path,
    record: SeriesRecord,
    mode: str,
    log_root: Path,
) -> tuple[Path, Path] | None:
    command = [
        config.tools.dcm2niix,
        "-b",
        "y",
        "-ba",
        "y" if config.conversion.anonymize_sidecars else "n",
        "-z",
        config.conversion.compression,
        "-o",
        str(output_dir),
        "-f",
        "converted",
        str(input_dir),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    log_path = log_root / f"sub-{record.subject_id}_{record.series_uid_hash}_{mode}.log"
    log_path.write_text(
        f"returncode={completed.returncode}\n{completed.stdout}\n{completed.stderr}",
        encoding="utf-8",
    )
    nifti = sorted(output_dir.glob("*.nii.gz")) or sorted(output_dir.glob("*.nii"))
    sidecars = sorted(output_dir.glob("*.json"))
    if completed.returncode != 0 or len(nifti) != 1 or len(sidecars) != 1:
        return None
    return nifti[0], sidecars[0]


def _prepare_fallback_series(source_paths: list[Path], destination: Path) -> list[str]:
    actions: list[str] = []
    for index, source in enumerate(source_paths, start=1):
        output = destination / f"instance-{index:06d}.dcm"
        dataset = pydicom.dcmread(source, force=True)
        transfer_syntax = _transfer_syntax(dataset)
        if transfer_syntax and getattr(transfer_syntax, "is_compressed", False):
            if str(transfer_syntax) in JPEG2000_TRANSFER_SYNTAXES:
                try:
                    dataset.decompress(decoding_plugin="gdcm")
                except TypeError:
                    dataset.decompress(handler_name="gdcm")
                except Exception as exc:
                    raise ConversionError(
                        f"GDCM decompression failed for {transfer_syntax}: {exc}"
                    ) from exc
                actions.append(
                    f"decompressed\t{index:06d}\t{transfer_syntax}\tpython-gdcm"
                )
                transfer_syntax = _transfer_syntax(dataset)
            else:
                decoder = decoder_command(str(transfer_syntax), source, output)
                if decoder is None:
                    raise ConversionError(
                        f"unsupported compressed transfer syntax: {transfer_syntax}"
                    )
                if shutil.which(decoder[0]) is None:
                    raise ConversionError(f"required decoder is missing: {decoder[0]}")
                completed = subprocess.run(
                    decoder, capture_output=True, text=True, check=False
                )
                if completed.returncode != 0:
                    raise ConversionError(
                        f"decoder failed for {transfer_syntax}: {completed.stderr.strip()}"
                    )
                dataset = pydicom.dcmread(output, force=True)
                actions.append(
                    f"decompressed\t{index:06d}\t{transfer_syntax}\t{decoder[0]}"
                )
        _sanitize_dataset(dataset)
        _ensure_file_meta(dataset)
        try:
            dataset.save_as(output, enforce_file_format=True)
        except (TypeError, ValueError) as first_error:
            dataset.remove_private_tags()
            _sanitize_dataset(dataset)
            _ensure_file_meta(dataset)
            try:
                dataset.save_as(output, enforce_file_format=True)
            except TypeError:
                dataset.save_as(output, write_like_original=False)
            actions.append(
                f"removed_private_tags\t{index:06d}\t{type(first_error).__name__}"
            )
        actions.append(f"rewritten\t{index:06d}")
    return actions


def decoder_command(transfer_syntax: str, source: Path, output: Path) -> list[str] | None:
    if transfer_syntax == "1.2.840.10008.1.2.5":
        return ["dcmdrle", str(source), str(output)]
    if transfer_syntax in {"1.2.840.10008.1.2.4.80", "1.2.840.10008.1.2.4.81"}:
        return ["dcmdjpls", str(source), str(output)]
    if transfer_syntax in JPEG2000_TRANSFER_SYNTAXES:
        return None
    if transfer_syntax.startswith("1.2.840.10008.1.2.4."):
        return ["dcmdjpeg", str(source), str(output)]
    return None


def _transfer_syntax(dataset: Any) -> Any:
    file_meta = getattr(dataset, "file_meta", None)
    return getattr(file_meta, "TransferSyntaxUID", None) if file_meta else None


def _sanitize_dataset(dataset: Any) -> None:
    protected_uids = {
        "SOPClassUID",
        "SOPInstanceUID",
        "StudyInstanceUID",
        "SeriesInstanceUID",
    }
    for element in list(dataset.iterall()):
        try:
            if element.VR in {"SH", "LO"}:
                maximum = 16 if element.VR == "SH" else 64
                values = element.value if element.VM > 1 else [element.value]
                cleaned = [_clean_ascii(value, maximum) for value in values]
                cleaned = [value for value in cleaned if value]
                if not cleaned:
                    del dataset[element.tag]
                else:
                    element.value = cleaned if element.VM > 1 else cleaned[0]
            elif element.VR == "UI" and element.keyword not in protected_uids:
                values = element.value if element.VM > 1 else [element.value]
                valid = [str(value).strip() for value in values if _valid_uid(value)]
                if not valid:
                    del dataset[element.tag]
                else:
                    element.value = valid if element.VM > 1 else valid[0]
        except (KeyError, TypeError, ValueError):
            continue


def _clean_ascii(value: Any, maximum: int) -> str:
    return str(value).encode("ascii", errors="ignore").decode("ascii").strip()[:maximum]


def _valid_uid(value: Any) -> bool:
    text = str(value).strip()
    return bool(
        text
        and len(text) <= 64
        and text[0] != "."
        and text[-1] != "."
        and ".." not in text
        and all(character in "0123456789." for character in text)
    )


def _ensure_file_meta(dataset: Any) -> None:
    if not hasattr(dataset, "file_meta") or dataset.file_meta is None:
        dataset.file_meta = FileMetaDataset()
    if not getattr(dataset.file_meta, "TransferSyntaxUID", None):
        if getattr(dataset, "is_little_endian", True):
            dataset.file_meta.TransferSyntaxUID = (
                ImplicitVRLittleEndian
                if getattr(dataset, "is_implicit_VR", True)
                else ExplicitVRLittleEndian
            )
        else:
            dataset.file_meta.TransferSyntaxUID = ExplicitVRBigEndian
    dataset.file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    for source_keyword, meta_keyword in (
        ("SOPClassUID", "MediaStorageSOPClassUID"),
        ("SOPInstanceUID", "MediaStorageSOPInstanceUID"),
    ):
        value = getattr(dataset, source_keyword, None)
        if value:
            setattr(dataset.file_meta, meta_keyword, value)


def _validate_converted_pair(nifti_path: Path, json_path: Path, record: SeriesRecord) -> None:
    image = nib.load(str(nifti_path))
    if len(image.shape) not in {3, 4} or any(size < 8 for size in image.shape[:3]):
        raise ConversionError(f"unexpected NIfTI shape: {image.shape}")
    if len(image.shape) == 4 and image.shape[3] != 1:
        raise ConversionError(f"structural candidate has multiple volumes: {image.shape}")
    affine = np.asarray(image.affine, dtype=float)
    invalid_affine = (
        affine.shape != (4, 4)
        or not np.isfinite(affine).all()
        or abs(np.linalg.det(affine[:3, :3])) < 1e-8
    )
    if invalid_affine:
        raise ConversionError("invalid or singular NIfTI affine")
    zooms = image.header.get_zooms()[:3]
    if any(not np.isfinite(value) or value <= 0 for value in zooms):
        raise ConversionError(f"invalid NIfTI voxel sizes: {zooms}")

    metadata = json.loads(json_path.read_text(encoding="utf-8"))
    json_iop = metadata.get("ImageOrientationPatientDICOM")
    if record.image_orientation_patient and json_iop:
        expected = classify_orientation(record.image_orientation_patient)
        observed = classify_orientation(json_iop)
        if expected.nearest_plane != observed.nearest_plane:
            raise ConversionError(
                f"DICOM/JSON plane mismatch: {expected.nearest_plane} vs {observed.nearest_plane}"
            )
    if record.image_orientation_patient:
        expected = classify_orientation(record.image_orientation_patient)
        affine_plane = classify_normal(affine[:3, 2])
        if expected.nearest_plane != affine_plane.nearest_plane:
            raise ConversionError(
                "DICOM/NIfTI affine plane mismatch: "
                f"{expected.nearest_plane} vs {affine_plane.nearest_plane}"
            )


def _install_selected(
    config: ProjectConfig,
    record: SeriesRecord,
    selection: SelectionRow,
    nifti_path: Path,
    json_path: Path,
) -> tuple[Path, list[dict[str, str]]]:
    anat_dir = config.paths.staging_bids_root / f"sub-{record.subject_id}" / "anat"
    anat_dir.mkdir(parents=True, exist_ok=True)
    suffix = "T1w" if record.candidate_type == "t1" else "FLAIR"
    existing = sorted(anat_dir.glob(f"*{suffix}.nii*")) + sorted(anat_dir.glob(f"*{suffix}.json"))
    diffs: list[dict[str, str]] = []
    backup_root = config.paths.audit_root / "replaced" / f"sub-{record.subject_id}" / "anat"
    for old in existing:
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / old.name
        if not backup.exists():
            shutil.copy2(old, backup)
        diffs.append(
            {
                "subject_id": record.subject_id,
                "modality": suffix,
                "action": "replaced",
                "old_path": str(old.relative_to(config.paths.staging_bids_root)),
                "old_sha256": _sha256(old),
                "new_path": f"sub-{record.subject_id}/anat/{selection.output_basename}.nii.gz",
                "series_uid_hash": record.series_uid_hash,
                "reason": selection.reason,
            }
        )
    target_nifti = anat_dir / f"{selection.output_basename}.nii.gz"
    target_json = anat_dir / f"{selection.output_basename}.json"
    staged_nifti = target_nifti.with_name(f".{target_nifti.name}.incoming")
    staged_json = target_json.with_name(f".{target_json.name}.incoming")
    _atomic_copy(nifti_path, staged_nifti)
    try:
        _atomic_copy(json_path, staged_json)
    except Exception:
        staged_nifti.unlink(missing_ok=True)
        raise
    for old in existing:
        if old not in {target_nifti, target_json}:
            old.unlink()
    os.replace(staged_nifti, target_nifti)
    os.replace(staged_json, target_json)
    if not diffs:
        diffs.append(
            {
                "subject_id": record.subject_id,
                "modality": suffix,
                "action": "added",
                "old_path": "",
                "old_sha256": "",
                "new_path": str(target_nifti.relative_to(config.paths.staging_bids_root)),
                "series_uid_hash": record.series_uid_hash,
                "reason": selection.reason,
            }
        )
    return target_nifti, diffs


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clear_directory(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _write_diff(path: Path, rows: Iterable[dict[str, str]]) -> None:
    fields = [
        "subject_id",
        "modality",
        "action",
        "old_path",
        "old_sha256",
        "new_path",
        "series_uid_hash",
        "reason",
    ]
    combined = list(rows)
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            combined = list(csv.DictReader(handle, delimiter="\t")) + combined
    keyed: dict[tuple[str, ...], dict[str, str]] = {}
    for row in combined:
        key = tuple(row.get(field, "") for field in fields)
        keyed[key] = row
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(keyed.values())

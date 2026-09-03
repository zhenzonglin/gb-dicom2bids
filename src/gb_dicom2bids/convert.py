from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import replace
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
from .manifest import _write_tsv, write_conversion_results
from .models import ConversionResult, SelectionRow, SeriesRecord
from .orientation import classify_normal, classify_orientation
from .qc_state import applied_choice, authorized_choice, writer_lock
from .qc_state import enabled as visual_qc_enabled
from .runtime import (
    ResourceSampler,
    atomic_write_json,
    process_is_alive,
    read_json,
    recover_stale_states,
    resource_blockers,
    resource_snapshot,
    series_state_path,
    update_run_state,
    update_series_state,
    utc_now,
)


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
    source = config.paths.existing_bids_root
    state = read_json(state_path)
    if state_path.exists():
        if Path(state.get("staging_bids_root", "")) != staging:
            raise ConversionError("staging seed state points to a different destination")
        if Path(state.get("existing_bids_root", "")) != source:
            raise ConversionError("staging seed state points to a different source")
        if state.get("status") == "completed":
            if not config.conversion.seed_from_existing_bids and staging.is_dir():
                return
            if staging.is_dir() and any(staging.iterdir()):
                return
    elif staging.exists() and any(staging.iterdir()):
        raise ConversionError(
            f"staging directory is non-empty without a seed record; refusing to merge: {staging}"
        )
    if dry_run:
        return
    config.paths.audit_root.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    if not config.conversion.seed_from_existing_bids:
        atomic_write_json(
            state_path,
            {
                "existing_bids_root": str(source),
                "staging_bids_root": str(staging),
                "status": "completed",
                "files_total": 0,
                "files_completed": 0,
                "bytes_total": 0,
                "bytes_completed": 0,
                "updated_at": utc_now(),
            },
        )
        return

    files = sorted(path for path in source.rglob("*") if path.is_file() or path.is_symlink())
    total_bytes = sum(path.stat().st_size for path in files if not path.is_symlink())
    started = time.monotonic()
    completed_bytes = 0
    completed_files = 0
    state = {
        "existing_bids_root": str(source),
        "staging_bids_root": str(staging),
        "status": "copying",
        "files_total": len(files),
        "files_completed": 0,
        "bytes_total": total_bytes,
        "bytes_completed": 0,
        "speed_bytes_per_second": 0.0,
        "eta_seconds": None,
        "updated_at": utc_now(),
    }
    atomic_write_json(state_path, state)
    for index, item in enumerate(files, start=1):
        relative = item.relative_to(source)
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0 if item.is_symlink() else item.stat().st_size
        if item.is_symlink():
            target = os.readlink(item)
            if not destination.is_symlink() or os.readlink(destination) != target:
                destination.unlink(missing_ok=True)
                os.symlink(target, destination)
        elif not _same_file(item, destination):
            _atomic_copy(item, destination)
        completed_bytes += size
        completed_files = index
        if index == len(files) or index % 128 == 0:
            elapsed = max(time.monotonic() - started, 1e-6)
            speed = completed_bytes / elapsed
            remaining = max(0, total_bytes - completed_bytes)
            state.update(
                {
                    "files_completed": completed_files,
                    "bytes_completed": completed_bytes,
                    "speed_bytes_per_second": speed,
                    "eta_seconds": remaining / speed if speed else None,
                    "updated_at": utc_now(),
                }
            )
            atomic_write_json(state_path, state)
            update_run_state(
                config,
                "seeding_staging",
                staging_files_completed=completed_files,
                staging_files_total=len(files),
                staging_bytes_completed=completed_bytes,
                staging_bytes_total=total_bytes,
                staging_speed_bytes_per_second=speed,
                staging_eta_seconds=remaining / speed if speed else None,
            )
    state.update({"status": "completed", "updated_at": utc_now()})
    atomic_write_json(state_path, state)


def _same_file(source: Path, destination: Path) -> bool:
    if not destination.is_file() or destination.is_symlink():
        return False
    source_stat = source.stat()
    destination_stat = destination.stat()
    return (
        source_stat.st_size == destination_stat.st_size
        and source_stat.st_mtime_ns == destination_stat.st_mtime_ns
    )


def convert_series_set(
    config: ProjectConfig,
    records: list[SeriesRecord],
    selections: list[SelectionRow],
    **kwargs: Any,
) -> list[ConversionResult]:
    with writer_lock(config):
        return _convert_series_set_locked(config, records, selections, **kwargs)


def _convert_series_set_locked(
    config: ProjectConfig,
    records: list[SeriesRecord],
    selections: list[SelectionRow],
    *,
    subjects: set[str] | None = None,
    dry_run: bool = False,
    workers: int | None = None,
    resume: bool | None = None,
    retry_failed: bool = False,
) -> list[ConversionResult]:
    require_inputs(config, need_existing_bids=config.conversion.seed_from_existing_bids)
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

    _wait_for_initial_resources(config)
    seed_staging(config)
    if shutil.which(config.tools.dcm2niix) is None and not Path(config.tools.dcm2niix).is_file():
        raise ConversionError(f"dcm2niix not found: {config.tools.dcm2niix}")
    if config.conversion.compression == "pigz" and not (
        shutil.which(config.tools.pigz) or Path(config.tools.pigz).is_file()
    ):
        raise ConversionError(f"pigz not found: {config.tools.pigz}")

    ensure_dataset_metadata(config)
    update_participants(config, records)
    resume_enabled = config.conversion.resume if resume is None else resume
    if resume_enabled:
        recover_stale_states(config.paths.audit_root)
    grouped: dict[str, list[SelectionRow]] = defaultdict(list)
    for selection in selected_or_review:
        grouped[selection.subject_id].append(selection)
    jobs: deque[tuple[list[SeriesRecord], list[SelectionRow]]] = deque()
    results: list[ConversionResult] = []
    for subject_id in sorted(grouped):
        subject_selections = sorted(
            grouped[subject_id],
            key=lambda row: (row.candidate_type, row.series_uid_hash),
        )
        subject_records = [
            by_hash[row.series_uid_hash]
            for row in subject_selections
            if row.series_uid_hash in by_hash
        ]
        missing = [row for row in subject_selections if row.series_uid_hash not in by_hash]
        for row in missing:
            update_series_state(
                config.paths.audit_root,
                row.series_uid_hash,
                "failed",
                subject_id=row.subject_id,
                candidate_type=row.candidate_type,
                worker_pid=os.getpid(),
                child_pid=None,
                finished_at=utc_now(),
                message="series missing from private inventory",
            )
            results.append(
                ConversionResult(
                    row.subject_id,
                    row.series_uid_hash,
                    row.candidate_type,
                    "failed",
                    "inventory",
                    message="series missing from private inventory",
                )
            )
        if subject_records:
            jobs.append((subject_records, subject_selections))
            for row in subject_selections:
                state = read_json(series_state_path(config.paths.audit_root, row.series_uid_hash))
                preserved_terminal = state.get("stage") in {
                    "converted",
                    "review_ready",
                    "failed",
                }
                live_owner = state.get("stage") in {
                    "linking",
                    "dcm2niix",
                    "compressing",
                    "validating",
                    "installing",
                } and process_is_alive(state.get("worker_pid"))
                if not (resume_enabled and (preserved_terminal or live_owner)):
                    update_series_state(
                        config.paths.audit_root,
                        row.series_uid_hash,
                        "queued",
                        subject_id=row.subject_id,
                        candidate_type=row.candidate_type,
                        mode=("selected" if row.decision_status == "selected" else "review"),
                        worker_pid=None,
                        child_pid=None,
                    )

    worker_count = workers or config.conversion.workers
    if worker_count < 1:
        raise ConversionError("workers must be at least 1")
    update_run_state(
        config,
        "converting",
        total_subjects=len(jobs),
        total_series=len(selected_or_review),
        series_uid_hashes=sorted(row.series_uid_hash for row in selected_or_review),
        workers=worker_count,
        completed_subjects=0,
        conversion_started_at=utc_now(),
    )
    diff_rows: list[dict[str, str]] = []
    completed_subjects = 0
    sampler = ResourceSampler(config)
    sampler.start()
    try:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            pending: dict[Any, list[SeriesRecord]] = {}
            while jobs or pending:
                while jobs and len(pending) < worker_count:
                    try:
                        snapshot = resource_snapshot(config)
                        blockers = resource_blockers(config, snapshot)
                    except OSError as exc:
                        blockers = [f"resource probe failed: {exc}"]
                    if blockers:
                        update_run_state(
                            config,
                            "paused_resources",
                            resource_blockers=blockers,
                            completed_subjects=completed_subjects,
                        )
                        break
                    subject_records, subject_selections = jobs.popleft()
                    future = executor.submit(
                        _convert_subject,
                        config,
                        subject_records,
                        subject_selections,
                        resume_enabled,
                        retry_failed,
                    )
                    pending[future] = subject_records
                if not pending:
                    time.sleep(config.runtime.status_interval_seconds)
                    continue
                done, _ = wait(
                    pending,
                    timeout=config.runtime.status_interval_seconds,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    subject_records = pending.pop(future)
                    try:
                        subject_results, subject_diffs = future.result()
                    except Exception as exc:
                        subject_results = []
                        for record in subject_records:
                            update_series_state(
                                config.paths.audit_root,
                                record.series_uid_hash,
                                "failed",
                                subject_id=record.subject_id,
                                candidate_type=record.candidate_type,
                                worker_pid=None,
                                child_pid=None,
                                finished_at=utc_now(),
                                message=f"subject worker failed: {exc}",
                            )
                            subject_results.append(
                                ConversionResult(
                                    record.subject_id,
                                    record.series_uid_hash,
                                    record.candidate_type,
                                    "failed",
                                    "worker",
                                    message=str(exc),
                                )
                            )
                        subject_diffs = []
                    results.extend(subject_results)
                    diff_rows.extend(subject_diffs)
                    completed_subjects += 1
                    write_conversion_results(
                        config.paths.audit_root / "conversion_status.tsv", results
                    )
                    _write_diff(config.paths.audit_root / "old_vs_v4_diff.tsv", diff_rows)
                    update_run_state(
                        config,
                        "converting",
                        completed_subjects=completed_subjects,
                        resource_blockers=[],
                    )
    finally:
        sampler.stop()
    results.sort(key=lambda row: (row.subject_id, row.candidate_type, row.series_uid_hash))
    write_conversion_results(config.paths.audit_root / "conversion_status.tsv", results)
    _write_diff(config.paths.audit_root / "old_vs_v4_diff.tsv", diff_rows)
    failed = sum(result.status == "failed" for result in results)
    update_run_state(
        config,
        "conversion_completed" if not failed else "conversion_completed_with_failures",
        completed_subjects=completed_subjects,
        failed_series=failed,
    )
    return results


def _wait_for_initial_resources(config: ProjectConfig) -> None:
    while True:
        try:
            blockers = resource_blockers(config, resource_snapshot(config))
        except OSError as exc:
            blockers = [f"resource probe failed: {exc}"]
        if not blockers:
            return
        update_run_state(config, "paused_resources", resource_blockers=blockers)
        time.sleep(config.runtime.status_interval_seconds)


def _convert_subject(
    config: ProjectConfig,
    records: list[SeriesRecord],
    selections: list[SelectionRow],
    resume: bool,
    retry_failed: bool,
) -> tuple[list[ConversionResult], list[dict[str, str]]]:
    by_hash = {record.series_uid_hash: record for record in records}
    results: list[ConversionResult] = []
    diffs: list[dict[str, str]] = []
    for selection in selections:
        record = by_hash.get(selection.series_uid_hash)
        if record is None:
            continue
        if (
            visual_qc_enabled(config)
            and selection.decision_status == "selected"
            and not applied_choice(config, record)
        ):
            selection = replace(selection, decision_status="review", reason="awaiting_visual_qc")
        previous = read_json(series_state_path(config.paths.audit_root, record.series_uid_hash))
        expected = (
            config.paths.staging_bids_root
            / f"sub-{record.subject_id}"
            / "anat"
            / f"{selection.output_basename}.nii.gz"
        )
        resumed = _resume_result(
            previous, record, selection, resume, retry_failed, expected_output=expected
        )
        if resumed is not None:
            results.append(resumed)
            continue
        try:
            candidate = _resume_result(
                previous, record, replace(selection, decision_status="review"), resume, retry_failed
            )
            if (
                previous.get("stage") == "review_ready"
                and candidate is not None
                and candidate.status == "skipped"
                and selection.decision_status == "selected"
            ):
                result, new_diffs = _promote_review(config, record, selection, candidate)
            else:
                result, new_diffs = _convert_one(config, record, selection)
            results.append(result)
            diffs.extend(new_diffs)
        except Exception as exc:
            finished = utc_now()
            state = update_series_state(
                config.paths.audit_root,
                record.series_uid_hash,
                "failed",
                subject_id=record.subject_id,
                candidate_type=record.candidate_type,
                worker_pid=os.getpid(),
                child_pid=None,
                finished_at=finished,
                message=str(exc),
            )
            results.append(
                ConversionResult(
                    record.subject_id,
                    record.series_uid_hash,
                    record.candidate_type,
                    "failed",
                    "conversion",
                    message=str(exc),
                    worker_pid=os.getpid(),
                    started_at=str(state.get("started_at", "")),
                    finished_at=finished,
                    log_path=str(state.get("log_path", "")),
                )
            )
    return results, diffs


def _resume_result(
    previous: dict[str, Any],
    record: SeriesRecord,
    selection: SelectionRow,
    resume: bool,
    retry_failed: bool,
    *,
    expected_output: Path | None = None,
) -> ConversionResult | None:
    if not resume or not previous:
        return None
    stage = str(previous.get("stage", ""))
    output = Path(str(previous.get("output_path", "")))
    checksum = str(previous.get("output_sha256", ""))
    sidecar = Path(str(previous.get("sidecar_path", "")))
    sidecar_checksum = str(previous.get("sidecar_sha256", ""))
    if (
        stage in {"converted", "review_ready"}
        and (previous.get("subject_id", record.subject_id) == record.subject_id)
        and (previous.get("series_uid_hash", record.series_uid_hash) == record.series_uid_hash)
        and (
            (
                stage == "converted"
                and selection.decision_status == "selected"
                and (expected_output is None or output.resolve() == expected_output.resolve())
            )
            or (stage == "review_ready" and selection.decision_status == "review")
        )
        and output.is_file()
        and checksum
        and _sha256(output) == checksum
        and sidecar.is_file()
        and sidecar_checksum
        and _sha256(sidecar) == sidecar_checksum
    ):
        return ConversionResult(
            record.subject_id,
            record.series_uid_hash,
            record.candidate_type,
            "skipped",
            "resume",
            output_path=str(output),
            message=f"verified prior {stage} output",
            output_sha256=checksum,
            sidecar_path=str(sidecar),
            sidecar_sha256=sidecar_checksum,
            worker_pid=os.getpid(),
            started_at=str(previous.get("started_at", "")),
            finished_at=str(previous.get("finished_at", "")),
            log_path=str(previous.get("log_path", "")),
        )
    if stage == "failed" and not retry_failed:
        return ConversionResult(
            record.subject_id,
            record.series_uid_hash,
            record.candidate_type,
            "failed",
            "preserved_failure",
            message=str(previous.get("message", "prior failure; use --retry-failed")),
            worker_pid=os.getpid(),
            started_at=str(previous.get("started_at", "")),
            finished_at=str(previous.get("finished_at", "")),
            log_path=str(previous.get("log_path", "")),
        )
    active_stages = {"linking", "dcm2niix", "compressing", "validating", "installing"}
    if stage in active_stages and process_is_alive(previous.get("worker_pid")):
        return ConversionResult(
            record.subject_id,
            record.series_uid_hash,
            record.candidate_type,
            "skipped",
            "active_worker",
            message="another live worker owns this series",
            worker_pid=int(previous["worker_pid"]),
            child_pid=previous.get("child_pid"),
        )
    return None


def _promote_review(
    config: ProjectConfig, record: SeriesRecord, selection: SelectionRow, prior: ConversionResult
) -> tuple[ConversionResult, list[dict[str, str]]]:
    image, sidecar = Path(prior.output_path), Path(prior.sidecar_path)
    _validate_converted_pair(image, sidecar, record)
    installed, diffs = _install_selected(config, record, selection, image, sidecar)
    installed_json = installed.with_name(installed.name.removesuffix(".nii.gz") + ".json")
    update_scans(config, record, selection, installed)
    result = replace(
        prior,
        status="converted",
        mode="promoted_review",
        output_path=str(installed),
        sidecar_path=str(installed_json),
        finished_at=utc_now(),
    )
    update_series_state(
        config.paths.audit_root,
        record.series_uid_hash,
        "converted",
        subject_id=record.subject_id,
        candidate_type=record.candidate_type,
        output_path=str(installed),
        sidecar_path=str(installed_json),
        output_sha256=prior.output_sha256,
        sidecar_sha256=prior.sidecar_sha256,
        finished_at=result.finished_at,
        child_pid=None,
    )
    return result, diffs


def _convert_one(
    config: ProjectConfig,
    record: SeriesRecord,
    selection: SelectionRow,
    *,
    preview_only: bool = False,
) -> tuple[ConversionResult, list[dict[str, str]]]:
    started_at = utc_now()
    started_clock = time.monotonic()
    work_root = config.work_root / f"sub-{record.subject_id}"
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
        update_series_state(
            config.paths.audit_root,
            record.series_uid_hash,
            "linking",
            subject_id=record.subject_id,
            candidate_type=record.candidate_type,
            worker_pid=os.getpid(),
            child_pid=None,
            started_at=started_at,
            mode=("selected" if selection.decision_status == "selected" else "review"),
        )
        _link_series(source_paths, input_dir)
        direct = _run_dcm2niix(config, input_dir, output_dir, record, "direct", log_root)
        mode = "direct"
        if direct is None:
            fixed_dir = temp_root / "fixed"
            fixed_dir.mkdir()
            repair_actions = _prepare_fallback_series(
                source_paths,
                fixed_dir,
                config=config,
                record=record,
                log_root=log_root,
            )
            repair_log = log_root / (f"sub-{record.subject_id}_{record.series_uid_hash}_repair.log")
            repair_log.write_text("\n".join(repair_actions) + "\n", encoding="utf-8")
            _clear_directory(output_dir)
            direct = _run_dcm2niix(config, fixed_dir, output_dir, record, "fallback", log_root)
            mode = "fallback"
        if direct is None:
            raise ConversionError("direct and fallback dcm2niix conversion failed")
        nifti_path, json_path = direct
        update_series_state(
            config.paths.audit_root,
            record.series_uid_hash,
            "validating",
            worker_pid=os.getpid(),
            child_pid=None,
        )
        if preview_only:
            from .qc_images import check_image

            if selection.decision_status != "review":
                raise ConversionError("preview-only conversion cannot install a selected image")
            check_image(nifti_path)
        else:
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
            update_series_state(
                config.paths.audit_root,
                record.series_uid_hash,
                "installing",
                worker_pid=os.getpid(),
            )
            _atomic_copy(nifti_path, target_nii)
            _atomic_copy(json_path, target_json)
            finished_at = utc_now()
            checksum = _sha256(target_nii)
            sidecar_checksum = _sha256(target_json)
            log_path = log_root / (f"sub-{record.subject_id}_{record.series_uid_hash}_{mode}.log")
            update_series_state(
                config.paths.audit_root,
                record.series_uid_hash,
                "review_ready",
                worker_pid=os.getpid(),
                child_pid=None,
                finished_at=finished_at,
                elapsed_seconds=time.monotonic() - started_clock,
                output_path=str(target_nii),
                output_sha256=checksum,
                sidecar_path=str(target_json),
                sidecar_sha256=sidecar_checksum,
                log_path=str(log_path),
                message=selection.reason,
            )
            return (
                ConversionResult(
                    record.subject_id,
                    record.series_uid_hash,
                    record.candidate_type,
                    "review_ready",
                    mode,
                    str(target_nii),
                    selection.reason,
                    output_sha256=checksum,
                    sidecar_path=str(target_json),
                    sidecar_sha256=sidecar_checksum,
                    worker_pid=os.getpid(),
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=time.monotonic() - started_clock,
                    log_path=str(log_path),
                ),
                [],
            )

        update_series_state(
            config.paths.audit_root,
            record.series_uid_hash,
            "installing",
            worker_pid=os.getpid(),
            child_pid=None,
        )
        installed, diffs = _install_selected(config, record, selection, nifti_path, json_path)
        update_scans(config, record, selection, installed)
        finished_at = utc_now()
        checksum = _sha256(installed)
        installed_json = installed.with_name(installed.name.removesuffix(".nii.gz") + ".json")
        sidecar_checksum = _sha256(installed_json)
        log_path = log_root / f"sub-{record.subject_id}_{record.series_uid_hash}_{mode}.log"
        elapsed = time.monotonic() - started_clock
        update_series_state(
            config.paths.audit_root,
            record.series_uid_hash,
            "converted",
            worker_pid=os.getpid(),
            child_pid=None,
            finished_at=finished_at,
            elapsed_seconds=elapsed,
            output_path=str(installed),
            output_sha256=checksum,
            sidecar_path=str(installed_json),
            sidecar_sha256=sidecar_checksum,
            log_path=str(log_path),
            message=selection.reason,
        )
        return (
            ConversionResult(
                record.subject_id,
                record.series_uid_hash,
                record.candidate_type,
                "converted",
                mode,
                str(installed),
                selection.reason,
                output_sha256=checksum,
                sidecar_path=str(installed_json),
                sidecar_sha256=sidecar_checksum,
                worker_pid=os.getpid(),
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                log_path=str(log_path),
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
    compression = "n" if config.conversion.compression == "pigz" else "y"
    command = [
        config.tools.dcm2niix,
        "-b",
        "y",
        "-ba",
        "y" if config.conversion.anonymize_sidecars else "n",
        "-z",
        compression,
        "--progress",
        "y",
        "-o",
        str(output_dir),
        "-f",
        "converted",
        str(input_dir),
    ]
    log_path = log_root / f"sub-{record.subject_id}_{record.series_uid_hash}_{mode}.log"
    returncode = _stream_command(
        command,
        log_path,
        config,
        record,
        "dcm2niix",
    )
    nifti = sorted(output_dir.glob("*.nii.gz")) or sorted(output_dir.glob("*.nii"))
    sidecars = sorted(output_dir.glob("*.json"))
    if returncode != 0 or len(nifti) != 1 or len(sidecars) != 1:
        return None
    nifti_path = nifti[0]
    if config.conversion.compression == "pigz":
        if nifti_path.suffix != ".nii":
            return None
        pigz_log = log_root / (f"sub-{record.subject_id}_{record.series_uid_hash}_{mode}_pigz.log")
        returncode = _stream_command(
            [
                config.tools.pigz,
                "-p",
                str(config.conversion.compression_threads),
                "-f",
                str(nifti_path),
            ],
            pigz_log,
            config,
            record,
            "compressing",
        )
        nifti_path = Path(f"{nifti_path}.gz")
        if returncode != 0 or not nifti_path.is_file():
            return None
    return nifti_path, sidecars[0]


def _stream_command(
    command: list[str],
    log_path: Path,
    config: ProjectConfig,
    record: SeriesRecord,
    stage: str,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"command={json.dumps(command)}\n")
        log.flush()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        update_series_state(
            config.paths.audit_root,
            record.series_uid_hash,
            stage,
            worker_pid=os.getpid(),
            child_pid=process.pid,
            log_path=str(log_path),
            last_tool=Path(command[0]).name,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            match = re.search(r"(?<!\d)(\d{1,3})%", line)
            if match:
                update_series_state(
                    config.paths.audit_root,
                    record.series_uid_hash,
                    stage,
                    worker_pid=os.getpid(),
                    child_pid=process.pid,
                    progress_percent=min(100, int(match.group(1))),
                    last_output=line.strip()[-500:],
                )
        returncode = process.wait()
        log.write(f"\nreturncode={returncode}\n")
    update_series_state(
        config.paths.audit_root,
        record.series_uid_hash,
        stage,
        worker_pid=os.getpid(),
        child_pid=None,
        returncode=returncode,
    )
    return returncode


def _prepare_fallback_series(
    source_paths: list[Path],
    destination: Path,
    *,
    config: ProjectConfig | None = None,
    record: SeriesRecord | None = None,
    log_root: Path | None = None,
) -> list[str]:
    actions: list[str] = []
    for index, source in enumerate(source_paths, start=1):
        output = destination / f"instance-{index:06d}.dcm"
        dataset = pydicom.dcmread(source, force=True)
        transfer_syntax = _transfer_syntax(dataset)
        if transfer_syntax and getattr(transfer_syntax, "is_compressed", False):
            if str(transfer_syntax) in JPEG2000_TRANSFER_SYNTAXES:
                if config is not None and record is not None:
                    update_series_state(
                        config.paths.audit_root,
                        record.series_uid_hash,
                        "dcm2niix",
                        worker_pid=os.getpid(),
                        child_pid=None,
                        last_tool="python-gdcm",
                    )
                try:
                    dataset.decompress(decoding_plugin="gdcm")
                except TypeError:
                    dataset.decompress(handler_name="gdcm")
                except Exception as exc:
                    raise ConversionError(
                        f"GDCM decompression failed for {transfer_syntax}: {exc}"
                    ) from exc
                actions.append(f"decompressed\t{index:06d}\t{transfer_syntax}\tpython-gdcm")
                transfer_syntax = _transfer_syntax(dataset)
            else:
                decoder = decoder_command(str(transfer_syntax), source, output)
                if decoder is None:
                    raise ConversionError(
                        f"unsupported compressed transfer syntax: {transfer_syntax}"
                    )
                if shutil.which(decoder[0]) is None:
                    raise ConversionError(f"required decoder is missing: {decoder[0]}")
                if config is not None and record is not None and log_root is not None:
                    decoder_log = log_root / (
                        f"sub-{record.subject_id}_{record.series_uid_hash}_"
                        f"{decoder[0]}_{index:06d}.log"
                    )
                    returncode = _stream_command(
                        decoder,
                        decoder_log,
                        config,
                        record,
                        "dcm2niix",
                    )
                    error = f"inspect {decoder_log}"
                else:
                    completed = subprocess.run(decoder, capture_output=True, text=True, check=False)
                    returncode = completed.returncode
                    error = completed.stderr.strip()
                if returncode != 0:
                    raise ConversionError(f"decoder failed for {transfer_syntax}: {error}")
                dataset = pydicom.dcmread(output, force=True)
                actions.append(f"decompressed\t{index:06d}\t{transfer_syntax}\t{decoder[0]}")
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
            actions.append(f"removed_private_tags\t{index:06d}\t{type(first_error).__name__}")
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
    *,
    visual_apply: bool = False,
) -> tuple[Path, list[dict[str, str]]]:
    if visual_qc_enabled(config) and not visual_apply:
        raise ConversionError("visual QC is enabled; installation requires qc_viewer.py --apply")
    if not authorized_choice(config, record, nifti_path):
        raise ConversionError("visual QC approval is required for this exact candidate/image")
    if visual_qc_enabled(config):
        from .qc_state import candidate_id, digest, qc_root, read_decision

        rating = (
            read_decision(qc_root(config), record.subject_id)
            .get("candidates", {})
            .get(candidate_id(record), {})
        )
        if rating.get("sidecar_sha256") != digest(json_path):
            raise ConversionError("sidecar differs from the visually approved candidate")
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
    _write_tsv(path, keyed.values(), fields)

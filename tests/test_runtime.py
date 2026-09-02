from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import write_nifti

from gb_dicom2bids.config import (
    ConversionConfig,
    DatasetConfig,
    PathsConfig,
    ProjectConfig,
    RuntimeConfig,
    SelectionConfig,
    ToolsConfig,
)
from gb_dicom2bids.convert import (
    _convert_subject,
    _resume_result,
    _run_dcm2niix,
    _validate_converted_pair,
    _wait_for_initial_resources,
    seed_staging,
)
from gb_dicom2bids.models import ConversionResult, SelectionRow
from gb_dicom2bids.runtime import (
    recover_stale_states,
    resource_blockers,
    series_state_path,
    status_snapshot,
    update_run_state,
    update_series_state,
)


def _config(tmp_path: Path, *, seed: bool = True) -> ProjectConfig:
    paths = PathsConfig(
        tmp_path / "dicom",
        tmp_path / "existing",
        tmp_path / "staging",
        tmp_path / "audit",
        tmp_path / "work",
    )
    return ProjectConfig(
        paths,
        DatasetConfig(),
        SelectionConfig(),
        ConversionConfig(
            seed_from_existing_bids=seed,
            compression="pigz",
            compression_threads=2,
        ),
        ToolsConfig(),
        runtime=RuntimeConfig(
            minimum_work_free_gb=50,
            minimum_staging_free_gb=500,
            minimum_available_memory_gb=128,
        ),
    )


def _selection(record) -> SelectionRow:
    return SelectionRow(
        center=record.center,
        subject_id=record.subject_id,
        study_uid_hash=record.study_uid_hash,
        series_uid_hash=record.series_uid_hash,
        candidate_type=record.candidate_type,
        decision_status="selected",
        score=100,
        reason="synthetic",
        source_plane=record.plane,
        source_kind=record.source_kind,
        protocol_id=record.protocol_id,
        output_basename=f"sub-{record.subject_id}_T1w",
    )


def test_staging_copy_resumes_without_changing_source(tmp_path) -> None:
    config = _config(tmp_path)
    source = config.paths.existing_bids_root / "sub-001" / "file.txt"
    source.parent.mkdir(parents=True)
    source.write_text("authoritative", encoding="utf-8")
    seed_staging(config)
    destination = config.paths.staging_bids_root / "sub-001" / "file.txt"
    destination.write_text("corrupt", encoding="utf-8")
    state_path = config.paths.audit_root / "staging_seed.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "copying"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    seed_staging(config)
    assert destination.read_text(encoding="utf-8") == "authoritative"
    assert source.read_text(encoding="utf-8") == "authoritative"


def test_dead_worker_is_marked_interrupted(tmp_path) -> None:
    audit = tmp_path / "audit"
    update_series_state(audit, "series", "dcm2niix", worker_pid=999_999_999)
    assert recover_stale_states(audit) == 1
    state = json.loads(series_state_path(audit, "series").read_text(encoding="utf-8"))
    assert state["stage"] == "interrupted"


def test_resume_requires_existing_checksum_match(record_factory, tmp_path) -> None:
    output = tmp_path / "output.nii.gz"
    output.write_bytes(b"synthetic")
    sidecar = tmp_path / "output.json"
    sidecar.write_text("{}", encoding="utf-8")
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    sidecar_checksum = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    record = record_factory()
    result = _resume_result(
        {
            "stage": "converted",
            "output_path": str(output),
            "output_sha256": checksum,
            "sidecar_path": str(sidecar),
            "sidecar_sha256": sidecar_checksum,
        },
        record,
        _selection(record),
        True,
        False,
    )
    assert result is not None and result.status == "skipped"
    output.write_bytes(b"changed")
    assert (
        _resume_result(
            {
                "stage": "converted",
                "output_path": str(output),
                "output_sha256": checksum,
                "sidecar_path": str(sidecar),
                "sidecar_sha256": sidecar_checksum,
            },
            record,
            _selection(record),
            True,
            False,
        )
        is None
    )


def test_pigz_thread_limit_and_uncompressed_dcm2niix(monkeypatch, record_factory, tmp_path) -> None:
    config = _config(tmp_path, seed=False)
    commands = []

    def fake_stream(command, _log, _config, _record, stage):
        commands.append((stage, command))
        output_dir = tmp_path / "output"
        if stage == "dcm2niix":
            write_nifti(output_dir / "converted.nii")
            (output_dir / "converted.json").write_text(
                json.dumps({"ImageOrientationPatientDICOM": [1, 0, 0, 0, 1, 0]}),
                encoding="utf-8",
            )
        else:
            raw = output_dir / "converted.nii"
            with (
                raw.open("rb") as source,
                gzip.open(output_dir / "converted.nii.gz", "wb") as destination,
            ):
                shutil.copyfileobj(source, destination)
            raw.unlink()
        return 0

    monkeypatch.setattr("gb_dicom2bids.convert._stream_command", fake_stream)
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    log_dir = tmp_path / "logs"
    input_dir.mkdir()
    output_dir.mkdir()
    pair = _run_dcm2niix(config, input_dir, output_dir, record_factory(), "direct", log_dir)
    assert pair is not None
    _validate_converted_pair(pair[0], pair[1], record_factory())
    first = commands[0][1]
    second = commands[1][1]
    assert commands[0][0] == "dcm2niix" and first[first.index("-z") + 1] == "n"
    assert commands[1][0] == "compressing" and second[second.index("-p") + 1] == "2"


def test_resource_threshold_blockers(tmp_path) -> None:
    config = _config(tmp_path)
    blockers = resource_blockers(
        config,
        {"work_free_gb": 49, "staging_free_gb": 499, "memory_available_gb": 127},
    )
    assert len(blockers) == 3


def test_failed_state_is_preserved_until_retry_requested(record_factory) -> None:
    record = record_factory()
    previous = {"stage": "failed", "message": "synthetic failure"}
    kept = _resume_result(previous, record, _selection(record), True, False)
    assert kept is not None and kept.mode == "preserved_failure"
    assert _resume_result(previous, record, _selection(record), True, True) is None


def test_one_subject_candidates_run_sequentially(monkeypatch, record_factory, tmp_path) -> None:
    config = _config(tmp_path, seed=False)
    t1 = record_factory(series_uid_hash="t1")
    flair = record_factory(
        series_uid_hash="flair",
        candidate_type="flair",
        protocol_id="flair-test",
    )
    order = []

    def fake_convert(_config, record, selection):
        order.append(record.series_uid_hash)
        return (
            ConversionResult(
                record.subject_id,
                record.series_uid_hash,
                record.candidate_type,
                "converted",
                "synthetic",
            ),
            [],
        )

    monkeypatch.setattr("gb_dicom2bids.convert._convert_one", fake_convert)
    results, _ = _convert_subject(
        config,
        [t1, flair],
        [_selection(t1), _selection(flair)],
        False,
        False,
    )
    assert order == ["t1", "flair"]
    assert len(results) == 2


def test_status_completed_count_is_monotonic(tmp_path) -> None:
    config = _config(tmp_path, seed=False)
    started = (datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="seconds")
    update_run_state(config, "converting", total_series=2, started_at=started)
    update_series_state(config.paths.audit_root, "one", "queued")
    before = status_snapshot(config)["completed"]
    update_series_state(config.paths.audit_root, "one", "converted")
    after = status_snapshot(config)["completed"]
    assert before == 0
    assert after == 1
    assert status_snapshot(config)["eta_seconds"] is not None


def test_transient_resource_probe_failure_pauses_then_recovers(monkeypatch, tmp_path) -> None:
    config = _config(tmp_path, seed=False)
    calls = iter(
        (
            OSError("temporary NFS failure"),
            {"work_free_gb": 500, "staging_free_gb": 900, "memory_available_gb": 256},
        )
    )

    def fake_snapshot(_config):
        value = next(calls)
        if isinstance(value, OSError):
            raise value
        return value

    monkeypatch.setattr("gb_dicom2bids.convert.resource_snapshot", fake_snapshot)
    monkeypatch.setattr("gb_dicom2bids.convert.time.sleep", lambda _seconds: None)
    _wait_for_initial_resources(config)
    state = json.loads((config.paths.audit_root / "run_status.json").read_text())
    assert state["stage"] == "paused_resources"
    assert "temporary NFS failure" in state["resource_blockers"][0]

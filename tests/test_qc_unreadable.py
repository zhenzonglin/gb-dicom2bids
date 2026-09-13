"""Synthetic whole-round read failures; source failures never become reusable rules."""

import copy
import errno
import gzip

import pytest
from conftest import write_nifti
from test_qc_exclusions import make_missing, negative
from test_qc_identify import publish

from gb_dicom2bids.qc_features import run_features
from gb_dicom2bids.qc_learning import feature_rows
from gb_dicom2bids.qc_protocols import ProtocolIndex, source_path
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import digest, read_decision
from gb_dicom2bids.qc_unreadable import image_read_failure
from gb_dicom2bids.runtime import atomic_write_json, read_json


@pytest.mark.parametrize(
    "error,qualifies",
    [
        (ValueError("unsupported image dimensions"), True),
        (ValueError("invalid NIfTI affine"), True),
        (EOFError("truncated payload"), True),
        (gzip.BadGzipFile("invalid gzip"), True),
        (OSError("Expected 400 bytes, got 30 bytes"), True),
        (OSError(errno.EIO, "I/O error"), False),
        (OSError(errno.ESTALE, "Stale file handle"), False),
        (FileNotFoundError("NFS unavailable"), False),
        (PermissionError("not readable"), False),
        (TimeoutError("timeout"), False),
        (MemoryError(), False),
        (ValueError("image exceeds preview memory limit"), False),
        (RuntimeError("unexpected server failure"), False),
    ],
)
def test_only_known_image_failures_qualify(error, qualifies):
    assert image_read_failure(error) is qualifies


def fail_preview(service, uid, monkeypatch, error=None):
    with monkeypatch.context() as scoped:

        def broken(_path):
            raise error or ValueError("unsupported image dimensions")

        scoped.setattr(service.volumes, "metadata", broken)
        service._prepare_job(uid)
    assert service.jobs[uid]["state"] == "failed"


def test_all_remaining_unreadable_skip_locally_and_persist(tmp_path, monkeypatch):
    index, _ = make_missing(
        tmp_path,
        {"phantom01": ["contrast-A", "contrast-B"], "phantom02": ["contrast-A", "contrast-B"]},
    )
    before = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    service = ReviewService(index.config)
    try:
        identify = service.assistance().identification
        state = copy.deepcopy(identify.state)
        first, second = index.subjects["phantom01"]
        fail_preview(service, first, monkeypatch)
        assert not identify.unreadable_candidates("phantom01", "t1")
        fail_preview(service, second, monkeypatch)
        for modality in ("t1", "flair"):
            assert identify.summary()["counts"][modality]["unreadable_skipped"] == 1
            assert identify.summary()["counts"][modality]["pending_subjects"] == 1
            assert identify.exclusions().round("phantom01", modality) == []
            assert not identify.unreadable_candidates("phantom02", modality)
        assert identify.state == state
        assert identify.list_subjects({})["subjects"][0]["id"] == "phantom02"
        assert all(
            c["unreadable_excluded_modalities"] for c in service.subject("phantom01")["candidates"]
        )
    finally:
        service.close()
    fresh = ProtocolIndex(index.config).identification
    assert fresh.summary()["counts"]["t1"]["unreadable_skipped"] == 1
    assert all(digest(p) == value for p, value in before.items())
    assert all(not read_decision(index.root.parent, s)["candidates"] for s in index.subjects)
    assert not list(index.config.paths.staging_bids_root.rglob("*.nii*"))


@pytest.mark.parametrize(
    "protection",
    ["untried", "readable", "transient", "defer", "recheck", "image", "choice", "retained"],
)
def test_any_unresolved_or_manual_protection_prevents_skip(tmp_path, monkeypatch, protection):
    index, _ = make_missing(tmp_path, {"phantom01": ["T1 A", "T1 B"]})
    service = ReviewService(index.config)
    try:
        identify = service.assistance().identification
        first, second = index.subjects["phantom01"]
        if protection == "defer":
            payload = negative(identify)
            payload.update(negative_templates=[], deferred_candidates=[second])
            publish(identify, payload)
        if protection == "recheck":
            identify.state["recheck"] = {"phantom01:t1": {"candidates": [second]}}
        if protection == "retained":
            identify.state["manual_completed"] = {
                "phantom01:t1": {"stamp": identify.subject_stamp("phantom01"), "source": "manual"}
            }
        if protection == "image":
            identify.index.decisions["phantom01"]["candidates"][second] = {
                "quality": "pass",
                "modality": "t1",
            }
        if protection == "choice":
            identify.index.decisions["phantom01"]["groups"]["t1"] = {"choice": second}
        fail_preview(service, first, monkeypatch)
        if protection == "readable":
            service._prepare_job(second)
            assert service.jobs[second]["state"] == "ready"
        elif protection != "untried":
            error = OSError(errno.EIO, "I/O error") if protection == "transient" else None
            fail_preview(service, second, monkeypatch, error)
        assert not identify.unreadable_candidates("phantom01", "t1")
        assert identify.summary()["counts"]["t1"]["unreadable_skipped"] == 0
    finally:
        service.close()


def test_retry_clear_changed_source_and_legacy_failure_stay_pending(tmp_path, monkeypatch):
    index, _ = make_missing(tmp_path, {"phantom01": ["contrast-A"]})
    service = ReviewService(index.config)
    uid = index.subjects["phantom01"][0]
    try:
        identify = service.assistance().identification
        fail_preview(service, uid, monkeypatch)
        assert identify.unreadable_candidates("phantom01", "t1")
        with monkeypatch.context() as scoped:
            # Observe immediately after queueing, before the worker completes.
            scoped.setattr(service.executor, "submit", lambda *args: None)
            assert service.prepare(uid, retry=True)["state"] == "queued"
            assert not identify.unreadable_candidates("phantom01", "t1")
        service._prepare_job(uid)
        assert service.jobs[uid]["state"] == "ready"
        assert not ProtocolIndex(index.config).identification.unreadable_previews
        fail_preview(service, uid, monkeypatch)
        path = source_path(index.config, index.records[uid])
        write_nifti(path, shape=(17, 19, 21))
        assert not ProtocolIndex(index.config).identification.unreadable_previews
        error = service.root / "errors" / f"{uid}.json"
        atomic_write_json(error, {"state": "failed", "error": "unsupported image dimensions"})
        assert not ProtocolIndex(index.config).identification.unreadable_previews
        assert read_json(error)["state"] == "failed"
    finally:
        service.close()


def test_real_bad_dimensions_excluded_without_quality_or_rules(tmp_path):
    index, _ = make_missing(tmp_path, {"phantom01": ["T1 A", "T1 B"]})
    for uid in index.subjects["phantom01"]:
        write_nifti(source_path(index.config, index.records[uid]), shape=(8, 8, 1))
    service = ReviewService(index.config)
    try:
        identify = service.assistance().identification
        for uid in index.subjects["phantom01"]:
            service._prepare_job(uid)
            assert service.jobs[uid]["failure_kind"] == "unreadable_image"
        assert identify.summary()["pending_groups"] == 0
        assert service.assistance().choices("phantom01")["t1"]["choice"] is None
        assert service.assistance().choices("phantom01")["t1"]["count"] == 0
        assert not identify.state["templates"]
        assert not identify.state["absent"]
        assert not read_decision(index.root.parent, "phantom01")["candidates"]
        assert identify.list_subjects({"queue": "unreadable"})["total"] == 2
        assert identify.list_subjects({"queue": "unreadable", "q": "not-present"})["total"] == 0
        identify.transition({"revision": identify.state["revision"], "phase": "quality"})
        assert feature_rows(service.assistance()) == []
        assert run_features(service.assistance(), workers=1)["total"] == 0
        assert service.list_subjects({})["total"] == 0
        assert service.list_subjects({"others": "1"})["total"] == 1
    finally:
        service.close()


def test_failure_keeps_group_identity_and_allows_saved_hold(tmp_path, monkeypatch):
    index, _ = make_missing(tmp_path, {"phantom01": ["T1 A", "T1 B"]})
    service = ReviewService(index.config)
    try:
        identify = service.assistance().identification
        payload = negative(identify)
        uid = index.subjects["phantom01"][0]
        payload.update(negative_templates=[], deferred_candidates=[uid])
        group_id = payload["group"]
        for u in index.subjects["phantom01"]:
            fail_preview(service, u, monkeypatch)
        assert identify.group(group_id)["pending_count"] == 0
        # A draft started before the last read failed can still be published.
        publish(identify, payload)
        assert identify.summary()["counts"]["t1"]["unreadable_skipped"] == 0
        assert identify.summary()["counts"]["t1"]["pending_subjects"] == 1
        state = copy.deepcopy(identify.state)
        identify.retain_manual_completions(state, identify.catalogue()["groups"])
        assert "phantom01:flair" not in state["manual_completed"]
    finally:
        service.close()

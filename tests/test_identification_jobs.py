"""Durable nonblocking receipts, guarded publishing, and commit-gap recovery."""

import threading
import time
import uuid
from pathlib import Path

import pytest
from test_qc_exclusions import make_missing

from gb_dicom2bids.qc_identify import Identification, require_quality
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import BusyError, ConflictError, digest, read_decision, writer_lock
from gb_dicom2bids.runtime import atomic_write_json, read_json


def setup_jobs(tmp_path):
    index, _ = make_missing(
        tmp_path,
        {
            "phantom01": ["T1 A", "T1 B", "FLAIR"],
            "phantom02": ["T1 C", "T1 D", "FLAIR"],
        },
    )
    service = ReviewService(index.config)
    service.assistance().identification.catalogue()
    return service, service.identification_jobs()


def request(service, subject="phantom01"):
    identify = service.assistance().identification
    group = next(g for g in identify.subject_groups(subject) if g["modality"] == "t1")
    return {
        "request_id": str(uuid.uuid4()),
        "payload": {
            "subject": subject,
            "group": group["id"],
            "target_modality": "t1",
            "revision": identify.state["revision"],
            "basis": identify.edit_basis(group["id"]),
            "reviewer": "zhenzong",
            "templates": {
                entry["id"]: {"modality": "t1", "priority": i}
                for i, entry in enumerate(group["templates"])
            },
        },
    }


def wait_done(jobs, count=1):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = jobs.status()
        if not state["pending"] and len(state["recent"]) >= count:
            return state["recent"]
        threading.Event().wait(0.02)
    pytest.fail(f"background jobs did not finish: {jobs.status()}")


def test_durable_idempotent_receipt_then_disjoint_rebase(tmp_path):
    service, jobs = setup_jobs(tmp_path)
    before = {p: digest(p) for p in service.config.nifti_import.source_root.rglob("*.nii.gz")}
    first, second = request(service), request(service, "phantom02")
    try:
        with writer_lock(service.config):
            assert jobs.submit(first)["state"] == "queued"
            assert jobs.submit(first)["id"] == uuid.UUID(first["request_id"]).hex
            jobs.submit(second)
            assert len(jobs.status()["pending"]) == 2
            with pytest.raises(BusyError, match="后台"):
                require_quality(service.root / "assist")
            altered = {**first, "payload": {**first["payload"], "reviewer": "different"}}
            with pytest.raises(ConflictError):
                jobs.submit(altered)
        results = wait_done(jobs, 2)
        assert all(j["state"] == "completed" for j in results), results
        assert sorted(j["result"]["revision"] for j in results) == [2, 3]
        assert jobs.submit(first)["state"] == "completed"
        assert not service.assistance().identification.summary()["pending_groups"]
        assert all(not read_decision(service.root, s)["candidates"] for s in service.by_subject)
        assert all(digest(p) == value for p, value in before.items())
        assert not list(service.config.paths.staging_bids_root.rglob("*.nii*"))
    finally:
        service.close()


def test_slow_publisher_does_not_hold_viewer_lock(tmp_path, monkeypatch):
    service, jobs = setup_jobs(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = Identification.publish

    def slow(self, payload):
        entered.set()
        assert release.wait(10)
        return original(self, payload)

    monkeypatch.setattr(Identification, "publish", slow)
    try:
        jobs.submit(request(service))
        assert entered.wait(5)
        started = time.monotonic()
        assert service.subject("phantom02")["subject"] == "phantom02"
        assert time.monotonic() - started < 1
        assert jobs.status()["pending"][0]["state"] == "running"
        release.set()
        assert wait_done(jobs)[0]["state"] == "completed"
    finally:
        release.set()
        service.close()


def test_changed_related_scope_fails_without_overwrite(tmp_path):
    service, jobs = setup_jobs(tmp_path)
    first, second = request(service), request(service)
    for entry in second["payload"]["templates"].values():
        entry["priority"] += 10
    try:
        with writer_lock(service.config):
            jobs.submit(first)
            jobs.submit(second)
        results = wait_done(jobs, 2)
        assert sorted(j["state"] for j in results) == ["completed", "failed"]
        failed = next(j for j in results if j["state"] == "failed")
        assert "已改变" in failed["error"]
        assert (
            service.assistance().identification.state["templates"] == first["payload"]["templates"]
        )
    finally:
        service.close()


def test_commit_before_receipt_failure_never_double_publishes(tmp_path, monkeypatch):
    service, jobs = setup_jobs(tmp_path)
    original = jobs._finish
    attempts = []

    def interrupted(job, state, **extra):
        attempts.append(state)
        if len(attempts) == 1:
            raise OSError("synthetic receipt write failure")
        return original(job, state, **extra)

    monkeypatch.setattr(jobs, "_finish", interrupted)
    try:
        jobs.submit(request(service))
        result = wait_done(jobs)[0]
        assert result["state"] == "completed"
        assert result["result"]["recovered"] is True
        assert service.assistance().identification.state["revision"] == 2
    finally:
        service.close()


def test_pending_cleanup_failure_keeps_saved_result_visible(tmp_path, monkeypatch):
    service, jobs = setup_jobs(tmp_path)
    original = Path.unlink
    attempts = []

    def fail_once(path, *args, **kwargs):
        if path.parent == jobs.pending:
            attempts.append(path)
            if len(attempts) == 1:
                raise OSError("synthetic pending cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    try:
        jobs.submit(request(service))
        results = wait_done(jobs)
        assert len(results) == 1
        assert results[0]["state"] == "completed"
        assert len(attempts) >= 2
        assert service.assistance().identification.state["revision"] == 2
    finally:
        service.close()


def test_orphan_history_before_state_commit_is_not_reported_as_saved(tmp_path, monkeypatch):
    service, jobs = setup_jobs(tmp_path)
    import gb_dicom2bids.qc_identify as module

    original = module.atomic_write_json
    reject = [True]

    def fail_state(path, value):
        if path.name == "identification.json" and reject[0]:
            reject[0] = False
            raise OSError("synthetic state commit failure")
        return original(path, value)

    monkeypatch.setattr(module, "atomic_write_json", fail_state)
    try:
        first = jobs.submit(request(service))
        assert wait_done(jobs)[0]["state"] == "failed"
        assert service.assistance().identification.state["revision"] == 1
        jobs.submit(request(service))
        results = wait_done(jobs, 2)
        assert sorted(j["state"] for j in results) == ["completed", "failed"]
        assert service.assistance().identification.state["revision"] == 3
        prior = read_json(jobs.results / f"{first['id']}.json")
        assert jobs._committed(prior) is None
    finally:
        service.close()


@pytest.mark.parametrize("running", [False, True])
def test_restart_recovers_queued_or_reports_uncommitted_running(tmp_path, running):
    service, jobs = setup_jobs(tmp_path)
    jobs.close()
    raw = request(service)
    job = jobs.submit(raw)
    path = jobs.pending / f"{job['id']}.json"
    if running:
        atomic_write_json(path, dict(read_json(path), state="running"))
    service.close()
    fresh = ReviewService(service.config)
    try:
        resumed = fresh.identification_jobs()
        result = wait_done(resumed)[0]
        assert result["state"] == ("failed" if running else "completed")
        if running:
            assert "保存中断" in result["error"]
            assert not fresh.assistance().identification.state["templates"]
    finally:
        fresh.close()


def test_failed_validation_keeps_group_pending_and_quality_untouched(tmp_path):
    service, jobs = setup_jobs(tmp_path)
    raw = request(service)
    for entry in raw["payload"]["templates"].values():
        entry["priority"] = 0
    try:
        jobs.submit(raw)
        result = wait_done(jobs)[0]
        assert result["state"] == "failed"
        assert "同优先级" in result["error"]
        assert service.assistance().identification.summary()["pending_groups"] == 2
        assert not read_decision(service.root, "phantom01")["candidates"]
    finally:
        service.close()

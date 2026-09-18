"""Submission order survives equal timestamps, clock changes, and queue recovery."""

import uuid

import pytest
from test_identification_jobs import request, setup_jobs, wait_done

import gb_dicom2bids.qc_jobs as module
from gb_dicom2bids.qc_jobs import IdentificationJobs
from gb_dicom2bids.qc_state import ConflictError
from gb_dicom2bids.runtime import atomic_write_json, read_json


@pytest.mark.parametrize("second_time", ["2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"])
def test_fifo_wins_over_uuid_and_wall_clock_after_restart(tmp_path, monkeypatch, second_time):
    service, jobs = setup_jobs(tmp_path)
    jobs.close()
    first, second = request(service), request(service)
    first["request_id"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    second["request_id"] = "00000000-0000-4000-8000-000000000000"
    for entry in second["payload"]["templates"].values():
        entry["priority"] += 10
    resumed = None
    try:
        monkeypatch.setattr(module, "utc_now", lambda: "2026-01-02T00:00:00Z")
        first_receipt = jobs.submit(first)
        monkeypatch.setattr(module, "utc_now", lambda: second_time)
        second_receipt = jobs.submit(second)
        assert [j["id"] for j in jobs.status()["pending"]] == [
            first_receipt["id"],
            second_receipt["id"],
        ]
        resumed = IdentificationJobs(service)
        results = {j["id"]: j for j in wait_done(resumed, 2)}
        assert results[first_receipt["id"]]["state"] == "completed"
        assert results[second_receipt["id"]]["state"] == "failed"
        assert "已改变" in results[second_receipt["id"]]["error"]
        assert (
            service.assistance().identification.state["templates"] == first["payload"]["templates"]
        )
    finally:
        if resumed:
            resumed.close()
        service.close()


def test_sequence_persists_and_duplicate_requests_do_not_advance_it(tmp_path, monkeypatch):
    service, jobs = setup_jobs(tmp_path)
    jobs.close()
    fresh = None
    try:
        first = request(service)
        assert jobs.submit(first)["queue_sequence"] == 1
        assert jobs.submit(first)["queue_sequence"] == 1
        altered = {**first, "payload": {**first["payload"], "reviewer": "another"}}
        with pytest.raises(ConflictError):
            jobs.submit(altered)
        # A fresh queue object reads the durable counter, not a process-local count.
        with monkeypatch.context() as stopped:
            stopped.setattr(IdentificationJobs, "_loop", lambda self: None)
            fresh = IdentificationJobs(service)
        assert fresh.submit(request(service, "phantom02"))["queue_sequence"] == 2
        assert read_json(jobs.root / "sequence.json") == {"last_sequence": 2}
    finally:
        if fresh:
            fresh.close()
        service.close()


def test_legacy_pending_precedes_new_jobs_without_rewriting_payload(tmp_path, monkeypatch):
    service, jobs = setup_jobs(tmp_path)
    jobs.close()
    resumed = None
    try:
        legacy = jobs.submit(request(service))
        path = jobs.pending / f"{legacy['id']}.json"
        original = read_json(path)
        original.pop("queue_sequence")
        original["submitted_at"] = "2026-12-31T00:00:00Z"
        atomic_write_json(path, original)
        monkeypatch.setattr(module, "utc_now", lambda: "2026-01-01T00:00:00Z")
        new = jobs.submit(request(service, "phantom02"))
        assert [j["id"] for j in jobs.status()["pending"]] == [legacy["id"], new["id"]]
        assert read_json(path) == original
        resumed = IdentificationJobs(service)
        results = {j["id"]: j for j in wait_done(resumed, 2)}
        assert results[legacy["id"]]["result"]["revision"] == 2
        assert results[new["id"]]["result"]["revision"] == 3
        assert read_json(jobs.results / path.name)["payload"] == original["payload"]
    finally:
        if resumed:
            resumed.close()
        service.close()


@pytest.mark.parametrize("saved_directory", ["pending", "results"])
def test_missing_counter_bootstraps_from_saved_sequence(tmp_path, saved_directory):
    service, jobs = setup_jobs(tmp_path)
    jobs.close()
    try:
        # Synthetic pre-existing receipt; no counter has been created yet.
        atomic_write_json(
            jobs.root / saved_directory / f"{uuid.uuid4().hex}.json",
            {"queue_sequence": 41},
        )
        assert jobs.submit(request(service))["queue_sequence"] == 42
    finally:
        service.close()


@pytest.mark.parametrize("counter", [{}, {"last_sequence": -1}, {"last_sequence": True}])
def test_invalid_counter_never_silently_resets_order(tmp_path, counter):
    service, jobs = setup_jobs(tmp_path)
    jobs.close()
    try:
        atomic_write_json(jobs.root / "sequence.json", counter)
        with pytest.raises(ValueError, match="序号"):
            jobs.submit(request(service))
        assert not list(jobs.pending.glob("*.json"))
        assert read_json(jobs.root / "sequence.json") == counter
    finally:
        service.close()


def test_failed_receipt_write_leaves_safe_gap_and_retry_can_resume(tmp_path, monkeypatch):
    service, jobs = setup_jobs(tmp_path)
    jobs.close()
    original = module.atomic_write_json
    failed = False

    def interrupt(path, value):
        nonlocal failed
        if path.parent == jobs.pending and not failed:
            failed = True
            raise OSError("synthetic receipt failure")
        original(path, value)

    monkeypatch.setattr(module, "atomic_write_json", interrupt)
    try:
        raw = request(service)
        with pytest.raises(OSError, match="receipt failure"):
            jobs.submit(raw)
        assert read_json(jobs.root / "sequence.json") == {"last_sequence": 1}
        assert jobs.submit(raw)["queue_sequence"] == 2
        assert jobs.submit(raw)["queue_sequence"] == 2
        assert len(jobs.status()["pending"]) == 1
    finally:
        service.close()
